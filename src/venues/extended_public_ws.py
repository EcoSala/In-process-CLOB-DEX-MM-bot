import asyncio
import json
import logging
from typing import Optional

import aiohttp

from src.core.config import ExtendedWSConfig
from src.data.top_of_book import TopOfBook

log = logging.getLogger("mm")

class ExtendedPublicWS:
    """
    Public WS for best bid/ask (depth=1) order book stream.
    Docs:
    - Host: wss://api.starknet.extended.exchange
    - Path: /stream.extended.exchange/v1/orderbooks/{market}?depth=1
    - Messages include SNAPSHOT/DELTA; depth=1 is always snapshot best bid/ask.
    """
    def __init__(self, cfg: ExtendedWSConfig):
        self.cfg = cfg
        self.tob = TopOfBook()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _ws_url(self) -> str:
        # Example from docs:
        # GET /stream.extended.exchange/v1/orderbooks/{market}?depth=1
        return (
            f"{self.cfg.host}/stream.extended.exchange/v1/orderbooks/"
            f"{self.cfg.market}?depth={self.cfg.depth}"
        )

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_and_stream()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"Extended WS error: {e!r}. Reconnecting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 20.0)

    async def _connect_and_stream(self) -> None:
        url = self._ws_url()
        headers = {
            "User-Agent": self.cfg.user_agent,  # docs say User-Agent header is required
        }
        log.info(f"Connecting Extended WS: {url}")

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.ws_connect(
                url,
                autoping=True,   # server pings every ~15s; aiohttp will pong automatically
                heartbeat=10.0,  # send pings from client too (safe)
                max_msg_size=10 * 1024 * 1024,
            ) as ws:
                async for msg in ws:
                    if self._stop.is_set():
                        break

                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle_message(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        raise ws.exception() or RuntimeError("WS error")
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                        raise RuntimeError("WS closed")

    def _handle_message(self, raw: str) -> None:
        data = json.loads(raw)

        # Example (docs):
        # {
        #   "ts": 1701563440000,
        #   "type": "SNAPSHOT",
        #   "data": { "m": "BTC-USD", "b":[{"p":"25670","q":"0.1"}], "a":[{"p":"25770","q":"0.1"}] },
        #   "seq": 1
        # }
        msg_type = data.get("type")
        payload = data.get("data") or {}
        market = payload.get("m")
        if market and market != self.cfg.market:
            return

        ts = data.get("ts")
        seq = data.get("seq")

        bids = payload.get("b") or []
        asks = payload.get("a") or []

        # depth=1 should give either 0 or 1 level; we handle both.
        if bids:
            self.tob.bid_px = float(bids[0]["p"])
            self.tob.bid_qty = float(bids[0]["q"])
        else:
            self.tob.bid_px = None
            self.tob.bid_qty = None

        if asks:
            self.tob.ask_px = float(asks[0]["p"])
            self.tob.ask_qty = float(asks[0]["q"])
        else:
            self.tob.ask_px = None
            self.tob.ask_qty = None

        self.tob.ts_ms = int(ts) if ts is not None else None
        self.tob.seq = int(seq) if seq is not None else None

        # Optional: detect out-of-order seq and reconnect later (Day 2+)
        if msg_type not in ("SNAPSHOT", "DELTA") and msg_type is not None:
            # ignore unknown types
            return
