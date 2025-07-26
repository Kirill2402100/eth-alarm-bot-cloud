# trade_executor.py
import logging
from datetime import datetime, timezone

log = logging.getLogger("bot")
TRADE_LOG_WS = None
# <<< Убрана переменная ANALYSIS_LOG_WS >>>

async def log_open_trade(trade_data):
    if not TRADE_LOG_WS: return
    try:
        headers = TRADE_LOG_WS.row_values(1)
        if 'StochRSI_at_Entry' in trade_data:
            trade_data['StochRSI_at_Entry'] = f"{trade_data['StochRSI_at_Entry']:.2f}"
        
        row_to_insert = [trade_data.get(header, '') for header in headers]
        TRADE_LOG_WS.append_row(row_to_insert, value_input_option='USER_ENTERED')
        log.info(f"Сигнал {trade_data.get('Signal_ID')} записан в Google Sheets.")
    except Exception as e:
        log.error(f"Ошибка при записи в Google Sheets: {e}", exc_info=True)

# <<< Добавлен аргумент atr_at_exit >>>
async def update_closed_trade(signal_id, status, exit_price, pnl_usd, pnl_percent, exit_detail=None, atr_at_exit=None):
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
        if exit_detail:
            updates["Exit_Detail"] = exit_detail
        # <<< Добавляем ATR в словарь для обновления >>>
        if atr_at_exit:
            updates["ATR_at_Exit"] = f"{atr_at_exit:.4f}"
        
        for key, value in updates.items():
            if key in headers:
                col = headers.index(key) + 1
                TRADE_LOG_WS.update_cell(row, col, str(value).replace('.', ','))
        
        log.info(f"Сделка {signal_id} обновлена в Google Sheets со статусом {status}.")

    except Exception as e:
        log.error(f"Ошибка при обновлении сделки {signal_id} в Google Sheets: {e}", exc_info=True)

# <<< УБРАНА ФУНКЦИЯ log_analysis_data >>>
