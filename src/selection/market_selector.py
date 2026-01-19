from dataclasses import dataclass
from typing import Iterable


@dataclass
class MarketSnapshot:
    market: str
    bid: float | None
    ask: float | None
    spread_bps: float | None
    tpm: float
    buy_ratio: float | None


@dataclass
class SelectorConfig:
    min_spread_bps: float
    min_tpm: float
    top_n: int


def score(s: MarketSnapshot) -> float:
    # Simple score: prioritize wide spread + active tape
    if s.spread_bps is None:
        return -1.0
    return float(s.spread_bps) * (1.0 + (s.tpm ** 0.5))


def select_markets(snaps: Iterable[MarketSnapshot], cfg: SelectorConfig) -> list[MarketSnapshot]:
    ok = []
    for s in snaps:
        if s.bid is None or s.ask is None or s.spread_bps is None:
            continue
        if s.spread_bps < cfg.min_spread_bps:
            continue
        if s.tpm < cfg.min_tpm:
            continue
        ok.append(s)

    ok.sort(key=score, reverse=True)
    return ok[: cfg.top_n]
