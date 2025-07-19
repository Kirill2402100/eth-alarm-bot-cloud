# trade_executor.py
# ============================================================================
# v25.5 - Возвращена логика экстренного выхода и столбец Trigger_Order_USD
# ============================================================================
import asyncio
from datetime import datetime, timezone
import gspread

TRADE_LOG_WS = None

async def log_trade_to_sheet(decision: dict):
    """Асинхронно логирует новую сделку в Google Sheets."""
    if not TRADE_LOG_WS: return False
    try:
        # --- ИЗМЕНЕНИЕ: Возвращен столбец Trigger_Order_USD ---
        row_data = [
            decision.get("Signal_ID"),
            decision.get("Timestamp_UTC"),
            decision.get("Pair"),
            decision.get("Algorithm_Type"),
            decision.get("Strategy_Idea"),
            decision.get("Entry_Price"),
            decision.get("SL_Price"),
            decision.get("TP_Price"),
            decision.get("side"),
            decision.get("Deposit"),
            decision.get("Leverage"),
            "OPEN",  # Status
            None,    # Exit_Time_UTC
            None,    # Exit_Price
            None,    # PNL_USD
            None,    # PNL_Percent
            decision.get("Trigger_Order_USD") # Возвращенное поле
        ]
        # --- КОНЕЦ ИЗМЕНЕНИЯ ---
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: TRADE_LOG_WS.append_row(row_data, value_input_option='USER_ENTERED'))
        return True
    except Exception as e:
        print(f"Google Sheets log error: {e}")
        return False

async def update_trade_in_sheet(signal: dict, status: str, exit_price: float, pnl_usd: float, pnl_percent: float):
    """Асинхронно обновляет существующую сделку в Google Sheets."""
    if not TRADE_LOG_WS: return False
    try:
        signal_id = signal.get("Signal_ID")
        if not signal_id: return False

        loop = asyncio.get_event_loop()
        cell = await loop.run_in_executor(None, lambda: TRADE_LOG_WS.find(signal_id))
        if not cell: return False

        exit_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        
        # --- ИЗМЕНЕНИЕ: Буквы столбцов остались прежними, т.к. Trigger_Order_USD идет после обновляемых полей ---
        # Столбцы: L=Status, M=Exit_Time, N=Exit_Price, O=PNL_USD, P=PNL_%
        update_tasks = [
            (f'L{cell.row}', status),
            (f'M{cell.row}', exit_time),
            (f'N{cell.row}', exit_price),
            (f'O{cell.row}', pnl_usd),
            (f'P{cell.row}', pnl_percent),
        ]
        
        await loop.run_in_executor(None, lambda: TRADE_LOG_WS.batch_update([{'range': r, 'values': [[v]]} for r, v in update_tasks]))

        return True
    except gspread.exceptions.CellNotFound:
        print(f"Signal ID {signal_id} not found in Google Sheet to update.")
        return False
    except Exception as e:
        print(f"Google Sheets update error: {e}")
        return False
