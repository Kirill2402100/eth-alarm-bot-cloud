import gspread
import gspread.utils
from gspread.exceptions import APIError, GSpreadException
import logging
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional

log = logging.getLogger("bot")

TRADE_LOG_WS = None
TRADING_HEADERS_CACHE = None
PENDING_TRADES: List[List] = []
SAFE_CHAR = '⧗'

BUFFER_LOCK: asyncio.Lock | None = None

def get_lock() -> asyncio.Lock:
    """Creates and returns a singleton asyncio.Lock instance bound to the running event loop."""
    global BUFFER_LOCK
    if BUFFER_LOCK is None:
        BUFFER_LOCK = asyncio.Lock()
    return BUFFER_LOCK

def safe_id(text: str) -> str:
    return text.replace(":", SAFE_CHAR).replace("/", SAFE_CHAR)

def clear_headers_cache():
    """Clears the trading headers cache, forcing a reload on the next call to get_headers."""
    global TRADING_HEADERS_CACHE
    TRADING_HEADERS_CACHE = None
    log.info("Trading headers cache has been cleared. Will be re-fetched on next access.")

def get_headers(worksheet: gspread.Worksheet):
    global TRADING_HEADERS_CACHE
    if TRADING_HEADERS_CACHE is None:
        log.info(f"Reading headers from worksheet '{worksheet.title}' for the first time...")
        all_values = worksheet.get_all_values()
        TRADING_HEADERS_CACHE = all_values[0] if all_values else []
    return TRADING_HEADERS_CACHE

def _prepare_row(headers: list, data: dict) -> list:
    if 'Signal_ID' in data:
        data['Signal_ID'] = safe_id(data['Signal_ID'])
    # ИЗМЕНЕНО: Устанавливаем значения по умолчанию для всех возможных полей
    for key in headers:
        data.setdefault(key, '')
    return [data.get(h, '') for h in headers]

async def log_open_trade(trade_data: Dict):
    if not TRADE_LOG_WS: return
    try:
        headers = get_headers(TRADE_LOG_WS)
        if 'Signal_ID' not in headers:
            log.critical("Column 'Signal_ID' is missing from headers! Cannot log trade.")
            return
        row_to_insert = _prepare_row(headers, trade_data)
        async with get_lock():
            PENDING_TRADES.append(row_to_insert)
        log.info(f"Signal {safe_id(trade_data.get('Signal_ID'))} buffered for logging.")
    except Exception as e:
        log.error(f"Error buffering open trade: {e}", exc_info=True)

async def flush_log_buffers():
    loop = asyncio.get_running_loop()
    chunk_to_flush = []
    
    async with get_lock():
        if not PENDING_TRADES:
            return
        chunk_to_flush = PENDING_TRADES[:40]
        del PENDING_TRADES[:40]

    if not chunk_to_flush:
        return

    log.info(f"Attempting to flush {len(chunk_to_flush)} trade(s) to Google Sheets...")
    
    try:
        await loop.run_in_executor(
            None, 
            lambda: TRADE_LOG_WS.append_rows(chunk_to_flush, value_input_option='USER_ENTERED')
        )
        log.info(f"Successfully flushed chunk of {len(chunk_to_flush)} trades.")
    except Exception as e:
        log.error(f"Failed to flush chunk to Sheets, returning it to buffer: {e}")
        async with get_lock():
            PENDING_TRADES[:0] = chunk_to_flush

async def update_closed_trade(
    signal_id: str, status: str, exit_price: float, 
    pnl_usd: float, pnl_display: float, reason: str, 
    extra_fields: Optional[Dict] = None):
    if not TRADE_LOG_WS: return
    
    headers = get_headers(TRADE_LOG_WS)
    id_clean = safe_id(signal_id)

    if 'Signal_ID' not in headers:
        log.critical("Column 'Signal_ID' is missing from headers! Cannot update trade.")
        return

    async with get_lock():
        for row in PENDING_TRADES:
            try:
                if len(row) > headers.index('Signal_ID') and row[headers.index('Signal_ID')] == id_clean:
                    log.info(f"Updating trade {id_clean} directly in the memory buffer.")
                    row_dict = dict(zip(headers, row))
                    row_dict.update({
                        'Status': status, 'Exit_Price': exit_price, 'Exit_Reason': reason,
                        'PNL_USD': pnl_usd, 'PNL_Percent': pnl_display,
                        'Exit_Time_UTC': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    })
                    if extra_fields: row_dict.update(extra_fields)
                    new_row = [row_dict.get(h, '') for h in headers]
                    row[:] = new_row
                    return
            except IndexError:
                log.error(f"IndexError while updating buffer for {id_clean}. Row length: {len(row)}, Headers length: {len(headers)}")
                continue


    loop = asyncio.get_running_loop()
    max_retries = 3
    for attempt in range(max_retries):
        try:
            cell = await loop.run_in_executor(None, lambda: TRADE_LOG_WS.find(id_clean))
            
            row_idx = cell.row
            current_row_values = await loop.run_in_executor(None, lambda: TRADE_LOG_WS.row_values(row_idx))
            while len(current_row_values) < len(headers): current_row_values.append('')
            updated_row_dict = dict(zip(headers, current_row_values))
            base_update = {
                'Status': status, 'Exit_Price': exit_price, 'Exit_Reason': reason,
                'PNL_USD': pnl_usd, 'PNL_Percent': pnl_display,
                'Exit_Time_UTC': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            }
            updated_row_dict.update(base_update)
            if extra_fields: updated_row_dict.update(extra_fields)
            final_row_data = [updated_row_dict.get(h, '') for h in headers]
            end_a1 = gspread.utils.rowcol_to_a1(row_idx, len(headers))
            range_to_update = f"A{row_idx}:{end_a1}"
            await loop.run_in_executor(None, lambda: TRADE_LOG_WS.update(range_to_update, [final_row_data]))
            log.info(f"Successfully updated/closed trade {id_clean} in Sheets at row {row_idx}.")
            return

        except GSpreadException as e:
            if "Cell not found" in str(e):
                log.warning(f"Trade {id_clean} not found in Sheets. Appending as a new closed row.")
                final_data_dict = {'Signal_ID': id_clean, 'Status': status, 'Exit_Price': exit_price, 'Exit_Reason': reason, 'PNL_USD': pnl_usd, 'PNL_Percent': pnl_display, 'Exit_Time_UTC': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
                if extra_fields: final_data_dict.update(extra_fields)
                final_row_data = _prepare_row(headers, final_data_dict)
                await loop.run_in_executor(None, lambda: TRADE_LOG_WS.append_row(final_row_data, value_input_option='USER_ENTERED'))
                log.info(f"Successfully appended closed trade {id_clean} as a new row.")
                return 

            elif isinstance(e, APIError) and e.response.status_code == 429 and attempt < max_retries - 1:
                wait_time = 2 ** (attempt + 1)
                log.warning(f"Google API rate limit hit on update. Retrying in {wait_time}s... (Attempt {attempt + 1}/{max_retries})")
                await asyncio.sleep(wait_time)
            
            else:
                log.error(f"A non-retriable GSpreadException occurred for {signal_id}: {e}", exc_info=True)
                return
        except Exception as e:
            log.error(f"Critical error in update_closed_trade for {signal_id}: {e}", exc_info=True)
            return
