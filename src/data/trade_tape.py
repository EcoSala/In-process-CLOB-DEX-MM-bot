from dataclasses import dataclass, field
from collections import deque
from typing import Deque, Optional, Tuple
import time

@dataclass
class TradeTape:
    # store (ts_ms, price, qty, side)
    trades: Deque[Tuple[int, float, float, str]] = field(default_factory=lambda: deque(maxlen=5000))
    last_ts_ms: Optional[int] = None

    def add(self, ts_ms: int, price: float, qty: float, side: str) -> None:
        self.trades.append((ts_ms, price, qty, side))
        self.last_ts_ms = ts_ms

    def trades_per_min(self, now_ms: Optional[int] = None, lookback_ms: int = 60_000) -> float:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        cutoff = now_ms - lookback_ms
        count = 0
        # iterate from newest backwards for speed
        for ts_ms, _, _, _ in reversed(self.trades):
            if ts_ms < cutoff:
                break
            count += 1
        return float(count)

    def buy_ratio(self, now_ms: Optional[int] = None, lookback_ms: int = 60_000) -> Optional[float]:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        cutoff = now_ms - lookback_ms
        buys = 0
        sells = 0
        for ts_ms, _, _, side in reversed(self.trades):
            if ts_ms < cutoff:
                break
            if side.lower().startswith("b"):
                buys += 1
            elif side.lower().startswith("s"):
                sells += 1
        total = buys + sells
        if total == 0:
            return None
        return buys / total

    def recent(self, seconds: float, now_ms: Optional[int] = None):
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        cutoff = now_ms - int(seconds * 1000)

        out = []
        for ts_ms, price, qty, side in reversed(self.trades):
            if ts_ms < cutoff:
                break
            out.append((ts_ms, price, qty, side))

        out.reverse()  # oldest -> newest
        return out
