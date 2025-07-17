# File: scanner_engine.py (v37.0 - Fixed Percentage SL/TP)
# Changelog 17-Jul-2025 (Europe/Belgrade):
# • Полный отказ от ATR в пользу фиксированного процентного SL/TP.
# • SL = 0.10%, TP = 0.15% от цены входа.
# • Удалена функция get_entry_atr и связанные с ней зависимости.

import asyncio
import pandas as pd
import ccxt.async_support as ccxt
from trade_executor import log_trade_to_sheet, update_trade_in_sheet
import time

# === Конфигурация Сканера и Стратегии =====================================
PAIR_TO_SCAN = 'BTC/USDT'
LARGE_ORDER_USD = 500000
TOP_N_ORDERS_TO_ANALYZE = 15
MIN_TOTAL_LIQUIDITY_USD = 2000000
MIN_IMBALANCE_RATIO = 3.0
MAX_PORTFOLIO_SIZE = 1

# --- Новые параметры для SL/TP ---
TP_PERCENT = 0.0015  # 0.15%
SL_PERCENT = 0.0010  # 0.10%
# RR ~1.5:1

# --- Старые параметры (больше не используются) ---
# MIN_RR_RATIO = 1.5
# SL_ATR_MULTIPLIER = 2.0
# ATR_CALCULATION_TIMEOUT = 10.0
# ATR_PERIOD = 14

COUNTER_ORDER_RATIO = 1.25

async def monitor_active_trades(exchange, app, broadcast_func, trade_log_ws, state, save_state_func):
    active_signals = state.get('monitored_signals')
    if not active_signals: return
    signal = active_signals[0]
    try:
        ticker = await exchange.fetch_ticker(signal['pair'])
        current_price = ticker.get('last')

        # print(f"MONITORING_PRICE_CHECK: Bot received price {current_price} at {pd.Timestamp.now(tz='UTC')}")

        if not current_price:
            print(f"Monitor Price Error: 'last' price was not received for {signal['pair']}.")
            return

        exit_status, exit_price = None, None
        trigger_order_usd = signal.get('trigger_order_usd', 0)
        if trigger_order_usd > 0:
            order_book = await exchange.fetch_order_book(signal['pair'], limit=25)
            if signal['side'] == 'LONG':
                counter_orders = [p * a for p, a in order_book.get('asks', []) if (p * a) > (trigger_order_usd * COUNTER_ORDER_RATIO)]
                if counter_orders: exit_status, exit_price = "EMERGENCY_EXIT", current_price
            elif signal['side'] == 'SHORT':
                counter_orders = [p * a for p, a in order_book.get('bids', []) if (p * a) > (trigger_order_usd * COUNTER_ORDER_RATIO)]
                if counter_orders: exit_status, exit_price = "EMERGENCY_EXIT", current_price
        
        if not exit_status:
            entry_price, sl_price, tp_price = signal['entry_price'], signal['sl_price'], signal['tp_price']
            if signal['side'] == 'LONG':
                if current_price <= sl_price: exit_status, exit_price = "SL_HIT", sl_price
                elif current_price >= tp_price: exit_status, exit_price = "TP_HIT", tp_price
            elif signal['side'] == 'SHORT':
                if current_price >= sl_price: exit_status, exit_price = "SL_HIT", sl_price
                elif current_price <= tp_price: exit_status, exit_price = "TP_HIT", tp_price
        
        if exit_status:
            entry_price = signal['entry_price']
            position_size_usd, leverage = 50, 100
            price_change_percent = ((exit_price - entry_price) / entry_price) if entry_price != 0 else 0
            if signal['side'] == 'SHORT': price_change_percent = -price_change_percent
            pnl_percent = price_change_percent * leverage * 100
            pnl_usd = position_size_usd * (pnl_percent / 100)
            await update_trade_in_sheet(trade_log_ws, signal, exit_status, exit_price, pnl_usd, pnl_percent)
            emoji = "⚠️" if exit_status == "EMERGENCY_EXIT" else ("✅" if pnl_usd > 0 else "❌")
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                   f"<b>Инструмент:</b> <code>{signal['pair']}</code>\n"
                   f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent:+.2f}%)</b>")
            await broadcast_func(app, msg)
            state['monitored_signals'] = []
            save_state_func()

    except Exception as e:
        error_message = f"⚠️ <b>Ошибка мониторинга сделки!</b>\n\nБот не смог проверить состояние позиции. Проверьте сделку вручную.\n\n<code>Ошибка: {e}</code>"
        print(f"CRITICAL MONITORING ERROR: {e}", exc_info=True)
        await broadcast_func(app, error_message)

# Функция get_entry_atr() больше не нужна и удалена

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
    dominant_side_is_bids = total_bids_usd > total_asks_usd
    if total_bids_usd > 0 and total_asks_usd > 0:
        imbalance_ratio = max(total_bids_usd / total_asks_usd, total_asks_usd / total_bids_usd)
    elif total_bids_usd > 0 or total_asks_usd > 0:
        imbalance_ratio = float('inf')

    if imbalance_ratio < MIN_IMBALANCE_RATIO: return

    dominant_side = "ПОКУПАТЕЛЕЙ" if dominant_side_is_bids else "ПРОДАВЦОВ"
    largest_order = (top_bids[0] if top_bids else None) if dominant_side_is_bids else (top_asks[0] if top_asks else None)
    expected_direction = "ВВЕРХ" if dominant_side_is_bids else "ВНИЗ"
    
    signal_msg = f"🔥 <b>АЛГО-СИГНАЛ!</b>\n"
    signal_msg += f"Сильный перевес на стороне {dominant_side} (дисбаланс {imbalance_ratio:.1f}x).\n"
    if largest_order:
        order_value_mln = largest_order['value_usd'] / 1000000
        order_price = largest_order['price']
        signal_msg += f"Ключевой ордер: ${order_value_mln:.2f} млн на уровне {order_price}.\n"
    signal_msg += f"Ожидание: вероятно движение {expected_direction}."
    
    await broadcast_func(app, signal_msg)

    try:
        trade_plan = {}
        ticker = await exchange.fetch_ticker(PAIR_TO_SCAN)
        current_price = ticker.get('last')
        
        if not current_price:
            await broadcast_func(app, f"⚠️ Не удалось получить текущую цену для {PAIR_TO_SCAN}. Сделка пропущена.")
            return
        
        # --- НОВЫЙ РАСЧЕТ SL/TP ---
        trade_plan['strategy_idea'] = "Pure Quant Entry (Fixed %)"
        trade_plan['entry_price'] = current_price
        if dominant_side_is_bids:
            trade_plan['side'] = "LONG"
            trade_plan['sl_price'] = current_price * (1 - SL_PERCENT)
            trade_plan['tp_price'] = current_price * (1 + TP_PERCENT)
        else: # dominant_side_is_sellers
            trade_plan['side'] = "SHORT"
            trade_plan['sl_price'] = current_price * (1 + SL_PERCENT)
            trade_plan['tp_price'] = current_price * (1 - TP_PERCENT)
        # --- КОНЕЦ НОВОГО РАСЧЕТА ---

        decision = {
            "signal_id": f"signal_{int(time.time() * 1000)}",
            "confidence_score": 10,
            "reason": f"Дисбаланс {imbalance_ratio:.1f}x в пользу {dominant_side}",
            "algorithm_type": "Imbalance",
        }
        decision.update(trade_plan)
        decision['pair'] = PAIR_TO_SCAN
        if largest_order:
            decision['trigger_order_usd'] = largest_order['value_usd']
        
        # entry_atr больше нет, передаем 0
        entry_atr = 0 
        
        msg = (f"<b>ВХОД В СДЕЛКУ</b>\n\n"
               f"<b>Тип:</b> {decision['strategy_idea']}\n"
               f"<b>Рассчитанный план (RR ~1.5:1):</b>\n"
               f" - Вход (<b>{decision['side']}</b>): <code>{decision['entry_price']:.2f}</code>\n"
               f" - SL: <code>{decision['sl_price']:.2f}</code>\n"
               f" - TP: <code>{decision['tp_price']:.2f}</code>")
        await broadcast_func(app, msg)

        # Логика сохранения сделки (сначала локально, потом в GSheets)
        state['monitored_signals'].append(decision)
        save_state_func()
        await broadcast_func(app, "✅ Сделка взята на мониторинг (сохранено локально).")

        try:
            success = await log_trade_to_sheet(trade_log_ws, decision, entry_atr) 
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
    print("Main Engine loop started (v37.0_fixed_percent).")
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
