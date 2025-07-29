import logging
from datetime import datetime, timezone

log = logging.getLogger("bot")

# Глобальная переменная, которая будет установлена из main.py
TRADE_LOG_WS = None

async def log_open_trade(trade_data):
    """Логирует открытие новой сделки."""
    if not TRADE_LOG_WS: return
    try:
        headers = TRADE_LOG_WS.row_values(1)
        trade_data['TSL_History'] = ''
        row_to_insert = [trade_data.get(header, '') for header in headers]
        TRADE_LOG_WS.append_row(row_to_insert)
        log.info(f"Signal {trade_data.get('Signal_ID')} logged to Google Sheets.")
    except Exception as e:
        log.error(f"Error logging open trade: {e}", exc_info=True)


async def log_tsl_update(signal_id, new_stop_price):
    """Добавляет информацию о перемещении трейлинг-стопа в ячейку истории."""
    if not TRADE_LOG_WS: return
    try:
        cell = TRADE_LOG_WS.find(signal_id)
        if not cell: return

        row_index = cell.row
        tsl_col_index = TRADE_LOG_WS.row_values(1).index("TSL_History") + 1
        
        current_history = TRADE_LOG_WS.cell(row_index, tsl_col_index).value or ""
        timestamp = datetime.now(timezone.utc).strftime('%H:%M:%S')
        new_entry = f"{new_stop_price:.4f}@{timestamp}"
        
        updated_history = f"{current_history}, {new_entry}".lstrip(", ")
        
        TRADE_LOG_WS.update_cell(row_index, tsl_col_index, updated_history)
        log.info(f"TSL for {signal_id} updated to {new_stop_price:.4f}")

    except Exception as e:
        log.error(f"Error logging TSL update: {e}", exc_info=True)


async def update_closed_trade(signal_id, status, exit_price, pnl_usd, pnl_percent, exit_reason):
    """Обновляет информацию о закрытой сделке."""
    if not TRADE_LOG_WS: return
    try:
        cell = TRADE_LOG_WS.find(signal_id)
        if not cell:
            log.error(f"Could not find trade {signal_id} to update.")
            return
            
        row_index = cell.row
        headers = TRADE_LOG_WS.row_values(1)
        
        updates = {
            "Status": status,
            "Exit_Time_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            "Exit_Price": f"{exit_price:.4f}",
            "PNL_USD": f"{pnl_usd:.2f}",
            "PNL_Percent": f"{pnl_percent:.2f}%",
            "Exit_Reason": exit_reason
        }
        
        for key, value in updates.items():
            if key in headers:
                col_index = headers.index(key) + 1
                TRADE_LOG_WS.update_cell(row_index, col_index, value)
        
        log.info(f"Trade {signal_id} updated in Google Sheets with status {status}.")

    except Exception as e:
        log.error(f"Error updating closed trade {signal_id}: {e}", exc_info=True)
