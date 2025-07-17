import gspread
from datetime import datetime, timezone

# Обновленный список заголовков
HEADERS = [
    "Signal_ID", "Timestamp_UTC", "Pair", "Confidence_Score", "Algorithm_Type", 
    "Strategy_Idea", "Entry_Price", "SL_Price", "TP_Price", 
    "Status", "Exit_Time_UTC", "Exit_Price", "Entry_ATR", "PNL_USD", "PNL_Percent",
    "Trigger_Order_USD"
]

async def log_trade_to_sheet(worksheet, decision, entry_atr):
    if not worksheet: return False
    try:
        row_to_add = []
        for header in HEADERS:
            if header == "Status": row_to_add.append("ACTIVE")
            elif header == "Entry_ATR": row_to_add.append(entry_atr)
            elif header in ["Exit_Time_UTC", "Exit_Price", "PNL_USD", "PNL_Percent"]:
                row_to_add.append("")
            else:
                row_to_add.append(decision.get(header, ''))
        worksheet.append_row(row_to_add)
        return True
    except Exception as e:
        print(f"GSheets log_trade_to_sheet Error: {e}", exc_info=True)
        return False

async def update_trade_in_sheet(worksheet, signal, exit_status, exit_price, pnl_usd, pnl_percent):
    if not worksheet: return False
    try:
        signal_id_to_find = signal.get('Signal_ID')
        if not signal_id_to_find: return False
        
        cell = worksheet.find(signal_id_to_find)
        if not cell: return False

        row_number = cell.row
        # Определяем буквы колонок по их позиции в HEADERS
        status_col = chr(ord('A') + HEADERS.index('Status'))
        exit_time_col = chr(ord('A') + HEADERS.index('Exit_Time_UTC'))
        exit_price_col = chr(ord('A') + HEADERS.index('Exit_Price'))
        pnl_usd_col = chr(ord('A') + HEADERS.index('PNL_USD'))
        pnl_percent_col = chr(ord('A') + HEADERS.index('PNL_Percent'))

        updates = [
            {'range': f"{status_col}{row_number}", 'values': [[exit_status]]},
            {'range': f"{exit_time_col}{row_number}", 'values': [[datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')]]},
            {'range': f"{exit_price_col}{row_number}", 'values': [[exit_price]]},
            {'range': f"{pnl_usd_col}{row_number}", 'values': [[pnl_usd]]},
            {'range': f"{pnl_percent_col}{row_number}", 'values': [[pnl_percent]]},
        ]
        worksheet.batch_update(updates)
        print(f"Successfully updated trade {signal_id_to_find} in Google Sheets.")
        return True
    except Exception as e:
        print(f"GSheets update_trade_in_sheet Error: {e}", exc_info=True)
        return False
