from dataclasses import dataclass
from typing import Any, Dict, Optional

from src.venues.extended_public_ws import ExtendedPublicWS
from src.venues.extended_trades_ws import ExtendedTradesWS
from src.core.config import ExtendedWSConfig


@dataclass
class MarketFeeds:
    public_ws: ExtendedPublicWS
    trades_ws: ExtendedTradesWS


class ExtendedMulti:
    def __init__(
        self,
        public_cfg: ExtendedWSConfig,
        trades_cfg: ExtendedWSConfig,
        markets: list[str],
        recorder: Any = None,
        book_depth: Optional[int] = None,
    ):
        self._feeds: Dict[str, MarketFeeds] = {}
        for m in markets:
            pc = public_cfg.model_copy()
            pc.market = m
            if book_depth is not None:
                pc.depth = int(book_depth)
            tc = trades_cfg.model_copy()
            tc.market = m
            self._feeds[m] = MarketFeeds(
                public_ws=ExtendedPublicWS(pc, recorder=recorder),
                trades_ws=ExtendedTradesWS(tc, recorder=recorder),
            )

    @property
    def feeds(self) -> Dict[str, MarketFeeds]:
        return self._feeds

    def start(self) -> None:
        for f in self._feeds.values():
            f.public_ws.start()
            f.trades_ws.start()

    async def stop(self) -> None:
        # stop trades first, then book
        for f in self._feeds.values():
            await f.trades_ws.stop()
        for f in self._feeds.values():
            await f.public_ws.stop()
