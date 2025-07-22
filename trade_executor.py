# trade_executor.py
# ============================================================================
# v37.6 - ФИКС ATR ЛОГИ
# ============================================================================
import asyncio
from datetime import datetime, timezone
import gspread

TRADE_LOG_WS = None

async def log_trade_to_sheet(decision: dict):
    if not TRADE_LOG_WS: return False
    try:
        row_data = [
            decision.get("Signal_ID"), decision.get("Timestamp_UTC"), decision.get("Pair"),
            decision.get("Algorithm_Type"), decision.get("Strategy_Idea"),
            decision.get("Entry_Price"), decision.get("SL_Price"), decision.get("TP_Price"),
            decision.get("side"), decision.get("Deposit"), decision.get("Leverage"),
            decision.get("ADX"), decision.get("PDI"), decision.get("MDI"),
            decision.get("Imbalance_Ratio"), decision.get("Aggression_Side"),
            "OPEN", None, None, None, None, decision.get("Trigger_Order_USD"),
            None, None, decision.get("ATR")  # ATR в конце
        ]
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: TRADE_LOG_WS.append_row(row_data, value_input_option='USER_ENTERED'))
        return True
    except Exception as e:
        print(f"Google Sheets log error: {e}")
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
        
        entry_time = datetime.fromisoformat(signal.get("Timestamp_UTC"))
        time_in_trade = round((datetime.fromisoformat(exit_time) - entry_time).total_seconds() / 60, 2)
        
        update_tasks = [
            (f'Q{cell.row}', status),
            (f'R{cell.row}', exit_time), (f'S{cell.row}', exit_price),
            (f'T{cell.row}', pnl_usd), (f'U{cell.row}', pnl_percent),
            (f'W{cell.row}', reason), (f'X{cell.row}', time_in_trade)
        ]
            
        await loop.run_in_executor(None, lambda: TRADE_LOG_WS.batch_update([{'range': r, 'values': [[v]]} for r, v in update_tasks]))
        return True
    except gspread.exceptions.CellNotFound:
        print(f"Signal ID {signal_id} not found in Google Sheet to update.")
        return False
    except Exception as e:
        print(f"Google Sheets update error: {e}")
        return False
