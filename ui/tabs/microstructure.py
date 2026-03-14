"""
Microstructure tab  ·  quote-evolution & fill-event replay chart.

Shows in a single time-aligned plot:
  ── Mid price line           (neutral / foreground, thick solid)
  ── Our quoted Bid line      (blue, thick solid)        ← primary
  ── Our quoted Ask line      (red,  thick solid)        ← primary
  -- Market best Bid line     (blue, thin dashed)        ← secondary / BBO
  -- Market best Ask line     (red,  thin dashed)        ← secondary / BBO
  ── Spread shading           (semi-transparent blue fill between our Bid and Ask)

Three distinct marker categories (shape + color encode role + direction):

  △  green triangle  — bot passive BUY  fill  (bot bought at our quoted bid)
  ▽  red   triangle  — bot passive SELL fill  (bot sold  at our quoted ask)
  ■  green square    — public tape BUY  trade  (market aggressor bought)
  ■  red   square    — public tape SELL trade  (market aggressor sold)
  ★  green star      — bot aggressive BUY  (hedge / market order: bot bought)
  ★  red   star      — bot aggressive SELL (hedge / market order: bot sold)

Data sources
────────────
  tick_series       → BotState.tick_series        (heartbeat tick for each price sample)
  price_series      → BotState.price_series       (mid, 1 sample / heartbeat tick)
  bid_series        → BotState.bid_series         (our quoted bid, same cadence)
  ask_series        → BotState.ask_series         (our quoted ask, same cadence)
  mkt_bid_series    → BotState.mkt_bid_series     (market TOB best bid, same cadence)
  mkt_ask_series    → BotState.mkt_ask_series     (market TOB best ask, same cadence)
  fill_events       → BotState.fill_events        (FillEvent, side="BUY"/"SELL"/"HEDGE_BUY"/"HEDGE_SELL")
  tape_trade_events → BotState.tape_trade_events  (TapeTradeEvent, public tape trades)

X-axis: ABSOLUTE HEARTBEAT TICK (same coordinate for lines AND event markers)
─────────────────────────────────────────────────────────────────────────────
All series lines are plotted as  (tick_series[i], price_series[i]).
All event markers are plotted at  (event.tick, event.price).

Because both use the same tick space there is NO drift, NO rebasing, and NO
index-vs-tick mismatch.  Old entries rotate out of the deque but existing
events keep their original tick coordinate forever.

Clock synchronisation
─────────────────────
  • market book updates (mkt_bid/ask)  ─┐ both sampled at the SAME heartbeat
  • our quoted bid/ask                  ─┘ in app.py → naturally aligned
  • tape trades      → tick = heartbeat tick when the trade was pushed to store
  • passive fills    → tick = paper.current_tick == heartbeat tick of the fill
  All four time sources therefore share the same tick coordinate system.

Inherent 1-tick lag between book and our quotes
────────────────────────────────────────────────
  The bot reads the TOB, then calls make_quote(), then both are sampled in
  the same append_series() call.  So on the chart they appear at the SAME
  tick — the lag is sub-tick and not visible.

Why a tape square and a passive fill marker may not overlap exactly
───────────────────────────────────────────────────────────────────
  • The tape records the MARKET transaction price (where the aggressor traded).
  • The passive fill is recorded at OUR QUOTE price (bid or ask).
  • In a real market these differ by sub-tick amounts; in simulation the
    fill price == our quote price which may differ slightly from the tape price.
  This is EXPECTED and not a bug.
"""
import logging

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSizePolicy,
)
from PySide6.QtCore import Qt

import pyqtgraph as pg

from telemetry.state import BotState

log = logging.getLogger(__name__)

# ── palette (mirrors dashboard.py) ───────────────────────────────────────────
_BG     = "#ffffff"
_PANEL  = "#f5f6fa"
_BORDER = "#c8c8d8"
_DIM    = "#8888aa"
_FG     = "#1a1a2e"
_GREEN  = "#00e676"
_RED    = "#ff4444"
_BLUE   = "#4d9fff"
_YELLOW = "#ffd740"

# Determine the downward-triangle symbol once at import time.
# pyqtgraph ≥ 0.13 defines 't1' as triangle-down; older builds fall back to 'd'.
_SELL_SYM = "t1"

# ── chart styling constants ────────────────────────────────────────────────────
# Single source of truth for all marker sizes and line widths.
# Change these two values to rescale the entire chart uniformly.
_MARKER_SIZE    = 20    # pixels – applied to ALL scatter types (squares, triangles, stars)
_MARKER_PEN_W   = 2.0   # pen width for all scatter marker outlines
_LINE_WIDTH     = 3.5   # primary lines: mid, our bid, our ask
_BBO_LINE_WIDTH = 2.0   # secondary lines: market BBO (dashed – intentionally thinner)


# ── legend helpers ────────────────────────────────────────────────────────────

def _legend_item(color: str, glyph: str, text: str) -> QLabel:
    lbl = QLabel(f" {glyph} {text}")
    lbl.setStyleSheet(
        f"color:{color}; font-size:9px; font-family:Consolas,monospace;"
        " background:transparent; padding:0px 6px;"
    )
    return lbl


def _separator() -> QLabel:
    sep = QLabel("  │")
    sep.setStyleSheet(
        f"color:{_BORDER}; font-size:12px; background:transparent; padding:0 2px;"
    )
    return sep


# ── MicrostructureTab ─────────────────────────────────────────────────────────

class MicrostructureTab(QWidget):
    """
    Full-tab market microstructure chart.

    Layout
    ──────
    ┌──────────────────────────────────────────────────────────────────┐
    │               Quote-evolution & fill-event chart                 │
    │   (mid / bid / ask lines  +  spread shading  +  fill markers)   │
    └──────────────────────────────────────────────────────────────────┘
    │  legend bar                                                      │
    └──────────────────────────────────────────────────────────────────┘
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(2)

        # ── chart ─────────────────────────────────────────────────────────────
        self._pw = pg.PlotWidget(background=_BG)
        pi = self._pw.getPlotItem()
        pi.setTitle("")          # no title – reclaim vertical space
        pi.showGrid(x=True, y=True, alpha=0.18)
        for axis in ("left", "bottom"):
            pi.getAxis(axis).setPen(_BORDER)
            pi.getAxis(axis).setTextPen(_DIM)
        pi.getAxis("bottom").setLabel("heartbeat tick", color=_DIM)
        pi.setMenuEnabled(False)
        # Remove pyqtgraph's internal ViewBox padding so setYRange is exact
        pi.getViewBox().setDefaultPadding(0.0)
        # Tighten the internal plot-item layout so axes hug the widget edges
        pi.layout.setContentsMargins(0, 0, 0, 0)
        self._pw.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self._pw, stretch=1)

        # ── layer 1: spread shading (fill between our bid and ask) ───────────
        # FillBetweenItem monitors sigPlotChanged on both curves and redraws
        # automatically whenever setData() is called on either.
        self._bid_curve = pg.PlotCurveItem(antialias=False)
        self._ask_curve = pg.PlotCurveItem(antialias=False)
        self._bid_curve.setPen(pg.mkPen(_BLUE, width=_LINE_WIDTH))
        self._ask_curve.setPen(pg.mkPen(_RED,  width=_LINE_WIDTH))

        _spread_brush = pg.mkBrush(77, 159, 255, 35)   # _BLUE, alpha ≈ 14%
        self._spread_fill = pg.FillBetweenItem(
            self._bid_curve, self._ask_curve, brush=_spread_brush,
        )
        self._pw.addItem(self._spread_fill)

        # ── layer 2: market BBO lines (thin dashed, rendered below our quotes) ─
        # Same hue as our bid/ask so the color identity (blue=bid, red=ask) is
        # preserved; thinner + dashed makes them visually secondary.
        self._mkt_bid_curve = pg.PlotCurveItem(antialias=False)
        self._mkt_ask_curve = pg.PlotCurveItem(antialias=False)
        self._mkt_bid_curve.setPen(
            pg.mkPen(_BLUE, width=_BBO_LINE_WIDTH, style=Qt.PenStyle.DashLine)
        )
        self._mkt_ask_curve.setPen(
            pg.mkPen(_RED,  width=_BBO_LINE_WIDTH, style=Qt.PenStyle.DashLine)
        )
        self._pw.addItem(self._mkt_bid_curve)
        self._pw.addItem(self._mkt_ask_curve)

        # ── layer 3: our quote lines (thick solid, rendered on top of BBO) ────
        self._pw.addItem(self._bid_curve)
        self._pw.addItem(self._ask_curve)

        # ── layer 4: mid price line ───────────────────────────────────────────
        self._mid_curve = pg.PlotCurveItem(antialias=False)
        self._mid_curve.setPen(pg.mkPen(_FG, width=_LINE_WIDTH))
        self._pw.addItem(self._mid_curve)

        # ── layer 4b: tape→fill connector lines (debug aid) ───────────────────
        # A faint dotted vertical segment is drawn between a passive fill price
        # and the tape trade price that triggered it, but ONLY when they differ
        # (i.e. the taker swept beyond our quote level).  When the two prices are
        # equal the markers already overlap, so no connector is needed.
        # Drawn as a single PlotCurveItem with NaN separators (one draw call,
        # zero per-pair overhead).
        self._connector_curve = pg.PlotCurveItem(antialias=False)
        self._connector_curve.setPen(
            pg.mkPen(_YELLOW, width=1.0, style=Qt.PenStyle.DotLine)
        )
        self._pw.addItem(self._connector_curve)

        # ── layer 5: public tape trade squares (background market activity) ─────
        # Many per tick → kept small and semi-transparent so they don't drown lines.
        self._tape_buy_scatter = pg.ScatterPlotItem(
            symbol="s",     # square
            size=_MARKER_SIZE,
            pen=pg.mkPen(_GREEN, width=_MARKER_PEN_W),
            brush=pg.mkBrush(0, 230, 118, 110),
        )
        self._pw.addItem(self._tape_buy_scatter)

        self._tape_sell_scatter = pg.ScatterPlotItem(
            symbol="s",     # square
            size=_MARKER_SIZE,
            pen=pg.mkPen(_RED, width=_MARKER_PEN_W),
            brush=pg.mkBrush(255, 68, 68, 110),
        )
        self._pw.addItem(self._tape_sell_scatter)

        # ── layer 6: passive fill triangles (bot's own maker fills) ──────────
        # BUY fills  — bot bought passively (filled at our bid)
        self._buy_scatter = pg.ScatterPlotItem(
            symbol="t1",    # triangle-up in pyqtgraph ≥ 0.13
            size=_MARKER_SIZE,
            pen=pg.mkPen(_GREEN, width=_MARKER_PEN_W),
            brush=pg.mkBrush(0, 230, 118, 220),
        )
        self._pw.addItem(self._buy_scatter)

        # SELL fills — bot sold passively (filled at our ask)
        self._sell_scatter = pg.ScatterPlotItem(
            symbol=_SELL_SYM,   # triangle-down (or diamond fallback)
            size=_MARKER_SIZE,
            pen=pg.mkPen(_RED, width=_MARKER_PEN_W),
            brush=pg.mkBrush(255, 68, 68, 220),
        )
        self._pw.addItem(self._sell_scatter)

        # ── layer 7: aggressive fill stars (bot's own hedge / market orders) ──
        # Rendered last → on top of everything else; largest and most prominent.
        self._agg_buy_scatter = pg.ScatterPlotItem(
            symbol="star",
            size=_MARKER_SIZE,
            pen=pg.mkPen(_GREEN, width=_MARKER_PEN_W),
            brush=pg.mkBrush(0, 230, 118, 240),
        )
        self._pw.addItem(self._agg_buy_scatter)

        self._agg_sell_scatter = pg.ScatterPlotItem(
            symbol="star",
            size=_MARKER_SIZE,
            pen=pg.mkPen(_RED, width=_MARKER_PEN_W),
            brush=pg.mkBrush(255, 68, 68, 240),
        )
        self._pw.addItem(self._agg_sell_scatter)

        # ── compact legend bar ────────────────────────────────────────────────
        legend_frame = QFrame()
        legend_frame.setStyleSheet(
            f"QFrame{{background:{_PANEL};border-top:1px solid {_BORDER};"
            "border-radius:0px;}}"
        )
        legend_frame.setFixedHeight(20)
        ll = QHBoxLayout(legend_frame)
        ll.setContentsMargins(8, 0, 8, 0)
        ll.setSpacing(0)

        ll.addWidget(_legend_item(_FG,    "─",  "Mid (exch)"))
        ll.addWidget(_legend_item(_BLUE,  "─",  "Our Bid"))
        ll.addWidget(_legend_item(_RED,   "─",  "Our Ask"))
        # Dashed lines = real exchange top-of-book (external, does NOT include our
        # simulated quotes).  We quote INSIDE the exchange spread, so the dashed
        # lines normally appear FARTHER from mid than our solid lines.
        ll.addWidget(_legend_item(_BLUE,  "╌",  "Exch Bid"))
        ll.addWidget(_legend_item(_RED,   "╌",  "Exch Ask"))
        ll.addWidget(_separator())
        ll.addWidget(_legend_item(_GREEN, "▲",  "Buy"))
        ll.addWidget(_legend_item(_RED,   "▽",  "Sell"))
        ll.addWidget(_separator())
        ll.addWidget(_legend_item(_GREEN, "■",  "Tape Buy"))
        ll.addWidget(_legend_item(_RED,   "■",  "Tape Sell"))
        ll.addWidget(_separator())
        ll.addWidget(_legend_item(_GREEN, "★",  "Hedge Buy"))
        ll.addWidget(_legend_item(_RED,   "★",  "Hedge Sell"))
        ll.addStretch()

        root.addWidget(legend_frame)

    # ──────────────────────────────────────────────────────────────────────────
    # Refresh  (called from QTimer, main thread only)
    # ──────────────────────────────────────────────────────────────────────────

    def refresh(self, snap: BotState) -> None:
        n = len(snap.price_series)
        if n == 0:
            self._mid_curve.setData([], [])
            self._bid_curve.setData([], [])
            self._ask_curve.setData([], [])
            self._mkt_bid_curve.setData([], [])
            self._mkt_ask_curve.setData([], [])
            self._connector_curve.setData([], [])
            self._tape_buy_scatter.setData([], [])
            self._tape_sell_scatter.setData([], [])
            self._buy_scatter.setData([], [])
            self._sell_scatter.setData([], [])
            self._agg_buy_scatter.setData([], [])
            self._agg_sell_scatter.setData([], [])
            return

        # ── X axis: ABSOLUTE TICK (single coordinate system for everything) ───
        #
        # tick_series[i] is the heartbeat tick that produced price_series[i].
        # All series lines are plotted against these tick values.
        # All event markers use event.tick directly — no formula, no rebasing.
        #
        # Fallback: if tick_series is missing or mismatched (e.g. old store
        # without the tick_buf), generate a synthetic tick series so the chart
        # still renders.  Markers will be approximate in that case.
        ts = snap.tick_series
        if len(ts) == n:
            xs = ts
        else:
            # Reconstruct best-effort ticks using snap.ticks as the right edge.
            # This path only triggers for snapshots produced before the fix.
            newest = snap.ticks if snap.ticks >= n else n
            xs = list(range(newest - n + 1, newest + 1))

        t_min = xs[0]   # oldest tick in the visible window
        t_max = xs[-1]  # newest tick in the visible window

        # ── Mid line ──────────────────────────────────────────────────────────
        self._mid_curve.setData(xs, snap.price_series, connect="finite")

        # ── Bid / ask quote lines ─────────────────────────────────────────────
        # Replace zeros with NaN so they don't contaminate the Y scale or draw
        # a line down to 0 before the bot starts quoting.
        def _mask(series: list) -> list:
            return [v if v > 0 else float("nan") for v in series]

        bid_s = snap.bid_series
        ask_s = snap.ask_series
        if len(bid_s) == n and len(ask_s) == n:
            self._bid_curve.setData(xs, _mask(bid_s), connect="finite")
            self._ask_curve.setData(xs, _mask(ask_s), connect="finite")
        else:
            self._bid_curve.setData([], [])
            self._ask_curve.setData([], [])

        mkt_bid_s = snap.mkt_bid_series
        mkt_ask_s = snap.mkt_ask_series
        if len(mkt_bid_s) == n and len(mkt_ask_s) == n:
            self._mkt_bid_curve.setData(xs, _mask(mkt_bid_s), connect="finite")
            self._mkt_ask_curve.setData(xs, _mask(mkt_ask_s), connect="finite")
        else:
            self._mkt_bid_curve.setData([], [])
            self._mkt_ask_curve.setData([], [])

        # ── Fill markers ──────────────────────────────────────────────────────
        # Each event carries event.tick which is the absolute heartbeat tick
        # at which it was recorded.  We plot directly at that tick — no index
        # arithmetic, no risk of drift.
        # Events outside [t_min, t_max] are simply out of the visible window
        # and skipped so we don't plot garbage.

        buy_xs:  list[float] = []
        buy_ys:  list[float] = []
        sell_xs: list[float] = []
        sell_ys: list[float] = []
        agg_buy_xs:  list[float] = []
        agg_buy_ys:  list[float] = []
        agg_sell_xs: list[float] = []
        agg_sell_ys: list[float] = []

        # Connector segments: one NaN-terminated vertical line per fill where
        # trigger_tape_price != fill price (taker swept beyond our quote).
        # Format: [x, x, nan, x, x, nan, ...] / [fill_px, tape_px, nan, ...]
        conn_xs: list[float] = []
        conn_ys: list[float] = []
        _nan = float("nan")

        for fe in snap.fill_events:
            if fe.market and snap.selected_market and fe.market != snap.selected_market:
                log.warning(
                    "Microstructure: skipping FillEvent market=%s (selected=%s) "
                    "— market-mixing leak detected",
                    fe.market, snap.selected_market,
                )
                continue
            if not (t_min <= fe.tick <= t_max and fe.price > 0):
                continue
            is_agg = fe.side.startswith("HEDGE")
            is_buy = "BUY" in fe.side
            if is_agg:
                if is_buy:
                    agg_buy_xs.append(float(fe.tick)); agg_buy_ys.append(fe.price)
                else:
                    agg_sell_xs.append(float(fe.tick)); agg_sell_ys.append(fe.price)
            else:
                if is_buy:
                    buy_xs.append(float(fe.tick)); buy_ys.append(fe.price)
                else:
                    sell_xs.append(float(fe.tick)); sell_ys.append(fe.price)
                # Connector: only draw when tape price differs from fill price
                # (a non-zero trigger_tape_price that doesn't equal the fill price).
                tp = fe.trigger_tape_price
                if tp and abs(tp - fe.price) > 1e-8:
                    conn_xs += [float(fe.tick), float(fe.tick), _nan]
                    conn_ys += [fe.price, tp, _nan]

        self._buy_scatter.setData(x=buy_xs, y=buy_ys)
        self._sell_scatter.setData(x=sell_xs, y=sell_ys)
        self._agg_buy_scatter.setData(x=agg_buy_xs, y=agg_buy_ys)
        self._agg_sell_scatter.setData(x=agg_sell_xs, y=agg_sell_ys)
        self._connector_curve.setData(conn_xs, conn_ys)

        # ── Tape trade squares ────────────────────────────────────────────────
        tape_buy_xs:  list[float] = []
        tape_buy_ys:  list[float] = []
        tape_sell_xs: list[float] = []
        tape_sell_ys: list[float] = []

        for te in snap.tape_trade_events:
            if te.market and snap.selected_market and te.market != snap.selected_market:
                log.warning(
                    "Microstructure: skipping TapeTradeEvent market=%s (selected=%s) "
                    "— market-mixing leak detected",
                    te.market, snap.selected_market,
                )
                continue
            if not (t_min <= te.tick <= t_max and te.price > 0):
                continue
            if te.side == "BUY":
                tape_buy_xs.append(float(te.tick)); tape_buy_ys.append(te.price)
            else:
                tape_sell_xs.append(float(te.tick)); tape_sell_ys.append(te.price)

        self._tape_buy_scatter.setData(x=tape_buy_xs, y=tape_buy_ys)
        self._tape_sell_scatter.setData(x=tape_sell_xs, y=tape_sell_ys)

        # ── Y-axis autoscale ──────────────────────────────────────────────────
        visible: list[float] = [p for p in snap.price_series if p > 0]
        if len(bid_s) == n:
            visible += [p for p in bid_s if p > 0]
            visible += [p for p in ask_s if p > 0]
        if len(mkt_bid_s) == n:
            visible += [p for p in mkt_bid_s if p > 0]
            visible += [p for p in mkt_ask_s if p > 0]
        visible += buy_ys + sell_ys + agg_buy_ys + agg_sell_ys + tape_buy_ys + tape_sell_ys

        if visible:
            y_min = min(visible)
            y_max = max(visible)
            pad = max((y_max - y_min) * 0.015, 0.01)
            self._pw.getPlotItem().setYRange(y_min - pad, y_max + pad, padding=0)
