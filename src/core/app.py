import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .logger import setup_fill_logger, spawn_fill_monitor_window

try:
    from telemetry.store import BotStateStore
    from telemetry.state import FillEvent, OrderRow, TapeTradeEvent
except ImportError:
    BotStateStore = None    # type: ignore[assignment,misc]
    FillEvent = None        # type: ignore[assignment,misc]
    OrderRow = None         # type: ignore[assignment,misc]
    TapeTradeEvent = None   # type: ignore[assignment,misc]
from src.venues.extended_multi import ExtendedMulti
from src.venues.extended_rest import ExtendedRESTClient
from src.selection.market_selector import MarketSnapshot, SelectorConfig, select_markets
from src.sim.paper_mm import (
    PaperMM, TradeStats, ExecutionTape, OrderLadder,
    InventoryControlParams, OFIParams, HedgeParams,
)
from src.signals.ofi import OFICalculator

log = logging.getLogger("mm")


@dataclass
class AppState:
    running: bool = True
    ticks: int = 0


class BotApp:
    def __init__(self, cfg: Config, store=None):
        self.cfg = cfg
        self.store = store
        self.state = AppState()
        self.trade_stats = TradeStats()
        self._stats_log_every = max(1, int(cfg.app.stats_log_every))
        
        # Setup dedicated fill logger (writes to fills.log)
        print_fills = getattr(cfg.sim, 'print_fills', True)
        self.fill_log_file = "fills.log"
        fill_logger = setup_fill_logger(enabled=print_fills, log_file=self.fill_log_file)
        
        # Execution tape for live fill logging
        tape_history = getattr(cfg.sim, 'execution_tape_history', 200)
        self.execution_tape = ExecutionTape(max_history=tape_history, fill_logger=fill_logger)
        
        # Order ladder for active orders display
        self.order_ladder_file = "orders_ladder.log"
        self.order_ladder = OrderLadder(log_file=self.order_ladder_file, max_levels=15)
        
        # Track if we should spawn monitor windows
        self._spawn_fill_window = print_fills and getattr(cfg.sim, 'fill_monitor_window', True)

        # Multi-market public book + trades feeds
        # Expects: cfg.extended_ws, cfg.extended_trades_ws, cfg.extended.markets
        self.ext_multi = ExtendedMulti(
            cfg.extended_ws,
            cfg.extended_ws,  # <-- FIX: was cfg.extended_ws twice
            cfg.extended.markets,
        )

        # Market selection thresholds
        self.selector_cfg = SelectorConfig(
            min_spread_bps=cfg.extended.selector.min_spread_bps,
            min_tpm=cfg.extended.selector.min_tpm,
            top_n=cfg.extended.selector.top_n,
        )
        
        # REST client for dynamic market discovery (if needed)
        self.rest_client: Optional[ExtendedRESTClient] = None
        if cfg.extended.markets_mode == "all":
            self.rest_client = ExtendedRESTClient(base_url=cfg.extended.api_base_url)
        
        # Pinned market mode (optional: force trading only one specific market)
        self.pinned_market = cfg.extended.pinned_market
        if self.pinned_market:
            log.info(f"🔒 PINNED MARKET MODE: {self.pinned_market} (all other markets ignored)")

        # Extended multi-market feeds (will be initialized in run())
        self.ext_multi: Optional[ExtendedMulti] = None

        # Paper market-making simulator
        _inv = cfg.sim.inventory
        self.paper = PaperMM(
            quote_half_spread_bps=cfg.sim.quote_half_spread_bps,
            quote_size_usd=cfg.sim.quote_size_usd,
            max_inventory_usd=cfg.sim.max_inventory_usd,
            tick_size=cfg.sim.tick_size,
            inventory_cfg=InventoryControlParams(
                inv_skew_strength=_inv.inv_skew_strength,
                size_skew_strength=_inv.size_skew_strength,
                min_size_mult=_inv.min_size_mult,
                max_size_mult=_inv.max_size_mult,
                near_limit_threshold=_inv.near_limit_threshold,
                near_limit_side_mult=_inv.near_limit_side_mult,
            ),
            ofi_cfg=OFIParams(
                ofi_skew_strength=cfg.sim.ofi.ofi_skew_strength,
            ),
            hedge_cfg=HedgeParams(
                trigger_threshold=cfg.sim.hedge.trigger_threshold,
                hedge_fraction=cfg.sim.hedge.hedge_fraction,
                cooldown_ticks=cfg.sim.hedge.cooldown_ticks,
                price_move_trigger_pct=cfg.sim.hedge.price_move_trigger_pct,
                taker_fee_pct=cfg.sim.hedge.taker_fee_pct,
            ),
            trade_stats=self.trade_stats,
            execution_tape=self.execution_tape,
            order_ladder=self.order_ladder,
        )

        # Per-market OFI calculators (lazy-initialised in heartbeat_loop)
        self._ofi_calcs: dict = {}

        # One-time warning flags
        self._warned_no_recent = False

        # Tracks the last fill pushed to the telemetry store (dedup by trade_id)
        self._last_pushed_fill_id: int = 0

        # Tracks the last tape-trade timestamp pushed per market (dedup by ts_ms)
        self._last_pushed_tape_ts: dict = {}

    @staticmethod
    def _mid_from_tob(tob):
        bid = getattr(tob, "bid_px", None)
        ask = getattr(tob, "ask_px", None)
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    async def heartbeat_loop(self):
        tick = float(self.cfg.app.tick_seconds)

        while self.state.running:
            self.state.ticks += 1
            snaps = []

            # 1) Build snapshots for ALL markets
            for m, f in self.ext_multi.feeds.items():
                tob = f.public_ws.tob
                bid, ask = tob.bid_px, tob.ask_px
                spread = tob.spread_bps()

                tape = f.trades_ws.tape
                # Assumes your tape supports these (as in Day2):
                tpm = tape.trades_per_min(60)
                br = tape.buy_ratio(60)

                snaps.append(
                    MarketSnapshot(
                        market=m,
                        bid=bid,
                        ask=ask,
                        spread_bps=spread,
                        tpm=tpm,
                        buy_ratio=br,
                    )
                )

            # 2) Pick markets
            picked = select_markets(snaps, self.selector_cfg)
            
            # 2b) Apply pinned market filter (if enabled)
            if self.pinned_market:
                picked = [p for p in picked if p.market == self.pinned_market]
                # If pinned market wasn't in selector output, try to add it manually
                if not picked and self.pinned_market in self.ext_multi.feeds:
                    pinned_feed = self.ext_multi.feeds[self.pinned_market]
                    tob = pinned_feed.public_ws.tob
                    bid = getattr(tob, "bid_px", None)
                    ask = getattr(tob, "ask_px", None)
                    if bid and ask and bid > 0 and ask > 0:
                        spread = 10000.0 * (ask - bid) / bid
                        tpm = pinned_feed.trades_ws.tape.trades_per_min(60) if hasattr(pinned_feed.trades_ws.tape, 'trades_per_min') else 0
                        picked = [MarketSnapshot(
                            market=self.pinned_market,
                            bid=bid,
                            ask=ask,
                            spread_bps=spread,
                            tpm=tpm,
                            buy_ratio=0.5,
                        )]
            
            # 3) Log selection
            if picked:
                def _fmt(p):
                    spr = "None" if p.spread_bps is None else f"{p.spread_bps:.2f}"
                    tpm_s = "None" if p.tpm is None else f"{p.tpm:.1f}"
                    return f"{p.market}(spr={spr}bps,tpm={tpm_s})"

                top = ", ".join(_fmt(p) for p in picked)
                log.info(f"tick={self.state.ticks} SELECTED ({len(picked)} of top_n={self.selector_cfg.top_n}): {top}")
            else:
                log.info(f"tick={self.state.ticks} SELECTED: (none)")

            # 4) Paper MM (quotes + paper fills) on selected markets
            _primary_quote = None   # Quote for current_market; captured inside loop
            for p in picked:
                f = self.ext_multi.feeds[p.market]
                tob = f.public_ws.tob

                mid = self._mid_from_tob(tob)
                if mid is None:
                    # No book yet for this market -> just skip it (do NOT sleep here)
                    continue

                # Set context for execution tape logging
                self.paper.current_tick = self.state.ticks
                self.paper.current_market = p.market

                # ── OFI signal ────────────────────────────────────────────────
                # Lazy-init one OFICalculator per market.
                if p.market not in self._ofi_calcs:
                    self._ofi_calcs[p.market] = OFICalculator(
                        mode=self.cfg.sim.ofi.mode,
                        window_s=self.cfg.sim.ofi.window_seconds,
                    )
                _ofi_calc = self._ofi_calcs[p.market]
                _ofi_calc.update(tob)
                _ofi_signal = (
                    _ofi_calc.signal(tape=f.trades_ws.tape)
                    if self.cfg.sim.ofi.enabled
                    else 0.0
                )

                # ── Hard hedge (BEFORE make_quote so position feeds into skew) ─
                if self.cfg.sim.hedge.enabled:
                    self.paper.execute_hedge(p.market, tob, mid)

                q = self.paper.make_quote(mid, ofi_signal=_ofi_signal)

                # Capture quote for the first (primary) market for telemetry.
                # Must be done here, before on_trade() may delete the order from
                # the ladder when a fill fully consumes it.
                if _primary_quote is None:
                    _primary_quote = q

                # Render order ladder snapshot for this market
                if self.order_ladder:
                    self.order_ladder.render_snapshot(p.market)

                tape = f.trades_ws.tape
                if hasattr(tape, "recent"):
                    # Use 2x tick window to account for timing and ensure we catch recent trades
                    recent_trades = list(tape.recent(tick * 2))
                    for tr in recent_trades:
                        # TradeTape.recent() returns tuples: (ts_ms, price, qty, side)
                        if isinstance(tr, tuple) and len(tr) == 4:
                            _, trade_px, trade_qty, side = tr
                        else:
                            # fallback for alternate trade representations
                            trade_px = getattr(tr, "price", None)
                            trade_qty = getattr(tr, "qty", None)
                            side = getattr(tr, "side", None)
                        if trade_px is None or trade_qty is None or side is None:
                            continue
                        self.paper.on_trade(
                            mid=mid,
                            trade_px=float(trade_px),
                            trade_qty=float(trade_qty),
                            side=str(side),
                            q=q,
                        )

                    # Push new tape trades to telemetry for the microstructure chart.
                    # Dedup by timestamp so trades already pushed last tick are skipped.
                    if self.store is not None and TapeTradeEvent is not None:
                        _last_ts = self._last_pushed_tape_ts.get(p.market, 0)
                        _max_ts  = _last_ts
                        for _tr in recent_trades:
                            if not (isinstance(_tr, tuple) and len(_tr) == 4):
                                continue
                            _ts_ms, _tpx, _tqty, _tside = _tr
                            if _ts_ms <= _last_ts:
                                continue
                            _s = str(_tside).upper()
                            _side_norm = "BUY" if _s.startswith("B") else "SELL"
                            self.store.record_tape_trade(TapeTradeEvent(
                                tick=self.state.ticks,
                                side=_side_norm,
                                price=float(_tpx),
                                size=float(_tqty),
                                market=p.market,
                            ))
                            if _ts_ms > _max_ts:
                                _max_ts = _ts_ms
                        if _max_ts > _last_ts:
                            self._last_pushed_tape_ts[p.market] = _max_ts
                else:
                    if not self._warned_no_recent:
                        log.warning("tape.recent(seconds) not implemented yet -> skipping paper fills.")
                        self._warned_no_recent = True

            # 5) Calculate PnL correctly by marking all positions to their respective markets
            mid_prices = {}
            for market in self.paper.positions.keys():
                f = self.ext_multi.feeds.get(market)
                if f:
                    tob = f.public_ws.tob
                    mid = self._mid_from_tob(tob)
                    if mid is not None:
                        mid_prices[market] = mid
            
            pnl = self.paper.mark_to_market(mid_prices)
            
            # Show active markets (rate-limited, every half stats interval)
            active_markets = {p.market for p in picked}
            if self.state.ticks % max(1, self._stats_log_every // 2) == 0:
                active_str = ",".join(sorted(active_markets)) if active_markets else "none"
                log.info(f"tick={self.state.ticks} ACTIVE_MARKETS: [{active_str}]")

            # Show position for currently selected market (if any)
            current_market = picked[0].market if picked else None
            market_pos = self.paper.positions.get(current_market, {}).get("pos", 0.0) if current_market else 0.0
            
            log.info(
                f"tick={self.state.ticks} PAPER: "
                f"market={current_market or 'none'} pos={market_pos:.6f} "
                f"norm_inv={self.paper.last_norm_inv:+.3f} "
                f"ofi={self.paper.last_ofi_signal:+.3f} "
                f"hedges={self.paper.hedge_count} "
                f"cash={self.paper.state.cash_usd:.2f} "
                f"pnl≈{pnl:.2f}"
            )

            if self.state.ticks % self._stats_log_every == 0:
                # Show stats across all markets
                total_trades = self.trade_stats.num_trades
                log.info(
                    f"tick={self.state.ticks} STATS: "
                    f"trades={total_trades} "
                    f"volume={self.trade_stats.total_volume:.6f} "
                    f"notional={self.trade_stats.total_notional:.2f} "
                    f"buys={self.trade_stats.buy_volume:.6f} "
                    f"sells={self.trade_stats.sell_volume:.6f} "
                    f"markets={len(self.paper.positions)} "
                    f"TOT_EDGE={self.paper.total_edge_collected:+.4f}"
                )
                
                # PNL_BREAKDOWN: show per-market positions and equity
                if self.paper.positions:
                    breakdown_lines = []
                    for market in sorted(self.paper.positions.keys()):
                        state = self.paper.positions[market]
                        pos = state["pos"]
                        avg_px = state["avg_price"]
                        rpnl = state["realized_pnl"]
                        mid_val = mid_prices.get(market, 0.0)
                        inv_usd = pos * mid_val if mid_val else 0.0
                        breakdown_lines.append(
                            f"{market}: pos={pos:.4f} avg={avg_px:.2f} mid={mid_val:.2f} "
                            f"inv=${inv_usd:.2f} rPnL={rpnl:.2f}"
                        )
                    log.info(
                        f"tick={self.state.ticks} PNL_BREAKDOWN: "
                        f"cash=${self.paper.state.cash_usd:.2f} equity=${pnl:.2f} | " +
                        " | ".join(breakdown_lines)
                    )

            # ── push state to telemetry store (UI reads this) ─────────────────
            if self.store is not None and OrderRow is not None:
                _now = time.time()
                _tob = None
                if current_market and self.ext_multi and current_market in self.ext_multi.feeds:
                    _tob = self.ext_multi.feeds[current_market].public_ws.tob

                _pos  = self.paper.positions.get(current_market, {}) if current_market else {}
                _inv  = _pos.get("pos", 0.0)
                _avg  = _pos.get("avg_price", 0.0)
                _rpnl = _pos.get("realized_pnl", 0.0)
                _mid  = mid_prices.get(current_market, 0.0) if current_market else 0.0
                _upnl = _inv * (_mid - _avg) if _avg > 0 and _mid > 0 else 0.0

                _order_rows = []
                if self.order_ladder and current_market:
                    for _o in self.order_ladder.get_market_orders(current_market):
                        _order_rows.append(OrderRow(
                            order_id=_o.order_id,
                            market=_o.market,
                            side=_o.side,
                            price=_o.price,
                            orig_qty=_o.orig_qty,
                            filled_qty=_o.filled_qty,
                            remaining_qty=_o.remaining_qty,
                            fill_pct=_o.fill_pct,
                            age_sec=_now - _o.created_ts,
                        ))

                self.store.update(
                    selected_market=current_market or "",
                    mid_price=_mid,
                    bid=getattr(_tob, "bid_px", 0.0) or 0.0,
                    ask=getattr(_tob, "ask_px", 0.0) or 0.0,
                    spread_bps=(picked[0].spread_bps or 0.0) if picked else 0.0,
                    tpm=(picked[0].tpm or 0.0) if picked else 0.0,
                    inventory=_inv,
                    avg_price=_avg,
                    cash_usd=self.paper.state.cash_usd,
                    realized_pnl=_rpnl,
                    unrealized_pnl=_upnl,
                    total_pnl=pnl,
                    total_trades=self.trade_stats.num_trades,
                    total_volume=self.trade_stats.total_volume,
                    total_notional=self.trade_stats.total_notional,
                    buy_volume=self.trade_stats.buy_volume,
                    sell_volume=self.trade_stats.sell_volume,
                    total_edge_collected=self.paper.total_edge_collected,
                    norm_inventory=self.paper.last_norm_inv,
                    ofi_signal=self.paper.last_ofi_signal,
                    hedge_count=self.paper.hedge_count,
                    reservation_price=self.paper.last_reservation_price,
                    inv_skew_bps=self.paper.last_inv_skew_bps,
                    ofi_skew_bps=self.paper.last_ofi_skew_bps,
                    ticks=self.state.ticks,
                    active_orders=_order_rows,
                    is_running=self.state.running,
                )
                if _mid > 0:
                    # ── Our quoted bid/ask ────────────────────────────────────
                    # Read from the Quote object captured BEFORE on_trade() so
                    # that a full fill (which deletes the order from active_orders)
                    # does NOT cause a spurious 0.0 / NaN gap in the series.
                    # Fall back to order_ladder with market filter if no quote
                    # was produced this tick (e.g. no market was selected).
                    if _primary_quote is not None:
                        _our_bid = _primary_quote.bid_px
                        _our_ask = _primary_quote.ask_px
                    else:
                        _our_bid = next(
                            (o.price for o in _order_rows
                             if o.side == "BID" and o.market == current_market), 0.0
                        )
                        _our_ask = next(
                            (o.price for o in _order_rows
                             if o.side == "ASK" and o.market == current_market), 0.0
                        )

                    _mkt_bid = getattr(_tob, "bid_px", 0.0) or 0.0
                    _mkt_ask = getattr(_tob, "ask_px", 0.0) or 0.0

                    # ── Invariant checks (violations logged as warnings) ──────
                    if _mkt_bid and _mkt_ask:
                        if _mkt_bid > _mkt_ask:
                            log.warning(
                                f"tick={self.state.ticks} INVARIANT[book]: "
                                f"exch_bid ({_mkt_bid}) > exch_ask ({_mkt_ask}) "
                                f"— exchange book crossed!"
                            )
                        elif not (_mkt_bid - 1e-8 <= _mid <= _mkt_ask + 1e-8):
                            log.warning(
                                f"tick={self.state.ticks} INVARIANT[mid]: "
                                f"mid ({_mid:.4f}) outside exchange BBO "
                                f"[{_mkt_bid:.4f}, {_mkt_ask:.4f}]"
                            )
                    if _our_bid > 0 and _our_ask > 0 and _our_bid >= _our_ask:
                        log.warning(
                            f"tick={self.state.ticks} INVARIANT[quotes]: "
                            f"our_bid ({_our_bid:.4f}) >= our_ask ({_our_ask:.4f}) "
                            f"— quotes crossed!"
                        )

                    # ── Per-tick diagnostic (enable with log level DEBUG) ─────
                    _in_our = (
                        "yes" if _our_bid and _our_ask and _our_bid <= _mid <= _our_ask
                        else "no (skewed)" if _our_bid and _our_ask
                        else "n/a"
                    )
                    _in_exch = (
                        "yes" if _mkt_bid and _mkt_ask and _mkt_bid <= _our_bid and _our_ask <= _mkt_ask
                        else "no (wider/outside)" if _mkt_bid and _mkt_ask
                        else "n/a"
                    )
                    log.debug(
                        f"tick={self.state.ticks} DIAG | "
                        f"exch=[{_mkt_bid:.4f}/{_mkt_ask:.4f}] mid={_mid:.4f} | "
                        f"our=[{_our_bid:.4f}/{_our_ask:.4f}] | "
                        f"mid_in_our_spread={_in_our} | "
                        f"our_in_exch_spread={_in_exch} | "
                        f"norm_inv={self.paper.last_norm_inv:+.3f}"
                    )

                    self.store.append_series(
                        tick=self.state.ticks,
                        price=_mid, our_bid=_our_bid, our_ask=_our_ask,
                        mkt_bid=_mkt_bid, mkt_ask=_mkt_ask,
                        inventory=_inv, pnl=pnl,
                        ofi_signal=self.paper.last_ofi_signal,
                        reservation=self.paper.last_reservation_price,
                    )

                # Push any new fills to the microstructure store buffer.
                # get_history() returns fills in chronological order; we advance
                # the watermark as each new fill is pushed (O(n) per tick, n tiny).
                if FillEvent is not None and self.execution_tape and current_market:
                    for _f in self.execution_tape.get_history_for_market(current_market):
                        if _f.trade_id > self._last_pushed_fill_id:
                            self.store.record_fill_event(FillEvent(
                                tick=_f.tick,
                                side=_f.side,
                                price=_f.price,
                                size=_f.size,
                                edge=_f.edge,
                                trigger_tape_price=_f.trigger_tape_price or 0.0,
                                market=_f.market,
                            ))
                            self._last_pushed_fill_id = _f.trade_id

            await asyncio.sleep(tick)

    async def _initialize_markets(self) -> list[str]:
        """
        Initialize market list based on markets_mode config.
        
        Returns list of market symbols to subscribe to.
        """
        mode = self.cfg.extended.markets_mode
        
        if mode == "static":
            # Use hardcoded market list from config
            markets = self.cfg.extended.markets
            log.info(f"📋 STATIC MODE: Using {len(markets)} configured markets")
            return markets
        
        elif mode == "all":
            # Dynamically discover all active markets from API
            log.info("🔍 DYNAMIC MODE: Discovering all active markets from Extended API...")
            
            if not self.rest_client:
                log.error("REST client not initialized for dynamic market discovery")
                return []
            
            markets = await self.rest_client.get_active_spot_markets()
            
            if not markets:
                log.warning("No markets discovered, falling back to config markets")
                return self.cfg.extended.markets
            
            log.info(f"✅ Discovered {len(markets)} active markets")
            log.info(f"   Markets: {', '.join(markets[:20])}" + 
                    (f" ... (+{len(markets)-20} more)" if len(markets) > 20 else ""))
            
            return markets
        
        else:
            log.error(f"Invalid markets_mode: {mode}. Must be 'static' or 'all'")
            return self.cfg.extended.markets
    
    async def run(self):
        log.info("Starting app...")
        
        # Spawn fill monitor window (Trade History PowerShell – kept as-is)
        if self._spawn_fill_window:
            spawn_fill_monitor_window(self.fill_log_file)

        if self.cfg.venues.extended.enabled:
            # Initialize markets (static or dynamic discovery)
            markets = await self._initialize_markets()
            
            if not markets:
                log.error("No markets available to trade. Exiting.")
                return
            
            # Create multi-market feeds
            log.info(f"🌐 Subscribing to {len(markets)} market feeds...")
            self.ext_multi = ExtendedMulti(
                public_cfg=self.cfg.extended_ws,
                trades_cfg=self.cfg.extended_ws,
                markets=markets,
            )
            self.ext_multi.start()

        try:
            await asyncio.gather(self.heartbeat_loop())
        except asyncio.CancelledError:
            pass
        finally:
            if self.cfg.venues.extended.enabled and self.ext_multi:
                await self.ext_multi.stop()
            
            if self.rest_client:
                await self.rest_client.close()

            log.info("Shutting down cleanly.")

    def stop(self):
        self.state.running = False
