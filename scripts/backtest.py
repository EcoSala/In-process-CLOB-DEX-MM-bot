"""
Multi-pair market-making backtest using Binance USDT-M perpetual futures as price proxy.

DATA MODEL
----------
We use Binance 1m OHLCV candles with taker_buy_volume as the simulation input.
Each candle provides:
  - open_time, open, high, low, close, volume, taker_buy_volume

This is richer than pure OHLC: `taker_buy_volume` tells us the aggressor-side split,
which drives our directional fill simulation.

FILL MODEL (PER CANDLE)
-----------------------
Each 1m candle generates two synthetic aggressor sweeps:
  BUY  aggressor sweeps price UP   to `high`  with qty = taker_buy_volume
  SELL aggressor sweeps price DOWN to `low`   with qty = (volume - taker_buy_volume)

If the aggressor reaches our quote price (price-priority), we check for a fill using
the volume-per-tick + queue model:

  1. Scale Binance volume to Extended Exchange:
        scaled_vol = agg_vol * volume_scale
        volume_scale = Extended_daily_base_vol / Binance_daily_base_vol  (~0.7–6%)

  2. Distribute scaled volume uniformly across the candle's tick range (volume-per-tick):
        ticks_in_range = candle_range / tick_size
        vol_per_tick   = scaled_vol / ticks_in_range

  3. PRO-RATA QUEUE MODEL (mathematically principled):
        In a pro-rata market, each resting order at a level receives a proportional
        share of the incoming volume.  With N_competitors also quoting at our level:

        fill_qty = min(our_quote_size, vol_per_tick / (1 + N_competitors))

        N_competitors is estimated from Extended Exchange USD volume tier:
          >$10M/day -> 8 competitors  (large liquid market, institutional presence)
          >$1M/day  -> 5 competitors  (mid-size market)
          >$100k/day -> 3 competitors (small market, few active MMs)
          <$100k/day -> 2 competitors (micro market)

  4. Apply hour-of-day volume weight (intraday profile):
        volume_weight = hourly_profile[candle_hour]  (1.0 = average hour)
        vol_per_tick  *= volume_weight

VOLUME PROFILE
--------------
Built from the 1m Binance historical data (already downloaded) by averaging volume
per hour-of-day across the backtest period.  This captures the well-known intraday
seasonality (US/EU session peaks, Asian night trough).

CAPITAL ALLOCATION (MULTI-PAIR)
--------------------------------
Each market gets a fixed max inventory (default $10,000) — this is the risk cap.
Quote size follows Extended liquidity, not max_inv:

  1m_notional = daily_usd_vol / 1440
  quote_usd   = clip(0.02 * 1m_notional, 50, 1000)

Floor $50 / cap $1000 so BTC does not dominate and thin alts still quote.

SPREAD
------
  half_spread_bps_i = max(8.0, extended_current_spread_bps_i)
  rationale: 8 bps is the empirical minimum needed to earn positive edge on
  SOL/USD during a 6-month bull run (Apr-Oct 2026 backtest shows ~break-even at 8 bps).
  For markets with naturally wider spreads, we match the market spread.

USAGE
-----
  # Single market (SOL-USD, 6 months, realistic fills):
  python scripts/backtest.py --realistic

  # Custom spread and inventory:
  python scripts/backtest.py --market BTC-USD --spread-bps 5 --max-inventory-usd 3000

  # All pairs on Extended Exchange with $25k total capital:
  python scripts/backtest.py --all-pairs

  # All pairs, top 30 by volume, custom capital:
  python scripts/backtest.py --all-pairs --top-n 30 --total-capital 25000
"""
import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections import deque
from pathlib import Path
from typing import Optional

import requests
import pandas as pd
import numpy as np

# ── Project root ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.sim.paper_mm import (
    PaperMM, HedgeParams, InventoryControlParams, OFIParams, TradeStats,
)
from src.data.top_of_book import TopOfBook
from src.core.config import load_config

# ── Binance endpoints ─────────────────────────────────────────────────────────
BINANCE_SPOT_BASE    = "https://api.binance.com"
BINANCE_FUTURES_BASE = "https://fapi.binance.com"

# ── Markets to exclude from all-pairs backtest ────────────────────────────────
# PAXG: commodity-backed, near-zero fills (40/day), not appropriate for MM
# TRX: near-zero fills (40/day), not worth infrastructure overhead
EXCLUDE_MARKETS = {"PAXG-USD", "TRX-USD"}

# ── Extended Exchange API ─────────────────────────────────────────────────────
EXTENDED_MARKETS_API = "https://api.starknet.extended.exchange/api/v1/info/markets"
EXTENDED_MARKETS_CACHE = ROOT / "scripts" / "extended_markets.json"

# ─────────────────────────────────────────────────────────────────────────────
# Data fetching
# ─────────────────────────────────────────────────────────────────────────────

def to_binance_symbol(market: str) -> str:
    """Convert Extended market name (e.g. 'SOL-USD') to Binance USDT-M symbol."""
    base = market.split("-")[0]
    return base + "USDT"


def fetch_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    verbose: bool = True,
    cache_dir: str = "",
    use_futures: bool = True,
) -> pd.DataFrame:
    """
    Fetch Binance klines for `symbol` between start_ms and end_ms (milliseconds).

    use_futures=True -> Binance USDT-M perpetual futures (fapi.binance.com) [RECOMMENDED]
    Returns DataFrame: open_time, open, high, low, close, volume, taker_buy_volume
    Results are cached by day boundary so re-runs within the same day are instant.
    Automatically retries on 429 rate-limit errors with exponential backoff.
    """
    import tempfile, os
    if not cache_dir:
        cache_dir = tempfile.gettempdir()

    start_day_ms = (start_ms // 86_400_000) * 86_400_000
    end_day_ms   = ((end_ms  // 86_400_000) + 1) * 86_400_000
    src = "fut" if use_futures else "spot"
    cache_file = os.path.join(
        cache_dir,
        f"mmbot_klines_{symbol}_{interval}_{src}_{start_day_ms}_{end_day_ms}.csv",
    )
    if os.path.exists(cache_file):
        if verbose:
            print(f"  [cache hit] Loading from {cache_file}")
        df = pd.read_csv(cache_file, parse_dates=["open_time"])
        df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
        return df

    base_url = BINANCE_FUTURES_BASE if use_futures else BINANCE_SPOT_BASE
    endpoint = "/fapi/v1/klines" if use_futures else "/api/v3/klines"
    url = base_url + endpoint
    all_rows = []
    current_start = start_ms
    batch = 0

    while current_start < end_ms:
        params = {
            "symbol":    symbol,
            "interval":  interval,
            "startTime": current_start,
            "endTime":   end_ms,
            "limit":     1000,
        }
        # Retry with exponential backoff on 429, timeouts, and connection errors
        last_err = None
        for attempt in range(6):
            try:
                resp = requests.get(url, params=params, timeout=30)
                if resp.status_code == 429:
                    wait = (2 ** attempt) * 2
                    if verbose:
                        print(f"  Rate limited (429) for {symbol}, waiting {wait}s...", flush=True)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                last_err = None
                break
            except (requests.HTTPError, requests.ConnectionError,
                    requests.Timeout, requests.exceptions.ChunkedEncodingError) as e:
                last_err = e
                wait = (2 ** attempt) * 2
                if verbose:
                    print(f"  Retry {attempt+1}/6 {symbol}: {e}  (wait {wait}s)", flush=True)
                time.sleep(wait)
        else:
            raise RuntimeError(f"Max retries exceeded for {symbol}: {last_err}")

        data = resp.json()
        if not data:
            break
        all_rows.extend(data)
        batch += 1
        last_close_ms = data[-1][6]
        current_start = last_close_ms + 1
        if verbose and batch % 5 == 0:
            pct = 100 * (current_start - start_ms) / max(1, end_ms - start_ms)
            print(f"  Fetched {len(all_rows):,} candles so far ({pct:.0f}%)...", end="\r", flush=True)
        time.sleep(0.15)   # respect Binance rate limit: ~400 req/min weight budget
        if len(data) < 1000:
            break

    if verbose:
        print(f"  Fetched {len(all_rows):,} candles total.          ")

    if not all_rows:
        raise RuntimeError(f"No klines returned for {symbol}")

    df = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "num_trades",
        "taker_buy_volume", "taker_buy_quote_volume", "_ignore",
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume", "taker_buy_volume"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("open_time").reset_index(drop=True)
    df = df[["open_time", "open", "high", "low", "close", "volume", "taker_buy_volume"]]
    try:
        df.to_csv(cache_file, index=False)
        if verbose:
            print(f"  [cached] Saved to {cache_file}")
    except Exception:
        pass
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Volume calibration
# ─────────────────────────────────────────────────────────────────────────────

def fetch_volume_scale(market: str, verbose: bool = True) -> float:
    """
    Compute volume_scale = Extended_24h_base_vol / Binance_24h_base_vol.

    This factor converts Binance candle volume to what would realistically trade
    on Extended Exchange.  For SOL-USD: ~1% (Extended trades ~1% of Binance futures).

    Falls back to 1.0 on any API error (no scaling).
    """
    bn_sym = to_binance_symbol(market)
    ext_market_name = market  # e.g. "SOL-USD"

    try:
        # Extended 24h base volume
        r_ext = requests.get(EXTENDED_MARKETS_API, timeout=10)
        r_ext.raise_for_status()
        ext_data = r_ext.json().get("data", [])
        ext_mkt  = next((m for m in ext_data if m.get("name") == ext_market_name), None)
        if not ext_mkt:
            raise ValueError(f"{ext_market_name} not found on Extended API")
        ext_base = float((ext_mkt.get("marketStats") or {}).get("dailyVolumeBase", 0) or 0)

        # Binance 24h base volume
        r_bn = requests.get(
            f"{BINANCE_FUTURES_BASE}/fapi/v1/ticker/24hr",
            params={"symbol": bn_sym}, timeout=10
        )
        r_bn.raise_for_status()
        bn_base = float(r_bn.json()["volume"])

        if bn_base <= 0:
            raise ValueError("Binance volume is zero")

        scale = ext_base / bn_base
        if verbose:
            print(f"\n  Volume scaling  ({market}):")
            print(f"    Extended 24h vol : {ext_base:>15,.0f}  {market.split('-')[0]}/day")
            print(f"    Binance  24h vol : {bn_base:>15,.0f}  {market.split('-')[0]}/day")
            print(f"    Scale factor     : {scale:.5f}  ({scale*100:.3f}% of Binance volume)")
        return scale

    except Exception as e:
        if verbose:
            print(f"\n  [volume scale] Error for {market}: {e}  (defaulting to 1.0)")
        return 1.0


def build_volume_profile(df: pd.DataFrame) -> list:
    """
    Build a 24-element intraday volume profile from 1m OHLCV data.

    Returns list[float] of length 24 (indexed by UTC hour 0..23).
    Values are normalised so that mean(profile) = 1.0.
    This captures the well-known intraday volume seasonality:
    - US/EU overlap peak (~14:00-16:00 UTC)
    - Asian night trough (~03:00-07:00 UTC)
    """
    df2 = df.copy()
    df2["hour"] = df2["open_time"].dt.hour
    avg_by_hour = df2.groupby("hour")["volume"].mean()
    profile = [float(avg_by_hour.get(h, avg_by_hour.mean())) for h in range(24)]
    mean_v = sum(profile) / 24
    if mean_v > 1e-10:
        profile = [v / mean_v for v in profile]
    return profile


# ─────────────────────────────────────────────────────────────────────────────
# Queue model
# ─────────────────────────────────────────────────────────────────────────────

def n_competitors_for_market(extended_usd_vol: float) -> int:
    """
    Estimate number of competing MMs quoting at the same price level on Extended Exchange.

    Extended is a smaller exchange; tier estimates are conservative.
    """
    if extended_usd_vol >= 10_000_000:   # BTC, HYPE, ETH, SOL tier
        return 8
    elif extended_usd_vol >= 1_000_000:  # mid-cap (NEAR, FIL, UNI...)
        return 5
    elif extended_usd_vol >= 100_000:    # small-cap ($100k-$1M/day)
        return 3
    else:
        return 2                          # micro (<$100k/day)


def hedge_threshold_for_market(extended_usd_vol: float) -> float:
    """
    Per-market inventory hedge trigger threshold based on Extended USD volume tier.

    Lower threshold = more aggressive hedging = smaller adverse position allowed.

    Rationale:
    - Thin markets have higher adverse selection per fill (each fill is a larger
      fraction of available depth), so we want to flatten inventory sooner.
    - Liquid markets have more mean-reversion and deeper books, so we can afford
      to hold a larger fraction of max_inv before hedging.

    Tiers:
      >= $10M/day  -> 0.80  (BTC, ETH, SOL — liquid, can absorb more inventory)
      >= $1M/day   -> 0.70  (mid-cap: NEAR, FIL, UNI...)
      >= $100k/day -> 0.60  (small-cap: $100k–$1M/day)
      <  $100k/day -> 0.50  (micro: thin markets, hedge early to limit adverse drift)
    """
    if extended_usd_vol >= 10_000_000:
        return 0.80
    elif extended_usd_vol >= 1_000_000:
        return 0.70
    elif extended_usd_vol >= 100_000:
        return 0.60
    else:
        return 0.50


def queue_fill_qty(
    vol_at_tick: float,
    quote_size: float,
    n_competitors: int = 4,
) -> float:
    """
    PRO-RATA QUEUE MODEL for limit order fills.

    In a pro-rata order book, each resting limit at a price level receives a
    fraction of the incoming aggressor volume proportional to its size relative
    to the total resting volume.

    With N_competitors other MMs each quoting the same size S as us:
        total_resting = (1 + N_competitors) * S
        our_fraction  = S / total_resting = 1 / (1 + N_competitors)
        fill_qty      = min(S, vol_at_tick * our_fraction)

    For time-priority (FIFO), the EXPECTED fill is the same in the long run
    assuming random queue position (uniform [0, N_comp]).  The pro-rata model
    gives equivalent expectation with lower variance, which is appropriate for
    a multi-year backtest where we're averaging over many queue positions.

    Parameters:
        vol_at_tick  : Volume (base asset) that trades at our specific tick level.
                       (= scaled Binance vol / ticks_in_candle_range)
        quote_size   : Our order size in base asset.
        n_competitors: Number of other MMs at the same level.

    Returns:
        Expected fill quantity (0 <= fill <= quote_size).
    """
    if n_competitors < 0:
        n_competitors = 0
    our_fraction = 1.0 / (1 + n_competitors)
    return min(quote_size, vol_at_tick * our_fraction)


# ─────────────────────────────────────────────────────────────────────────────
# Extended market info & capital allocation
# ─────────────────────────────────────────────────────────────────────────────

def load_extended_markets(
    min_usd_vol: float = 5_000,
    top_n: Optional[int] = None,
) -> list:
    """
    Load Extended Exchange active crypto market metadata.

    Priority: load from cached JSON (scripts/extended_markets.json) if available.
    Falls back to live API fetch.

    Returns list of dicts, sorted by usd_vol descending:
        name, base, usd_vol, base_vol, spread_bps, tick_size, bn_sym, vol_scale
    """
    cache = EXTENDED_MARKETS_CACHE
    if cache.exists():
        try:
            markets = json.loads(cache.read_text())
        except Exception:
            markets = _fetch_extended_markets_from_api()
    else:
        markets = _fetch_extended_markets_from_api()
        try:
            cache.write_text(json.dumps(markets, indent=2))
        except Exception:
            pass

    # Filter
    markets = [
        m for m in markets
        if m.get("usd_vol", 0) >= min_usd_vol
        and m.get("bn_sym")
        and not m["name"].startswith("k")   # skip synthetic k-tokens
        and "_" not in m["name"]             # skip _24_5 derivatives
    ]
    markets.sort(key=lambda x: -x.get("usd_vol", 0))
    if top_n is not None:
        markets = markets[:top_n]
    return markets


def _fetch_extended_markets_from_api() -> list:
    """Fetch fresh Extended market data from the API."""
    r_ext = requests.get(EXTENDED_MARKETS_API, timeout=20)
    r_ext.raise_for_status()
    raw = r_ext.json()["data"]

    r_bn = requests.get(f"{BINANCE_FUTURES_BASE}/fapi/v1/ticker/24hr", timeout=15)
    r_bn.raise_for_status()
    bn_vol  = {t["symbol"]: float(t["volume"])    for t in r_bn.json()}
    bn_info = requests.get(f"{BINANCE_FUTURES_BASE}/fapi/v1/exchangeInfo", timeout=15).json()
    bn_syms = {s["symbol"] for s in bn_info["symbols"] if s["status"] == "TRADING"}

    markets = []
    for m in raw:
        if m.get("category") != "Crypto" or m.get("status") != "ACTIVE":
            continue
        name = m["name"]
        if "_" in name or name.startswith("k") or "SPOT" in name:
            continue
        base = name.split("-")[0]
        bn_sym = base + "USDT"
        if bn_sym not in bn_syms:
            continue
        stats = m.get("marketStats") or {}
        usd_vol  = float(stats.get("dailyVolume", 0) or 0)
        base_vol = float(stats.get("dailyVolumeBase", 0) or 0)
        bid_px   = float(stats.get("bidPrice", 0) or 0)
        ask_px   = float(stats.get("askPrice", 0) or 0)
        spread_bps = (10000 * (ask_px - bid_px) / ((bid_px + ask_px) / 2)
                      if bid_px > 0 and ask_px > 0 else 0.0)
        tc = m.get("tradingConfig") or {}
        tick_size = float(tc.get("minPriceChange", 0.01) or 0.01)
        b_base    = bn_vol.get(bn_sym, 1.0)
        vol_scale = (base_vol / b_base) if b_base > 0 and base_vol > 0 else 0.0
        markets.append({
            "name": name, "base": base,
            "usd_vol": usd_vol, "base_vol": base_vol,
            "spread_bps": round(spread_bps, 2),
            "tick_size": tick_size,
            "bn_sym": bn_sym,
            "vol_scale": round(vol_scale, 6),
        })
    markets.sort(key=lambda x: -x["usd_vol"])
    return markets


def quote_size_from_volume(usd_vol: float, floor: float = 50.0, cap: float = 1_000.0,
                           participation: float = 0.02) -> float:
    """
    Quote size as a fraction of typical 1-minute Extended notional.

    1m_notional = daily_usd_vol / 1440
    quote_usd   = clip(participation * 1m_notional, floor, cap)
    """
    minute_notional = max(0.0, float(usd_vol)) / 1440.0
    return max(floor, min(cap, participation * minute_notional))


def allocate_capital(
    markets: list,
    per_pair_max_inv: float = 10_000,
) -> dict:
    """
    Equal per-pair inventory cap; volume-scaled quote size.

    Returns dict: market_name -> {max_inv, quote_size, half_spread_bps, n_competitors,
                                  hedge_threshold, vol_scale, tick_size, bn_sym}

    - max_inv: $per_pair_max_inv on every market (default $10,000) — risk cap
    - Quote size: 2% of typical 1-minute Extended notional, clipped to [$50, $1,000]
    - Half-spread floor: max(8 bps, current Extended half-spread)
      (actual quoted spread is further widened by lagged realized vol in PaperMM)
    """
    if not markets:
        return {}

    result = {}
    for m in markets:
        name       = m["name"]
        max_inv    = float(per_pair_max_inv)
        usd_vol    = m.get("usd_vol", 0)
        quote_size = quote_size_from_volume(usd_vol)
        half_spread = max(8.0, m.get("spread_bps", 0) / 2.0)
        tick_size   = max(1e-7, float(m.get("tick_size", 0.01)))
        result[name] = {
            "max_inv":         round(max_inv, 2),
            "quote_size":      round(quote_size, 2),
            "half_spread_bps": round(half_spread, 2),
            "n_competitors":   n_competitors_for_market(usd_vol),
            "hedge_threshold": hedge_threshold_for_market(usd_vol),
            "vol_scale":       float(m.get("vol_scale", 1.0)),
            "tick_size":       tick_size,
            "bn_sym":          m["bn_sym"],
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Performance metrics
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(equity: pd.Series, candle_interval_minutes: int = 1) -> dict:
    if len(equity) < 2:
        return {}
    returns = equity.diff().dropna()
    candles_per_year = (60 * 24 / candle_interval_minutes) * 365
    total_pnl = float(equity.iloc[-1] - equity.iloc[0])
    std  = returns.std()
    mean = returns.mean()
    sharpe = (mean / std * math.sqrt(candles_per_year)) if std > 1e-12 else 0.0
    roll_max  = equity.cummax()
    max_dd    = float((equity - roll_max).min())
    win_rate  = float((returns > 0).mean())
    gp = float(returns[returns > 0].sum())
    gl = float(abs(returns[returns < 0].sum()))
    pf = gp / gl if gl > 0 else float("inf")
    return {
        "total_pnl_usd": total_pnl, "sharpe_ratio": sharpe,
        "max_drawdown_usd": max_dd, "win_rate": win_rate,
        "profit_factor": pf, "num_candles": len(equity),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main simulation
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TickResult:
    open_time: datetime
    mid: float
    bid: float
    ask: float
    our_bid: float
    our_ask: float
    pos: float
    cash: float
    equity: float
    norm_inv: float
    ofi: float
    hedge_fired: bool
    passive_fill: bool
    fill_side: str
    fill_notional: float


def run_backtest(
    market: str,
    df: pd.DataFrame,
    cfg_path: str = "config.yaml",
    verbose: bool = True,
    # ── Fill model ────────────────────────────────────────────────────────────
    volume_scale: float = 1.0,
    n_competitors: int = 4,
    volume_profile: Optional[list] = None,
    min_fill_qty: float = 0.0,
    # ── Per-market overrides (override config.yaml) ───────────────────────────
    max_inventory_usd: Optional[float] = None,
    spread_bps: Optional[float] = None,      # half-spread in bps
    quote_size_usd: Optional[float] = None,
    tick_size_override: Optional[float] = None,
    hedge_threshold: Optional[float] = None, # override trigger_threshold in HedgeParams
) -> tuple:
    """
    Run the PaperMM simulation over historical candle data.

    Fill model (volume-per-tick + pro-rata queue):
        1. Scale Binance volume: scaled_vol = agg_vol * volume_scale
        2. Distribute uniformly: vol_per_tick = scaled_vol / ticks_in_range
        3. Apply intraday weight: vol_per_tick *= volume_profile[hour]
        4. Queue model: fill = queue_fill_qty(vol_per_tick, quote_size, n_competitors)

    See module docstring for full model description.
    """
    cfg = load_config(cfg_path)
    sim = cfg.sim
    _inv = sim.inventory

    eff_max_inv    = max_inventory_usd  if max_inventory_usd  is not None else sim.max_inventory_usd
    eff_spread_bps = spread_bps         if spread_bps          is not None else sim.quote_half_spread_bps
    eff_quote_size = quote_size_usd     if quote_size_usd      is not None else sim.quote_size_usd
    eff_tick_size  = tick_size_override if tick_size_override  is not None else sim.tick_size

    trade_stats = TradeStats()
    paper = PaperMM(
        quote_half_spread_bps=eff_spread_bps,
        quote_size_usd=eff_quote_size,
        max_inventory_usd=eff_max_inv,
        tick_size=eff_tick_size,
        maker_fee_pct=sim.maker_fee_pct,
        inventory_cfg=InventoryControlParams(
            inv_skew_strength=_inv.inv_skew_strength,
            size_skew_strength=_inv.size_skew_strength,
            min_size_mult=_inv.min_size_mult,
            max_size_mult=_inv.max_size_mult,
            near_limit_threshold=_inv.near_limit_threshold,
            near_limit_side_mult=_inv.near_limit_side_mult,
            vol_spread_k=_inv.vol_spread_k,
            vol_ewma_span=_inv.vol_ewma_span,
            max_half_spread_bps=_inv.max_half_spread_bps,
            toxic_inv_threshold=_inv.toxic_inv_threshold,
            toxic_ofi_threshold=_inv.toxic_ofi_threshold,
        ),
        ofi_cfg=OFIParams(
            ofi_skew_strength=sim.ofi.ofi_skew_strength if sim.ofi.enabled else 0.0,
        ),
        hedge_cfg=HedgeParams(
            trigger_threshold=hedge_threshold if hedge_threshold is not None else sim.hedge.trigger_threshold,
            hedge_fraction=sim.hedge.hedge_fraction,
            cooldown_ticks=sim.hedge.cooldown_ticks,
            taker_fee_pct=sim.hedge.taker_fee_pct,
        ),
        trade_stats=trade_stats,
    )
    paper.current_market = market

    ofi_window = int(sim.ofi.window_seconds)
    _ofi_buf: deque = deque(maxlen=max(1, ofi_window))

    results: list = []

    if verbose:
        print(f"\nRunning simulation on {len(df):,} candles for {market}...")

    for tick, row in enumerate(df.itertuples(index=False)):
        paper.current_tick = tick

        mid      = float(row.open)
        high     = float(row.high)
        low      = float(row.low)
        buy_vol  = float(row.taker_buy_volume)
        sell_vol = max(0.0, float(row.volume) - buy_vol)

        # ── OFI signal ────────────────────────────────────────────────────────
        _ofi_buf.append((buy_vol, sell_vol))
        ofi_signal = 0.0
        if sim.ofi.enabled and len(_ofi_buf) >= 2:
            total_bv = sum(b for b, _ in _ofi_buf)
            total_sv = sum(s for _, s in _ofi_buf)
            denom = total_bv + total_sv
            if denom > 1e-12:
                ofi_signal = max(-1.0, min(1.0, (total_bv - total_sv) / denom))

        # ── Synthetic TOB ─────────────────────────────────────────────────────
        tick_sz = eff_tick_size
        tob = TopOfBook(
            bid_px=mid - tick_sz, bid_qty=1e6,
            ask_px=mid + tick_sz, ask_qty=1e6,
        )

        # ── Hedge (before quoting) ────────────────────────────────────────────
        hedge_fired = False
        if sim.hedge.enabled:
            hedge_fired = paper.execute_hedge(market, tob, mid)

        # ── Generate quotes ───────────────────────────────────────────────────
        q = paper.make_quote(mid, ofi_signal=ofi_signal)

        # ── Intraday volume weight ────────────────────────────────────────────
        # Scale volume by time-of-day to reflect intraday liquidity seasonality.
        hour_weight = 1.0
        if volume_profile is not None:
            try:
                hour_weight = volume_profile[row.open_time.hour]
            except (AttributeError, IndexError):
                pass

        # ── Fill simulation ───────────────────────────────────────────────────
        #
        # DATA: We use Binance 1m klines with taker_buy_volume (aggressor split).
        # This is richer than pure OHLC: the taker volume split captures whether
        # each candle's activity was primarily buy-driven or sell-driven.
        #
        # FILL MODEL:
        #   BUY  aggressor swept price UP   to `high`  (qty = taker_buy_volume)
        #   SELL aggressor swept price DOWN to `low`   (qty = total - taker_buy)
        #
        # For each aggressor that reaches our quote:
        #   1. Scale Binance volume to Extended Exchange:
        #        scaled_vol = agg_vol * volume_scale
        #   2. Volume-per-tick (uniform distribution over candle range):
        #        ticks_in_range = candle_range / tick_size
        #        vol_per_tick   = scaled_vol / ticks_in_range
        #   3. Intraday weight:
        #        vol_per_tick *= hour_weight
        #   4. Pro-rata queue model:
        #        fill_qty = min(quote_size, vol_per_tick / (1 + N_competitors))
        #
        candle_range  = max(high - low, 1e-8)
        ticks_in_range = max(1.0, candle_range / tick_sz)

        pos_before = paper.positions.get(market, {}).get("pos", 0.0)

        for agg_side, agg_px, agg_vol in [
            ("BUY",  high, buy_vol),
            ("SELL", low,  sell_vol),
        ]:
            if agg_vol <= 0:
                continue
            # Price-priority gate
            if agg_side == "BUY"  and agg_px < q.ask_px:
                continue
            if agg_side == "SELL" and agg_px > q.bid_px:
                continue

            # Scale to Extended and distribute uniformly across tick range
            scaled_vol   = agg_vol * volume_scale
            vol_per_tick = (scaled_vol / ticks_in_range) * hour_weight

            # Pro-rata queue fill
            quote_size_base = q.ask_qty if agg_side == "BUY" else q.bid_qty
            if quote_size_base < 1e-12:
                continue
            effective_vol   = queue_fill_qty(vol_per_tick, quote_size_base, n_competitors)

            if effective_vol < min_fill_qty:
                continue

            paper.on_trade(
                mid=mid, trade_px=agg_px, trade_qty=effective_vol,
                side=agg_side, q=q,
            )

        pos_after = paper.positions.get(market, {}).get("pos", 0.0)
        pos_delta = pos_after - pos_before
        passive_fill = abs(pos_delta) > 1e-10
        fill_side_str = "BUY" if pos_delta > 0 else ("SELL" if pos_delta < 0 else "")

        close_mid = float(row.close)
        equity    = paper.mark_to_market({market: close_mid})
        fill_notional = abs(pos_delta) * mid if passive_fill else 0.0

        # Lagged vol: this candle's range feeds the NEXT quote (no look-ahead)
        if mid > 0:
            paper.update_realized_vol(10000.0 * (high - low) / mid)

        results.append(TickResult(
            open_time=row.open_time, mid=mid,
            bid=tob.bid_px, ask=tob.ask_px,
            our_bid=q.bid_px, our_ask=q.ask_px,
            pos=pos_after, cash=paper.state.cash_usd, equity=equity,
            norm_inv=paper.last_norm_inv, ofi=ofi_signal,
            hedge_fired=hedge_fired, passive_fill=passive_fill,
            fill_side=fill_side_str, fill_notional=fill_notional,
        ))

        if verbose and tick % 10_000 == 0 and tick > 0:
            print(f"  tick={tick:,}  equity=${equity:.2f}  pos={pos_after:.4f}  hedges={paper.hedge_count}", flush=True)

    return results, trade_stats, paper


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

def print_report(
    market: str,
    results: list,
    stats: TradeStats,
    paper: PaperMM,
    start_dt: datetime,
    end_dt: datetime,
    volume_scale: float = 1.0,
    n_competitors: int = 4,
    half_spread_bps: float = 0.2,
) -> None:
    if not results:
        print("No results.")
        return

    eq = pd.Series([r.equity for r in results], index=[r.open_time for r in results])
    metrics = compute_metrics(eq)
    fill_rate = sum(1 for r in results if r.passive_fill) / len(results)
    pos_state = paper.positions.get(market, {})

    sep  = "=" * 66
    dash = "-" * 66
    arrow = "->"

    print()
    print(sep)
    print(f"  BACKTEST RESULTS")
    print(f"  {market}  |  {start_dt.date()} {arrow} {end_dt.date()}")
    print(f"  Fill model: vol_scale={volume_scale:.4f}  n_comp={n_competitors}  spread={half_spread_bps:.1f}bps")
    print(sep)
    print(f"  Period              : {len(results):,} minutes ({len(results)/60/24:.1f} days)")
    print(f"  Total PnL           : ${metrics.get('total_pnl_usd', 0):+.2f}")
    print(f"  Sharpe ratio        : {metrics.get('sharpe_ratio', 0):.3f}  (annualised, 1-min returns)")
    print(f"  Max drawdown        : ${metrics.get('max_drawdown_usd', 0):.2f}")
    print(f"  Win rate (per min)  : {metrics.get('win_rate', 0):.1%}")
    print(f"  Profit factor       : {metrics.get('profit_factor', 0):.2f}")
    print(dash)
    print(f"  Passive fills       : {stats.num_trades}")
    print(f"  Fill rate           : {fill_rate:.2%}  (minutes with fill / total)")
    avg_fill = stats.total_volume / max(1, stats.num_trades)
    print(f"  Avg fill size       : {avg_fill:.4f}  {market.split('-')[0]}")
    print(f"  Total volume        : {stats.total_volume:.4f}  {market.split('-')[0]}")
    print(f"  Total notional      : ${stats.total_notional:,.0f}")
    print(f"  Buy volume          : {stats.buy_volume:.4f}")
    print(f"  Sell volume         : {stats.sell_volume:.4f}")
    imb = stats.buy_volume - stats.sell_volume
    print(f"  Volume imbalance    : {imb:+.4f}  (+ = net long; should match final pos)")
    print(dash)
    print(f"  Total edge (net fee): ${paper.total_edge_collected:+.2f}")
    print(f"  Hedge count         : {paper.hedge_count}")
    final_pos = pos_state.get("pos", 0.0)
    avg_px    = pos_state.get("avg_price", 0.0)
    print(f"  Final position      : {final_pos:.4f}  {market.split('-')[0]}  (avg px {avg_px:.4f})")
    print(f"  Final cash          : ${paper.state.cash_usd:+.2f}")
    print(f"  Final equity        : ${results[-1].equity:+.2f}")
    print(sep)


def save_results(results: list, output_path: str) -> None:
    rows = []
    for r in results:
        rows.append({
            "open_time": r.open_time, "mid": r.mid,
            "bid": r.bid, "ask": r.ask,
            "our_bid": r.our_bid, "our_ask": r.our_ask,
            "pos": r.pos, "cash": r.cash, "equity": r.equity,
            "norm_inv": r.norm_inv, "ofi": r.ofi,
            "hedge_fired": r.hedge_fired, "passive_fill": r.passive_fill,
            "fill_side": r.fill_side, "fill_notional": r.fill_notional,
        })
    pd.DataFrame(rows).to_csv(output_path, index=False)
    print(f"Results saved to: {output_path}")


def plot_results(
    market: str,
    results: list,
    start_dt: datetime,
    end_dt: datetime,
    output_path: str = "",
    volume_scale: float = 1.0,
    n_competitors: int = 4,
    half_spread_bps: float = 0.2,
    hedge_threshold: float = 0.9,
) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        print("matplotlib not installed; skipping chart.")
        return

    times    = [r.open_time for r in results]
    equity   = [r.equity for r in results]
    pos      = [r.pos for r in results]
    mid      = [r.mid for r in results]
    norm_inv = [r.norm_inv for r in results]

    fig, axes = plt.subplots(4, 1, figsize=(14, 12), sharex=True)
    fill_mode = f"p=1/{1+n_competitors}  scale={volume_scale:.3%}  spread={half_spread_bps:.1f}bps"
    fig.suptitle(
        f"Market-Making Backtest  |  {market}  [{fill_mode}]  |  "
        f"{start_dt.date()} to {end_dt.date()}",
        fontsize=11
    )

    ax = axes[0]
    ax.plot(times, equity, color="steelblue", linewidth=0.7, label="Equity (USD)")
    ax.axhline(0, color="gray", linewidth=0.5, linestyle="--")
    ax.fill_between(times, 0, equity, where=[e >= 0 for e in equity], alpha=0.15, color="green")
    ax.fill_between(times, 0, equity, where=[e <  0 for e in equity], alpha=0.15, color="red")
    ax.set_ylabel("PnL (USD)")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    ax.plot(times, mid, color="black", linewidth=0.5, label=f"{market} mid")
    ax.set_ylabel("Price (USD)")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.3)

    ax = axes[2]
    ax.plot(times, pos, color="darkorange", linewidth=0.6, label="Position")
    ax.axhline(0, color="gray", linewidth=0.5, linestyle="--")
    ax.fill_between(times, 0, pos, where=[p >= 0 for p in pos], alpha=0.2, color="orange")
    ax.fill_between(times, 0, pos, where=[p <  0 for p in pos], alpha=0.2, color="purple")
    ax.set_ylabel(f"Position ({market.split('-')[0]})")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.3)

    ax = axes[3]
    ax.plot(times, norm_inv, color="crimson", linewidth=0.5, label="norm_inv")
    ax.axhline(0,               color="gray", linewidth=0.5, linestyle="--")
    ax.axhline( hedge_threshold, color="red",  linewidth=0.5, linestyle=":", alpha=0.7, label=f"ht={hedge_threshold:.2f}")
    ax.axhline(-hedge_threshold, color="red",  linewidth=0.5, linestyle=":", alpha=0.7)
    ax.set_ylim(-1.1, 1.1)
    ax.set_ylabel("Norm inventory")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())

    plt.xticks(rotation=30)
    plt.tight_layout()

    if output_path:
        plt.savefig(output_path, dpi=150, bbox_inches="tight")
        print(f"Chart saved to: {output_path}")
    else:
        plt.show()
    plt.close(fig)


# ─────────────────────────────────────────────────────────────────────────────
# Multi-pair backtest
# ─────────────────────────────────────────────────────────────────────────────

def _download_single_pair(args_tuple):
    """Worker for parallel data download. Returns (market_name, df_or_error)."""
    bn_sym, start_ms, end_ms, verbose = args_tuple
    try:
        df = fetch_klines(bn_sym, "1m", start_ms, end_ms,
                          verbose=verbose, cache_dir="", use_futures=True)
        return bn_sym, df, None
    except Exception as e:
        return bn_sym, None, str(e)


def _downsample_plot(results: list, step: int = 60) -> dict:
    """Keep hourly points (plus the last tick) for plotting. Full 1m series is too large."""
    if not results:
        return {"times": [], "equity": [], "norm_inv": [], "inv_usd": []}
    idxs = list(range(0, len(results), step))
    if idxs[-1] != len(results) - 1:
        idxs.append(len(results) - 1)
    return {
        "times":    [results[i].open_time for i in idxs],
        "equity":   [results[i].equity    for i in idxs],
        "norm_inv": [results[i].norm_inv  for i in idxs],
        "inv_usd":  [results[i].pos * results[i].mid for i in idxs],
    }


def _hourly_close(df: pd.DataFrame) -> pd.Series:
    """Resample 1m OHLCV to 1h last close (UTC), for lagged BTC beta."""
    s = df.set_index("open_time")["close"].copy()
    s.index = pd.to_datetime(s.index, utc=True)
    return s.resample("1h").last().dropna()


def _align_plot_field(all_results: dict, field: str) -> pd.DataFrame:
    """
    Outer-join per-market hourly series on timestamp.
    Forward-fill after a market lists; 0 before first observation (no look-ahead).
    """
    series = {}
    for name, r in all_results.items():
        plot = r.get("plot") or {}
        times = plot.get("times") or []
        vals = plot.get(field) or []
        if not times or len(times) != len(vals):
            continue
        s = pd.Series(vals, index=pd.to_datetime(times, utc=True)).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        series[name] = s
    if not series:
        return pd.DataFrame()
    frame = pd.DataFrame(series).sort_index()
    return frame.ffill().fillna(0.0)


# Conservative BTC-factor overlay (all-pairs only). Not Markowitz.
FACTOR_HEDGE_THRESHOLD_USD = 15_000.0   # fire only if |net BTC-beta USD| exceeds this
FACTOR_HEDGE_FRACTION      = 0.30       # flatten 30% of the *excess* above the threshold
FACTOR_HEDGE_COOLDOWN_H    = 6          # hours between overlay hedges
FACTOR_BETA_WINDOW_H       = 30 * 24    # ~30 days of 1h returns
FACTOR_BETA_HAIRCUT        = 0.5
FACTOR_BETA_CLIP           = 1.5
FACTOR_TAKER_FEE           = 0.00025    # Extended taker 0.025%


def simulate_btc_factor_hedge(all_results: dict) -> dict:
    """
    Walk the combined book hourly and taker-hedge residual BTC-beta on BTC-USD only.

    beta_used_i = 0.5 * clip(rolling 30d 1h beta vs BTC, -1.5, 1.5), lagged one hour.
    BTC-USD itself uses beta 1. Overlay position is extra (does not rewrite MM fills).
    No-op if BTC-USD is missing from results.
    """
    empty = {
        "count": 0, "equity": None, "hedge_usd": None,
        "net_factor": None, "final_pnl": 0.0,
    }
    if "BTC-USD" not in all_results:
        print("  [factor hedge] BTC-USD not in results — skipping overlay.")
        return empty

    inv_df = _align_plot_field(all_results, "inv_usd")
    if inv_df.empty:
        return empty

    px = {}
    for name, r in all_results.items():
        s = r.get("hourly_px")
        if s is None or len(s) < 50:
            continue
        s = s.copy()
        s.index = pd.to_datetime(s.index, utc=True)
        s = s[~s.index.duplicated(keep="last")].sort_index()
        px[name] = s
    if "BTC-USD" not in px:
        print("  [factor hedge] no BTC hourly closes — skipping overlay.")
        return empty

    px_df = pd.DataFrame(px).reindex(inv_df.index).ffill()
    btc_px = px_df["BTC-USD"]
    btc_ret = btc_px.pct_change()

    beta_cols = {}
    for name in inv_df.columns:
        if name == "BTC-USD":
            beta_cols[name] = pd.Series(1.0, index=inv_df.index)
            continue
        if name not in px_df.columns:
            beta_cols[name] = pd.Series(0.0, index=inv_df.index)
            continue
        ret = px_df[name].pct_change()
        cov = ret.rolling(FACTOR_BETA_WINDOW_H, min_periods=48).cov(btc_ret)
        var = btc_ret.rolling(FACTOR_BETA_WINDOW_H, min_periods=48).var()
        raw = (cov / var.replace(0.0, np.nan)).shift(1)  # lagged: no look-ahead
        used = FACTOR_BETA_HAIRCUT * raw.clip(-FACTOR_BETA_CLIP, FACTOR_BETA_CLIP)
        beta_cols[name] = used.fillna(0.0)
    beta_df = pd.DataFrame(beta_cols).reindex(inv_df.index).fillna(0.0)

    mm_factor = (beta_df * inv_df).sum(axis=1)

    hedge_btc = 0.0
    cash = 0.0
    last_hedge_i = -10**9
    count = 0
    eq_path = []
    hedge_usd_path = []
    net_path = []

    times = list(inv_df.index)
    for i, t in enumerate(times):
        px_t = float(btc_px.iloc[i]) if pd.notna(btc_px.iloc[i]) else 0.0
        hedge_usd = hedge_btc * px_t
        net = float(mm_factor.iloc[i]) + hedge_usd

        if (
            px_t > 0
            and abs(net) > FACTOR_HEDGE_THRESHOLD_USD
            and (i - last_hedge_i) >= FACTOR_HEDGE_COOLDOWN_H
        ):
            excess = net - math.copysign(FACTOR_HEDGE_THRESHOLD_USD, net)
            trade_usd = FACTOR_HEDGE_FRACTION * excess  # >0 → sell BTC (cut long beta)
            qty = trade_usd / px_t
            hedge_btc -= qty
            fee = abs(trade_usd) * FACTOR_TAKER_FEE
            cash += trade_usd - fee
            last_hedge_i = i
            count += 1
            hedge_usd = hedge_btc * px_t
            net = float(mm_factor.iloc[i]) + hedge_usd

        eq_path.append(cash + hedge_usd)
        hedge_usd_path.append(hedge_usd)
        net_path.append(net)

    eq_s = pd.Series(eq_path, index=inv_df.index)
    print(f"  [factor hedge] {count} BTC overlay hedges  |  overlay PnL ${eq_s.iloc[-1]:+.2f}")
    return {
        "count": count,
        "equity": eq_s,
        "hedge_usd": pd.Series(hedge_usd_path, index=inv_df.index),
        "net_factor": pd.Series(net_path, index=inv_df.index),
        "final_pnl": float(eq_s.iloc[-1]),
        "mm_factor": mm_factor,
    }


def plot_aggregate_pnl(all_results: dict, out_dir: Path, factor: Optional[dict] = None) -> None:
    """Portfolio equity: sum of per-market MM PnL (+ optional BTC factor overlay)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        print("matplotlib not installed; skipping aggregate PnL chart.")
        return

    eq = _align_plot_field(all_results, "equity")
    if eq.empty:
        return
    port = eq.sum(axis=1)
    final = float(port.iloc[-1])

    fig, ax = plt.subplots(figsize=(14, 5))
    ax.plot(port.index, port.values, color="steelblue", linewidth=1.1, label="MM book")
    ax.axhline(0, color="gray", linewidth=0.5, linestyle="--")
    ax.fill_between(port.index, 0, port.values,
                    where=port.values >= 0, alpha=0.15, color="green")
    ax.fill_between(port.index, 0, port.values,
                    where=port.values < 0, alpha=0.15, color="red")

    extra = ""
    if factor and factor.get("equity") is not None:
        combined = port.add(factor["equity"].reindex(port.index).fillna(0.0), fill_value=0.0)
        ax.plot(combined.index, combined.values, color="black", linewidth=1.0,
                linestyle="--", label="MM + BTC factor hedge")
        extra = (f"  |  +factor ${float(combined.iloc[-1]):+.0f}  "
                 f"({factor.get('count', 0)} overlay hedges)")

    ax.set_ylabel("PnL (USD)")
    ax.set_title(f"Aggregate MM PnL  |  final ${final:+,.0f}{extra}", loc="left")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    plt.xticks(rotation=25)
    plt.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "aggregate_pnl.png"
    plt.savefig(str(path), dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"  Chart saved: {path}")


def plot_aggregate_exposure(all_results: dict, out_dir: Path, factor: Optional[dict] = None) -> None:
    """Net and gross inventory USD of the combined book."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        print("matplotlib not installed; skipping aggregate exposure chart.")
        return

    inv = _align_plot_field(all_results, "inv_usd")
    if inv.empty:
        return
    net = inv.sum(axis=1)
    gross = inv.abs().sum(axis=1)
    max_net = float(net.abs().max())
    max_gross = float(gross.max())

    fig, ax = plt.subplots(figsize=(14, 5))
    ax.plot(net.index, net.values, color="darkorange", linewidth=1.0, label="Net inventory USD")
    ax.plot(gross.index, gross.values, color="steelblue", linewidth=1.0, label="Gross |inventory| USD")
    ax.axhline(0, color="gray", linewidth=0.5, linestyle="--")
    if factor and factor.get("hedge_usd") is not None:
        net_h = net.add(factor["hedge_usd"].reindex(net.index).fillna(0.0), fill_value=0.0)
        ax.plot(net_h.index, net_h.values, color="black", linewidth=0.9,
                linestyle="--", label="Net + BTC factor overlay")
        ax.axhline( FACTOR_HEDGE_THRESHOLD_USD, color="red", linewidth=0.6, linestyle=":", alpha=0.7)
        ax.axhline(-FACTOR_HEDGE_THRESHOLD_USD, color="red", linewidth=0.6, linestyle=":", alpha=0.7)

    ax.set_ylabel("USD")
    ax.set_title(
        f"Aggregate exposure  |  max |net| ${max_net:,.0f}  |  max gross ${max_gross:,.0f}",
        loc="left",
    )
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    plt.xticks(rotation=25)
    plt.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "aggregate_exposure.png"
    plt.savefig(str(path), dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"  Chart saved: {path}")


def run_all_pairs_backtest(
    total_capital: float = 25_000,
    months: int = 6,
    top_n: Optional[int] = None,
    min_usd_vol: float = 5_000,
    cfg_path: str = "config.yaml",
    verbose: bool = True,
    no_plot: bool = False,
    save_csv: str = "",
    output_dir: str = "",
    per_pair_max_inv: float = 10_000,
    factor_hedge: bool = True,
) -> dict:
    """
    Run the market-making backtest for ALL active Extended Exchange pairs.

    1. Load Extended markets from JSON cache (or fetch live).
    2. Assign $per_pair_max_inv inventory to every market.
    3. Download 1m Binance data in parallel (cached after first run).
    4. For each market: build volume profile, run simulation.
    5. Print aggregate report and save combined CSV.

    Full per-tick results are not retained (12-month runs would OOM). Plot series
    are hourly-downsampled; the combined CSV is written incrementally per market.
    """
    out_dir = Path(output_dir) if output_dir else ROOT
    if save_csv:
        csv_path = Path(save_csv)
        if not csv_path.is_absolute():
            csv_path = ROOT / save_csv
        save_csv = str(csv_path)
        if csv_path.exists():
            csv_path.unlink()

    print(f"\n{'='*70}")
    print(f"  ALL-PAIRS MULTI-MARKET BACKTEST")
    print(f"  Per-pair max inv: ${per_pair_max_inv:,.0f}  |  Lookback: {months} months")
    print(f"{'='*70}")

    # ── Load markets ──────────────────────────────────────────────────────────
    markets = load_extended_markets(min_usd_vol=min_usd_vol, top_n=None)
    if not markets:
        print("ERROR: No markets found. Run scripts/discover_markets.py first.")
        return {}

    # Exclude first, then apply top-n so we still get N tradeable markets
    before = len(markets)
    markets = [m for m in markets if m["name"] not in EXCLUDE_MARKETS]
    n_excluded = before - len(markets)
    if n_excluded:
        print(f"\nExcluded: {', '.join(sorted(EXCLUDE_MARKETS))}  ({n_excluded} markets removed)")
    if top_n is not None:
        markets = markets[:top_n]

    print(f"\nMarkets loaded: {len(markets)}")

    # ── Capital allocation ────────────────────────────────────────────────────
    alloc = allocate_capital(markets, per_pair_max_inv=per_pair_max_inv)
    gross = per_pair_max_inv * len(markets)

    print(f"\nCapital allocation (${per_pair_max_inv:,.0f}/pair, gross ${gross:,.0f}):")
    print(f"  {'Market':<20} {'Max Inv':>8}  {'Quote$':>7}  {'Spread':>8}  {'NComp':>5}  {'HgThr':>6}  {'VolScale':>9}")
    print(f"  {'-'*78}")
    for m in markets[:20]:  # show top 20
        name = m["name"]
        a    = alloc[name]
        print(f"  {name:<20} {a['max_inv']:>8,.0f}  {a['quote_size']:>7,.0f}  "
              f"{a['half_spread_bps']:>7.1f}bps  {a['n_competitors']:>5}  {a['hedge_threshold']:>5.2f}  {a['vol_scale']:>8.3%}")
    if len(markets) > 20:
        print(f"  ... ({len(markets)-20} more markets not shown)")

    # ── Date range ────────────────────────────────────────────────────────────
    now_utc  = datetime.now(timezone.utc)
    end_dt   = now_utc
    start_dt = end_dt - timedelta(days=months * 30)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp() * 1000)

    print(f"\nBacktest period: {start_dt.date()} to {end_dt.date()}")

    # ── Download data in parallel ─────────────────────────────────────────────
    print(f"\nDownloading {len(markets)} market datasets (parallel, cached)...")
    download_args = [
        (alloc[m["name"]]["bn_sym"], start_ms, end_ms, False)
        for m in markets
    ]
    downloaded = {}
    failed = []

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {}
        for i, a in enumerate(download_args):
            time.sleep(0.5)  # stagger submissions to avoid burst rate-limit
            futures[executor.submit(_download_single_pair, a)] = a[0]
        done = 0
        for future in as_completed(futures):
            bn_sym, df, err = future.result()
            done += 1
            if err:
                print(f"  [{done}/{len(markets)}] FAILED {bn_sym}: {err}")
                failed.append(bn_sym)
            else:
                downloaded[bn_sym] = df
                if verbose:
                    print(f"  [{done}/{len(markets)}] {bn_sym}: {len(df):,} candles", flush=True)

    print(f"\nDownloaded: {len(downloaded)}/{len(markets)}  |  Failed: {len(failed)}")
    if failed:
        print(f"  First-pass failures: {', '.join(failed)}")
        print(f"\nRetrying {len(failed)} failed downloads sequentially after 20s...")
        time.sleep(20)
        still_failed = []
        for bn_sym in failed:
            _, df, err = _download_single_pair((bn_sym, start_ms, end_ms, True))
            if err or df is None:
                still_failed.append(bn_sym)
                print(f"  RETRY FAILED {bn_sym}: {err}")
            else:
                downloaded[bn_sym] = df
                print(f"  RETRY OK {bn_sym}: {len(df):,} candles", flush=True)
            time.sleep(1.0)
        failed = still_failed
        print(f"\nAfter retry: {len(downloaded)}/{len(markets)}  |  Still failed: {len(failed)}")
        if failed:
            print(f"  Skipping: {', '.join(failed)}")

    # ── Run backtests sequentially ────────────────────────────────────────────
    print(f"\nRunning backtests...")
    all_results = {}
    cfg = load_config(cfg_path if Path(cfg_path).is_absolute()
                      else str(ROOT / cfg_path))

    for i, m in enumerate(markets, 1):
        name   = m["name"]
        a      = alloc[name]
        bn_sym = a["bn_sym"]

        if bn_sym not in downloaded:
            continue

        df = downloaded[bn_sym]
        if len(df) < 100:
            print(f"  [{i}/{len(markets)}] {name}: skipping (only {len(df)} candles)")
            continue

        # Build volume profile from this market's data
        profile = build_volume_profile(df)

        print(f"  [{i}/{len(markets)}] {name}  "
              f"({len(df):,} candles, ${a['max_inv']:,.0f} max_inv, "
              f"{a['half_spread_bps']:.1f}bps, N={a['n_competitors']}, "
              f"ht={a['hedge_threshold']:.2f})", flush=True)

        try:
            results, stats, paper = run_backtest(
                market=name,
                df=df,
                cfg_path=cfg_path if Path(cfg_path).is_absolute() else str(ROOT / cfg_path),
                verbose=False,
                volume_scale=a["vol_scale"],
                n_competitors=a["n_competitors"],
                volume_profile=profile,
                max_inventory_usd=a["max_inv"],
                spread_bps=a["half_spread_bps"],
                quote_size_usd=a["quote_size"],
                tick_size_override=a["tick_size"],
                hedge_threshold=a["hedge_threshold"],
            )
        except Exception as e:
            print(f"    ERROR: {e}")
            continue

        eq    = results[-1].equity if results else 0.0
        pos   = paper.positions.get(name, {}).get("pos", 0.0)
        edge  = paper.total_edge_collected
        n_candles = len(results)
        ndays = n_candles / (60 * 24)
        fill_count = stats.num_trades
        minutes_with_fill = sum(1 for t in results if t.passive_fill)
        fill_rate = minutes_with_fill / n_candles if n_candles else 0.0
        print(f"    equity={eq:+.2f}  edge={edge:+.2f}  pos={pos:+.3f}  "
              f"fills={fill_count:,}  hedges={paper.hedge_count}  days={ndays:.0f}")

        eq_series = pd.Series([t.equity for t in results])
        metrics = compute_metrics(eq_series)

        all_results[name] = {
            "plot": _downsample_plot(results),
            "hourly_px": _hourly_close(df),
            "stats": stats,
            "paper": paper,
            "config": a,
            "metrics": metrics,
            "n_candles": n_candles,
            "fill_count": fill_count,
            "fill_rate": fill_rate,
            "final_equity": eq,
        }

        if save_csv:
            header = not Path(save_csv).exists()
            rows = [{"market": name, **vars(tick)} for tick in results]
            pd.DataFrame(rows).to_csv(save_csv, mode="a", header=header, index=False)
            del rows

        del results, eq_series
        downloaded.pop(bn_sym, None)

    # ── Conservative BTC-factor overlay (all-pairs only) ──────────────────────
    factor = {"count": 0, "final_pnl": 0.0}
    if factor_hedge:
        print("\nRunning BTC-factor overlay hedge (lagged 30d 1h beta, haircut 0.5)...")
        factor = simulate_btc_factor_hedge(all_results)
    else:
        print("\nBTC-factor overlay hedge disabled (--no-factor-hedge).")

    # ── Aggregate report ──────────────────────────────────────────────────────
    print_all_pairs_report(all_results, per_pair_max_inv * len(all_results), factor=factor)

    if save_csv:
        print(f"\nCombined CSV saved to: {save_csv}")

    # ── Individual charts ─────────────────────────────────────────────────────
    if not no_plot:
        plots_dir = out_dir / "plots"
        print(f"\nGenerating batched charts -> {plots_dir}/all_pairs_*.png ...")
        plot_all_pairs_batched(all_results, plots_dir, batch_size=10)
        print(f"Generating aggregate charts -> {plots_dir}/aggregate_*.png ...")
        plot_aggregate_pnl(all_results, plots_dir, factor=factor if factor_hedge else None)
        plot_aggregate_exposure(all_results, plots_dir, factor=factor if factor_hedge else None)
        print(f"Charts complete.")

    return all_results


def plot_all_pairs_batched(
    all_results: dict,
    out_dir: Path,
    batch_size: int = 10,
) -> None:
    """
    Generate batched PNG charts for all markets, sorted by total PnL descending.

    Each file contains up to `batch_size` markets arranged as a 2-column grid.
    Each market subplot shows:
      - Top: Cumulative equity curve (coloured green/red for above/below zero)
      - Bottom: Normalised inventory (norm_inv) with hedge threshold line
    Files are saved as: <out_dir>/all_pairs_1.png, all_pairs_2.png, ...
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        print("matplotlib not installed; skipping charts.")
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    # Sort markets by PnL descending
    sorted_markets = sorted(
        all_results.items(),
        key=lambda kv: kv[1].get("final_equity", 0.0),
        reverse=True,
    )

    batches = [sorted_markets[i:i+batch_size] for i in range(0, len(sorted_markets), batch_size)]

    for batch_idx, batch in enumerate(batches, 1):
        n_markets = len(batch)
        n_cols = 2
        n_rows = n_markets  # 2 subplots per market (equity + norm_inv), stacked
        fig_height = max(4, n_markets * 3.5)
        fig_width = 20

        # Layout: n_markets rows × 2 cols — left col = equity, right col = norm_inv
        fig, axes = plt.subplots(
            n_markets, 2,
            figsize=(fig_width, fig_height),
            squeeze=False,
        )
        fig.suptitle(
            f"All-Pairs Market-Making Backtest  |  Batch {batch_idx}/{len(batches)}  "
            f"(sorted by PnL descending)",
            fontsize=12, fontweight="bold",
        )

        for row_idx, (name, r) in enumerate(batch):
            plot    = r.get("plot") or {}
            a       = r["config"]
            times   = plot.get("times") or []
            equity  = plot.get("equity") or []
            norm_inv = plot.get("norm_inv") or []
            if not times:
                axes[row_idx, 0].set_visible(False)
                axes[row_idx, 1].set_visible(False)
                continue

            ht        = a.get("hedge_threshold", 0.9)
            total_pnl = r.get("final_equity", equity[-1] if equity else 0.0)
            fill_rate = r.get("fill_rate", 0.0)
            fill_count = r.get("fill_count", 0)
            ndays     = r.get("n_candles", 0) / (60 * 24)
            sharpe    = (r.get("metrics") or {}).get("sharpe_ratio", 0.0)

            # ── Equity subplot ────────────────────────────────────────────────
            ax_eq = axes[row_idx, 0]
            ax_eq.plot(times, equity, color="steelblue", linewidth=0.6)
            ax_eq.axhline(0, color="gray", linewidth=0.5, linestyle="--")
            ax_eq.fill_between(times, 0, equity,
                               where=[e >= 0 for e in equity], alpha=0.2, color="green")
            ax_eq.fill_between(times, 0, equity,
                               where=[e <  0 for e in equity], alpha=0.2, color="red")
            ax_eq.set_ylabel("PnL (USD)", fontsize=7)
            ax_eq.tick_params(labelsize=6)
            ax_eq.grid(True, alpha=0.3)
            ax_eq.set_title(
                f"{name}  |  PnL: ${total_pnl:+.1f}  Sharpe: {sharpe:.2f}  "
                f"Fill: {fill_rate:.1%} ({fill_count:,})  Days: {ndays:.0f}",
                fontsize=8, loc="left",
            )
            # X-axis labels only on bottom row
            if row_idx < n_markets - 1:
                ax_eq.set_xticklabels([])
            else:
                ax_eq.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
                ax_eq.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
                plt.setp(ax_eq.xaxis.get_majorticklabels(), rotation=25, fontsize=6)

            # ── Norm_inv subplot ──────────────────────────────────────────────
            ax_ni = axes[row_idx, 1]
            ax_ni.plot(times, norm_inv, color="darkorange", linewidth=0.5)
            ax_ni.axhline(0,   color="gray", linewidth=0.5, linestyle="--")
            ax_ni.axhline( ht, color="red",  linewidth=0.8, linestyle=":", alpha=0.8,
                           label=f"ht={ht:.2f}")
            ax_ni.axhline(-ht, color="red",  linewidth=0.8, linestyle=":", alpha=0.8)
            ax_ni.fill_between(times, 0, norm_inv,
                               where=[v >= 0 for v in norm_inv], alpha=0.15, color="orange")
            ax_ni.fill_between(times, 0, norm_inv,
                               where=[v <  0 for v in norm_inv], alpha=0.15, color="purple")
            ax_ni.set_ylim(-1.1, 1.1)
            ax_ni.set_ylabel("Norm inv", fontsize=7)
            ax_ni.tick_params(labelsize=6)
            ax_ni.grid(True, alpha=0.3)
            ax_ni.set_title(
                f"{name}  |  MaxInv: ${a['max_inv']:,.0f}  "
                f"Spread: {a['half_spread_bps']:.1f}bps  HgThr: {ht:.2f}",
                fontsize=8, loc="left",
            )
            ax_ni.legend(fontsize=6, loc="upper right")
            if row_idx < n_markets - 1:
                ax_ni.set_xticklabels([])
            else:
                ax_ni.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
                ax_ni.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
                plt.setp(ax_ni.xaxis.get_majorticklabels(), rotation=25, fontsize=6)

        plt.tight_layout(rect=[0, 0, 1, 0.97])
        out_path = out_dir / f"all_pairs_{batch_idx}.png"
        plt.savefig(str(out_path), dpi=130, bbox_inches="tight")
        print(f"  Chart saved: {out_path}")
        plt.close(fig)


def print_all_pairs_report(all_results: dict, total_capital: float,
                           factor: Optional[dict] = None) -> None:
    """Print aggregate summary table for all-pairs backtest."""
    if not all_results:
        print("No results to report.")
        return

    print()
    sep = "=" * 115
    print(sep)
    print(f"  ALL-PAIRS SUMMARY   (per-pair max inv × N = ${total_capital:,.0f} gross)")
    print(sep)
    hdr = (f"  {'Market':<20} {'Days':>5}  {'PnL':>9}  {'CAGR':>7}  {'Sharpe':>7}  "
           f"{'Edge':>9}  {'Fills':>7}  {'FillR':>6}  {'MaxInv':>7}  {'HgThr':>6}  {'Hedges':>6}")
    print(hdr)
    print(f"  {'-'*112}")

    total_pnl  = 0.0
    total_edge = 0.0
    winning = 0

    rows = []
    for name, r in all_results.items():
        paper   = r["paper"]
        a       = r["config"]
        met     = r.get("metrics") or {}
        n_candles = r.get("n_candles", 0)
        if n_candles <= 0:
            continue

        pnl   = met.get("total_pnl_usd", r.get("final_equity", 0.0))
        sh    = met.get("sharpe_ratio", 0)
        ndays = n_candles / (60 * 24)
        cagr  = (pnl / a["max_inv"] / (ndays / 365)) * 100 if ndays > 1 else 0.0
        fills = r.get("fill_count", 0)
        fr    = r.get("fill_rate", 0.0)
        edge  = paper.total_edge_collected
        hedges = paper.hedge_count
        ht    = a.get("hedge_threshold", 0.9)

        total_pnl  += pnl
        total_edge += edge
        if pnl > 0:
            winning += 1

        rows.append({
            "name": name, "ndays": ndays, "pnl": pnl, "cagr": cagr,
            "sharpe": sh, "edge": edge, "fills": fills, "fill_rate": fr,
            "max_inv": a["max_inv"], "hedge_threshold": ht, "hedges": hedges,
        })

    rows.sort(key=lambda x: -x["pnl"])

    for row in rows:
        star = "*" if row["pnl"] > 0 else " "
        print(f"  {row['name']:<20} {row['ndays']:>5.0f}  "
              f"${row['pnl']:>+8.2f}  {row['cagr']:>+6.1f}%  {row['sharpe']:>7.3f}  "
              f"${row['edge']:>+8.2f}  {row['fills']:>7,}  {row['fill_rate']:>5.1%}  "
              f"${row['max_inv']:>6,.0f}  {row['hedge_threshold']:>5.2f}  {row['hedges']:>6}{star}")

    print(f"  {'-'*112}")
    print(f"  {'TOTAL':<20}        ${total_pnl:>+8.2f}           ${total_edge:>+8.2f}"
          f"                                 ({winning}/{len(rows)} profitable)")
    if factor:
        fh = int(factor.get("count", 0) or 0)
        fp = float(factor.get("final_pnl", 0.0) or 0.0)
        print(f"  {'BTC factor hedge':<20}        ${fp:>+8.2f}           "
              f"overlay hedges={fh}")
        print(f"  {'TOTAL + overlay':<20}        ${total_pnl + fp:>+8.2f}")
    print(sep)
    print(f"\n  * = profitable market")
    print(f"  CAGR = annualised return on per-market max_inventory")
    print(f"  HgThr = hedge trigger threshold (fraction of max_inv before hedging)")
    print(f"  Fills = total passive fill count (stats.num_trades) over backtest period")
    print(f"  BTC factor hedge = conservative overlay (lagged 30d 1h beta, 50% haircut,")
    print(f"    fires if |net BTC-beta USD| > ${FACTOR_HEDGE_THRESHOLD_USD:,.0f}, flattens "
          f"{FACTOR_HEDGE_FRACTION:.0%} of excess on BTC only, {FACTOR_HEDGE_COOLDOWN_H}h cooldown).")
    print(f"  Note: Volume data from Binance USDT-M futures, scaled by Extended 24h vol/Binance 24h vol.")
    print(f"  Fill model: volume-per-tick + pro-rata queue (1/(1+N_competitors) share).")
    print(f"  Quote size: 2% of 1-minute Extended notional, clipped to [$50, $1,000].")
    print(f"  Spread: max(8bps, current Extended half-spread) per market, then vol-scaled.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Market-making backtest on Binance OHLCV data scaled to Extended Exchange."
    )
    # ── Market selection ──────────────────────────────────────────────────────
    parser.add_argument("--market", default="SOL-USD")
    parser.add_argument("--all-pairs", action="store_true",
        help="Backtest ALL Extended Exchange pairs that also exist on Binance futures.")
    parser.add_argument("--top-n", type=int, default=None,
        help="Limit to top N markets by Extended USD volume (--all-pairs only).")
    parser.add_argument("--min-usd-vol", type=float, default=5000,
        help="Min Extended USD volume/day to include a market (default 5000).")

    # ── Date range ────────────────────────────────────────────────────────────
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)

    # ── Config ────────────────────────────────────────────────────────────────
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--interval", default="1m",
        help="Candle interval (default 1m). Binance supports: 1m, 3m, 5m, 15m, 1h.")

    # ── Fill model parameters ─────────────────────────────────────────────────
    parser.add_argument("--realistic", action="store_true",
        help="Enable realistic fill model (volume scaling + queue model).")
    parser.add_argument("--volume-scale", type=float, default=None, metavar="S",
        help="Manual volume scale override (Extended vol / Binance vol). "
             "Default: auto-fetched from Extended API.")
    parser.add_argument("--n-competitors", type=int, default=None, metavar="N",
        help="Number of competing MMs in the queue (for queue model). "
             "Default: derived from market volume tier.")
    parser.add_argument("--participation-rate", type=float, default=None, metavar="P",
        help="Legacy: fraction of volume captured (overrides queue model). "
             "Queue model uses 1/(1+N_competitors) instead.")

    # ── Per-market overrides ──────────────────────────────────────────────────
    parser.add_argument("--max-inventory-usd", type=float, default=None, metavar="USD",
        help="Override max inventory per market (default: from config.yaml).")
    parser.add_argument("--total-capital", type=float, default=25000, metavar="USD",
        help="Total capital for --all-pairs allocation (default: 25000).")
    parser.add_argument("--spread-bps", type=float, default=None, metavar="BPS",
        help="Override half-spread in bps (e.g. 8.0 = 8 bps each side).")
    parser.add_argument("--quote-size", type=float, default=None, metavar="USD",
        help="Override quote size in USD.")

    # ── Output ────────────────────────────────────────────────────────────────
    parser.add_argument("--spot", action="store_true")
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument("--no-factor-hedge", action="store_true",
        help="Disable the conservative BTC-beta overlay hedge in --all-pairs mode.")
    parser.add_argument(
        "--save-csv", nargs="?", const="backtest_all_pairs_v2.csv", default="",
        help="Save tick-level results to CSV. If passed with no path, writes backtest_all_pairs_v2.csv.",
    )
    parser.add_argument("--min-fill-qty", type=float, default=0.0, metavar="Q")
    return parser.parse_args()


def main():
    args = parse_args()

    # ── Config path ───────────────────────────────────────────────────────────
    cfg_path = args.config
    if not Path(cfg_path).is_absolute():
        cfg_path = str(ROOT / cfg_path)

    # ── All-pairs mode ────────────────────────────────────────────────────────
    if args.all_pairs:
        save_csv = args.save_csv
        if save_csv == "":
            save_csv = "backtest_all_pairs_v2.csv"
        run_all_pairs_backtest(
            total_capital=args.total_capital,
            months=args.months,
            top_n=args.top_n,
            min_usd_vol=args.min_usd_vol,
            cfg_path=cfg_path,
            verbose=True,
            no_plot=args.no_plot,
            save_csv=save_csv,
            output_dir=str(ROOT),
            per_pair_max_inv=args.max_inventory_usd if args.max_inventory_usd else 10_000,
            factor_hedge=not args.no_factor_hedge,
        )
        return

    # ── Single-market mode ────────────────────────────────────────────────────
    now_utc = datetime.now(timezone.utc)
    end_dt  = (datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
               if args.end else now_utc)
    start_dt = (datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if args.start else end_dt - timedelta(days=args.months * 30))
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp() * 1000)

    # ── Volume scale ──────────────────────────────────────────────────────────
    if args.realistic or args.volume_scale:
        volume_scale = args.volume_scale if args.volume_scale else fetch_volume_scale(args.market)
    else:
        volume_scale = 1.0  # optimistic: no scaling

    # ── Queue / participation ─────────────────────────────────────────────────
    if args.n_competitors is not None:
        n_comp = args.n_competitors
    elif args.participation_rate is not None:
        # Convert legacy participation_rate to equivalent n_competitors
        p = float(args.participation_rate)
        n_comp = max(0, round(1/p - 1))
        print(f"  Note: --participation-rate {p} converted to N_competitors={n_comp}")
    elif args.realistic:
        # Auto from market info cache
        markets = load_extended_markets()
        mkt = next((m for m in markets if m["name"] == args.market), None)
        n_comp = n_competitors_for_market(mkt["usd_vol"]) if mkt else 4
    else:
        n_comp = 0   # optimistic: we fill 100% of vol at our tick level

    # ── Fetch Binance data ────────────────────────────────────────────────────
    bn_sym = to_binance_symbol(args.market)
    use_futures = not args.spot
    src = "Binance USDT-M Perp" if use_futures else "Binance Spot"
    print(f"\nBacktesting {args.market}  |  {src} ({bn_sym})")
    print(f"Period: {start_dt.date()} to {end_dt.date()}  |  Candle: {args.interval}")
    print(f"Fill model: vol_scale={volume_scale:.4f}  n_competitors={n_comp}  "
          f"spread={args.spread_bps or 'config'}bps")

    print(f"\nFetching {args.interval} klines...")
    try:
        df = fetch_klines(bn_sym, args.interval, start_ms, end_ms,
                          verbose=True, cache_dir="", use_futures=use_futures)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print(f"Downloaded {len(df):,} candles "
          f"({df['open_time'].iloc[0].date()} to {df['open_time'].iloc[-1].date()})")

    # Build volume profile
    profile = build_volume_profile(df)

    # ── Tick size from Extended market info ───────────────────────────────────
    tick_override = None
    markets = load_extended_markets()
    mkt_info = next((m for m in markets if m["name"] == args.market), None)
    if mkt_info:
        raw_tick = mkt_info.get("tick_size", 0)
        if raw_tick and float(raw_tick) > 1e-10:
            tick_override = float(raw_tick)

    # ── Run simulation ────────────────────────────────────────────────────────
    results, stats, paper = run_backtest(
        market=args.market,
        df=df,
        cfg_path=cfg_path,
        verbose=True,
        volume_scale=volume_scale,
        n_competitors=n_comp,
        volume_profile=profile,
        min_fill_qty=args.min_fill_qty,
        max_inventory_usd=args.max_inventory_usd,
        spread_bps=args.spread_bps,
        quote_size_usd=args.quote_size,
        tick_size_override=tick_override,
    )

    eff_spread = args.spread_bps or load_config(cfg_path).sim.quote_half_spread_bps
    print_report(args.market, results, stats, paper, start_dt, end_dt,
                 volume_scale=volume_scale, n_competitors=n_comp,
                 half_spread_bps=eff_spread)

    if args.save_csv:
        save_results(results, args.save_csv)

    if not args.no_plot:
        chart_path = f"backtest_{args.market.replace('-','_')}.png"
        print(f"\nGenerating chart...")
        plot_results(args.market, results, start_dt, end_dt,
                     output_path=chart_path,
                     volume_scale=volume_scale, n_competitors=n_comp,
                     half_spread_bps=eff_spread)


if __name__ == "__main__":
    main()
