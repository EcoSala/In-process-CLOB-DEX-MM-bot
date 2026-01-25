import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

log = logging.getLogger("mm")


@dataclass
class PaperState:
    pos_base: float = 0.0     # inventory in base (e.g. BTC)
    cash_usd: float = 0.0     # cash PnL in USD
    realized_pnl: float = 0.0


@dataclass
class Quote:
    bid_px: float
    ask_px: float
    qty_base: float  # size on each side


@dataclass
class Fill:
    """Represents a single execution/fill"""
    trade_id: int
    timestamp: datetime
    tick: int
    market: str
    side: str
    size: float
    price: float
    notional: float
    avg_price_after: float
    pos_after: float
    cash_after: float
    pnl_after: float
    realized_pnl_trade: float  # Realized PnL from this trade
    realized_pnl_total: float  # Cumulative realized PnL


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
        )
        self.fills.append(fill)
        if self.fill_logger:
            self._print_fill(fill)
    
    def _print_fill(self, fill: Fill) -> None:
        """Print a single fill line with alternating row colors and BUY/SELL highlighting"""
        # ANSI color codes (Windows PowerShell compatible)
        RESET = "\033[0m"
        BLUE = "\033[94m"      # Bright blue for odd trades
        YELLOW = "\033[93m"    # Bright yellow for even trades
        GREEN = "\033[92m"     # Bright green for BUY
        RED = "\033[91m"       # Bright red for SELL
        
        # Determine row color based on trade_id
        row_color = BLUE if fill.trade_id % 2 == 1 else YELLOW
        
        # Format components
        ts_str = fill.timestamp.strftime("%H:%M:%S.%f")[:-3]  # milliseconds
        pos_sign = "+" if fill.pos_after >= 0 else ""
        rpnl_trade_sign = "+" if fill.realized_pnl_trade >= 0 else ""
        rpnl_total_sign = "+" if fill.realized_pnl_total >= 0 else ""
        
        # Determine side color
        side_color = GREEN if fill.side == "BUY" else RED
        side_padded = fill.side.ljust(4)
        
        # Build the colored line
        # StripAnsiFormatter will remove colors when writing to file
        colored_line = (
            f"{row_color}#{fill.trade_id:05d} | {ts_str} | tick={fill.tick:<3} | "
            f"{fill.market:<7} | {RESET}{side_color}{side_padded}{RESET}{row_color} | "
            f"{fill.size:.4f} @{fill.price:.2f} | ${fill.notional:.2f} | "
            f"avg={fill.avg_price_after:.2f} | pos={pos_sign}{fill.pos_after:.4f} | "
            f"rPnL={rpnl_trade_sign}{fill.realized_pnl_trade:.2f} | "
            f"rPnLtot={rpnl_total_sign}{fill.realized_pnl_total:.2f}{RESET}"
        )
        
        # Log the colored line (file handler strips ANSI codes automatically)
        self.fill_logger.info(colored_line)
    
    def get_history(self, n: Optional[int] = None) -> list:
        """Return last n fills (or all if n is None)"""
        if n is None:
            return list(self.fills)
        return list(self.fills)[-n:]


class PaperMM:
    def __init__(
        self,
        quote_half_spread_bps: float,
        quote_size_usd: float,
        max_inventory_usd: float,
        trade_stats: Optional[TradeStats] = None,
        execution_tape: Optional[ExecutionTape] = None,
    ):
        self.half_spread_bps = float(quote_half_spread_bps)
        self.quote_size_usd = float(quote_size_usd)
        self.max_inventory_usd = float(max_inventory_usd)
        self.state = PaperState()
        self.trade_stats = trade_stats
        self.execution_tape = execution_tape
        
        # Track per-market position state
        # Each market has: position, avg_price, realized_pnl_total
        self.positions = {}  # market -> {"pos": float, "avg_price": float, "realized_pnl": float}
        
        # Track current tick and market for tape logging
        self.current_tick = 0
        self.current_market = ""

    def make_quote(self, mid: float) -> Quote:
        half = self.half_spread_bps / 10000.0
        bid = mid * (1.0 - half)
        ask = mid * (1.0 + half)
        qty = self.quote_size_usd / mid
        return Quote(bid_px=bid, ask_px=ask, qty_base=qty)

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

        if side == "BUY":
            # we sell to buyer at our ask
            if q.ask_px <= trade_px and inv_usd > -self.max_inventory_usd:
                fill_qty = min(q.qty_base, trade_qty)
                fill_px = q.ask_px
                
                # Calculate realized PnL (we're selling, so negative qty)
                realized_pnl_trade, new_avg_price = self._calculate_realized_pnl(
                    old_pos, -fill_qty, fill_px, old_avg_price
                )
                
                # Update per-market position state
                pos_state["pos"] -= fill_qty
                pos_state["avg_price"] = new_avg_price
                pos_state["realized_pnl"] += realized_pnl_trade
                
                # Update global cash
                self.state.cash_usd += fill_qty * fill_px
                
                # Record stats
                if self.trade_stats:
                    self.trade_stats.record(fill_qty, fill_px, side)
                
                # Record to execution tape
                if self.execution_tape:
                    pnl = self.mark_to_mid(mid)
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
                    )
                    
        elif side == "SELL":
            # we buy from seller at our bid
            if q.bid_px >= trade_px and inv_usd < self.max_inventory_usd:
                fill_qty = min(q.qty_base, trade_qty)
                fill_px = q.bid_px
                
                # Calculate realized PnL (we're buying, so positive qty)
                realized_pnl_trade, new_avg_price = self._calculate_realized_pnl(
                    old_pos, fill_qty, fill_px, old_avg_price
                )
                
                # Update per-market position state
                pos_state["pos"] += fill_qty
                pos_state["avg_price"] = new_avg_price
                pos_state["realized_pnl"] += realized_pnl_trade
                
                # Update global cash
                self.state.cash_usd -= fill_qty * fill_px
                
                # Record stats
                if self.trade_stats:
                    self.trade_stats.record(fill_qty, fill_px, side)
                
                # Record to execution tape
                if self.execution_tape:
                    pnl = self.mark_to_mid(mid)
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
                    )

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
