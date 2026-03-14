"""
Thread-safe state store.

Trading thread  → update() / append_series()  (called from asyncio worker thread)
UI thread       → get_snapshot()              (called from QTimer every 125 ms)

Only one lock acquisition per tick; get_snapshot() copies all data under lock so
the UI thread never holds the lock while doing Qt work.
"""
import threading
import time
from collections import deque
from typing import Optional

from telemetry.state import BotState, FillEvent, OrderRow, TapeTradeEvent

_SERIES_LEN      = 300   # ~5 minutes at 1-second ticks
_FILL_LEN        = 200   # max bot-fill events (passive + aggressive)
_TAPE_TRADE_LEN  = 1000  # max public tape trades (~1.5 min of a 10 TPM market)


class BotStateStore:
    def __init__(self, series_len: int = _SERIES_LEN) -> None:
        self._lock = threading.Lock()
        self._s = BotState()

        # Rolling buffers kept here; only lists are exposed to UI via snapshot.
        # _tick_buf stores the heartbeat tick number for each series sample so
        # the UI can use absolute ticks as X-axis coordinates (no drift).
        self._tick_buf:    deque = deque(maxlen=series_len)   # heartbeat tick per sample
        self._price_buf:   deque = deque(maxlen=series_len)
        self._bid_buf:     deque = deque(maxlen=series_len)   # our quoted bid
        self._ask_buf:     deque = deque(maxlen=series_len)   # our quoted ask
        self._mkt_bid_buf: deque = deque(maxlen=series_len)   # market best bid
        self._mkt_ask_buf: deque = deque(maxlen=series_len)   # market best ask
        self._inv_buf:     deque = deque(maxlen=series_len)
        self._pnl_buf:     deque = deque(maxlen=series_len)
        self._ret_buf:     deque = deque(maxlen=series_len)
        self._ofi_buf:     deque = deque(maxlen=series_len)   # OFI signal per tick
        self._res_buf:     deque = deque(maxlen=series_len)   # reservation_price per tick

        # Fill events for microstructure chart markers
        self._fill_buf:       deque = deque(maxlen=_FILL_LEN)
        # Public tape trade events for microstructure chart squares
        self._tape_trade_buf: deque = deque(maxlen=_TAPE_TRADE_LEN)

        self._start_ts: float = time.time()

    # ──────────────────────────────────────────────────────────────────────────
    # Writer API  (trading thread)
    # ──────────────────────────────────────────────────────────────────────────

    def update(self, **kwargs) -> None:
        """Atomically patch any BotState scalar fields."""
        with self._lock:
            s = self._s
            for k, v in kwargs.items():
                if hasattr(s, k):
                    setattr(s, k, v)
            s.last_update = time.time()
            s.uptime_sec = time.time() - self._start_ts

    def append_series(
        self,
        tick: Optional[int] = None,
        price: Optional[float] = None,
        our_bid: Optional[float] = None,
        our_ask: Optional[float] = None,
        mkt_bid: Optional[float] = None,
        mkt_ask: Optional[float] = None,
        inventory: Optional[float] = None,
        pnl: Optional[float] = None,
        ofi_signal: Optional[float] = None,
        reservation: Optional[float] = None,
    ) -> None:
        """Append one data point to the rolling series buffers.

        ``tick`` must be passed on every call so that tick_series stays in
        sync with price_series (same length, same index mapping).  The UI
        uses tick_series as the X-axis so that event markers (which carry
        their own absolute tick) are always aligned with the lines.
        """
        with self._lock:
            if price is not None:
                if self._price_buf:
                    prev = self._price_buf[-1]
                    ret = (price - prev) / prev if prev else 0.0
                    self._ret_buf.append(ret)
                self._price_buf.append(price)
                # Always keep tick_buf in sync with price_buf.
                if tick is not None:
                    self._tick_buf.append(tick)
            if our_bid is not None:
                self._bid_buf.append(our_bid)
            if our_ask is not None:
                self._ask_buf.append(our_ask)
            if mkt_bid is not None:
                self._mkt_bid_buf.append(mkt_bid)
            if mkt_ask is not None:
                self._mkt_ask_buf.append(mkt_ask)
            if inventory is not None:
                self._inv_buf.append(inventory)
            if pnl is not None:
                self._pnl_buf.append(pnl)
            if ofi_signal is not None:
                self._ofi_buf.append(ofi_signal)
            if reservation is not None:
                self._res_buf.append(reservation)

    def record_fill_event(self, event: FillEvent) -> None:
        """Record a bot fill event for the microstructure chart."""
        with self._lock:
            self._fill_buf.append(event)

    def record_tape_trade(self, event: TapeTradeEvent) -> None:
        """Record a public tape trade for the microstructure chart squares."""
        with self._lock:
            self._tape_trade_buf.append(event)

    # ──────────────────────────────────────────────────────────────────────────
    # Reader API  (UI thread)
    # ──────────────────────────────────────────────────────────────────────────

    def get_snapshot(self) -> BotState:
        """
        Returns a fully-independent copy of current state.
        The caller owns the returned object; no lock is held after return.
        """
        with self._lock:
            s = self._s
            return BotState(
                selected_market=s.selected_market,
                mid_price=s.mid_price,
                bid=s.bid,
                ask=s.ask,
                spread_bps=s.spread_bps,
                tpm=s.tpm,
                inventory=s.inventory,
                avg_price=s.avg_price,
                cash_usd=s.cash_usd,
                realized_pnl=s.realized_pnl,
                unrealized_pnl=s.unrealized_pnl,
                total_pnl=s.total_pnl,
                total_trades=s.total_trades,
                total_volume=s.total_volume,
                total_notional=s.total_notional,
                buy_volume=s.buy_volume,
                sell_volume=s.sell_volume,
                total_edge_collected=s.total_edge_collected,
                norm_inventory=s.norm_inventory,
                ofi_signal=s.ofi_signal,
                hedge_count=s.hedge_count,
                ticks=s.ticks,
                active_orders=list(s.active_orders),   # shallow copy of list of frozen rows
                tick_series=list(self._tick_buf),
                price_series=list(self._price_buf),
                bid_series=list(self._bid_buf),
                ask_series=list(self._ask_buf),
                mkt_bid_series=list(self._mkt_bid_buf),
                mkt_ask_series=list(self._mkt_ask_buf),
                ofi_series=list(self._ofi_buf),
                reservation_series=list(self._res_buf),
                inventory_series=list(self._inv_buf),
                pnl_series=list(self._pnl_buf),
                returns_series=list(self._ret_buf),
                fill_events=list(self._fill_buf),
                tape_trade_events=list(self._tape_trade_buf),
                is_running=s.is_running,
                last_update=s.last_update,
                uptime_sec=s.uptime_sec,
            )
