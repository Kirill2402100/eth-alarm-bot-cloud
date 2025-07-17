# File: trade_executor.py (Final Version)
# Changelog 18-Jul-2025 (Europe/Belgrade):
# • Исправлена критическая ошибка в функции log_trade_to_sheet,
#   которая приводила к сбою при записи данных в Google-таблицу.
# • Логика сбора данных для строки была полностью переработана
#   для максимальной надежности.

import gspread
from datetime import datetime, timezone

# Список заголовков для точного соответствия с таблицей
HEADERS = [
    "Signal_ID", "Timestamp_UTC", "Pair", "Confidence_Score", "Algorithm_Type", 
    "Strategy_Idea", "LLM_Reason", "Entry_Price", "SL_Price", "TP_Price", 
    "Status", "Exit_Time_UTC", "Exit_Price", "Entry_ATR", "PNL_USD", "PNL_Percent",
    "Trigger_Order_USD"
]

async def log_trade_to_sheet(worksheet, decision, entry_atr):
    """
    Добавляет новую строку в таблицу при открытии сделки.
    Эта версия имеет надежную логику сборки строки.
    """
    if not worksheet: return False
    try:
        row_to_add = []
        # Собираем строку строго в порядке заголовков
        for header in HEADERS:
            if header == "Status":
                row_to_add.append("ACTIVE")
            elif header == "Entry_ATR":
                # Явно используем переданное значение ATR
                row_to_add.append(entry_atr)
            elif header in ["Exit_Time_UTC", "Exit_Price", "PNL_USD", "PNL_Percent"]:
                # Эти поля остаются пустыми при создании
                row_to_add.append("")
            else:
                # Все остальные данные берем из словаря 'decision'
                row_to_add.append(decision.get(header, ''))
        
        worksheet.append_row(row_to_add)
        return True
    except Exception as e:
        print(f"GSheets log_trade_to_sheet Error: {e}", exc_info=True)
        return False

async def update_trade_in_sheet(worksheet, signal, exit_status, exit_price, pnl_usd, pnl_percent):
    """
    Находит существующую строку по ID и обновляет ее при закрытии сделки.
    """
    if not worksheet: return False
    try:
        signal_id_to_find = signal.get('signal_id')
        if not signal_id_to_find:
            print("Update Error: signal_id not found in the signal object.")
            return False

        cell = worksheet.find(signal_id_to_find)
        if not cell:
            print(f"Update Error: Could not find row with Signal_ID {signal_id_to_find}")
            return False

        row_number = cell.row
        updates = [
            {'range': f"K{row_number}", 'values': [[exit_status]]}, # Status
            {'range': f"L{row_number}", 'values': [[datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')]]}, # Exit_Time_UTC
            {'range': f"M{row_number}", 'values': [[exit_price]]}, # Exit_Price
            {'range': f"O{row_number}", 'values': [[pnl_usd]]},     # PNL_USD
            {'range': f"P{row_number}", 'values': [[pnl_percent]]}, # PNL_Percent
        ]
        
        worksheet.batch_update(updates)
        print(f"Successfully updated trade {signal_id_to_find} in Google Sheets.")
        return True

    except gspread.exceptions.CellNotFound:
        print(f"Update Error: Cell with Signal_ID {signal_id_to_find} not found.")
        return False
    except Exception as e:
        print(f"GSheets update_trade_in_sheet Error: {e}", exc_info=True)
        return False
