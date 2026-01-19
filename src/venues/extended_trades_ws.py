import asyncio
import json
import logging
from typing import Optional

import aiohttp

from src.core.config import ExtendedWSConfig
from src.data.trade_tape import TradeTape

log = logging.getLogger("mm")

class ExtendedTradesWS:
    def __init__(self, cfg: ExtendedWSConfig):
        self.cfg = cfg
        self.tape = TradeTape()
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
        # NOTE: you must match Extended's trades stream path.
        # If your docs say a different path, paste it and I'll adjust instantly.
        # Common pattern is something like:
        # /stream.extended.exchange/v1/publicTrades/{market}
        return f"{self.cfg.host}/stream.extended.exchange/v1/publicTrades/{self.cfg.market}"

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_and_stream()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"Extended Trades WS error: {e!r}. Reconnecting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 20.0)

    async def _connect_and_stream(self) -> None:
        url = self._ws_url()
        headers = {"User-Agent": self.cfg.user_agent}
        log.info(f"Connecting Extended Trades WS: {url}")

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.ws_connect(
                url,
                autoping=True,
                heartbeat=10.0,
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
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        trades = msg.get("data")
        if not isinstance(trades, list):
            return

        for t in trades:
            ts = t.get("T")
            price = t.get("p")
            qty = t.get("q")
            side = t.get("S")

            if ts is None or price is None or qty is None or side is None:
                continue

            self.tape.add(int(ts), float(price), float(qty), str(side))

