# trade_executor.py
import logging
from datetime import datetime, timezone

log = logging.getLogger("bot")
TRADE_LOG_WS = None

async def log_trade_to_sheet(trade_data):
    """Записывает информацию о сигнале в Google Sheet."""
    if not TRADE_LOG_WS:
        log.warning("Google Sheets (TRADE_LOG_WS) не инициализирован. Пропуск логирования.")
        return

    try:
        # Определяем порядок колонок в соответствии с вашими старыми заголовками
        headers = [
            "Signal_ID", "Timestamp_UTC", "Pair", "Algorithm_Type", "Strategy_Idea",
            "Entry_Price", "SL_Price", "TP_Price", "side", "Probability", "Status"
        ]
        
        # Собираем данные для записи в правильном порядке
        row_to_insert = []
        for header in headers:
            row_to_insert.append(trade_data.get(header, '')) # Используем get для безопасности

        TRADE_LOG_WS.append_row(row_to_insert, value_input_option='USER_ENTERED')
        log.info(f"Сигнал {trade_data['Signal_ID']} записан в Google Sheets.")

    except Exception as e:
        log.error(f"Ошибка при записи в Google Sheets: {e}", exc_info=True)
