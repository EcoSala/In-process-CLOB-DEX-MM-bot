"""
main.py – PySide6 entry point.

Threading model
---------------
  Main thread   QApplication + MainWindow + QTimer (UI redraws at 8 Hz)
  Worker thread asyncio event loop running BotApp.run()

The two sides share a single BotStateStore protected by threading.Lock.
Trading code never calls Qt; Qt code never blocks on I/O.

Usage
-----
  python main.py
"""
import os
import sys
import asyncio
import threading
import logging

# Must be set before any pyqtgraph import so it picks the right Qt binding.
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

from PySide6.QtWidgets import QApplication

from src.core.config import load_config
from src.core.logger import setup_logging
from src.core.app import BotApp
from src.audio.audio_engine import AudioEngine
from telemetry.store import BotStateStore
from ui.main_window import MainWindow

log = logging.getLogger("mm")


def _run_engine(bot: BotApp) -> None:
    """Worker thread target: spin up asyncio and run the trading engine."""
    try:
        asyncio.run(bot.run())
    except Exception as exc:
        log.error(f"Trading engine crashed: {exc!r}", exc_info=True)


def main() -> None:
    cfg = load_config("config.yaml")
    setup_logging(cfg.app.log_level)

    store = BotStateStore()

    # ── Qt must start on the main thread ─────────────────────────────────────
    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("MM Bot")
    qt_app.setStyle("Fusion")   # consistent cross-platform chrome

    # ── create bot with shared store ─────────────────────────────────────────
    bot = BotApp(cfg, store=store)

    # ── audio engine (main thread – non-blocking via internal worker) ─────────
    audio = AudioEngine(
        enabled=cfg.audio.enabled,
        volume=cfg.audio.volume,
    )

    # ── start trading engine in a daemon thread ───────────────────────────────
    engine_thread = threading.Thread(
        target=_run_engine,
        args=(bot,),
        daemon=True,
        name="trading-engine",
    )
    engine_thread.start()

    # ── launch window ─────────────────────────────────────────────────────────
    window = MainWindow(store, audio=audio)
    window.show()

    def _on_quit() -> None:
        log.info("UI closed – stopping trading engine…")
        audio.shutdown()
        bot.stop()
        engine_thread.join(timeout=4.0)
        if engine_thread.is_alive():
            log.warning("Trading thread did not exit cleanly within timeout.")

    qt_app.aboutToQuit.connect(_on_quit)
    sys.exit(qt_app.exec())


if __name__ == "__main__":
    main()
