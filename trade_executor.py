# File: trade_executor.py (v3 - With State Management)

import uuid
import asyncio
from datetime import datetime, timezone

# Эта функция будет вызываться из основного файла, чтобы передать нам доступ к состоянию
state = {}
def set_state_object(main_state):
    global state
    state = main_state

def save_state_func():
    # Эта функция будет установлена из основного файла
    pass

async def log_trade_to_sheet(worksheet, decision: dict):
    if not worksheet:
        print("Trade Executor: Worksheet not available. Skipping log.")
        return False

    try:
        signal_id = str(uuid.uuid4())[:8]
        entry_time = datetime.now(timezone.utc)
        
        # Создаем объект сигнала для мониторинга
        active_signal = {
            "signal_id": signal_id,
            "pair": decision.get("pair"),
            "status": "ACTIVE",
            "entry_time_utc": entry_time.isoformat(),
            "side": "LONG" if "Long" in decision.get("strategy_idea", "") else "SHORT",
            "entry_price": decision.get("entry_price"),
            "sl_price": decision.get("sl_price"),
            "tp_price": decision.get("tp_price"),
        }
        
        # Добавляем сигнал в портфель и сохраняем состояние
        state['monitored_signals'].append(active_signal)
        save_state_func()
        print(f"Added {active_signal['pair']} to active monitoring portfolio.")

        row_data = [
            signal_id, entry_time.isoformat(), decision.get("pair"), decision.get("confidence_score"),
            decision.get("algorithm_type"), decision.get("strategy_idea"), decision.get("reason"),
            active_signal["entry_price"], active_signal["sl_price"], active_signal["tp_price"],
            # Добавляем пустые поля для будущего обновления
            "ACTIVE", "", "" # Status, Exit_Time, Exit_Price
        ]
        
        await asyncio.to_thread(worksheet.append_row, row_data, value_input_option='USER_ENTERED')
        print(f"✅ Trade logged to Google Sheets for pair {decision.get('pair')}")
        return True

    except Exception as e:
        print(f"❌ Error logging trade to Google Sheets: {e}")
        return False

async def update_trade_in_sheet(worksheet, signal: dict, exit_status: str, exit_price: float):
    if not worksheet: return
    try:
        cell = await asyncio.to_thread(worksheet.find, signal['signal_id'])
        if not cell: return

        exit_time = datetime.now(timezone.utc).isoformat()
        
        # Обновляем ячейки в найденной строке
        update_range = f"K{cell.row}:M{cell.row}"
        update_values = [[exit_status, exit_time, exit_price]]
        
        await asyncio.to_thread(worksheet.update, update_range, update_values, value_input_option='USER_ENTERED')
        print(f"✅ Updated trade {signal['signal_id']} in Google Sheets with status {exit_status}.")

    except Exception as e:
        print(f"❌ Error updating trade in Google Sheets: {e}")
