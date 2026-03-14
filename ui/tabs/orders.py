"""
Orders tab: live QTableView of active paper orders.

Columns: ID | Market | Side | Price | Orig Qty | Filled | Remaining | Fill % | Age
Row highlighting:
  - BID rows  → dark blue tint
  - ASK rows  → dark red tint
  - Partial   → dark amber tint (overlaid)
  - Side text → green for BID, red for ASK
"""
from typing import Any, List

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTableView, QAbstractItemView, QHeaderView,
)
from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex
from PySide6.QtGui import QColor, QBrush, QFont

from telemetry.state import BotState, OrderRow

# ── column config ─────────────────────────────────────────────────────────────
_COLS = [
    ("ID",        70,  Qt.AlignmentFlag.AlignLeft),
    ("Market",    90,  Qt.AlignmentFlag.AlignLeft),
    ("Side",      55,  Qt.AlignmentFlag.AlignCenter),
    ("Price",    100,  Qt.AlignmentFlag.AlignRight),
    ("Orig Qty",  90,  Qt.AlignmentFlag.AlignRight),
    ("Filled",    90,  Qt.AlignmentFlag.AlignRight),
    ("Remaining", 90,  Qt.AlignmentFlag.AlignRight),
    ("Fill %",    65,  Qt.AlignmentFlag.AlignRight),
    ("Age (s)",   65,  Qt.AlignmentFlag.AlignRight),
]

# ── palette ───────────────────────────────────────────────────────────────────
_BG_BID     = QColor("#0a1520")
_BG_ASK     = QColor("#1a0808")
_BG_PARTIAL = QColor("#1a1400")
_CLR_GREEN  = QColor("#00e676")
_CLR_RED    = QColor("#ff4444")
_CLR_DIM    = QColor("#5858a0")
_CLR_FG     = QColor("#d8d8ee")


# ── table model ───────────────────────────────────────────────────────────────

class _OrdersModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: List[OrderRow] = []

    def set_rows(self, rows: List[OrderRow]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_COLS)

    def headerData(self, section: int, orientation: Qt.Orientation, role=Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return _COLS[section][0]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._rows):
            return None

        row: OrderRow = self._rows[index.row()]
        col: int = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return self._cell_text(row, col)

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 2:                        # Side column: green / red
                return QBrush(_CLR_GREEN if row.side == "BID" else _CLR_RED)
            return QBrush(_CLR_FG)

        if role == Qt.ItemDataRole.BackgroundRole:
            is_partial = 0 < row.fill_pct < 0.995
            if is_partial:
                return QBrush(_BG_PARTIAL)
            return QBrush(_BG_BID if row.side == "BID" else _BG_ASK)

        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(_COLS[col][2] | Qt.AlignmentFlag.AlignVCenter)

        return None

    @staticmethod
    def _cell_text(r: OrderRow, col: int) -> str:
        match col:
            case 0: return r.order_id
            case 1: return r.market
            case 2: return r.side
            case 3: return f"{r.price:,.2f}"
            case 4: return f"{r.orig_qty:.6f}"
            case 5: return f"{r.filled_qty:.6f}"
            case 6: return f"{r.remaining_qty:.6f}"
            case 7: return f"{r.fill_pct * 100:.1f}%"
            case 8: return f"{r.age_sec:.1f}"
            case _: return ""


# ── OrdersTab ─────────────────────────────────────────────────────────────────

class OrdersTab(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(5)

        # Summary bar
        self._summary = QLabel("Active orders: 0")
        self._summary.setStyleSheet("color:#5858a0; font-size:10px; padding:2px 4px;")
        layout.addWidget(self._summary)

        # Table
        self._model = _OrdersModel()
        self._view = QTableView()
        self._view.setModel(self._model)
        self._view.setAlternatingRowColors(False)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._view.verticalHeader().setVisible(False)
        self._view.setShowGrid(True)
        self._view.setWordWrap(False)

        hdr = self._view.horizontalHeader()
        hdr.setStretchLastSection(True)
        for i, (_, w, _) in enumerate(_COLS):
            if w > 0:
                self._view.setColumnWidth(i, w)
        self._view.verticalHeader().setDefaultSectionSize(22)

        layout.addWidget(self._view)

        # Legend
        legend = QLabel(
            "  ▮  BID row    ▮  ASK row    ▮  Partial fill"
        )
        legend.setStyleSheet("color:#5858a0; font-size:9px; padding:2px 4px;")
        layout.addWidget(legend)

    def refresh(self, snap: BotState) -> None:
        self._model.set_rows(snap.active_orders)
        n = len(snap.active_orders)
        inv_sign = "+" if snap.inventory > 0 else ""
        self._summary.setText(
            f"Active orders: {n}   │  market: {snap.selected_market or '—'}  "
            f"│  inventory: {inv_sign}{snap.inventory:.6f}  "
            f"│  trades: {snap.total_trades}"
        )
