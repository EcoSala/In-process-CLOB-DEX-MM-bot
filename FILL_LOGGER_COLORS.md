# Fill Logger Color Enhancement

## Summary

Enhanced the execution tape logger with alternating row colors and BUY/SELL highlighting for improved readability in the PowerShell monitor window.

## Features Implemented

### 1. Trade Numbering
- Each fill now has a monotonically increasing `trade_id` starting from 1
- Trade IDs are displayed as `#00001`, `#00002`, etc. (5-digit format)
- Trade counter persists within the `ExecutionTape` instance

### 2. Alternating Row Colors
- **Odd trades** (#00001, #00003, #00005, ...): **BLUE** rows
- **Even trades** (#00002, #00004, #00006, ...): **YELLOW** rows
- Row color is determined by `trade_id % 2`

### 3. BUY/SELL Color Highlighting
- **BUY**: Always displayed in **GREEN** (overrides row color)
- **SELL**: Always displayed in **RED** (overrides row color)
- Only the BUY/SELL text is colored independently; rest of the row uses the alternating color

### 4. ANSI Color Codes
Uses Windows PowerShell-compatible ANSI codes:
- `\033[94m` - Bright blue (odd trades)
- `\033[93m` - Bright yellow (even trades)
- `\033[92m` - Bright green (BUY)
- `\033[91m` - Bright red (SELL)
- `\033[0m` - Reset (prevents color bleeding)

### 5. File vs Console Output
- **fills.log**: Plain text without ANSI codes (easy to parse/analyze)
- **Console/PowerShell window**: N/A (tails the plain file)
- Uses `StripAnsiFormatter` to automatically remove ANSI codes when writing to file

## Implementation Details

### Modified Files

#### 1. `src/sim/paper_mm.py`
- Added `trade_id` field to `Fill` dataclass
- Added `trade_counter` to `ExecutionTape` class
- Updated `record_fill()` to increment and assign trade IDs
- Enhanced `_print_fill()` with:
  - ANSI color code definitions
  - Row color logic based on `trade_id % 2`
  - Side color override for BUY/SELL
  - Proper color reset to prevent bleeding

#### 2. `src/core/logger.py`
- Created `StripAnsiFormatter` class to strip ANSI codes for file output
- Updated `setup_fill_logger()` to use the custom formatter
- File handler automatically strips colors using regex pattern

### Example Output Format

```
#00001 | 16:07:53.964 | tick=1   | BTC-USD | BUY  | 0.0100 @50000.00 | $500.00 | avg=50000.00 | pos=+0.0100 | rPnL=+0.00 | rPnLtot=+0.00
#00002 | 16:07:53.967 | tick=2   | ETH-USD | BUY  | 0.1500 @3000.00 | $450.00 | avg=3000.00 | pos=+0.1500 | rPnL=+0.00 | rPnLtot=+0.00
#00003 | 16:07:53.969 | tick=3   | BTC-USD | SELL | 0.0050 @50500.00 | $252.50 | avg=50500.00 | pos=+0.0050 | rPnL=+2.50 | rPnLtot=+2.50
```

**Visual representation:**
- Line #00001: 🟦 BLUE row with 🟩 GREEN "BUY"
- Line #00002: 🟨 YELLOW row with 🟩 GREEN "BUY"
- Line #00003: 🟦 BLUE row with 🟥 RED "SELL"

## Testing

Created test files to verify implementation:
- `test_fill_format.py`: Demonstrates ANSI color output
- Run with: `python test_fill_format.py`

## Notes

### PowerShell Compatibility
- Modern Windows PowerShell and Windows Terminal support ANSI color codes natively
- Legacy CMD.exe may not render colors (will show escape codes instead)
- Recommend using Windows Terminal or PowerShell 7+ for best experience

### Color Configuration
Colors are hardcoded in `_print_fill()` method. To change colors:
1. Modify the ANSI escape codes in `src/sim/paper_mm.py` lines 117-120
2. ANSI color code reference: https://en.wikipedia.org/wiki/ANSI_escape_code#Colors

### Performance
- Minimal performance impact: only string formatting overhead
- Colors are only applied during logging, not during fill calculations
- No impact on trading logic or PnL calculations

## Constraints Met

✅ Alternating row colors based on trade_id  
✅ BUY/SELL independent coloring  
✅ Trade numbering with monotonic IDs  
✅ Windows PowerShell-compatible ANSI codes  
✅ Proper color reset (no bleeding)  
✅ Colors stripped from file output  
✅ No changes to trading logic or PnL math  
✅ Maintains current field order and content  

## Future Enhancements

Potential improvements for later:
1. Make colors configurable via config.yaml
2. Add color themes (e.g., dark mode, light mode)
3. Support for profit/loss coloring (green for positive, red for negative rPnL)
4. Optional gradient colors for large vs small fills
