# File: scanner_engine.py (v40.0 - Final Fix)
# Changelog 18-Jul-2025 (Europe/Belgrade):
# • Исправлено несоответствие ключей в словаре 'decision',
#   чтобы данные корректно записывались в Google-таблицу.

import asyncio
import pandas as pd
import ccxt.async_support as ccxt
from trade_executor import log_trade_to_sheet, update_trade_in_sheet
import time

# --- Конфигурация ---
PAIR_TO_SCAN = 'BTC/USDT'
LARGE_ORDER_USD = 500000
TOP_N_ORDERS_TO_ANALYZE = 15
MIN_TOTAL_LIQUIDITY_USD = 2000000
MIN_IMBALANCE_RATIO = 3.0
MAX_PORTFOLIO_SIZE = 1
TP_PERCENT = 0.0015
SL_PERCENT = 0.0010
COUNTER_ORDER_RATIO = 1.25

async def monitor_active_trades(exchange, app, broadcast_func, trade_log_ws, state, save_state_func):
    active_signals = state.get('monitored_signals')
    if not active_signals: return
    signal = active_signals[0]
    try:
        ohlcv = await exchange.fetch_ohlcv(signal['Pair'], timeframe='1m', limit=1)
        if not ohlcv:
            print(f"Monitor OHLCV Error: No 1m candle data received for {signal['Pair']}.")
            return
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
                if signal['side'] == 'LONG':
                    counter_orders = [p * a for p, a in order_book.get('asks', []) if (p * a) > (trigger_order_usd * COUNTER_ORDER_RATIO)]
                    if counter_orders: exit_status, exit_price = "EMERGENCY_EXIT", current_price
                elif signal['side'] == 'SHORT':
                    counter_orders = [p * a for p, a in order_book.get('bids', []) if (p * a) > (trigger_order_usd * COUNTER_ORDER_RATIO)]
                    if counter_orders: exit_status, exit_price = "EMERGENCY_EXIT", current_price
        if exit_status:
            position_size_usd, leverage = 50, 100
            price_change_percent = ((exit_price - entry_price) / entry_price) if entry_price != 0 else 0
            if signal['side'] == 'SHORT': price_change_percent = -price_change_percent
            pnl_percent = price_change_percent * leverage * 100
            pnl_usd = position_size_usd * (pnl_percent / 100)
            await update_trade_in_sheet(trade_log_ws, signal, exit_status, exit_price, pnl_usd, pnl_percent)
            emoji = "⚠️" if exit_status == "EMERGENCY_EXIT" else ("✅" if pnl_usd > 0 else "❌")
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                   f"<b>Инструмент:</b> <code>{signal['Pair']}</code>\n"
                   f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent:+.2f}%)</b>")
            await broadcast_func(app, msg)
            state['monitored_signals'] = []
            save_state_func()
    except Exception as e:
        error_message = f"⚠️ <b>Ошибка мониторинга сделки!</b>\n\n<code>Ошибка: {e}</code>"
        print(f"CRITICAL MONITORING ERROR: {e}", exc_info=True)
        await broadcast_func(app, error_message)

async def scan_for_new_opportunities(exchange, app, broadcast_func, trade_log_ws, state, save_state_func):
    try:
        order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=50)
        large_bids = sorted([{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('bids', []) if p and a and (p*a > LARGE_ORDER_USD)], key=lambda x: x['value_usd'], reverse=True)
        large_asks = sorted([{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('asks', []) if p and a and (p*a > LARGE_ORDER_USD)], key=lambda x: x['value_usd'], reverse=True)
    except Exception as e:
        print(f"Order Book Error: {e}")
        return

    top_bids = large_bids[:TOP_N_ORDERS_TO_ANALYZE]
    top_asks = large_asks[:TOP_N_ORDERS_TO_ANALYZE]
    total_bids_usd = sum(b['value_usd'] for b in top_bids)
    total_asks_usd = sum(a['value_usd'] for a in top_asks)
    if (total_bids_usd + total_asks_usd) < MIN_TOTAL_LIQUIDITY_USD: return
    
    imbalance_ratio = 0
    if total_bids_usd > 0 and total_asks_usd > 0:
        imbalance_ratio = max(total_bids_usd / total_asks_usd, total_asks_usd / total_bids_usd)
    elif total_bids_usd > 0 or total_asks_usd > 0:
        imbalance_ratio = float('inf')
    if imbalance_ratio < MIN_IMBALANCE_RATIO: return

    dominant_side_is_bids = total_bids_usd > total_asks_usd
    dominant_side = "ПОКУПАТЕЛЕЙ" if dominant_side_is_bids else "ПРОДАВЦОВ"
    largest_order = (top_bids[0] if top_bids else None) if dominant_side_is_bids else (top_asks[0] if top_asks else None)
    expected_direction = "ВВЕРХ" if dominant_side_is_bids else "ВНИЗ"
    
    signal_msg = (f"🔥 <b>АЛГО-СИГНАЛ!</b>\n"
                  f"Сильный перевес на стороне {dominant_side} (дисбаланс {imbalance_ratio:.1f}x).\n")
    if largest_order:
        order_value_mln = largest_order['value_usd'] / 1000000
        order_price = largest_order['price']
        signal_msg += f"Ключевой ордер: ${order_value_mln:.2f} млн на уровне {order_price}.\n"
    signal_msg += f"Ожидание: вероятно движение {expected_direction}."
    
    await broadcast_func(app, signal_msg)

    try:
        ticker = await exchange.fetch_ticker(PAIR_TO_SCAN)
        current_price = ticker.get('last')
        if not current_price:
            await broadcast_func(app, f"⚠️ Не удалось получить текущую цену для {PAIR_TO_SCAN}. Сделка пропущена.")
            return
        
        # --- ИСПРАВЛЕНИЕ: Ключи словаря приводятся в соответствие с HEADERS ---
        trade_plan = {"Strategy_Idea": "Pure Quant Entry (Fixed %)", "Entry_Price": current_price}
        if dominant_side_is_bids:
            trade_plan['side'] = "LONG"
            trade_plan['SL_Price'] = current_price * (1 - SL_PERCENT)
            trade_plan['TP_Price'] = current_price * (1 + TP_PERCENT)
        else:
            trade_plan['side'] = "SHORT"
            trade_plan['SL_Price'] = current_price * (1 + SL_PERCENT)
            trade_plan['TP_Price'] = current_price * (1 - TP_PERCENT)

        decision = {
            "Signal_ID": f"signal_{int(time.time() * 1000)}",
            "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            "Confidence_Score": 10,
            "LLM_Reason": f"Дисбаланс {imbalance_ratio:.1f}x в пользу {dominant_side}",
            "Algorithm_Type": "Imbalance",
            "Pair": PAIR_TO_SCAN,
        }
        decision.update(trade_plan)
        if largest_order:
            decision['Trigger_Order_USD'] = largest_order['value_usd']
        
        msg = (f"<b>ВХОД В СДЕЛКУ</b>\n\n"
               f"<b>Тип:</b> {decision['Strategy_Idea']}\n"
               f"<b>Рассчитанный план (RR ~1.5:1):</b>\n"
               f" - Вход (<b>{decision['side']}</b>): <code>{decision['Entry_Price']:.2f}</code>\n"
               f" - SL: <code>{decision['SL_Price']:.2f}</code>\n"
               f" - TP: <code>{decision['TP_Price']:.2f}</code>")
        await broadcast_func(app, msg)

        state['monitored_signals'].append(decision)
        save_state_func()
        await broadcast_func(app, "✅ Сделка взята на мониторинг (сохранено локально).")

        try:
            success = await log_trade_to_sheet(trade_log_ws, decision, 0) 
            if success:
                await broadcast_func(app, "✅ ...и успешно залогирована в Google Sheets.")
            else:
                await broadcast_func(app, "⚠️ Не удалось сохранить сделку в Google Sheets (ошибка записи).")
        except Exception as e:
            print(f"Google Sheets logging failed: {e}", exc_info=True)
            await broadcast_func(app, f"⚠️ Ошибка при записи в Google Sheets: {e}")

    except Exception as e:
        print(f"Error processing new opportunity: {e}", exc_info=True)
        await broadcast_func(app, "Произошла внутренняя ошибка при обработке сигнала.")

async def scanner_main_loop(app, broadcast_func, trade_log_ws, state, save_state_func):
    print("Main Engine loop started (v40.0_final_fix).")
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}})
    scan_interval = 15
    while state.get("bot_on", True):
        try:
            await monitor_active_trades(exchange, app, broadcast_func, trade_log_ws, state, save_state_func)
            if len(state.get('monitored_signals', [])) < MAX_PORTFOLIO_SIZE:
                await scan_for_new_opportunities(exchange, app, broadcast_func, trade_log_ws, state, save_state_func)
            await asyncio.sleep(scan_interval)
        except asyncio.CancelledError:
            print("Main Engine loop cancelled.")
            break
        except Exception as e:
            print(f"CRITICAL Error in Main Engine loop: {e}", exc_info=True)
            await asyncio.sleep(60)
    print("Main Engine loop stopped.")
    await exchange.close()
