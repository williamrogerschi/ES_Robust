# main.py
import asyncio
import logging
import sys
from datetime import datetime
from strategy import GridStrategy
from models import CONFIG_PRESETS
from broker import IBKRBroker

# =============================================================================
# SELECT MODE: "scalp" | "scalp_robust" | "grid"
# =============================================================================
MODE = "scalp"
# =============================================================================

MODE_DESCRIPTIONS = {
    "scalp": {
        "label": "📈 MODE: SCALP",
        "notes": [
            "• {contracts} contract(s) max",
            "• Grid entries only",
            "• Trailing stop enabled",
            "• SL: ~8 pts | TP: ~12 pts | Trail activates: +6 pts",
            "• Best for: Normal sessions, choppy/ranging opens",
        ]
    },
    "scalp_robust": {
        "label": "🛡️  MODE: SCALP ROBUST",
        "notes": [
            "• {contracts} contract(s) max",
            "• Scalp core logic + session filter + 5m trend alignment",
            "• Trailing stop enabled",
            "• SL: ~8 pts | TP: ~12 pts | Trail activates: +6 pts",
            "• Session: 9:30-12:00 CT only",
            "• 5m trend must align with 1m direction",
            "• Best for: High-quality morning session trades",
        ]
    },
    "grid": {
        "label": "📊 MODE: GRID",
        "notes": [
            "• {contracts} contract(s) max",
            "• No trailing stop",
            "• SL: ~28 pts | TP: ~17 pts",
            "• Best for: Ranging markets, mean reversion",
        ]
    },
}


class DualWriter:
    """Writes print() output to both console and log file."""
    def __init__(self, console, file):
        self.console = console
        self.file = file

    def write(self, text):
        self.console.write(text)
        self.file.write(text)

    def flush(self):
        self.console.flush()
        self.file.flush()


def setup_logging():
    log_filename = f"es_bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    fmt = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # File handler - INFO and above only (no raw socket noise)
    fh = logging.FileHandler(log_filename, mode='w', encoding='utf-8')
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)

    # Console handler - WARNING and above (clean terminal)
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers = []
    root.addHandler(fh)
    root.addHandler(ch)

    # Suppress ib_insync raw socket debug in both handlers
    logging.getLogger('ib_insync').setLevel(logging.WARNING)

    # Redirect print() to also write to log file
    log_file_handle = open(log_filename, 'a', encoding='utf-8')
    sys.stdout = DualWriter(sys.stdout, log_file_handle)

    print(f"Logging to: {log_filename}")
    return log_filename


async def main():
    log_file = setup_logging()

    if MODE not in CONFIG_PRESETS:
        raise ValueError(f"Unknown MODE: '{MODE}'. Valid options: {list(CONFIG_PRESETS.keys())}")

    config = CONFIG_PRESETS[MODE]()
    broker = IBKRBroker(symbol="ES")

    desc = MODE_DESCRIPTIONS[MODE]
    print("\n" + "="*60)
    print(desc["label"])
    for note in desc["notes"]:
        print(f"   {note.format(contracts=config.contracts_per_trade)}")
    print("="*60)

    try:
        await broker.connect_async(host="127.0.0.1", port=7497, client_id=10)
        await broker.get_front_month_contract_async()

        strategy = GridStrategy(broker=broker, config=config)

        print("\n" + "="*60)
        print("Loading historical data for indicator warm-up...")
        print("="*60)

        historical_bars = await broker.get_historical_bars(duration="1 D", bar_size="1 min")

        if historical_bars:
            print(f"\nProcessing {len(historical_bars)} historical bars...")

            for bar in historical_bars:
                strategy.bars.append(bar)
                strategy.last_price = bar['close']

                current_day = bar['time'].day
                if strategy.last_reset_day != current_day:
                    strategy.daily_pnl = 0.0
                    strategy.last_reset_day = current_day

            if strategy.indicators.calculate_all(strategy.bars):
                strategy.current_trend = strategy._determine_trend()
                strategy.grid_levels = strategy._calculate_grid_levels()

                ind = strategy.indicators.cache
                print(f"\nIndicators ready!")
                print(f"  Bars loaded: {len(strategy.bars)}")
                print(f"  Last price: {strategy.last_price:.2f}")
                print(f"  Trend: {strategy.current_trend.value}")
                print(f"  RSI: {ind['rsi']:.1f}")
                print(f"  ATR: {ind['atr']:.2f}")
                print(f"  MACD: {ind['macd']['macd']:.2f} / Signal: {ind['macd']['signal']:.2f}")
                print(f"  MA: {ind['short_ma']:.2f} / {ind['long_ma']:.2f} / {ind['super_long_ma']:.2f}")
            else:
                print(f"Still need more bars. Have {len(strategy.bars)}, need ~{config.super_long_ma_length}")
        else:
            print("No historical data loaded. Strategy will warm up with live bars.")

        print("\n" + "-"*60)
        account_value = broker.get_account_value()
        buying_power = broker.get_buying_power()
        current_position = broker.get_position()
        print(f"Account Value: ${account_value:,.2f}")
        print(f"Buying Power: ${buying_power:,.2f}")
        print(f"Current Position: {current_position} contracts")
        print("-"*60)

        print("\n" + "="*60)
        print("Starting live 1-minute bar stream...")
        print(f"Strategy is now ACTIVE ({MODE.upper()} mode)")
        print("="*60 + "\n")

        async for bar in broker.stream_1m_bars():
            await strategy.on_new_bar(bar)

    except KeyboardInterrupt:
        print("\n\n" + "="*60)
        print("Stopped by user.")
        print("="*60)

        if 'strategy' in locals():
            print(f"\nFinal Summary ({MODE.upper()} mode):")
            print(f"  Daily P&L: ${strategy.daily_pnl:+,.2f}")
            print(f"  Open Positions: {strategy.position_count}")
            print(f"  Contracts per trade: {config.contracts_per_trade}")
            print(f"  Equity: ${strategy.equity:,.2f}")
            print(f"\n  Log saved to: {log_file}")

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await broker.disconnect_async()


if __name__ == "__main__":
    asyncio.run(main())