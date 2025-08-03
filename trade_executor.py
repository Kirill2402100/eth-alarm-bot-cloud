# trade_executor.py

import logging
from datetime import datetime
import numpy as np
from typing import List, Dict

log = logging.getLogger("bot")

# --- Клиент для лога сделок ---
TRADE_LOG_WS = None

# --- Кеш для заголовков ---
TRADING_HEADERS_CACHE = None

# --- Буфер для пакетной записи ---
PENDING_TRADES: List[List] = []

def _get_cached_headers(ws, cache_key):
    """Читает заголовки из кеша или запрашивает их, если кеш пуст."""
    global TRADING_HEADERS_CACHE
    if TRADING_HEADERS_CACHE is None:
        log.info(f"Headers for 'trading' not cached. Fetching from Google Sheets...")
        TRADING_HEADERS_CACHE = ws.row_values(1)
    return TRADING_HEADERS_CACHE

def _make_serializable(value):
    """Приводит значение к типу, который поддерживается JSON."""
    if isinstance(value, np.generic): return value.item()
    if isinstance(value, datetime): return value.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, bool): return str(value).upper()
    return value

def _prepare_row(headers: list, data: dict) -> list:
    """Подготавливает строку данных для записи в Google Sheets."""
    return [_make_serializable(data.get(h, '')) for h in headers]

async def log_open_trade(trade_data: Dict):
    """ДОБАВЛЯЕТ сделку в буфер для последующей пакетной записи."""
    if not TRADE_LOG_WS: return
    try:
        headers = _get_cached_headers(TRADE_LOG_WS, 'trading')
        trade_data.update({
            "Exit_Price": "", "Exit_Time_UTC": "", "Exit_Reason": "",
            "PNL_USD": "", "PNL_Percent": ""
        })
        row_to_insert = _prepare_row(headers, trade_data)
        PENDING_TRADES.append(row_to_insert)
        log.info(f"Signal {trade_data.get('Signal_ID')} buffered for logging.")
    except Exception as e:
        log.error(f"Error buffering open trade: {e}", exc_info=True)

async def update_closed_trade(signal_id, status, exit_price, pnl_usd, pnl_percent, exit_reason):
    """Обновляет закрытую сделку ОДНИМ пакетным запросом."""
    if not TRADE_LOG_WS: return
    try:
        cell = TRADE_LOG_WS.find(signal_id)
        if not cell:
            log.error(f"Could not find trade {signal_id} to update.")
            return

        row_index = cell.row
        headers = _get_cached_headers(TRADE_LOG_WS, 'trading')
        
        updates_data = {
            "Status": status, "Exit_Time_UTC": datetime.now(), "Exit_Price": exit_price,
            "PNL_USD": f"{pnl_usd:.2f}", "PNL_Percent": f"{pnl_percent:.2f}%", "Exit_Reason": exit_reason
        }
        
        batch_updates = []
        for key, value in updates_data.items():
            if key in headers:
                col_index = headers.index(key) + 1
                sanitized_value = _make_serializable(value)
                batch_updates.append({
                    'range': f"{chr(ord('A') + col_index - 1)}{row_index}",
                    'values': [[sanitized_value]],
                })
        
        if batch_updates:
            TRADE_LOG_WS.batch_update(batch_updates)
            log.info(f"Trade {signal_id} updated in Trading_Log with status {status}.")

    except Exception as e:
        log.error(f"Error updating closed trade {signal_id}: {e}", exc_info=True)

async def log_diagnostic_entry(data: Dict):
    """Функция отключена."""
    pass

async def flush_log_buffers():
    """Отправляет накопленные логи сделок в Google Sheets."""
    global PENDING_TRADES
    if PENDING_TRADES:
        try:
            log.info(f"Flushing {len(PENDING_TRADES)} trade(s) to Google Sheets...")
            TRADE_LOG_WS.append_rows(PENDING_TRADES, value_input_option='USER_ENTERED')
            PENDING_TRADES = []
        except Exception as e:
            log.error(f"Error flushing trade logs: {e}", exc_info=True)
