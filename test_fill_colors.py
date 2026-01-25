"""
Test to verify fill logger coloring and formatting.
Run with: python test_fill_colors.py
"""
import logging
from src.sim.paper_mm import ExecutionTape
from src.core.logger import setup_fill_logger


def test_fill_colors():
    """Test fill logger with colored output"""
    print("Testing Fill Logger with Colors\n")
    print("=" * 80)
    print("Note: This will create 'test_fills.log' and print colored output below.\n")
    
    # Setup fill logger
    fill_logger = setup_fill_logger(enabled=True, log_file="test_fills.log")
    
    # Create execution tape
    tape = ExecutionTape(max_history=10, fill_logger=fill_logger)
    
    # Simulate various fills
    print("Simulated fills (colored):")
    print("-" * 80)
    
    # Fill 1: BUY (odd trade_id -> blue row)
    tape.record_fill(
        tick=1,
        market="BTC-USD",
        side="BUY",
        size=0.0100,
        price=50000.00,
        notional=500.00,
        avg_price_after=50000.00,
        pos_after=0.0100,
        cash_after=-500.00,
        pnl_after=0.00,
        realized_pnl_trade=0.00,
        realized_pnl_total=0.00,
    )
    
    # Fill 2: BUY (even trade_id -> yellow row)
    tape.record_fill(
        tick=2,
        market="ETH-USD",
        side="BUY",
        size=0.1500,
        price=3000.00,
        notional=450.00,
        avg_price_after=3000.00,
        pos_after=0.1500,
        cash_after=-450.00,
        pnl_after=0.00,
        realized_pnl_trade=0.00,
        realized_pnl_total=0.00,
    )
    
    # Fill 3: SELL (odd trade_id -> blue row)
    tape.record_fill(
        tick=3,
        market="BTC-USD",
        side="SELL",
        size=0.0050,
        price=50500.00,
        notional=252.50,
        avg_price_after=50000.00,
        pos_after=0.0050,
        cash_after=-247.50,
        pnl_after=2.50,
        realized_pnl_trade=2.50,
        realized_pnl_total=2.50,
    )
    
    # Fill 4: SELL (even trade_id -> yellow row)
    tape.record_fill(
        tick=4,
        market="SOL-USD",
        side="SELL",
        size=2.5000,
        price=125.50,
        notional=313.75,
        avg_price_after=124.00,
        pos_after=-1.0000,
        cash_after=66.25,
        pnl_after=70.00,
        realized_pnl_trade=3.75,
        realized_pnl_total=6.25,
    )
    
    # Fill 5: BUY (odd trade_id -> blue row)
    tape.record_fill(
        tick=5,
        market="ETH-USD",
        side="BUY",
        size=0.2000,
        price=2950.00,
        notional=590.00,
        avg_price_after=2970.00,
        pos_after=0.3500,
        cash_after=-1040.00,
        pnl_after=-17.50,
        realized_pnl_trade=0.00,
        realized_pnl_total=6.25,
    )
    
    print("\n" + "=" * 80)
    print(f"✓ Created {tape.trade_counter} fills")
    print(f"\nCheck 'test_fills.log' for plain text (no colors)")
    print("\nColor legend:")
    print("  - Odd trades (#00001, #00003, #00005): BLUE rows")
    print("  - Even trades (#00002, #00004): YELLOW rows")
    print("  - BUY: always GREEN (overrides row color)")
    print("  - SELL: always RED (overrides row color)")
    
    # Show file contents
    print("\n" + "=" * 80)
    print("File contents (test_fills.log - no colors):")
    print("-" * 80)
    with open("test_fills.log", "r") as f:
        print(f.read())


if __name__ == "__main__":
    test_fill_colors()
