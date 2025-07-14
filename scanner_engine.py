# File: scanner_engine.py

import asyncio
# Импортируем общие переменные из data_feeder
from data_feeder import is_running, last_data

# --- КОНФИГУРАЦИЯ СКАНЕРА ---
# Определяем "плиту" как заявку объемом более $50,000
LARGE_ORDER_USD = 50000 

async def scanner_main_loop():
    """
    Главный цикл сканера, который анализирует данные,
    полученные data_feeder'ом.
    """
    print("Scanner Engine loop started.")
    while is_running:
        await asyncio.sleep(5) # Анализируем данные каждые 5 секунд

        # Копируем данные, чтобы избежать проблем с одновременным доступом
        current_data_snapshot = dict(last_data)

        print("\n--- Running Scan Cycle ---")
        for symbol, data in current_data_snapshot.items():
            if not data or 'bids' not in data or 'asks' not in data:
                continue

            # Ищем крупные заявки на покупку (биды)
            for price, amount in data.get('bids', []):
                order_value_usd = price * amount
                if order_value_usd > LARGE_ORDER_USD:
                    print(f"✅ FOUND LARGE BID on {symbol}: {amount:.2f} @ ${price:.4f} (Value: ${order_value_usd:,.0f})")

            # Ищем крупные заявки на продажу (аски)
            for price, amount in data.get('asks', []):
                order_value_usd = price * amount
                if order_value_usd > LARGE_ORDER_USD:
                    print(f"🛑 FOUND LARGE ASK on {symbol}: {amount:.2f} @ ${price:.4f} (Value: ${order_value_usd:,.0f})")
