# Probabilistic Fill Model - Quick Reference

## Formula

```
P_fill = β × D × F × V × Q
```

## Components

### β - Base Fill Rate
```
β = 0.20  (20% at touch, normal conditions)
```

### D - Distance Decay
```
D = exp(-λ × distance_ticks)

where:
  λ = 1.5 (decay rate)
  distance_ticks = |my_price - best_price| / tick_size
```

**Values:**
- 0 ticks: D = 1.00 (100%)
- 1 tick: D = 0.22 (22%)
- 2 ticks: D = 0.05 (5%)

### F - Flow Intensity
```
F = min(1.5, volume_per_sec / baseline_volume_per_sec)

where:
  volume_per_sec = recent_volume / lookback_seconds
  baseline_volume_per_sec = 0.5  (calibrate per market)
```

**Values:**
- Low flow (0.5×): F = 0.50
- Normal flow (1×): F = 1.00
- High flow (1.5×): F = 1.50 (capped)

### V - Volatility Penalty
```
V = 1 / (1 + k × max(0, vol_z_score))

where:
  k = 0.8 (penalty strength)
  vol_z_score = (realized_vol - baseline_vol) / (baseline_vol × 0.5)
  baseline_vol = 0.50  (50% annualized)
```

**Values:**
- Low vol (z=-1): V = 1.00
- Normal vol (z=0): V = 1.00
- High vol (z=+1): V = 0.56 (44% penalty)
- Extreme vol (z=+2): V = 0.38 (62% penalty)

### Q - Queue Position
```
Q = heuristic(distance_ticks)
```

**Values:**
- At touch (d<0.5): Q = 0.5 (middle of queue)
- 1 tick away (0.5≤d<1.5): Q = 0.8 (early if reached)
- 2+ ticks (d≥1.5): Q = 0.3 (low priority)

## Example Calculations

### Scenario 1: Best Case
```
Distance: 0 ticks (at touch)
Flow: 1.5× baseline
Volatility: Normal (z=0)
Queue: Middle (0.5)

P_fill = 0.20 × 1.00 × 1.50 × 1.00 × 0.50
       = 0.15 (15%)
```

### Scenario 2: Normal Case
```
Distance: 0 ticks
Flow: 1× baseline
Volatility: Normal (z=0)
Queue: Middle (0.5)

P_fill = 0.20 × 1.00 × 1.00 × 1.00 × 0.50
       = 0.10 (10%)
```

### Scenario 3: Off-Touch
```
Distance: 1 tick
Flow: 1× baseline
Volatility: Normal (z=0)
Queue: Early (0.8)

P_fill = 0.20 × 0.22 × 1.00 × 1.00 × 0.80
       = 0.035 (3.5%)
```

### Scenario 4: High Volatility
```
Distance: 0 ticks
Flow: 1× baseline
Volatility: High (z=+2)
Queue: Middle (0.5)

P_fill = 0.20 × 1.00 × 1.00 × 0.38 × 0.50
       = 0.038 (3.8%)
```

### Scenario 5: Worst Case
```
Distance: 2 ticks
Flow: 0.5× baseline
Volatility: High (z=+2)
Queue: Low (0.3)

P_fill = 0.20 × 0.05 × 0.50 × 0.38 × 0.30
       = 0.0006 (0.06%)
```

## Partial Fill Size

```python
if random.random() < P_fill:
    max_fill = min(my_qty, trade_qty)
    partial_factor = min(1.0, trade_qty / (2 × my_qty))
    fill_qty = max_fill × partial_factor
    
    if fill_qty < 0.01 × my_qty:
        fill_qty = None  # Too small, reject
```

## Configuration

```yaml
sim:
  fill_model: "probabilistic"
  fill_model_params:
    base_fill_rate: 0.20
    decay_lambda: 1.5
    vol_penalty_k: 0.8
    baseline_vol: 0.50
    baseline_volume_per_sec: 0.5
    flow_cap: 1.5
    min_fill_fraction: 0.01
    tick_size_bps: 1.0
```

## Calibration Checklist

- [ ] Measure market's typical `volume_per_sec` → set `baseline_volume_per_sec`
- [ ] Measure market's typical volatility → set `baseline_vol`
- [ ] Collect real fill data (if available) → fit `base_fill_rate` and `decay_lambda`
- [ ] Validate: simulated fills ≈ 70-90% of actual fills

## Common Issues

### Too Many Fills
- **Solution:** Lower `base_fill_rate` to 0.10-0.15
- Or increase `decay_lambda` to 2.0-2.5

### Too Few Fills
- **Solution:** Raise `base_fill_rate` to 0.25-0.30
- Or decrease `decay_lambda` to 1.0-1.2

### No Fills at All
- **Check:** `baseline_volume_per_sec` might be too high
- **Check:** Are you always 2+ ticks away from touch?

### Fills Only at Touch
- **Expected:** This is normal! Passive orders rarely fill when off-touch.

## Comparison Table

| Condition | Deterministic | Probabilistic |
|-----------|---------------|---------------|
| At touch, normal | 100% | 10% |
| At touch, high flow | 100% | 15% |
| 1 tick away | 100% (if reached) | 2-4% |
| 2 ticks away | 50% | <1% |
| High volatility | 100% | 4-6% |

## Key Insights

1. **Distance dominates:** 1 tick = 80% reduction
2. **Volatility hurts:** 2σ spike = 60% reduction
3. **Flow helps (capped):** Max +50% boost
4. **Queue matters:** At touch, assume middle (50%)
5. **Expect 70-85% fewer fills** than deterministic

## One-Liner Summary

**"Probabilistic fill model: Conservative, microstructure-aware simulation that accounts for distance, flow, volatility, and queue position to provide realistic feedback for passive market-making strategies."**
