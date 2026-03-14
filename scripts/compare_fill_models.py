"""
Quick comparison script: Deterministic vs. Probabilistic fill models

Run this to see the expected difference in fill rates, PnL, and trade frequency.
"""

import sys
import math
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sim.probabilistic_fill import ProbabilisticFillModel, FillModelParams


def visualize_distance_decay():
    """Show how fill probability decays with distance from touch."""
    model = ProbabilisticFillModel()
    
    print("=" * 80)
    print("DISTANCE DECAY (normal flow, normal vol)")
    print("=" * 80)
    print(f"{'Distance (ticks)':<20} {'Fill Probability':<20} {'Decay Factor':<20}")
    print("-" * 80)
    
    for distance_ticks in [0, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0]:
        distance_decay = math.exp(-1.5 * distance_ticks)
        
        # Base probability (assuming normal conditions)
        base_prob = 0.20 * distance_decay * 1.0 * 1.0 * 0.5  # base * decay * flow * vol * queue
        
        print(f"{distance_ticks:<20.1f} {base_prob:<20.1%} {distance_decay:<20.2f}")
    
    print()


def visualize_volatility_impact():
    """Show how volatility reduces fill probability."""
    model = ProbabilisticFillModel()
    
    print("=" * 80)
    print("VOLATILITY PENALTY (at touch, normal flow)")
    print("=" * 80)
    print(f"{'Vol Z-Score':<20} {'Vol Penalty':<20} {'Fill Probability':<20}")
    print("-" * 80)
    
    for vol_z in [-1, 0, 0.5, 1.0, 1.5, 2.0, 3.0]:
        vol_penalty = 1.0 / (1.0 + 0.8 * max(0, vol_z))
        
        # At touch, normal flow
        base_prob = 0.20 * 1.0 * 1.0 * vol_penalty * 0.5
        
        print(f"{vol_z:<20.1f} {vol_penalty:<20.2f} {base_prob:<20.1%}")
    
    print()


def visualize_flow_impact():
    """Show how flow intensity boosts fill probability."""
    model = ProbabilisticFillModel()
    
    print("=" * 80)
    print("FLOW INTENSITY (at touch, normal vol)")
    print("=" * 80)
    print(f"{'Flow Factor':<20} {'Fill Probability':<20}")
    print("-" * 80)
    
    for flow_factor in [0.1, 0.5, 1.0, 1.5, 2.0]:
        # Cap at 1.5
        flow_capped = min(1.5, flow_factor)
        
        # At touch, normal vol
        base_prob = 0.20 * 1.0 * flow_capped * 1.0 * 0.5
        
        print(f"{flow_factor:<20.1f} {base_prob:<20.1%}")
    
    print()


def simulate_fill_rates():
    """Simulate fill rates for various scenarios."""
    import random
    
    model = ProbabilisticFillModel()
    
    scenarios = [
        {
            "name": "Best case (at touch, high flow, low vol)",
            "distance_ticks": 0,
            "flow_factor": 1.5,
            "vol_z_score": -0.5,
        },
        {
            "name": "Normal case (at touch, normal flow, normal vol)",
            "distance_ticks": 0,
            "flow_factor": 1.0,
            "vol_z_score": 0.0,
        },
        {
            "name": "Off-touch (1 tick, normal flow, normal vol)",
            "distance_ticks": 1.0,
            "flow_factor": 1.0,
            "vol_z_score": 0.0,
        },
        {
            "name": "Worst case (2 ticks, low flow, high vol)",
            "distance_ticks": 2.0,
            "flow_factor": 0.5,
            "vol_z_score": 2.0,
        },
    ]
    
    print("=" * 80)
    print("SIMULATED FILL RATES (1000 trade events per scenario)")
    print("=" * 80)
    print(f"{'Scenario':<50} {'Fill Rate':<15} {'Avg Fill Size':<15}")
    print("-" * 80)
    
    for scenario in scenarios:
        # Compute theoretical fill probability
        distance_decay = math.exp(-1.5 * scenario["distance_ticks"])
        flow_intensity = min(1.5, scenario["flow_factor"])
        vol_penalty = 1.0 / (1.0 + 0.8 * max(0, scenario["vol_z_score"]))
        queue_factor = 0.5 if scenario["distance_ticks"] < 0.5 else (0.8 if scenario["distance_ticks"] < 1.5 else 0.3)
        
        fill_prob = 0.20 * distance_decay * flow_intensity * vol_penalty * queue_factor
        
        # Simulate 1000 trade events
        fills = 0
        fill_sizes = []
        
        for _ in range(1000):
            fill_qty = model.compute_partial_fill_size(
                fill_prob=fill_prob,
                my_qty=10.0,
                trade_qty=5.0,
            )
            if fill_qty is not None:
                fills += 1
                fill_sizes.append(fill_qty)
        
        fill_rate = fills / 1000
        avg_fill_size = sum(fill_sizes) / len(fill_sizes) if fill_sizes else 0
        
        print(f"{scenario['name']:<50} {fill_rate:<15.1%} {avg_fill_size:<15.2f}")
    
    print()


def compare_models():
    """Compare deterministic vs. probabilistic for typical trading session."""
    print("=" * 80)
    print("MODEL COMPARISON: 1 Hour of Trading")
    print("=" * 80)
    print()
    
    # Assumptions
    trades_per_hour = 200  # Market has 200 trades/hour
    my_orders_at_touch_pct = 0.60  # 60% of time at touch
    my_orders_1tick_pct = 0.30  # 30% of time 1 tick away
    my_orders_2tick_pct = 0.10  # 10% of time 2+ ticks away
    
    # Deterministic model (touch = 100% fill)
    det_fills_at_touch = trades_per_hour * my_orders_at_touch_pct * 1.0
    det_fills_1tick = trades_per_hour * my_orders_1tick_pct * 1.0  # Assumes aggressive flow reaches
    det_fills_2tick = trades_per_hour * my_orders_2tick_pct * 0.5  # Conservative: 50% reach
    det_total_fills = det_fills_at_touch + det_fills_1tick + det_fills_2tick
    
    # Probabilistic model
    prob_fills_at_touch = trades_per_hour * my_orders_at_touch_pct * 0.10  # 10% fill rate
    prob_fills_1tick = trades_per_hour * my_orders_1tick_pct * 0.025  # 2.5% fill rate
    prob_fills_2tick = trades_per_hour * my_orders_2tick_pct * 0.005  # 0.5% fill rate
    prob_total_fills = prob_fills_at_touch + prob_fills_1tick + prob_fills_2tick
    
    print(f"Market trade count: {trades_per_hour} trades/hour")
    print()
    print(f"{'Model':<30} {'At Touch Fills':<20} {'1 Tick Fills':<20} {'2+ Tick Fills':<20} {'Total Fills':<15}")
    print("-" * 105)
    print(f"{'Deterministic (old)':<30} {det_fills_at_touch:<20.1f} {det_fills_1tick:<20.1f} {det_fills_2tick:<20.1f} {det_total_fills:<15.1f}")
    print(f"{'Probabilistic (new)':<30} {prob_fills_at_touch:<20.1f} {prob_fills_1tick:<20.1f} {prob_fills_2tick:<20.1f} {prob_total_fills:<15.1f}")
    print()
    print(f"Reduction: {(1 - prob_total_fills/det_total_fills):.0%} fewer fills with probabilistic model")
    print()
    
    # PnL impact (rough estimate)
    print("Expected PnL Impact:")
    print(f"  Deterministic: {det_total_fills:.0f} fills × $0.50 spread = ${det_total_fills * 0.50:.2f}/hour")
    print(f"  Probabilistic: {prob_total_fills:.0f} fills × $0.50 spread = ${prob_total_fills * 0.50:.2f}/hour")
    print(f"  Difference: {(prob_total_fills / det_total_fills):.1%} of deterministic PnL")
    print()


def main():
    """Run all comparisons."""
    print("\n")
    print("╔" + "═" * 78 + "╗")
    print("║" + " PROBABILISTIC FILL MODEL - COMPARISON & VISUALIZATION ".center(78) + "║")
    print("╚" + "═" * 78 + "╝")
    print()
    
    visualize_distance_decay()
    visualize_volatility_impact()
    visualize_flow_impact()
    simulate_fill_rates()
    compare_models()
    
    print("=" * 80)
    print("KEY TAKEAWAYS:")
    print("=" * 80)
    print()
    print("1. Distance matters: 1 tick away = ~80% reduction in fill probability")
    print("2. Volatility hurts: 2σ volatility spike = ~60% reduction in fills")
    print("3. Flow helps: 1.5× normal flow = +50% boost (capped)")
    print("4. Expect 3-5× fewer fills than deterministic model")
    print("5. This is GOOD: realistic fills prevent overconfidence in strategies")
    print()
    print("To use probabilistic model:")
    print("  1. Set fill_model: 'probabilistic' in config.yaml")
    print("  2. Calibrate baseline_volume_per_sec per market")
    print("  3. Run both models side-by-side to validate")
    print()


if __name__ == "__main__":
    main()
