"""
OFI Analysis tab — measures how well the OFI signal predicts future
price direction and magnitude across four forward horizons.

Layout
------
┌──────────────────────────────────────────────────────────────────────┐
│  ACCURACY SUMMARY TABLE                                              │
│  Horizon │ Hit Rate │ Correlation │ Samples                          │
├──────────────────────────────────────────────────────────────────────┤
│  ROLLING HIT RATE chart  (30-pair sliding window, 4 horizons)       │
│  y: 0–100 %   centre dashed line at 50 %                            │
├──────────────────────────────────────────────────────────────────────┤
│  SCATTER PLOTS  OFI (X)  vs  Forward Return (Y)  — 2 × 2 grid      │
│  Green dot = correct directional call   Red dot = wrong call        │
└──────────────────────────────────────────────────────────────────────┘

All computation is local to refresh(); no new telemetry fields needed.
Data source: snap.ofi_series, snap.price_series, snap.tick_series
  (all length-300 rolling buffers, always same length).

Statistics per horizon h:
  • forward_return[i] = (price[i+h] − price[i]) / price[i]
  • hit[i]           = sign(ofi[i]) == sign(forward_return[i])
                       (skipped when ofi[i] == 0 or return[i] == 0)
  • hit_rate         = hits / valid_pairs
  • correlation      = Pearson(ofi_values, forward_returns)
  • rolling_hit_rate: sliding window of 30 consecutive pairs
"""
import math
import logging

import numpy as np
import pyqtgraph as pg
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QFrame, QSizePolicy,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from telemetry.state import BotState

log = logging.getLogger(__name__)

# ── palette (matches the rest of the UI) ──────────────────────────────────────
_BG     = "#ffffff"
_PANEL  = "#f5f6fa"
_BORDER = "#c8c8d8"
_DIM    = "#8888aa"
_FG     = "#1a1a2e"
_GREEN  = "#00b96b"
_YELLOW = "#f9a825"
_RED    = "#e53935"
_BLUE   = "#1565c0"
_PURPLE = "#7b1fa2"
_TEAL   = "#00838f"

# Horizon definitions  ─────────────────────────────────────────────────────────
_HORIZONS = [5, 10, 30, 60]          # ticks ahead
_HORIZON_LABELS = ["5 ticks", "10 ticks", "30 ticks", "60 ticks"]
_HORIZON_COLORS = [_BLUE, _TEAL, _PURPLE, _YELLOW]   # one per horizon

_ROLL_WINDOW = 30       # pairs per rolling-hit-rate window
_MIN_PAIRS   = 10       # minimum valid pairs before showing a metric


# ── helpers ───────────────────────────────────────────────────────────────────

def _hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet(f"color: {_BORDER};")
    return f


def _hit_color(rate: float) -> str:
    if rate >= 0.55:
        return _GREEN
    if rate >= 0.50:
        return _YELLOW
    return _RED


def _corr_color(c: float) -> str:
    if c > 0.10:
        return _GREEN
    if c >= 0.0:
        return _YELLOW
    return _RED


def _make_plot(title: str, height: int, x_label: str = "", y_label: str = "") -> pg.PlotWidget:
    pw = pg.PlotWidget(background=_BG)
    pw.setFixedHeight(height)
    pw.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    pw.showGrid(x=True, y=True, alpha=0.20)
    pi = pw.getPlotItem()
    pi.hideButtons()
    pi.setMenuEnabled(False)
    pi.setContentsMargins(2, 2, 2, 2)
    for ax_name in ("bottom", "left"):
        ax = pi.getAxis(ax_name)
        ax.setPen(pg.mkPen(_DIM))
        ax.setTextPen(pg.mkPen(_DIM))
        ax.setStyle(tickFont=QFont("Consolas", 8))
    if title:
        pw.setTitle(title, color=_DIM, size="9pt")
    if x_label:
        pi.setLabel("bottom", x_label, color=_DIM)
    if y_label:
        pi.setLabel("left", y_label, color=_DIM)
    return pw


def _pearson(xs: list, ys: list) -> float:
    """Return Pearson correlation; 0.0 if degenerate."""
    n = len(xs)
    if n < 3:
        return 0.0
    arr = np.array([[xs[i], ys[i]] for i in range(n)], dtype=float)
    # numpy corrcoef on two 1-D arrays is fastest
    r = np.corrcoef(arr[:, 0], arr[:, 1])
    val = float(r[0, 1])
    return 0.0 if math.isnan(val) else val


def _compute_pairs(ofi: list, prices: list, h: int):
    """
    Return (ofi_vals, fwd_returns, hits) for horizon h.

    A pair is 'valid' when ofi[i] != 0 AND forward_return[i] != 0.
    Correlation is computed on ALL (ofi, return) pairs (incl. zero OFI).
    Hit-rate and rolling hit-rate use only valid pairs.
    """
    n = len(ofi)
    if n < h + 1 or len(prices) != n:
        return [], [], []

    ofi_all, ret_all = [], []
    ofi_valid, ret_valid, hits = [], [], []

    for i in range(n - h):
        p0 = prices[i]
        ph = prices[i + h]
        if p0 <= 0:
            continue
        ret = (ph - p0) / p0
        ofi_all.append(ofi[i])
        ret_all.append(ret)

        o = ofi[i]
        if o == 0.0 or ret == 0.0:
            continue
        ofi_valid.append(o)
        ret_valid.append(ret)
        hit = (o > 0) == (ret > 0)
        hits.append(1 if hit else 0)

    return (ofi_all, ret_all), (ofi_valid, ret_valid, hits)


def _rolling_hit_rate(hits: list, window: int) -> list:
    """Sliding-window hit rate (fraction in [0,1])."""
    result = []
    for i in range(len(hits)):
        start = max(0, i - window + 1)
        seg = hits[start: i + 1]
        result.append(sum(seg) / len(seg))
    return result


# ── OFIAnalysisTab ─────────────────────────────────────────────────────────────

class OFIAnalysisTab(QWidget):
    """
    OFI predictive accuracy analysis.
    All heavy lifting done in refresh(); widget structure built once in __init__.
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        # ── Section 1: accuracy summary table ────────────────────────────────
        root.addWidget(self._build_summary_panel())
        root.addWidget(_hline())

        # ── Section 2: rolling hit rate chart ────────────────────────────────
        root.addWidget(self._build_rolling_chart())
        root.addWidget(_hline())

        # ── Section 3: scatter plots 2 × 2 ───────────────────────────────────
        root.addLayout(self._build_scatter_grid())

        # small note at the bottom
        note = QLabel(
            "Note: 300-sample rolling buffer (~5 min).  "
            "Hit rate excludes zero-OFI and zero-return ticks.  "
            "Correlation uses all ticks.  Minimum 10 pairs required to display."
        )
        note.setStyleSheet(f"color:{_DIM}; font-size:9px; padding:2px;")
        note.setWordWrap(True)
        root.addWidget(note)

    # ──────────────────────────────────────────────────────────────────────────
    # Panel builders
    # ──────────────────────────────────────────────────────────────────────────

    def _build_summary_panel(self) -> QWidget:
        panel = QFrame()
        panel.setStyleSheet(
            f"QFrame {{ background:{_PANEL}; border:1px solid {_BORDER};"
            f" border-radius:4px; }}"
        )
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        title = QLabel("  ACCURACY SUMMARY  —  OFI directional hit rate & Pearson correlation vs forward return")
        title.setStyleSheet(
            f"color:{_DIM}; font-size:9px; font-weight:bold; padding-bottom:4px;"
        )
        outer.addWidget(title)

        grid = QGridLayout()
        grid.setSpacing(4)
        grid.setContentsMargins(4, 0, 4, 0)

        # Header row
        for col, (text, align) in enumerate([
            ("Horizon",      Qt.AlignmentFlag.AlignLeft),
            ("Hit Rate",     Qt.AlignmentFlag.AlignCenter),
            ("Correlation",  Qt.AlignmentFlag.AlignCenter),
            ("Samples",      Qt.AlignmentFlag.AlignCenter),
            ("",             Qt.AlignmentFlag.AlignLeft),   # status note
        ]):
            lbl = QLabel(text)
            lbl.setAlignment(align)
            lbl.setStyleSheet(
                f"color:{_DIM}; font-size:9px; font-weight:bold;"
                f" border-bottom:1px solid {_BORDER}; padding:2px 6px;"
            )
            grid.addWidget(lbl, 0, col)

        # Data rows — one per horizon
        self._sum_rows: list[dict] = []
        for row_i, (h, label, color) in enumerate(
            zip(_HORIZONS, _HORIZON_LABELS, _HORIZON_COLORS), start=1
        ):
            h_lbl = QLabel(label)
            h_lbl.setStyleSheet(f"color:{color}; font-size:10px; padding:2px 6px;")
            h_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)

            hr_lbl = QLabel("—")
            hr_lbl.setStyleSheet(f"color:{_FG}; font-size:10px; font-weight:bold; padding:2px 6px;")
            hr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

            corr_lbl = QLabel("—")
            corr_lbl.setStyleSheet(f"color:{_FG}; font-size:10px; font-weight:bold; padding:2px 6px;")
            corr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

            n_lbl = QLabel("—")
            n_lbl.setStyleSheet(f"color:{_DIM}; font-size:10px; padding:2px 6px;")
            n_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

            note_lbl = QLabel("")
            note_lbl.setStyleSheet(f"color:{_DIM}; font-size:9px; padding:2px 4px;")

            grid.addWidget(h_lbl,    row_i, 0)
            grid.addWidget(hr_lbl,   row_i, 1)
            grid.addWidget(corr_lbl, row_i, 2)
            grid.addWidget(n_lbl,    row_i, 3)
            grid.addWidget(note_lbl, row_i, 4)

            self._sum_rows.append({
                "hit":  hr_lbl,
                "corr": corr_lbl,
                "n":    n_lbl,
                "note": note_lbl,
            })

        grid.setColumnStretch(4, 1)
        outer.addLayout(grid)
        return panel

    def _build_rolling_chart(self) -> pg.PlotWidget:
        pw = _make_plot(
            "Rolling Hit Rate  (30-pair sliding window)",
            height=180,
            x_label="tick",
            y_label="%",
        )
        pw.setYRange(0, 100, padding=0.05)

        # 50 % reference line
        ref = pg.InfiniteLine(
            pos=50, angle=0,
            pen=pg.mkPen(_DIM, width=1, style=Qt.PenStyle.DashLine),
        )
        pw.addItem(ref)

        legend = pw.addLegend(offset=(10, 10), labelTextColor=_FG)
        self._roll_curves: list[pg.PlotCurveItem] = []
        for label, color in zip(_HORIZON_LABELS, _HORIZON_COLORS):
            curve = pg.PlotCurveItem(
                name=label,
                pen=pg.mkPen(color, width=1.8),
                connect="finite",
            )
            pw.addItem(curve)
            legend.addItem(curve, label)
            self._roll_curves.append(curve)

        return pw

    def _build_scatter_grid(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setSpacing(6)

        self._scatter_plots: list[pg.PlotWidget] = []
        self._scatter_hits:  list[pg.ScatterPlotItem] = []
        self._scatter_miss:  list[pg.ScatterPlotItem] = []
        self._scatter_zero:  list[pg.ScatterPlotItem] = []

        positions = [(0, 0), (0, 1), (1, 0), (1, 1)]
        for idx, (h, label, color) in enumerate(
            zip(_HORIZONS, _HORIZON_LABELS, _HORIZON_COLORS)
        ):
            pw = _make_plot(
                f"OFI vs  +{h}-tick  Return",
                height=200,
                x_label="OFI signal",
                y_label="fwd return",
            )
            pw.setXRange(-1.05, 1.05, padding=0)

            # Zero reference lines
            pw.addItem(pg.InfiniteLine(
                pos=0, angle=90,
                pen=pg.mkPen(_DIM, width=1, style=Qt.PenStyle.DotLine),
            ))
            pw.addItem(pg.InfiniteLine(
                pos=0, angle=0,
                pen=pg.mkPen(_DIM, width=1, style=Qt.PenStyle.DotLine),
            ))

            # Hit (correct call), miss (wrong call), zero-signal
            s_hit  = pg.ScatterPlotItem(size=5, pen=None, brush=pg.mkBrush(_GREEN + "99"))
            s_miss = pg.ScatterPlotItem(size=5, pen=None, brush=pg.mkBrush(_RED   + "99"))
            s_zero = pg.ScatterPlotItem(size=4, pen=None, brush=pg.mkBrush(_DIM   + "66"))
            pw.addItem(s_hit)
            pw.addItem(s_miss)
            pw.addItem(s_zero)

            self._scatter_plots.append(pw)
            self._scatter_hits.append(s_hit)
            self._scatter_miss.append(s_miss)
            self._scatter_zero.append(s_zero)

            r, c = positions[idx]
            grid.addWidget(pw, r, c)

        return grid

    # ──────────────────────────────────────────────────────────────────────────
    # Refresh  (called every UI tick)
    # ──────────────────────────────────────────────────────────────────────────

    def refresh(self, snap: BotState) -> None:
        ofi    = snap.ofi_series
        prices = snap.price_series
        ticks  = snap.tick_series
        n      = len(ofi)

        # Need at least price+tick alignment and enough samples for longest horizon
        if n < _HORIZONS[-1] + _MIN_PAIRS or len(prices) != n or len(ticks) != n:
            # Clear everything and wait for more data
            for row in self._sum_rows:
                row["hit"].setText("—")
                row["corr"].setText("—")
                row["n"].setText("—")
                row["note"].setText("warming up…")
            for c in self._roll_curves:
                c.setData([], [])
            for i in range(len(_HORIZONS)):
                self._scatter_hits[i].setData([], [])
                self._scatter_miss[i].setData([], [])
                self._scatter_zero[i].setData([], [])
            return

        for idx, h in enumerate(_HORIZONS):
            (ofi_all, ret_all), (ofi_v, ret_v, hits) = _compute_pairs(ofi, prices, h)

            n_all   = len(ofi_all)
            n_valid = len(hits)
            row     = self._sum_rows[idx]

            # ── summary table ─────────────────────────────────────────────────
            if n_valid >= _MIN_PAIRS:
                rate = sum(hits) / n_valid
                hr_text  = f"{rate * 100:.1f} %"
                hr_color = _hit_color(rate)
                row["hit"].setText(hr_text)
                row["hit"].setStyleSheet(
                    f"color:{hr_color}; font-size:10px; font-weight:bold; padding:2px 6px;"
                )
            else:
                row["hit"].setText("—")
                row["hit"].setStyleSheet(
                    f"color:{_DIM}; font-size:10px; font-weight:bold; padding:2px 6px;"
                )

            if n_all >= _MIN_PAIRS:
                corr      = _pearson(ofi_all, ret_all)
                corr_text = f"{corr:+.3f}"
                row["corr"].setText(corr_text)
                row["corr"].setStyleSheet(
                    f"color:{_corr_color(corr)}; font-size:10px; font-weight:bold; padding:2px 6px;"
                )
            else:
                row["corr"].setText("—")
                row["corr"].setStyleSheet(
                    f"color:{_DIM}; font-size:10px; font-weight:bold; padding:2px 6px;"
                )

            row["n"].setText(str(n_valid))
            if n_valid < _MIN_PAIRS:
                row["note"].setText(f"need {_MIN_PAIRS - n_valid} more pairs")
            elif n_valid < 30:
                row["note"].setText("low sample count")
            else:
                row["note"].setText("")

            # ── rolling hit rate ──────────────────────────────────────────────
            if n_valid >= _ROLL_WINDOW:
                roll = _rolling_hit_rate(hits, _ROLL_WINDOW)
                # X axis: use tick index of each valid pair (approximate — we
                # track how many ofi[i] had non-zero ofi and non-zero return)
                roll_x = list(range(len(roll)))
                roll_y = [r * 100.0 for r in roll]
                self._roll_curves[idx].setData(roll_x, roll_y, connect="finite")
            else:
                self._roll_curves[idx].setData([], [])

            # ── scatter plot ──────────────────────────────────────────────────
            hit_x, hit_y   = [], []
            miss_x, miss_y = [], []
            zero_x, zero_y = [], []

            for i in range(len(ofi_all)):
                o = ofi_all[i]
                r = ret_all[i]
                if o == 0.0 or r == 0.0:
                    zero_x.append(o); zero_y.append(r)
                elif (o > 0) == (r > 0):
                    hit_x.append(o); hit_y.append(r)
                else:
                    miss_x.append(o); miss_y.append(r)

            self._scatter_hits[idx].setData(x=hit_x,  y=hit_y)
            self._scatter_miss[idx].setData(x=miss_x, y=miss_y)
            self._scatter_zero[idx].setData(x=zero_x, y=zero_y)
