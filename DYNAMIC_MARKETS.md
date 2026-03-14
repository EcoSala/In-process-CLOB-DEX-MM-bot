# Dynamic Market Discovery - Extended Exchange

## Summary

Implemented dynamic market discovery for Extended Exchange, allowing the bot to automatically subscribe to **all available active markets** instead of using a hardcoded list.

## Features

### 1. Two Operating Modes

**Static Mode** (default for existing configs):
- Uses hardcoded market list from `extended.markets` config
- No REST API calls needed
- Suitable for focused trading on specific markets

**All Mode** (new):
- Automatically discovers all active markets from Extended API
- Fetches market list on bot startup
- Filters for `ACTIVE` status only
- Falls back to config markets if API call fails

### 2. REST API Integration

**Endpoint Used:**
```
GET https://api.starknet.extended.exchange/api/v1/info/markets
```

**Response Structure:**
```json
{
  "status": "OK",
  "data": [
    {
      "name": "BTC-USD",
      "active": true,
      "status": "ACTIVE",
      "assetName": "BTC",
      "tradingConfig": {
        "minOrderSize": "0.001",
        "maxLeverage": "50",
        ...
      }
    }
  ]
}
```

**Filtering Logic:**
- Only markets with `status == "ACTIVE"` are included
- Only markets with `active == true` are included
- Excludes: `REDUCE_ONLY`, `DELISTED`, `PRELISTED`, `DISABLED`

### 3. WebSocket Subscription Scaling

**Current Implementation:**
- Creates one `ExtendedPublicWS` + one `ExtendedTradesWS` per market
- Each market gets its own independent WS connection
- No cross-market interference

**Performance Considerations:**
- **Memory**: Each WS client uses minimal memory (order book + trade tape)
- **CPU**: WebSocket messages are async, non-blocking
- **Network**: Extended's WS infrastructure handles many connections per client
- **Rate Limits**: Public WS streams have no documented rate limits

**Tested Scale:**
- **Current**: 3 markets (BTC, ETH, SOL)
- **Expected with "all" mode**: ~20-50 markets (based on typical perpetual exchanges)
- **Theoretical maximum**: Limited by Extended's server capacity, not client

### 4. Market Selector Integration

The market selector (`select_markets`) already supports large market sets:

**Current Flow:**
1. Build `MarketSnapshot` for each market (spread, TPM, mid)
2. Filter by `min_spread_bps` and `min_tpm`
3. Sort by spread (tightest first)
4. Take top N markets (`top_n`)

**No changes needed** - the selector naturally handles 3 markets or 300 markets.

**Performance:**
- Snapshot building: O(n) where n = number of markets
- Filtering: O(n)
- Sorting: O(n log n)
- Total per tick: ~microseconds even for 100+ markets

## Implementation Details

### Modified Files

#### 1. `src/venues/extended_rest.py` (NEW)

**Added `ExtendedRESTClient` class:**
- `get_markets()`: Fetches all markets from Extended API
- `get_active_spot_markets()`: Filters for active tradeable markets
- Async aiohttp-based HTTP client
- 10-second timeout per request
- Handles 429 rate limits gracefully
- Auto-retry not implemented (fails fast to avoid blocking startup)

**Error Handling:**
- HTTP errors → returns empty list, logs error
- 429 rate limit → logs warning, returns empty list
- Network errors → logs error, returns empty list
- Fallback: config markets are used if API fails

#### 2. `src/core/config.py`

**Updated `ExtendedMarketsConfig`:**
```python
class ExtendedMarketsConfig(BaseModel):
    markets_mode: str = "static"  # "static" or "all"
    markets: list[str] = []  # Only used if markets_mode == "static"
    api_base_url: str = "https://api.starknet.extended.exchange"
    selector: MarketSelectorConfig
    pinned_market: str | None = None
```

**New Fields:**
- `markets_mode`: Operating mode selector
- `api_base_url`: REST API base URL for market discovery
- `markets`: Now optional, used only in static mode

#### 3. `src/core/app.py`

**Modified `__init__`:**
- Initialize `rest_client` if `markets_mode == "all"`
- Defer `ext_multi` initialization to `run()` (was previously in `__init__`)

**Added `_initialize_markets()` method:**
- Async method called during bot startup
- Routes to static or dynamic mode based on config
- Returns list of market symbols to subscribe to
- Logs detailed market discovery info

**Modified `run()` method:**
- Calls `_initialize_markets()` before starting WS connections
- Creates `ExtendedMulti` with discovered markets
- Starts all WS connections
- Logs discovered market count

**Modified cleanup:**
- Added `rest_client.close()` in finally block

#### 4. `example_config.yaml`

**Updated extended config:**
```yaml
extended:
  markets_mode: "all"  # "static" or "all"
  markets: ["BTC-USD", "ETH-USD", "SOL-USD"]  # Only used if markets_mode == "static"
  api_base_url: "https://api.starknet.extended.exchange"
  selector:
    min_spread_bps: 0.3
    min_tpm: 5
    top_n: 1
  pinned_market: "SOL-USD"
```

**To use static mode:**
```yaml
extended:
  markets_mode: "static"
  markets: ["BTC-USD", "ETH-USD"]  # Only these two
```

**To use all mode:**
```yaml
extended:
  markets_mode: "all"
  markets: []  # Ignored, can be empty
```

## Rate Limits & Considerations

### Extended Exchange API Rate Limits

**Public REST Endpoints:**
- **Standard**: 1,000 requests/minute (shared across all REST endpoints)
- **Market Makers**: 60,000 requests/5 minutes

**Impact:**
- Market discovery: 1 call per bot startup
- No ongoing REST calls (only WS after startup)
- Well within rate limits even for frequent restarts

**WebSocket Streams:**
- No documented rate limits for public streams
- One orderbook stream + one trades stream per market
- Expected to handle dozens of markets per client

### Performance Benchmarks (Estimated)

**Startup Time:**
- Static mode (3 markets): ~1-2 seconds
- All mode (50 markets): ~2-3 seconds (HTTP call adds ~500ms)

**Per-Tick Processing:**
- 3 markets: ~1-2ms per tick
- 50 markets: ~5-10ms per tick (mostly snapshot building)
- 100 markets: ~10-20ms per tick

**Memory Usage:**
- Per market: ~100KB (order book + trade tape)
- 50 markets: ~5MB incremental
- Negligible compared to bot baseline (~50MB)

## Usage

### Switching Between Modes

**To enable dynamic discovery:**

1. Edit `example_config.yaml`:
```yaml
extended:
  markets_mode: "all"
```

2. Run the bot:
```powershell
python run.py
```

3. Check startup logs:
```
🔍 DYNAMIC MODE: Discovering all active markets from Extended API...
✅ Discovered 47 active markets
   Markets: BTC-USD, ETH-USD, SOL-USD, AVAX-USD, ...
🌐 Subscribing to 47 market feeds...
```

**To switch back to static mode:**

1. Edit `example_config.yaml`:
```yaml
extended:
  markets_mode: "static"
  markets: ["BTC-USD", "SOL-USD"]
```

2. Run the bot:
```powershell
python run.py
```

3. Check startup logs:
```
📋 STATIC MODE: Using 2 configured markets
🌐 Subscribing to 2 market feeds...
```

## Acceptance Tests

### Test 1: Static Mode (Existing Behavior)

**Config:**
```yaml
extended:
  markets_mode: "static"
  markets: ["BTC-USD", "ETH-USD"]
```

**Expected:**
- Bot starts normally
- Logs show: `📋 STATIC MODE: Using 2 configured markets`
- Only BTC-USD and ETH-USD feeds are created
- No REST API calls

**Pass Criteria:**
✅ Bot behavior unchanged from before  
✅ No HTTP errors  
✅ Market selector works normally  

### Test 2: Dynamic Discovery (All Mode)

**Config:**
```yaml
extended:
  markets_mode: "all"
```

**Expected:**
- Bot starts with REST call to Extended API
- Logs show discovered market count
- WS subscriptions created for all active markets
- Market selector filters to top_n

**Pass Criteria:**
✅ Bot logs discovered markets  
✅ Market selector picks from full universe  
✅ No crashes or errors  

### Test 3: API Failure Fallback

**Scenario:** Extended API is unreachable

**Expected:**
- HTTP error logged
- Fallback to `extended.markets` config
- Bot continues with static market list

**Pass Criteria:**
✅ Bot does not crash  
✅ Error is logged  
✅ Fallback markets are used  

### Test 4: Pinned Market Mode

**Config:**
```yaml
extended:
  markets_mode: "all"
  pinned_market: "SOL-USD"
```

**Expected:**
- All markets discovered
- Only SOL-USD is traded (pinned_market filter active)

**Pass Criteria:**
✅ Discovers all markets  
✅ Only trades SOL-USD  
✅ Pinned market mode log appears  

## Future Enhancements

### Short-Term
- Add retry logic for market discovery (exponential backoff)
- Cache discovered markets to disk (avoid REST call on every restart)
- Add metrics: market count, discovery latency

### Medium-Term
- Periodic market list refresh (every N minutes)
- Detect new markets added during bot runtime
- Support market-specific config overrides (per-market max_inventory, etc.)

### Long-Term
- Multi-exchange support (Hyperliquid, dYdX, etc. with same dynamic discovery pattern)
- Market health monitoring (auto-disable markets with stale data)
- Smart market selection using volume, liquidity, volatility

## Troubleshooting

### Issue: No markets discovered

**Symptoms:**
```
No markets discovered, falling back to config markets
```

**Causes:**
- Network connectivity issues
- Extended API temporarily down
- Rate limit hit (429)

**Solution:**
- Check network connection
- Check Extended API status
- Switch to `markets_mode: "static"` temporarily

### Issue: Bot slow to start

**Symptoms:**
- Startup takes >5 seconds

**Causes:**
- HTTP timeout waiting for market discovery

**Solution:**
- Reduce HTTP timeout in `extended_rest.py` (currently 10s)
- Use static mode if low latency critical

### Issue: Too many markets, selector slow

**Symptoms:**
- High CPU usage during heartbeat

**Causes:**
- Selector building snapshots for 100+ markets per tick

**Solution:**
- Use pinned_market to force single market
- Increase tick_seconds (e.g. 2.0 instead of 1.0)
- Pre-filter markets by volume/liquidity before passing to selector

## Configuration Reference

### `extended.markets_mode`

**Type:** `string`  
**Values:** `"static"` | `"all"`  
**Default:** `"static"`  
**Description:** Operating mode for market discovery

### `extended.markets`

**Type:** `list[string]`  
**Default:** `[]`  
**Description:** Hardcoded market list (used only in static mode)

### `extended.api_base_url`

**Type:** `string`  
**Default:** `"https://api.starknet.extended.exchange"`  
**Description:** Extended REST API base URL for market discovery

## API Documentation Reference

**Endpoint:**
```
GET /api/v1/info/markets
```

**Documentation:**
https://api.docs.extended.exchange/#get-markets

**Response Fields:**
- `name`: Market symbol (e.g. "BTC-USD")
- `active`: Boolean, market is active
- `status`: "ACTIVE" | "REDUCE_ONLY" | "DELISTED" | "PRELISTED" | "DISABLED"
- `assetName`: Base asset (e.g. "BTC")
- `collateralAssetName`: Quote asset (e.g. "USD")
- `tradingConfig`: Min/max sizes, leverage, etc.

## Notes

- **Paper mode only**: This feature is read-only, no real orders are placed
- **WS scaling**: Extended's infrastructure can handle many concurrent WS connections
- **No authentication**: Market discovery uses public API, no API key needed
- **Startup latency**: Dynamic mode adds ~500ms HTTP call to startup
- **Fallback safety**: API failure does not crash bot, falls back to config markets
