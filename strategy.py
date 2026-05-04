"""
ES Futures Grid Trading Strategy - ROBUST VERSION
Scalp robust mode only — includes MACD momentum filter.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from models import TrendState, Position, StrategyConfig, PendingOrder
from indicators import Indicators

UTC = ZoneInfo("UTC")
CENTRAL = ZoneInfo("America/Chicago")


class GridStrategy:
    def __init__(self, broker, config: StrategyConfig = None):
        self.broker = broker
        self.config = config or StrategyConfig()
        
        # Price data
        self.bars: List[Dict] = []
        self.last_price: float = 0.0
        
        # Indicators
        self.indicators = Indicators(self.config)
        
        # Trend tracking
        self.current_trend = TrendState.SIDEWAYS
        self.previous_trend = TrendState.SIDEWAYS
        self.confirmed_trend = TrendState.SIDEWAYS
        self.trend_history: List[TrendState] = []
        
        # Grid state
        self.grid_anchor_price: Optional[float] = None
        self.grid_anchor_time: Optional[datetime] = None
        self.grid_levels: List[float] = []
        
        # Position tracking
        self.positions: List[Position] = []
        self.position_count: int = 0
        
        # PENDING ORDER TRACKING - orders submitted but not yet filled
        self.pending_orders: Dict[int, PendingOrder] = {}  # order_id -> PendingOrder
        
        # P&L tracking
        self.equity = self.config.initial_equity
        self.daily_pnl: float = 0.0
        self.last_reset_day: Optional[int] = None
        self.prev_rsi: float = 50.0

        # --- MACD MOMENTUM FILTER ---
        # Stores the MACD value from the previous bar so we can check
        # whether MACD is rising or falling when an entry signal fires.
        self._prev_macd: float = 0.0

        # Tracks every trade the MACD filter blocks for end-of-session analysis
        self._macd_blocked_trades: List[Dict] = []

        self.bars_since_exit: int = 999  # Starts high so first entry is always allowed

        # Session low tracking for scalp_robust short filter
        self._session_low: float = float('inf')
        self._session_low_date: Optional[int] = None

        # 5-minute bar aggregation
        self.bars_5m: List[Dict] = []
        self.indicators_5m = Indicators(self.config)
        self.current_trend_5m: TrendState = TrendState.SIDEWAYS
        self._5m_bar_buffer: List[Dict] = []  # accumulates 1-min bars until 5 collected

    def _round_to_tick(self, price: float) -> float:
        return round(price / self.config.tick_size) * self.config.tick_size

    def _determine_trend(self) -> TrendState:
        ind = self.indicators.cache
        bullish_score = 0
        bearish_score = 0
        if ind['short_ma'] > ind['long_ma']:
            bullish_score += 20
        else:
            bearish_score += 20
        if ind['long_ma'] > ind['super_long_ma']:
            bullish_score += 30
        else:
            bearish_score += 30
        macd = ind['macd']
        if macd['macd'] > macd['signal'] and macd['macd'] > 0:
            bullish_score += 25
        elif macd['macd'] < macd['signal'] and macd['macd'] < 0:
            bearish_score += 25
        if ind['momentum'] > 0:
            bullish_score += 25
        else:
            bearish_score += 25
        if bullish_score >= 70:
            return TrendState.STRONG_BULLISH
        elif bullish_score >= 40:
            return TrendState.MODERATE_BULLISH
        elif bearish_score >= 70:
            return TrendState.STRONG_BEARISH
        elif bearish_score >= 40:
            return TrendState.MODERATE_BEARISH
        else:
            return TrendState.SIDEWAYS

    def _determine_trend_5m(self) -> TrendState:
        if not self.indicators_5m.cache:
            return TrendState.SIDEWAYS
        ind = self.indicators_5m.cache
        bullish_score = 0
        bearish_score = 0
        if ind['short_ma'] > ind['long_ma']:
            bullish_score += 20
        else:
            bearish_score += 20
        if ind['long_ma'] > ind['super_long_ma']:
            bullish_score += 30
        else:
            bearish_score += 30
        macd = ind['macd']
        if macd['macd'] > macd['signal'] and macd['macd'] > 0:
            bullish_score += 25
        elif macd['macd'] < macd['signal'] and macd['macd'] < 0:
            bearish_score += 25
        if ind['momentum'] > 0:
            bullish_score += 25
        else:
            bearish_score += 25
        if bullish_score >= 70:
            return TrendState.STRONG_BULLISH
        elif bullish_score >= 40:
            return TrendState.MODERATE_BULLISH
        elif bearish_score >= 70:
            return TrendState.STRONG_BEARISH
        elif bearish_score >= 40:
            return TrendState.MODERATE_BEARISH
        else:
            return TrendState.SIDEWAYS

    def _get_confirmed_trend(self) -> TrendState:
        self.trend_history.append(self.current_trend)
        if len(self.trend_history) > self.config.trend_confirmation_bars:
            self.trend_history.pop(0)
        if len(self.trend_history) < self.config.trend_confirmation_bars:
            return self.confirmed_trend
        if all(t == self.trend_history[0] for t in self.trend_history):
            self.confirmed_trend = self.trend_history[0]
        return self.confirmed_trend

    def _calculate_grid_size(self) -> float:
        base = self.config.base_grid_pct
        if self.config.use_volatility_grid and 'atr' in self.indicators.cache:
            atr = self.indicators.cache['atr']
            atr_pct = (atr / self.last_price) * 100
            return max(base, atr_pct * self.config.atr_multiplier)
        return base

    def _should_reset_grid_anchor(self) -> bool:
        if self.confirmed_trend != self.previous_trend:
            return True
        if self.position_count == 0 and self.grid_anchor_time:
            age = datetime.now(UTC) - self.grid_anchor_time
            if age > timedelta(minutes=30):
                return True
        if self.grid_anchor_price and self.position_count == 0:
            distance_pct = abs(self.last_price - self.grid_anchor_price) / self.last_price * 100
            grid_size = self._calculate_grid_size()
            if distance_pct > grid_size * self.config.max_anchor_distance_grids:
                return True
        return False

    def _set_grid_anchor(self):
        trend = self.confirmed_trend
        grid_size = self._calculate_grid_size()
        max_distance = self.last_price * (grid_size / 100) * self.config.max_anchor_distance_grids
        if trend in [TrendState.STRONG_BEARISH, TrendState.MODERATE_BEARISH]:
            swing = self.indicators.cache.get('swing_high', self.last_price)
            if swing > self.last_price + max_distance:
                self.grid_anchor_price = self.last_price + max_distance
            else:
                self.grid_anchor_price = swing
        elif trend in [TrendState.STRONG_BULLISH, TrendState.MODERATE_BULLISH]:
            swing = self.indicators.cache.get('swing_low', self.last_price)
            if swing < self.last_price - max_distance:
                self.grid_anchor_price = self.last_price - max_distance
            else:
                self.grid_anchor_price = swing
        else:
            self.grid_anchor_price = self.last_price
        self.grid_anchor_price = self._round_to_tick(self.grid_anchor_price)
        self.grid_anchor_time = datetime.now(UTC)
        print(f"  🎯 Grid anchor set @ {self.grid_anchor_price:.2f} ({trend.value})")

    def _calculate_grid_levels(self) -> List[float]:
        if not self.grid_anchor_price:
            return []
        grid_size = self._calculate_grid_size()
        grid_step = self.last_price * (grid_size / 100)
        grid_step = self._round_to_tick(grid_step)
        levels = []
        trend = self.confirmed_trend
        if trend in [TrendState.STRONG_BEARISH, TrendState.MODERATE_BEARISH]:
            for i in range(self.config.max_positions):
                level = self._round_to_tick(self.grid_anchor_price + (i * grid_step))
                levels.append(level)
        elif trend in [TrendState.STRONG_BULLISH, TrendState.MODERATE_BULLISH]:
            for i in range(self.config.max_positions):
                level = self._round_to_tick(self.grid_anchor_price - (i * grid_step))
                levels.append(level)
        else:
            for i in range(self.config.max_positions):
                levels.append(self._round_to_tick(self.grid_anchor_price + ((i + 1) * grid_step)))
                levels.append(self._round_to_tick(self.grid_anchor_price - ((i + 1) * grid_step)))
        return sorted(levels)

    def _calculate_position_size(self, entry_price: float) -> float:
        if self.config.use_risk_based_position:
            risk_amount = self.equity * (self.config.risk_per_trade_pct / 100)
            stop_distance = entry_price * (self.config.stop_loss_pct / 100)
            size = risk_amount / stop_distance
        else:
            size = 1.0
        max_size = (self.equity * self.config.max_leverage) / entry_price
        return min(size, max_size)

    def _get_contracts(self) -> int:
        """Returns contract size based on current ATR.
        Drops to reduced size when ATR exceeds high volatility threshold.
        """
        atr = self.indicators.cache.get('atr', 0)
        if atr >= self.config.atr_high_volatility_threshold:
            return self.config.contracts_per_trade_high_vol
        return self.config.contracts_per_trade

    # -------------------------------------------------------------------------
    # MACD MOMENTUM FILTER
    # -------------------------------------------------------------------------
    def _macd_momentum_ok(self, direction: str) -> bool:
        """Returns True if MACD is moving in the same direction as the trade.

        For a LONG: we want MACD rising (current >= previous).
        For a SHORT: we want MACD falling (current <= previous).

        Flat (current == previous) is treated as a pass — blocking on zero
        change is too strict and essentially never happens with floating point
        EMA calculations anyway.
        """
        current_macd = self.indicators.cache.get('macd', {}).get('macd', 0.0)
        if direction == 'long':
            return current_macd >= self._prev_macd
        elif direction == 'short':
            return current_macd <= self._prev_macd
        return True

    async def _check_entries_scalp(self, bar: Dict):
        total_orders = self.position_count + len(self.pending_orders)
        if total_orders >= self.config.max_positions:
            return
        if self.bars_since_exit < self.config.post_exit_cooldown_bars:
            remaining = self.config.post_exit_cooldown_bars - self.bars_since_exit
            print(f"  ⏸️ Cooldown: {remaining} bar(s) remaining after exit")
            return
        if len(self.bars) < 2:
            return
        if self.config.use_session_filter:
            bar_ct = bar['time'].astimezone(CENTRAL)
            session_start = bar_ct.replace(hour=self.config.session_start_hour, minute=self.config.session_start_minute, second=0, microsecond=0)
            session_end = bar_ct.replace(hour=self.config.session_end_hour, minute=self.config.session_end_minute, second=0, microsecond=0)
            if not (session_start <= bar_ct < session_end):
                print(f"  🕐 Outside session ({bar_ct.strftime('%H:%M')} CT) — no entries")
                return
        if self.config.use_5m_filter:
            def direction(t):
                if t in [TrendState.STRONG_BULLISH, TrendState.MODERATE_BULLISH]:
                    return 'bull'
                elif t in [TrendState.STRONG_BEARISH, TrendState.MODERATE_BEARISH]:
                    return 'bear'
                return 'sideways'
            trend_1m_dir = direction(self.confirmed_trend)
            trend_5m_dir = direction(self.current_trend_5m)
            if trend_5m_dir != 'sideways' and trend_1m_dir != trend_5m_dir:
                print(f"  🚫 5m trend mismatch: 1m={self.confirmed_trend.value} vs 5m={self.current_trend_5m.value}")
                return
        if self.config.use_volume_filter and len(self.bars) >= self.config.volume_lookback:
            recent_vols = [b['volume'] for b in self.bars[-self.config.volume_lookback:]]
            avg_vol = sum(recent_vols) / len(recent_vols)
            if avg_vol > 0 and bar['volume'] < self.config.volume_spike_multiplier * avg_vol:
                threshold = self.config.volume_spike_multiplier * avg_vol
                print(f"  🔇 Low volume ({bar['volume']:.0f} < threshold {threshold:.0f} | avg {avg_vol:.0f}) — skipping")
                return
        trend = self.confirmed_trend
        rsi = self.indicators.cache.get('rsi', 50)
        current_price = bar['close']
        prev_bar = self.bars[-2]
        current_macd = self.indicators.cache.get('macd', {}).get('macd', 0.0)

        # RAW TREND CONTRADICTION FILTER
        # Block any trade where the raw (unconfirmed) trend directly contradicts
        # the intended trade direction. Prevents entering against clear momentum
        # even when confirmed trend hasn't caught up yet.
        raw_is_bullish = self.current_trend in [TrendState.STRONG_BULLISH, TrendState.MODERATE_BULLISH]
        raw_is_bearish = self.current_trend in [TrendState.STRONG_BEARISH, TrendState.MODERATE_BEARISH]
        confirmed_wants_short = trend in [TrendState.STRONG_BEARISH, TrendState.MODERATE_BEARISH] or trend == TrendState.SIDEWAYS
        confirmed_wants_long = trend in [TrendState.STRONG_BULLISH, TrendState.MODERATE_BULLISH] or trend == TrendState.SIDEWAYS

        def _raw_trend_blocks_short() -> bool:
            return raw_is_bullish

        def _raw_trend_blocks_long() -> bool:
            return raw_is_bearish

        def _short_blocked_by_session_low() -> bool:
            if not self.config.use_session_low_short_filter:
                return False
            bar_ct = bar['time'].astimezone(CENTRAL)
            session_open = bar_ct.replace(hour=8, minute=30, second=0, microsecond=0)
            hours_since_open = (bar_ct - session_open).total_seconds() / 3600
            if hours_since_open < 0 or hours_since_open > self.config.session_low_short_hours:
                return False
            if self._session_low == float('inf'):
                return False
            pts_above_low = current_price - self._session_low
            if pts_above_low > self.config.session_low_short_buffer:
                print(f"  🚫 Short blocked: price {current_price:.2f} is {pts_above_low:.1f} pts above session low {self._session_low:.2f} (limit: {self.config.session_low_short_buffer} pts, window: {self.config.session_low_short_hours:.1f} hrs)")
                return True
            return False

        if trend in [TrendState.STRONG_BEARISH, TrendState.MODERATE_BEARISH]:
            if rsi > self.config.entry_rsi_bearish:
                if current_price < prev_bar['low']:
                    if not _short_blocked_by_session_low():
                        if _raw_trend_blocks_short():
                            print(f"  🚫 Raw trend blocks SHORT | raw: {self.current_trend.value}")
                            return
                        if not self._macd_momentum_ok('short'):
                            print(f"  ⏭️ MACD filter blocked SHORT | MACD: {current_macd:.2f} vs prev {self._prev_macd:.2f}")
                            self._macd_blocked_trades.append({
                                'time': bar['time'],
                                'direction': 'short',
                                'macd': current_macd,
                                'prev_macd': self._prev_macd,
                                'price': current_price,
                                'rsi': rsi,
                                'trend': trend.value
                            })
                            return
                        await self._enter_short(current_price, rsi, trend)
        elif trend in [TrendState.STRONG_BULLISH, TrendState.MODERATE_BULLISH]:
            if rsi < self.config.entry_rsi_bullish:
                if current_price > prev_bar['high']:
                    if _raw_trend_blocks_long():
                        print(f"  🚫 Raw trend blocks LONG | raw: {self.current_trend.value}")
                        return
                    if not self._macd_momentum_ok('long'):
                        print(f"  ⏭️ MACD filter blocked LONG | MACD: {current_macd:.2f} vs prev {self._prev_macd:.2f}")
                        self._macd_blocked_trades.append({
                            'time': bar['time'],
                            'direction': 'long',
                            'macd': current_macd,
                            'prev_macd': self._prev_macd,
                            'price': current_price,
                            'rsi': rsi,
                            'trend': trend.value
                        })
                        return
                    await self._enter_long(current_price, rsi, trend)
        elif trend == TrendState.SIDEWAYS:
            if rsi > self.config.entry_rsi_sideways_short:
                if current_price < prev_bar['low']:
                    if not _short_blocked_by_session_low():
                        if _raw_trend_blocks_short():
                            print(f"  🚫 Raw trend blocks SHORT (sideways) | raw: {self.current_trend.value}")
                            return
                        if not self._macd_momentum_ok('short'):
                            print(f"  ⏭️ MACD filter blocked SHORT (sideways) | MACD: {current_macd:.2f} vs prev {self._prev_macd:.2f}")
                            self._macd_blocked_trades.append({
                                'time': bar['time'],
                                'direction': 'short',
                                'macd': current_macd,
                                'prev_macd': self._prev_macd,
                                'price': current_price,
                                'rsi': rsi,
                                'trend': trend.value
                            })
                            return
                        await self._enter_short(current_price, rsi, trend)
            elif rsi < self.config.entry_rsi_sideways_long:
                if current_price > prev_bar['high']:
                    if _raw_trend_blocks_long():
                        print(f"  🚫 Raw trend blocks LONG (sideways) | raw: {self.current_trend.value}")
                        return
                    if not self._macd_momentum_ok('long'):
                        print(f"  ⏭️ MACD filter blocked LONG (sideways) | MACD: {current_macd:.2f} vs prev {self._prev_macd:.2f}")
                        self._macd_blocked_trades.append({
                            'time': bar['time'],
                            'direction': 'long',
                            'macd': current_macd,
                            'prev_macd': self._prev_macd,
                            'price': current_price,
                            'rsi': rsi,
                            'trend': trend.value
                        })
                        return
                    await self._enter_long(current_price, rsi, trend)

        if self.config.use_trend_follow_entry and self.position_count == 0 and not self.pending_orders:
            macd = self.indicators.cache.get('macd', {}).get('macd', 0)
            rsi_rising = rsi > self.prev_rsi
            rsi_falling = rsi < self.prev_rsi
            strong_long = trend == TrendState.STRONG_BULLISH
            strong_short = trend == TrendState.STRONG_BEARISH
            moderate_long = (self.config.trend_follow_allow_moderate and trend == TrendState.MODERATE_BULLISH and current_price > self.grid_anchor_price and rsi_rising and macd > 0)
            moderate_short = (self.config.trend_follow_allow_moderate and trend == TrendState.MODERATE_BEARISH and current_price < self.grid_anchor_price and rsi_falling and macd < 0)
            if (strong_long or moderate_long) and rsi < self.config.trend_follow_rsi_long:
                reason = "strong" if strong_long else "moderate+gates"
                print(f"  🚀 Trend-follow LONG [{reason}] | RSI: {rsi:.1f} | MACD: {macd:.2f}")
                await self._enter_long(current_price, rsi, trend)
            elif (strong_short or moderate_short) and rsi > self.config.trend_follow_rsi_short:
                reason = "strong" if strong_short else "moderate+gates"
                print(f"  🚀 Trend-follow SHORT [{reason}] | RSI: {rsi:.1f} | MACD: {macd:.2f}")
                await self._enter_short(current_price, rsi, trend)

    async def _enter_long(self, level: float, rsi: float, trend: TrendState):
        level = self._round_to_tick(level)
        size = self._calculate_position_size(level)
        stop_loss = self._round_to_tick(level * (1 - self.config.stop_loss_pct / 100))
        take_profit = self._round_to_tick(level * (1 + self.config.take_profit_pct / 100))
        trailing_stop = self._round_to_tick(level * (1 - self.config.trailing_stop_pct / 100)) if self.config.use_trailing_stop else None
        contracts = self._get_contracts()
        trade = await self.broker.place_limit_order('BUY', contracts, level)
        order_id = trade.order.orderId
        pending = PendingOrder(order_id=order_id, side='long', limit_price=level, size=size, stop_loss=stop_loss, take_profit=take_profit, trailing_stop=trailing_stop, submit_time=datetime.now(UTC), grid_level=level)
        self.pending_orders[order_id] = pending
        reason = f"Grid Long ({trend.value}, RSI: {rsi:.1f})"
        atr = self.indicators.cache.get('atr', 0)
        vol_note = f" [HIGH VOL ATR:{atr:.2f}→{contracts}cts]" if contracts < self.config.contracts_per_trade else f" [{contracts}cts]"
        print(f"  ⬆️ LONG ORDER @ {level:.2f} | Size: {size:.2f} | SL: {stop_loss:.2f} | TP: {take_profit:.2f}{vol_note}")
        print(f"     Reason: {reason}")
        print(f"     ⏳ Order {order_id} PENDING - awaiting fill confirmation")

    async def _enter_short(self, level: float, rsi: float, trend: TrendState):
        level = self._round_to_tick(level)
        size = self._calculate_position_size(level)
        stop_loss = self._round_to_tick(level * (1 + self.config.stop_loss_pct / 100))
        take_profit = self._round_to_tick(level * (1 - self.config.take_profit_pct / 100))
        trailing_stop = self._round_to_tick(level * (1 + self.config.trailing_stop_pct / 100)) if self.config.use_trailing_stop else None
        contracts = self._get_contracts()
        trade = await self.broker.place_limit_order('SELL', contracts, level)
        order_id = trade.order.orderId
        pending = PendingOrder(order_id=order_id, side='short', limit_price=level, size=size, stop_loss=stop_loss, take_profit=take_profit, trailing_stop=trailing_stop, submit_time=datetime.now(UTC), grid_level=level)
        self.pending_orders[order_id] = pending
        reason = f"Grid Short ({trend.value}, RSI: {rsi:.1f})"
        atr = self.indicators.cache.get('atr', 0)
        vol_note = f" [HIGH VOL ATR:{atr:.2f}→{contracts}cts]" if contracts < self.config.contracts_per_trade else f" [{contracts}cts]"
        print(f"  ⬇️ SHORT ORDER @ {level:.2f} | Size: {size:.2f} | SL: {stop_loss:.2f} | TP: {take_profit:.2f}{vol_note}")
        print(f"     Reason: {reason}")
        print(f"     ⏳ Order {order_id} PENDING - awaiting fill confirmation")

    async def _check_pending_orders(self):
        if not self.pending_orders:
            return
        try:
            open_orders = self.broker.ib.openOrders()
            open_order_ids = {trade.order.orderId for trade in self.broker.ib.openTrades()}
        except:
            open_order_ids = set()
        for order_id in list(self.pending_orders.keys()):
            pending = self.pending_orders[order_id]
            trade = None
            for t in self.broker.ib.trades():
                if t.order.orderId == order_id:
                    trade = t
                    break
            if trade is None:
                print(f"  ⚠️ Order {order_id} not found - removing from pending")
                del self.pending_orders[order_id]
                continue
            if trade.orderStatus.status == 'Filled' and trade.fills:
                fill_price = trade.fills[-1].execution.price
                if self.config.use_grid_stop and self.grid_levels:
                    if pending.side == 'long':
                        last_level = min(self.grid_levels)
                        stop_loss = self._round_to_tick(last_level - self.config.grid_stop_buffer_pts)
                        take_profit = self._round_to_tick(fill_price + self.config.take_profit_pts)
                    else:
                        last_level = max(self.grid_levels)
                        stop_loss = self._round_to_tick(last_level + self.config.grid_stop_buffer_pts)
                        take_profit = self._round_to_tick(fill_price - self.config.take_profit_pts)
                    print(f"     Grid stop: last level {last_level:.2f} + {self.config.grid_stop_buffer_pts}pt buffer → SL {stop_loss:.2f}")
                else:
                    if pending.side == 'long':
                        stop_loss = self._round_to_tick(fill_price - self.config.stop_loss_pts)
                        take_profit = self._round_to_tick(fill_price + self.config.take_profit_pts)
                    else:
                        stop_loss = self._round_to_tick(fill_price + self.config.stop_loss_pts)
                        take_profit = self._round_to_tick(fill_price - self.config.take_profit_pts)

                filled_qty = int(trade.orderStatus.filled)

                stop_action = 'SELL' if pending.side == 'long' else 'BUY'
                offset = self.config.stop_limit_offset_pts
                if pending.side == 'long':
                    stop_limit_price = self._round_to_tick(stop_loss - offset)
                else:
                    stop_limit_price = self._round_to_tick(stop_loss + offset)
                stop_trade = await self.broker.place_stop_limit_order(stop_action, filled_qty, stop_loss, stop_limit_price)
                stop_order_id = stop_trade.order.orderId if stop_trade else None

                tp_action = 'SELL' if pending.side == 'long' else 'BUY'
                tp_trade = await self.broker.place_limit_order(tp_action, filled_qty, take_profit)
                tp_order_id = tp_trade.order.orderId if tp_trade else None

                position = Position(
                    side=pending.side,
                    entry_price=fill_price,
                    size=float(filled_qty),
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    trailing_stop=None,
                    entry_time=datetime.now(UTC),
                    grid_level=pending.grid_level,
                    order_id=order_id,
                    stop_order_id=stop_order_id,
                    tp_order_id=tp_order_id,
                    trailing_activated=False,
                    highest_price=fill_price if pending.side == 'long' else None,
                    lowest_price=fill_price if pending.side == 'short' else None
                )
                self.positions.append(position)
                self.position_count += 1
                del self.pending_orders[order_id]
                print(f"  ✅ FILL CONFIRMED: {pending.side.upper()} @ {fill_price:.2f} (order {order_id})")
                print(f"     📊 Bracket placed: SL #{stop_order_id} @ {stop_loss:.2f} | TP #{tp_order_id} @ {take_profit:.2f}")
                print(f"     Trail activates @ +{self.config.trailing_activation_pts:.1f} pts")
            elif trade.orderStatus.status in ['Cancelled', 'ApiCancelled', 'Inactive']:
                print(f"  ❌ Order {order_id} cancelled/expired - removing from pending")
                del self.pending_orders[order_id]
            else:
                age_seconds = (datetime.now(UTC) - pending.submit_time).total_seconds()
                if age_seconds > 120:
                    print(f"  ⏰ Order {order_id} timed out after {age_seconds:.0f}s - cancelling")
                    try:
                        self.broker.ib.cancelOrder(trade.order)
                    except:
                        pass
                    del self.pending_orders[order_id]

    def _weighted_avg_fill(self, fills, fallback: float) -> float:
        if not fills:
            return fallback
        total_qty = sum(f.execution.shares for f in fills)
        if total_qty == 0:
            return fallback
        return sum(f.execution.price * f.execution.shares for f in fills) / total_qty

    async def _check_exits(self, bar: Dict):
        high = bar['high']
        low = bar['low']
        current_price = bar['close']
        positions_to_remove = []
        for position in self.positions:
            exit_price = None
            exit_reason = None
            order_to_cancel = None
            if position.stop_order_id and position.stop_order_id in self.broker._filled_orders:
                filled_trade = self.broker._filled_orders[position.stop_order_id]
                exit_price = self._weighted_avg_fill(filled_trade.fills, position.stop_loss)
                exit_reason = "Trailing Stop" if position.trailing_activated else "Stop Loss"
                order_to_cancel = position.tp_order_id
                del self.broker._filled_orders[position.stop_order_id]
            elif position.tp_order_id and position.tp_order_id in self.broker._filled_orders:
                filled_trade = self.broker._filled_orders[position.tp_order_id]
                exit_price = self._weighted_avg_fill(filled_trade.fills, position.take_profit)
                exit_reason = "Take Profit"
                order_to_cancel = position.stop_order_id
                del self.broker._filled_orders[position.tp_order_id]
            if exit_price:
                if order_to_cancel:
                    await self.broker.cancel_order_by_id(order_to_cancel)
                if position.side == 'long':
                    pnl_pts = exit_price - position.entry_price
                else:
                    pnl_pts = position.entry_price - exit_price
                pnl_dollars = pnl_pts * 50 * int(position.size)
                self.daily_pnl += pnl_dollars
                self.equity += pnl_dollars
                emoji = "✅" if pnl_pts >= 0 else "❌"
                print(f"  {emoji} EXIT {position.side.upper()} @ {exit_price:.2f} | {exit_reason}")
                print(f"     P&L: {pnl_pts:+.2f} pts (${pnl_dollars:+,.2f}) | Daily: ${self.daily_pnl:+,.2f}")
                positions_to_remove.append(position)
                continue
            if position.side == 'long':
                if position.highest_price is None or high > position.highest_price:
                    position.highest_price = high
                profit_pts = position.highest_price - position.entry_price
                if not position.trailing_activated and profit_pts >= self.config.trailing_activation_pts:
                    new_stop = self._round_to_tick(position.entry_price + self.config.trailing_distance_pts)
                    if new_stop > position.stop_loss:
                        old_stop = position.stop_loss
                        confirmed = True
                        if position.stop_order_id:
                            confirmed = await self.broker.modify_stop_order(position.stop_order_id, new_stop)
                        if confirmed:
                            position.trailing_activated = True
                            position.stop_loss = new_stop
                            position.trailing_stop = new_stop
                            print(f"  🔄 TRAILING ACTIVATED @ +{profit_pts:.2f}pts | Stop: {old_stop:.2f} → {new_stop:.2f}")
                        else:
                            print(f"  ⚠️ Trail activation failed — IBKR did not confirm stop move. Will retry next bar.")
                elif position.trailing_activated:
                    new_stop = self._round_to_tick(position.highest_price - self.config.trailing_distance_pts)
                    if new_stop > position.stop_loss:
                        old_stop = position.stop_loss
                        position.stop_loss = new_stop
                        position.trailing_stop = new_stop
                        if position.stop_order_id:
                            await self.broker.modify_stop_order(position.stop_order_id, new_stop)
                        print(f"  📈 TRAIL UPDATE: High {position.highest_price:.2f} | Stop: {old_stop:.2f} → {new_stop:.2f}")
            else:
                if position.lowest_price is None or low < position.lowest_price:
                    position.lowest_price = low
                profit_pts = position.entry_price - position.lowest_price
                if not position.trailing_activated and profit_pts >= self.config.trailing_activation_pts:
                    new_stop = self._round_to_tick(position.entry_price - self.config.trailing_distance_pts)
                    if new_stop < position.stop_loss:
                        old_stop = position.stop_loss
                        confirmed = True
                        if position.stop_order_id:
                            confirmed = await self.broker.modify_stop_order(position.stop_order_id, new_stop)
                        if confirmed:
                            position.trailing_activated = True
                            position.stop_loss = new_stop
                            position.trailing_stop = new_stop
                            print(f"  🔄 TRAILING ACTIVATED @ +{profit_pts:.2f}pts | Stop: {old_stop:.2f} → {new_stop:.2f}")
                        else:
                            print(f"  ⚠️ Trail activation failed — IBKR did not confirm stop move. Will retry next bar.")
                elif position.trailing_activated:
                    new_stop = self._round_to_tick(position.lowest_price + self.config.trailing_distance_pts)
                    if new_stop < position.stop_loss:
                        old_stop = position.stop_loss
                        position.stop_loss = new_stop
                        position.trailing_stop = new_stop
                        if position.stop_order_id:
                            await self.broker.modify_stop_order(position.stop_order_id, new_stop)
                        print(f"  📉 TRAIL UPDATE: Low {position.lowest_price:.2f} | Stop: {old_stop:.2f} → {new_stop:.2f}")
            if self.config.use_trend_reversal_exit:
                time_in_trade = (datetime.now(UTC) - position.entry_time).total_seconds() / 60
                if time_in_trade >= self.config.trend_cooldown_minutes:
                    if self._should_exit_on_trend_reversal(position):
                        if position.stop_order_id:
                            await self.broker.cancel_order_by_id(position.stop_order_id)
                        if position.tp_order_id:
                            await self.broker.cancel_order_by_id(position.tp_order_id)
                        await self._close_position(position, current_price, "Trend Reversal")
                        continue
            if self.config.time_based_exit:
                holding_time = datetime.now(UTC) - position.entry_time
                if holding_time > timedelta(hours=self.config.max_holding_hours):
                    if position.stop_order_id:
                        await self.broker.cancel_order_by_id(position.stop_order_id)
                    if position.tp_order_id:
                        await self.broker.cancel_order_by_id(position.tp_order_id)
                    await self._close_position(position, current_price, "Time Exit")
                    continue
        for position in positions_to_remove:
            if position in self.positions:
                self.positions.remove(position)
                self.position_count = max(0, self.position_count - 1)
                self.bars_since_exit = 0

    def _should_exit_on_trend_reversal(self, position: Position) -> bool:
        trend = self.confirmed_trend
        if position.side == 'long':
            return trend in [TrendState.STRONG_BEARISH, TrendState.MODERATE_BEARISH]
        else:
            return trend in [TrendState.STRONG_BULLISH, TrendState.MODERATE_BULLISH]

    async def _close_position(self, position: Position, trigger_price: float, reason: str):
        action = 'SELL' if position.side == 'long' else 'BUY'
        trade = await self.broker.place_market_order(action, 1)
        await asyncio.sleep(0.3)
        if trade and trade.fills:
            actual_exit = trade.fills[-1].execution.price
        else:
            actual_exit = trigger_price
            print(f"  ⚠️ Fill price not available, using trigger: {trigger_price:.2f}")
        if position.side == 'long':
            pnl = (actual_exit - position.entry_price) * int(position.size) * 50
        else:
            pnl = (position.entry_price - actual_exit) * int(position.size) * 50
        self.daily_pnl += pnl
        self.equity += pnl
        self.positions.remove(position)
        self.position_count -= 1
        self.bars_since_exit = 0
        print(f"  ❌ CLOSE {position.side.upper()} @ {actual_exit:.2f} (trigger: {trigger_price:.2f}) | P&L: ${pnl:+,.2f} | {reason}")
        print(f"     Daily P&L: ${self.daily_pnl:+,.2f} | Equity: ${self.equity:,.2f}")

    def _check_daily_loss_limit(self) -> bool:
        max_loss = self.config.initial_equity * (self.config.max_loss_per_day_pct / 100)
        return self.daily_pnl <= -max_loss

    def print_macd_filter_summary(self):
        """Call this at end of session to see every trade the MACD filter blocked."""
        print(f"\n{'='*60}")
        print(f"🔍 MACD FILTER SUMMARY — {len(self._macd_blocked_trades)} trade(s) blocked")
        print(f"{'='*60}")
        if not self._macd_blocked_trades:
            print("  No trades blocked today.")
        for t in self._macd_blocked_trades:
            ct = t['time'].astimezone(CENTRAL)
            print(f"  {ct.strftime('%H:%M')} | {t['direction'].upper()} @ {t['price']:.2f} | "
                  f"MACD: {t['macd']:.2f} vs prev {t['prev_macd']:.2f} | "
                  f"RSI: {t['rsi']:.1f} | Trend: {t['trend']}")
        print(f"{'='*60}\n")

    async def on_new_bar(self, bar: Dict):
        self.bars.append(bar)
        self.last_price = bar['close']
        current_day = bar['time'].day

        # Track session low for scalp_robust short filter
        if self._session_low_date != current_day:
            self._session_low = bar['low']
            self._session_low_date = current_day
        else:
            if bar['low'] < self._session_low:
                self._session_low = bar['low']
        if self.last_reset_day != current_day:
            if self.last_reset_day is not None:
                print(f"\n📅 New trading day. Previous day P&L: ${self.daily_pnl:+,.2f}")
            self.daily_pnl = 0.0
            self.last_reset_day = current_day
        await self._check_pending_orders()
        local_time = bar['time'].astimezone(CENTRAL)
        time_str = local_time.strftime('%Y-%m-%d %H:%M')
        print(f"\n[{time_str}] O:{bar['open']:.2f} H:{bar['high']:.2f} L:{bar['low']:.2f} C:{bar['close']:.2f} V:{bar['volume']}")
        # Snapshot PREVIOUS bar's MACD before recalculating indicators
        # Must happen BEFORE calculate_all so we capture last bar's value, not current
        if self.indicators.cache:
            self._prev_macd = self.indicators.cache.get('macd', {}).get('macd', 0.0)

        if not self.indicators.calculate_all(self.bars):
            bars_needed = self.config.super_long_ma_length - len(self.bars)
            print(f"  ⏳ Warming up... need {bars_needed} more bars")
            return
        self.previous_trend = self.confirmed_trend
        self.prev_rsi = self.indicators.cache.get('rsi', 50)

        self.bars_since_exit += 1
        self.current_trend = self._determine_trend()
        self._get_confirmed_trend()
        if self.config.use_5m_filter:
            self._5m_bar_buffer.append(bar)
            if len(self._5m_bar_buffer) >= 5:
                bar_5m = {
                    'time': self._5m_bar_buffer[0]['time'],
                    'open': self._5m_bar_buffer[0]['open'],
                    'high': max(b['high'] for b in self._5m_bar_buffer),
                    'low': min(b['low'] for b in self._5m_bar_buffer),
                    'close': self._5m_bar_buffer[-1]['close'],
                    'volume': sum(b['volume'] for b in self._5m_bar_buffer)
                }
                self.bars_5m.append(bar_5m)
                self._5m_bar_buffer = []
                if self.indicators_5m.calculate_all(self.bars_5m):
                    self.current_trend_5m = self._determine_trend_5m()
        grid_size = self._calculate_grid_size()
        ind = self.indicators.cache
        trend_display = f"{self.confirmed_trend.value}"
        if self.current_trend != self.confirmed_trend:
            trend_display += f" (raw: {self.current_trend.value})"
        if len(self.bars) >= 2:
            prev = self.bars[-2]
            print(f"  📈 Trend: {trend_display}")
            print(f"  🗺️  Grid: {grid_size:.3f}% | 🔺 {prev['high']:.2f} 🔻 {prev['low']:.2f}")
        print(f"  📡 RSI: {ind['rsi']:.1f} | MACD: {ind['macd']['macd']:.2f} (prev: {self._prev_macd:.2f}) | ATR: {ind['atr']:.2f}")
        if self.config.use_5m_filter:
            print(f"  📊 Filled: {self.position_count} | Pending: {len(self.pending_orders)} | 5m: {self.current_trend_5m.value}")
        else:
            print(f"  📊 Filled: {self.position_count} | Pending: {len(self.pending_orders)}")
        for order_id, pending in self.pending_orders.items():
            age_sec = (datetime.now(UTC) - pending.submit_time).total_seconds()
            print(f"     ⏳ PENDING {pending.side.upper()} @ {pending.limit_price:.2f} (order {order_id}, {age_sec:.0f}s)")
        for pos in self.positions:
            if pos.side == 'long':
                unrealized_pnl = (self.last_price - pos.entry_price) * int(pos.size) * 50
                profit_pts = (pos.highest_price or self.last_price) - pos.entry_price
            else:
                unrealized_pnl = (pos.entry_price - self.last_price) * int(pos.size) * 50
                profit_pts = pos.entry_price - (pos.lowest_price or self.last_price)
            if self.config.use_trailing_stop and pos.trailing_activated:
                active_stop = pos.trailing_stop
                stop_label = "Trail"
            else:
                active_stop = pos.stop_loss
                stop_label = "SL"
            status = f"  📍 {pos.side.upper()} @ {pos.entry_price:.2f} | {stop_label}: {active_stop:.2f} | TP: {pos.take_profit:.2f} | P&L: ${unrealized_pnl:+,.2f}"
            if self.config.use_trailing_stop:
                if pos.trailing_activated:
                    status += f" | 🔒 Trailing"
                else:
                    pts_to_activate = self.config.trailing_activation_pts - profit_pts
                    if pts_to_activate > 0:
                        status += f" | +{pts_to_activate:.1f} to trail"
            print(status)
        if self._check_daily_loss_limit():
            print(f"  🛑 MAX DAILY LOSS REACHED - Closing all positions")
            for position in list(self.positions):
                await self._close_position(position, bar['close'], "Max Daily Loss")
            return
        await self._check_exits(bar)
        await self._check_entries_scalp(bar)
        print(f"  💰 Daily P&L: ${self.daily_pnl:+,.2f}")