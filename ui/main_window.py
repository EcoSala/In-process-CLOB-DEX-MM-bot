"""
MainWindow: root Qt window housing the tab widget and status bar.

Owns the QTimer that drives all UI refreshes at ~8 Hz (125 ms intervals).
All rendering happens exclusively on the main thread through this timer.

Audio events are fired here, in _tick(), by comparing the current snapshot's
fill_events / tape_trade_events lengths against the last-seen counts.  New
events since the previous tick are dispatched to AudioEngine.  No trading
logic runs on this thread.
"""
import time
from typing import TYPE_CHECKING, Optional

from PySide6.QtWidgets import QMainWindow, QTabWidget, QLabel, QStatusBar
from PySide6.QtCore import QTimer

from telemetry.store import BotStateStore
from ui.tabs.dashboard import DashboardTab
from ui.tabs.microstructure import MicrostructureTab
from ui.tabs.orders import OrdersTab
from ui.tabs.distributions import DistributionsTab
from ui.tabs.signals import SignalsTab
from ui.tabs.ofi_analysis import OFIAnalysisTab

if TYPE_CHECKING:
    from src.audio.audio_engine import AudioEngine

_REFRESH_MS = 125   # 8 Hz

_LIGHT_STYLE = """
QMainWindow, QWidget {
    background-color: #f5f6fa;
    color: #1a1a2e;
    font-family: Consolas, 'Courier New', monospace;
    font-size: 11px;
}
QTabWidget::pane {
    border: 1px solid #c8c8d8;
    background-color: #f5f6fa;
}
QTabBar::tab {
    background-color: #eaeaf2;
    color: #8888aa;
    padding: 7px 22px;
    border: 1px solid #c8c8d8;
    border-bottom: none;
    margin-right: 2px;
    min-width: 110px;
}
QTabBar::tab:selected {
    background-color: #f5f6fa;
    color: #1565c0;
    border-bottom: 2px solid #1565c0;
}
QTabBar::tab:hover:!selected {
    color: #445588;
}
QTableView {
    background-color: #ffffff;
    gridline-color: #e0e0ec;
    border: 1px solid #c8c8d8;
    selection-background-color: #cce3ff;
    selection-color: #1a1a2e;
}
QHeaderView::section {
    background-color: #eaeaf2;
    color: #8888aa;
    padding: 5px 8px;
    border: 1px solid #c8c8d8;
    font-weight: bold;
    font-size: 10px;
}
QScrollBar:vertical {
    background: #eaeaf2;
    width: 7px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #b8b8d0;
    border-radius: 3px;
    min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QStatusBar {
    background-color: #eaeaf2;
    color: #8888aa;
    border-top: 1px solid #c8c8d8;
    font-size: 10px;
}
"""


class MainWindow(QMainWindow):
    def __init__(
        self,
        store: BotStateStore,
        audio: "Optional[AudioEngine]" = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._audio = audio

        # Cursors for detecting new events each tick.
        # We compare list lengths; if the deque rotates (length shrinks) we
        # reset the cursor so we never replay stale events.
        self._last_fill_count: int = 0
        self._last_tape_count: int = 0

        self.setWindowTitle("MM Bot  ·  Market Making Dashboard")
        self.resize(1360, 880)
        self.setMinimumSize(900, 600)
        self.setStyleSheet(_LIGHT_STYLE)

        # ── tabs ──────────────────────────────────────────────────────────────
        tabs = QTabWidget()
        self._dash    = DashboardTab()
        self._micro   = MicrostructureTab()
        self._signals = SignalsTab()
        self._ofi     = OFIAnalysisTab()
        self._ord     = OrdersTab()
        self._dist    = DistributionsTab()
        tabs.addTab(self._dash,    "📊   Dashboard")
        tabs.addTab(self._micro,   "🔬   Microstructure")
        tabs.addTab(self._signals, "🧠   Signals / Model")
        tabs.addTab(self._ofi,     "📡   OFI Analysis")
        tabs.addTab(self._ord,     "📋   Orders")
        tabs.addTab(self._dist,    "📈   Distributions")
        self.setCentralWidget(tabs)

        # ── status bar ────────────────────────────────────────────────────────
        self._status_lbl = QLabel("Connecting to trading engine…")
        self._status_lbl.setStyleSheet("color: #8888aa; padding: 1px 8px;")
        self.statusBar().addPermanentWidget(self._status_lbl)

        # ── refresh timer ─────────────────────────────────────────────────────
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(_REFRESH_MS)

    # ──────────────────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        snap = self._store.get_snapshot()

        self._dash.refresh(snap)
        self._micro.refresh(snap)
        self._signals.refresh(snap)
        self._ofi.refresh(snap)
        self._ord.refresh(snap)
        self._dist.refresh(snap)
        self._update_status(snap)

        if self._audio is not None:
            self._fire_audio(snap)

    def _fire_audio(self, snap) -> None:
        """Dispatch audio cues for any events that arrived since the last tick."""
        fills = snap.fill_events
        tapes = snap.tape_trade_events

        n_fills = len(fills)
        n_tapes = len(tapes)

        # Guard against deque rotation (list got shorter than our saved cursor).
        if n_fills < self._last_fill_count:
            self._last_fill_count = n_fills
        if n_tapes < self._last_tape_count:
            self._last_tape_count = n_tapes

        # ── fills (passive + hedge) ───────────────────────────────────────────
        for fe in fills[self._last_fill_count:]:
            side = fe.side
            if side == "BUY":
                self._audio.play_passive_buy()
            elif side == "SELL":
                self._audio.play_passive_sell()
            elif side == "HEDGE_BUY":
                self._audio.play_hedge_buy()
            elif side == "HEDGE_SELL":
                self._audio.play_hedge_sell()
        self._last_fill_count = n_fills

        # ── public tape trades ────────────────────────────────────────────────
        for te in tapes[self._last_tape_count:]:
            if te.side == "BUY":
                self._audio.play_tape_buy()
            else:
                self._audio.play_tape_sell()
        self._last_tape_count = n_tapes

    def _update_status(self, snap) -> None:
        state = "● RUNNING" if snap.is_running else "○  IDLE"
        up = int(snap.uptime_sec)
        h, r = divmod(up, 3600)
        m, s = divmod(r, 60)
        mkt = snap.selected_market or "—"
        self._status_lbl.setText(
            f"{state}  │  market: {mkt}  │  ticks: {snap.ticks}"
            f"  │  trades: {snap.total_trades}"
            f"  │  uptime: {h:02d}:{m:02d}:{s:02d}"
        )

    def closeEvent(self, event) -> None:
        self._timer.stop()
        if self._audio is not None:
            self._audio.shutdown()
        super().closeEvent(event)
