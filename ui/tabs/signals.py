"""
Signals / Model tab — real-time model-state monitor.

Layout
------
┌──────────────────────────────────────────────────────────────────────┐
│  TOP ROW                                                             │
│  ┌─────────────────────┐  ┌────────────────────────────────────────┐ │
│  │  KEY METRICS        │  │  SPREAD POSITION BAR (price ruler)     │ │
│  │  11 label/value rows│  ├────────────────────────────────────────┤ │
│  │                     │  │  SIGNAL CONTRIBUTIONS TABLE            │ │
│  │                     │  ├──────────────────┬─────────────────────┤ │
│  │                     │  │  ALIGNMENT badge │  OFI PRESSURE GAUGE │ │
│  └─────────────────────┘  └────────────────────────────────────────┘ │
├──────────────────────────────────────────────────────────────────────┤
│  Mid vs Reservation Price chart (rolling)                            │
├──────────────────────────────────────────────────────────────────────┤
│  OFI  │  Inventory  │  Quote Skew  │  Exch Spread   (sparklines)    │
└──────────────────────────────────────────────────────────────────────┘

All metrics are derived from BotState — no recomputation of trading
logic in the UI layer.
"""
import logging

import pyqtgraph as pg
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QFrame, QSizePolicy, QScrollArea,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from telemetry.state import BotState

log = logging.getLogger(__name__)

# ── palette (matches dashboard / microstructure) ──────────────────────────────
_BG     = "#ffffff"
_PANEL  = "#f5f6fa"
_BORDER = "#c8c8d8"
_DIM    = "#8888aa"
_FG     = "#1a1a2e"
_GREEN  = "#00b96b"
_RED    = "#e53935"
_BLUE   = "#1565c0"
_PURPLE = "#7b1fa2"
_ORANGE = "#e65100"
_YELLOW = "#f9a825"
_TEAL   = "#00838f"

# pyqtgraph pens
_PEN_MID  = pg.mkPen(_ORANGE, width=2)
_PEN_RES  = pg.mkPen(_PURPLE, width=2, style=Qt.PenStyle.DashLine)
_PEN_OFI  = pg.mkPen(_TEAL,   width=1.5)
_PEN_INV  = pg.mkPen(_BLUE,   width=1.5)
_PEN_SKEW = pg.mkPen(_PURPLE, width=1.5)
_PEN_SPR  = pg.mkPen(_DIM,    width=1.5)
_PEN_ZERO = pg.mkPen(_DIM,    width=1, style=Qt.PenStyle.DotLine)

_SPARKLINE_H = 90   # px height for sparkline charts
_MID_RES_H   = 200  # px height for mid vs reservation chart


# ── helper: thin horizontal separator ─────────────────────────────────────────
def _hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet(f"color: {_BORDER};")
    return f


def _vline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.VLine)
    f.setStyleSheet(f"color: {_BORDER};")
    return f


# ── helper: create a minimal pyqtgraph plot ────────────────────────────────────
def _make_spark(title: str = "", height: int = _SPARKLINE_H) -> pg.PlotWidget:
    pw = pg.PlotWidget(background=_BG)
    pw.setFixedHeight(height)
    pw.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    pw.showGrid(x=False, y=True, alpha=0.25)
    pw.getPlotItem().hideButtons()
    pw.getPlotItem().setMenuEnabled(False)
    pi = pw.getPlotItem()
    pi.setContentsMargins(0, 0, 0, 0)
    ax_b = pi.getAxis("bottom")
    ax_l = pi.getAxis("left")
    for ax in (ax_b, ax_l):
        ax.setPen(pg.mkPen(_DIM))
        ax.setTextPen(pg.mkPen(_DIM))
        ax.setStyle(tickFont=QFont("Consolas", 8))
    ax_b.setStyle(showValues=False)   # hide X tick labels to save space
    if title:
        pw.setTitle(title, color=_DIM, size="9pt")
    return pw


# ── metric row (dim label + styled value) ─────────────────────────────────────
class _MetricRow(QWidget):
    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 1, 4, 1)
        lay.setSpacing(6)
        self._lbl = QLabel(label)
        self._lbl.setStyleSheet(f"color:{_DIM}; font-size:10px;")
        self._lbl.setFixedWidth(130)
        self._val = QLabel("—")
        self._val.setStyleSheet(f"color:{_FG}; font-size:10px; font-weight:bold;")
        lay.addWidget(self._lbl)
        lay.addWidget(self._val, stretch=1)

    def set(self, text: str, color: str = _FG) -> None:
        self._val.setText(text)
        self._val.setStyleSheet(
            f"color:{color}; font-size:10px; font-weight:bold;"
        )


# ── SignalsTab ─────────────────────────────────────────────────────────────────
class SignalsTab(QWidget):
    """
    Signals / Model tab.  refresh(snap) is called every UI tick; no state is
    held between ticks except the rolling pyqtgraph curves (which are kept as
    PlotCurveItems so setData() replaces data in-place — no widget recreation).
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        # ── TOP ROW ───────────────────────────────────────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        top_row.addWidget(self._build_metrics_panel(), stretch=0)
        top_row.addWidget(_vline())
        top_row.addLayout(self._build_right_panel(), stretch=1)
        root.addLayout(top_row)

        root.addWidget(_hline())

        # ── MID vs RESERVATION CHART ──────────────────────────────────────────
        root.addWidget(self._build_mid_res_chart())

        root.addWidget(_hline())

        # ── SPARKLINE ROW ─────────────────────────────────────────────────────
        root.addLayout(self._build_sparklines())

    # ──────────────────────────────────────────────────────────────────────────
    # Panel builders
    # ──────────────────────────────────────────────────────────────────────────

    def _build_metrics_panel(self) -> QWidget:
        """Left panel: 11 labeled metric rows."""
        panel = QFrame()
        panel.setStyleSheet(
            f"QFrame {{ background:{_PANEL}; border:1px solid {_BORDER};"
            f" border-radius:4px; }}"
        )
        panel.setFixedWidth(270)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(0)

        title = QLabel("  KEY MODEL METRICS")
        title.setStyleSheet(
            f"color:{_DIM}; font-size:9px; font-weight:bold; padding:2px 0px 4px 0px;"
        )
        lay.addWidget(title)
        lay.addWidget(_hline())

        self._m: dict[str, _MetricRow] = {}
        rows = [
            ("mid",           "Mid Price"),
            ("exch_bid",      "Exch Bid"),
            ("exch_ask",      "Exch Ask"),
            ("reservation",   "Reservation"),
            ("inv",           "Inventory"),
            ("norm_inv",      "Norm Inventory"),
            ("ofi",           "OFI Signal"),
            ("quote_skew",    "Quote Skew"),
            ("exch_spread",   "Exch Spread"),
            ("our_spread",    "Our Spread"),
            ("d_best_bid",    "Dist → Best Bid"),
            ("d_best_ask",    "Dist → Best Ask"),
            ("inside_spread", "Inside Spread"),
        ]
        for key, label in rows:
            row = _MetricRow(label)
            self._m[key] = row
            lay.addWidget(row)

        lay.addStretch()
        return panel

    def _build_right_panel(self) -> QVBoxLayout:
        """Right column: spread bar + signal contributions + alignment + gauge."""
        lay = QVBoxLayout()
        lay.setSpacing(6)

        # ── spread position bar ───────────────────────────────────────────────
        spread_lbl = QLabel("  SPREAD POSITION BAR")
        spread_lbl.setStyleSheet(
            f"color:{_DIM}; font-size:9px; font-weight:bold;"
        )
        lay.addWidget(spread_lbl)
        self._spread_bar = self._build_spread_bar()
        lay.addWidget(self._spread_bar)

        lay.addWidget(_hline())

        # ── signal contributions ──────────────────────────────────────────────
        contrib_lbl = QLabel("  SIGNAL CONTRIBUTIONS")
        contrib_lbl.setStyleSheet(
            f"color:{_DIM}; font-size:9px; font-weight:bold;"
        )
        lay.addWidget(contrib_lbl)
        lay.addLayout(self._build_contributions_panel())

        lay.addWidget(_hline())

        # ── alignment + gauge side by side ────────────────────────────────────
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)
        bottom_row.addWidget(self._build_alignment_badge())
        bottom_row.addWidget(_vline())
        bottom_row.addWidget(self._build_gauge(), stretch=1)
        lay.addLayout(bottom_row)

        lay.addStretch()
        return lay

    def _build_spread_bar(self) -> pg.PlotWidget:
        """Horizontal price ruler showing all key levels inside the exchange spread."""
        pw = pg.PlotWidget(background=_BG)
        pw.setFixedHeight(100)
        pw.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        pw.getPlotItem().hideButtons()
        pw.getPlotItem().setMenuEnabled(False)
        pi = pw.getPlotItem()
        pi.setContentsMargins(4, 4, 4, 4)

        # Fixed Y: 0–1.  X floats with prices.
        pw.setYRange(0.0, 1.0, padding=0)
        pi.getAxis("left").setStyle(showValues=False)
        pi.getAxis("left").hide()
        ax_b = pi.getAxis("bottom")
        ax_b.setPen(pg.mkPen(_DIM))
        ax_b.setTextPen(pg.mkPen(_DIM))
        ax_b.setStyle(tickFont=QFont("Consolas", 8))
        pw.showGrid(x=False, y=False)

        # Filled region between our bid and ask (highlight our quote spread)
        self._sb_our_fill = pg.FillBetweenItem(
            pg.PlotCurveItem([0, 0], [0.15, 0.15]),
            pg.PlotCurveItem([0, 0], [0.85, 0.85]),
            brush=pg.mkBrush(21, 101, 192, 30),
        )
        pw.addItem(self._sb_our_fill)

        # Vertical lines for each price level (created once, updated each tick)
        def _iline(color: str, w: float = 1.5, style=Qt.PenStyle.SolidLine):
            il = pg.InfiniteLine(
                angle=90,
                pen=pg.mkPen(color, width=w, style=style),
                movable=False,
            )
            pw.addItem(il)
            return il

        self._sb_exch_bid = _iline(_DIM, 1.5, Qt.PenStyle.DashLine)
        self._sb_exch_ask = _iline(_DIM, 1.5, Qt.PenStyle.DashLine)
        self._sb_mid      = _iline(_ORANGE, 2.5)
        self._sb_res      = _iline(_PURPLE, 2.0, Qt.PenStyle.DashLine)
        self._sb_our_bid  = _iline(_GREEN,  2.0)
        self._sb_our_ask  = _iline(_RED,    2.0)

        # Text labels above each line
        def _txt(color: str, label: str, anchor=(0.5, 1.0)):
            ti = pg.TextItem(text=label, color=color, anchor=anchor)
            ti.setFont(QFont("Consolas", 8))
            pw.addItem(ti)
            return ti

        self._sb_lbl_eb  = _txt(_DIM,    "EB",  (0.5, 1.0))
        self._sb_lbl_ea  = _txt(_DIM,    "EA",  (0.5, 1.0))
        self._sb_lbl_mid = _txt(_ORANGE, "mid", (0.5, 1.0))
        self._sb_lbl_res = _txt(_PURPLE, "res", (0.5, 0.0))
        self._sb_lbl_ob  = _txt(_GREEN,  "bid", (0.5, 0.0))
        self._sb_lbl_oa  = _txt(_RED,    "ask", (0.5, 0.0))

        return pw

    def _build_contributions_panel(self) -> QGridLayout:
        """Signal contribution rows: label | value | bar."""
        lay = QGridLayout()
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(2)
        lay.setColumnStretch(1, 1)

        def _make_pair(label: str, row: int):
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color:{_DIM}; font-size:10px;")
            val = QLabel("—")
            val.setStyleSheet(
                f"color:{_FG}; font-size:10px; font-weight:bold; min-width:70px;"
            )
            lay.addWidget(lbl, row, 0)
            lay.addWidget(val, row, 1)
            return val

        self._c_inv   = _make_pair("Inv skew", 0)
        self._c_ofi   = _make_pair("OFI skew", 1)
        self._c_total = _make_pair("Total skew", 2)
        sep = _hline()
        lay.addWidget(sep, 3, 0, 1, 2)
        self._c_res_vs_mid = _make_pair("Res − Mid", 4)

        return lay

    def _build_alignment_badge(self) -> QWidget:
        """Colored badge showing quote-vs-flow alignment."""
        w = QFrame()
        w.setFixedWidth(140)
        w.setFixedHeight(60)
        w.setStyleSheet(
            f"QFrame {{ background:{_PANEL}; border:1px solid {_BORDER};"
            f" border-radius:4px; }}"
        )
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lbl = QLabel("ALIGNMENT")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(f"color:{_DIM}; font-size:8px;")
        self._align_val = QLabel("—")
        self._align_val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._align_val.setStyleSheet(
            f"color:{_FG}; font-size:11px; font-weight:bold;"
        )
        lay.addWidget(lbl)
        lay.addWidget(self._align_val)
        return w

    def _build_gauge(self) -> pg.PlotWidget:
        """Horizontal OFI pressure gauge: SELL ←|→ BUY."""
        pw = pg.PlotWidget(background=_BG)
        pw.setFixedHeight(60)
        pw.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        pw.getPlotItem().hideButtons()
        pw.getPlotItem().setMenuEnabled(False)
        pi = pw.getPlotItem()

        # Fixed extents: X in [-1, 1], Y in [0, 1]
        pw.setXRange(-1.0, 1.0, padding=0.02)
        pw.setYRange(0.0, 1.0, padding=0)
        for ax_name in ("bottom", "left"):
            ax = pi.getAxis(ax_name)
            ax.setStyle(showValues=False)
            ax.hide()
        pw.showGrid(x=False, y=False)

        # Background track
        bg = pg.BarGraphItem(x=[0.0], height=[0.4], width=2.0, y0=0.3,
                             brush=pg.mkBrush(_BORDER))
        pw.addItem(bg)

        # Center marker
        center = pg.InfiniteLine(pos=0.0, angle=90,
                                 pen=pg.mkPen(_FG, width=1.5))
        pw.addItem(center)

        # Active bar (width and x updated each tick)
        self._gauge_bar = pg.BarGraphItem(
            x=[0.0], height=[0.4], width=0.0, y0=0.3,
            brush=pg.mkBrush(_TEAL)
        )
        pw.addItem(self._gauge_bar)

        # Labels
        sell_txt = pg.TextItem("SELL", color=_RED, anchor=(0.0, 0.5))
        sell_txt.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
        sell_txt.setPos(-0.98, 0.88)
        pw.addItem(sell_txt)

        buy_txt = pg.TextItem("BUY", color=_GREEN, anchor=(1.0, 0.5))
        buy_txt.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
        buy_txt.setPos(0.98, 0.88)
        pw.addItem(buy_txt)

        self._gauge_ofi_label = pg.TextItem("", color=_FG, anchor=(0.5, 0.0))
        self._gauge_ofi_label.setFont(QFont("Consolas", 8))
        self._gauge_ofi_label.setPos(0.0, 0.05)
        pw.addItem(self._gauge_ofi_label)

        return pw

    def _build_mid_res_chart(self) -> pg.PlotWidget:
        """Mid vs Reservation Price rolling chart."""
        pw = _make_spark("Mid vs Reservation Price", height=_MID_RES_H)
        pw.setFixedHeight(_MID_RES_H)
        # Re-show X axis tick values for this larger chart
        pw.getPlotItem().getAxis("bottom").setStyle(showValues=True)
        pw.showGrid(x=True, y=True, alpha=0.2)

        legend = pw.addLegend(offset=(10, 10), labelTextColor=_FG)
        legend.setLabelTextColor(_FG)

        self._mr_mid_curve = pg.PlotCurveItem(
            name="Mid", pen=_PEN_MID, connect="finite"
        )
        self._mr_res_curve = pg.PlotCurveItem(
            name="Reservation", pen=_PEN_RES, connect="finite"
        )
        pw.addItem(self._mr_mid_curve)
        pw.addItem(self._mr_res_curve)

        legend.addItem(self._mr_mid_curve, "Mid")
        legend.addItem(self._mr_res_curve, "Reservation")

        return pw

    def _build_sparklines(self) -> QHBoxLayout:
        """Four small sparkline charts: OFI / Inventory / Quote Skew / Exch Spread."""
        lay = QHBoxLayout()
        lay.setSpacing(6)

        self._sp_ofi  = _make_spark("OFI Signal")
        self._sp_inv  = _make_spark("Inventory (norm)")
        self._sp_skew = _make_spark("Quote Skew (bps)")
        self._sp_spr  = _make_spark("Exch Spread")

        # Curves (one per sparkline)
        self._sp_ofi_curve  = pg.PlotCurveItem(pen=_PEN_OFI,  connect="finite")
        self._sp_inv_curve  = pg.PlotCurveItem(pen=_PEN_INV,  connect="finite")
        self._sp_skew_curve = pg.PlotCurveItem(pen=_PEN_SKEW, connect="finite")
        self._sp_spr_curve  = pg.PlotCurveItem(pen=_PEN_SPR,  connect="finite")

        # Zero reference lines (OFI and skew)
        for sp, curve in [
            (self._sp_ofi,  self._sp_ofi_curve),
            (self._sp_inv,  self._sp_inv_curve),
            (self._sp_skew, self._sp_skew_curve),
            (self._sp_spr,  self._sp_spr_curve),
        ]:
            sp.addItem(curve)
            sp.addItem(pg.InfiniteLine(pos=0.0, angle=0, pen=_PEN_ZERO))

        for sp in (self._sp_ofi, self._sp_inv, self._sp_skew, self._sp_spr):
            lay.addWidget(sp)

        return lay

    # ──────────────────────────────────────────────────────────────────────────
    # Refresh  (called every UI tick from MainWindow._tick)
    # ──────────────────────────────────────────────────────────────────────────

    def refresh(self, snap: BotState) -> None:
        mid   = snap.mid_price
        eb    = snap.bid          # exchange best bid
        ea    = snap.ask          # exchange best ask
        res   = snap.reservation_price
        inv   = snap.inventory
        ni    = snap.norm_inventory
        ofi   = snap.ofi_signal
        inv_b = snap.inv_skew_bps
        ofi_b = snap.ofi_skew_bps

        # Derive our current quotes from series tails (0 if not yet available)
        ob = snap.bid_series[-1] if snap.bid_series else 0.0
        oa = snap.ask_series[-1] if snap.ask_series else 0.0

        # Derived metrics
        exch_spread = ea - eb if ea > 0 and eb > 0 else 0.0
        our_spread  = oa - ob if oa > 0 and ob > 0 else 0.0
        d_bid       = ob - eb if ob > 0 and eb > 0 else 0.0
        d_ask       = ea - oa if ea > 0 and oa > 0 else 0.0
        quote_skew  = res - mid if res > 0 and mid > 0 else 0.0
        inside      = ob > eb and oa < ea if ob > 0 and oa > 0 and eb > 0 and ea > 0 else False

        # Alignment: +1 = aligned, −1 = opposing, 0 = neutral
        sign_ofi = 1 if ofi > 0.01 else (-1 if ofi < -0.01 else 0)
        sign_skew = 1 if quote_skew > 1e-5 else (-1 if quote_skew < -1e-5 else 0)
        alignment = sign_ofi * sign_skew

        # ── Section 1: key metrics panel ──────────────────────────────────────
        self._m["mid"].set(f"{mid:.2f}" if mid else "—")
        self._m["exch_bid"].set(f"{eb:.2f}" if eb else "—", _GREEN)
        self._m["exch_ask"].set(f"{ea:.2f}" if ea else "—", _RED)

        res_color = _PURPLE
        self._m["reservation"].set(f"{res:.4f}" if res else "—", res_color)

        inv_color = _RED if ni > 0.4 else (_GREEN if ni < -0.4 else _FG)
        self._m["inv"].set(f"{inv:+.6f}" if inv != 0 else "0.000000", inv_color)
        self._m["norm_inv"].set(f"{ni:+.3f}", inv_color)

        ofi_color = _GREEN if ofi > 0.1 else (_RED if ofi < -0.1 else _FG)
        self._m["ofi"].set(f"{ofi:+.3f}" if ofi != 0 else "0.000", ofi_color)

        skew_color = _GREEN if quote_skew > 1e-5 else (_RED if quote_skew < -1e-5 else _FG)
        self._m["quote_skew"].set(
            f"{quote_skew:+.4f}" if mid else "—", skew_color
        )
        self._m["exch_spread"].set(
            f"{exch_spread:.4f}" if exch_spread > 0 else "—"
        )
        self._m["our_spread"].set(
            f"{our_spread:.4f}" if our_spread > 0 else "—",
            _BLUE if our_spread > 0 else _FG,
        )
        self._m["d_best_bid"].set(
            f"{d_bid:+.4f}" if ob > 0 and eb > 0 else "—",
            _GREEN if d_bid >= 0 else _RED,
        )
        self._m["d_best_ask"].set(
            f"{d_ask:+.4f}" if oa > 0 and ea > 0 else "—",
            _GREEN if d_ask >= 0 else _RED,
        )
        inside_color = _GREEN if inside else _RED
        self._m["inside_spread"].set("YES ✓" if inside else "NO", inside_color)

        # ── Section 2: spread position bar ────────────────────────────────────
        self._update_spread_bar(eb, ea, mid, res, ob, oa)

        # ── Section 3: signal contributions ───────────────────────────────────
        total_bps = inv_b + ofi_b
        self._c_inv.setText(
            f"{inv_b:+.3f} bps" if inv_b != 0 else "0.000 bps"
        )
        self._c_inv.setStyleSheet(
            f"color:{'#e53935' if inv_b < 0 else '#00b96b'}; font-size:10px; font-weight:bold;"
        )
        self._c_ofi.setText(
            f"{ofi_b:+.3f} bps" if ofi_b != 0 else "0.000 bps"
        )
        self._c_ofi.setStyleSheet(
            f"color:{'#00b96b' if ofi_b > 0 else '#e53935' if ofi_b < 0 else _FG};"
            f" font-size:10px; font-weight:bold;"
        )
        self._c_total.setText(f"{total_bps:+.3f} bps")
        self._c_total.setStyleSheet(
            f"color:{'#00b96b' if total_bps > 0 else '#e53935' if total_bps < 0 else _FG};"
            f" font-size:10px; font-weight:bold;"
        )
        self._c_res_vs_mid.setText(
            f"{quote_skew:+.4f}" if mid else "—"
        )
        self._c_res_vs_mid.setStyleSheet(
            f"color:{skew_color}; font-size:10px; font-weight:bold;"
        )

        # ── Section 6: alignment badge ─────────────────────────────────────────
        if alignment == 1:
            txt, color = "ALIGNED  ✓", _GREEN
        elif alignment == -1:
            txt, color = "OPPOSING ✗", _RED
        else:
            txt, color = "NEUTRAL  ·", _DIM
        self._align_val.setText(txt)
        self._align_val.setStyleSheet(
            f"color:{color}; font-size:11px; font-weight:bold;"
        )

        # ── Section 7: OFI pressure gauge ─────────────────────────────────────
        self._update_gauge(ofi)

        # ── Section 5: Mid vs Reservation chart ───────────────────────────────
        xs    = snap.tick_series
        ps    = snap.price_series
        rs    = snap.reservation_series
        n_xs  = len(xs)
        if n_xs > 0 and len(ps) == n_xs:
            self._mr_mid_curve.setData(xs, ps, connect="finite")
        if n_xs > 0 and len(rs) == n_xs:
            res_clean = [v if v > 0 else float("nan") for v in rs]
            self._mr_res_curve.setData(xs, res_clean, connect="finite")

        # ── Section 4: sparklines ──────────────────────────────────────────────
        ofi_s = snap.ofi_series
        inv_s = snap.inventory_series

        if n_xs > 0:
            if len(ofi_s) == n_xs:
                self._sp_ofi_curve.setData(xs, ofi_s, connect="finite")
                c = _TEAL if (ofi_s[-1] if ofi_s else 0) >= 0 else _RED
                self._sp_ofi_curve.setPen(pg.mkPen(c, width=1.5))

            if len(inv_s) == n_xs:
                self._sp_inv_curve.setData(xs, inv_s, connect="finite")

            # Quote skew history: build from bid/ask series
            bids = snap.bid_series
            asks = snap.ask_series
            if len(bids) == n_xs and len(asks) == n_xs and len(ps) == n_xs:
                # skew_series[i] = (bid[i]+ask[i])/2 - mid[i]  (our mid vs exchange mid)
                skew_s = [
                    ((b + a) / 2.0 - p) if b > 0 and a > 0 and p > 0 else float("nan")
                    for b, a, p in zip(bids, asks, ps)
                ]
                self._sp_skew_curve.setData(xs, skew_s, connect="finite")

            # Exchange spread history
            mb = snap.mkt_bid_series
            ma = snap.mkt_ask_series
            if len(mb) == n_xs and len(ma) == n_xs:
                spr_s = [
                    a - b if a > 0 and b > 0 else float("nan")
                    for b, a in zip(mb, ma)
                ]
                self._sp_spr_curve.setData(xs, spr_s, connect="finite")

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _update_spread_bar(
        self,
        eb: float, ea: float,
        mid: float, res: float,
        ob: float, oa: float,
    ) -> None:
        """Position InfiniteLines and labels on the spread-bar chart."""
        if not (eb > 0 and ea > 0 and mid > 0):
            return

        # Pad X range slightly outside the exchange spread
        pad = (ea - eb) * 0.5
        self._spread_bar.setXRange(eb - pad, ea + pad, padding=0)

        # Update line positions
        for line, px in [
            (self._sb_exch_bid, eb),
            (self._sb_exch_ask, ea),
            (self._sb_mid,      mid),
            (self._sb_res,      res if res > 0 else mid),
            (self._sb_our_bid,  ob if ob > 0 else eb),
            (self._sb_our_ask,  oa if oa > 0 else ea),
        ]:
            line.setPos(px)

        # Update label positions  (y=0.92 = just below top edge of the fixed Y 0-1 range)
        y_top, y_bot = 0.92, 0.10
        for label, px, y in [
            (self._sb_lbl_eb,  eb,                              y_top),
            (self._sb_lbl_ea,  ea,                              y_top),
            (self._sb_lbl_mid, mid,                             y_top),
            (self._sb_lbl_res, res if res > 0 else mid,         y_bot),
            (self._sb_lbl_ob,  ob if ob > 0 else eb,            y_bot),
            (self._sb_lbl_oa,  oa if oa > 0 else ea,            y_bot),
        ]:
            label.setPos(px, y)

        # Update our-spread fill region
        if ob > 0 and oa > 0:
            self._sb_our_fill.setCurves(
                pg.PlotCurveItem([ob, oa], [0.15, 0.15]),
                pg.PlotCurveItem([ob, oa], [0.85, 0.85]),
            )

    def _update_gauge(self, ofi: float) -> None:
        """Redraw the OFI pressure bar."""
        # Bar spans from 0 to ofi (signed), centered at x=0
        bar_x  = ofi / 2.0         # center of bar
        bar_w  = abs(ofi)
        color  = _GREEN if ofi >= 0 else _RED
        self._gauge_bar.setOpts(
            x=[bar_x], width=[bar_w], height=[0.4], y0=0.3,
            brush=pg.mkBrush(color),
        )
        self._gauge_ofi_label.setText(f"OFI {ofi:+.3f}")
        self._gauge_ofi_label.setPos(ofi / 2.0, 0.05)
