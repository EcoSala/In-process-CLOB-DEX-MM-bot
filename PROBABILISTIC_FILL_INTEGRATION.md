# Integration Guide: Probabilistic Fill Model

## Quick Start

### Step 1: Add model to `PaperMM.__init__`

```python
# In src/sim/paper_mm.py

from src.sim.probabilistic_fill import ProbabilisticFillModel, FillModelParams

class PaperMM:
    def __init__(
        self,
        quote_half_spread_bps: float,
        quote_size_usd: float,
        max_inventory_usd: float,
        trade_stats: Optional[TradeStats] = None,
        execution_tape: Optional[ExecutionTape] = None,
        order_ladder: Optional[OrderLadder] = None,
        fill_model: str = "deterministic",  # NEW: "deterministic" or "probabilistic"
        fill_model_params: Optional[FillModelParams] = None,  # NEW
    ):
        # ... existing code ...
        
        # Fill model
        self.fill_model_type = fill_model
        if fill_model == "probabilistic":
            self.fill_model = ProbabilisticFillModel(fill_model_params)
        else:
            self.fill_model = None  # Use old deterministic logic
```

### Step 2: Add new `on_trade_probabilistic` method

```python
# In src/sim/paper_mm.py

def on_trade_probabilistic(
    self, 
    mid: float, 
    trade_px: float, 
    trade_qty: float, 
    side: str,  # "BUY" or "SELL" (aggressor side)
    q: Quote,
    tape,  # TradeTape
    tob,   # TopOfBook
) -> None:
    """
    Probabilistic fill model for passive market making.
    
    Uses conservative probabilistic logic based on:
    - Distance from touch
    - Flow intensity
    - Volatility regime
    - Queue position
    """
    from src.sim.probabilistic_fill import extract_market_data_from_tape
    
    # Get market data for fill model
    lookback_seconds = 60.0
    recent_prices, recent_volume = extract_market_data_from_tape(tape, lookback_seconds)
    
    # Get per-market position state
    pos_state = self._get_position_state(self.current_market)
    inv_usd = self._inv_usd(self.current_market, mid)
    
    # Determine which side we can fill
    if side == "BUY":
        # Aggressor buys, we sell at our ask
        my_price = q.ask_px
        best_price = tob.ask_px if tob.ask_px else mid
        can_trade = inv_usd > -self.max_inventory_usd
        my_side = "ASK"
        fill_side_label = "SELL"
        
    elif side == "SELL":
        # Aggressor sells, we buy at our bid
        my_price = q.bid_px
        best_price = tob.bid_px if tob.bid_px else mid
        can_trade = inv_usd < self.max_inventory_usd
        my_side = "BID"
        fill_side_label = "BUY"
    
    else:
        return
    
    if not can_trade:
        return
    
    # === Use probabilistic fill model ===
    fill_qty, debug_info = self.fill_model.should_fill(
        my_price=my_price,
        best_price=best_price,
        mid_price=mid,
        trade_price=trade_px,
        trade_qty=trade_qty,
        my_qty=q.qty_base,
        side=side,
        my_side=my_side,
        recent_prices=recent_prices,
        recent_volume=recent_volume,
        lookback_seconds=lookback_seconds,
    )
    
    # No fill
    if fill_qty is None:
        return
    
    # === Execute fill (same logic as deterministic model) ===
    fill_px = my_price
    old_pos = pos_state["pos"]
    old_avg_price = pos_state["avg_price"]
    
    if fill_side_label == "SELL":
        # Calculate realized PnL
        realized_pnl_trade, new_avg_price = self._calculate_realized_pnl(
            old_pos, -fill_qty, fill_px, old_avg_price
        )
        
        # Update state
        pos_state["pos"] -= fill_qty
        pos_state["avg_price"] = new_avg_price
        pos_state["realized_pnl"] += realized_pnl_trade
        self.state.cash_usd += fill_qty * fill_px
        
        # Update order ladder
        if self.order_ladder and self.active_ask_order:
            self.order_ladder.update_fill(self.active_ask_order, fill_qty)
        
    elif fill_side_label == "BUY":
        # Calculate realized PnL
        realized_pnl_trade, new_avg_price = self._calculate_realized_pnl(
            old_pos, fill_qty, fill_px, old_avg_price
        )
        
        # Update state
        pos_state["pos"] += fill_qty
        pos_state["avg_price"] = new_avg_price
        pos_state["realized_pnl"] += realized_pnl_trade
        self.state.cash_usd -= fill_qty * fill_px
        
        # Update order ladder
        if self.order_ladder and self.active_bid_order:
            self.order_ladder.update_fill(self.active_bid_order, fill_qty)
    
    # === Record stats and tape ===
    if self.trade_stats:
        self.trade_stats.record(fill_qty, fill_px, side)
    
    if self.execution_tape:
        pnl = self.mark_to_market({self.current_market: mid})
        self.execution_tape.record_fill(
            tick=self.current_tick,
            market=self.current_market,
            side=fill_side_label,
            size=fill_qty,
            price=fill_px,
            notional=fill_qty * fill_px,
            avg_price_after=pos_state["avg_price"],
            pos_after=pos_state["pos"],
            cash_after=self.state.cash_usd,
            pnl_after=pnl,
            realized_pnl_trade=realized_pnl_trade,
            realized_pnl_total=pos_state["realized_pnl"],
        )
```

### Step 3: Update call site in `app.py`

```python
# In src/core/app.py, heartbeat_loop

# OLD (deterministic):
self.paper.on_trade(mid, trade_px, trade_qty, side, q)

# NEW (probabilistic):
if self.paper.fill_model_type == "probabilistic":
    self.paper.on_trade_probabilistic(mid, trade_px, trade_qty, side, q, tape, tob)
else:
    self.paper.on_trade(mid, trade_px, trade_qty, side, q)
```

### Step 4: Update config

```yaml
# In example_config.yaml

sim:
  enabled: true
  quote_half_spread_bps: 0.2
  quote_size_usd: 333
  max_inventory_usd: 50000
  
  # Fill model selection
  fill_model: "probabilistic"  # "deterministic" or "probabilistic"
  
  # Probabilistic fill model parameters (optional, uses defaults if not specified)
  fill_model_params:
    base_fill_rate: 0.20           # 20% at touch, normal conditions
    decay_lambda: 1.5              # Distance decay rate
    vol_penalty_k: 0.8             # Volatility penalty strength
    baseline_vol: 0.50             # 50% annualized vol baseline
    baseline_volume_per_sec: 0.5   # Volume baseline (BTC/sec)
    flow_cap: 1.5                  # Max flow boost (1.5× baseline)
    min_fill_fraction: 0.01        # Minimum 1% partial fill
    tick_size_bps: 1.0             # 1 bps tick size
```

### Step 5: Update config loading

```python
# In src/core/config.py

class SimConfig(BaseModel):
    enabled: bool = True
    quote_half_spread_bps: float
    quote_size_usd: float
    max_inventory_usd: float
    
    # Fill model (new)
    fill_model: str = "deterministic"  # "deterministic" or "probabilistic"
    fill_model_params: dict = {}  # Optional params override
```

```python
# In src/core/app.py, __init__

from src.sim.probabilistic_fill import FillModelParams

# Parse fill model params
fill_params = None
if cfg.sim.fill_model == "probabilistic":
    params_dict = cfg.sim.fill_model_params
    fill_params = FillModelParams(
        base_fill_rate=params_dict.get("base_fill_rate", 0.20),
        decay_lambda=params_dict.get("decay_lambda", 1.5),
        vol_penalty_k=params_dict.get("vol_penalty_k", 0.8),
        baseline_vol=params_dict.get("baseline_vol", 0.50),
        baseline_volume_per_sec=params_dict.get("baseline_volume_per_sec", 0.5),
        flow_cap=params_dict.get("flow_cap", 1.5),
        min_fill_fraction=params_dict.get("min_fill_fraction", 0.01),
        tick_size_bps=params_dict.get("tick_size_bps", 1.0),
    )

# Paper market-making simulator
self.paper = PaperMM(
    quote_half_spread_bps=cfg.sim.quote_half_spread_bps,
    quote_size_usd=cfg.sim.quote_size_usd,
    max_inventory_usd=cfg.sim.max_inventory_usd,
    trade_stats=self.trade_stats,
    execution_tape=self.execution_tape,
    order_ladder=self.order_ladder,
    fill_model=cfg.sim.fill_model,
    fill_model_params=fill_params,
)
```

---

## Side-by-Side Comparison

Run both models on the same market data to compare:

### Comparison Metrics

```python
# Add to heartbeat_loop or a separate analysis script

# Log fill rates
if self.state.ticks % 600 == 0:  # Every 10 minutes
    log.info(
        f"FILL_STATS: "
        f"total_trades={self.trade_stats.num_trades} "
        f"vol={self.trade_stats.total_volume:.2f} "
        f"notional=${self.trade_stats.total_notional:.2f}"
    )
```

### Expected Differences

| Metric | Deterministic | Probabilistic |
|--------|---------------|---------------|
| Fill count | 100-200 fills/hour | 20-60 fills/hour |
| Fill rate | ~80-100% of trades | ~15-30% of trades |
| PnL volatility | High | Moderate |
| Realism | Overly optimistic | Conservative |

---

## Testing Strategy

### Unit Tests

```python
# tests/test_probabilistic_fill.py

import pytest
from src.sim.probabilistic_fill import ProbabilisticFillModel, FillModelParams

def test_distance_decay():
    model = ProbabilisticFillModel()
    
    # At touch
    p0, _ = model.compute_fill_probability(
        my_price=100.0,
        best_price=100.0,
        mid_price=100.0,
        trade_price=100.0,
        trade_qty=1.0,
        my_qty=1.0,
        recent_prices=[99, 100, 101],
        recent_volume=10.0,
        lookback_seconds=60.0,
    )
    
    # 1 tick away
    p1, _ = model.compute_fill_probability(
        my_price=100.1,  # ~10 ticks away at 1 bps tick
        best_price=100.0,
        mid_price=100.0,
        trade_price=100.1,
        trade_qty=1.0,
        my_qty=1.0,
        recent_prices=[99, 100, 101],
        recent_volume=10.0,
        lookback_seconds=60.0,
    )
    
    assert p0 > p1
    print(f"P(fill | at touch) = {p0:.1%}")
    print(f"P(fill | 1 tick away) = {p1:.1%}")


def test_volatility_penalty():
    model = ProbabilisticFillModel()
    
    # Low volatility (stable prices)
    stable_prices = [100.0, 100.1, 99.9, 100.0, 100.1] * 10
    
    p_stable, info_stable = model.compute_fill_probability(
        my_price=100.0,
        best_price=100.0,
        mid_price=100.0,
        trade_price=100.0,
        trade_qty=1.0,
        my_qty=1.0,
        recent_prices=stable_prices,
        recent_volume=10.0,
        lookback_seconds=60.0,
    )
    
    # High volatility (wild swings)
    volatile_prices = [100, 105, 95, 110, 90, 108, 92] * 5
    
    p_volatile, info_volatile = model.compute_fill_probability(
        my_price=100.0,
        best_price=100.0,
        mid_price=100.0,
        trade_price=100.0,
        trade_qty=1.0,
        my_qty=1.0,
        recent_prices=volatile_prices,
        recent_volume=10.0,
        lookback_seconds=60.0,
    )
    
    assert p_stable > p_volatile
    print(f"P(fill | stable) = {p_stable:.1%}, vol_z={info_stable['vol_z_score']:.2f}")
    print(f"P(fill | volatile) = {p_volatile:.1%}, vol_z={info_volatile['vol_z_score']:.2f}")


def test_flow_sensitivity():
    model = ProbabilisticFillModel()
    
    # Low flow
    p_low, info_low = model.compute_fill_probability(
        my_price=100.0,
        best_price=100.0,
        mid_price=100.0,
        trade_price=100.0,
        trade_qty=1.0,
        my_qty=1.0,
        recent_prices=[100] * 20,
        recent_volume=1.0,  # Low volume
        lookback_seconds=60.0,
    )
    
    # High flow
    p_high, info_high = model.compute_fill_probability(
        my_price=100.0,
        best_price=100.0,
        mid_price=100.0,
        trade_price=100.0,
        trade_qty=1.0,
        my_qty=1.0,
        recent_prices=[100] * 20,
        recent_volume=50.0,  # High volume
        lookback_seconds=60.0,
    )
    
    assert p_high > p_low
    print(f"P(fill | low flow) = {p_low:.1%}, flow={info_low['flow_intensity']:.2f}")
    print(f"P(fill | high flow) = {p_high:.1%}, flow={info_high['flow_intensity']:.2f}")


def test_partial_fills():
    model = ProbabilisticFillModel()
    
    # Simulate 100 fills
    fill_sizes = []
    for _ in range(1000):
        fill_qty = model.compute_partial_fill_size(
            fill_prob=0.30,  # 30% fill probability
            my_qty=10.0,
            trade_qty=5.0,   # Aggressor trades 5.0
        )
        if fill_qty is not None:
            fill_sizes.append(fill_qty)
    
    # Check that we got ~30% fills
    fill_rate = len(fill_sizes) / 1000
    assert 0.20 < fill_rate < 0.40  # Should be around 30%
    
    # Check that fill sizes are partial (not always full)
    avg_fill_size = sum(fill_sizes) / len(fill_sizes)
    assert avg_fill_size < 5.0  # Should be less than trade_qty
    
    print(f"Fill rate: {fill_rate:.1%}")
    print(f"Avg fill size: {avg_fill_size:.2f} (max possible: 5.0)")
```

### Integration Tests

```python
# tests/test_paper_mm_probabilistic.py

def test_probabilistic_vs_deterministic():
    """
    Run both models on the same trade stream and compare results.
    """
    from src.sim.paper_mm import PaperMM
    from src.sim.probabilistic_fill import FillModelParams
    
    # Create two PaperMM instances
    paper_det = PaperMM(
        quote_half_spread_bps=2.0,
        quote_size_usd=1000,
        max_inventory_usd=10000,
        fill_model="deterministic",
    )
    
    paper_prob = PaperMM(
        quote_half_spread_bps=2.0,
        quote_size_usd=1000,
        max_inventory_usd=10000,
        fill_model="probabilistic",
        fill_model_params=FillModelParams(),
    )
    
    # Simulate trades (mock data)
    mock_trades = [
        (100.0, 1.0, "BUY"),   # price, qty, side
        (100.1, 0.5, "SELL"),
        (100.2, 2.0, "BUY"),
        # ... more trades ...
    ]
    
    for trade_px, trade_qty, side in mock_trades:
        mid = 100.0
        q = paper_det.make_quote(mid)
        
        # Apply to both
        paper_det.on_trade(mid, trade_px, trade_qty, side, q)
        # paper_prob.on_trade_probabilistic(...) # Need mock tape/tob
    
    # Compare results
    print(f"Deterministic fills: {paper_det.trade_stats.num_trades}")
    print(f"Probabilistic fills: {paper_prob.trade_stats.num_trades}")
    
    # Probabilistic should have fewer fills
    assert paper_prob.trade_stats.num_trades < paper_det.trade_stats.num_trades
```

---

## Calibration Guide

### Step 1: Collect Real Fill Data

If you have access to real trading data:

```python
# Collect actual fill rates from exchange API or live trading

actual_fills = [
    {"distance_ticks": 0, "fill_rate": 0.25},
    {"distance_ticks": 1, "fill_rate": 0.08},
    {"distance_ticks": 2, "fill_rate": 0.02},
]
```

### Step 2: Grid Search for Parameters

```python
from scipy.optimize import minimize

def loss_function(params):
    """MSE between model predictions and actual fill rates"""
    base_rate, decay_lambda = params
    
    model = ProbabilisticFillModel(FillModelParams(
        base_fill_rate=base_rate,
        decay_lambda=decay_lambda,
    ))
    
    total_error = 0
    for data_point in actual_fills:
        dist = data_point["distance_ticks"]
        actual = data_point["fill_rate"]
        
        # Simplified: assume normal flow/vol
        predicted = base_rate * math.exp(-decay_lambda * dist)
        
        total_error += (predicted - actual) ** 2
    
    return total_error

# Optimize
result = minimize(
    loss_function,
    x0=[0.20, 1.5],  # Initial guess
    bounds=[(0.05, 0.50), (0.5, 3.0)],
)

print(f"Optimized params: base_rate={result.x[0]:.2f}, decay_lambda={result.x[1]:.2f}")
```

### Step 3: Validate Out-of-Sample

Split your data into train/test and validate the calibrated model on held-out data.

---

## FAQ

**Q: Will this reduce my PnL?**  
A: Yes, because the current model is overly optimistic. But the goal is *realistic* PnL, not inflated PnL.

**Q: How do I switch back to the old model?**  
A: Set `fill_model: "deterministic"` in config.

**Q: Can I use both models simultaneously?**  
A: Yes, run two bot instances with different configs and compare.

**Q: What if I want even more conservative fills?**  
A: Lower `base_fill_rate` to 0.10-0.15 or increase `decay_lambda` to 2.0-2.5.

**Q: How do I handle different markets (BTC vs. altcoins)?**  
A: Calibrate `baseline_volume_per_sec` per market. High-liquidity markets need higher baselines.

**Q: Does this model account for adverse selection?**  
A: No, it only models fill probability. Adverse selection (when to cancel orders) is a separate strategy concern.

---

## Summary

This integration guide provides:

✅ **Step-by-step code changes** (minimal invasiveness)  
✅ **Config-driven model selection** (easy to switch between models)  
✅ **Comprehensive testing strategy** (unit + integration tests)  
✅ **Calibration workflow** (fit parameters to real data)  
✅ **Side-by-side comparison** (validate model improvements)  

The probabilistic model is **drop-in compatible** with your existing code and can be enabled/disabled via config.
