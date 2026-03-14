"""
Probabilistic fill model for paper market making.

This module implements a conservative, research-grade fill simulation that accounts for:
- Distance from best bid/ask (exponential decay)
- Market flow intensity (volume/trade rate)
- Volatility regime (penalty for unstable markets)
- Queue position (heuristic FIFO model)
- Partial fills (proportional to aggressor volume)

The model is designed to UNDERESTIMATE fills rather than overestimate them,
providing realistic feedback for passive market-making strategies.
"""

import math
import random
from dataclasses import dataclass
from typing import Optional


@dataclass
class FillModelParams:
    """Parameters for the probabilistic fill model."""
    
    # Base fill rate when at touch, normal conditions
    base_fill_rate: float = 0.20  # 20%
    
    # Distance decay: exp(-lambda * distance_ticks)
    decay_lambda: float = 1.5  # Steep decay (22% at 1 tick, 5% at 2 ticks)
    
    # Volatility penalty: 1 / (1 + k * vol_z_score)
    vol_penalty_k: float = 0.8  # Moderate penalty
    
    # Baseline volatility (annualized, for normalization)
    baseline_vol: float = 0.50  # 50% (typical for crypto)
    
    # Baseline volume per second (for flow normalization)
    baseline_volume_per_sec: float = 0.5  # 0.5 BTC/sec (calibrate per market)
    
    # Flow intensity cap (conservative: max 1.5× boost)
    flow_cap: float = 1.5
    
    # Minimum fill fraction (avoid dust fills)
    min_fill_fraction: float = 0.01  # 1% of order
    
    # Tick size (as fraction of mid, e.g., 0.0001 = 1 bps)
    tick_size_bps: float = 1.0  # 1 bps


class ProbabilisticFillModel:
    """
    Conservative probabilistic fill model for passive market making.
    
    Core principle: Better to underestimate fills than create false confidence.
    """
    
    def __init__(self, params: Optional[FillModelParams] = None):
        self.params = params or FillModelParams()
    
    def compute_volatility_z_score(
        self, 
        recent_prices: list[float], 
        baseline_vol: Optional[float] = None
    ) -> float:
        """
        Compute realized volatility z-score from recent trade prices.
        
        Args:
            recent_prices: List of prices (chronological)
            baseline_vol: Expected normal volatility (default: self.params.baseline_vol)
        
        Returns:
            Z-score: (realized_vol - baseline_vol) / (baseline_vol * 0.5)
            Positive = higher than normal volatility
        """
        if baseline_vol is None:
            baseline_vol = self.params.baseline_vol
        
        if len(recent_prices) < 10:
            return 0.0  # Insufficient data, assume normal
        
        # Compute log returns
        returns = []
        for i in range(1, len(recent_prices)):
            if recent_prices[i-1] > 0 and recent_prices[i] > 0:
                ret = math.log(recent_prices[i] / recent_prices[i-1])
                returns.append(ret)
        
        if len(returns) < 2:
            return 0.0
        
        # Standard deviation of returns
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret)**2 for r in returns) / len(returns)
        std_dev = math.sqrt(variance)
        
        # Annualize (assuming prices are ~1 second apart)
        # sqrt(seconds_per_year) = sqrt(365 * 24 * 60 * 60) ≈ 5615
        realized_vol = std_dev * math.sqrt(31536000)  # 365 * 24 * 60 * 60
        
        # Z-score
        z_score = (realized_vol - baseline_vol) / (baseline_vol * 0.5)
        
        return z_score
    
    def compute_flow_intensity(
        self, 
        recent_volume: float, 
        lookback_seconds: float,
        baseline_volume_per_sec: Optional[float] = None
    ) -> float:
        """
        Compute flow intensity factor from recent trading volume.
        
        Args:
            recent_volume: Total volume in lookback window
            lookback_seconds: Lookback window size
            baseline_volume_per_sec: Expected normal volume rate
        
        Returns:
            Flow factor (≥ 0.1, capped at self.params.flow_cap)
            > 1.0 = higher than normal flow
        """
        if baseline_volume_per_sec is None:
            baseline_volume_per_sec = self.params.baseline_volume_per_sec
        
        if lookback_seconds <= 0:
            return 1.0
        
        volume_per_sec = recent_volume / lookback_seconds
        
        # Normalize and cap
        flow_factor = volume_per_sec / baseline_volume_per_sec
        flow_factor = min(self.params.flow_cap, flow_factor)
        
        # Floor at 0.1 to avoid zero probability
        return max(0.1, flow_factor)
    
    def compute_queue_position_factor(self, distance_ticks: float) -> float:
        """
        Heuristic queue position factor based on distance from touch.
        
        Intuition:
        - At touch: Many competitors, assume middle of queue (0.5)
        - 1 tick behind: Fewer orders, assume early if flow reaches (0.8)
        - 2+ ticks: Very unlikely unless massive flow (0.3)
        
        Args:
            distance_ticks: Distance from best price in ticks
        
        Returns:
            Queue position factor (0 < Q ≤ 1)
        """
        if distance_ticks < 0.5:
            return 0.5  # At touch, middle of queue
        elif distance_ticks < 1.5:
            return 0.8  # 1 tick behind, early in queue
        else:
            return 0.3  # 2+ ticks, low priority
    
    def compute_fill_probability(
        self,
        my_price: float,
        best_price: float,
        mid_price: float,
        trade_price: float,
        trade_qty: float,
        my_qty: float,
        recent_prices: list[float],
        recent_volume: float,
        lookback_seconds: float,
    ) -> tuple[float, dict]:
        """
        Compute fill probability for a passive market-making order.
        
        Args:
            my_price: My order price
            best_price: Current best bid/ask
            mid_price: Mid price
            trade_price: Aggressor trade price
            trade_qty: Aggressor trade quantity
            my_qty: My order quantity
            recent_prices: Recent trade prices (for volatility)
            recent_volume: Recent volume (for flow intensity)
            lookback_seconds: Lookback window for volume
        
        Returns:
            (fill_probability, debug_info)
        """
        # Compute tick size
        tick_size = mid_price * (self.params.tick_size_bps / 10000.0)
        
        # Distance in ticks
        distance_ticks = abs(my_price - best_price) / tick_size
        
        # Component 1: Distance decay
        distance_decay = math.exp(-self.params.decay_lambda * distance_ticks)
        
        # Component 2: Flow intensity
        flow_intensity = self.compute_flow_intensity(
            recent_volume, 
            lookback_seconds,
            self.params.baseline_volume_per_sec
        )
        
        # Component 3: Volatility penalty
        vol_z_score = self.compute_volatility_z_score(
            recent_prices,
            self.params.baseline_vol
        )
        vol_penalty = 1.0 / (1.0 + self.params.vol_penalty_k * max(0, vol_z_score))
        
        # Component 4: Queue position
        queue_factor = self.compute_queue_position_factor(distance_ticks)
        
        # Combined fill probability
        fill_prob = (
            self.params.base_fill_rate 
            * distance_decay 
            * flow_intensity 
            * vol_penalty 
            * queue_factor
        )
        
        # Cap at 100%
        fill_prob = min(1.0, fill_prob)
        
        # Debug info
        debug_info = {
            "distance_ticks": distance_ticks,
            "distance_decay": distance_decay,
            "flow_intensity": flow_intensity,
            "vol_z_score": vol_z_score,
            "vol_penalty": vol_penalty,
            "queue_factor": queue_factor,
            "fill_prob": fill_prob,
        }
        
        return fill_prob, debug_info
    
    def compute_partial_fill_size(
        self, 
        fill_prob: float,
        my_qty: float,
        trade_qty: float,
    ) -> Optional[float]:
        """
        Determine fill size using Bernoulli draw + partial fill factor.
        
        Args:
            fill_prob: Computed fill probability
            my_qty: My order quantity
            trade_qty: Aggressor trade quantity
        
        Returns:
            Fill quantity (None if no fill)
        """
        # Bernoulli draw
        if random.random() > fill_prob:
            return None  # No fill
        
        # Maximum possible fill
        max_fill = min(my_qty, trade_qty)
        
        # Partial fill factor (conservative: fill only a fraction)
        # Intuition: Small aggressor → small fill, Large aggressor → potentially full fill
        partial_factor = min(1.0, trade_qty / (2.0 * my_qty))
        
        fill_qty = max_fill * partial_factor
        
        # Enforce minimum fill size
        min_fill = self.params.min_fill_fraction * my_qty
        if fill_qty < min_fill:
            return None
        
        return fill_qty
    
    def should_fill(
        self,
        my_price: float,
        best_price: float,
        mid_price: float,
        trade_price: float,
        trade_qty: float,
        my_qty: float,
        side: str,  # "BUY" (aggressor buying) or "SELL" (aggressor selling)
        my_side: str,  # "BID" or "ASK"
        recent_prices: list[float],
        recent_volume: float,
        lookback_seconds: float,
    ) -> tuple[Optional[float], dict]:
        """
        Main entry point: Determine if order fills and by how much.
        
        Args:
            my_price: My order price
            best_price: Current best bid/ask on my side
            mid_price: Mid price
            trade_price: Aggressor trade price
            trade_qty: Aggressor trade quantity
            my_qty: My order quantity
            side: Aggressor side ("BUY" or "SELL")
            my_side: My order side ("BID" or "ASK")
            recent_prices: Recent trade prices (for volatility)
            recent_volume: Recent volume (for flow intensity)
            lookback_seconds: Lookback window
        
        Returns:
            (fill_qty, debug_info) or (None, debug_info) if no fill
        """
        # Direction filter: Only fill if aggressor trades THROUGH our price
        if my_side == "ASK" and side == "BUY":
            if trade_price < my_price:
                return None, {"reason": "aggressor_didnt_reach_ask"}
        
        if my_side == "BID" and side == "SELL":
            if trade_price > my_price:
                return None, {"reason": "aggressor_didnt_reach_bid"}
        
        # Compute fill probability
        fill_prob, debug_info = self.compute_fill_probability(
            my_price=my_price,
            best_price=best_price,
            mid_price=mid_price,
            trade_price=trade_price,
            trade_qty=trade_qty,
            my_qty=my_qty,
            recent_prices=recent_prices,
            recent_volume=recent_volume,
            lookback_seconds=lookback_seconds,
        )
        
        # Compute fill size (with Bernoulli draw)
        fill_qty = self.compute_partial_fill_size(fill_prob, my_qty, trade_qty)
        
        if fill_qty is None:
            debug_info["reason"] = "bernoulli_no_fill"
        else:
            debug_info["fill_qty"] = fill_qty
        
        return fill_qty, debug_info


def extract_market_data_from_tape(tape, lookback_seconds: float = 60.0):
    """
    Helper function to extract recent prices and volume from TradeTape.
    
    Args:
        tape: TradeTape instance
        lookback_seconds: Lookback window
    
    Returns:
        (recent_prices, recent_volume)
    """
    recent_trades = tape.recent(lookback_seconds)
    
    if not recent_trades:
        return [], 0.0
    
    recent_prices = [price for (ts_ms, price, qty, side) in recent_trades]
    recent_volume = sum(qty for (ts_ms, price, qty, side) in recent_trades)
    
    return recent_prices, recent_volume
