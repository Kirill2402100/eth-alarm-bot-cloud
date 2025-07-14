# File: trade_executor.py

import uuid
from datetime import datetime, timezone

async def log_trade_to_sheet(worksheet, decision: dict):
    """
    Форматирует решение от LLM и записывает его как сделку в Google-таблицу.
    """
    if not worksheet:
        print("Trade Executor: Worksheet not available. Skipping log.")
        return

    try:
        signal_id = str(uuid.uuid4())[:8]
        timestamp = datetime.now(timezone.utc).isoformat()
        
        # Собираем строку для записи в том же порядке, что и HEADERS
        row_data = [
            signal_id,
            timestamp,
            decision.get("pair"),
            decision.get("confidence_score"),
            decision.get("algorithm_type"),
            decision.get("strategy_idea"),
            decision.get("reason"),
            decision.get("entry_price"),
            decision.get("sl_price"),
            decision.get("tp_price"),
        ]
        
        # Асинхронно добавляем строку в таблицу
        await asyncio.to_thread(worksheet.append_row, row_data, value_input_option='USER_ENTERED')
        print(f"✅ Trade logged to Google Sheets for pair {decision.get('pair')}")
        return True # Возвращаем успех

    except Exception as e:
        print(f"❌ Error logging trade to Google Sheets: {e}")
        return False
