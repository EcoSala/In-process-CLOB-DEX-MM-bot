import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, TYPE_CHECKING
import time
import uuid

if TYPE_CHECKING:
    from src.data.top_of_book import TopOfBook

log = logging.getLogger("mm")


def snap_bid(price: float, tick: float) -> float:
    """
    Floor a bid price to the nearest tick boundary.

    Bids must never round UP (that would narrow the spread and potentially cross
    the ask, or cause the bot to buy above its intended level).

    The inner round(..., 9) prevents float-division artefacts such as
    125.54 / 0.01 == 12553.999999999998 flooring to 12553 instead of 12554.
    """
    return round(math.floor(round(price / tick, 9)) * tick, 8)


def snap_ask(price: float, tick: float) -> float:
    """
    Ceil an ask price to the nearest tick boundary.

    Asks must never round DOWN (that would narrow the spread or cross the bid).
    """
    return round(math.ceil(round(price / tick, 9)) * tick, 8)


def calculate_trade_edge(trade_price: float, mid_price: float, side: str, size: float) -> float:
    """
    Compute the edge collected on a single passive fill (in USD).

    For a market maker:
      - BUY fill  : we bought below mid  →  edge = (mid - trade_price) * size
      - SELL fill : we sold above mid    →  edge = (trade_price - mid) * size

    Edge is always ≥ 0 when quoting inside the spread.
    `side` must be the *bot's* fill side ("BUY" or "SELL").
    """
    if side == "BUY":
        return (mid_price - trade_price) * size
    else:  # SELL
        return (trade_price - mid_price) * size


@dataclass
class PaperState:
    pos_base: float = 0.0     # inventory in base (e.g. BTC)
    cash_usd: float = 0.0     # cash PnL in USD
    realized_pnl: float = 0.0


@dataclass
class Quote:
    bid_px:  float
    ask_px:  float
    bid_qty: float   # size on the bid side  (may differ from ask after size skew)
    ask_qty: float   # size on the ask side


@dataclass
class Fill:
    """Represents a single execution/fill"""
    trade_id: int
    timestamp: datetime
    tick: int
    market: str
    side: str        # "BUY" | "SELL" for passive fills; "HEDGE_BUY" | "HEDGE_SELL" for hedge fills
    size: float
    price: float
    notional: float
    avg_price_after: float
    pos_after: float
    cash_after: float
    pnl_after: float
    realized_pnl_trade: float  # Realized PnL from this trade
    realized_pnl_total: float  # Cumulative realized PnL
    edge: float                # Edge collected on this fill  (≥ 0 for passive; 0.0 for hedges)
    edge_total: float          # Cumulative edge collected across all fills (excludes hedge fills)
    is_hedge: bool = False     # True for aggressive hedge fills; excluded from total_edge_collected
    trigger_tape_price: Optional[float] = None  # Raw tape trade price that triggered this passive fill.
                                                 # None for hedge fills (no triggering tape event).
                                                 # Equals fill price when taker hit exactly our quote;
                                                 # greater than fill price when taker swept deeper.


@dataclass
class TradeStats:
    total_volume: float = 0.0      # base units traded (abs)
    total_notional: float = 0.0    # quote units traded (abs)
    num_trades: int = 0
    buy_volume: float = 0.0        # base units on buy side
    sell_volume: float = 0.0       # base units on sell side

    def record(self, qty: float, price: float, side: str) -> None:
        # qty is signed; stats use absolute
        vol = abs(qty)
        notional = abs(qty * price)

        self.total_volume += vol
        self.total_notional += notional
        self.num_trades += 1

        side_up = side.upper()
        if side_up.startswith("B"):
            self.buy_volume += vol
        elif side_up.startswith("S"):
            self.sell_volume += vol


@dataclass
class ActiveOrder:
    order_id: str
    market: str
    side: str  # "BID" or "ASK"
    price: float
    orig_qty: float
    filled_qty: float
    created_ts: float
    updated_ts: float
    
    @property
    def remaining_qty(self) -> float:
        return max(0.0, self.orig_qty - self.filled_qty)
    
    @property
    def fill_pct(self) -> float:
        return self.filled_qty / self.orig_qty if self.orig_qty > 0 else 0.0
    
    @property
    def is_complete(self) -> bool:
        return self.remaining_qty <= 1e-6 or self.fill_pct >= 0.995


class OrderLadder:
    """
    Tracks active orders and renders a ladder-style display showing only
    price levels where the bot has active orders (not the full orderbook).
    """
    def __init__(self, log_file: str = "orders_ladder.log", max_levels: int = 15):
        self.log_file = log_file
        self.max_levels = max_levels
        self.active_orders: dict[str, ActiveOrder] = {}  # order_id -> ActiveOrder
        self.recently_filled: dict[str, float] = {}  # order_id -> timestamp when completed
        self.recent_filled_ttl = 2.0  # seconds to keep recently filled orders visible
        self.last_render_time = 0.0
        self.render_interval = 0.25  # 250ms between renders
    
    def add_order(self, market: str, side: str, price: float, qty: float) -> str:
        """Add a new active order. Returns order_id."""
        order_id = str(uuid.uuid4())[:8]
        now = time.time()
        order = ActiveOrder(
            order_id=order_id,
            market=market,
            side=side,
            price=price,
            orig_qty=qty,
            filled_qty=0.0,
            created_ts=now,
            updated_ts=now,
        )
        self.active_orders[order_id] = order
        return order_id
    
    def update_fill(self, order_id: str, fill_qty: float):
        """Update an order with a partial or full fill."""
        if order_id not in self.active_orders:
            return
        
        order = self.active_orders[order_id]
        order.filled_qty += fill_qty
        order.updated_ts = time.time()
        
        # If order is complete, move to recently_filled cache
        if order.is_complete:
            self.recently_filled[order_id] = time.time()
            del self.active_orders[order_id]
    
    def cleanup_recent_filled(self):
        """Remove recently filled orders that have expired from cache."""
        now = time.time()
        expired = [oid for oid, ts in self.recently_filled.items() 
                   if now - ts > self.recent_filled_ttl]
        for oid in expired:
            del self.recently_filled[oid]
    
    def render_snapshot(self, market: str):
        """Render and write ladder snapshot to file."""
        now = time.time()
        
        # Rate limit rendering
        if now - self.last_render_time < self.render_interval:
            return
        
        self.last_render_time = now
        self.cleanup_recent_filled()
        
        # Aggregate orders by price level for the given market
        bids_agg = {}  # price -> (filled, remaining)
        asks_agg = {}  # price -> (remaining, filled)
        
        for order in self.active_orders.values():
            if order.market != market:
                continue
            
            price = order.price
            filled = order.filled_qty
            remaining = order.remaining_qty
            
            if order.side == "BID":
                if price not in bids_agg:
                    bids_agg[price] = [0.0, 0.0]
                bids_agg[price][0] += filled
                bids_agg[price][1] += remaining
            else:  # ASK
                if price not in asks_agg:
                    asks_agg[price] = [0.0, 0.0]
                asks_agg[price][0] += remaining
                asks_agg[price][1] += filled
        
        # Sort: bids descending, asks ascending
        sorted_bids = sorted(bids_agg.items(), key=lambda x: x[0], reverse=True)[:self.max_levels]
        sorted_asks = sorted(asks_agg.items(), key=lambda x: x[0])[:self.max_levels]
        
        # Build output
        lines = []
        lines.append("=" * 80)
        lines.append(f"  ACTIVE ORDERS LADDER - {market}  ".center(80))
        lines.append("=" * 80)
        lines.append("")
        lines.append(f"{'BIDS':<25} {'PRICE':^30} {'ASKS':>25}")
        lines.append(f"{'filled':>10} {'resting':>12}   {'':^30}   {'resting':<12} {'filled':<10}")
        lines.append("-" * 80)
        
        # Display asks (top to bottom, highest first)
        for price, (remaining, filled) in reversed(sorted_asks):
            ask_str = f"{remaining:>12.4f} {filled:<10.4f}"
            lines.append(f"{'':>22}   {price:^30.2f}   {ask_str:<25}")
        
        # Separator
        if sorted_bids or sorted_asks:
            lines.append("-" * 80)
        
        # Display bids (top to bottom, highest first)
        for price, (filled, remaining) in sorted_bids:
            bid_str = f"{filled:>10.4f} {remaining:>12.4f}"
            lines.append(f"{bid_str:<22}   {price:^30.2f}   {'':>25}")
        
        lines.append("")
        lines.append(f"Active: {len(self.active_orders)} orders | Recently filled: {len(self.recently_filled)} | {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
        lines.append("=" * 80)
        
        # Write snapshot by overwriting file
        with open(self.log_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
    
    def get_market_orders(self, market: str) -> list:
        """Return all active ActiveOrder objects for *market* only.

        Prefer this over iterating ``active_orders.values()`` directly so
        every call site is automatically market-scoped.
        """
        return [o for o in self.active_orders.values() if o.market == market]

    def cancel_all_orders(self, market: str = None):
        """Cancel all active orders (optionally for a specific market)."""
        to_remove = []
        for order_id, order in self.active_orders.items():
            if market is None or order.market == market:
                to_remove.append(order_id)
        
        for order_id in to_remove:
            del self.active_orders[order_id]


class ExecutionTape:
    """Records and prints execution fills in real-time"""
    
    def __init__(self, max_history: int = 200, fill_logger: Optional[logging.Logger] = None):
        self.max_history = max_history
        self.fills = deque(maxlen=max_history)
        self.fill_logger = fill_logger
        self.trade_counter = 0  # Monotonically increasing trade ID
    
    def record_fill(
        self,
        tick: int,
        market: str,
        side: str,
        size: float,
        price: float,
        notional: float,
        avg_price_after: float,
        pos_after: float,
        cash_after: float,
        pnl_after: float,
        realized_pnl_trade: float,
        realized_pnl_total: float,
        edge: float = 0.0,
        edge_total: float = 0.0,
        is_hedge: bool = False,
        trigger_tape_price: Optional[float] = None,
    ) -> None:
        """Record a fill and print it immediately"""
        self.trade_counter += 1
        fill = Fill(
            trade_id=self.trade_counter,
            timestamp=datetime.now(),
            tick=tick,
            market=market,
            side=side,
            size=size,
            price=price,
            notional=notional,
            avg_price_after=avg_price_after,
            pos_after=pos_after,
            cash_after=cash_after,
            pnl_after=pnl_after,
            realized_pnl_trade=realized_pnl_trade,
            realized_pnl_total=realized_pnl_total,
            edge=edge,
            edge_total=edge_total,
            is_hedge=is_hedge,
            trigger_tape_price=trigger_tape_price,
        )
        self.fills.append(fill)
        if self.fill_logger:
            self._print_fill(fill)
    
    def _print_fill(self, fill: Fill) -> None:
        """Print a single fill line with alternating row colors and BUY/SELL highlighting"""
        # ANSI color codes (Windows PowerShell compatible)
        RESET = "\033[0m"
        WHITE = "\033[0m"      # Default white for odd trades
        BLUE = "\033[94m"      # Bright blue for even trades
        GREEN = "\033[92m"     # Bright green for BUY
        RED = "\033[91m"       # Bright red for SELL
        
        # Determine row color based on trade_id
        row_color = WHITE if fill.trade_id % 2 == 1 else BLUE
        
        # Format components
        ts_str = fill.timestamp.strftime("%H:%M:%S.%f")[:-3]  # milliseconds
        pos_sign = "+" if fill.pos_after >= 0 else ""
        rpnl_trade_sign = "+" if fill.realized_pnl_trade >= 0 else ""
        rpnl_total_sign = "+" if fill.realized_pnl_total >= 0 else ""
        
        # Determine side color; hedge fills use yellow
        YELLOW = "\033[93m"
        if fill.is_hedge:
            side_color = YELLOW
        elif fill.side == "BUY":
            side_color = GREEN
        else:
            side_color = RED
        side_padded = fill.side.ljust(10)
        
        edge_sign       = "+" if fill.edge >= 0 else ""
        edge_total_sign = "+" if fill.edge_total >= 0 else ""

        # Build the colored line
        # StripAnsiFormatter will remove colors when writing to file
        colored_line = (
            f"{row_color}#{fill.trade_id:05d} | {ts_str} | tick={fill.tick:<3} | "
            f"{fill.market:<7} | {RESET}{side_color}{side_padded}{RESET}{row_color} | "
            f"{fill.size:.4f} @{fill.price:.2f} | ${fill.notional:.2f} | "
            f"avg={fill.avg_price_after:.2f} | pos={pos_sign}{fill.pos_after:.4f} | "
            f"rPnL={rpnl_trade_sign}{fill.realized_pnl_trade:.2f} | "
            f"rPnLtot={rpnl_total_sign}{fill.realized_pnl_total:.2f} | "
            f"edge={edge_sign}{fill.edge:.2f} | "
            f"TOT_EDGE={edge_total_sign}{fill.edge_total:.2f}{RESET}"
        )
        
        # Log the colored line (file handler strips ANSI codes automatically)
        self.fill_logger.info(colored_line)
    
    def get_history(self, n: Optional[int] = None) -> list:
        """Return last n fills (or all if n is None) across all markets."""
        if n is None:
            return list(self.fills)
        return list(self.fills)[-n:]

    def get_history_for_market(self, market: str, n: Optional[int] = None) -> list:
        """Return fills for *market* only (chronological order).

        Prefer this over ``get_history()`` at every call site that feeds
        a single-market display, so fills from other markets cannot
        contaminate the output.
        """
        fills = [f for f in self.fills if f.market == market]
        if n is not None:
            return fills[-n:]
        return fills


@dataclass
class InventoryControlParams:
    """
    All tunable knobs for the inventory-control pipeline.
    Mirrors InventoryConfig in config.py; kept as a plain dataclass so
    paper_mm.py has no pydantic dependency.
    """
    inv_skew_strength:    float = 10.0  # bps price shift per unit norm_inv
    size_skew_strength:   float = 0.5   # fractional size change per unit norm_inv
    min_size_mult:        float = 0.1   # floor on size multiplier
    max_size_mult:        float = 2.0   # ceiling on size multiplier
    near_limit_threshold: float = 0.8   # |norm_inv| above which dampening fires
    near_limit_side_mult: float = 0.1   # bad-side size factor near the limit
    # Volatility-scaled spread: half_spread = max(base, k * ewma_1m_range_bps)
    vol_spread_k:         float = 0.5
    vol_ewma_span:        int   = 30    # 1m candles (or live ticks) in the EWMA
    max_half_spread_bps:  float = 40.0  # cap so crash bars don't quote 200 bps
    # One-sided quoting when inventory or OFI is toxic
    toxic_inv_threshold:  float = 0.4   # |norm_inv| → quote only the flattening side
    toxic_ofi_threshold:  float = 0.3   # |ofi| → pull the informed/toxic side


@dataclass
class OFIParams:
    """
    OFI signal parameters passed into PaperMM.
    Mirrors OFIConfig in config.py; no pydantic dependency.
    """
    ofi_skew_strength: float = 1.0  # bps shift on reservation_price per unit OFI signal


@dataclass
class HedgeParams:
    """
    Hard inventory hedge parameters passed into PaperMM.
    Mirrors HedgeConfig in config.py; no pydantic dependency.
    """
    trigger_threshold:      float = 0.9     # |norm_inv| above which inventory-limit hedge fires
    hedge_fraction:         float = 0.5     # fraction of position to flatten per inventory-limit hedge
    cooldown_ticks:         int   = 5       # minimum ticks between hedges on same market
    taker_fee_pct:          float = 0.0225  # taker fee in % charged on every hedge notional


class PaperMM:
    def __init__(
        self,
        quote_half_spread_bps: float,
        quote_size_usd: float,
        max_inventory_usd: float,
        tick_size: float = 0.01,
        maker_fee_pct: float = 0.0,
        inventory_cfg: Optional[InventoryControlParams] = None,
        ofi_cfg: Optional[OFIParams] = None,
        hedge_cfg: Optional[HedgeParams] = None,
        trade_stats: Optional[TradeStats] = None,
        execution_tape: Optional[ExecutionTape] = None,
        order_ladder: Optional[OrderLadder] = None,
    ):
        self.half_spread_bps = float(quote_half_spread_bps)
        self.quote_size_usd = float(quote_size_usd)
        self.max_inventory_usd = float(max_inventory_usd)
        self.tick_size = float(tick_size)
        self.maker_fee_rate = float(maker_fee_pct) / 100.0  # e.g. 0.02% → 0.0002
        self.state = PaperState()
        self.trade_stats = trade_stats
        self.execution_tape = execution_tape
        self.order_ladder = order_ladder

        # Inventory control parameters (use defaults if none supplied)
        _inv = inventory_cfg if inventory_cfg is not None else InventoryControlParams()
        self.inv_skew_strength    = _inv.inv_skew_strength
        self.size_skew_strength   = _inv.size_skew_strength
        self.min_size_mult        = _inv.min_size_mult
        self.max_size_mult        = _inv.max_size_mult
        self.near_limit_threshold = _inv.near_limit_threshold
        self.near_limit_side_mult = _inv.near_limit_side_mult
        self.vol_spread_k         = _inv.vol_spread_k
        self.vol_ewma_span        = max(2, int(_inv.vol_ewma_span))
        self.max_half_spread_bps  = _inv.max_half_spread_bps
        self.toxic_inv_threshold  = _inv.toxic_inv_threshold
        self.toxic_ofi_threshold  = _inv.toxic_ofi_threshold
        self._vol_alpha           = 2.0 / (self.vol_ewma_span + 1.0)
        self._vol_ewma_bps        = 0.0
        self._vol_ready           = False
        self.last_half_spread_bps = float(quote_half_spread_bps)

        # OFI signal parameters
        _ofi = ofi_cfg if ofi_cfg is not None else OFIParams()
        self.ofi_skew_strength: float = _ofi.ofi_skew_strength

        # Hard hedge parameters
        _hedge = hedge_cfg if hedge_cfg is not None else HedgeParams()
        self.hedge_trigger_threshold:      float = _hedge.trigger_threshold
        self.hedge_fraction:               float = _hedge.hedge_fraction
        self.hedge_cooldown_ticks:         int   = _hedge.cooldown_ticks
        self.taker_fee_rate:               float = _hedge.taker_fee_pct / 100.0

        # Per-market hedge cooldown tracking: market → last tick when hedge fired
        self._last_hedge_tick: dict = {}

        # Cumulative hedge fill count (telemetry; does NOT include passive fills)
        self.hedge_count: int = 0

        # Last-tick diagnostics – readable by app.py for logging / telemetry
        self.last_norm_inv:           float = 0.0
        self.last_reservation_price:  float = 0.0
        self.last_ofi_signal:         float = 0.0   # last OFI value passed to make_quote
        # Signal-contribution diagnostics (bps relative to mid)
        # Negative when long (inventory pushes price down), positive when buy-pressure (OFI shifts up).
        self.last_inv_skew_bps:       float = 0.0
        self.last_ofi_skew_bps:       float = 0.0

        # Track per-market position state
        # Each market has: position, avg_price, realized_pnl_total
        self.positions = {}  # market -> {"pos": float, "avg_price": float, "realized_pnl": float}
        
        # Cumulative edge collected across all fills
        self.total_edge_collected: float = 0.0

        # Track current tick and market for tape logging
        self.current_tick = 0
        self.current_market = ""
        
        # Track quote order IDs for fill matching
        self.active_bid_order: Optional[str] = None
        self.active_ask_order: Optional[str] = None

    def make_quote(self, mid: float, ofi_signal: float = 0.0) -> Quote:
        # ── Step 1: Normalised inventory ────────────────────────────────────────
        # inv_usd is positive when long, negative when short; clamped to [-1, 1].
        inv_usd  = self._inv_usd(self.current_market, mid)
        norm_inv = max(-1.0, min(1.0, inv_usd / self.max_inventory_usd)) if self.max_inventory_usd else 0.0

        # ── Step 2: Reservation price (inventory-skewed fair value) ─────────────
        # Long inventory  → shift down  → quotes become more attractive for sellers
        # Short inventory → shift up    → quotes become more attractive for buyers
        skew_bps          = self.inv_skew_strength * norm_inv   # signed bps
        reservation_price = mid * (1.0 - skew_bps / 10_000.0)

        # ── Step 2b: OFI bias (additive on top of inventory skew) ───────────────
        # Positive OFI (net buying pressure) → price likely to rise → shift quotes
        # UP to avoid selling too cheaply and to be more aggressive on the bid side.
        # Negative OFI → shift DOWN.  ofi_signal is clamped to [-1, +1] by OFICalculator.
        if ofi_signal != 0.0 and self.ofi_skew_strength != 0.0:
            ofi_bps           = self.ofi_skew_strength * ofi_signal   # signed bps
            reservation_price = reservation_price * (1.0 + ofi_bps / 10_000.0)
        self.last_ofi_signal = ofi_signal

        # ── Step 3: Volatility-scaled half-spread ──────────────────────────────
        # Base floor is quote_half_spread_bps (typically max(8, market spread/2)).
        # Widen with lagged EWMA of 1m range so volatile alts are not quoted tight.
        half_bps = self.half_spread_bps
        if self.vol_spread_k > 0.0 and self._vol_ready:
            half_bps = max(half_bps, self.vol_spread_k * self._vol_ewma_bps)
        if self.max_half_spread_bps > 0.0:
            half_bps = min(half_bps, self.max_half_spread_bps)
        self.last_half_spread_bps = half_bps
        half    = half_bps / 10_000.0
        raw_bid = reservation_price * (1.0 - half)
        raw_ask = reservation_price * (1.0 + half)

        # ── Step 4: Base quote size ───────────────────────────────────────────────
        base_qty = self.quote_size_usd / mid

        # ── Step 5: Asymmetric size skew ─────────────────────────────────────────
        # Long  → smaller bid (don't buy more), larger ask (sell more)
        # Short → larger bid (buy more),        smaller ask (don't sell more)
        def _clamp_mult(v: float) -> float:
            return max(self.min_size_mult, min(self.max_size_mult, v))

        bid_mult = _clamp_mult(1.0 - self.size_skew_strength * norm_inv)
        ask_mult = _clamp_mult(1.0 + self.size_skew_strength * norm_inv)
        bid_qty  = base_qty * bid_mult
        ask_qty  = base_qty * ask_mult

        # ── Step 6: Near-limit dampening ─────────────────────────────────────────
        # When inventory is deep on one side, further reduce quoting on the
        # side that would push inventory further toward the limit.
        if norm_inv > self.near_limit_threshold:
            # Near long limit – strongly discourage additional buys
            bid_qty *= self.near_limit_side_mult
        elif norm_inv < -self.near_limit_threshold:
            # Near short limit – strongly discourage additional sells
            ask_qty *= self.near_limit_side_mult

        # ── Step 6b: Pull the toxic side (one-sided quoting) ─────────────────────
        # Inventory flatten always wins: if we are long we MUST keep the ask.
        # OFI then pulls the informed side when we are not inventory-constrained.
        quote_bid = True
        quote_ask = True
        if self.toxic_ofi_threshold > 0.0:
            if ofi_signal >= self.toxic_ofi_threshold:
                quote_ask = False   # buying pressure → don't sell into the lift
            if ofi_signal <= -self.toxic_ofi_threshold:
                quote_bid = False   # selling pressure → don't buy the dump
        if self.toxic_inv_threshold > 0.0:
            if norm_inv >= self.toxic_inv_threshold:
                quote_bid = False
                quote_ask = True    # long → only flatten (sell)
            elif norm_inv <= -self.toxic_inv_threshold:
                quote_ask = False
                quote_bid = True    # short → only flatten (buy)
        if not quote_bid:
            bid_qty = 0.0
        if not quote_ask:
            ask_qty = 0.0

        # ── Step 7: Tick snapping ────────────────────────────────────────────────
        # Applied AFTER all price math so that small skews are not silently lost.
        bid = snap_bid(raw_bid, self.tick_size)
        ask = snap_ask(raw_ask, self.tick_size)

        # ── Step 8: Crossed-quote guard ──────────────────────────────────────────
        if bid >= ask:
            ask = bid + self.tick_size

        # ── Persist diagnostics for caller (logging / telemetry) ────────────────
        self.last_norm_inv          = norm_inv
        self.last_reservation_price = reservation_price
        # last_ofi_signal already set at Step 2b above
        # Decompose reservation skew into per-signal contributions (in bps):
        #   inv:  how many bps inventory shifted reservation from mid
        #   ofi:  how many bps OFI shifted reservation (0 when OFI is disabled/zero)
        self.last_inv_skew_bps = -skew_bps  # negative when long → res shifted down
        self.last_ofi_skew_bps = (
            self.ofi_skew_strength * ofi_signal
            if ofi_signal != 0.0 and self.ofi_skew_strength != 0.0
            else 0.0
        )

        # ── Update order ladder ──────────────────────────────────────────────────
        if self.order_ladder:
            self.order_ladder.cancel_all_orders(self.current_market)
            self.active_bid_order = None
            self.active_ask_order = None
            if bid_qty > 1e-12:
                self.active_bid_order = self.order_ladder.add_order(
                    market=self.current_market, side="BID", price=bid, qty=bid_qty
                )
            if ask_qty > 1e-12:
                self.active_ask_order = self.order_ladder.add_order(
                    market=self.current_market, side="ASK", price=ask, qty=ask_qty
                )

        return Quote(bid_px=bid, ask_px=ask, bid_qty=bid_qty, ask_qty=ask_qty)

    def update_realized_vol(self, range_bps: float) -> None:
        """
        Feed the *completed* bar's range (in bps) into the EWMA.

        Must be called AFTER quoting/fills for the current bar so the next
        quote uses only lagged volatility (no look-ahead).
        """
        if range_bps < 0.0:
            return
        if not self._vol_ready:
            self._vol_ewma_bps = float(range_bps)
            self._vol_ready = True
            return
        a = self._vol_alpha
        self._vol_ewma_bps = a * float(range_bps) + (1.0 - a) * self._vol_ewma_bps

    def _inv_usd(self, market: str, mid: float) -> float:
        """Get inventory in USD for a specific market"""
        if market not in self.positions:
            return 0.0
        return self.positions[market]["pos"] * mid
    
    def _get_position_state(self, market: str) -> dict:
        """Get or create position state for a market"""
        if market not in self.positions:
            self.positions[market] = {"pos": 0.0, "avg_price": 0.0, "realized_pnl": 0.0}
        return self.positions[market]

    def _calculate_realized_pnl(
        self, old_pos: float, fill_qty_signed: float, fill_px: float, old_avg_price: float
    ) -> tuple[float, float]:
        """
        Calculate realized PnL and new average price for a fill.
        
        Args:
            old_pos: Position before fill (signed)
            fill_qty_signed: Fill quantity (signed, + for buy, - for sell)
            fill_px: Fill price
            
        Returns:
            (realized_pnl_trade, new_avg_price)
        """
        new_pos = old_pos + fill_qty_signed
        realized_pnl_trade = 0.0
        new_avg_price = old_avg_price
        
        # Case 1: Flat position -> opening new position
        if abs(old_pos) < 1e-8:
            new_avg_price = fill_px
            realized_pnl_trade = 0.0
            
        # Case 2: Same direction (increasing exposure)
        elif (old_pos > 0 and fill_qty_signed > 0) or (old_pos < 0 and fill_qty_signed < 0):
            # Adding to position: update VWAP
            total_cost_before = abs(old_pos) * old_avg_price
            fill_cost = abs(fill_qty_signed) * fill_px
            new_avg_price = (total_cost_before + fill_cost) / abs(new_pos)
            realized_pnl_trade = 0.0
            
        # Case 3: Opposite direction (reducing or flipping)
        else:
            # Check if we're reducing, closing, or flipping
            if abs(new_pos) < 1e-8:
                # Closing entire position
                qty_closed = abs(old_pos)
                if old_pos > 0:
                    # Closing long: realized = (exit - entry) * qty
                    realized_pnl_trade = (fill_px - old_avg_price) * qty_closed
                else:
                    # Closing short: realized = (entry - exit) * qty
                    realized_pnl_trade = (old_avg_price - fill_px) * qty_closed
                new_avg_price = 0.0
                
            elif (old_pos > 0 and new_pos > 0) or (old_pos < 0 and new_pos < 0):
                # Reducing position (same sign)
                qty_closed = abs(fill_qty_signed)
                if old_pos > 0:
                    realized_pnl_trade = (fill_px - old_avg_price) * qty_closed
                else:
                    realized_pnl_trade = (old_avg_price - fill_px) * qty_closed
                # Average price stays the same when reducing
                new_avg_price = old_avg_price
                
            else:
                # Flipping through zero
                qty_closed = abs(old_pos)
                qty_opened = abs(new_pos)
                
                # Realize PnL on closed portion
                if old_pos > 0:
                    realized_pnl_trade = (fill_px - old_avg_price) * qty_closed
                else:
                    realized_pnl_trade = (old_avg_price - fill_px) * qty_closed
                    
                # New position starts at fill price
                new_avg_price = fill_px
        
        return realized_pnl_trade, new_avg_price

    def on_trade(self, mid: float, trade_px: float, trade_qty: float, side: str, q: Quote) -> None:
        """
        side: 'BUY' means aggressor buy (trade at ask side)
              'SELL' means aggressor sell (trade at bid side)
        Fill rule (simple):
          - If aggressor BUY and our ask <= trade_px -> we get filled on ask
          - If aggressor SELL and our bid >= trade_px -> we get filled on bid
        We cap inventory by max_inventory_usd.
        """
        # Get per-market position state
        pos_state = self._get_position_state(self.current_market)
        old_pos = pos_state["pos"]
        old_avg_price = pos_state["avg_price"]
        
        inv_usd = self._inv_usd(self.current_market, mid)
        if mid <= 0:
            return

        side_u = str(side).upper()
        aggressor_buy = side_u.startswith("B")
        aggressor_sell = side_u.startswith("S")

        if aggressor_buy:
            # we sell to buyer at our ask
            room_qty = max(0.0, (inv_usd + self.max_inventory_usd) / mid)
            if q.ask_px <= trade_px:
                fill_qty = min(q.ask_qty, trade_qty, room_qty)
                if fill_qty < 1e-12:
                    return
                fill_px = q.ask_px
                q.ask_qty = max(0.0, q.ask_qty - fill_qty)
                
                # Calculate realized PnL (we're selling, so negative qty)
                realized_pnl_trade, new_avg_price = self._calculate_realized_pnl(
                    old_pos, -fill_qty, fill_px, old_avg_price
                )
                
                # Update per-market position state
                pos_state["pos"] -= fill_qty
                pos_state["avg_price"] = new_avg_price
                pos_state["realized_pnl"] += realized_pnl_trade
                
                # Update global cash (deduct maker fee on passive sell)
                maker_fee = fill_qty * fill_px * self.maker_fee_rate
                self.state.cash_usd += fill_qty * fill_px - maker_fee
                
                # Update order ladder (ask filled)
                if self.order_ladder and self.active_ask_order:
                    self.order_ladder.update_fill(self.active_ask_order, fill_qty)
                
                # Accumulate edge net of maker fee (bot's side is SELL, fill price is ask > mid)
                fill_edge = calculate_trade_edge(fill_px, mid, "SELL", fill_qty) - maker_fee
                self.total_edge_collected += fill_edge

                # Record stats
                if self.trade_stats:
                    self.trade_stats.record(fill_qty, fill_px, side)
                
                # Record to execution tape
                # Note: pnl_after = global_cash + this_market_MTM; for multi-market
                # setups this is approximate (global cash includes PnL from other markets).
                if self.execution_tape:
                    pnl = self.state.cash_usd + pos_state["pos"] * mid
                    self.execution_tape.record_fill(
                        tick=self.current_tick,
                        market=self.current_market,
                        side="SELL",  # we sell when aggressor buys
                        size=fill_qty,
                        price=fill_px,
                        notional=fill_qty * fill_px,
                        avg_price_after=pos_state["avg_price"],
                        pos_after=pos_state["pos"],
                        cash_after=self.state.cash_usd,
                        pnl_after=pnl,
                        realized_pnl_trade=realized_pnl_trade,
                        realized_pnl_total=pos_state["realized_pnl"],
                        edge=fill_edge,
                        edge_total=self.total_edge_collected,
                        trigger_tape_price=trade_px,
                    )

        elif aggressor_sell:
            # we buy from seller at our bid
            room_qty = max(0.0, (self.max_inventory_usd - inv_usd) / mid)
            if q.bid_px >= trade_px:
                fill_qty = min(q.bid_qty, trade_qty, room_qty)
                if fill_qty < 1e-12:
                    return
                fill_px = q.bid_px
                q.bid_qty = max(0.0, q.bid_qty - fill_qty)
                
                # Calculate realized PnL (we're buying, so positive qty)
                realized_pnl_trade, new_avg_price = self._calculate_realized_pnl(
                    old_pos, fill_qty, fill_px, old_avg_price
                )
                
                # Update per-market position state
                pos_state["pos"] += fill_qty
                pos_state["avg_price"] = new_avg_price
                pos_state["realized_pnl"] += realized_pnl_trade
                
                # Update global cash (add maker fee cost on passive buy)
                maker_fee = fill_qty * fill_px * self.maker_fee_rate
                self.state.cash_usd -= fill_qty * fill_px + maker_fee
                
                # Update order ladder (bid filled)
                if self.order_ladder and self.active_bid_order:
                    self.order_ladder.update_fill(self.active_bid_order, fill_qty)
                
                # Accumulate edge net of maker fee (bot's side is BUY, fill price is bid < mid)
                fill_edge = calculate_trade_edge(fill_px, mid, "BUY", fill_qty) - maker_fee
                self.total_edge_collected += fill_edge

                # Record stats
                if self.trade_stats:
                    self.trade_stats.record(fill_qty, fill_px, side)
                
                # Record to execution tape
                # Note: pnl_after is approximate for multi-market (see SELL branch comment).
                if self.execution_tape:
                    pnl = self.state.cash_usd + pos_state["pos"] * mid
                    self.execution_tape.record_fill(
                        tick=self.current_tick,
                        market=self.current_market,
                        side="BUY",  # we buy when aggressor sells
                        size=fill_qty,
                        price=fill_px,
                        notional=fill_qty * fill_px,
                        avg_price_after=pos_state["avg_price"],
                        pos_after=pos_state["pos"],
                        cash_after=self.state.cash_usd,
                        pnl_after=pnl,
                        realized_pnl_trade=realized_pnl_trade,
                        realized_pnl_total=pos_state["realized_pnl"],
                        edge=fill_edge,
                        edge_total=self.total_edge_collected,
                        trigger_tape_price=trade_px,
                    )

    def execute_hedge(self, market: str, tob: "TopOfBook", mid: float) -> bool:
        """
        Simulate an aggressive (market-order) inventory-limit hedge fill.

        Fires when |norm_inv| ≥ hedge_trigger_threshold AND the cooldown has elapsed.
        Flattens hedge_fraction of the current position at the best available price
        (hit the bid when long, lift the ask when short).

        Taker fee:
          fee = notional × taker_fee_rate  deducted from cash_usd on every hedge fill.
          Reflected in pnl_after; NOT counted in total_edge_collected.

        Returns True if a hedge was executed this tick, False otherwise.
        """
        pos_state   = self._get_position_state(market)
        current_pos = pos_state["pos"]
        if abs(current_pos) < 1e-10:
            return False

        inv_usd  = self._inv_usd(market, mid)
        norm_inv = max(-1.0, min(1.0, inv_usd / self.max_inventory_usd)) if self.max_inventory_usd else 0.0

        # ── Inventory-limit hedge with cooldown ───────────────────────────────
        last_tick = self._last_hedge_tick.get(market, -(self.hedge_cooldown_ticks + 1))
        cooldown_ok = self.current_tick - last_tick >= self.hedge_cooldown_ticks
        if not (abs(norm_inv) >= self.hedge_trigger_threshold and cooldown_ok):
            return False

        # ── Determine direction and fill price ────────────────────────────────
        if current_pos > 0:
            # Long → sell at best bid (cross the spread)
            fill_px = getattr(tob, "bid_px", None)
            if fill_px is None or fill_px <= 0:
                return False
            fill_qty   = abs(current_pos) * self.hedge_fraction
            qty_signed = -fill_qty
            side_str   = "HEDGE_SELL"
        else:
            # Short → buy at best ask
            fill_px = getattr(tob, "ask_px", None)
            if fill_px is None or fill_px <= 0:
                return False
            fill_qty   = abs(current_pos) * self.hedge_fraction
            qty_signed = fill_qty
            side_str   = "HEDGE_BUY"

        if fill_qty < 1e-10:
            return False

        # ── Execute position update ───────────────────────────────────────────
        old_pos       = pos_state["pos"]
        old_avg_price = pos_state["avg_price"]

        realized_pnl_trade, new_avg_price = self._calculate_realized_pnl(
            old_pos, qty_signed, fill_px, old_avg_price
        )

        pos_state["pos"]          += qty_signed
        pos_state["avg_price"]     = new_avg_price
        pos_state["realized_pnl"] += realized_pnl_trade

        # ── Taker fee ─────────────────────────────────────────────────────────
        notional = fill_qty * fill_px
        fee      = notional * self.taker_fee_rate

        if qty_signed > 0:   # hedge buy: pay notional + fee
            self.state.cash_usd -= notional + fee
        else:                # hedge sell: receive notional − fee
            self.state.cash_usd += notional - fee

        # ── Cooldown + counters ───────────────────────────────────────────────
        self._last_hedge_tick[market] = self.current_tick
        self.hedge_count += 1

        # ── Log to execution tape ─────────────────────────────────────────────
        # Edge is intentionally -fee — hedge fills are taker trades (cost, not edge).
        # total_edge_collected is NOT incremented.
        if self.execution_tape:
            pnl = self.state.cash_usd + pos_state["pos"] * mid
            self.execution_tape.record_fill(
                tick=self.current_tick,
                market=market,
                side=side_str,
                size=fill_qty,
                price=fill_px,
                notional=notional,
                avg_price_after=pos_state["avg_price"],
                pos_after=pos_state["pos"],
                cash_after=self.state.cash_usd,
                pnl_after=pnl,
                realized_pnl_trade=realized_pnl_trade,
                realized_pnl_total=pos_state["realized_pnl"],
                edge=-fee,               # fee shows as negative edge in the tape
                edge_total=self.total_edge_collected,
                is_hedge=True,
            )

        log.info(
            f"HEDGE[INV_LIMIT]: {side_str} {fill_qty:.4f} @ {fill_px:.2f} "
            f"fee=${fee:.4f} "
            f"(norm_inv={norm_inv:+.3f} → "
            f"{max(-1.0, min(1.0, pos_state['pos'] * mid / self.max_inventory_usd)):+.3f})"
        )
        return True

    def mark_to_market(self, mid_prices: dict[str, float]) -> float:
        """
        Calculate total equity (PnL) by marking all positions to their respective market mids.
        
        Args:
            mid_prices: dict mapping market -> mid_price (e.g. {"ETH-USD": 2924.0, "SOL-USD": 125.5})
        
        Returns:
            Total equity = cash + sum(position[market] * mid[market])
        """
        total_inventory_usd = 0.0
        for market, state in self.positions.items():
            if market in mid_prices and mid_prices[market] is not None:
                total_inventory_usd += state["pos"] * mid_prices[market]
        
        return self.state.cash_usd + total_inventory_usd
    
    def mark_to_mid(self, mid: float) -> float:
        """Legacy single-market PnL (deprecated, kept for compatibility)"""
        # This is only correct if there's a single position
        total_inventory_usd = sum(
            state["pos"] * mid for state in self.positions.values()
        )
        return self.state.cash_usd + total_inventory_usd

           
            