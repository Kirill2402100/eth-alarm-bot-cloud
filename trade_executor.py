# File: trade_executor.py
# Contains functions for interacting with Google Sheets.

import gspread
from datetime import datetime, timezone

# Список заголовков для поиска колонок по имени
HEADERS = [
    "Signal_ID", "Timestamp_UTC", "Pair", "Confidence_Score", "Algorithm_Type", 
    "Strategy_Idea", "LLM_Reason", "Entry_Price", "SL_Price", "TP_Price", 
    "Status", "Exit_Time_UTC", "Exit_Price", "Entry_ATR", "PNL_USD", "PNL_Percent",
    "Trigger_Order_USD" # Убедитесь, что этот заголовок есть в вашей таблице
]

# Эта функция используется при входе в сделку.
# Она добавляет новую строку.
async def log_trade_to_sheet(worksheet, decision, entry_atr):
    if not worksheet: return False
    try:
        # Убедимся, что все поля из HEADERS существуют в decision
        row_to_add = []
        for header in HEADERS:
            # Поля, которые должны быть пустыми при входе
            if header in ["Status", "Exit_Time_UTC", "Exit_Price", "PNL_USD", "PNL_Percent"]:
                # Ставим 'ACTIVE' при создании
                row_to_add.append('ACTIVE' if header == "Status" else '')
            else:
                row_to_add.append(decision.get(header, ''))
        
        worksheet.append_row(row_to_add)
        return True
    except Exception as e:
        print(f"GSheets log_trade_to_sheet Error: {e}", exc_info=True)
        return False

# --- НОВАЯ, НАДЕЖНАЯ ФУНКЦИЯ ОБНОВЛЕНИЯ ---
# Эта функция используется при выходе из сделки.
# Она находит нужную строку и обновляет её.
async def update_trade_in_sheet(worksheet, signal, exit_status, exit_price, pnl_usd, pnl_percent):
    if not worksheet: return False
    try:
        signal_id_to_find = signal.get('signal_id')
        if not signal_id_to_find:
            print("Update Error: signal_id not found in the signal object.")
            return False

        # 1. Находим ячейку с ID сигнала
        cell = worksheet.find(signal_id_to_find)
        if not cell:
            print(f"Update Error: Could not find row with Signal_ID {signal_id_to_find}")
            return False

        # 2. Готовим данные для обновления
        row_number = cell.row
        updates = [
            {'range': f"K{row_number}", 'values': [[exit_status]]}, # Status
            {'range': f"L{row_number}", 'values': [[datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')]]}, # Exit_Time_UTC
            {'range': f"M{row_number}", 'values': [[exit_price]]}, # Exit_Price
            {'range': f"O{row_number}", 'values': [[pnl_usd]]},     # PNL_USD
            {'range': f"P{row_number}", 'values': [[pnl_percent]]}, # PNL_Percent
        ]
        
        # 3. Отправляем все обновления одним пакетом
        worksheet.batch_update(updates)
        print(f"Successfully updated trade {signal_id_to_find} in Google Sheets.")
        return True

    except gspread.exceptions.CellNotFound:
        print(f"Update Error: Cell with Signal_ID {signal_id_to_find} not found.")
        return False
    except Exception as e:
        print(f"GSheets update_trade_in_sheet Error: {e}", exc_info=True)
        return False
