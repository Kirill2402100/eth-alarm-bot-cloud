# File: scanner_engine.py (v35.1 - Price Check)
# Changelog 17-Jul-2025 (Europe/Belgrade):
# • Добавлен диагностический print для отслеживания цены, получаемой
#   от биржи в цикле мониторинга.

import asyncio
import pandas as pd
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
ATR_CALCULATION_TIMEOUT = 10.0
ATR_PERIOD = 14

async def monitor_active_trades(exchange, app, broadcast_func, trade_log_ws, state, save_state_func):
    active_signals = state.get('monitored_signals')
    if not active_signals: return
    signal = active_signals[0]
    try:
        ticker = await exchange.fetch_ticker(signal['pair'])
        current_price = ticker.get('last')

        # --- ДИАГНОСТИЧЕСКАЯ СТРОКА ---
        print(f"MONITORING_PRICE_CHECK: Bot received price {current_price} at {pd.Timestamp.now(tz='UTC')}")
        # --- КОНЕЦ ДИАГНОСТИЧЕСКОЙ СТРОКИ ---

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

async def get_entry_atr(exchange, pair):
    try:
        ohlcv = await exchange.fetch_ohlcv(pair, TIMEFRAME, limit=ATR_PERIOD * 2)
        if not ohlcv or len(ohlcv) < ATR_PERIOD:
            return 0
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col])
        high_low = df['high'] - df['low']
        high_prev_close = (df['high'] - df['close'].shift()).abs()
        low_prev_close = (df['low'] - df['close'].shift()).abs()
        tr = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/ATR_PERIOD, adjust=False).mean()
        atr_value = atr.iloc[-1]
        return atr_value if not pd.isna(atr_value) and atr_value > 0 else 0
    except Exception as e:
        print(f"ATR Calculation Error: {e}", exc_info=True)
        return 0

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
        
        entry_atr = await asyncio.wait_for(get_entry_atr(exchange, PAIR_TO_SCAN), timeout=ATR_CALCULATION_TIMEOUT)

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
            "confidence_score": 10,
            "reason": f"Дисбаланс {imbalance_ratio:.1f}x в пользу {dominant_side}",
            "algorithm_type": "Imbalance",
        }
        decision.update(trade_plan)
        decision['pair'] = PAIR_TO_SCAN
        if largest_order:
            decision['trigger_order_usd'] = largest_order['value_usd']
        
        msg = (f"<b>ВХОД В СДЕЛКУ</b>\n\n"
               f"<b>Тип:</b> {decision['strategy_idea']}\n"
               f"<b>Рассчитанный план (RR ~{MIN_RR_RATIO:.1f}:1):</b>\n"
               f" - Вход (<b>{decision['side']}</b>): <code>{decision['entry_price']:.2f}</code>\n"
               f" - SL: <code>{decision['sl_price']:.2f}</code>\n"
               f" - TP: <code>{decision['tp_price']:.2f}</code>")
        await broadcast_func(app, msg)

        success = await log_trade_to_sheet(trade_log_ws, decision, entry_atr, state, save_state_func)
        if success:
            await broadcast_func(app, "✅ Виртуальная сделка успешно залогирована и взята на мониторинг.")

    except asyncio.TimeoutError:
        await broadcast_func(app, "⚠️ Не удалось рассчитать ATR (превышен таймаут). Сделка пропущена.")
    except Exception as e:
        print(f"Error processing new opportunity: {e}", exc_info=True)
        await broadcast_func(app, "Произошла внутренняя ошибка при обработке сигнала.")


async def scanner_main_loop(app, broadcast_func, trade_log_ws, state, save_state_func):
    print("Main Engine loop started (v35.1_price_check).")
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
