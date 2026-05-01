#indicators.py
from typing import Dict, List
from models import StrategyConfig


class Indicators:
    """Calculate technical indicators from price data."""
    
    def __init__(self, config: StrategyConfig):
        self.config = config
        self._cache: Dict = {}
    
    @property
    def cache(self) -> Dict:
        return self._cache
    
    def calculate_all(self, bars: List[Dict]) -> bool:
        """Calculate all indicators. Returns True if enough data."""
        if len(bars) < self.config.super_long_ma_length:
            return False
        
        closes = [b['close'] for b in bars]
        highs = [b['high'] for b in bars]
        lows = [b['low'] for b in bars]
        
        # Moving averages
        self._cache['short_ma'] = self._sma(closes, self.config.short_ma_length)
        self._cache['long_ma'] = self._sma(closes, self.config.long_ma_length)
        self._cache['super_long_ma'] = self._sma(closes, self.config.super_long_ma_length)
        
        # ATR
        self._cache['atr'] = self._atr(highs, lows, closes, self.config.atr_length)
        
        # RSI
        self._cache['rsi'] = self._rsi(closes, self.config.rsi_length)
        
        # MACD
        self._cache['macd'] = self._macd(
            closes,
            self.config.macd_fast,
            self.config.macd_slow,
            self.config.macd_signal
        )
        
        # Momentum
        self._cache['momentum'] = self._momentum(closes, self.config.short_ma_length)
        
        # Swing high/low for anchor
        lookback = self.config.lookback_for_anchor
        self._cache['swing_high'] = max(highs[-lookback:])
        self._cache['swing_low'] = min(lows[-lookback:])
        
        return True
    
    def _sma(self, data: List[float], length: int) -> float:
        """Simple Moving Average."""
        if len(data) < length:
            return data[-1] if data else 0.0
        return sum(data[-length:]) / length
    
    def _ema(self, data: List[float], length: int) -> List[float]:
        """Exponential Moving Average - returns full series."""
        if len(data) < length:
            return data.copy()
        
        multiplier = 2 / (length + 1)
        ema_values = [sum(data[:length]) / length]  # Start with SMA
        
        for price in data[length:]:
            ema_values.append((price - ema_values[-1]) * multiplier + ema_values[-1])
        
        return ema_values
    
    def _atr(self, highs: List[float], lows: List[float], closes: List[float], length: int) -> float:
        """Average True Range."""
        if len(closes) < length + 1:
            return highs[-1] - lows[-1] if highs and lows else 0.0
        
        true_ranges = []
        for i in range(-length, 0):
            high = highs[i]
            low = lows[i]
            prev_close = closes[i - 1]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
        
        return sum(true_ranges) / length
    
    def _rsi(self, closes: List[float], length: int) -> float:
        """Relative Strength Index."""
        if len(closes) < length + 1:
            return 50.0
        
        changes = [closes[i] - closes[i-1] for i in range(-length, 0)]
        gains = [c if c > 0 else 0 for c in changes]
        losses = [-c if c < 0 else 0 for c in changes]
        
        avg_gain = sum(gains) / length
        avg_loss = sum(losses) / length
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    def _macd(self, closes: List[float], fast: int, slow: int, signal: int) -> Dict:
        """MACD indicator."""
        if len(closes) < slow + signal:
            return {'macd': 0.0, 'signal': 0.0, 'histogram': 0.0}
        
        ema_fast = self._ema(closes, fast)
        ema_slow = self._ema(closes, slow)
        
        # Align lengths
        min_len = min(len(ema_fast), len(ema_slow))
        macd_line = [ema_fast[-(min_len-i)] - ema_slow[-(min_len-i)] for i in range(min_len)]
        
        signal_line = self._ema(macd_line, signal)
        
        current_macd = macd_line[-1] if macd_line else 0.0
        current_signal = signal_line[-1] if signal_line else 0.0
        
        return {
            'macd': current_macd,
            'signal': current_signal,
            'histogram': current_macd - current_signal
        }
    
    def _momentum(self, closes: List[float], length: int) -> float:
        """Price momentum."""
        if len(closes) < length:
            return 0.0
        return closes[-1] - closes[-length]