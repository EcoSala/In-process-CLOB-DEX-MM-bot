"""
Simple test to verify realized PnL accounting logic.
Run with: python test_realized_pnl.py
"""
from src.sim.paper_mm import PaperMM, Quote


def test_realized_pnl():
    """Test various scenarios for realized PnL calculation"""
    print("Testing Realized PnL Accounting\n")
    print("=" * 60)
    
    # Create a paper MM instance (without tape/stats for simplicity)
    mm = PaperMM(
        quote_half_spread_bps=10.0,
        quote_size_usd=1000.0,
        max_inventory_usd=50000.0,
    )
    
    # Simulate fills manually
    def simulate_fill(description: str, our_side: str, qty: float, px: float):
        """Simulate a fill and print the results"""
        old_pos = mm.state.pos_base
        old_avg = mm.avg_price
        old_rpnl = mm.realized_pnl_total
        
        # Calculate what would happen
        fill_qty_signed = qty if our_side == "BUY" else -qty
        rpnl_trade, new_avg = mm._calculate_realized_pnl(old_pos, fill_qty_signed, px)
        
        # Apply the fill
        if our_side == "BUY":
            mm.state.pos_base += qty
            mm.state.cash_usd -= qty * px
        else:
            mm.state.pos_base -= qty
            mm.state.cash_usd += qty * px
        
        mm.avg_price = new_avg
        mm.realized_pnl_total += rpnl_trade
        
        print(f"\n{description}")
        print(f"  {our_side} {qty:.4f} @ ${px:.2f}")
        print(f"  Position: {old_pos:+.4f} -> {mm.state.pos_base:+.4f}")
        print(f"  Avg Price: ${old_avg:.2f} -> ${mm.avg_price:.2f}")
        print(f"  Realized PnL (trade): ${rpnl_trade:+.2f}")
        print(f"  Realized PnL (total): ${old_rpnl:+.2f} -> ${mm.realized_pnl_total:+.2f}")
        print(f"  Cash: ${mm.state.cash_usd:+.2f}")
    
    # Test Case 1: Open long position
    simulate_fill("Test 1: Open long position", "BUY", 1.0, 100.0)
    assert mm.state.pos_base == 1.0
    assert mm.avg_price == 100.0
    assert mm.realized_pnl_total == 0.0
    
    # Test Case 2: Add to long position (VWAP)
    simulate_fill("Test 2: Add to long position", "BUY", 1.0, 110.0)
    assert mm.state.pos_base == 2.0
    assert mm.avg_price == 105.0  # (100 + 110) / 2
    assert mm.realized_pnl_total == 0.0
    
    # Test Case 3: Partially close long position (realize profit)
    simulate_fill("Test 3: Partially close long", "SELL", 1.0, 120.0)
    assert mm.state.pos_base == 1.0
    assert mm.avg_price == 105.0  # Stays the same when reducing
    assert mm.realized_pnl_total == 15.0  # (120 - 105) * 1
    
    # Test Case 4: Close remaining long (realize profit)
    simulate_fill("Test 4: Close remaining long", "SELL", 1.0, 130.0)
    assert abs(mm.state.pos_base) < 1e-8  # Should be ~0
    assert mm.avg_price == 0.0
    assert mm.realized_pnl_total == 40.0  # 15 + (130 - 105) * 1
    
    # Test Case 5: Open short position
    simulate_fill("Test 5: Open short position", "SELL", 2.0, 125.0)
    assert mm.state.pos_base == -2.0
    assert mm.avg_price == 125.0
    assert mm.realized_pnl_total == 40.0  # Unchanged
    
    # Test Case 6: Add to short position
    simulate_fill("Test 6: Add to short position", "SELL", 1.0, 120.0)
    assert mm.state.pos_base == -3.0
    assert abs(mm.avg_price - 123.33) < 0.01  # (125*2 + 120*1) / 3
    assert mm.realized_pnl_total == 40.0  # Unchanged
    
    # Test Case 7: Partially close short (realize profit)
    simulate_fill("Test 7: Partially close short", "BUY", 1.0, 115.0)
    assert mm.state.pos_base == -2.0
    assert abs(mm.avg_price - 123.33) < 0.01  # Unchanged when reducing
    expected_rpnl = 40.0 + (123.33 - 115.0) * 1  # ~48.33
    assert abs(mm.realized_pnl_total - expected_rpnl) < 0.01
    
    # Test Case 8: Flip from short to long
    simulate_fill("Test 8: Flip from short to long", "BUY", 4.0, 110.0)
    assert mm.state.pos_base == 2.0  # -2 + 4 = +2
    assert mm.avg_price == 110.0  # New position at fill price
    # Realized: close 2 short @ 123.33, buy @ 110 = (123.33 - 110) * 2 = ~26.66
    expected_rpnl = 48.33 + (123.33 - 110.0) * 2
    assert abs(mm.realized_pnl_total - expected_rpnl) < 0.01
    
    print("\n" + "=" * 60)
    print("[PASS] All tests passed!")
    print(f"\nFinal state:")
    print(f"  Position: {mm.state.pos_base:+.4f}")
    print(f"  Avg Price: ${mm.avg_price:.2f}")
    print(f"  Realized PnL: ${mm.realized_pnl_total:+.2f}")
    print(f"  Cash: ${mm.state.cash_usd:+.2f}")


if __name__ == "__main__":
    test_realized_pnl()
