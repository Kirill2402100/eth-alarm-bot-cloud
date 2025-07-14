# File: data_feeder.py (v3 - Final)

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
last_data = {symbol: {} for symbol in SYMBOLS}

# --- Циклы-обработчики для ОДНОГО символа ---

async def single_trade_loop(exchange, symbol):
    """Следит за сделками для одного конкретного символа."""
    while is_running:
        try:
            trades = await exchange.watch_trades(symbol)
            for trade in trades:
                last_data[symbol]['last_price'] = trade['price']
                last_data[symbol]['last_side'] = trade['side']
        except Exception as e:
            print(f"Error in trade loop for {symbol}: {e}")
            last_data[symbol]['error'] = 'TradeFeed Down'
            await asyncio.sleep(10)

async def single_orderbook_loop(exchange, symbol):
    """Следит за стаканом для одного конкретного символа."""
    while is_running:
        try:
            orderbook = await exchange.watch_order_book(symbol)
            last_data[symbol]['best_bid'] = orderbook['bids'][0][0] if orderbook['bids'] else 'N/A'
            last_data[symbol]['best_ask'] = orderbook['asks'][0][0] if orderbook['asks'] else 'N/A'
            if 'error' in last_data[symbol]: # Очищаем ошибку, если канал восстановился
                del last_data[symbol]['error']
        except Exception as e:
            print(f"Error in order book loop for {symbol}: {e}")
            last_data[symbol]['error'] = 'BookFeed Down'
            await asyncio.sleep(10)

# --- Задача-репортер (без изменений) ---

async def telegram_reporter_loop(app, chat_ids):
    """Каждые 30 секунд отправляет отчет в Telegram."""
    print("Telegram reporter loop started.")
    while is_running:
        await asyncio.sleep(30)
        report_lines = ["<b>🛰️ Data Feed Status: ACTIVE</b>"]
        for symbol in SYMBOLS:
            data = last_data.get(symbol, {})
            side_emoji = "🟢" if data.get('last_side') == 'buy' else "🔴" if data.get('last_side') == 'sell' else "⚪️"
            
            if 'error' in data:
                 report_lines.append(f"<code>{symbol.split(':')[0]:<10}</code> ⚠️ {data['error']}")
            else:
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
    global is_running
    is_running = True
    print("Data Feeder main loop initiated. Creating individual watchers...")

    exchange = ccxtpro.mexc({'options': {'defaultType': 'swap'}})
    
    tasks = []
    # Создаем по две задачи (сделки + стакан) для КАЖДОГО символа
    for symbol in SYMBOLS:
        tasks.append(single_trade_loop(exchange, symbol))
        tasks.append(single_orderbook_loop(exchange, symbol))
    
    # Добавляем одну задачу для отправки отчетов
    tasks.append(telegram_reporter_loop(app, chat_ids))

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        print("Data feed main loop cancelled.")
    finally:
        await exchange.close()
        print("Exchange connection closed.")

def stop_data_feed():
    global is_running
    is_running = False
    print("Stop command received for data feeder.")
