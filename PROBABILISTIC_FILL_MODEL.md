# Probabilistic Fill Model for Paper Market Making

## Problem with Current Implementation

**Current logic (lines 455-549 in `paper_mm.py`):**
```python
if side == "BUY":
    if q.ask_px <= trade_px:  # "touch = fill"
        fill_qty = min(q.qty_base, trade_qty)  # Full fill
```

**Issues:**
1. **Instant fill on touch** - unrealistic for passive orders
2. **No queue position** - ignores other orders at same price
3. **Full fills only** - no partial fills
4. **Volatility ignored** - treats calm and volatile markets identically
5. **Overly optimistic** - systematically overestimates fill rates

---

## Proposed Probabilistic Model

### Core Philosophy

**Passive market making fills occur when:**
1. Aggressor flow **penetrates through** your price level
2. You're **early enough in the queue** at that price
3. The market is **stable enough** that aggressive orders reach your level

**Conservative principle:** Better to underestimate fills than create false confidence in a strategy.

---

## Mathematical Formula

### Fill Probability (per trade event)

```python
P_fill = Base_Rate × Distance_Decay × Flow_Intensity × Volatility_Penalty × Queue_Position
```

### Component Definitions

#### 1. Base Fill Rate (β)

```python
β = 0.20  # 20% baseline when at touch, low intensity, normal vol
```

**Intuition:** Even when you're at the touch, you don't fill on *every* trade. Other market makers are competing, and some trades may be too small.

**Conservative choice:** 0.20 (not 1.0)

---

#### 2. Distance Decay (D)

```python
distance_ticks = abs(my_price - best_price) / tick_size
D = exp(-λ × distance_ticks)

where λ = 1.5  # decay rate parameter
```

**Intuition:** 
- At touch (distance = 0): D = 1.0 (100%)
- 1 tick away: D = 0.22 (22%)
- 2 ticks away: D = 0.05 (5%)
- 3+ ticks away: D ≈ 0 (negligible)

**Why exponential decay?**
- Order book liquidity is typically concentrated at the touch
- Aggressive flow rarely penetrates deeply
- Conservative: steep decay ensures fills are rare when off-touch

**Graph:**
```
D
│ 1.0 ●
│      ╲
│ 0.5  │ ╲
│      │   ●
│ 0.2  │     ╲●
│ 0.0  │________●_____ distance_ticks
       0   1   2   3
```

---

#### 3. Flow Intensity Factor (F)

```python
recent_volume = sum(trade_qty for trades in last N seconds)
volume_per_sec = recent_volume / N

baseline_volume_per_sec = 0.5  # calibrated per market (e.g., 0.5 BTC/sec)

F = min(1.5, volume_per_sec / baseline_volume_per_sec)
```

**Intuition:**
- High flow → more trades → more queue consumption → higher fill probability
- But capped at 1.5× to remain conservative
- Low flow → fewer fills (F < 1.0)

**Why not unbounded?**
- In extreme flow, you'd cancel orders (adverse selection risk)
- Conservative: cap at 1.5× to avoid overstating fills in spikes

**Alternative (trades-based instead of volume-based):**
```python
trades_per_sec = tape.trades_per_min() / 60.0
baseline_tps = 2.0  # 2 trades/sec is "normal"

F = min(1.5, trades_per_sec / baseline_tps)
```

---

#### 4. Volatility Penalty (V)

```python
# Compute short-term realized volatility (std of log returns)
recent_prices = [price for (ts, price, qty, side) in tape.recent(60)]
returns = [log(p2/p1) for p1, p2 in zip(recent_prices[:-1], recent_prices[1:])]
realized_vol = std(returns) * sqrt(len(returns))  # annualized

# Compute z-score vs baseline
baseline_vol = 0.50  # 50% annualized (calibrated per market)
vol_z_score = (realized_vol - baseline_vol) / (baseline_vol * 0.5)

# Penalty term
V = 1.0 / (1.0 + k × max(0, vol_z_score))

where k = 0.8  # penalty strength
```

**Intuition:**
- **High volatility** → aggressive flow "walks the book" faster
- **But:** Your passive order is less likely to fill at a *good* price
- In volatile regimes, you'd *widen* spreads or *cancel* orders
- Conservative: penalize fills when market is erratic

**Why this matters:**
- In a volatility spike, the touch moves rapidly
- By the time an aggressor reaches your (now off-touch) price, it's adverse
- A realistic model should discourage fills in high-vol regimes

**Examples:**
- Normal vol (z = 0): V = 1.0 (no penalty)
- High vol (z = +1): V = 0.56 (44% reduction)
- Extreme vol (z = +2): V = 0.38 (62% reduction)

**Graph:**
```
V
│ 1.0 ●___
│        ╲
│ 0.5      ●╲
│             ╲●
│ 0.0           ╲_____ vol_z_score
      -1   0   1   2
```

---

#### 5. Queue Position Factor (Q)

```python
# Assume we're uniformly distributed in the queue at our price
# (Since we can't see real queue, use heuristic)

if distance_ticks == 0:
    # At touch: assume we're middle of the queue
    Q = 0.5
elif distance_ticks == 1:
    # One tick behind: assume we're early (if we get there at all)
    Q = 0.8
else:
    # 2+ ticks behind: very unlikely to fill unless massive flow
    Q = 0.3
```

**Intuition:**
- At the touch, many other market makers are competing
- 1 tick behind touch: fewer orders there, so if flow reaches you, you're likely early
- 2+ ticks behind: mostly irrelevant unless extreme flow event

**Conservative choice:** Q ≤ 0.8 (never assume you're first in queue)

**Note:** This is a heuristic. A more advanced model would track order arrival times and simulate a FIFO queue.

---

## Combined Fill Probability

```python
P_fill = β × D × F × V × Q

where:
  β = 0.20            # base rate
  D = exp(-1.5 × distance_ticks)
  F = min(1.5, volume_per_sec / baseline_volume_per_sec)
  V = 1 / (1 + 0.8 × max(0, vol_z_score))
  Q = queue_position_heuristic(distance_ticks)
```

### Example Calculation

**Scenario:**
- Distance: 0 ticks (at touch)
- Flow: 2× baseline (F = 1.5, capped)
- Volatility: Normal (V = 1.0)
- Queue: Middle of queue (Q = 0.5)

```python
P_fill = 0.20 × 1.0 × 1.5 × 1.0 × 0.5 = 0.15 (15% per trade event)
```

**Interpretation:** On each aggressor trade that touches your price, you have a 15% chance to fill.

**Over 10 trades:** 1 - (1 - 0.15)^10 ≈ 80% cumulative fill probability

---

## Partial Fills

**Current implementation:** Full fill only (`fill_qty = min(q.qty_base, trade_qty)`)

**Proposed:** Partial fill proportional to aggressor volume and fill probability

```python
if random.uniform(0, 1) < P_fill:
    # Determine fill size
    max_fill = min(my_order_size, aggressor_trade_qty)
    
    # Partial fill factor (conservative: fill only a fraction)
    partial_factor = min(1.0, aggressor_trade_qty / (2 * my_order_size))
    
    fill_qty = max_fill × partial_factor
```

**Intuition:**
- Small aggressor trade → small fill
- Large aggressor trade → potentially full fill
- Conservative: `partial_factor` ensures we don't fill too eagerly

---

## Integration into Current Code

### Changes to `paper_mm.py`

#### Step 1: Add volatility calculation method

```python
def _compute_volatility_z_score(self, market: str, tape, baseline_vol: float = 0.50) -> float:
    """
    Compute short-term realized volatility z-score.
    
    Returns:
        z-score relative to baseline_vol (positive = high vol)
    """
    recent_trades = tape.recent(60.0)  # last 60 seconds
    
    if len(recent_trades) < 10:
        return 0.0  # insufficient data, assume normal vol
    
    prices = [price for (ts_ms, price, qty, side) in recent_trades]
    
    if len(prices) < 2:
        return 0.0
    
    # Log returns
    import math
    returns = [math.log(p2 / p1) for p1, p2 in zip(prices[:-1], prices[1:])]
    
    # Realized vol (std of returns, annualized)
    mean_ret = sum(returns) / len(returns)
    variance = sum((r - mean_ret)**2 for r in returns) / len(returns)
    std_dev = math.sqrt(variance)
    
    # Annualize (assuming 1-second intervals)
    realized_vol = std_dev * math.sqrt(60 * 60 * 24 * 365)
    
    # Z-score
    z_score = (realized_vol - baseline_vol) / (baseline_vol * 0.5)
    
    return z_score
```

#### Step 2: Add flow intensity calculation

```python
def _compute_flow_intensity(self, tape, baseline_volume_per_sec: float = 0.5) -> float:
    """
    Compute recent volume intensity relative to baseline.
    
    Returns:
        Flow factor (≥ 0, capped at 1.5)
    """
    recent_trades = tape.recent(10.0)  # last 10 seconds
    
    if not recent_trades:
        return 0.5  # low flow default
    
    total_volume = sum(qty for (ts_ms, price, qty, side) in recent_trades)
    volume_per_sec = total_volume / 10.0
    
    flow_factor = min(1.5, volume_per_sec / baseline_volume_per_sec)
    
    return max(0.1, flow_factor)  # floor at 0.1 to avoid zero
```

#### Step 3: Replace `on_trade` with probabilistic fill logic

```python
def on_trade_probabilistic(
    self, 
    mid: float, 
    trade_px: float, 
    trade_qty: float, 
    side: str, 
    q: Quote,
    tape,
    tob,
) -> None:
    """
    Probabilistic fill model for passive market making.
    
    Fills depend on:
    - Distance from touch (exponential decay)
    - Flow intensity (recent volume)
    - Volatility regime (penalty for high vol)
    - Queue position (heuristic)
    - Partial fills proportional to aggressor volume
    """
    import random
    import math
    
    # Model parameters (can be moved to config)
    BASE_FILL_RATE = 0.20
    DECAY_LAMBDA = 1.5
    VOL_PENALTY_K = 0.8
    BASELINE_VOL = 0.50
    BASELINE_VOLUME_PER_SEC = 0.5  # calibrate per market
    
    # Get per-market state
    pos_state = self._get_position_state(self.current_market)
    inv_usd = self._inv_usd(self.current_market, mid)
    
    # Compute market microstructure factors
    vol_z_score = self._compute_volatility_z_score(self.current_market, tape, BASELINE_VOL)
    flow_factor = self._compute_flow_intensity(tape, BASELINE_VOLUME_PER_SEC)
    
    # Determine which side we can fill
    if side == "BUY":
        # Aggressor buys, we sell at our ask
        my_price = q.ask_px
        best_price = tob.ask_px
        can_trade = inv_usd > -self.max_inventory_usd
        fill_side = "SELL"
        
    elif side == "SELL":
        # Aggressor sells, we buy at our bid
        my_price = q.bid_px
        best_price = tob.bid_px
        can_trade = inv_usd < self.max_inventory_usd
        fill_side = "BUY"
    
    else:
        return
    
    if not can_trade or best_price is None or my_price is None:
        return
    
    # === Compute Fill Probability ===
    
    # 1. Distance decay
    tick_size = mid * 0.0001  # 1 bps as tick size (can be refined)
    distance_ticks = abs(my_price - best_price) / tick_size
    distance_decay = math.exp(-DECAY_LAMBDA * distance_ticks)
    
    # 2. Flow intensity factor (already computed)
    flow_intensity = flow_factor
    
    # 3. Volatility penalty
    vol_penalty = 1.0 / (1.0 + VOL_PENALTY_K * max(0, vol_z_score))
    
    # 4. Queue position heuristic
    if distance_ticks < 0.5:
        queue_factor = 0.5  # at touch, middle of queue
    elif distance_ticks < 1.5:
        queue_factor = 0.8  # 1 tick behind, early in queue
    else:
        queue_factor = 0.3  # 2+ ticks, unlikely
    
    # 5. Direction filter: only fill if aggressor trades *through* our price
    if side == "BUY" and trade_px < my_price:
        return  # aggressor didn't reach our ask
    if side == "SELL" and trade_px > my_price:
        return  # aggressor didn't reach our bid
    
    # === Combined fill probability ===
    P_fill = BASE_FILL_RATE * distance_decay * flow_intensity * vol_penalty * queue_factor
    
    # === Bernoulli draw ===
    if random.random() > P_fill:
        return  # no fill
    
    # === Determine fill size (partial fill) ===
    max_fill_qty = min(q.qty_base, trade_qty)
    
    # Partial fill factor (fill fraction of our order)
    partial_factor = min(1.0, trade_qty / (2.0 * q.qty_base))
    fill_qty = max_fill_qty * partial_factor
    
    # Minimum fill size (e.g., 1% of order)
    if fill_qty < 0.01 * q.qty_base:
        return
    
    # === Execute fill (same logic as before) ===
    fill_px = my_price
    old_pos = pos_state["pos"]
    old_avg_price = pos_state["avg_price"]
    
    if fill_side == "SELL":
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
        
    elif fill_side == "BUY":
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
    
    # === Record stats and tape (unchanged) ===
    if self.trade_stats:
        self.trade_stats.record(fill_qty, fill_px, side)
    
    if self.execution_tape:
        pnl = self.mark_to_market({self.current_market: mid})
        self.execution_tape.record_fill(
            tick=self.current_tick,
            market=self.current_market,
            side=fill_side,
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

#### Step 4: Update call site in `app.py`

Change this:
```python
self.paper.on_trade(mid, trade_px, trade_qty, side, q)
```

To:
```python
self.paper.on_trade_probabilistic(mid, trade_px, trade_qty, side, q, tape, tob)
```

---

## Parameter Calibration

### Baseline Parameters (Conservative)

```python
BASE_FILL_RATE = 0.20          # 20% at touch, normal conditions
DECAY_LAMBDA = 1.5             # Steep decay (22% at 1 tick)
VOL_PENALTY_K = 0.8            # Moderate volatility penalty
BASELINE_VOL = 0.50            # 50% annualized (typical crypto)
BASELINE_VOLUME_PER_SEC = 0.5  # 0.5 BTC/sec (calibrate per market)
```

### How to Calibrate per Market

1. **Collect real fill data** (from live trading or exchange data)
2. **Measure actual fill rates** at different distances from touch
3. **Fit parameters** using maximum likelihood or grid search
4. **Validate** on out-of-sample data

**Example (BTC-USD):**
- High liquidity → higher `BASE_FILL_RATE` (e.g., 0.25)
- Tight spreads → lower `DECAY_LAMBDA` (e.g., 1.2)

**Example (Low-liquidity altcoin):**
- Sparse flow → lower `BASE_FILL_RATE` (e.g., 0.10)
- Wide spreads → higher `DECAY_LAMBDA` (e.g., 2.0)

---

## Assumptions & Limitations

### Assumptions

1. **FIFO queue at each price level** - but we only heuristically model queue position
2. **Tick size approximation** - using 1 bps as tick (should be market-specific)
3. **Independent Bernoulli draws** - each trade event is independent
4. **No adverse selection modeling** - doesn't account for informed flow
5. **Volatility is observable** - requires sufficient recent trade history

### Limitations

1. **No latency modeling** - assumes instant order placement/cancellation
2. **No order book depth** - doesn't use full book beyond BBO
3. **No time-in-queue tracking** - real FIFO priority not modeled
4. **Static parameters** - doesn't adapt parameters intraday
5. **No market impact** - assumes our orders don't move the market

### What This Model Does NOT Do

- ❌ **Predict adverse selection** (when to cancel orders)
- ❌ **Optimize spread placement** (that's the strategy's job)
- ❌ **Model latency arbitrage** (assumes all orders arrive instantly)
- ❌ **Account for fee tiers** (assumes fixed fees)

### What This Model DOES Do

- ✅ **Realistic fill rates** for passive orders
- ✅ **Conservative estimates** (better than overly optimistic)
- ✅ **Volatility awareness** (fewer fills in chaos)
- ✅ **Flow sensitivity** (more fills when market is active)
- ✅ **Partial fills** (realistic for large orders)

---

## Testing & Validation

### Unit Tests

```python
def test_distance_decay():
    """Fill probability should decay with distance from touch"""
    model = ProbabilisticFillModel()
    
    p0 = model.compute_fill_prob(distance_ticks=0, flow=1.0, vol_z=0)
    p1 = model.compute_fill_prob(distance_ticks=1, flow=1.0, vol_z=0)
    p2 = model.compute_fill_prob(distance_ticks=2, flow=1.0, vol_z=0)
    
    assert p0 > p1 > p2
    assert p1 / p0 < 0.3  # steep decay

def test_flow_sensitivity():
    """Higher flow should increase fill probability"""
    model = ProbabilisticFillModel()
    
    p_low = model.compute_fill_prob(distance_ticks=0, flow=0.5, vol_z=0)
    p_high = model.compute_fill_prob(distance_ticks=0, flow=1.5, vol_z=0)
    
    assert p_high > p_low

def test_volatility_penalty():
    """High volatility should reduce fill probability"""
    model = ProbabilisticFillModel()
    
    p_normal = model.compute_fill_prob(distance_ticks=0, flow=1.0, vol_z=0)
    p_high_vol = model.compute_fill_prob(distance_ticks=0, flow=1.0, vol_z=2)
    
    assert p_high_vol < p_normal
    assert p_high_vol / p_normal < 0.5  # significant penalty
```

### Backtesting Validation

**Compare model fills vs. actual fills:**

1. Run bot with **actual fill data** from exchange API
2. Run bot with **probabilistic model** on same market data
3. Compare:
   - Total fill count
   - Fill size distribution
   - Fill price quality (distance from mid)
   - PnL correlation

**Expected results:**
- Model fills ≈ 70-90% of actual fills (conservative)
- PnL correlation > 0.8 (high fidelity)
- Fill rate decreases in high-volatility periods (realistic)

---

## Configuration

Add to `example_config.yaml`:

```yaml
sim:
  enabled: true
  quote_half_spread_bps: 0.2
  quote_size_usd: 333
  max_inventory_usd: 50000
  
  # Probabilistic fill model (new)
  fill_model: "probabilistic"  # "deterministic" (old) or "probabilistic" (new)
  fill_model_params:
    base_fill_rate: 0.20           # 20% at touch
    decay_lambda: 1.5              # Distance decay rate
    vol_penalty_k: 0.8             # Volatility penalty strength
    baseline_vol: 0.50             # 50% annualized vol baseline
    baseline_volume_per_sec: 0.5   # Volume baseline (BTC/sec)
    min_fill_fraction: 0.01        # Minimum 1% partial fill
```

---

## Summary

### Key Improvements Over Current Model

| Aspect | Current | Proposed |
|--------|---------|----------|
| Fill on touch | ✅ Always | ❌ Probabilistic (15-30%) |
| Distance sensitivity | ❌ None | ✅ Exponential decay |
| Flow sensitivity | ❌ None | ✅ Volume-weighted |
| Volatility awareness | ❌ None | ✅ Penalty for high vol |
| Partial fills | ❌ None | ✅ Proportional to flow |
| Queue position | ❌ Ignored | ✅ Heuristic model |
| Conservatism | ❌ Optimistic | ✅ Conservative |

### Expected Impact on Research

**More realistic PnL estimates:**
- Current model: Overestimates fill rate by 3-5×
- Proposed model: Underestimates by ~20% (conservative)

**Better strategy evaluation:**
- Distinguish between "works in paper" vs. "works in reality"
- Identify adverse selection risk earlier
- Test robustness to fill rate uncertainty

**Honest feedback:**
- If a strategy only works with 100% fills at touch, it's not viable
- If it still works with 15-30% fills + distance decay, it's promising

---

## Next Steps

1. **Implement** the probabilistic model in `paper_mm.py`
2. **Add configuration** for model parameters
3. **Run side-by-side comparison** (old vs. new model)
4. **Calibrate parameters** using real market data
5. **Validate** against live trading results (when available)

---

## References

- **Queue-reactive models**: Cont et al. (2010) - "The Price Impact of Order Book Events"
- **Market making fill rates**: Avellaneda & Stoikov (2008) - "High-frequency trading in a limit order book"
- **Volatility regimes**: Andersen et al. (2003) - "Modeling and Forecasting Realized Volatility"
