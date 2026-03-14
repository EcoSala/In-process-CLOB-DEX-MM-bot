# Active Orders Ladder - Live Terminal Window

## Summary

Implemented a third live terminal window that displays a ladder-style view of ONLY the bot's active orders (not the full orderbook). Shows only price levels where active orders exist.

## Features

### 1. Order Tracking
- Each quote generates tracked orders (bid + ask)
- Tracks: order_id, market, side, price, orig_qty, filled_qty, timestamps
- Orders are updated on partial fills
- Orders are removed when fully filled or 99.5%+ filled

### 2. Ladder Display
Shows active orders aggregated by price level:
```
================================================================================
                    ACTIVE ORDERS LADDER - SOL-USD
================================================================================

BIDS                          PRICE                          ASKS
    filled      resting                                      resting   filled
--------------------------------------------------------------------------------
                                          125.90                0.2500    0.0000
                                          125.85                0.2500    0.0000
--------------------------------------------------------------------------------
     0.0000       0.2500              125.75
     0.1200       0.1300              125.70

Active: 4 orders | Recently filled: 0 | 16:50:23.142
================================================================================
```

### 3. Recently Filled Cache (Optional)
- Completed orders stay visible for 2 seconds after filling
- Prevents "blinking" when orders are filled and immediately replaced

### 4. PowerShell Window
- Automatically opens on bot startup
- Refreshes every 250ms (non-scrolling, like `top` or `watch`)
- Shows only the current snapshot (file is overwritten, not appended)

## Implementation Details

### Modified Files

#### 1. `src/sim/paper_mm.py`

**Added classes:**
- `ActiveOrder` dataclass: Represents a single tracked order with properties for `remaining_qty`, `fill_pct`, `is_complete`
- `OrderLadder` class: Tracks active orders, aggregates by price level, renders ladder snapshot

**Modified `PaperMM` class:**
- Added `order_ladder` parameter to `__init__`
- Added `active_bid_order` and `active_ask_order` tracking
- Modified `make_quote()` to:
  - Cancel old orders for the market
  - Create new bid/ask orders in the ladder
  - Track order IDs
- Modified `on_trade()` to update order fills:
  - When bid fills: `order_ladder.update_fill(active_bid_order, fill_qty)`
  - When ask fills: `order_ladder.update_fill(active_ask_order, fill_qty)`

#### 2. `src/core/logger.py`

**Added function:**
- `spawn_order_ladder_window()`: Spawns PowerShell window that:
  - Clears screen every 250ms
  - Reads `orders_ladder.log`
  - Displays non-scrolling snapshot (like `watch`/`top`)

#### 3. `src/core/app.py`

**Modified `__init__`:**
- Created `OrderLadder` instance
- Set `_spawn_ladder_window = True`

**Modified `heartbeat_loop`:**
- After `make_quote()`, call `order_ladder.render_snapshot(market)` to update the display

**Modified `run()`:**
- Spawn order ladder window on startup (if enabled)

## Technical Details

### Order Lifecycle

1. **Creation**: When `make_quote()` is called
   - Cancels old orders for current market
   - Creates new bid + ask orders
   - Returns order IDs

2. **Update**: When `on_trade()` processes a fill
   - Updates `filled_qty` for the matched order
   - Marks order as complete if `remaining_qty <= 1e-6` or `fill_pct >= 99.5%`

3. **Removal**: 
   - Immediate: When complete (moved to recently_filled cache)
   - Delayed: After 2 seconds in recently_filled cache

### Aggregation Logic

Orders are aggregated by price level per market:
- **Bids**: `{price: (total_filled, total_remaining)}`  sorted descending
- **Asks**: `{price: (total_remaining, total_filled)}` sorted ascending

Shows up to 15 levels per side (configurable).

### Refresh Cadence

- Rate limited to 250ms between renders (4 updates/second)
- Triggered every tick after `make_quote()` (but rate limit prevents spam)
- File is overwritten each time (snapshot mode, not append)

### PowerShell Display

The window runs:
```powershell
while ($true) {
    Clear-Host
    Get-Content 'orders_ladder.log'
    Start-Sleep -Milliseconds 250
}
```

This creates a non-scrolling "panel" that refreshes like `top` or `watch`.

## Constraints Met

✅ Only shows price levels where bot has active orders (no empty levels)  
✅ Tracks order state (filled + remaining qty)  
✅ Removes completed orders  
✅ Recently filled cache (2 second TTL)  
✅ Snapshot mode (overwrites file, non-scrolling display)  
✅ 250ms refresh rate  
✅ Automatic PowerShell window spawn  
✅ No changes to trading/quoting logic  
✅ Minimal, readable code  

## Usage

Run the bot as normal:
```powershell
cd $HOME\Desktop\mm_bot
.\venv\Scripts\Activate.ps1
python run.py
```

**Three windows will open:**
1. **Main terminal**: Bot logs (SELECTED, PAPER, STATS, etc.)
2. **Fills window**: Live trade execution tape (scrolling)
3. **Orders ladder window**: Active orders snapshot (non-scrolling, refreshing)

## Configuration

No config changes needed. The feature is always enabled.

To disable the ladder window, set `_spawn_ladder_window = False` in `src/core/app.py` line 39.

## Future Enhancements

- Add color coding (filled vs resting)
- Show spread and mid price reference line
- Display total notional at risk per side
- Per-market tabs if trading multiple markets
