"""
Order Flow Imbalance (OFI) signal calculator.

Supports two modes controlled by `mode` at construction time:

  "trade"  (default, zero extra state)
  ──────────────────────────────────────────────────────────────────────────
  Computes signed-volume imbalance from the public TradeTape.
  OFI = (Σ buy_qty  −  Σ sell_qty) / (Σ total_qty + ε)  over `window_s`
  Range: [−1, +1].  Positive = net buying pressure.

  "quote"  (requires TopOfBook to carry prev_bid_qty / prev_ask_qty)
  ──────────────────────────────────────────────────────────────────────────
  Accumulates bid-side / ask-side qty deltas at each heartbeat tick.
  delta_t = (bid_qty_t − bid_qty_{t-1}) − (ask_qty_t − ask_qty_{t-1})
  OFI = Σ delta_t / (Σ |delta_t| + ε)  over the rolling window.
  Range: [−1, +1].  Positive = bids deepening / asks thinning (buy pressure).

Usage (one calculator per market):
  calc = OFICalculator(mode="trade", window_s=10.0)
  calc.update(tob)                    # call each heartbeat tick
  signal = calc.signal(tape=tape)     # returns float in [-1, +1]
"""
import time
from collections import deque
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.data.top_of_book import TopOfBook
    from src.data.trade_tape import TradeTape


class OFICalculator:
    """
    Per-market OFI signal.

    Parameters
    ----------
    mode        : "trade" or "quote"
    window_s    : look-back window in seconds
    max_samples : maximum quote-delta samples to keep (bounded memory)
    """

    def __init__(
        self,
        mode: str = "trade",
        window_s: float = 10.0,
        max_samples: int = 600,
    ) -> None:
        if mode not in ("trade", "quote"):
            raise ValueError(f"OFICalculator mode must be 'trade' or 'quote', got {mode!r}")
        self._mode = mode
        self._window_s = window_s

        # Rolling buffer of (timestamp, delta) for quote mode
        self._quote_buf: deque = deque(maxlen=max_samples)

        # Previous TOB quantities — kept here so TopOfBook stays a plain dataclass
        self._prev_bid_qty: Optional[float] = None
        self._prev_ask_qty: Optional[float] = None

        # Last computed signal value (cached for telemetry even between calls)
        self.last_signal: float = 0.0

    # ──────────────────────────────────────────────────────────────────────────
    # Writer API  (call once per heartbeat tick, before signal())
    # ──────────────────────────────────────────────────────────────────────────

    def update(self, tob: "TopOfBook") -> None:
        """
        Update internal quote-delta state from the current TopOfBook snapshot.
        Must be called once per tick *before* calling signal().

        In trade mode this is a no-op (all data comes from TradeTape in signal()).
        In quote mode this records bid/ask qty deltas into the rolling buffer.
        """
        if self._mode != "quote":
            return

        bid_qty = tob.bid_qty
        ask_qty = tob.ask_qty

        if bid_qty is not None and ask_qty is not None:
            if self._prev_bid_qty is not None and self._prev_ask_qty is not None:
                delta = (bid_qty - self._prev_bid_qty) - (ask_qty - self._prev_ask_qty)
                self._quote_buf.append((time.time(), delta))

        # Always update previous, even if we couldn't compute a delta this tick
        self._prev_bid_qty = bid_qty
        self._prev_ask_qty = ask_qty

    # ──────────────────────────────────────────────────────────────────────────
    # Reader API
    # ──────────────────────────────────────────────────────────────────────────

    def signal(self, tape: Optional["TradeTape"] = None) -> float:
        """
        Return a normalised OFI signal in [−1, +1].

        Parameters
        ----------
        tape : required when mode=="trade"; ignored in quote mode.

        Returns
        -------
        float in [−1, +1].
            +1  →  pure buying pressure
            −1  →  pure selling pressure
             0  →  balanced / no data
        """
        if self._mode == "trade":
            result = self._trade_ofi(tape)
        else:
            result = self._quote_ofi()

        self.last_signal = result
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _trade_ofi(self, tape: Optional["TradeTape"]) -> float:
        if tape is None:
            return 0.0

        try:
            recent = tape.recent(self._window_s)
        except Exception:
            return 0.0

        buy_vol = 0.0
        sell_vol = 0.0
        for _, _, qty, side in recent:
            s = str(side).upper()
            if s.startswith("B"):
                buy_vol += float(qty)
            elif s.startswith("S"):
                sell_vol += float(qty)

        total = buy_vol + sell_vol
        if total < 1e-12:
            return 0.0

        return (buy_vol - sell_vol) / total

    def _quote_ofi(self) -> float:
        if not self._quote_buf:
            return 0.0

        cutoff = time.time() - self._window_s
        window_sum = 0.0
        abs_sum = 0.0
        for ts, delta in self._quote_buf:
            if ts >= cutoff:
                window_sum += delta
                abs_sum += abs(delta)

        if abs_sum < 1e-12:
            return 0.0

        # Clamp to [-1, +1] in case of extreme deltas
        raw = window_sum / abs_sum
        return max(-1.0, min(1.0, raw))
