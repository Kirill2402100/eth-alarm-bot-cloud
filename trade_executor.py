# trade_executor.py

import logging
from datetime import datetime
import numpy as np

log = logging.getLogger("bot")

TRADE_LOG_WS = None
DIAGNOSTIC_LOG_WS = None

# --- Кеш для заголовков ---
TRADING_HEADERS_CACHE = None
DIAGNOSTIC_HEADERS_CACHE = None

# ===========================================================================
# HELPER FUNCTIONS
# ===========================================================================

def _get_cached_headers(ws, cache_key):
    """Читает заголовки из кеша или запрашивает их, если кеш пуст."""
    global TRADING_HEADERS_CACHE, DIAGNOSTIC_HEADERS_CACHE
    
    cache = TRADING_HEADERS_CACHE if cache_key == 'trading' else DIAGNOSTIC_HEADERS_CACHE
    
    if cache is None:
        log.info(f"Headers for '{cache_key}' not cached. Fetching from Google Sheets...")
        cache = ws.row_values(1)
        if cache_key == 'trading':
            TRADING_HEADERS_CACHE = cache
        else:
            DIAGNOSTIC_HEADERS_CACHE = cache
    return cache

def _make_serializable(value):
    """Приводит значение к типу, который поддерживается JSON."""
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (datetime,)):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, bool):
        return str(value).upper()
    return value

def _prepare_row(headers: list, data: dict) -> list:
    """Подготавливает строку данных для записи в Google Sheets."""
    return [_make_serializable(data.get(h, '')) for h in headers]

# ===========================================================================
# LOGGING FUNCTIONS
# ===========================================================================

async def log_open_trade(trade_data):
    if not TRADE_LOG_WS: return
    try:
        headers = _get_cached_headers(TRADE_LOG_WS, 'trading') # Используем кеш
        trade_data.update({
            "Exit_Price": "", "Exit_Time_UTC": "", "Exit_Reason": "",
            "PNL_USD": "", "PNL_Percent": ""
        })
        row_to_insert = _prepare_row(headers, trade_data)
        TRADE_LOG_WS.append_row(row_to_insert)
        log.info(f"Signal {trade_data.get('Signal_ID')} logged to Trading_Log.")
    except Exception as e:
        log.error(f"Error logging open trade: {e}", exc_info=True)

async def update_closed_trade(signal_id, status, exit_price, pnl_usd, pnl_percent, exit_reason):
    if not TRADE_LOG_WS: return
    try:
        cell = TRADE_LOG_WS.find(signal_id)
        if not cell:
            log.error(f"Could not find trade {signal_id} to update.")
            return
            
        row_index = cell.row
        headers = _get_cached_headers(TRADE_LOG_WS, 'trading') # Используем кеш
        
        updates = {
            "Status": status, "Exit_Time_UTC": datetime.now(), "Exit_Price": exit_price,
            "PNL_USD": f"{pnl_usd:.2f}", "PNL_Percent": f"{pnl_percent:.2f}%", "Exit_Reason": exit_reason
        }
        
        for key, value in updates.items():
            if key in headers:
                col_index = headers.index(key) + 1
                sanitized_value = _make_serializable(value)
                TRADE_LOG_WS.update_cell(row_index, col_index, sanitized_value)
        
        log.info(f"Trade {signal_id} updated in Trading_Log with status {status}.")
    except Exception as e:
        log.error(f"Error updating closed trade {signal_id}: {e}", exc_info=True)

async def log_diagnostic_entry(data):
    if not DIAGNOSTIC_LOG_WS: return
    try:
        headers = _get_cached_headers(DIAGNOSTIC_LOG_WS, 'diagnostic') # Используем кеш
        row_to_insert = _prepare_row(headers, data)
        DIAGNOSTIC_LOG_WS.append_row(row_to_insert)
        log.info(f"Diagnostic entry for {data.get('Pair')} logged.")
    except Exception as e:
        log.error(f"Error logging diagnostic entry: {e}", exc_info=True)
