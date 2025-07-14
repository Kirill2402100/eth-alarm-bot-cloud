# File: trade_monitor.py

import asyncio
from data_feeder import last_data
from trade_executor import update_trade_in_sheet

# Переменные, которые будут установлены из основного файла
state = {}
save_state_func = None
broadcast_func = None
trade_log_ws = None

def init_monitor(main_state, main_save_func, main_broadcast_func, main_ws):
    """Инициализирует монитор, передавая ему нужные объекты."""
    global state, save_state_func, broadcast_func, trade_log_ws
    state = main_state
    save_state_func = main_save_func
    broadcast_func = main_broadcast_func
    trade_log_ws = main_ws

async def monitor_main_loop(app):
    print("Trade Monitor loop started.")
    while True:
        await asyncio.sleep(3) # Проверяем цены каждые 3 секунды
        
        if not state.get('monitored_signals'):
            continue

        signals_to_remove = []
        for signal in state['monitored_signals']:
            pair_for_feed = signal['pair'] + ":USDT"
            
            # Получаем последнюю цену из нашего data_feeder'а
            price_data = last_data.get(pair_for_feed, {})
            current_price = price_data.get('last_price')
            
            if not current_price: continue

            exit_status, exit_price = None, None
            
            # Логика закрытия сделки
            if signal['side'] == 'LONG':
                if current_price <= signal['sl_price']:
                    exit_status, exit_price = "SL_HIT", signal['sl_price']
                elif current_price >= signal['tp_price']:
                    exit_status, exit_price = "TP_HIT", signal['tp_price']
            elif signal['side'] == 'SHORT':
                if current_price >= signal['sl_price']:
                    exit_status, exit_price = "SL_HIT", signal['sl_price']
                elif current_price <= signal['tp_price']:
                    exit_status, exit_price = "TP_HIT", signal['tp_price']

            if exit_status:
                print(f"Closing trade {signal['signal_id']} with status {exit_status}")
                # Обновляем запись в таблице
                await update_trade_in_sheet(trade_log_ws, signal, exit_status, exit_price)
                
                # Отправляем сообщение в Telegram
                pnl_percent = ((exit_price - signal['entry_price']) / signal['entry_price']) * 100
                if signal['side'] == 'SHORT': pnl_percent = -pnl_percent
                
                emoji = "✅" if exit_status == "TP_HIT" else "❌"
                msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                       f"<b>Инструмент:</b> <code>{signal['pair']}</code>\n"
                       f"<b>Результат: {pnl_percent:+.2f}%</b> (симуляция)")
                await broadcast_func(app, msg)
                
                signals_to_remove.append(signal)

        if signals_to_remove:
            # Удаляем закрытые сигналы из портфеля
            state['monitored_signals'] = [s for s in state['monitored_signals'] if s not in signals_to_remove]
            save_state_func()
