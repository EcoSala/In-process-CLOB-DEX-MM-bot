"""
Immutable snapshot dataclasses passed from trading thread → UI thread.
All fields are plain scalars or plain lists (no deques, no locks).
"""
from dataclasses import dataclass, field
from typing import List


@dataclass
class FillEvent:
    """
    Slim fill record for the microstructure chart (bot's own fills only).
    Created from ExecutionTape.Fill by the trading thread; consumed read-only by UI.

    side values:
      "BUY"        — passive fill: bot bought at our bid
      "SELL"       — passive fill: bot sold at our ask
      "HEDGE_BUY"  — aggressive hedge: bot sent a market buy
      "HEDGE_SELL" — aggressive hedge: bot sent a market sell

    trigger_tape_price:
      For passive fills only — the raw exchange trade price that triggered this fill.
      None for hedge fills (no triggering tape event).
      When trigger_tape_price == price  → the taker hit exactly our quote level.
      When trigger_tape_price  > price  → the taker swept beyond our quote (deeper).
      The microstructure chart draws a faint connector line between the two
      values only when they differ, making sweep-through fills visually distinct.
    """
    tick:   int
    side:   str
    price:  float
    size:   float
    edge:   float
    trigger_tape_price: float = 0.0  # 0.0 means not set (hedge fill or unknown)
    market: str = ""  # originating market (e.g. "SOL-USD"); empty = legacy/unknown


@dataclass
class TapeTradeEvent:
    """
    A single public market trade pulled from TradeTape for the microstructure chart.
    Represents the taker/aggressor side of an exchange trade.
    NOT a fill by this bot — just observed market activity.
    """
    tick:   int
    side:   str    # "BUY" or "SELL" (aggressor/taker side)
    price:  float
    size:   float
    market: str = ""  # originating market (e.g. "SOL-USD"); empty = legacy/unknown


@dataclass
class OrderRow:
    """One active paper order, frozen for UI consumption."""
    order_id: str
    market: str
    side: str           # "BID" or "ASK"
    price: float
    orig_qty: float
    filled_qty: float
    remaining_qty: float
    fill_pct: float     # 0.0 – 1.0
    age_sec: float


@dataclass
class BotState:
    """
    Full bot state snapshot.  Created by BotStateStore.get_snapshot() under lock,
    then handed to the UI thread which reads it freely (no lock needed).
    """
    # ── market ────────────────────────────────────────────────────────────────
    selected_market: str = ""
    mid_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    spread_bps: float = 0.0
    tpm: float = 0.0

    # ── position ──────────────────────────────────────────────────────────────
    inventory: float = 0.0
    avg_price: float = 0.0
    cash_usd: float = 0.0

    # ── PnL ───────────────────────────────────────────────────────────────────
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_pnl: float = 0.0

    # ── cumulative stats ──────────────────────────────────────────────────────
    total_trades: int = 0
    total_volume: float = 0.0
    total_notional: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    total_edge_collected: float = 0.0   # Σ (edge_per_unit × size) across all fills
    norm_inventory: float = 0.0         # inventory / max_inventory, clamped to [-1, 1]
    ofi_signal: float = 0.0             # latest OFI value for the active market [-1, +1]
    hedge_count: int = 0                # cumulative aggressive hedge fills since start
    ticks: int = 0

    # ── model state (Signals tab) ─────────────────────────────────────────────
    reservation_price: float = 0.0     # fair-value mid after all skews, before spread
    inv_skew_bps: float = 0.0          # inventory contribution to reservation skew (bps)
    ofi_skew_bps: float = 0.0          # OFI contribution to reservation skew (bps)

    # ── orders ────────────────────────────────────────────────────────────────
    active_orders: List[OrderRow] = field(default_factory=list)

    # ── rolling series (plain lists, safe to hand to UI) ──────────────────────
    # tick_series[i] is the heartbeat tick that produced price_series[i].
    # This is the single source of truth for X-axis alignment: all series lines
    # are plotted against tick_series, and all event markers use event.tick
    # directly.  Both are in the same coordinate system so there is no drift.
    tick_series: List[int] = field(default_factory=list)
    price_series: List[float] = field(default_factory=list)
    bid_series: List[float] = field(default_factory=list)      # our quoted bid per tick
    ask_series: List[float] = field(default_factory=list)      # our quoted ask per tick
    mkt_bid_series: List[float] = field(default_factory=list)  # market best bid (TOB) per tick
    mkt_ask_series: List[float] = field(default_factory=list)  # market best ask (TOB) per tick
    inventory_series: List[float] = field(default_factory=list)
    pnl_series: List[float] = field(default_factory=list)
    returns_series: List[float] = field(default_factory=list)
    ofi_series: List[float] = field(default_factory=list)      # OFI signal per tick [-1, +1]
    reservation_series: List[float] = field(default_factory=list)  # reservation_price per tick

    # ── fill events (microstructure chart markers) ────────────────────────────
    fill_events: List[FillEvent] = field(default_factory=list)
    tape_trade_events: List[TapeTradeEvent] = field(default_factory=list)

    # ── meta ──────────────────────────────────────────────────────────────────
    is_running: bool = False
    last_update: float = 0.0
    uptime_sec: float = 0.0
