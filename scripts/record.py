"""Record Extended L2 + public trades only (no paper MM, no orders).

Usage (from repo root):
  python scripts/record.py
  python scripts/record.py --config config.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.config import load_config
from src.core.logger import setup_logging
from src.venues.extended_multi import ExtendedMulti
from src.venues.recorder import MarketRecorder

log = logging.getLogger("mm")


async def _run(cfg_path: str) -> None:
    cfg = load_config(cfg_path)
    setup_logging(cfg.app.log_level, log_file=cfg.app.log_file)
    rec = cfg.recording
    if not rec.enabled:
        log.error("recording.enabled is false in %s", cfg_path)
        return

    markets = list(rec.markets) or list(cfg.extended.markets) or ["SOL-USD"]
    if cfg.extended.pinned_market and cfg.extended.pinned_market not in markets:
        markets.append(cfg.extended.pinned_market)

    recorder = MarketRecorder(root=rec.dir, markets=markets)
    recorder.start()
    multi = ExtendedMulti(
        public_cfg=cfg.extended_ws,
        trades_cfg=cfg.extended_ws,
        markets=markets,
        recorder=recorder,
        book_depth=rec.book_depth,
    )
    log.info("Recorder-only mode (no paper MM, no orders). markets=%s depth=%s", markets, rec.book_depth)
    log.info("Writing to %s — Ctrl+C to stop.", Path(rec.dir).resolve())
    multi.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await multi.stop()
        await recorder.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Record Extended L2 + trades (no orders).")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.config))
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
