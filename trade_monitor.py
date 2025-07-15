# File: trade_monitor.py (v3 - Realistic Spread-Based Monitoring)

import asyncio
from data_feeder import last_data
from trade_executor import update_trade_in_sheet

# --- Переменные, которые будут установлены из основного файла ---
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
    print("Trade Monitor loop started (v_realistic_spread_check).")
    while True:
        try:
            await asyncio.sleep(2) # Проверяем цены каждые 2 секунды
            
            if not state.get('monitored_signals'):
                continue

            signals_to_remove = []
            for signal in state['monitored_signals']:
                pair_for_feed = signal['pair'] + ":USDT"
                
                price_data = last_data.get(pair_for_feed, {})
                # --- НОВАЯ ЛОГИКА: Получаем лучший бид и аск ---
                bids = price_data.get('bids', [])
                asks = price_data.get('asks', [])

                if not bids or not asks: continue

                best_bid = bids[0][0]
                best_ask = asks[0][0]

                exit_status, exit_price = None, None
                
                # --- УЛУЧШЕННАЯ ЛОГИКА ЗАКРЫТИЯ СДЕЛКИ ---
                if signal['side'] == 'LONG':
                    # Для закрытия лонга по TP, кто-то должен захотеть КУПИТЬ у нас по этой цене (смотрим на BID)
                    if best_bid >= signal['tp_price']:
                        exit_status, exit_price = "TP_HIT", signal['tp_price']
                    # Для закрытия лонга по SL, мы должны ПРОДАТЬ по рынку (смотрим на ASK)
                    elif best_ask <= signal['sl_price']:
                        exit_status, exit_price = "SL_HIT", signal['sl_price']

                elif signal['side'] == 'SHORT':
                    # Для закрытия шорта по TP, мы должны КУПИТЬ по рынку (смотрим на ASK)
                    if best_ask <= signal['tp_price']:
                        exit_status, exit_price = "TP_HIT", signal['tp_price']
                    # Для закрытия шорта по SL, кто-то должен ПРОДАТЬ нам по этой цене (смотрим на BID)
                    elif best_bid >= signal['sl_price']:
                        exit_status, exit_price = "SL_HIT", signal['sl_price']

                if exit_status:
                    # --- Расчет P&L (без изменений) ---
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
