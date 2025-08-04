# trade_executor.py

import gspread
import gspread.utils
import logging
from datetime import datetime, timezone
from typing import Dict, List

log = logging.getLogger("bot")

TRADE_LOG_WS = None
TRADING_HEADERS_CACHE = None
PENDING_TRADES: List[List] = []
SAFE_CHAR = '⧗'

# ===========================================================================
# HELPER FUNCTIONS
# ===========================================================================

def safe_id(text: str) -> str:
    """Заменяет небезопасные символы (':', '/') в ID для Google Sheets."""
    return text.replace(":", SAFE_CHAR).replace("/", SAFE_CHAR)

def get_headers(worksheet: gspread.Worksheet):
    """Получает и кэширует заголовки листа."""
    global TRADING_HEADERS_CACHE
    if TRADING_HEADERS_CACHE is None:
        log.info(f"Reading headers from worksheet '{worksheet.title}' for the first time...")
        TRADING_HEADERS_CACHE = worksheet.row_values(1)
    return TRADING_HEADERS_CACHE

def _prepare_row(headers: list, data: dict) -> list:
    """Подготавливает строку данных, используя безопасный ID."""
    if 'Signal_ID' in data:
        data['Signal_ID'] = safe_id(data['Signal_ID'])
    return [data.get(h, '') for h in headers]

# ===========================================================================
# LOGGING FUNCTIONS
# ===========================================================================

async def log_open_trade(trade_data: Dict):
    if not TRADE_LOG_WS: return
    try:
        headers = get_headers(TRADE_LOG_WS)
        row_to_insert = _prepare_row(headers, trade_data)
        PENDING_TRADES.append(row_to_insert)
        log.info(f"Signal {safe_id(trade_data.get('Signal_ID'))} buffered for logging.")
    except Exception as e:
        log.error(f"Error buffering open trade: {e}", exc_info=True)

async def flush_log_buffers():
    global PENDING_TRADES
    if PENDING_TRADES:
        try:
            log.info(f"Flushing {len(PENDING_TRADES)} trade(s) to Google Sheets...")
            for i in range(0, len(PENDING_TRADES), 40):
                chunk = PENDING_TRADES[i:i + 40]
                TRADE_LOG_WS.append_rows(chunk, value_input_option='USER_ENTERED')
                log.info(f"Flushed chunk of {len(chunk)} trades.")
            PENDING_TRADES = []
        except Exception as e:
            log.error(f"Error flushing trade logs: {e}", exc_info=True)

async def update_closed_trade(signal_id: str, status: str, exit_price: float, pnl_usd: float, pnl_display: float, reason: str):
    if not TRADE_LOG_WS: return
    try:
        headers = get_headers(TRADE_LOG_WS)
        id_clean = safe_id(signal_id)
        
        try:
            cell = TRADE_LOG_WS.find(id_clean)
        except gspread.CellNotFound:
            log.warning(f"Trade {id_clean} not found. Appending as a new closed row.")
            placeholder_row = [''] * len(headers)
            placeholder_row[headers.index('Signal_ID')] = id_clean
            TRADE_LOG_WS.append_row(placeholder_row, value_input_option='USER_ENTERED')
            cell = TRADE_LOG_WS.find(id_clean)
        
        row_idx = cell.row
        
        current_row_values = TRADE_LOG_WS.row_values(row_idx)
        updated_row_dict = dict(zip(headers, current_row_values))

        # ИЗМЕНЕНО: Ключи словаря теперь точно соответствуют заголовкам в таблице
        updated_row_dict.update({
            'Status': status, 
            'Exit_Price': exit_price, 
            'Exit_Reason': reason,          # <-- Исправлено
            'PNL_USD': pnl_usd,             # <-- Исправлено
            'PNL_Percent': pnl_display,     # <-- Исправлено
            'Exit_Time_UTC': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        })

        final_row_data = [updated_row_dict.get(h, '') for h in headers]
        
        last_col_letter = gspread.utils.rowcol_to_a1(1, len(headers)).rstrip('1')
        range_to_update = f"A{row_idx}:{last_col_letter}{row_idx}"
        
        TRADE_LOG_WS.update(range_to_update, [final_row_data])
        
        log.info(f"Successfully updated/closed trade {id_clean} at row {row_idx}.")
    except Exception as e:
        log.error(f"Critical error in update_closed_trade for {signal_id}: {e}", exc_info=True)
