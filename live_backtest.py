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

# Variables globales
trade_logs = []
results_data = {}
interrupted = False

# Configuration des logs
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
    """Log market snapshot information"""
    snapshot_info = (
        f"SNAPSHOT - ID: {snapshot['snapshot_id']} | "
        f"Best bid: {snapshot['best_bid']:.8f} | "
        f"Best ask: {snapshot['best_ask']:.8f} | "
        f"Current price: {snapshot['current_price']:.8f}"
    )
    logger.info(snapshot_info)

def cleanup_on_exit():
    """Nettoyage à l'arrêt du programme"""
    global interrupted
    logger.info(f"Nettoyage et arrêt du programme (PID: {os.getpid()})...")
    interrupted = True
    os._exit(0)

def signal_handler(sig, frame):
    """Gestionnaire de signal"""
    logger.info(f"Signal {sig} reçu. Arrêt en cours...")
    cleanup_on_exit()

# Enregistrement des gestionnaires
atexit.register(cleanup_on_exit)
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def create_testnet_exchange():
    """Créer une instance d'échange Binance Testnet"""
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
        'apiKey': '',
        'secret': '',
        'urls': {
            'api': {
                'public': 'https://testnet.binance.vision/api/v3',
                'private': 'https://testnet.binance.vision/api/v3',
                'sapi': 'https://testnet.binance.vision/sapi/v1',
            }
        }
    })
    return exchange

def fetch_binance_testnet_data(symbol='FDUSDUSDC', timeframe='1h', limit=100):
    """Fetch OHLCV data from Binance Testnet"""
    try:
        exchange = create_testnet_exchange()
        logger.info("Connexion à Binance Spot Test Network établie")
        
        exchange.privateGetAccount()
        response = exchange.publicGetExchangeInfo()
        market_info = next((m for m in response['symbols'] if m['symbol'] == symbol), None)

        if not market_info or market_info['status'] != 'TRADING':
            logger.error(f"La paire {symbol} n'est pas disponible ou n'est pas activée.")
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
        
        logger.info(f"Récupération des données OHLCV pour {symbol} sur le timeframe {timeframe}...")
        klines = exchange.publicGetKlines(klines_params)
        
        if not klines:
            logger.error(f"Aucune donnée OHLCV trouvée pour {symbol}.")
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
        
        logger.info(f"Données OHLCV récupérées avec succès pour {symbol}")
        return {
            'ohlcv_data': ohlcv_df,
            'exchange': exchange,
            'raw_symbol': symbol,
            'ccxt_symbol': convert_symbol_format(symbol)
        }
    except Exception as e:
        logger.error(f"Erreur lors de la récupération des données: {str(e)}")
        logger.debug(traceback.format_exc())
        return None

def convert_symbol_format(raw_symbol):
    """Convertit le format du symbole"""
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
        logger.info(f"Récupération des snapshots de l'order book pour {symbol}...")
        if '/' not in symbol:
            symbol = convert_symbol_format(symbol)
        
        snapshots = []
        symbol_for_api = symbol.replace('/', '')
        
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
        
        params = {'symbol': symbol_for_api, 'limit': limit}
        test_book = exchange.publicGetDepth(params)
        test_book = {
            'bids': [[float(p), float(q)] for p, q in test_book['bids']],
            'asks': [[float(p), float(q)] for p, q in test_book['asks']],
            'timestamp': pd.to_datetime(time.time(), unit='s'),
            'datetime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'nonce': None
        }
        logger.info(f"Test de récupération d'order book réussi pour {symbol}")
        
        for i in range(num_snapshots):
            if interrupted:
                logger.info(f"Arrêt prématuré après {len(snapshots)} snapshots récupérés.")
                break
            
            try:
                params = {'symbol': symbol_for_api, 'limit': limit}
                book_data = exchange.publicGetDepth(params)
                order_book = {
                    'bids': [[float(p), float(q)] for p, q in book_data['bids']],
                    'asks': [[float(p), float(q)] for p, q in book_data['asks']],
                    'timestamp': pd.to_datetime(time.time(), unit='s'),
                    'datetime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'nonce': None
                }
                
                best_bid = order_book['bids'][0][0] if order_book['bids'] else None
                best_ask = order_book['asks'][0][0] if order_book['asks'] else None
                current_price = (best_bid + best_ask) / 2 if best_bid and best_ask else None
                
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
                    
            except Exception as e:
                logger.error(f"Erreur lors de la récupération du snapshot {i+1}: {e}")
                time.sleep(2)
                continue
                
        if snapshots:
            trader.force_sell_all(current_price)
            
        logger.info(f"Récupération de {len(snapshots)} snapshots terminée pour {symbol}.")
        return snapshots, trader.get_results()
        
    except Exception as e:
        logger.error(f"Erreur lors de la récupération des snapshots pour {symbol}: {e}")
        logger.error(traceback.format_exc())
        return snapshots if snapshots else [], {}

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
        self.symbol = symbol
        self.grid_lower = grid_lower
        self.grid_upper = grid_upper
        self.grid_step = grid_step
        self.order_size = order_size
        self.initial_capital = initial_capital
        self.fee_rate = fee_rate
        self.max_active_orders = max_active_orders
        self.max_pending_buys = max_pending_buys
        self.recap_interval = recap_interval
        
        self.portfolio = {'base': 0, 'quote': initial_capital}
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

    def initialize_grid_orders(self):
        """Initialise les ordres limites d'achat"""
        for level in self.grid_levels:
            if (self.portfolio['quote'] >= (self.order_size + self.order_size * self.fee_rate) and 
                self.pending_buys < self.max_pending_buys and 
                level not in self.orders and 
                self.last_purchase_price[level] is None):
                self._place_buy_order(level)

    def process_snapshot(self, snapshot, current_price, snapshot_id: int):
        """Traite un snapshot et gère les ordres"""
        if not current_price:
            return

        self.last_price = current_price
        best_bid = snapshot['bids'][0][0] if snapshot['bids'] else None
        best_ask = snapshot['asks'][0][0] if snapshot['asks'] else None
        
        if not best_bid or not best_ask:
            logger.warning(f"Snapshot {snapshot_id} sans bid/ask valides, ignoré")
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

    def _place_buy_order(self, level):
        """Place un ordre limite d'achat"""
        if not (self.grid_lower <= level <= self.grid_upper):
            logger.warning(f"Tentative de placement d'ordre hors grille : {level:.8f}")
            return False
        if level in self.orders:
            logger.info(f"Un ordre existe déjà au niveau {level:.8f}, aucun nouvel ordre placé")
            return False
        if self.portfolio['quote'] < (self.order_size + self.order_size * self.fee_rate):
            logger.warning(f"Fonds insuffisants pour placer un ordre à {level:.8f}")
            return False
        if self.pending_buys >= self.max_pending_buys:
            logger.warning("Nombre maximum d'achats en attente atteint")
            return False

        buy_amount = self.order_size / level
        fee = self.order_size * self.fee_rate
        order = {
            'type': 'LIMIT_BUY',
            'price': level,
            'amount': buy_amount,
            'value': self.order_size,
            'fee': fee,
            'status': 'PENDING',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        self.orders[level] = order
        self.pending_buys += 1
        logger.info(f"PLACE - BUY | Level: {level:.8f} | Amount: {buy_amount:.8f} | Value: {self.order_size:.2f}")
        return True

    def _place_sell_order(self, buy_level, sell_level):
        """Place un ordre limite de vente"""
        if not (self.grid_lower <= sell_level <= self.grid_upper):
            logger.warning(f"Tentative de placement d'ordre de vente hors grille : {sell_level:.8f}")
            return False
        
        if buy_level not in self.last_purchase_price or self.last_purchase_price[buy_level] is None:
            logger.warning(f"Aucun achat actif au niveau {buy_level:.8f}, vente non placée")
            return False
        
        buy_price = self.last_purchase_price[buy_level]
        sell_amount = self.order_size / buy_price
        
        if sell_amount > self.portfolio['base']:
            logger.warning(f"Quantité de base insuffisante pour vendre {sell_amount}")
            return False

        sell_value = sell_amount * sell_level
        fee = sell_value * self.fee_rate
        
        order = {
            'type': 'LIMIT_SELL',
            'price': sell_level,
            'amount': sell_amount,
            'value': sell_value,
            'fee': fee,
            'original_buy_level': buy_level,
            'status': 'PENDING',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        self.orders[sell_level] = order
        self.pending_sells += 1
        logger.info(f"PLACE - SELL | Level: {sell_level:.8f} | Amount: {sell_amount:.8f} | Value: {sell_value:.2f} | From Buy at: {buy_level:.8f}")
        return True

    def _execute_limit_buy(self, order, level, best_ask):
        """Exécute un ordre limite d'achat"""
        execution_price = order['price']
        fee = order['fee']
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
        """Exécute un ordre limite de vente"""
        sell_value = order['value']
        fee = order['fee']
        
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
        """Retourne les résultats actuels"""
        trade_profits = [t['profit'] for t in self.trades if t['type'] in ['SELL', 'FINAL_SELL']]
        buy_trades = [t for t in self.trades if t['type'] == 'BUY']
        sell_trades = [t for t in self.trades if t['type'] in ['SELL', 'FINAL_SELL']]
        
        unrealized_profit = 0
        if self.portfolio['base'] > 0 and self.last_price:
            current_value = self.portfolio['base'] * self.last_price
            initial_value = self.initial_capital - self.portfolio['quote']
            unrealized_profit = current_value - initial_value
        
        total_profit = self.total_profit + unrealized_profit
        
        base_currency = self.symbol.split('/')[0]
        quote_currency = self.symbol.split('/')[1]
        
        return {
            'snapshot_id': snapshot_id,
            'symbol': self.symbol,
            'current_portfolio_quote': self.portfolio['quote'],
            'current_portfolio_base': self.portfolio['base'],
            'quote_currency': quote_currency,
            'base_currency': base_currency,
            'initial_capital': self.initial_capital,
            'total_profit': total_profit,
            'roi_percentage': (total_profit / self.initial_capital) * 100 if self.initial_capital > 0 else 0,
            'total_trades': len(self.trades),
            'total_trades_buy': len(buy_trades),
            'total_trades_sell': len(sell_trades),
            'active_orders': self.active_orders,
            'pending_buys': self.pending_buys,
            'pending_sells': self.pending_sells,
            'avg_profit_per_trade': np.mean(trade_profits) if trade_profits else 0,
            'median_profit_per_trade': np.median(trade_profits) if trade_profits else 0,
            'trade_profits_min': min(trade_profits) if trade_profits else 0,
            'trade_profits_max': max(trade_profits) if trade_profits else 0
        }

    def _print_periodic_recap(self, results: dict):
        """Affiche un récapitulatif périodique"""
        recap = (
            f"\n=== Récapitulatif Trading (Snapshot {results['snapshot_id']}) ===\n"
            f"Paire: {results['symbol']}\n"
            f"Portfolio Quote: {results['current_portfolio_quote']:.2f} {results['quote_currency']}\n"
            f"Portfolio Base: {results['current_portfolio_base']:.4f} {results['base_currency']}\n"
            f"Capital Initial: {results['initial_capital']} {results['quote_currency']}\n"
            f"Profit Total: {results['total_profit']:.2f} {results['quote_currency']}\n"
            f"ROI: {results['roi_percentage']:.2f}%\n"
            f"\nStatistiques Trades:\n"
            f"Trades Total Exécutés: {results['total_trades']}\n"
            f"  - Achats Exécutés: {results['total_trades_buy']}\n"
            f"  - Ventes Exécutées: {results['total_trades_sell']}\n"
            f"Ordres d'Achat Actifs: {results['active_orders']} (positions ouvertes)\n"
            f"Ordres d'Achat en Attente: {results['pending_buys']} (achats non exécutés)\n"
            f"Ordres de Vente en Attente: {results['pending_sells']} (ventes non exécutées)\n"
            f"\nProfits (sur ventes réalisées):\n"
            f"Moyen/Trade: {results['avg_profit_per_trade']:.4f} {results['quote_currency']}\n"
            f"Médian/Trade: {results['median_profit_per_trade']:.4f} {results['quote_currency']}\n"
            f"Min: {results['trade_profits_min']:.4f} {results['quote_currency']}\n"
            f"Max: {results['trade_profits_max']:.4f} {results['quote_currency']}\n"
        )
        logger.info(recap)

    def force_sell_all(self, current_price):
        """Force la vente de tous les actifs"""
        if self.portfolio['base'] > 0:
            sell_value = self.portfolio['base'] * current_price
            fee = sell_value * self.fee_rate
            final_value = sell_value - fee
            avg_cost_per_unit = self.initial_capital / (self.portfolio['base'] + (self.portfolio['quote'] / current_price))
            initial_cost = self.portfolio['base'] * avg_cost_per_unit
            profit = final_value - initial_cost
            self.total_profit += profit
            self.portfolio['quote'] += final_value
            
            trade_log = {
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'type': 'FINAL_SELL',
                'price': float(f"{current_price:.8f}"),
                'amount': self.portfolio['base'],
                'value': sell_value,
                'fee': fee,
                'profit': profit,
                'portfolio_base': 0,
                'portfolio_quote': self.portfolio['quote']
            }
            self.trades.append(trade_log)
            self.trade_profits.append(profit)
            log_trade(logger, trade_log)
            self.portfolio['base'] = 0

    def get_results(self):
        """Retourne les résultats du trading"""
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
        f"TRADE - Type: {trade['type']} | "
        f"Price: {trade['price']:.8f} | "
        f"Amount: {trade['amount']:.8f} | "
        f"Value: {trade['value']:.2f} | "
        f"Fee: {trade['fee']:.2f} | "
        f"Profit: {trade['profit']:.2f} | "
        f"Portfolio base: {trade['portfolio_base']:.4f} | "
        f"Portfolio quote: {trade['portfolio_quote']:.2f}"
    )
    logger.info(trade_info)

def run_backtest_with_testnet_data():
    """Exécute le backtest"""
    logger = logging.getLogger(__name__)
    
    try:
        print("Configuration du backtest sur Binance Testnet:")
        
        symbol = input("Symbole (par défaut FDUSDUSDC): ") or "FDUSDUSDC"
        num_snapshots = int(input("Nombre de snapshots d'order book (par défaut 10000): ") or "10000")
        interval = float(input("Intervalle entre les snapshots en secondes (par défaut 1): ") or "1")
        grid_lower = float(input("Niveau inférieur de la grille (par défaut 0.9980): ") or "0.9980")
        grid_upper = float(input("Niveau supérieur de la grille (par défaut 0.9990): ") or "0.9990")
        grid_step = float(input("Pas de la grille (par défaut 0.0001): ") or "0.0001")
        order_size = float(input("Taille des ordres (par défaut 250.0): ") or "250.0")
        initial_capital = float(input("Capital initial (par défaut 10000.0): ") or "10000.0")
        fee_rate = float(input("Taux de frais par trade (par défaut 0): ") or "0")
        max_pending_buys = int(input("Nombre maximum d'achats en attente (par défaut 11): ") or "11")
        recap_interval = int(input("Intervalle de récapitulatif (snapshots, par défaut 50): ") or "50")

        data = fetch_binance_testnet_data(symbol=symbol)
        if not data:
            logger.error("Impossible de récupérer les données.")
            return

        exchange = data['exchange']
        ccxt_symbol = data['ccxt_symbol']
        
        trader = StablecoinGridTrader(
            exchange=exchange,
            symbol=ccxt_symbol,
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
            exchange, ccxt_symbol, limit=100, 
            num_snapshots=num_snapshots, interval=interval,
            recap_interval=recap_interval
        )

        if not order_book_snapshots:
            logger.error("Aucun snapshot récupéré.")
            return

        logger.info(f"Backtest terminé - Profit total: {results.get('total_profit', 0)}")

    except KeyboardInterrupt:
        logger.info("Interruption utilisateur détectée")
    except Exception as e:
        logger.error(f"Erreur dans le backtest: {e}")
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    try:
        run_backtest_with_testnet_data()
    except KeyboardInterrupt:
        logger.info("Programme interrompu par l'utilisateur.")
    except Exception as e:
        logger.error(f"Erreur lors de l'exécution du backtest: {e}")
