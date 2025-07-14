# File: scanner_engine.py (v2 - Independent Loop)

import asyncio
# Импортируем только общий словарь с данными
from data_feeder import last_data

# --- КОНФИГУРАЦИЯ СКАНЕРА ---
# Определяем "плиту" как заявку объемом более $50,000
LARGE_ORDER_USD = 50000

async def scanner_main_loop():
    """
    Главный цикл сканера, который анализирует данные,
    полученные data_feeder'ом. Теперь работает в бесконечном цикле.
    """
    print("Scanner Engine loop started.")
    # Используем 'while True', так как жизненный цикл теперь
    # управляется созданием/отменой задачи в основном боте.
    while True:
        try:
            await asyncio.sleep(5) # Анализируем данные каждые 5 секунд

            # Копируем данные, чтобы избежать проблем с одновременным доступом
            current_data_snapshot = dict(last_data)

            print("\n--- Running Scan Cycle ---")
            for symbol, data in current_data_snapshot.items():
                if not data or not data.get('bids') or not data.get('asks'):
                    continue

                # Ищем крупные заявки на покупку (биды)
                for price, amount in data['bids']:
                    if price is None or amount is None: continue
                    order_value_usd = price * amount
                    if order_value_usd > LARGE_ORDER_USD:
                        print(f"✅ FOUND LARGE BID on {symbol}: {amount:.2f} @ ${price:.4f} (Value: ${order_value_usd:,.0f})")

                # Ищем крупные заявки на продажу (аски)
                for price, amount in data['asks']:
                    if price is None or amount is None: continue
                    order_value_usd = price * amount
                    if order_value_usd > LARGE_ORDER_USD:
                        print(f"🛑 FOUND LARGE ASK on {symbol}: {amount:.2f} @ ${price:.4f} (Value: ${order_value_usd:,.0f})")
        except asyncio.CancelledError:
            print("Scanner Engine loop cancelled.")
            break # Выходим из цикла при отмене
        except Exception as e:
            print(f"Error in Scanner Engine loop: {e}")
            await asyncio.sleep(10) # В случае другой ошибки, ждем и продолжаем
