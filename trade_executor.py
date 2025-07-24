# trade_executor.py
import logging
from datetime import datetime, timezone

log = logging.getLogger("bot")
TRADE_LOG_WS = None

async def log_open_trade(trade_data):
    if not TRADE_LOG_WS: return
    try:
        headers = TRADE_LOG_WS.row_values(1)
        row_to_insert = [trade_data.get(header, '') for header in headers]
        TRADE_LOG_WS.append_row(row_to_insert, value_input_option='USER_ENTERED')
        log.info(f"Сигнал {trade_data.get('Signal_ID')} записан в Google Sheets.")
    except Exception as e:
        log.error(f"Ошибка при записи в Google Sheets: {e}", exc_info=True)

async def update_closed_trade(signal_id, status, exit_price, pnl_usd, pnl_percent):
    if not TRADE_LOG_WS: return
    try:
        cell = TRADE_LOG_WS.find(signal_id)
        if not cell:
            log.error(f"Не удалось найти сделку с ID {signal_id} для обновления.")
            return
        
        row = cell.row
        headers = TRADE_LOG_WS.row_values(1)
        
        # Обновляем ячейки по именам колонок
        updates = {
            "Status": status,
            "Exit_Time_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            "Exit_Price": exit_price,
            "PNL_USD": pnl_usd,
            "PNL_Percent": pnl_percent
        }
        
        for key, value in updates.items():
            if key in headers:
                col = headers.index(key) + 1
                TRADE_LOG_WS.update_cell(row, col, value)
        
        log.info(f"Сделка {signal_id} обновлена в Google Sheets со статусом {status}.")

    except Exception as e:
        log.error(f"Ошибка при обновлении сделки {signal_id} в Google Sheets: {e}", exc_info=True)
