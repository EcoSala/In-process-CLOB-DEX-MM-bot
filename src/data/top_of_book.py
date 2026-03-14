from dataclasses import dataclass
from typing import Optional

@dataclass
class TopOfBook:
    bid_px: Optional[float] = None
    bid_qty: Optional[float] = None
    ask_px: Optional[float] = None
    ask_qty: Optional[float] = None
    ts_ms: Optional[int] = None
    seq: Optional[int] = None

    # Previous-tick quantities — set by ExtendedPublicWS before each overwrite.
    # Used by OFICalculator (quote mode) and any other consumer that needs deltas.
    prev_bid_qty: Optional[float] = None
    prev_ask_qty: Optional[float] = None

    def mid(self) -> Optional[float]:
        if self.bid_px is None or self.ask_px is None:
            return None
        return 0.5 * (self.bid_px + self.ask_px)

    def spread_bps(self) -> Optional[float]:
        m = self.mid()
        if m is None or self.bid_px is None or self.ask_px is None:
            return None
        return (self.ask_px - self.bid_px) / m * 10_000
