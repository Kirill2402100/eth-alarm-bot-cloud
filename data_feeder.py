# File: data_feeder.py (v2 - Corrected)

import asyncio
import ccxt.pro as ccxtpro
from telegram import constants

# Наш "белый список" активов для Фазы 1
SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT',
    'WLD/USDT:USDT', 'ORDI/USDT:USDT', 'PEPE/USDT:USDT'
]

# --- Глобальные переменные для обмена данными между задачами ---
is_running = False
# Общий словарь для хранения последних данных по каждому символу
last_data = {symbol: {} for symbol in SYMBOLS}

async def trades_loop(exchange):
    """Задача №1: Бесконечно получает данные о сделках."""
    print("Trade loop started.")
    while is_running:
        try:
            trades = await exchange.watch_trades_for_symbols(SYMBOLS)
            for trade in trades:
                symbol = trade['symbol']
                last_data[symbol]['last_price'] = trade['price']
                last_data[symbol]['last_side'] = trade['side']
        except Exception as e:
            print(f"Error in trades_loop: {e}")
            await asyncio.sleep(5)

async def orderbook_loop(exchange):
    """Задача №2: Бесконечно получает данные о стакане."""
    print("Order book loop started.")
    while is_running:
        try:
            orderbook = await exchange.watch_order_book_for_symbols(SYMBOLS)
            symbol = orderbook['symbol']
            last_data[symbol]['best_bid'] = orderbook['bids'][0][0] if orderbook['bids'] else 'N/A'
            last_data[symbol]['best_ask'] = orderbook['asks'][0][0] if orderbook['asks'] else 'N/A'
        except Exception as e:
            print(f"Error in orderbook_loop: {e}")
            await asyncio.sleep(5)

async def telegram_reporter_loop(app, chat_ids):
    """Задача №3: Каждые 30 секунд отправляет отчет в Telegram."""
    print("Telegram reporter loop started.")
    while is_running:
        await asyncio.sleep(30)
        report_lines = ["<b>🛰️ Data Feed Status: ACTIVE</b>"]
        for symbol, data in last_data.items():
            side_emoji = "🟢" if data.get('last_side') == 'buy' else "🔴" if data.get('last_side') == 'sell' else "⚪️"
            report_lines.append(
                f"<code>{symbol.split(':')[0]:<10}</code> {side_emoji} Bid: {data.get('best_bid', 'N/A')} | Ask: {data.get('best_ask', 'N/A')}"
            )
        
        message = "\n".join(report_lines)
        for cid in chat_ids:
            try:
                await app.bot.send_message(chat_id=cid, text=message, parse_mode=constants.ParseMode.HTML)
            except Exception as e:
                print(f"Failed to send message to {cid}: {e}")

# --- Управляющие функции ---

async def data_feed_main_loop(app, chat_ids):
    """
    Главная функция, которая запускает и управляет всеми задачами.
    """
    global is_running
    is_running = True
    print("Data Feeder main loop initiated.")

    exchange = ccxtpro.mexc({'options': {'defaultType': 'swap'}})
    
    # Запускаем три независимые задачи параллельно
    try:
        await asyncio.gather(
            trades_loop(exchange),
            orderbook_loop(exchange),
            telegram_reporter_loop(app, chat_ids)
        )
    except asyncio.CancelledError:
        print("Data feed main loop cancelled.")
    finally:
        await exchange.close()
        print("Exchange connection closed.")

def stop_data_feed():
    """Функция для остановки цикла извне."""
    global is_running
    is_running = False
    print("Stop command received for data feeder.")
