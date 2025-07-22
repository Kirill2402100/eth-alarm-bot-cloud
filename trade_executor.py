============================================================================
v38.0 - УПРОЩЕННАЯ СТРАТЕГИЯ: ОБНОВЛЕНИЕ row_data И СТОЛБЦОВ
- Убраны Imbalance_Ratio, Aggression_Side, Trigger_Order_USD из row_data
- Обновлены буквы столбцов в update_trade_in_sheet (P для Status, U для Exit_Reason, V для Time_In_Trade)
- DEBUG row_data адаптировано
============================================================================
import asyncio
from datetime import datetime, timezone
import gspread

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
decision.get("ADX"), decision.get("PDI"), decision.get("MDI"), decision.get("ATR"),
"OPEN", None, None, None, None, None, None
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

entry_time = datetime.strptime(signal.get("Timestamp_UTC"), '%Y-%m%d %H:%M:%S')
time_in_trade = round((datetime.strptime(exit_time, '%Y-%m-%d %H:%M:%S') - entry_time).total_seconds() / 60, 2)

update_tasks = [
{'range': f'P{cell.row}', 'values': [[status]]},
{'range': f'Q{cell.row}', 'values': [[exit_time]]},
{'range': f'R{cell.row}', 'values': [[exit_price]]},
{'range': f'S{cell.row}', 'values': [[pnl_usd]]},
{'range': f'T{cell.row}', 'values': [[pnl_percent]]},
{'range': f'U{cell.row}', 'values': [[reason]]},
{'range': f'V{cell.row}', 'values': [[time_in_trade]]}
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
data.get("Timestamp_UTC"), data.get("ADX"), data.get("PDI"), data.get("MDI"),
data.get("ATR"), data.get("Side"), data.get("DI_Diff"), data.get("Reason_Prop")
]
loop = asyncio.get_event_loop()
await loop.run_in_executor(None, lambda: DEBUG_LOG_WS.append_row(row_data, value_input_option='USER_ENTERED'))
return True
except Exception as e:
log.error(f"Google Sheets debug log error: {e}")
return False
