# trade_executor.py
import logging
from datetime import datetime, timezone

log = logging.getLogger("bot")
TRADE_LOG_WS = None
ANALYSIS_LOG_WS = None # <<< Новая переменная для листа аналитики

async def log_open_trade(trade_data):
    if not TRADE_LOG_WS: return
    try:
        headers = TRADE_LOG_WS.row_values(1)
        # Убедимся, что StochRSI правильно форматируется для таблицы
        if 'StochRSI_at_Entry' in trade_data:
            trade_data['StochRSI_at_Entry'] = f"{trade_data['StochRSI_at_Entry']:.2f}"
        
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
        
        updates = {
            "Status": status,
            "Exit_Time_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            "Exit_Price": f"{exit_price:.4f}",
            "PNL_USD": f"{pnl_usd:.2f}",
            "PNL_Percent": f"{pnl_percent:.2f}%"
        }
        
        for key, value in updates.items():
            if key in headers:
                col = headers.index(key) + 1
                TRADE_LOG_WS.update_cell(row, col, str(value).replace('.', ',')) # Заменяем точки на запятые для Google Sheets
        
        log.info(f"Сделка {signal_id} обновлена в Google Sheets со статусом {status}.")

    except Exception as e:
        log.error(f"Ошибка при обновлении сделки {signal_id} в Google Sheets: {e}", exc_info=True)

# <<< НОВАЯ ФУНКЦИЯ ДЛЯ ЛОГИРОВАНИЯ АНАЛИТИКИ >>>
async def log_analysis_data(analysis_data):
    if not ANALYSIS_LOG_WS: return
    try:
        headers = ANALYSIS_LOG_WS.row_values(1)
        row_to_insert = [analysis_data.get(header, '') for header in headers]
        ANALYSIS_LOG_WS.append_row(row_to_insert, value_input_option='USER_ENTERED')
    except Exception as e:
        # Не будем спамить в основной лог, если аналитика не записалась
        pass
