# File: trade_monitor.py

import asyncio
from data_feeder import last_data
from trade_executor import update_trade_in_sheet

state = {}
save_state_func = None
broadcast_func = None
trade_log_ws = None

def init_monitor(main_state, main_save_func, main_broadcast_func, main_ws):
    global state, save_state_func, broadcast_func, trade_log_ws
    state = main_state
    save_state_func = main_save_func
    broadcast_func = main_broadcast_func
    trade_log_ws = main_ws

async def monitor_main_loop(app):
    print("Trade Monitor loop started.")
    while True:
        try:
            await asyncio.sleep(3)
            if not state.get('monitored_signals'):
                continue

            signals_to_remove = []
            for signal in state['monitored_signals']:
                pair_for_feed = signal['pair'] + ":USDT"
                price_data = last_data.get(pair_for_feed, {})
                current_price = price_data.get('last_price')
                if not current_price: continue

                exit_status, exit_price = None, None
                
                if signal['side'] == 'LONG':
                    if current_price <= signal['sl_price']: exit_status, exit_price = "SL_HIT", signal['sl_price']
                    elif current_price >= signal['tp_price']: exit_status, exit_price = "TP_HIT", signal['tp_price']
                elif signal['side'] == 'SHORT':
                    if current_price >= signal['sl_price']: exit_status, exit_price = "SL_HIT", signal['sl_price']
                    elif current_price <= signal['tp_price']: exit_status, exit_price = "TP_HIT", signal['tp_price']

                if exit_status:
                    position_size_usd = 50
                    leverage = 100
                    price_change_percent = ((exit_price - signal['entry_price']) / signal['entry_price'])
                    if signal['side'] == 'SHORT': price_change_percent = -price_change_percent
                    pnl_percent = price_change_percent * leverage * 100
                    pnl_usd = position_size_usd * (pnl_percent / 100)
                    
                    await update_trade_in_sheet(trade_log_ws, signal, exit_status, exit_price, pnl_usd, pnl_percent)
                    
                    emoji = "✅" if pnl_usd > 0 else "❌"
                    msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                           f"<b>Инструмент:</b> <code>{signal['pair']}</code>\n"
                           f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent:+.2f}%)</b>")
                    await broadcast_func(app, msg)
                    signals_to_remove.append(signal)

            if signals_to_remove:
                state['monitored_signals'] = [s for s in state['monitored_signals'] if s not in signals_to_remove]
                save_state_func()
        except asyncio.CancelledError:
            print("Trade Monitor loop cancelled.")
            break
        except Exception as e:
            print(f"Error in monitor loop: {e}")
