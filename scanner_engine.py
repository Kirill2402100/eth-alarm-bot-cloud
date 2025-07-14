# File: scanner_engine.py (v3 - Robust Unpacking)

import asyncio
# Импортируем только общий словарь с данными
from data_feeder import last_data

# --- КОНФИГУРАЦИЯ СКАНЕРА ---
# Определяем "плиту" как заявку объемом более $50,000
LARGE_ORDER_USD = 50000

async def scanner_main_loop():
    """
    Главный цикл сканера, который анализирует данные,
    полученные data_feeder'ом.
    """
    print("Scanner Engine loop started.")
    while True:
        try:
            await asyncio.sleep(5) # Анализируем данные каждые 5 секунд

            current_data_snapshot = dict(last_data)

            print("\n--- Running Scan Cycle ---")
            for symbol, data in current_data_snapshot.items():
                if not data or not data.get('bids') or not data.get('asks'):
                    continue

                # Ищем крупные заявки на покупку (биды)
                for order in data.get('bids', []):
                    # ПРОВЕРКА ПЕРЕД РАСПАКОВКОЙ
                    if not (isinstance(order, (list, tuple)) and len(order) >= 2):
                        continue # Пропускаем некорректно сформированную запись
                    
                    price, amount = order[0], order[1]
                    if price is None or amount is None: continue

                    order_value_usd = price * amount
                    if order_value_usd > LARGE_ORDER_USD:
                        print(f"✅ FOUND LARGE BID on {symbol}: {amount:.2f} @ ${price:.4f} (Value: ${order_value_usd:,.0f})")

                # Ищем крупные заявки на продажу (аски)
                for order in data.get('asks', []):
                    # ПРОВЕРКА ПЕРЕД РАСПАКОВКОЙ
                    if not (isinstance(order, (list, tuple)) and len(order) >= 2):
                        continue # Пропускаем некорректно сформированную запись

                    price, amount = order[0], order[1]
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
