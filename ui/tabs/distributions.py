"""
Distributions tab: histogram of per-tick log-returns + summary statistics.

Stats displayed: N, Mean, Std Dev, Skewness, Kurtosis, Annualised Sharpe (rough).
Histogram is only redrawn when new data points arrive (optimised for low CPU).
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSizePolicy,
)
from PySide6.QtCore import Qt

import pyqtgraph as pg
import numpy as np

from telemetry.state import BotState

# ── palette ───────────────────────────────────────────────────────────────────
_BG     = "#ffffff"
_PANEL  = "#f5f6fa"
_BORDER = "#c8c8d8"
_DIM    = "#8888aa"
_FG     = "#1a1a2e"
_BLUE   = "#4d9fff"
_YELLOW = "#ffd740"
_GREEN  = "#00e676"
_RED    = "#ff4444"


# ── single stat display ───────────────────────────────────────────────────────

class _StatCard(QFrame):
    _VAL_STYLE = (
        "color:{color}; font-size:14px; font-weight:bold;"
        " font-family:Consolas,monospace; background:transparent; border:none;"
    )
    _LBL_STYLE = (
        f"color:{_DIM}; font-size:9px; font-weight:bold; letter-spacing:1px;"
        " background:transparent; border:none;"
    )

    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            f"QFrame{{background:{_PANEL};border:1px solid {_BORDER};border-radius:4px;}}"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(50)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 5, 10, 5)
        lay.setSpacing(1)

        self._lbl = QLabel(label.upper())
        self._lbl.setStyleSheet(self._LBL_STYLE)
        self._val = QLabel("—")
        self._val.setStyleSheet(self._VAL_STYLE.format(color=_FG))

        lay.addWidget(self._lbl)
        lay.addWidget(self._val)

    def set(self, text: str, color: str = _FG) -> None:
        self._val.setText(text)
        self._val.setStyleSheet(self._VAL_STYLE.format(color=color))


# ── DistributionsTab ──────────────────────────────────────────────────────────

class DistributionsTab(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Stat cards row
        row = QHBoxLayout()
        row.setSpacing(4)
        self._s_n       = _StatCard("N (returns)")
        self._s_mean    = _StatCard("Mean return")
        self._s_std     = _StatCard("Std dev")
        self._s_skew    = _StatCard("Skewness")
        self._s_kurt    = _StatCard("Ex. kurtosis")
        self._s_sharpe  = _StatCard("Sharpe (est)")
        for w in (self._s_n, self._s_mean, self._s_std, self._s_skew, self._s_kurt, self._s_sharpe):
            row.addWidget(w)
        layout.addLayout(row)

        # Histogram
        self._plot = pg.PlotWidget(background=_BG)
        self._plot.setTitle("Returns Distribution", color=_DIM, size="9pt")
        pi = self._plot.getPlotItem()
        pi.setLabel("bottom", "Return", color=_DIM)
        pi.setLabel("left", "Count",   color=_DIM)
        pi.showGrid(x=False, y=True, alpha=0.20)
        pi.setMenuEnabled(False)
        for axis in ("left", "bottom"):
            pi.getAxis(axis).setPen(_BORDER)
            pi.getAxis(axis).setTextPen(_DIM)
        layout.addWidget(self._plot)

        # Mean vertical line
        self._mean_line = pg.InfiniteLine(
            angle=90,
            pen=pg.mkPen(_YELLOW, width=1.5, style=Qt.PenStyle.DashLine),
            label="μ",
            labelOpts={"color": _YELLOW, "position": 0.85},
        )
        self._plot.addItem(self._mean_line)
        self._mean_line.setVisible(False)

        # Gaussian overlay curve (optional, cosmetic)
        self._gauss_curve = self._plot.plot(
            pen=pg.mkPen(_GREEN, width=1, style=Qt.PenStyle.DotLine),
        )

        self._bar_item: pg.BarGraphItem | None = None
        self._last_n: int = 0           # only redraw when data changes

    # ──────────────────────────────────────────────────────────────────────────

    def refresh(self, snap: BotState) -> None:
        returns = snap.returns_series
        n = len(returns)
        self._s_n.set(str(n))

        if n < 5:
            for w in (self._s_mean, self._s_std, self._s_skew, self._s_kurt, self._s_sharpe):
                w.set("—")
            self._mean_line.setVisible(False)
            return

        arr = np.asarray(returns, dtype=np.float64)
        mean = float(np.mean(arr))
        std  = float(np.std(arr, ddof=1)) if n > 1 else 0.0

        # Annualised Sharpe: assume ~1 tick/sec → 86400 ticks/day
        sharpe = (mean / std * np.sqrt(86400)) if std > 0 else 0.0

        centered = arr - mean
        skew = float(np.mean(centered ** 3) / std ** 3) if std > 0 else 0.0
        kurt = float(np.mean(centered ** 4) / std ** 4 - 3) if std > 0 else 0.0

        sharpe_clr = _GREEN if sharpe > 0 else _RED
        self._s_mean.set(f"{mean * 100:.4f}%")
        self._s_std.set(f"{std * 100:.4f}%")
        self._s_skew.set(f"{skew:.3f}", _YELLOW if abs(skew) > 1 else _FG)
        self._s_kurt.set(f"{kurt:.3f}", _YELLOW if abs(kurt) > 3 else _FG)
        self._s_sharpe.set(f"{sharpe:.2f}", sharpe_clr)

        # Redraw histogram only when count changes
        if n == self._last_n:
            return
        self._last_n = n

        num_bins = min(60, max(10, n // 4))
        counts, edges = np.histogram(arr, bins=num_bins)
        width = float(edges[1] - edges[0])
        x = edges[:-1].tolist()

        if self._bar_item is not None:
            self._plot.removeItem(self._bar_item)

        self._bar_item = pg.BarGraphItem(
            x=x,
            height=counts.tolist(),
            width=width,
            brush=pg.mkBrush(_BLUE),
            pen=pg.mkPen("#1a1a40"),
        )
        self._plot.addItem(self._bar_item)

        # Gaussian overlay – skip if std is zero (not enough distinct data yet)
        if std > 1e-10:
            x_range = np.linspace(float(edges[0]), float(edges[-1]), 200)
            gauss = (
                (1 / (std * np.sqrt(2 * np.pi)))
                * np.exp(-0.5 * ((x_range - mean) / std) ** 2)
                * n * width
            )
            self._gauss_curve.setData(x_range.tolist(), gauss.tolist())
        else:
            self._gauss_curve.setData([], [])

        self._mean_line.setValue(mean)
        self._mean_line.setVisible(True)
