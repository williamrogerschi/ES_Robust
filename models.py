"""
Data models for ES Futures Grid Trading Strategy
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from enum import Enum


class TrendState(Enum):
    STRONG_BULLISH = "strong_bullish"
    MODERATE_BULLISH = "moderate_bullish"
    STRONG_BEARISH = "strong_bearish"
    MODERATE_BEARISH = "moderate_bearish"
    SIDEWAYS = "sideways"


@dataclass
class Position:
    """Tracks an open position with its associated IB orders."""
    side: str  # 'long' or 'short'
    entry_price: float
    size: float
    stop_loss: float
    take_profit: float
    trailing_stop: Optional[float]
    entry_time: datetime
    grid_level: float
    order_id: Optional[int] = None

    # Native IB order tracking
    stop_order_id: Optional[int] = None
    tp_order_id: Optional[int] = None

    # Trailing stop state
    trailing_activated: bool = False
    highest_price: Optional[float] = None  # For long positions
    lowest_price: Optional[float] = None   # For short positions


@dataclass
class PendingOrder:
    """Tracks a pending limit order waiting for fill."""
    order_id: int
    side: str  # 'long' or 'short'
    limit_price: float
    size: float
    stop_loss: float
    take_profit: float
    trailing_stop: Optional[float]
    submit_time: datetime
    grid_level: float


@dataclass
class StrategyConfig:
    # Instrument settings
    tick_size: float = 0.25

    # Entry mode
    use_grid_entry: bool = True

    # Grid settings
    base_grid_pct: float = 0.10
    max_positions: int = 1
    use_volatility_grid: bool = True
    max_anchor_distance_grids: int = 3
    lookback_for_anchor: int = 20

    # ATR settings
    atr_length: int = 14
    atr_multiplier: float = 1.5

    # RSI settings
    rsi_length: int = 14
    rsi_overbought: int = 70
    rsi_oversold: int = 30

    # RSI entry thresholds
    entry_rsi_bearish: float = 55.0
    entry_rsi_bullish: float = 45.0
    entry_rsi_sideways_short: float = 70.0
    entry_rsi_sideways_long: float = 30.0

    # MA settings
    short_ma_length: int = 20
    long_ma_length: int = 50
    super_long_ma_length: int = 200

    # MACD settings
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    # Trend confirmation
    trend_confirmation_bars: int = 3

    # Risk management - points-based
    stop_loss_pts: float = 8.0
    take_profit_pts: float = 12.0
    trailing_activation_pts: float = 5.0
    trailing_distance_pts: float = 5.0

    # Risk management - pct-based
    stop_loss_pct: float = 0.117
    take_profit_pct: float = 0.175
    use_trailing_stop: bool = True
    trailing_stop_pct: float = 0.073

    max_loss_per_day_pct: float = 2.0

    # Trend reversal exit
    use_trend_reversal_exit: bool = False
    trend_cooldown_minutes: int = 5

    # Time-based exit
    time_based_exit: bool = True
    max_holding_hours: int = 4

    # Position sizing
    use_risk_based_position: bool = False
    risk_per_trade_pct: float = 1.0
    max_leverage: float = 3.0
    contracts_per_trade: int = 1

    # ATR-based position sizing
    # If ATR >= atr_high_volatility_threshold, use reduced contracts
    atr_high_volatility_threshold: float = 4.5
    contracts_per_trade_high_vol: int = 5

    # Grid mode stop
    use_grid_stop: bool = False
    grid_stop_buffer_pts: float = 6.0

    # Stop-limit offset: how far below (long) or above (short) the stop price to set the limit
    # Prevents catastrophic slippage on stop fills — set to 0 to use plain market stop
    stop_limit_offset_pts: float = 4.0

    # Trend-following entry (scalp_aggressive only)
    use_trend_follow_entry: bool = False
    trend_follow_rsi_long: float = 45.0
    trend_follow_rsi_short: float = 55.0
    trend_follow_allow_moderate: bool = False
    post_exit_cooldown_bars: int = 2

    # scalp_robust filters
    use_session_filter: bool = False
    session_start_hour: int = 9
    session_start_minute: int = 30
    session_end_hour: int = 12
    session_end_minute: int = 0
    use_5m_filter: bool = False
    use_volume_filter: bool = False
    volume_spike_multiplier: float = 1.5
    volume_lookback: int = 20

    # Session low short filter (scalp_robust only)
    use_session_low_short_filter: bool = False
    session_low_short_buffer: float = 10.0   # pts above session low to block shorts
    session_low_short_hours: float = 2.0     # hours after open to apply filter

    # Account
    initial_equity: float = 100000.0


# =============================================================================
# CONFIG PRESETS
# =============================================================================

def get_scalp_config() -> StrategyConfig:
    return StrategyConfig(
        use_grid_entry=False,
        max_positions=1,
        base_grid_pct=0.08,
        use_volatility_grid=True,
        atr_multiplier=1.2,
        max_anchor_distance_grids=2,
        stop_loss_pct=0.12,
        take_profit_pct=0.18,
        trailing_stop_pct=0.07,
        trailing_activation_pts=6.0,
        trailing_distance_pts=6.0,
        use_trailing_stop=True,
        max_loss_per_day_pct=100.0,
        trend_confirmation_bars=2,
        use_trend_reversal_exit=False,
        entry_rsi_bearish=55.0,
        entry_rsi_bullish=45.0,
        entry_rsi_sideways_short=70.0,
        entry_rsi_sideways_long=30.0,
        contracts_per_trade=10,
        atr_high_volatility_threshold=4.5,
        contracts_per_trade_high_vol=5,
        use_session_filter=True,
        session_start_hour=8,
        session_start_minute=30,
        session_end_hour=14,
        session_end_minute=30,
    )


def get_grid_config() -> StrategyConfig:
    return StrategyConfig(
        use_grid_entry=True,
        max_positions=3,
        base_grid_pct=0.12,
        use_volatility_grid=True,
        atr_multiplier=1.5,
        max_anchor_distance_grids=3,
        take_profit_pts=12.0,
        use_trailing_stop=False,
        max_loss_per_day_pct=2.0,
        trend_confirmation_bars=3,
        use_trend_reversal_exit=False,
        entry_rsi_bearish=60.0,
        entry_rsi_bullish=40.0,
        entry_rsi_sideways_short=70.0,
        entry_rsi_sideways_long=30.0,
        contracts_per_trade=3,
        use_grid_stop=True,
        grid_stop_buffer_pts=6.0,
        atr_high_volatility_threshold=4.5,
        contracts_per_trade_high_vol=5,
    )


def get_scalp_robust_config() -> StrategyConfig:
    return StrategyConfig(
        use_grid_entry=False,
        max_positions=1,
        base_grid_pct=0.08,
        use_volatility_grid=True,
        atr_multiplier=1.2,
        max_anchor_distance_grids=2,
        stop_loss_pct=0.12,
        take_profit_pct=0.18,
        trailing_stop_pct=0.07,
        trailing_activation_pts=6.0,
        trailing_distance_pts=6.0,
        use_trailing_stop=True,
        max_loss_per_day_pct=100.0,
        trend_confirmation_bars=2,
        use_trend_reversal_exit=False,
        entry_rsi_bearish=55.0,
        entry_rsi_bullish=45.0,
        entry_rsi_sideways_short=70.0,
        entry_rsi_sideways_long=30.0,
        contracts_per_trade=10,
        atr_high_volatility_threshold=4.5,
        contracts_per_trade_high_vol=5,
        use_session_filter=False,
        session_start_hour=8,
        session_start_minute=30,
        session_end_hour=12,
        session_end_minute=0,
        use_5m_filter=True,
        use_volume_filter=False,
        volume_spike_multiplier=1.2,
        volume_lookback=50,
        use_session_low_short_filter=True,
        session_low_short_buffer=10.0,
        session_low_short_hours=2.0,
    )


CONFIG_PRESETS = {
    'scalp': get_scalp_config,
    'scalp_robust': get_scalp_robust_config,
    'grid': get_grid_config,
}