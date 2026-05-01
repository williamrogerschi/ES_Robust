# broker.py
from ib_insync import IB, Future, Order, Trade, util
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Dict, List

CENTRAL = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")


class IBKRBroker:
    def __init__(self, symbol="ES"):
        util.startLoop()
        self.ib = IB()
        self.symbol = symbol
        self.contract = None
        
        self._current_1min = None
        self._last_start = None
        self._bar_queue = asyncio.Queue()
        self._rt_bars = None
        
        self._open_orders: Dict[int, Trade] = {}
        self._filled_orders: Dict[int, Trade] = {}

    async def connect_async(self, host="127.0.0.1", port=7497, client_id=10):
        await self.ib.connectAsync(host, port, clientId=client_id)
        print(f"✓ Connected to IBKR (paper) - clientId={client_id}")
        self.ib.orderStatusEvent += self._on_order_status
        self.ib.execDetailsEvent += self._on_exec_details

    async def disconnect_async(self):
        if self.ib.isConnected():
            await self.cancel_all_orders()
            self.ib.disconnect()
            print("✓ Disconnected from IBKR")

    async def get_front_month_contract_async(self):
        template = Future(symbol=self.symbol, exchange="CME", currency="USD")
        details = await self.ib.reqContractDetailsAsync(template)
        if not details:
            raise ValueError(f"No contract found for {self.symbol}")
        today = datetime.now(UTC).strftime('%Y%m%d')
        active = [d for d in details if d.contract.lastTradeDateOrContractMonth > today]
        if not active:
            raise ValueError(f"No active contracts found for {self.symbol}")
        active.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
        self.contract = active[0].contract
        await self.ib.qualifyContractsAsync(self.contract)
        print(f"✓ Front-month contract: {self.contract.localSymbol} expiry={self.contract.lastTradeDateOrContractMonth}")

    async def get_historical_bars(self, duration: str = "1 D", bar_size: str = "1 min") -> list:
        if not self.contract:
            raise RuntimeError("Contract not set. Run get_front_month_contract_async() first.")
        print(f"→ Fetching historical bars ({duration}, {bar_size})...")
        bars = await self.ib.reqHistoricalDataAsync(
            contract=self.contract, endDateTime='', durationStr=duration,
            barSizeSetting=bar_size, whatToShow='TRADES', useRTH=False, formatDate=1
        )
        if not bars:
            print("  ⚠️ No historical bars returned")
            return []
        result = []
        for bar in bars:
            dt = bar.date
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            result.append({"time": dt, "open": bar.open, "high": bar.high,
                           "low": bar.low, "close": bar.close, "volume": bar.volume})
        print(f"✓ Loaded {len(result)} historical bars")
        return result

    def _on_rt_bar(self, bars, has_new_bar: bool):
        if not bars:
            return
        bar = bars[-1]
        dt = bar.time
        dt_utc = dt.replace(tzinfo=UTC)
        minute_start = dt_utc.replace(second=0, microsecond=0)
        if self._last_start != minute_start:
            if self._current_1min is not None:
                asyncio.create_task(self._bar_queue.put(dict(self._current_1min)))
            self._current_1min = {"time": minute_start, "open": bar.open_, "high": bar.high,
                                   "low": bar.low, "close": bar.close, "volume": bar.volume}
            self._last_start = minute_start
        else:
            self._current_1min["high"] = max(self._current_1min["high"], bar.high)
            self._current_1min["low"] = min(self._current_1min["low"], bar.low)
            self._current_1min["close"] = bar.close
            self._current_1min["volume"] += bar.volume

    async def stream_1m_bars(self):
        if not self.contract:
            raise RuntimeError("Contract not set. Run get_front_month_contract_async() first.")
        self._rt_bars = self.ib.reqRealTimeBars(
            contract=self.contract, barSize=5, whatToShow="TRADES", useRTH=False
        )
        self._rt_bars.updateEvent += self._on_rt_bar
        print("→ Subscribed to 5-second real-time bars (aggregating to 1 min)")
        try:
            while True:
                bar = await self._bar_queue.get()
                yield bar
        finally:
            if self._rt_bars:
                self.ib.cancelRealTimeBars(self._rt_bars)
                self._rt_bars.updateEvent -= self._on_rt_bar
            print("→ Real-time bars cancelled")

    def _on_order_status(self, trade: Trade):
        order_id = trade.order.orderId
        status = trade.orderStatus.status
        print(f"  📋 Order {order_id} status: {status}")
        if status in ['Filled', 'Cancelled', 'ApiCancelled']:
            if order_id in self._open_orders:
                del self._open_orders[order_id]
            if status == 'Filled':
                self._filled_orders[order_id] = trade

    def _on_exec_details(self, trade: Trade, fill):
        print(f"  ✅ Fill: {fill.execution.side} {fill.execution.shares} @ {fill.execution.price}")

    async def place_market_order(self, action: str, quantity: int) -> Optional[Trade]:
        if not self.contract:
            raise RuntimeError("Contract not set")
        order = Order(action=action, orderType='MKT', totalQuantity=quantity, tif='DAY', transmit=True)
        trade = self.ib.placeOrder(self.contract, order)
        self._open_orders[order.orderId] = trade
        print(f"  📤 Market {action} {quantity} contracts submitted (ID: {order.orderId})")
        await asyncio.sleep(0.1)
        return trade

    async def place_limit_order(self, action: str, quantity: int, limit_price: float) -> Optional[Trade]:
        if not self.contract:
            raise RuntimeError("Contract not set")
        order = Order(action=action, orderType='LMT', totalQuantity=quantity,
                      lmtPrice=limit_price, tif='DAY', transmit=True)
        trade = self.ib.placeOrder(self.contract, order)
        self._open_orders[order.orderId] = trade
        print(f"  📤 Limit {action} {quantity} @ {limit_price:.2f} submitted (ID: {order.orderId})")
        await asyncio.sleep(0.1)
        return trade

    async def place_stop_order(self, action: str, quantity: int, stop_price: float) -> Optional[Trade]:
        """Plain market stop — consider place_stop_limit_order to prevent slippage."""
        if not self.contract:
            raise RuntimeError("Contract not set")
        order = Order(action=action, orderType='STP', totalQuantity=quantity,
                      auxPrice=stop_price, tif='GTC', transmit=True)
        trade = self.ib.placeOrder(self.contract, order)
        self._open_orders[order.orderId] = trade
        print(f"  📤 Stop {action} {quantity} @ {stop_price:.2f} submitted (ID: {order.orderId})")
        await asyncio.sleep(0.1)
        return trade

    async def place_stop_limit_order(self, action: str, quantity: int,
                                      stop_price: float, limit_price: float) -> Optional[Trade]:
        """Place a stop-limit order to prevent catastrophic slippage on exits.

        Triggers at stop_price, then fills at limit_price or better.
          Long exits (SELL): limit_price = stop_price - offset  (floor on fill quality)
          Short exits (BUY): limit_price = stop_price + offset  (ceiling on fill quality)

        Risk: if price gaps MORE than the offset through the stop, the order won't fill
        and the position stays open. Tune stop_limit_offset_pts in StrategyConfig to
        balance slippage protection vs. non-fill risk.
        """
        if not self.contract:
            raise RuntimeError("Contract not set")
        order = Order(
            action=action,
            orderType='STP LMT',
            totalQuantity=quantity,
            auxPrice=stop_price,
            lmtPrice=limit_price,
            tif='GTC',
            transmit=True
        )
        trade = self.ib.placeOrder(self.contract, order)
        self._open_orders[order.orderId] = trade
        print(f"  📤 Stop-Limit {action} {quantity} @ stop:{stop_price:.2f} / lmt:{limit_price:.2f} submitted (ID: {order.orderId})")
        await asyncio.sleep(0.1)
        return trade

    async def modify_stop_order(self, order_id: int, new_stop_price: float) -> bool:
        """Modify an existing stop or stop-limit order to a new price.
        For STP LMT orders, the limit price shifts by the same delta as the stop.
        """
        for trade in self.ib.trades():
            if trade.order.orderId == order_id:
                try:
                    old_stop = trade.order.auxPrice
                    trade.order.auxPrice = new_stop_price
                    if trade.order.orderType == 'STP LMT' and trade.order.lmtPrice:
                        offset = trade.order.lmtPrice - old_stop
                        trade.order.lmtPrice = round((new_stop_price + offset) * 4) / 4
                    self.ib.placeOrder(trade.contract, trade.order)
                    for _ in range(20):
                        await asyncio.sleep(0.25)
                        status = trade.orderStatus.status
                        if status in ('Submitted', 'PreSubmitted'):
                            print(f"  ✏️ Stop order {order_id} modified → {new_stop_price:.2f} [confirmed]")
                            return True
                    print(f"  ⚠️ Stop order {order_id} modification sent but not confirmed — stop may be delayed")
                    return False
                except Exception as e:
                    print(f"  ⚠️ Failed to modify order {order_id}: {e}")
                    return False
        print(f"  ⚠️ modify_stop_order: order {order_id} not found")
        return False

    async def place_bracket_order(self, action: str, quantity: int, entry_price: float,
                                   take_profit_price: float, stop_loss_price: float) -> Optional[List[Trade]]:
        if not self.contract:
            raise RuntimeError("Contract not set")
        bracket = self.ib.bracketOrder(
            action=action, quantity=quantity, limitPrice=entry_price,
            takeProfitPrice=take_profit_price, stopLossPrice=stop_loss_price
        )
        trades = []
        for order in bracket:
            trade = self.ib.placeOrder(self.contract, order)
            self._open_orders[order.orderId] = trade
            trades.append(trade)
        print(f"  📤 Bracket {action}: Entry @ {entry_price:.2f}, TP @ {take_profit_price:.2f}, SL @ {stop_loss_price:.2f}")
        await asyncio.sleep(0.1)
        return trades

    async def cancel_order(self, trade: Trade) -> bool:
        try:
            self.ib.cancelOrder(trade.order)
            print(f"  ❌ Cancel request sent for order {trade.order.orderId}")
            await asyncio.sleep(0.1)
            return True
        except Exception as e:
            print(f"  ⚠️ Failed to cancel order: {e}")
            return False

    async def cancel_order_by_id(self, order_id: int) -> bool:
        for trade in self.ib.trades():
            if trade.order.orderId == order_id:
                try:
                    self.ib.cancelOrder(trade.order)
                    await asyncio.sleep(0.1)
                    return True
                except Exception as e:
                    print(f"  ⚠️ Failed to cancel order {order_id}: {e}")
                    return False
        print(f"  ⚠️ Order {order_id} not found for cancellation")
        return False

    async def cancel_all_orders(self):
        if not self._open_orders:
            return
        print(f"  ❌ Cancelling {len(self._open_orders)} open orders...")
        for trade in list(self._open_orders.values()):
            await self.cancel_order(trade)

    async def close_all_positions(self) -> Optional[Trade]:
        positions = self.ib.positions()
        for pos in positions:
            if pos.contract.symbol == self.symbol:
                quantity = abs(pos.position)
                action = 'SELL' if pos.position > 0 else 'BUY'
                print(f"  🔄 Closing {pos.position} {self.symbol} position...")
                return await self.place_market_order(action, int(quantity))
        return None

    def get_position(self) -> int:
        positions = self.ib.positions()
        for pos in positions:
            if pos.contract.symbol == self.symbol:
                return int(pos.position)
        return 0

    def get_account_value(self) -> float:
        account_values = self.ib.accountValues()
        for av in account_values:
            if av.tag == 'NetLiquidation' and av.currency == 'USD':
                return float(av.value)
        return 0.0

    def get_buying_power(self) -> float:
        account_values = self.ib.accountValues()
        for av in account_values:
            if av.tag == 'BuyingPower' and av.currency == 'USD':
                return float(av.value)
        return 0.0

    async def get_current_price(self) -> Optional[float]:
        if not self.contract:
            return None
        ticker = self.ib.reqMktData(self.contract, '', False, False)
        await asyncio.sleep(0.5)
        price = ticker.marketPrice()
        self.ib.cancelMktData(self.contract)
        return price if price > 0 else None