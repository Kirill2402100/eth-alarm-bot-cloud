# File: scanner_engine.py (v33 - Pure Quant)
# Changelog 17-Jul-2025 (Europe/Belgrade):
# • Полный отказ от LLM. Решение о входе принимается на основе прохождения
#   алгоритмического фильтра.
# • Удален весь код, связанный с LLM, для повышения надежности и скорости.

import asyncio
import time
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from trade_executor import log_trade_to_sheet, update_trade_in_sheet

# === Конфигурация Сканера и Стратегии =====================================
PAIR_TO_SCAN = 'BTC/USDT'
TIMEFRAME = '15m'
LARGE_ORDER_USD = 500000
TOP_N_ORDERS_TO_ANALYZE = 15
MIN_TOTAL_LIQUIDITY_USD = 2000000
MIN_IMBALANCE_RATIO = 3.0
MAX_PORTFOLIO_SIZE = 1
MIN_RR_RATIO = 1.5
SL_ATR_MULTIPLIER = 2.0
COUNTER_ORDER_RATIO = 1.25

async def monitor_active_trades(exchange, app, broadcast_func, trade_log_ws, state, save_state_func):
    active_signals = state.get('monitored_signals')
    if not active_signals: return
    signal = active_signals[0]
    try:
        ticker = await exchange.fetch_ticker(signal['pair'])
        current_price = ticker.get('last')
        if not current_price: return
        exit_status, exit_price = None, None
        trigger_order_usd = signal.get('trigger_order_usd', 0)
        if trigger_order_usd > 0:
            order_book = await exchange.fetch_order_book(signal['pair'], limit=25)
            if signal['side'] == 'LONG':
                counter_orders = [p * a for p, a in order_book.get('asks', []) if (p * a) > (trigger_order_usd * COUNTER_ORDER_RATIO)]
                if counter_orders:
                    exit_status, exit_price = "EMERGENCY_EXIT", current_price
            elif signal['side'] == 'SHORT':
                counter_orders = [p * a for p, a in order_book.get('bids', []) if (p * a) > (trigger_order_usd * COUNTER_ORDER_RATIO)]
                if counter_orders:
                    exit_status, exit_price = "EMERGENCY_EXIT", current_price
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
        print(f"Monitor Error: {e}", exc_info=True)

async def get_entry_atr(exchange, pair):
    try:
        ohlcv = await exchange.fetch_ohlcv(pair, TIMEFRAME, limit=20)
        if not ohlcv or len(ohlcv) < 15:
            print(f"ATR Error: Not enough OHLCV data for {pair}. Got: {len(ohlcv) if ohlcv else 0}")
            return 0
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col])
        df.ta.atr(length=14, append=True)
        atr_value = df.iloc[-1]['ATR_14']
        if pd.isna(atr_value):
            print(f"ATR Error: ATR calculation resulted in NaN for {pair}.")
            return 0
        return atr_value
    except Exception as e:
        print(f"ATR Error: {e}", exc_info=True)
        return 0

async def scan_for_new_opportunities(exchange, app, broadcast_func, trade_log_ws, state, save_state_func):
    try:
        order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=50)
        large_bids = sorted([{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('bids', []) if p and a and (p*a > LARGE_ORDER_USD)], key=lambda x: x['value_usd'], reverse=True)
        large_asks = sorted([{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('asks', []) if p and a and (p*a > LARGE_ORDER_USD)], key=lambda x: x['value_usd'], reverse=True)
    except Exception as e:
        print(f"Could not fetch order book for {PAIR_TO_SCAN}: {e}")
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

    # --- ЛОГИКА ПРИНЯТИЯ РЕШЕНИЙ БЕЗ LLM ---
    dominant_side = "ПОКУПАТЕЛЕЙ" if dominant_side_is_bids else "ПРОДАВЦОВ"
    largest_order = (top_bids[0] if top_bids else None) if dominant_side_is_bids else (top_asks[0] if top_asks else None)
    
    reason_text = f"Алгоритмический фильтр пройден. Дисбаланс {imbalance_ratio:.1f}x в пользу {dominant_side}."
    
    await broadcast_func(app, f"🔥 <b>АЛГО-СИГНАЛ!</b>\n\n{reason_text}")

    try:
        trade_plan = {}
        ticker = await exchange.fetch_ticker(PAIR_TO_SCAN)
        current_price = ticker.get('last')
        if not current_price: return
        
        entry_atr = await get_entry_atr(exchange, PAIR_TO_SCAN)
        if entry_atr == 0:
            await broadcast_func(app, "⚠️ Не удалось рассчитать ATR. Сделка пропущена.")
            return
        
        trade_plan['strategy_idea'] = "Pure Quant Entry (ATR)"
        trade_plan['entry_price'] = current_price
        if dominant_side_is_bids:
            trade_plan['side'] = "LONG"
            trade_plan['sl_price'] = current_price - (entry_atr * SL_ATR_MULTIPLIER)
            trade_plan['tp_price'] = current_price + (entry_atr * SL_ATR_MULTIPLIER * MIN_RR_RATIO)
        else:
            trade_plan['side'] = "SHORT"
            trade_plan['sl_price'] = current_price + (entry_atr * SL_ATR_MULTIPLIER)
            trade_plan['tp_price'] = current_price - (entry_atr * SL_ATR_MULTIPLIER * MIN_RR_RATIO)

        decision = {
            "confidence_score": 10, # Максимальная уверенность, т.к. фильтр пройден
            "reason": reason_text,
            "algorithm_type": "Imbalance",
        }
        decision.update(trade_plan)
        decision['pair'] = PAIR_TO_SCAN
        if largest_order:
            decision['trigger_order_usd'] = largest_order['value_usd']
        
        msg = (f"<b>ВХОД В СДЕЛКУ</b>\n\n"
               f"<b>Тип:</b> {decision['strategy_idea']}\n"
               f"<b>Рассчитанный план (RR ~{MIN_RR_RATIO:.1f}:1):</b>\n"
               f"  - Вход: <code>{decision['entry_price']:.2f}</code>\n"
               f"  - SL: <code>{decision['sl_price']:.2f}</code>\n"
               f"  - TP: <code>{decision['tp_price']:.2f}</code>")
        await broadcast_func(app, msg)

        success = await log_trade_to_sheet(trade_log_ws, decision, entry_atr, state, save_state_func)
        if success:
            await broadcast_func(app, "✅ Виртуальная сделка успешно залогирована и взята на мониторинг.")

    except Exception as e:
        print(f"Error processing new opportunity: {e}", exc_info=True)

async def scanner_main_loop(app, broadcast_func, trade_log_ws, state, save_state_func):
    print("Main Engine loop started (v33_pure_quant).")
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}})
    scan_interval = 15 # секунд
    
    while state.get("bot_on", True):
        try:
            # Мониторим активные сделки чаще, чем ищем новые
            await monitor_active_trades(exchange, app, broadcast_func, trade_log_ws, state, save_state_func)
            
            # Сканируем новые возможности только если нет активных сделок
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
