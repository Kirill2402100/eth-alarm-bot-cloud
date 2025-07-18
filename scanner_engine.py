import asyncio
import pandas as pd
import ccxt.async_support as ccxt
from trade_executor import log_trade_to_sheet, update_trade_in_sheet
import time
from datetime import datetime, timezone
import numpy as np

# === Конфигурация ===
PAIR_TO_SCAN = 'BTC/USDT'
TOP_N_ORDERS_TO_ANALYZE = 15
MAX_PORTFOLIO_SIZE = 1
TP_PERCENT = 0.0015
SL_PERCENT = 0.0010
COUNTER_ORDER_RATIO = 1.25

PARAM_BOUNDS = {
    'MIN_LIQUIDITY': 1000000, 'MAX_LIQUIDITY': 6000000,
    'MIN_IMBALANCE': 2.0, 'MAX_IMBALANCE': 4.0,
    'MIN_LARGE_ORDER': 200000, 'MAX_LARGE_ORDER': 800000
}

class DynamicParameters:
    def __init__(self):
        self.MIN_TOTAL_LIQUIDITY_USD = 2000000
        self.MIN_IMBALANCE_RATIO = 2.5
        self.LARGE_ORDER_USD = 300000

    def update(self, new_params):
        # Применяем коридор к новым параметрам
        self.MIN_TOTAL_LIQUIDITY_USD = int(np.clip(
            new_params['MIN_TOTAL_LIQUIDITY_USD'], PARAM_BOUNDS['MIN_LIQUIDITY'], PARAM_BOUNDS['MAX_LIQUIDITY']
        ))
        self.MIN_IMBALANCE_RATIO = round(np.clip(
            new_params['MIN_IMBALANCE_RATIO'], PARAM_BOUNDS['MIN_IMBALANCE'], PARAM_BOUNDS['MAX_IMBALANCE']
        ), 1)
        self.LARGE_ORDER_USD = int(np.clip(
            new_params['LARGE_ORDER_USD'], PARAM_BOUNDS['MIN_LARGE_ORDER'], PARAM_BOUNDS['MAX_LARGE_ORDER']
        ))

# (monitor_active_trades без изменений)
async def monitor_active_trades(exchange, app, broadcast_func, trade_log_ws, state, save_state_func):
    active_signals = state.get('monitored_signals')
    if not active_signals: return
    signal = active_signals[0]
    try:
        ohlcv = await exchange.fetch_ohlcv(signal['Pair'], timeframe='1m', limit=1)
        if not ohlcv: return

        current_candle = ohlcv[0]
        candle_high = float(current_candle[2])
        candle_low = float(current_candle[3])
        exit_status, exit_price = None, None
        entry_price, sl_price, tp_price = signal['Entry_Price'], signal['SL_Price'], signal['TP_Price']
        
        if signal['side'] == 'LONG':
            if candle_low <= sl_price: exit_status, exit_price = "SL_HIT", sl_price
            elif candle_high >= tp_price: exit_status, exit_price = "TP_HIT", tp_price
        elif signal['side'] == 'SHORT':
            if candle_high >= sl_price: exit_status, exit_price = "SL_HIT", sl_price
            elif candle_low <= tp_price: exit_status, exit_price = "TP_HIT", tp_price
        
        if not exit_status:
            trigger_order_usd = signal.get('Trigger_Order_USD', 0)
            if trigger_order_usd > 0:
                order_book = await exchange.fetch_order_book(signal['Pair'], limit=25)
                current_price = float(current_candle[4])
                if signal['side'] == 'LONG' and any((p*a) > (trigger_order_usd * COUNTER_ORDER_RATIO) for p, a in order_book.get('asks', [])):
                    exit_status, exit_price = "EMERGENCY_EXIT", current_price
                elif signal['side'] == 'SHORT' and any((p*a) > (trigger_order_usd * COUNTER_ORDER_RATIO) for p, a in order_book.get('bids', [])):
                    exit_status, exit_price = "EMERGENCY_EXIT", current_price

        if exit_status:
            pnl_percent = (((exit_price - entry_price) / entry_price if entry_price != 0 else 0) * (-1 if signal['side'] == 'SHORT' else 1) * 100 * 100)
            pnl_usd = 50 * (pnl_percent / 100)
            await update_trade_in_sheet(trade_log_ws, signal, exit_status, exit_price, pnl_usd, pnl_percent)
            emoji = "⚠️" if exit_status == "EMERGENCY_EXIT" else ("✅" if pnl_usd > 0 else "❌")
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                   f"<b>Инструмент:</b> <code>{signal['Pair']}</code>\n"
                   f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent:+.2f}%)</b>")
            await broadcast_func(app, msg)
            state['monitored_signals'] = []
            save_state_func()

    except Exception as e:
        error_message = f"⚠️ <b>Ошибка мониторинга сделки!</b>\n<code>Ошибка: {e}</code>"
        print(f"CRITICAL MONITORING ERROR: {e}", exc_info=True)
        await broadcast_func(app, error_message)

# --- НОВАЯ ВЕРСИЯ ФУНКЦИИ КАЛИБРОВКИ ---
async def recalculate_parameters_periodically(exchange, params_manager, app, broadcast_func, state):
    """Каждые 10 минут адаптирует параметры."""
    while True:
        await asyncio.sleep(600) # Ждем 10 минут
        
        time_since_last_trade = time.time() - state.get('last_trade_timestamp', 0)
        
        if time_since_last_trade > 600: # Если сделок не было
            await broadcast_func(app, "⏳ Сделок за 10 мин не было, смягчаю фильтры на 10%...")
            
            # Берем ТЕКУЩИЕ параметры и УМЕНЬШАЕМ их
            new_params = {
                'MIN_TOTAL_LIQUIDITY_USD': params_manager.MIN_TOTAL_LIQUIDITY_USD * 0.9,
                'MIN_IMBALANCE_RATIO': params_manager.MIN_IMBALANCE_RATIO * 0.9,
                'LARGE_ORDER_USD': params_manager.LARGE_ORDER_USD * 0.9
            }
            params_manager.update(new_params)

        else: # Если сделки были
            await broadcast_func(app, "⏳ Сделки были, запускаю стандартную калибровку (2 мин)...")
            
            liquidity_samples, imbalance_samples = [], []
            for _ in range(8): # Собираем данные 2 минуты
                try:
                    order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=100)
                    bids = sorted([p * a for p, a in order_book.get('bids', [])], reverse=True)
                    asks = sorted([p * a for p, a in order_book.get('asks', [])], reverse=True)
                    top_bids_usd = sum(bids[:TOP_N_ORDERS_TO_ANALYZE])
                    top_asks_usd = sum(asks[:TOP_N_ORDERS_TO_ANALYZE])
                    liquidity_samples.append(top_bids_usd + top_asks_usd)
                    if top_bids_usd > 0 and top_asks_usd > 0:
                        imbalance_samples.append(max(top_bids_usd, top_asks_usd) / min(top_bids_usd, top_asks_usd))
                except Exception as e:
                    print(f"Data collection sample failed: {e}")
                await asyncio.sleep(15)

            if liquidity_samples and imbalance_samples:
                median_liquidity = np.median(liquidity_samples)
                median_imbalance = np.median(imbalance_samples)
                base_params = {
                    'MIN_TOTAL_LIQUIDITY_USD': median_liquidity * 0.8,
                    'MIN_IMBALANCE_RATIO': median_imbalance * 1.2,
                    'LARGE_ORDER_USD': median_liquidity / 10
                }
                params_manager.update(base_params)
            else:
                await broadcast_func(app, "⚠️ Калибровка не удалась: нет данных.")
                continue

        report_msg = (
            f"📈 <b>Параметры обновлены (v{app.bot_version})</b>\n\n"
            f"<b>Ликвидность:</b> <code>${params_manager.MIN_TOTAL_LIQUIDITY_USD:,}</code>\n"
            f"<b>Дисбаланс:</b> <code>{params_manager.MIN_IMBALANCE_RATIO:.1f}x</code>\n"
            f"<b>Крупный ордер:</b> <code>${params_manager.LARGE_ORDER_USD:,}</code>"
        )
        await broadcast_func(app, report_msg)

# (scan_for_new_opportunities без изменений)
async def scan_for_new_opportunities(exchange, params_manager, app, broadcast_func, trade_log_ws, state, save_state_func):
    try:
        order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=50)
    except Exception as e:
        print(f"Order Book Error: {e}")
        return

    large_order_usd = params_manager.LARGE_ORDER_USD
    min_total_liquidity = params_manager.MIN_TOTAL_LIQUIDITY_USD
    min_imbalance_ratio = params_manager.MIN_IMBALANCE_RATIO

    large_bids = sorted([{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('bids', []) if p and a and (p*a > large_order_usd)], key=lambda x: x['value_usd'], reverse=True)
    large_asks = sorted([{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('asks', []) if p and a and (p*a > large_order_usd)], key=lambda x: x['value_usd'], reverse=True)
    total_bids_usd = sum(b['value_usd'] for b in large_bids[:TOP_N_ORDERS_TO_ANALYZE])
    total_asks_usd = sum(a['value_usd'] for a in large_asks[:TOP_N_ORDERS_TO_ANALYZE])
    
    if (total_bids_usd + total_asks_usd) < min_total_liquidity: return
    
    imbalance_ratio = (max(total_bids_usd, total_asks_usd) / min(total_bids_usd, total_asks_usd) 
                       if total_bids_usd > 0 and total_asks_usd > 0 else float('inf'))
    if imbalance_ratio < min_imbalance_ratio: return

    dominant_side_is_bids = total_bids_usd > total_asks_usd
    dominant_side = "ПОКУПАТЕЛЕЙ" if dominant_side_is_bids else "ПРОДАВЦОВ"
    largest_order = (large_bids[0] if large_bids else None) if dominant_side_is_bids else (large_asks[0] if large_asks else None)
    expected_direction = "ВВЕРХ" if dominant_side_is_bids else "ВНИЗ"
    
    signal_msg = (f"🔥 <b>АЛГО-СИГНАЛ!</b>\n"
                  f"Сильный перевес на стороне {dominant_side} (дисбаланс {imbalance_ratio:.1f}x).\n")
    if largest_order:
        signal_msg += f"Ключевой ордер: ${largest_order['value_usd']/1e6:.2f} млн на уровне {largest_order['price']}.\n"
    signal_msg += f"Ожидание: вероятно движение {expected_direction}."
    
    await broadcast_func(app, signal_msg)

    try:
        ticker = await exchange.fetch_ticker(PAIR_TO_SCAN)
        current_price = ticker.get('last')
        if not current_price:
            await broadcast_func(app, f"⚠️ Не удалось получить цену. Сделка пропущена.")
            return

        side = "LONG" if dominant_side_is_bids else "SHORT"
        sl_price = current_price * (1 - SL_PERCENT if side == "LONG" else 1 + SL_PERCENT)
        tp_price = current_price * (1 + TP_PERCENT if side == "LONG" else 1 - TP_PERCENT)

        decision = {
            "Signal_ID": f"signal_{int(time.time() * 1000)}", "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            "Pair": PAIR_TO_SCAN, "Confidence_Score": 10, "Algorithm_Type": "Imbalance",
            "Strategy_Idea": f"Дисбаланс {imbalance_ratio:.1f}x в пользу {dominant_side}",
            "Entry_Price": current_price, "SL_Price": sl_price, "TP_Price": tp_price, "side": side,
            "Trigger_Order_USD": largest_order['value_usd'] if largest_order else 0,
            "Param_Liquidity": min_total_liquidity, "Param_Imbalance": min_imbalance_ratio,
            "Param_Large_Order": large_order_usd
        }
        
        msg = (f"<b>ВХОД В СДЕЛКУ</b>\n\n"
               f"<b>Тип:</b> Pure Quant Entry (Fixed %)\n"
               f"<b>Рассчитанный план (RR ~1.5:1):</b>\n"
               f" - Вход (<b>{side}</b>): <code>{current_price:.2f}</code>\n"
               f" - SL: <code>{sl_price:.2f}</code>\n"
               f" - TP: <code>{tp_price:.2f}</code>")
        await broadcast_func(app, msg)

        state['monitored_signals'].append(decision)
        state['last_trade_timestamp'] = time.time()
        save_state_func()
        await broadcast_func(app, "✅ Сделка взята на мониторинг.")
        
        if await log_trade_to_sheet(trade_log_ws, decision):
            await broadcast_func(app, "✅ ...успешно залогирована в Google Sheets.")
        else:
            await broadcast_func(app, "⚠️ Не удалось сохранить сделку в Google Sheets.")

    except Exception as e:
        print(f"Error processing new opportunity: {e}", exc_info=True)
        await broadcast_func(app, "Произошла внутренняя ошибка при обработке сигнала.")

# (scanner_main_loop без изменений, кроме передачи 'state')
async def scanner_main_loop(app, broadcast_func, trade_log_ws, state, save_state_func):
    bot_version = "15.0.0"
    app.bot_version = bot_version
    print(f"Main Engine loop started (v{bot_version}).")
    
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}})
    params_manager = DynamicParameters()
    
    recalculation_task = asyncio.create_task(
        recalculate_parameters_periodically(exchange, params_manager, app, broadcast_func, state)
    )

    scan_interval = 15
    while state.get("bot_on", True):
        try:
            await monitor_active_trades(exchange, app, broadcast_func, trade_log_ws, state, save_state_func)
            if not state.get('monitored_signals'):
                await scan_for_new_opportunities(exchange, params_manager, app, broadcast_func, trade_log_ws, state, save_state_func)
            await asyncio.sleep(scan_interval)
        except asyncio.CancelledError:
            print("Main Engine loop cancelled.")
            recalculation_task.cancel()
            break
        except Exception as e:
            print(f"CRITICAL Error in Main Engine loop: {e}", exc_info=True)
            await asyncio.sleep(60)
            
    print("Main Engine loop stopped.")
    await exchange.close()
