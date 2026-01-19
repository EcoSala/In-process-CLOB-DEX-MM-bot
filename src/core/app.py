import asyncio
import logging
from dataclasses import dataclass

from .config import Config
from src.venues.extended_multi import ExtendedMulti
from src.selection.market_selector import MarketSnapshot, SelectorConfig, select_markets
from src.sim.paper_mm import PaperMM

log = logging.getLogger("mm")


@dataclass
class AppState:
    running: bool = True
    ticks: int = 0


class BotApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = AppState()

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
                log.info(f"tick={self.state.ticks} SELECTED: {top}")
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

                tape = f.trades_ws.tape
                if hasattr(tape, "recent"):
                    for tr in tape.recent(tick):
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

            # 5) Mark-to-mid PnL using first picked market (if any)
            pnl = 0.0
            if picked:
                m0 = picked[0].market
                tob0 = self.ext_multi.feeds[m0].public_ws.tob
                mid0 = self._mid_from_tob(tob0)
                if mid0 is not None:
                    pnl = self.paper.mark_to_mid(mid0)

            log.info(
                f"tick={self.state.ticks} PAPER: "
                f"pos={self.paper.state.pos_base:.6f} "
                f"cash={self.paper.state.cash_usd:.2f} "
                f"pnl≈{pnl:.2f}"
            )

            await asyncio.sleep(tick)

    async def run(self):
        log.info("Starting app...")

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
