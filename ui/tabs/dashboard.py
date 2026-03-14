"""
Dashboard tab: KPI cards grid + 3 real-time pyqtgraph plots.

Layout
------
┌──────────────────────────────────────────────────────────────────┐
│  Market │ Mid Price │ Spread │ TPM  │ Bid │ Ask │ Inv │ Avg Px  │
├──────────────────────────────────────────────────────────────────┤
│  Cash   │   rPnL   │  uPnL  │  Total PnL  │ Trades │  Notional │
├──────────────────────────────────────────────────────────────────┤
│                        Price chart                               │
│                       Inventory chart                            │
│                         PnL chart                                │
└──────────────────────────────────────────────────────────────────┘
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QGridLayout,
    QLabel, QFrame, QSizePolicy,
)
from PySide6.QtCore import Qt

import pyqtgraph as pg

from telemetry.state import BotState

# ── palette ───────────────────────────────────────────────────────────────────
_BG      = "#ffffff"
_PANEL   = "#f5f6fa"
_BORDER  = "#c8c8d8"
_DIM     = "#8888aa"
_FG      = "#1a1a2e"
_GREEN   = "#00e676"
_RED     = "#ff4444"
_BLUE    = "#4d9fff"
_PURPLE  = "#a855f7"
_YELLOW  = "#ffd740"

# ── candlestick chart tuning ──────────────────────────────────────────────────
_CANDLE_BUCKET = 15   # price samples per candle  (≈1 sample/s → 15-s candles)
_VWAP_WINDOW   = 20   # rolling window in candles for the MA overlay


def _pnl_clr(v: float) -> str:
    return _GREEN if v >= 0 else _RED


def _inv_clr(v: float) -> str:
    if v > 1e-7:
        return _GREEN
    if v < -1e-7:
        return _RED
    return _FG


# ── KPI card ──────────────────────────────────────────────────────────────────

class _KpiCard(QFrame):
    """Small card: dim label on top, large bold value below."""

    _VAL_STYLE = (
        "color:{color}; font-size:15px; font-weight:bold;"
        " font-family:Consolas,monospace; background:transparent; border:none;"
    )
    _LBL_STYLE = (
        f"color:{_DIM}; font-size:9px; font-weight:bold; letter-spacing:1px;"
        " text-transform:uppercase; background:transparent; border:none;"
    )

    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            f"QFrame{{background:{_PANEL};border:1px solid {_BORDER};border-radius:4px;}}"
        )
        self.setMinimumHeight(54)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

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


# ── thin pyqtgraph chart (inventory / PnL) ───────────────────────────────────

class _Chart(pg.PlotWidget):
    """Pre-configured PlotWidget with a single curve; call update_data() each tick."""

    def __init__(self, title: str, color: str, parent=None) -> None:
        super().__init__(parent, background=_BG)
        pi = self.getPlotItem()
        pi.setTitle(title, color=_DIM, size="8pt")
        pi.showGrid(x=True, y=True, alpha=0.20)
        for axis in ("left", "bottom"):
            pi.getAxis(axis).setPen(_BORDER)
            pi.getAxis(axis).setTextPen(_DIM)
        pi.setMenuEnabled(False)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._curve = self.plot(pen=pg.mkPen(color, width=2.5), antialias=True)
        self._zero = pg.InfiniteLine(
            pos=0, angle=0,
            pen=pg.mkPen(_BORDER, width=1, style=Qt.PenStyle.DashLine),
        )
        self.addItem(self._zero)

    def update_data(self, data: list) -> None:
        if not data:
            self._curve.setData([], [])
            return
        n = len(data)
        self._curve.setData(list(range(n)), data)


# ── OHLC candlestick chart (price) ────────────────────────────────────────────

class _OHLCChart(pg.PlotWidget):
    """
    15-second OHLC candlestick chart built from the 1-sample/s mid-price series.

    Pipeline
    --------
    price_series (list of floats, 1/s)
      → group into _CANDLE_BUCKET-sample buckets  → (open, high, low, close)
      → wick lines via PlotCurveItem(connect='pairs')
      → body rectangles via BarGraphItem (per-bar green/red colour)
      → rolling MA(_VWAP_WINDOW) overlay
      → Y-axis clamped to [min(low) - pad, max(high) + pad]
      → live bid / ask horizontal reference lines
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent, background=_BG)
        pi = self.getPlotItem()
        pi.setTitle(
            f"Price  (OHLC · {_CANDLE_BUCKET} s  |  MA {_VWAP_WINDOW})",
            color=_DIM, size="8pt",
        )
        pi.showGrid(x=True, y=True, alpha=0.20)
        for axis in ("left", "bottom"):
            pi.getAxis(axis).setPen(_BORDER)
            pi.getAxis(axis).setTextPen(_DIM)
        pi.setMenuEnabled(False)
        pi.hideAxis("bottom")       # bucket index carries no wall-clock meaning
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Layer order: wicks → bodies → MA → bid/ask lines
        self._wicks = pg.PlotCurveItem(antialias=False)
        self._wicks.setPen(pg.mkPen(_DIM, width=1.5))
        self.addItem(self._wicks)

        self._bars = pg.BarGraphItem(x=[], y=[], height=[], width=0.6)
        self.addItem(self._bars)

        self._ma = pg.PlotCurveItem(antialias=False)
        self._ma.setPen(pg.mkPen(_YELLOW, width=2.0))
        self.addItem(self._ma)

        self._bid_line = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen(_GREEN, width=1, style=Qt.PenStyle.DotLine),
            movable=False,
        )
        self._ask_line = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen(_RED, width=1, style=Qt.PenStyle.DotLine),
            movable=False,
        )
        self.addItem(self._bid_line)
        self.addItem(self._ask_line)

    # ------------------------------------------------------------------
    def update_data(
        self, price_series: list, bid: float = 0.0, ask: float = 0.0
    ) -> None:
        if not price_series:
            self._wicks.setData([], [])
            self._bars.setOpts(x=[], y=[], height=[], width=0.6)
            self._ma.setData([], [])
            return

        # ── 1. Build OHLC candles ─────────────────────────────────────────────
        # Each bucket is _CANDLE_BUCKET consecutive 1-s mid-price samples.
        candles: list[tuple] = []   # (x_idx, open, high, low, close)
        for i in range(0, len(price_series), _CANDLE_BUCKET):
            bucket = price_series[i : i + _CANDLE_BUCKET]
            if not bucket:
                continue
            candles.append((
                len(candles),   # sequential x-index (0, 1, 2, …)
                bucket[0],      # open
                max(bucket),    # high
                min(bucket),    # low
                bucket[-1],     # close
            ))

        if not candles:
            return

        # ── 2. Wicks: one vertical line per candle via connect='pairs' ─────────
        # Interleaved layout: [x0, x0, x1, x1, …] / [low0, high0, low1, high1, …]
        wick_x: list[float] = []
        wick_y: list[float] = []
        for (x, _o, h, l, _c) in candles:
            wick_x.extend([x, x])
            wick_y.extend([l, h])
        self._wicks.setData(x=wick_x, y=wick_y, connect="pairs")

        # ── 3. Bodies ─────────────────────────────────────────────────────────
        bar_x   = [c[0] for c in candles]
        bar_bot = [min(c[1], c[4]) for c in candles]           # bottom of body
        bar_h   = [max(abs(c[4] - c[1]), 0.001) for c in candles]  # body height
        brushes = [
            pg.mkBrush(_GREEN if c[4] >= c[1] else _RED) for c in candles
        ]
        pens = [
            pg.mkPen(_GREEN if c[4] >= c[1] else _RED, width=1.0) for c in candles
        ]
        self._bars.setOpts(
            x=bar_x, y=bar_bot, height=bar_h,
            width=0.6, brushes=brushes, pens=pens,
        )

        # ── 4. Rolling MA over close prices (equal-weight, no per-tick volume) ─
        closes = [c[4] for c in candles]
        xs     = [c[0] for c in candles]
        ma: list[float] = []
        for i in range(len(closes)):
            start   = max(0, i - _VWAP_WINDOW + 1)
            window  = closes[start : i + 1]
            ma.append(sum(window) / len(window))
        self._ma.setData(xs, ma)

        # ── 5. Y-axis: fit exactly to visible candle range + 5 % padding ──────
        y_min = min(c[3] for c in candles)
        y_max = max(c[2] for c in candles)
        pad   = max((y_max - y_min) * 0.05, 0.05)
        self.getPlotItem().setYRange(y_min - pad, y_max + pad, padding=0)

        # ── 6. Bid / ask reference lines ──────────────────────────────────────
        if bid > 0:
            self._bid_line.setValue(bid)
        if ask > 0:
            self._ask_line.setValue(ask)


# ── DashboardTab ──────────────────────────────────────────────────────────────

class DashboardTab(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(5)

        # ── row 0: market / quote / activity ─────────────────────────────────
        g0 = QGridLayout()
        g0.setSpacing(4)
        self._k_market  = _KpiCard("Market")
        self._k_mid     = _KpiCard("Mid Price")
        self._k_spread  = _KpiCard("Spread")
        self._k_tpm     = _KpiCard("TPM")
        self._k_bid     = _KpiCard("Bid")
        self._k_ask     = _KpiCard("Ask")
        self._k_inv      = _KpiCard("Inventory")
        self._k_avg      = _KpiCard("Avg Price")
        self._k_norm_inv = _KpiCard("Norm Inv")
        for col, card in enumerate([self._k_market, self._k_mid, self._k_spread, self._k_tpm]):
            g0.addWidget(card, 0, col)
        for col, card in enumerate([self._k_bid, self._k_ask, self._k_inv, self._k_avg, self._k_norm_inv]):
            g0.addWidget(card, 1, col)
        root.addLayout(g0)

        # ── row 1: accounting ─────────────────────────────────────────────────
        g1 = QGridLayout()
        g1.setSpacing(4)
        self._k_cash    = _KpiCard("Cash")
        self._k_rpnl    = _KpiCard("Realized PnL")
        self._k_upnl    = _KpiCard("Unrealized PnL")
        self._k_totpnl  = _KpiCard("Total PnL")
        self._k_edge    = _KpiCard("Total Edge Collected")
        self._k_trades  = _KpiCard("Trades")
        self._k_vol     = _KpiCard("Volume")
        self._k_notl    = _KpiCard("Notional")
        self._k_buysell = _KpiCard("Buy / Sell Vol")
        for col, card in enumerate([self._k_cash, self._k_rpnl, self._k_upnl, self._k_totpnl, self._k_edge]):
            g1.addWidget(card, 0, col)
        for col, card in enumerate([self._k_trades, self._k_vol, self._k_notl, self._k_buysell]):
            g1.addWidget(card, 1, col)
        root.addLayout(g1)

        # ── charts ────────────────────────────────────────────────────────────
        self._chart_price = _OHLCChart()
        self._chart_inv   = _Chart("Inventory  (base)", _PURPLE)
        self._chart_pnl   = _Chart("PnL  mark-to-market  (USD)", _GREEN)
        root.addWidget(self._chart_price)
        root.addWidget(self._chart_inv)
        root.addWidget(self._chart_pnl)

    # ──────────────────────────────────────────────────────────────────────────

    def refresh(self, snap: BotState) -> None:
        # ── row 0 ─────────────────────────────────────────────────────────────
        self._k_market.set(snap.selected_market or "—")
        self._k_mid.set(f"{snap.mid_price:,.4f}" if snap.mid_price else "—")
        self._k_spread.set(
            f"{snap.spread_bps:.2f} bps",
            _YELLOW if snap.spread_bps > 0 else _DIM,
        )
        self._k_tpm.set(f"{snap.tpm:.1f}")
        self._k_bid.set(f"{snap.bid:,.4f}" if snap.bid else "—", _GREEN)
        self._k_ask.set(f"{snap.ask:,.4f}" if snap.ask else "—", _RED)
        inv_sign = "+" if snap.inventory > 0 else ""
        self._k_inv.set(f"{inv_sign}{snap.inventory:.6f}", _inv_clr(snap.inventory))
        self._k_avg.set(f"{snap.avg_price:,.4f}" if snap.avg_price else "—")
        ni = snap.norm_inventory
        ni_color = _RED if ni > 0.6 else (_GREEN if ni < -0.6 else _FG)
        self._k_norm_inv.set(f"{ni:+.3f}", ni_color)

        # ── row 1 ─────────────────────────────────────────────────────────────
        self._k_cash.set(f"${snap.cash_usd:+,.2f}", _pnl_clr(snap.cash_usd))
        self._k_rpnl.set(f"${snap.realized_pnl:+,.2f}", _pnl_clr(snap.realized_pnl))
        self._k_upnl.set(f"${snap.unrealized_pnl:+,.2f}", _pnl_clr(snap.unrealized_pnl))
        self._k_totpnl.set(f"${snap.total_pnl:+,.2f}", _pnl_clr(snap.total_pnl))
        self._k_edge.set(f"${snap.total_edge_collected:+,.2f}", _GREEN)
        self._k_trades.set(str(snap.total_trades))
        self._k_vol.set(f"{snap.total_volume:.4f}")
        self._k_notl.set(f"${snap.total_notional:,.0f}")
        self._k_buysell.set(
            f"{snap.buy_volume:.4f} / {snap.sell_volume:.4f}",
            _BLUE,
        )

        # ── charts ────────────────────────────────────────────────────────────
        self._chart_price.update_data(snap.price_series, snap.bid, snap.ask)
        self._chart_inv.update_data(snap.inventory_series)
        self._chart_pnl.update_data(snap.pnl_series)
