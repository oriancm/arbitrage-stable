import ccxt
import pandas as pd
import numpy as np
from typing import List, Dict
import logging
import time
from datetime import datetime
import signal
import traceback
import os
import sys
import atexit
from decimal import Decimal, ROUND_FLOOR
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("API_KEY")
api_secret = os.getenv("API_SECRET")

# Global variables
trade_logs = []
results_data = {}
interrupted = False

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    force=True,
    handlers=[
        logging.FileHandler('trading.log', mode='a'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def log_snapshot(logger, snapshot):
    """Log market SNAPSHOT information"""
    snapshot_info = (
        f"SNAPSHOT - ID: {snapshot['snapshot_id']} | "
        f"Best bid: {snapshot['best_bid']:.8f} | "
        f"Best ask: {snapshot['best_ask']:.8f} | "
        f"Current price: {snapshot['current_price']:.8f}"
    )
    logger.info(snapshot_info)

def cleanup_on_exit():
    """Cleanup on program exit"""
    global interrupted
    logger.info(f"Cleaning up and exiting program (PID: {os.getpid()})...")
    interrupted = True
    os._exit(0)

def signal_handler(sig, frame):
    """Signal handler"""
    logger.info(f"Signal {sig} received. Shutting down...")
    cleanup_on_exit()

# Register handlers
atexit.register(cleanup_on_exit)
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def create_testnet_exchange():
    """Create a Binance Testnet exchange instance"""
    exchange = ccxt.binance({
        'enableRateLimit': True,
        'options': {
            'defaultType': 'spot',
            'adjustForTimeDifference': True,
            'loadMarkets': True,
            'fetchCurrencies': False,
            'warnOnFetchOpenOrdersWithoutSymbol': False,
            'recvWindow': 10000,
        },
        'apiKey': api_key,
        'secret': api_secret,
        'urls': {
            'api': {
                'public': 'https://testnet.binance.vision/api/v3',
                'private': 'https://testnet.binance.vision/api/v3',
                'sapi': 'https://testnet.binance.vision/sapi/v1',
            }
        }
    })
    return exchange

def check_available_pairs(exchange):
    """Check available trading pairs on the exchange"""
    try:
        response = exchange.publicGetExchangeInfo()
        symbols = [s['symbol'] for s in response['symbols'] if s['status'] == 'TRADING']
        logger.info(f"Available trading pairs: {symbols}")
        return symbols
    except Exception as e:
        logger.error(f"Error checking available pairs: {e}")
        return []

def fetch_binance_testnet_data(symbol='FDUSDUSDC', timeframe='1h', limit=100):
    """Fetch OHLCV data from Binance Testnet"""
    try:
        exchange = create_testnet_exchange()
        logger.info("Connected to Binance Spot Test Network")
        
        exchange.privateGetAccount()
        response = exchange.publicGetExchangeInfo()
        market_info = next((m for m in response['symbols'] if m['symbol'] == symbol), None)

        if not market_info or market_info['status'] != 'TRADING':
            logger.error(f"Pair {symbol} is not available or not trading.")
            return None
            
        timeframe_map = {
            '1m': '1m', '3m': '3m', '5m': '5m', '15m': '15m', '30m': '30m',
            '1h': '1h', '2h': '2h', '4h': '4h', '6h': '6h', '8h': '8h', '12h': '12h',
            '1d': '1d', '3d': '3d', '1w': '1w', '1M': '1M'
        }
        interval = timeframe_map.get(timeframe, '1h')
        
        klines_params = {
            'symbol': symbol,
            'interval': interval,
            'limit': limit
        }
        
        logger.info(f"Fetching OHLCV data for {symbol} on timeframe {timeframe}...")
        klines = exchange.publicGetKlines(klines_params)
        
        if not klines:
            logger.error(f"No OHLCV data found for {symbol}.")
            return None
        
        ohlcv_df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignored'
        ])
        
        ohlcv_df['timestamp'] = pd.to_datetime(ohlcv_df['timestamp'].astype(float), unit='ms')
        ohlcv_df['open'] = ohlcv_df['open'].astype(float)
        ohlcv_df['high'] = ohlcv_df['high'].astype(float)
        ohlcv_df['low'] = ohlcv_df['low'].astype(float)
        ohlcv_df['close'] = ohlcv_df['close'].astype(float)
        ohlcv_df['volume'] = ohlcv_df['volume'].astype(float)
        ohlcv_df = ohlcv_df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
        
        logger.info(f"OHLCV data successfully fetched for {symbol}")
        return {
            'ohlcv_data': ohlcv_df,
            'exchange': exchange,
            'raw_symbol': symbol,
            'ccxt_symbol': convert_symbol_format(symbol)
        }
    except Exception as e:
        logger.error(f"Error fetching data: {str(e)}")
        logger.debug(traceback.format_exc())
        return None

def convert_symbol_format(raw_symbol):
    """Convert symbol format"""
    if raw_symbol == 'FDUSDUSDC':
        return 'FDUSD/USDC'
    elif raw_symbol == 'USDCUSDT':
        return 'USDC/USDT'
    else:
        for i in range(len(raw_symbol) - 1, 2, -1):
            base = raw_symbol[:i]
            quote = raw_symbol[i:]
            if len(base) >= 2 and len(quote) >= 2:
                return f"{base}/{quote}"
        return raw_symbol

def fetch_order_book_snapshots(exchange, symbol, limit=100, num_snapshots=10000, interval=1.0, recap_interval=50):
    """Fetch order book snapshots"""
    try:
        logger.info(f"Fetching order book snapshots for {symbol}...")
        if '/' not in symbol:
            symbol = convert_symbol_format(symbol)
        
        snapshots = []
        symbol_for_api = symbol.replace('/', '')
        last_error_time = 0
        error_count = 0
        MAX_ERRORS = 3
        ERROR_RESET_TIME = 60
        
        trader = StablecoinGridTrader(
            exchange=exchange,
            symbol=symbol,
            grid_lower=0.9980,
            grid_upper=0.9990,
            grid_step=0.0001,
            order_size=250.0,
            initial_capital=10000.0,
            fee_rate=0,
            recap_interval=recap_interval
        )
        
        # Initial order book test
        params = {'symbol': symbol_for_api, 'limit': limit}
        test_book = exchange.publicGetDepth(params)
        if not test_book or 'bids' not in test_book or 'asks' not in test_book:
            logger.error("Invalid order book format")
            return [], {}
            
        logger.info(f"Order book fetch test successful for {symbol}")
        
        for i in range(num_snapshots):
            if interrupted:
                break
            
            try:
                current_time = time.time()
                if current_time - last_error_time > ERROR_RESET_TIME:
                    error_count = 0
                
                if error_count >= MAX_ERRORS:
                    logger.error(f"Too many consecutive errors ({MAX_ERRORS}), stopping process")
                    break
                
                params = {'symbol': symbol_for_api, 'limit': limit}
                book_data = exchange.publicGetDepth(params)
                
                if not isinstance(book_data, dict) or 'bids' not in book_data or 'asks' not in book_data:
                    raise ValueError("Invalid order book format")
                
                if not book_data['bids'] or not book_data['asks']:
                    raise ValueError("Empty order book")
                
                bids = [[float(p), float(q)] for p, q in book_data['bids'][:limit]]
                asks = [[float(p), float(q)] for p, q in book_data['asks'][:limit]]
                
                if not bids or not asks:
                    raise ValueError("Invalid bid/ask data")
                
                order_book = {
                    'bids': bids,
                    'asks': asks,
                    'timestamp': pd.to_datetime(current_time, unit='s'),
                    'datetime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'nonce': None
                }
                
                best_bid = order_book['bids'][0][0]
                best_ask = order_book['asks'][0][0]
                current_price = (best_bid + best_ask) / 2
                
                log_snapshot(logger, {
                    'snapshot_id': i+1,
                    'best_bid': best_bid,
                    'best_ask': best_ask,
                    'current_price': current_price
                })
                
                if current_price:
                    trader.process_snapshot(order_book, current_price, i+1)
                
                snapshots.append(order_book)
                if i < num_snapshots - 1 and not interrupted:
                    time.sleep(interval)
                    
                error_count = 0
                
            except Exception as e:
                error_count += 1
                last_error_time = current_time
                time.sleep(interval)
                continue
                
        if snapshots:
            trader.force_sell_all(current_price)
            
        return snapshots, trader.get_results()
        
    except Exception as e:
        logger.error(f"Error fetching snapshots: {e}")
        return snapshots if 'snapshots' in locals() else [], {}

class StablecoinGridTrader:
    def __init__(self, 
                 exchange,
                 symbol: str = 'FDUSD/USDC',
                 grid_lower: float = 0.9980,  
                 grid_upper: float = 0.9990,  
                 grid_step: float = 0.0001,
                 order_size: float = 250.0,
                 initial_capital: float = 10000.0,
                 fee_rate: float = 0.000,
                 max_active_orders: int = 40,
                 max_pending_buys: int = 11,
                 recap_interval: int = 50):
        self.exchange = exchange
        self.symbol = symbol.replace('/', '')  # 'FDUSDUSDC'
        self.grid_lower = grid_lower
        self.grid_upper = grid_upper
        self.grid_step = grid_step
        self.order_size = order_size
        self.initial_capital = initial_capital
        self.fee_rate = fee_rate
        self.max_active_orders = max_active_orders
        self.max_pending_buys = max_pending_buys
        self.recap_interval = recap_interval
        
        self.portfolio = {'base': 0, 'quote': 0}
        self.trades = []
        self.total_profit = 0
        self.trade_profits = []
        
        self.grid_levels = np.arange(grid_lower, grid_upper + grid_step, grid_step)
        self.active_orders = 0
        self.pending_buys = 0
        self.pending_sells = 0
        self.level_orders = {level: 0 for level in self.grid_levels}
        self.last_purchase_price = {level: None for level in self.grid_levels}
        self.orders = {}
        self.last_price = None
        self.last_recap_snapshot = 0

        self.symbol_info = self._get_symbol_info()
        self.lot_size_filter = self._get_lot_size_filter()
        self.price_filter = self._get_price_filter()
        self.reserved_quote = 0
        self.reserved_base = 0
        self.placed_orders = set()
        success = self.update_balance()
        logger.info(f"Initial balance update: base={self.portfolio['base']:.8f} FDUSD, quote={self.portfolio['quote']:.8f} USDC")
        self.initial_quote = self.portfolio['quote']
        
        self.MIN_ORDER_VALUE = 10.0
        self.SAFETY_MARGIN = 1.02
        self.order_count = 0

    def _get_symbol_info(self):
        """Fetch symbol information"""
        try:
            response = self.exchange.publicGetExchangeInfo()
            return next((s for s in response['symbols'] if s['symbol'] == self.symbol), None)
        except Exception as e:
            logger.error(f"Error fetching symbol info: {e}")
            return None

    def _get_lot_size_filter(self):
        """Fetch LOT_SIZE filter"""
        if self.symbol_info:
            return next((f for f in self.symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
        return None

    def _get_price_filter(self):
        """Fetch PRICE_FILTER"""
        if self.symbol_info:
            return next((f for f in self.symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
        return None

    def _adjust_quantity(self, quantity):
        """Adjust quantity based on LOT_SIZE filter"""
        if not self.lot_size_filter:
            return quantity
        
        step_size = float(self.lot_size_filter['stepSize'])
        min_qty = float(self.lot_size_filter['minQty'])
        max_qty = float(self.lot_size_filter['maxQty'])
        
        precision = len(str(step_size).split('.')[-1]) if '.' in str(step_size) else 0
        adjusted_quantity = round(quantity / step_size) * step_size
        adjusted_quantity = float(f"%.{precision}f" % adjusted_quantity)
        
        if adjusted_quantity < min_qty:
            adjusted_quantity = min_qty
        elif adjusted_quantity > max_qty:
            adjusted_quantity = max_qty
            
        return adjusted_quantity

    def _adjust_price(self, price):
        """Adjust price based on PRICE_FILTER"""
        if not self.price_filter:
            return price
            
        tick_size = float(self.price_filter['tickSize'])
        precision = len(str(tick_size).split('.')[-1]) if '.' in str(tick_size) else 0
        adjusted_price = round(price / tick_size) * tick_size
        return float(f"%.{precision}f" % adjusted_price)

    def update_balance(self):
        """Update account balance with explicit asset names"""
        try:
            balance = self.exchange.privateGetAccount()
            if 'balances' in balance:
                for asset in balance['balances']:
                    if asset['asset'] == 'FDUSD':
                        self.portfolio['base'] = float(asset['free'])
                        self.reserved_base = float(asset['locked'])
                    elif asset['asset'] == 'USDC':
                        self.portfolio['quote'] = float(asset['free'])
                        self.reserved_quote = float(asset['locked'])
                return True
        except Exception as e:
            logger.error(f"Error updating balance: {e}")
            return False
        
    def get_available_quote(self):
        """Return available quote balance"""
        return self.portfolio['quote'] - self.reserved_quote

    def get_available_base(self):
        """Return available base balance"""
        return self.portfolio['base'] - self.reserved_base

    def check_balance(self, side, amount, price):
        """Check if balance is sufficient"""
        try:
            if side == 'BUY':
                required = amount * price * (1 + self.fee_rate)
                available = self.get_available_quote()
                return available >= required
            else:
                available = self.get_available_base()
                return available >= amount
        except Exception as e:
            logger.error(f"Error checking balance: {e}")
            return False

    def reserve_funds(self, side, amount, price):
        """Reserve funds for an order"""
        if side == 'BUY':
            required = amount * price * (1 + self.fee_rate)
            if self.get_available_quote() >= required:
                self.reserved_quote += required
                return True
        else:
            if self.get_available_base() >= amount:
                self.reserved_base += amount
                return True
        return False

    def release_funds(self, side, amount, price):
        """Release reserved funds"""
        if side == 'BUY':
            required = amount * price * (1 + self.fee_rate)
            self.reserved_quote = max(0, self.reserved_quote - required)
        else:
            self.reserved_base = max(0, self.reserved_base - amount)

    def _place_order_on_exchange(self, side, price, amount):
        """Place an order on Binance Testnet"""
        try:
            adjusted_price = self._adjust_price(price)
            adjusted_amount = self._adjust_quantity(amount)
            
            required_quote = adjusted_amount * adjusted_price * (1 + self.fee_rate) if side == 'BUY' else 0
            if side == 'BUY' and required_quote > self.get_available_quote():
                logger.warning(f"Insufficient funds - Required: {required_quote:.2f}, Available: {self.get_available_quote():.2f}")
                return None

            if not self.reserve_funds(side, adjusted_amount, adjusted_price):
                return None
            
            params = {
                'symbol': self.symbol,
                'side': side,
                'type': 'LIMIT',
                'timeInForce': 'GTC',
                'quantity': f"{adjusted_amount:.8f}",
                'price': f"{adjusted_price:.8f}"
            }
            
            response = self.exchange.privatePostOrder(params)
            return {
                'order_id': response['orderId'],
                'status': response['status'],
                'executed_qty': float(response['executedQty'])
            }
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            self.release_funds(side, adjusted_amount, adjusted_price)
            return None

    def initialize_grid_orders(self):
        """Initialize limit buy orders"""
        if not self.update_balance():
            logger.error("Unable to update balance")
            return

        available_quote = self.get_available_quote()
        safe_order_size = min(
            self.order_size,
            (available_quote / self.SAFETY_MARGIN) / (self.max_pending_buys + 1)
        )

        if safe_order_size < self.MIN_ORDER_VALUE:
            return

        max_orders = min(
            self.max_pending_buys - self.pending_buys,
            int(available_quote / (safe_order_size * self.SAFETY_MARGIN))
        )

        orders_placed = 0
        for level in self.grid_levels:
            if orders_placed >= max_orders:
                break

            if (level not in self.orders and 
                self.last_purchase_price[level] is None and
                level not in self.placed_orders):
                
                buy_amount = safe_order_size / level
                if self.check_balance('BUY', buy_amount, level):
                    if self._place_buy_order(level, buy_amount):
                        orders_placed += 1
                        self.placed_orders.add(level)
                else:
                    break

    def process_snapshot(self, snapshot, current_price, snapshot_id: int):
        """Process a snapshot and manage orders"""
        if not current_price:
            return

        self.last_price = current_price
        best_bid = snapshot['bids'][0][0] if snapshot['bids'] else None
        best_ask = snapshot['asks'][0][0] if snapshot['asks'] else None
        
        if not best_bid or not best_ask:
            logger.warning(f"Snapshot {snapshot_id} without valid bid/ask, skipped")
            return

        eligible_buy_orders = [
            (price_level, order) for price_level, order in self.orders.items()
            if order['status'] == 'PENDING' and order['type'] == 'LIMIT_BUY' 
            and abs(price_level - best_ask) < self.grid_step / 2
        ]
        
        if eligible_buy_orders:
            price_level, order = max(eligible_buy_orders, key=lambda x: x[0])
            self._execute_limit_buy(order, price_level, best_ask)
            sell_level = price_level + self.grid_step
            if self.grid_lower <= sell_level <= self.grid_upper:
                self._place_sell_order(price_level, sell_level)

        for price_level, order in list(self.orders.items()):
            if order['status'] == 'PENDING' and order['type'] == 'LIMIT_SELL' and best_bid >= order['price']:
                self._execute_limit_sell(order, price_level)
                break

        if self.pending_buys < self.max_pending_buys:
            self.initialize_grid_orders()

        if (snapshot_id - self.last_recap_snapshot) >= self.recap_interval:
            results = self.get_current_results(snapshot_id)
            self._print_periodic_recap(results)
            self.last_recap_snapshot = snapshot_id

    def _place_buy_order(self, level, amount):
        """Place a limit buy order"""
        if not (self.grid_lower <= level <= self.grid_upper):
            logger.warning(f"Attempt to place order outside grid: {level:.8f}")
            return False

        required_quote = amount * level * self.SAFETY_MARGIN
        available_quote = self.get_available_quote()
        
        if available_quote < required_quote:
            logger.warning(f"Insufficient funds - Required: {required_quote:.2f}, Available: {available_quote:.2f}")
            return False

        order_response = self._place_order_on_exchange('BUY', level, amount)
        
        if order_response:
            value = amount * level
            order = {
                'order_id': order_response['order_id'],
                'type': 'LIMIT_BUY',
                'price': level,
                'amount': amount,
                'value': value,
                'status': 'PENDING',
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            self.orders[level] = order
            self.pending_buys += 1
            self.order_count += 1
            logger.info(f"PLACE - BUY | Level: {level:.8f} | Amount: {amount:.8f} | Value: {value:.2f} | OrderID: {order_response['order_id']}")
            return True
        return False

    def _place_sell_order(self, buy_level, sell_level):
        """Place a limit sell order"""
        if not (self.grid_lower <= sell_level <= self.grid_upper):
            return False

        buy_price = self.last_purchase_price[buy_level]
        sell_amount = self.order_size / buy_price

        order_response = self._place_order_on_exchange('SELL', sell_level, sell_amount)
        
        if order_response:
            sell_value = sell_amount * sell_level
            order = {
                'order_id': order_response['order_id'],
                'type': 'LIMIT_SELL',
                'price': sell_level,
                'amount': sell_amount,
                'value': sell_value,
                'original_buy_level': buy_level,
                'status': 'PENDING',
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            self.orders[sell_level] = order
            self.pending_sells += 1
            logger.info(f"PLACE - SELL | Level: {sell_level:.8f} | Amount: {sell_amount:.8f} | OrderID: {order_response['order_id']}")
            return True
        return False

    def _execute_limit_buy(self, order, level, best_ask):
        """Execute a limit buy order"""
        execution_price = order['price']
        fee = self.order_size * self.fee_rate
        amount = self.order_size / execution_price
        value = self.order_size
        
        self.portfolio['base'] += amount
        self.portfolio['quote'] -= (value + fee)
        self.last_purchase_price[level] = execution_price
        self.level_orders[level] += 1
        self.active_orders += 1
        self.pending_buys -= 1
        
        trade_log = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'type': 'BUY',
            'level': level,
            'price': float(f"{execution_price:.8f}"),
            'amount': amount,
            'value': value,
            'fee': fee,
            'profit': 0,
            'portfolio_base': self.portfolio['base'],
            'portfolio_quote': self.portfolio['quote']
        }
        self.trades.append(trade_log)
        log_trade(logger, trade_log)
        del self.orders[level]

    def _execute_limit_sell(self, order, level):
        """Execute a limit sell order"""
        sell_value = order['value']
        fee = self.order_size * self.fee_rate
        
        self.portfolio['quote'] += (sell_value - fee)
        self.portfolio['base'] -= order['amount']
        
        buy_level = order.get('original_buy_level', level - self.grid_step)
        buy_price = self.last_purchase_price[buy_level]
        buy_amount = order['amount']
        buy_value = self.order_size
        buy_fee = buy_value * self.fee_rate
        buy_cost = buy_value + buy_fee
        
        profit = (sell_value - fee) - buy_cost
        
        self.trade_profits.append(profit)
        self.total_profit += profit
        
        trade_log = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'type': 'SELL',
            'level': level,
            'price': float(f"{order['price']:.8f}"),
            'amount': order['amount'],
            'value': sell_value,
            'fee': fee,
            'profit': profit,
            'portfolio_base': self.portfolio['base'],
            'portfolio_quote': self.portfolio['quote']
        }
        self.trades.append(trade_log)
        log_trade(logger, trade_log)
        
        del self.orders[level]
        self.level_orders[buy_level] = 0
        self.active_orders -= 1
        self.last_purchase_price[buy_level] = None
        self.pending_sells -= 1

    def get_current_results(self, snapshot_id: int) -> dict:
        """Return current trading results"""
        trade_profits = [t['profit'] for t in self.trades if t['type'] in ['SELL', 'FINAL_SELL']]
        buy_trades = [t for t in self.trades if t['type'] == 'BUY']
        sell_trades = [t for t in self.trades if t['type'] in ['SELL', 'FINAL_SELL']]
        
        # Update balance to ensure latest values
        self.update_balance()
        
        # Unrealized profit based on current base holdings
        unrealized_profit = 0
        if self.portfolio['base'] > 0 and self.last_price:
            current_value = self.portfolio['base'] * self.last_price
            unrealized_profit = current_value  # Unrealized gain/loss from base holdings
        
        # Total profit is realized trades plus unrealized, relative to initial_quote
        total_profit = self.total_profit + unrealized_profit
        
        return {
            'snapshot_id': snapshot_id,
            'symbol': self.symbol,
            'current_portfolio_quote': self.portfolio['quote'],
            'current_portfolio_base': self.portfolio['base'],
            'quote_currency': 'USDC',  # Hardcoded for clarity
            'base_currency': 'FDUSD',  # Hardcoded for clarity
            'initial_capital': self.initial_capital,  # Display only, not used for calc
            'total_profit': self.portfolio['quote'] + unrealized_profit - self.initial_quote,
            'roi_percentage': ((self.portfolio['quote'] + unrealized_profit - self.initial_quote) / self.initial_quote) * 100 if self.initial_quote > 0 else 0,
            'total_trades': len(self.trades),
            'total_trades_buy': len(buy_trades),
            'total_trades_sell': len(sell_trades),
            'active_orders': self.active_orders,
            'pending_buys': self.pending_buys,
            'pending_sells': self.pending_sells,
            'avg_profit_per_trade': np.mean(trade_profits) if trade_profits else 0,
            'median_profit_per_trade': np.median(trade_profits) if trade_profits else 0,
        }

    def _print_periodic_recap(self, results: dict):
        """Print periodic recap"""
        recap = (
            f"\n=== Trading Recap (Snapshot {results['snapshot_id']}) ===\n"
            f"Pair: {results['symbol']}\n"
            f"Portfolio Quote: {results['current_portfolio_quote']:.8f} {results['quote_currency']}\n"
            f"Portfolio Base: {results['current_portfolio_base']:.8f} {results['base_currency']}\n"
            f"Initial Capital (Target): {results['initial_capital']:.2f} {results['quote_currency']}\n"
            f"Total Profit: {results['total_profit']:.2f} {results['quote_currency']}\n"
            f"ROI: {results['roi_percentage']:.2f}%\n"
            f"\nTrade Stats:\n"
            f"Total Trades Executed: {results['total_trades']}\n"
            f"  - Buy Trades: {results['total_trades_buy']}\n"
            f"  - Sell Trades: {results['total_trades_sell']}\n"
            f"Active Orders: {results['active_orders']} (open positions)\n"
            f"Pending Buy Orders: {results['pending_buys']}\n"
            f"Pending Sell Orders: {results['pending_sells']}\n"
            f"\nProfits (from completed sells):\n"
            f"Avg/Trade: {results['avg_profit_per_trade']:.4f} {results['quote_currency']}\n"
            f"Median/Trade: {results['median_profit_per_trade']:.4f} {results['quote_currency']}\n"
        )
        logger.info(recap)

    def force_sell_all(self, current_price):
        """Force sell all assets"""
        if self.portfolio['base'] > 0:
            order_response = self._place_order_on_exchange('SELL', current_price, self.portfolio['base'])
            if order_response:
                logger.info(f"FORCE SELL | Price: {current_price:.8f} | Amount: {self.portfolio['base']:.8f}")
                self.portfolio['base'] = 0

    def get_results(self):
        """Return trading results"""
        total_fees = sum(t.get('fee', 0) for t in self.trades)
        return {
            'total_trades': len(self.trades),
            'final_portfolio': self.portfolio,
            'total_profit': self.total_profit,
            'total_fees': total_fees
        }

def log_trade(logger, trade):
    """Log individual trade information"""
    trade_info = (
        f"TRADE - {trade['type']} | "
        f"Price: {trade['price']:.8f} | "
        f"Amount: {trade['amount']:.8f} | "
        f"Value: {trade['value']:.2f} | "
        f"Fee: {trade['fee']:.2f} | "
        f"Profit: {trade['profit']:.2f} | "
        f"Portfolio base: {trade['portfolio_base']:.4f} | "
        f"Portfolio quote: {trade['portfolio_quote']:.2f}"
    )
    logger.info(trade_info)

def verify_initial_balance(exchange, symbol):
    """Verify initial account balance"""
    try:
        balance = exchange.privateGetAccount()
        if 'balances' not in balance:
            logger.error("Unable to fetch balances")
            return False
            
        # Correctly extract base and quote currencies
        if symbol == 'FDUSDUSDC':
            base_currency = 'FDUSD'  # 5 characters
            quote_currency = 'USDC'  # 4 characters
        else:
            # Fallback for other symbols (assuming 4-character split)
            base_currency = symbol[:4]
            quote_currency = symbol[4:]
        
        balances = {
            asset['asset']: {
                'free': float(asset['free']),
                'locked': float(asset['locked'])
            }
            for asset in balance['balances']
            if asset['asset'] in [base_currency, quote_currency]
        }
        
        logger.info(f"\n=== Balance Check for {symbol} ===")
        
        if base_currency not in balances:
            logger.error(f"Balance {base_currency} not found")
            return False
        logger.info(f"Balance {base_currency}: {balances[base_currency]['free']:.8f}")
            
        if quote_currency not in balances:
            logger.error(f"Balance {quote_currency} not found")
            return False
        logger.info(f"Balance {quote_currency}: {balances[quote_currency]['free']:.8f}")
            
        base_balance = balances[base_currency]['free']
        quote_balance = balances[quote_currency]['free']
        
        if quote_balance < 100:
            logger.error(f"Insufficient {quote_currency} balance: {quote_balance:.2f}")
            return False
            
        logger.info(f"\nSufficient balances for trading:")
        logger.info(f"{base_currency}: {base_balance:.2f}")
        logger.info(f"{quote_currency}: {quote_balance:.2f}")
        return True
        
    except Exception as e:
        logger.error(f"Error verifying balances: {e}")
        logger.error(traceback.format_exc())
        return False

def reset_account_balance(exchange, target_usdc=10000.0):
    """Reset account balances to 0 FDUSD and 0 USDC (if > 1), then adjust to 10000 USDC total"""
    try:
        logger.info("=== Initial Balance State ===")
        balance = exchange.privateGetAccount({'recvWindow': 5000})
        
        initial_balances = {
            asset['asset']: float(asset['free'])
            for asset in balance['balances']
            if asset['asset'] in ['BTC', 'USDC', 'FDUSD']
        }
        for asset, amount in initial_balances.items():
            logger.info(f"Balance {asset}: {amount:.8f}")

        usdc_balance = initial_balances.get('USDC', 0)
        
        # Step 1: Convert FDUSD to USDC if > 1
        fdusd_balance = initial_balances.get('FDUSD', 0)
        if fdusd_balance > 1:
            logger.info(f"Converting FDUSD to USDC: {fdusd_balance:.8f}")
            symbol_info = exchange.publicGetExchangeInfo()
            fdusdusdc_info = next((s for s in symbol_info['symbols'] if s['symbol'] == 'FDUSDUSDC'), None)
            if not fdusdusdc_info:
                logger.error("FDUSDUSDC pair not available")
                return False
            lot_size_filter = next((f for f in fdusdusdc_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            step_size = float(lot_size_filter['stepSize'])
            precision = len(str(step_size).split('.')[-1]) if '.' in str(step_size) else 0
            adjusted_fdusd = round(fdusd_balance / step_size) * step_size
            adjusted_fdusd = float(f"%.{precision}f" % adjusted_fdusd)
            
            params = {
                'symbol': 'FDUSDUSDC',
                'side': 'SELL',
                'type': 'MARKET',
                'quantity': f"{adjusted_fdusd:.8f}",
                'recvWindow': 5000
            }
            response = exchange.privatePostOrder(params)
            logger.info(f"Converted {adjusted_fdusd:.8f} FDUSD to USDC - Response: {response}")
            time.sleep(1)
            balance = exchange.privateGetAccount({'recvWindow': 5000})
            usdc_balance = float(next(
                (asset['free'] for asset in balance['balances'] if asset['asset'] == 'USDC'),
                0
            ))
            logger.info(f"USDC balance after FDUSD conversion: {usdc_balance:.8f}")
        else:
            logger.info(f"FDUSD balance {fdusd_balance:.8f} <= 1, skipping conversion")

        # Step 2: Convert USDC to BTC if > 1
        if usdc_balance > 1:
            logger.info(f"Converting all USDC to BTC: {usdc_balance:.8f}")
            symbol_info = exchange.publicGetExchangeInfo()
            btcusdc_info = next((s for s in symbol_info['symbols'] if s['symbol'] == 'BTCUSDC'), None)
            if not btcusdc_info:
                logger.error("BTCUSDC pair not available on Testnet")
                return False
            
            btc_price = float(exchange.publicGetTickerPrice({'symbol': 'BTCUSDC'})['price'])
            logger.info(f"BTCUSDC price: {btc_price:.2f}")
            
            params = {
                'symbol': 'BTCUSDC',
                'side': 'BUY',
                'type': 'MARKET',
                'quoteOrderQty': f"{usdc_balance:.8f}",
                'recvWindow': 5000
            }
            response = exchange.privatePostOrder(params)
            logger.info(f"Converted {usdc_balance:.8f} USDC to BTC - Response: {response}")
            time.sleep(1)
        else:
            logger.info(f"USDC balance {usdc_balance:.8f} <= 1, skipping conversion")
        
        # Verify balances after conversions
        balance = exchange.privateGetAccount({'recvWindow': 5000})
        usdc_balance = float(next(
            (asset['free'] for asset in balance['balances'] if asset['asset'] == 'USDC'),
            0
        ))
        fdusd_balance = float(next(
            (asset['free'] for asset in balance['balances'] if asset['asset'] == 'FDUSD'),
            0
        ))
        btc_balance = float(next(
            (asset['free'] for asset in balance['balances'] if asset['asset'] == 'BTC'),
            0
        ))
        logger.info(f"Intermediate USDC balance: {usdc_balance:.8f}")
        logger.info(f"Intermediate FDUSD balance: {fdusd_balance:.8f}")
        logger.info(f"Intermediate BTC balance: {btc_balance:.8f}")

        # Step 3: Convert BTC to reach 10000 USDC total
        if btc_balance > 0:
            symbol_info = exchange.publicGetExchangeInfo()
            btcusdc_info = next((s for s in symbol_info['symbols'] if s['symbol'] == 'BTCUSDC'), None)
            if not btcusdc_info:
                logger.error("BTCUSDC pair not available on Testnet")
                return False
            lot_size_filter = next((f for f in btcusdc_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            step_size = Decimal(str(lot_size_filter['stepSize']))
            precision = len(str(step_size).split('.')[-1]) if '.' in str(step_size) else 0
            
            btc_price = float(exchange.publicGetTickerPrice({'symbol': 'BTCUSDC'})['price'])
            logger.info(f"BTCUSDC price: {btc_price:.2f}")
            usdc_needed = target_usdc - usdc_balance
            btc_needed = usdc_needed / btc_price * 1.015  # Keep buffer for now
            logger.info(f"BTC needed for {usdc_needed} USDC (to reach {target_usdc} total): {btc_needed:.8f}")
            btc_to_convert = min(btc_balance, btc_needed)
            logger.info(f"BTC to convert (before adjustment): {btc_to_convert:.8f}")
            
            btc_to_convert_dec = Decimal(str(btc_to_convert))
            adjusted_btc_dec = (btc_to_convert_dec // step_size) * step_size
            if adjusted_btc_dec < step_size:
                adjusted_btc_dec = step_size
            adjusted_btc = float(adjusted_btc_dec.quantize(Decimal('0.' + '0' * precision), rounding=ROUND_FLOOR))
            logger.info(f"Adjusted BTC quantity: {adjusted_btc:.8f}")
            
            if adjusted_btc > 0:
                params = {
                    'symbol': 'BTCUSDC',
                    'side': 'SELL',
                    'type': 'MARKET',
                    'quantity': f"{adjusted_btc:.8f}",
                    'recvWindow': 5000
                }
                response = exchange.privatePostOrder(params)
                logger.info(f"Converted {adjusted_btc:.8f} BTC to USDC - Response: {response}")
                time.sleep(1)
            else:
                logger.error("Adjusted BTC quantity is 0, conversion aborted")
                return False
        else:
            logger.error("No BTC available to convert to USDC")
            return False

        # Final verification
        balance = exchange.privateGetAccount({'recvWindow': 5000})
        final_usdc = float(next(
            (asset['free'] for asset in balance['balances'] if asset['asset'] == 'USDC'),
            0
        ))
        final_fdusd = float(next(
            (asset['free'] for asset in balance['balances'] if asset['asset'] == 'FDUSD'),
            0
        ))
        final_btc = float(next(
            (asset['free'] for asset in balance['balances'] if asset['asset'] == 'BTC'),
            0
        ))
        logger.info(f"Final USDC balance: {final_usdc:.8f}")
        logger.info(f"Final FDUSD balance: {final_fdusd:.8f}")
        logger.info(f"Final BTC balance: {final_btc:.8f}")
        
        if final_usdc >= target_usdc - 200 and final_usdc <= target_usdc + 200 and final_fdusd <= 1:
            return True
        else:
            logger.error(f"Final balances incorrect - USDC: {final_usdc:.8f}, FDUSD: {final_fdusd:.8f}")
            return False

    except Exception as e:
        logger.error(f"Error resetting balances: {str(e)}")
        logger.debug(traceback.format_exc())
        return False

    
def run_backtest_with_testnet_data():
    """Run the backtest"""
    try:
        print("Configuring backtest on Binance Testnet:")
        
        symbol = input("Symbol (default FDUSDUSDC): ") or "FDUSDUSDC"
        
        exchange = create_testnet_exchange()
        available_pairs = check_available_pairs(exchange)
        if symbol not in available_pairs:
            logger.error(f"{symbol} not available on Testnet")
            return
        
        if not reset_account_balance(exchange):
            logger.error("Failed to reset account balances")
            return
        
        if not verify_initial_balance(exchange, symbol):
            logger.error("Initial balance verification failed")
            return
        
        logger.info(f"Fetching OHLCV data for {symbol} on timeframe 1h...")
        ohlcv_data = exchange.publicGetKlines({'symbol': symbol, 'interval': '1h', 'limit': 100})
        if ohlcv_data:
            logger.info(f"OHLCV data successfully fetched for {symbol}")
        
        num_snapshots = int(input("Number of order book snapshots (default 10000): ") or "10000")
        interval = float(input("Interval between snapshots in seconds (default 1): ") or "1")
        grid_lower = float(input("Grid lower level (default 0.9980): ") or "0.9980")
        grid_upper = float(input("Grid upper level (default 0.9990): ") or "0.9990")
        grid_step = float(input("Grid step (default 0.0001): ") or "0.0001")
        order_size = float(input("Order size (default 250.0): ") or "250.0")
        initial_capital = float(input("Initial capital (default 10000.0): ") or "10000.0")
        fee_rate = float(input("Fee rate per trade (default 0): ") or "0")
        max_pending_buys = int(input("Max pending buys (default 11): ") or "11")
        recap_interval = int(input("Recap interval (snapshots, default 50): ") or "50")

        data = {
            'exchange': exchange,
            'raw_symbol': symbol,
            'ccxt_symbol': convert_symbol_format(symbol)
        }

        trader = StablecoinGridTrader(
            exchange=data['exchange'],
            symbol=data['ccxt_symbol'],
            grid_lower=grid_lower,
            grid_upper=grid_upper,
            grid_step=grid_step,
            order_size=order_size,
            initial_capital=initial_capital,
            fee_rate=fee_rate,
            max_pending_buys=max_pending_buys,
            recap_interval=recap_interval
        )

        order_book_snapshots, results = fetch_order_book_snapshots(
            exchange, data['ccxt_symbol'], limit=100, 
            num_snapshots=num_snapshots, interval=interval,
            recap_interval=recap_interval
        )

        if not order_book_snapshots:
            logger.error("No snapshots retrieved.")
            return

        logger.info(f"Backtest completed - Total profit: {results.get('total_profit', 0)}")

    except KeyboardInterrupt:
        logger.info("User interruption detected")
    except Exception as e:
        logger.error(f"Error in backtest: {e}")
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    try:
        run_backtest_with_testnet_data()
    except KeyboardInterrupt:
        logger.info("Program interrupted by user.")
    except Exception as e:
        logger.error(f"Error running backtest: {e}")