"""
Fetch all Extended Exchange active crypto markets with full metadata.
Outputs a JSON file with: name, usd_vol, base_vol, spread_bps, tick_size, bn_sym, n_competitors
"""
import requests, json, math

# ── Fetch Extended markets ────────────────────────────────────────────────────
r_ext = requests.get("https://api.starknet.extended.exchange/api/v1/info/markets", timeout=20)
raw = r_ext.json()["data"]

# ── Fetch Binance futures symbols ─────────────────────────────────────────────
r_bn = requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=20)
bn_map = {s["symbol"]: s for s in r_bn.json()["symbols"] if s["status"] == "TRADING"}

# ── Fetch Binance 24h volume for each relevant symbol ─────────────────────────
r_tick = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=20)
bn_vol = {t["symbol"]: float(t["volume"]) for t in r_tick.json()}
bn_price = {t["symbol"]: float(t["lastPrice"]) for t in r_tick.json()}

markets = []
for m in raw:
    if m.get("category") != "Crypto":
        continue
    if m.get("status") != "ACTIVE":
        continue
    name = m["name"]
    # Skip k-prefixed synthetic tokens and _24_5 derivatives
    base = name.split("-")[0]
    if base.startswith("k") or "_" in name:
        continue
    # Skip spot synthetics
    if "SPOT" in name:
        continue

    stats = m.get("marketStats") or {}
    usd_vol  = float(stats.get("dailyVolume", 0) or 0)
    base_vol = float(stats.get("dailyVolumeBase", 0) or 0)
    bid_px   = float(stats.get("bidPrice", 0) or 0)
    ask_px   = float(stats.get("askPrice", 0) or 0)
    last_px  = float(stats.get("lastPrice", 0) or 0)

    # Spread
    spread_bps = 0.0
    if bid_px > 0 and ask_px > 0:
        mid = (bid_px + ask_px) / 2
        spread_bps = 10000 * (ask_px - bid_px) / mid
        last_px = mid

    # Tick size from tradingConfig
    tc = m.get("tradingConfig") or {}
    tick_size = float(tc.get("minPriceChange", 0.01) or 0.01)
    # Also get min order size
    min_order_size = float(tc.get("minOrderSize", 1) or 1)
    min_price_change_steps = float(tc.get("minOrderSizeChange", 1) or 1)

    # Binance symbol
    bn_sym = base + "USDT"
    on_binance = bn_sym in bn_map
    if not on_binance:
        continue

    # Binance volumes
    b_vol_base = bn_vol.get(bn_sym, 0.0)
    b_last     = bn_price.get(bn_sym, last_px or 1.0)

    # Volume scale
    vol_scale = (base_vol / b_vol_base) if b_vol_base > 0 and base_vol > 0 else 0.0

    markets.append({
        "name":        name,
        "base":        base,
        "usd_vol":     usd_vol,
        "base_vol":    base_vol,
        "bid":         bid_px,
        "ask":         ask_px,
        "last_px":     last_px or b_last,
        "spread_bps":  round(spread_bps, 2),
        "tick_size":   tick_size,
        "min_order_base": min_order_size,  # minimum order in base units (useful for quote sizing)
        "bn_sym":      bn_sym,
        "bn_vol_base": b_vol_base,
        "bn_last":     b_last,
        "vol_scale":   round(vol_scale, 6),
    })

# Sort by USD volume
markets.sort(key=lambda x: -x["usd_vol"])

# Save to JSON
out_path = "scripts/extended_markets.json"
with open(out_path, "w") as f:
    json.dump(markets, f, indent=2)

# Print summary
print(f"Active crypto markets on both Extended + Binance futures: {len(markets)}")
print(f"Saved to {out_path}\n")
print(f"{'Market':<20} {'Ext Vol USD':>14}  {'Spread':>8}  {'Tick':>10}  {'Vol Scale':>10}  {'Bn Last':>10}")
print("-" * 85)
for m in markets:
    if m["usd_vol"] < 1000:
        continue  # skip zero-vol markets in display
    print(f"{m['name']:<20} {m['usd_vol']:>14,.0f}  {m['spread_bps']:>7.1f}bps  {m['tick_size']:>10.5f}  {m['vol_scale']:>9.4%}  {m['bn_last']:>10.4f}")
