import gspread
import gspread.utils
from gspread.exceptions import APIError, GSpreadException
import logging
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional

# ИЗМЕНЕНО: отдельный логгер под модуль
log = logging.getLogger("trade_executor")

TRADE_LOG_WS = None
TRADING_HEADERS_CACHE = None
PENDING_TRADES: List[List] = []
SAFE_CHAR = '⧗'

BUFFER_LOCK: asyncio.Lock | None = None

# --- Хедеры листа для BMR-DCA ---
BMR_HEADERS = [
    "Event_ID","Signal_ID","Timestamp_UTC","Pair","Side","Event",
    "Step_No","Step_Margin_USDT","Cum_Margin_USDT","Leverage",
    "Entry_Price","Avg_Price","TP_Pct","TP_Price","SL_Price","Liq_Est_Price",
    "Next_DCA_ATR_mult","Next_DCA_Price",
    "ATR_5m","ATR_1h","RSI_5m","ADX_5m","Supertrend","Vol_z",
    "Range_Lower","Range_Upper","Range_Width",
    "Fee_Rate_Maker","Fee_Rate_Taker","Fee_Est_USDT",
    "PNL_Realized_USDT","PNL_Realized_Pct","Time_In_Trade_min"
]

# --- УТИЛИТЫ/СЕРВИС ---

def get_lock() -> asyncio.Lock:
    global BUFFER_LOCK
    if BUFFER_LOCK is None:
        BUFFER_LOCK = asyncio.Lock()
    return BUFFER_LOCK

def safe_id(text: str) -> str:
    return text.replace(":", SAFE_CHAR).replace("/", SAFE_CHAR)

def clear_headers_cache():
    global TRADING_HEADERS_CACHE
    TRADING_HEADERS_CACHE = None
    log.info("Trading headers cache cleared; will be re-fetched on next access.")

def get_headers(worksheet: gspread.Worksheet):
    global TRADING_HEADERS_CACHE
    if TRADING_HEADERS_CACHE is None:
        log.info(f"Reading headers from worksheet '{worksheet.title}'...")
        try:
            all_values = worksheet.get_all_values()
            TRADING_HEADERS_CACHE = all_values[0] if all_values else []
        except gspread.exceptions.GSpreadException as e:
            log.error(f"Failed to get worksheet values: {e}")
            TRADING_HEADERS_CACHE = []
    return TRADING_HEADERS_CACHE

async def _write_headers(ws: gspread.Worksheet, headers: list):
    """Пишем заголовки в строку 1 и обновляем кэш."""
    loop = asyncio.get_running_loop()
    end_a1 = gspread.utils.rowcol_to_a1(1, len(headers))
    await loop.run_in_executor(None, lambda: ws.update(f"A1:{end_a1}", [headers]))
    # Синхронизируем кэш
    global TRADING_HEADERS_CACHE, TRADE_LOG_WS
    TRADE_LOG_WS = ws
    TRADING_HEADERS_CACHE = headers[:]
    log.info(f"Header row set for '{ws.title}': {len(headers)} columns.")

# НОВОЕ: гарантируем отдельный лист под BMR и нужные хедеры
async def ensure_bmr_log_sheet(gfile: gspread.Spreadsheet, title: str = "BMR_DCA_Log"):
    """Создаёт (или переиспользует) лист с BMR_HEADERS и настраивает TRADE_LOG_WS."""
    loop = asyncio.get_running_loop()
    ws = None
    try:
        ws = await loop.run_in_executor(None, lambda: gfile.worksheet(title))
        clear_headers_cache() # Сбрасываем кэш, чтобы перечитать актуальные заголовки
        headers = get_headers(ws)
        if headers != BMR_HEADERS:
            log.warning(f"Headers mismatch in '{title}'. Archiving and creating a new sheet.")
            archive_title = f"{title}_archive_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
            await loop.run_in_executor(None, lambda: ws.update_title(archive_title))
            ws = await loop.run_in_executor(None, lambda: gfile.add_worksheet(title=title, rows=1000, cols=len(BMR_HEADERS)))
            await _write_headers(ws, BMR_HEADERS)
        else:
            global TRADE_LOG_WS
            TRADE_LOG_WS = ws
            log.info(f"Successfully attached to existing BMR sheet: {title}")

    except gspread.exceptions.WorksheetNotFound:
        log.info(f"Worksheet '{title}' not found. Creating a new one.")
        ws = await loop.run_in_executor(None, lambda: gfile.add_worksheet(title=title, rows=1000, cols=len(BMR_HEADERS)))
        await _write_headers(ws, BMR_HEADERS)

    clear_headers_cache()
    _ = get_headers(ws) # Заполняем кэш
    log.info(f"BMR sheet is ready: {title}")

def _prepare_row(headers: list, data: dict) -> list:
    if 'Signal_ID' in data:
        data['Signal_ID'] = safe_id(data.get('Signal_ID', ''))
    # Убедимся, что все значения - строки или числа, а не None
    return [data.get(h, '') for h in headers]

# --- ЛОГИРОВАНИЕ СОБЫТИЙ BMR ---

async def bmr_log_event(data: dict):
    """Append event row for BMR-DCA sheet. Самопочинка заголовков при необходимости."""
    if not TRADE_LOG_WS:
        log.warning("bmr_log_event called but TRADE_LOG_WS is not set.")
        return
    try:
        headers = get_headers(TRADE_LOG_WS)
        # Если лист пустой/не тот — ставим правильные заголовки
        if not headers or headers != BMR_HEADERS:
            log.warning("Headers in the sheet are missing or incorrect. Attempting to fix.")
            await _write_headers(TRADE_LOG_WS, BMR_HEADERS)
            headers = BMR_HEADERS

        row = _prepare_row(headers, data)
        async with get_lock():
            PENDING_TRADES.append(row)
        log.info(f"[BMR] Buffered event {data.get('Event')} for {safe_id(data.get('Signal_ID',''))}")
    except Exception as e:
        log.error(f"[BMR] Error buffering event: {e}", exc_info=True)

# --- СТАРЫЕ ФУНКЦИИ (совместимость) ---

async def log_open_trade(trade_data: Dict):
    if not TRADE_LOG_WS: return
    try:
        headers = get_headers(TRADE_LOG_WS)
        row_to_insert = _prepare_row(headers, trade_data)
        async with get_lock():
            PENDING_TRADES.append(row_to_insert)
        log.info(f"Signal {safe_id(trade_data.get('Signal_ID',''))} buffered.")
    except Exception as e:
        log.error(f"Error buffering open trade: {e}", exc_info=True)

async def flush_log_buffers():
    loop = asyncio.get_running_loop()
    while True:
        chunk_to_flush = []
        async with get_lock():
            if not PENDING_TRADES:
                return
            chunk_to_flush = PENDING_TRADES[:40]
            del PENDING_TRADES[:40]

        if not chunk_to_flush:
            return

        log.info(f"Flushing {len(chunk_to_flush)} row(s) to Google Sheets...")
        try:
            await loop.run_in_executor(
                None,
                lambda: TRADE_LOG_WS.append_rows(chunk_to_flush, value_input_option='USER_ENTERED')
            )
            log.info("Flush OK.")
        except Exception as e:
            log.error(f"Flush failed, returning chunk to buffer: {e}")
            async with get_lock():
                PENDING_TRADES[:0] = chunk_to_flush
            return  # выходим, чтобы не зациклиться на ошибке

async def update_closed_trade(
    signal_id: str, status: str, exit_price: float,
    pnl_usd: float, pnl_display: float, reason: str,
    extra_fields: Optional[Dict] = None
):
    """Апдейт для старых и новых схем листа."""
    if not TRADE_LOG_WS: return

    headers = get_headers(TRADE_LOG_WS)
    id_clean = safe_id(signal_id)

    if 'Signal_ID' not in headers:
        log.critical("Column 'Signal_ID' is missing; cannot update trade.")
        return

    # 1) Сначала пытаемся обновить буфер
    async with get_lock():
        try:
            idx = headers.index('Signal_ID')
            for row in PENDING_TRADES:
                if len(row) > idx and row[idx] == id_clean:
                    log.info(f"Updating buffered row for {id_clean}")
                    row_dict = dict(zip(headers, row))

                    # Маппинг под оба формата
                    if 'PNL_Realized_USDT' in headers:
                        row_dict.update({'Status': status, 'Exit_Price': exit_price, 'Exit_Reason': reason, 'PNL_Realized_USDT': pnl_usd, 'PNL_Realized_Pct': pnl_display, 'Timestamp_UTC': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
                    else:
                        row_dict.update({'Status': status, 'Exit_Price': exit_price, 'Exit_Reason': reason, 'PNL_USD': pnl_usd, 'PNL_Percent': pnl_display, 'Exit_Time_UTC': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
                    
                    if extra_fields: row_dict.update(extra_fields)
                    
                    new_row = _prepare_row(headers, row_dict)
                    row[:] = new_row
                    return
        except ValueError:
            log.error("Could not find 'Signal_ID' in headers during buffer update.")


    # 2) Если строки уже нет в буфере — ищем и обновляем в листе
    loop = asyncio.get_running_loop()
    max_retries = 3
    for attempt in range(max_retries):
        try:
            cell = await loop.run_in_executor(None, lambda: TRADE_LOG_WS.find(id_clean))
            row_idx = cell.row
            current_row_values = await loop.run_in_executor(None, lambda: TRADE_LOG_WS.row_values(row_idx))
            while len(current_row_values) < len(headers):
                current_row_values.append('')

            updated = dict(zip(headers, current_row_values))
            if 'PNL_Realized_USDT' in headers:
                base = {'Status': status, 'Exit_Price': exit_price, 'Exit_Reason': reason, 'PNL_Realized_USDT': pnl_usd, 'PNL_Realized_Pct': pnl_display, 'Timestamp_UTC': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
            else:
                base = {'Status': status, 'Exit_Price': exit_price, 'Exit_Reason': reason, 'PNL_USD': pnl_usd, 'PNL_Percent': pnl_display, 'Exit_Time_UTC': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
            
            updated.update(base)
            if extra_fields: updated.update(extra_fields)

            final_row = _prepare_row(headers, updated)
            end_a1 = gspread.utils.rowcol_to_a1(row_idx, len(headers))
            await loop.run_in_executor(None, lambda: TRADE_LOG_WS.update(f"A{row_idx}:{end_a1}", [final_row]))
            log.info(f"Closed trade {id_clean} updated at row {row_idx}.")
            return

        except GSpreadException as e:
            if "Cell not found" in str(e):
                log.warning(f"{id_clean} not found; appending as new row.")
                base = {'Signal_ID': id_clean, 'Status': status, 'Exit_Price': exit_price, 'Exit_Reason': reason}
                if 'PNL_Realized_USDT' in headers:
                    base.update({'PNL_Realized_USDT': pnl_usd, 'PNL_Realized_Pct': pnl_display, 'Timestamp_UTC': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
                else:
                    base.update({'PNL_USD': pnl_usd, 'PNL_Percent': pnl_display, 'Exit_Time_UTC': datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})

                if extra_fields: base.update(extra_fields)
                final_row = _prepare_row(headers, base)
                await loop.run_in_executor(None, lambda: TRADE_LOG_WS.append_row(final_row, value_input_option='USER_ENTERED'))
                log.info(f"Closed trade {id_clean} appended.")
                return

            if isinstance(e, APIError) and getattr(e, "response", None) and e.response.status_code == 429 and attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                log.warning(f"Sheets rate limit. Retry in {wait}s (attempt {attempt+1}/{max_retries})")
                await asyncio.sleep(wait)
                continue

            log.error(f"GSpreadException on update {id_clean}: {e}", exc_info=True)
            return
        except Exception as e:
            log.error(f"Critical error in update_closed_trade for {id_clean}: {e}", exc_info=True)
            return
