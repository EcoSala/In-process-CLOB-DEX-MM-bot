import asyncio
import logging
from dataclasses import dataclass

from .config import Config
from .logger import setup_fill_logger, spawn_fill_monitor_window
from src.venues.extended_multi import ExtendedMulti
from src.selection.market_selector import MarketSnapshot, SelectorConfig, select_markets
from src.sim.paper_mm import PaperMM, TradeStats, ExecutionTape

log = logging.getLogger("mm")


@dataclass
class AppState:
    running: bool = True
    ticks: int = 0


class BotApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = AppState()
        self.trade_stats = TradeStats()
        self._stats_log_every = max(1, int(cfg.app.stats_log_every))
        
        # Setup dedicated fill logger (writes to fills.log)
        print_fills = getattr(cfg.sim, 'print_fills', True)
        self.fill_log_file = "fills.log"
        fill_logger = setup_fill_logger(enabled=print_fills, log_file=self.fill_log_file)
        
        # Execution tape for live fill logging
        tape_history = getattr(cfg.sim, 'execution_tape_history', 200)
        self.execution_tape = ExecutionTape(max_history=tape_history, fill_logger=fill_logger)
        
        # Track if we should spawn fill monitor window
        self._spawn_fill_window = print_fills and getattr(cfg.sim, 'fill_monitor_window', True)

        # Multi-market public book + trades feeds
        # Expects: cfg.extended_ws, cfg.extended_trades_ws, cfg.extended.markets
        self.ext_multi = ExtendedMulti(
            cfg.extended_ws,
            cfg.extended_ws,  # <-- FIX: was cfg.extended_ws twice
            cfg.extended.markets,
        )

        # Market selection thresholds
        self.selector_cfg = SelectorConfig(
            min_spread_bps=cfg.extended.selector.min_spread_bps,
            min_tpm=cfg.extended.selector.min_tpm,
            top_n=cfg.extended.selector.top_n,
        )

        # Paper market-making simulator
        self.paper = PaperMM(
            quote_half_spread_bps=cfg.sim.quote_half_spread_bps,
            quote_size_usd=cfg.sim.quote_size_usd,
            max_inventory_usd=cfg.sim.max_inventory_usd,
            trade_stats=self.trade_stats,
            execution_tape=self.execution_tape,
        )

        # One-time warning flags
        self._warned_no_recent = False

    @staticmethod
    def _mid_from_tob(tob):
        bid = getattr(tob, "bid_px", None)
        ask = getattr(tob, "ask_px", None)
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    async def heartbeat_loop(self):
        tick = float(self.cfg.app.tick_seconds)

        while self.state.running:
            self.state.ticks += 1
            snaps = []

            # 1) Build snapshots for ALL markets
            for m, f in self.ext_multi.feeds.items():
                tob = f.public_ws.tob
                bid, ask = tob.bid_px, tob.ask_px
                spread = tob.spread_bps()

                tape = f.trades_ws.tape
                # Assumes your tape supports these (as in Day2):
                tpm = tape.trades_per_min(60)
                br = tape.buy_ratio(60)

                snaps.append(
                    MarketSnapshot(
                        market=m,
                        bid=bid,
                        ask=ask,
                        spread_bps=spread,
                        tpm=tpm,
                        buy_ratio=br,
                    )
                )

            # 2) Pick markets
            picked = select_markets(snaps, self.selector_cfg)

            # 3) Log selection
            if picked:
                def _fmt(p):
                    spr = "None" if p.spread_bps is None else f"{p.spread_bps:.2f}"
                    tpm_s = "None" if p.tpm is None else f"{p.tpm:.1f}"
                    return f"{p.market}(spr={spr}bps,tpm={tpm_s})"

                top = ", ".join(_fmt(p) for p in picked)
                log.info(f"tick={self.state.ticks} SELECTED ({len(picked)} of top_n={self.selector_cfg.top_n}): {top}")
            else:
                log.info(f"tick={self.state.ticks} SELECTED: (none)")

            # 4) Paper MM (quotes + paper fills) on selected markets
            for p in picked:
                f = self.ext_multi.feeds[p.market]
                tob = f.public_ws.tob

                mid = self._mid_from_tob(tob)
                if mid is None:
                    # No book yet for this market -> just skip it (do NOT sleep here)
                    continue

                q = self.paper.make_quote(mid)
                
                # Set context for execution tape logging
                self.paper.current_tick = self.state.ticks
                self.paper.current_market = p.market

                tape = f.trades_ws.tape
                if hasattr(tape, "recent"):
                    # Use 2x tick window to account for timing and ensure we catch recent trades
                    recent_trades = list(tape.recent(tick * 2))
                    for tr in recent_trades:
                        # TradeTape.recent() returns tuples: (ts_ms, price, qty, side)
                        if isinstance(tr, tuple) and len(tr) == 4:
                            _, trade_px, trade_qty, side = tr
                        else:
                            # fallback for alternate trade representations
                            trade_px = getattr(tr, "price", None)
                            trade_qty = getattr(tr, "qty", None)
                            side = getattr(tr, "side", None)
                        if trade_px is None or trade_qty is None or side is None:
                            continue
                        self.paper.on_trade(
                            mid=mid,
                            trade_px=float(trade_px),
                            trade_qty=float(trade_qty),
                            side=str(side),
                            q=q,
                        )
                else:
                    if not self._warned_no_recent:
                        log.warning("tape.recent(seconds) not implemented yet -> skipping paper fills.")
                        self._warned_no_recent = True

            # 5) Calculate PnL correctly by marking all positions to their respective markets
            mid_prices = {}
            for market in self.paper.positions.keys():
                f = self.ext_multi.feeds.get(market)
                if f:
                    tob = f.public_ws.tob
                    mid = self._mid_from_tob(tob)
                    if mid is not None:
                        mid_prices[market] = mid
            
            pnl = self.paper.mark_to_market(mid_prices)
            
            # Show active markets (rate-limited, every half stats interval)
            active_markets = {p.market for p in picked}
            if self.state.ticks % max(1, self._stats_log_every // 2) == 0:
                active_str = ",".join(sorted(active_markets)) if active_markets else "none"
                log.info(f"tick={self.state.ticks} ACTIVE_MARKETS: [{active_str}]")

            # Show position for currently selected market (if any)
            current_market = picked[0].market if picked else None
            market_pos = self.paper.positions.get(current_market, {}).get("pos", 0.0) if current_market else 0.0
            
            log.info(
                f"tick={self.state.ticks} PAPER: "
                f"market={current_market or 'none'} pos={market_pos:.6f} "
                f"cash={self.paper.state.cash_usd:.2f} "
                f"pnl≈{pnl:.2f}"
            )

            if self.state.ticks % self._stats_log_every == 0:
                # Show stats across all markets
                total_trades = self.trade_stats.num_trades
                log.info(
                    f"tick={self.state.ticks} STATS: "
                    f"trades={total_trades} "
                    f"volume={self.trade_stats.total_volume:.6f} "
                    f"notional={self.trade_stats.total_notional:.2f} "
                    f"buys={self.trade_stats.buy_volume:.6f} "
                    f"sells={self.trade_stats.sell_volume:.6f} "
                    f"markets={len(self.paper.positions)}"
                )
                
                # PNL_BREAKDOWN: show per-market positions and equity
                if self.paper.positions:
                    breakdown_lines = []
                    for market in sorted(self.paper.positions.keys()):
                        state = self.paper.positions[market]
                        pos = state["pos"]
                        avg_px = state["avg_price"]
                        rpnl = state["realized_pnl"]
                        mid_val = mid_prices.get(market, 0.0)
                        inv_usd = pos * mid_val if mid_val else 0.0
                        breakdown_lines.append(
                            f"{market}: pos={pos:.4f} avg={avg_px:.2f} mid={mid_val:.2f} "
                            f"inv=${inv_usd:.2f} rPnL={rpnl:.2f}"
                        )
                    log.info(
                        f"tick={self.state.ticks} PNL_BREAKDOWN: "
                        f"cash=${self.paper.state.cash_usd:.2f} equity=${pnl:.2f} | " +
                        " | ".join(breakdown_lines)
                    )

            await asyncio.sleep(tick)

    async def run(self):
        log.info("Starting app...")
        
        # Spawn separate PowerShell window for fill monitoring
        if self._spawn_fill_window:
            spawn_fill_monitor_window(self.fill_log_file)

        if self.cfg.venues.extended.enabled:
            self.ext_multi.start()

        try:
            await asyncio.gather(self.heartbeat_loop())
        except asyncio.CancelledError:
            pass
        finally:
            if self.cfg.venues.extended.enabled:
                await self.ext_multi.stop()

            log.info("Shutting down cleanly.")

    def stop(self):
        self.state.running = False
