"""Durable gzip JSONL recorder for Extended public WS + paper quotes/fills.

The writer runs as its own asyncio task. WS callbacks only enqueue; quoting
failures cannot stop the writer. Hourly files:

  {dir}/{market}/{book|trades|quotes|fills}/YYYYMMDD_HH.jsonl.gz
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TextIO

log = logging.getLogger("mm.recorder")

_QUEUE_MAX = 20_000
_FLUSH_SECONDS = 1.0


def _exch_ts(raw: str) -> Optional[int]:
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    ts = obj.get("ts")
    if ts is not None:
        try:
            return int(ts)
        except (TypeError, ValueError):
            return None
    data = obj.get("data")
    if isinstance(data, list) and data:
        t = data[0].get("T") if isinstance(data[0], dict) else None
        if t is not None:
            try:
                return int(t)
            except (TypeError, ValueError):
                return None
    return None


class MarketRecorder:
    """Non-blocking capture + background gzip writer with hourly rotation."""

    def __init__(
        self,
        root: str = "data/recordings",
        markets: Optional[list[str]] = None,
        flush_seconds: float = _FLUSH_SECONDS,
    ):
        self.root = Path(root)
        self.markets = set(markets) if markets else None
        self.flush_seconds = float(flush_seconds)
        self._q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._task: Optional[asyncio.Task] = None
        self._files: dict[tuple[str, str], TextIO] = {}
        self._hours: dict[tuple[str, str], str] = {}
        self._last_flush = time.monotonic()
        self._dropped = 0
        self._written = 0
        self._stop = asyncio.Event()

    def _allowed(self, market: str) -> bool:
        return self.markets is None or market in self.markets

    def capture(self, stream: str, market: str, raw: str, recv_ns: Optional[int] = None) -> None:
        """Enqueue a raw WS TEXT frame. Never raises into the WS loop."""
        if not self._allowed(market):
            return
        try:
            self._q.put_nowait(("ws", recv_ns or time.time_ns(), stream, market, raw))
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 100 == 1:
                log.warning("recorder queue full, dropped=%d", self._dropped)
        except Exception:
            log.exception("recorder.capture failed")

    def record_quote(
        self,
        *,
        ts: int,
        market: str,
        bid_px: float,
        ask_px: float,
        bid_sz: float,
        ask_sz: float,
        mid: float,
        norm_inv: float,
    ) -> None:
        if not self._allowed(market):
            return
        payload = {
            "ts": ts,
            "recv_ns": time.time_ns(),
            "market": market,
            "bid_px": bid_px,
            "ask_px": ask_px,
            "bid_sz": bid_sz,
            "ask_sz": ask_sz,
            "mid": mid,
            "norm_inv": norm_inv,
        }
        self._put_json("quotes", market, payload)

    def record_fill(self, payload: dict[str, Any]) -> None:
        market = str(payload.get("market") or "")
        if not market or not self._allowed(market):
            return
        payload = dict(payload)
        payload.setdefault("recv_ns", time.time_ns())
        self._put_json("fills", market, payload)

    def record_metrics(self, payload: dict[str, Any]) -> None:
        """Hourly jsonl under {dir}/metrics/ — not filtered by recording.markets."""
        payload = dict(payload)
        payload.setdefault("recv_ns", time.time_ns())
        payload.setdefault("ts", int(time.time() * 1000))
        self._put_json("metrics", "metrics", payload)

    def _put_json(self, stream: str, market: str, payload: dict[str, Any]) -> None:
        try:
            self._q.put_nowait(("json", stream, market, payload))
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 100 == 1:
                log.warning("recorder queue full, dropped=%d", self._dropped)
        except Exception:
            log.exception("recorder enqueue failed")

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self.root.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._writer(), name="market-recorder")
        log.info(
            "recorder started dir=%s markets=%s",
            self.root.resolve(),
            sorted(self.markets) if self.markets is not None else "ALL",
        )

    async def stop(self) -> None:
        self._stop.set()
        try:
            self._q.put_nowait(("stop",))
        except asyncio.QueueFull:
            pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=8.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None
        self._close_all()
        log.info(
            "recorder stopped written=%d dropped=%d",
            self._written,
            self._dropped,
        )

    async def _writer(self) -> None:
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self._q.get(), timeout=self.flush_seconds)
                except asyncio.TimeoutError:
                    self._flush_all()
                    if self._stop.is_set() and self._q.empty():
                        break
                    continue
                if not item or item[0] == "stop":
                    while True:
                        try:
                            leftover = self._q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if leftover and leftover[0] != "stop":
                            self._safe_handle(leftover)
                    break
                self._safe_handle(item)
                if (time.monotonic() - self._last_flush) >= self.flush_seconds:
                    self._flush_all()
        except asyncio.CancelledError:
            raise
        finally:
            self._flush_all()
            self._close_all()

    def _safe_handle(self, item: tuple) -> None:
        try:
            self._handle(item)
        except Exception:
            log.exception("recorder write failed; continuing")

    def _handle(self, item: tuple) -> None:
        kind = item[0]
        if kind == "ws":
            _, recv_ns, stream, market, raw = item
            envelope = {
                "recv_ns": recv_ns,
                "exch_ts": _exch_ts(raw),
                "stream": stream,
                "market": market,
                "raw": raw,
            }
            self._write_line(market, stream, envelope)
        elif kind == "json":
            _, stream, market, payload = item
            self._write_line(market, stream, payload)

    def _write_line(self, market: str, stream: str, obj: dict[str, Any]) -> None:
        hour = datetime.now(timezone.utc).strftime("%Y%m%d_%H")
        key = (market, stream)
        fh = self._files.get(key)
        if fh is None or self._hours.get(key) != hour:
            if fh is not None:
                self._close_one(key)
            if stream == "metrics":
                path = self.root / "metrics" / f"{hour}.jsonl.gz"
            else:
                path = self.root / market / stream / f"{hour}.jsonl.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = gzip.open(path, mode="at", compresslevel=4, encoding="utf-8")
            self._files[key] = fh
            self._hours[key] = hour
        fh.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
        fh.write("\n")
        self._written += 1

    def _flush_all(self) -> None:
        for fh in self._files.values():
            try:
                fh.flush()
                buf = getattr(fh, "fileobj", None) or getattr(fh, "buffer", None)
                if buf is not None:
                    buf.flush()
            except Exception:
                log.exception("recorder flush failed")
        self._last_flush = time.monotonic()

    def _close_one(self, key: tuple[str, str]) -> None:
        fh = self._files.pop(key, None)
        self._hours.pop(key, None)
        if fh is None:
            return
        try:
            fh.flush()
            fh.close()
        except Exception:
            log.exception("recorder close failed key=%s", key)

    def _close_all(self) -> None:
        for key in list(self._files):
            self._close_one(key)
