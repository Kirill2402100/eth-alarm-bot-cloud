# File: data_feeder.py

import asyncio
import ccxt.pro as ccxtpro
from telegram import constants

# Наш "белый список" активов для Фазы 1
SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT',
    'WLD/USDT:USDT', 'ORDI/USDT:USDT', 'PEPE/USDT:USDT'
]

# Глобальная переменная для управления состоянием цикла
is_running = False

async def data_feed_main_loop(app, chat_ids):
    """
    Главный цикл, который подключается к бирже и получает данные,
    отправляя периодические отчеты в Telegram.
    """
    global is_running
    is_running = True
    print("Data Feeder loop started.")

    exchange = ccxtpro.mexc({'options': {'defaultType': 'swap'}})

    last_report_time = asyncio.get_event_loop().time()
    last_data = {symbol: {} for symbol in SYMBOLS}

    async def trade_handler(trade):
        # Просто обновляем последнее известное состояние
        last_data[trade['symbol']]['last_price'] = trade['price']
        last_data[trade['symbol']]['last_side'] = trade['side']

    async def orderbook_handler(orderbook):
        # Обновляем лучшее предложение и спрос
        last_data[orderbook['symbol']]['best_bid'] = orderbook['bids'][0][0] if orderbook['bids'] else 'N/A'
        last_data[orderbook['symbol']]['best_ask'] = orderbook['asks'][0][0] if orderbook['asks'] else 'N/A'

    # Подписываемся на все потоки данных
    loops = [
        exchange.watch_trades_for_symbols(SYMBOLS, params={'unified': False}),
        exchange.watch_order_books_for_symbols(SYMBOLS)
    ]

    while is_running:
        try:
            # Используем gather для обработки всех сообщений по мере их поступления
            events = await asyncio.gather(*[asyncio.create_task(item) for item in loops], return_exceptions=True)
            for event in events:
                if isinstance(event, Exception):
                    continue # Игнорируем ошибки для краткости
                
                # Unified=False в watch_trades_for_symbols возвращает список сделок
                if isinstance(event, list):
                    for trade in event:
                       await trade_handler(trade)
                # watch_order_books_for_symbols возвращает один ордербук
                elif isinstance(event, dict) and 'bids' in event:
                    await orderbook_handler(event)

            # Отправляем сводный отчет в Telegram каждые 30 секунд
            current_time = asyncio.get_event_loop().time()
            if (current_time - last_report_time) > 30:
                report_lines = ["<b>🛰️ Data Feed Status: ACTIVE</b>"]
                for symbol, data in last_data.items():
                    side_emoji = "🟢" if data.get('last_side') == 'buy' else "🔴" if data.get('last_side') == 'sell' else "⚪️"
                    report_lines.append(
                        f"<code>{symbol.split(':')[0]:<10}</code> {side_emoji} Bid: {data.get('best_bid', 'N/A')} | Ask: {data.get('best_ask', 'N/A')}"
                    )
                
                for cid in chat_ids:
                    await app.bot.send_message(chat_id=cid, text="\n".join(report_lines), parse_mode=constants.ParseMode.HTML)
                
                last_report_time = current_time

        except Exception as e:
            print(f"Critical error in data feed main loop: {e}")
            await asyncio.sleep(10) # Пауза перед попыткой переподключения
    
    await exchange.close()
    print("Data Feeder loop stopped and connection closed.")


def stop_data_feed():
    """Функция для остановки цикла извне."""
    global is_running
    is_running = False
