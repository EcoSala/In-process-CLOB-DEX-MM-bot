from dataclasses import dataclass


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


class PaperMM:
    def __init__(self, quote_half_spread_bps: float, quote_size_usd: float, max_inventory_usd: float):
        self.half_spread_bps = float(quote_half_spread_bps)
        self.quote_size_usd = float(quote_size_usd)
        self.max_inventory_usd = float(max_inventory_usd)
        self.state = PaperState()

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
        elif side == "SELL":
            # we buy from seller at our bid
            if q.bid_px >= trade_px and inv_usd < self.max_inventory_usd:
                fill_qty = min(q.qty_base, trade_qty)
                self.state.pos_base += fill_qty
                self.state.cash_usd -= fill_qty * q.bid_px

    def mark_to_mid(self, mid: float) -> float:
        # PnL = cash + inventory marked
        return self.state.cash_usd + self.state.pos_base * mid
