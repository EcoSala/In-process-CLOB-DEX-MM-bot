"""Headless paper MM + recorder. No Qt, no audio, no real orders.

Usage (from repo root):
  python run.py
  python run.py --config config.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from src.core.config import load_config
from src.core.logger import setup_logging
from src.core.app import BotApp

log = logging.getLogger("mm")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, app: BotApp) -> None:
    def _on_signal() -> None:
        log.info("signal received — stopping (no real orders were placed)")
        app.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_args: _on_signal())


async def _run(cfg_path: str) -> None:
    cfg = load_config(cfg_path)
    setup_logging(cfg.app.log_level, log_file=cfg.app.log_file)

    rec_dir = Path(cfg.recording.dir)
    log.info("config=%s recording.dir=%s", Path(cfg_path).resolve(), rec_dir)

    app = BotApp(cfg)
    _install_signal_handlers(asyncio.get_running_loop(), app)

    try:
        await app.run()
    except asyncio.CancelledError:
        app.stop()
        raise
    finally:
        app.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Headless paper MM + Extended recorder (no live orders).")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.config))
    except KeyboardInterrupt:
        log.warning("KeyboardInterrupt: stopping...")


if __name__ == "__main__":
    main()
