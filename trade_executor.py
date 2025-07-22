# trade_executor.py
# ============================================================================
# v39.0 - НОВАЯ СТРАТЕГИЯ: ОБНОВЛЕНИЕ row_data И СТОЛБЦОВ
# - Убраны старые индикаторы, добавлены RSI, Stoch_K, Stoch_D
# - Добавлен Intermediate_Triggered для аналитики
# - Обновлены буквы столбцов в update_trade_in_sheet
# ============================================================================
import asyncio
from datetime import datetime, timezone
import gspread
import logging

log = logging.getLogger("bot")

TRADE_LOG_WS = None
DEBUG_LOG_WS = None

async def log_trade_to_sheet(decision: dict):
    if not TRADE_LOG_WS: return False
    try:
        row_data = [
            decision.get("Signal_ID"), decision.get("Timestamp_UTC"), decision.get("Pair"),
            decision.get("Algorithm_Type"), decision.get("Strategy_Idea"),
            decision.get("Entry_Price"), decision.get("SL_Price"), decision.get("TP_Price"),
            decision.get("side"), decision.get("Deposit"), decision.get("Leverage"),
            decision.get("RSI"), decision.get("Stoch_K"), decision.get("Stoch_D"),
            "OPEN", None, None, None, None, None, None, "No"
        ]
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: TRADE_LOG_WS.append_row(row_data, value_input_option='USER_ENTERED'))
        return True
    except Exception as e:
        log.error(f"Google Sheets log error: {e}")
        return False

async def update_trade_in_sheet(signal: dict, status: str, exit_price: float, pnl_usd: float, pnl_percent: float, reason: str = None):
    if not TRADE_LOG_WS: return False
    try:
        signal_id = signal.get("Signal_ID")
        if not signal_id: return False
        loop = asyncio.get_event_loop()
        cell = await loop.run_in_executor(None, lambda: TRADE_LOG_WS.find(signal_id))
        if not cell: return False
        exit_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        
        entry_time = datetime.strptime(signal.get("Timestamp_UTC"), '%Y-%m-%d %H:%M:%S')
        time_in_trade = round((datetime.strptime(exit_time, '%Y-%m-%d %H:%M:%S') - entry_time).total_seconds() / 60, 2)
        
        intermediate_triggered = "Yes" if signal.get("intermediate_triggered", False) else "No"
        
        update_tasks = [
            {'range': f'O{cell.row}', 'values': [[status]]},
            {'range': f'P{cell.row}', 'values': [[exit_time]]},
            {'range': f'Q{cell.row}', 'values': [[exit_price]]},
            {'range': f'R{cell.row}', 'values': [[pnl_usd]]},
            {'range': f'S{cell.row}', 'values': [[pnl_percent]]},
            {'range': f'T{cell.row}', 'values': [[reason]]},
            {'range': f'U{cell.row}', 'values': [[time_in_trade]]},
            {'range': f'V{cell.row}', 'values': [[intermediate_triggered]]}
        ]
            
        await loop.run_in_executor(None, lambda: TRADE_LOG_WS.batch_update(update_tasks))
        return True
    except gspread.exceptions.CellNotFound:
        log.warning(f"Signal_ID {signal_id} not found in Google Sheet.")
        return False
    except Exception as e:
        log.error(f"Google Sheets update error: {e}")
        return False

async def log_debug_data(data: dict):
    if not DEBUG_LOG_WS: return False
    try:
        row_data = [
            data.get("Timestamp_UTC"), data.get("RSI"), data.get("Stoch_K"), data.get("Stoch_D"),
            data.get("Side"), data.get("Reason_Prop")
        ]
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: DEBUG_LOG_WS.append_row(row_data, value_input_option='USER_ENTERED'))
        return True
    except Exception as e:
        log.error(f"Google Sheets debug log error: {e}")
        return False
