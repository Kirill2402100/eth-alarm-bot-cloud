# File: trade_executor.py (v5 - Robust Sheet Update)

import uuid
import asyncio
from datetime import datetime, timezone

state = {}
save_state_func = None

def init_executor(main_state, main_save_func):
    global state, save_state_func
    state = main_state
    save_state_func = main_save_func

async def log_trade_to_sheet(worksheet, decision: dict, entry_atr: float):
    if not worksheet:
        print("Trade Executor: Worksheet not available.")
        return False
    try:
        signal_id = str(uuid.uuid4())[:8]
        entry_time = datetime.now(timezone.utc)
        pair = decision.get("pair")
        strategy = decision.get("strategy_idea", "")

        active_signal = {
            "signal_id": signal_id, "pair": pair, "status": "ACTIVE",
            "entry_time_utc": entry_time.isoformat(),
            "side": "LONG" if "Long" in strategy else "SHORT" if "Short" in strategy else "N/A",
            "entry_price": float(decision.get("entry_price")),
            "sl_price": float(decision.get("sl_price")), 
            "tp_price": float(decision.get("tp_price")),
            "entry_atr": entry_atr,
        }
        
        state.setdefault('monitored_signals', []).append(active_signal)
        save_state_func()
        print(f"Added {pair} to active monitoring portfolio.")

        row_data = [
            signal_id, entry_time.isoformat(), pair, decision.get("confidence_score"),
            decision.get("algorithm_type"), strategy, decision.get("reason"),
            active_signal["entry_price"], active_signal["sl_price"], active_signal["tp_price"],
            "ACTIVE", "", "", entry_atr, "", ""
        ]
        await asyncio.to_thread(worksheet.append_row, row_data, value_input_option='USER_ENTERED')
        print(f"✅ Trade logged to Google Sheets for pair {pair}")
        return True
    except Exception as e:
        print(f"❌ Error logging trade to Google Sheets: {e}")
        return False

async def update_trade_in_sheet(worksheet, signal: dict, exit_status: str, exit_price: float, pnl_usd: float, pnl_percent: float):
    if not worksheet: return
    try:
        # --- НОВАЯ, НАДЕЖНАЯ ЛОГИКА ПОИСКА СТРОКИ ---
        all_signal_ids = await asyncio.to_thread(worksheet.col_values, 1) # Получаем все значения из первой колонки (A)
        try:
            # Ищем индекс нашего signal_id. Добавляем 1, так как индексация списков с 0, а строк в таблице с 1.
            row_number = all_signal_ids.index(signal['signal_id']) + 1
        except ValueError:
            print(f"Could not find row for signal_id {signal['signal_id']}. Update failed.")
            return
        # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

        exit_time = datetime.now(timezone.utc).isoformat()
        # Диапазон обновления K:P для найденной строки
        update_range = f"K{row_number}:P{row_number}"
        update_values = [[exit_status, exit_time, exit_price, signal.get('entry_atr', ''), pnl_usd, pnl_percent]]
        
        await asyncio.to_thread(worksheet.update, update_range, update_values, value_input_option='USER_ENTERED')
        print(f"✅ Updated trade {signal['signal_id']} in Google Sheets with status {exit_status}.")
    except Exception as e:
        print(f"❌ Error updating trade in Google Sheets: {e}")
