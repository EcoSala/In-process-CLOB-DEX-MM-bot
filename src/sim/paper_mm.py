from dataclasses import dataclass
from typing import Optional


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


class PaperMM:
    def __init__(
        self,
        quote_half_spread_bps: float,
        quote_size_usd: float,
        max_inventory_usd: float,
        trade_stats: Optional[TradeStats] = None,
    ):
        self.half_spread_bps = float(quote_half_spread_bps)
        self.quote_size_usd = float(quote_size_usd)
        self.max_inventory_usd = float(max_inventory_usd)
        self.state = PaperState()
        self.trade_stats = trade_stats

    def make_quote(self, mid: float) -> Quote:
        half = self.half_spread_bps / 10000.0
        bid = mid * (1.0 - half)
        ask = mid * (1.0 + half)
        qty = self.quote_size_usd / mid
        return Quote(bid_px=bid, ask_px=ask, qty_base=qty)

    def _inv_usd(self, mid: float) -> float:
        return self.state.pos_base * mid

    def on_trade(self, mid: float, trade_px: float, trade_qty: float, side: str, q: Quote) -> None:
        """
        side: 'BUY' means aggressor buy (trade at ask side)
              'SELL' means aggressor sell (trade at bid side)
        Fill rule (simple):
          - If aggressor BUY and our ask <= trade_px -> we get filled on ask
          - If aggressor SELL and our bid >= trade_px -> we get filled on bid
        We cap inventory by max_inventory_usd.
        """
        inv_usd = self._inv_usd(mid)

        if side == "BUY":
            # we sell to buyer at our ask
            if q.ask_px <= trade_px and inv_usd > -self.max_inventory_usd:
                fill_qty = min(q.qty_base, trade_qty)
                self.state.pos_base -= fill_qty
                self.state.cash_usd += fill_qty * q.ask_px
                if self.trade_stats:
                    self.trade_stats.record(fill_qty, q.ask_px, side)
        elif side == "SELL":
            # we buy from seller at our bid
            if q.bid_px >= trade_px and inv_usd < self.max_inventory_usd:
                fill_qty = min(q.qty_base, trade_qty)
                self.state.pos_base += fill_qty
                self.state.cash_usd -= fill_qty * q.bid_px
                if self.trade_stats:
                    self.trade_stats.record(fill_qty, q.bid_px, side)

    def mark_to_mid(self, mid: float) -> float:
        # PnL = cash + inventory marked
        return self.state.cash_usd + self.state.pos_base * mid
