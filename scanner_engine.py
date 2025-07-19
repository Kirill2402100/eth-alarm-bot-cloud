# scanner_engine.py
# ============================================================================
# v25.5 - Возвращена логика экстренного выхода
# ============================================================================
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
TP_PERCENT = 0.0018
SL_PERCENT = 0.0012
# --- ИЗМЕНЕНИЕ: Возвращена константа для экстренного выхода ---
COUNTER_ORDER_RATIO = 2.0
# --- КОНЕЦ ИЗМЕНЕНИЯ ---
PARAMS_CALM_MARKET = {"MIN_TOTAL_LIQUIDITY_USD": 1500000, "MIN_IMBALANCE_RATIO": 2.5, "LARGE_ORDER_USD": 250000}
PARAMS_ACTIVE_MARKET = {"MIN_TOTAL_LIQUIDITY_USD": 3000000, "MIN_IMBALANCE_RATIO": 3.0, "LARGE_ORDER_USD": 500000}
API_TIMEOUT = 10.0

async def monitor_active_trades(exchange, app, broadcast_func, state, save_state_func):
    active_signals = state.get('monitored_signals')
    if not active_signals: return
    signal = active_signals[0]
    
    # --- ИЗМЕНЕНИЕ: Возвращена переменная trigger_order_usd ---
    pair, entry_price, sl_price, tp_price, side, trigger_order_usd = (
        signal.get('Pair'), signal.get('Entry_Price'), signal.get('SL_Price'),
        signal.get('TP_Price'), signal.get('side'), signal.get('Trigger_Order_USD', 0)
    )
    # --- КОНЕЦ ИЗМЕНЕНИЯ ---
    
    if not all([pair, entry_price, sl_price, tp_price, side]):
        state['monitored_signals'] = []
        save_state_func()
        await broadcast_func(app, "⚠️ Ошибка в данных сделки, мониторинг остановлен.")
        return
    try:
        try:
            params = {'type': 'swap'}
            ohlcv = await asyncio.wait_for(exchange.fetch_ohlcv(pair, timeframe='1m', limit=2, params=params), timeout=API_TIMEOUT)
        except asyncio.TimeoutError:
            print(f"Monitor OHLCV Timeout for {pair}")
            return
        if not ohlcv or len(ohlcv) < 2: return
        exit_status, exit_price = None, None
        for candle in ohlcv:
            candle_high, candle_low = float(candle[2]), float(candle[3])
            if side == 'LONG':
                if candle_low <= sl_price: exit_status, exit_price = "SL_HIT", sl_price
                elif candle_high >= tp_price: exit_status, exit_price = "TP_HIT", tp_price
            elif side == 'SHORT':
                if candle_high >= sl_price: exit_status, exit_price = "SL_HIT", sl_price
                elif candle_low <= tp_price: exit_status, exit_price = "TP_HIT", tp_price
            if exit_status: break
        
        # --- ИЗМЕНЕНИЕ: Возвращена логика экстренного выхода ---
        if not exit_status and trigger_order_usd > 0:
            order_book = await exchange.fetch_order_book(pair, limit=25, params={'type': 'swap'})
            current_price = float(ohlcv[-1][4])
            if side == 'LONG' and any((p*a) > (trigger_order_usd * COUNTER_ORDER_RATIO) for p, a in order_book.get('asks', [])):
                exit_status, exit_price = "EMERGENCY_EXIT", current_price
            elif side == 'SHORT' and any((p*a) > (trigger_order_usd * COUNTER_ORDER_RATIO) for p, a in order_book.get('bids', [])):
                exit_status, exit_price = "EMERGENCY_EXIT", current_price
        # --- КОНЕЦ ИЗМЕНЕНИЯ ---
        
        if exit_status:
            leverage = signal.get('Leverage', 100)
            deposit = signal.get('Deposit', 50)
            pnl_percent_raw = ((exit_price - entry_price) / entry_price if entry_price != 0 else 0) * (-1 if side == 'SHORT' else 1)
            pnl_usd = deposit * leverage * pnl_percent_raw
            pnl_percent_display = pnl_percent_raw * 100 * leverage
            await update_trade_in_sheet(signal, exit_status, exit_price, pnl_usd, pnl_percent_display)
            # --- ИЗМЕНЕНИЕ: Возвращен эмодзи для EMERGENCY_EXIT ---
            emoji = "⚠️" if exit_status == "EMERGENCY_EXIT" else ("✅" if pnl_usd > 0 else "❌")
            # --- КОНЕЦ ИЗМЕНЕНИЯ ---
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                   f"<b>Инструмент:</b> <code>{pair}</code>\n"
                   f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent_display:+.2f}%)</b>")
            await broadcast_func(app, msg)
            state['monitored_signals'] = []
            save_state_func()
    except Exception as e:
        error_message = f"⚠️ <b>Критическая ошибка мониторинга!</b>\n<code>Ошибка: {e}</code>"
        print(f"CRITICAL MONITORING ERROR: {e}", exc_info=True)
        await broadcast_func(app, error_message)

async def get_cvd_analysis(exchange, pair, expected_side):
    try:
        trades = await exchange.fetch_trades(pair, limit=100, params={'type': 'swap'})
        if not trades: return {'confirmed': True, 'reason': "Нет данных о сделках"}
        buy_volume = sum(trade['cost'] for trade in trades if trade['side'] == 'buy')
        sell_volume = sum(trade['cost'] for trade in trades if trade['side'] == 'sell')
        cvd = buy_volume - sell_volume
        reason_text = f"Покупки: ${buy_volume/1000:,.0f}k | Продажи: ${sell_volume/1000:,.0f}k"
        if expected_side == "LONG" and cvd < 0: return {'confirmed': False, 'reason': reason_text}
        if expected_side == "SHORT" and cvd > 0: return {'confirmed': False, 'reason': reason_text}
        return {'confirmed': True, 'reason': reason_text}
    except Exception as e:
        print(f"CVD confirmation error: {e}")
        return {'confirmed': True, 'reason': f"Ошибка CVD: {e}"}

async def get_adx_value(exchange, pair, timeframe='15m', period=14):
    try:
        params = {'type': 'swap'}
        ohlcv = await exchange.fetch_ohlcv(pair, timeframe, limit=period * 3, params=params)
        if len(ohlcv) < period * 2: return None
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        plus_dm, minus_dm = df['high'].diff(), df['low'].diff()
        plus_dm[plus_dm < 0], minus_dm[minus_dm > 0] = 0, 0
        tr = pd.concat([df['high'] - df['low'], abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1))], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/period, adjust=False).mean()
        plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
        minus_di = 100 * (abs(minus_dm.ewm(alpha=1/period, adjust=False).mean()) / atr)
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
        return dx.ewm(alpha=1/period, adjust=False).mean().fillna(0).iloc[-1]
    except Exception as e:
        print(f"ADX calculation error: {e}")
        return None

async def scan_for_new_opportunities(exchange, app, broadcast_func, state, save_state_func):
    if 'cvd_cooldown_until' in state and time.time() < state['cvd_cooldown_until']:
        state['last_status_info'] = f"Кулдаун: {int(state['cvd_cooldown_until'] - time.time())} сек."
        return
    elif 'cvd_cooldown_until' in state:
        del state['cvd_cooldown_until']
        save_state_func()
    adx_value = await get_adx_value(exchange, PAIR_TO_SCAN)
    if adx_value is None:
        state['last_status_info'] = "Ошибка расчета ADX."
        return
    params = PARAMS_ACTIVE_MARKET if adx_value >= 25 else PARAMS_CALM_MARKET
    market_mode = "Активный" if adx_value >= 25 else "Спокойный"
    state['last_status_info'] = f"Поиск | Режим: {market_mode} (ADX: {adx_value:.1f})"
    min_total_liquidity, min_imbalance_ratio, large_order_usd = params.values()
    order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=50, params={'type': 'swap'})
    large_bids = sorted([{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('bids', []) if p and a and (p*a > large_order_usd)], key=lambda x: x['value_usd'], reverse=True)
    large_asks = sorted([{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('asks', []) if p and a and (p*a > large_order_usd)], key=lambda x: x['value_usd'], reverse=True)
    total_bids_usd = sum(b['value_usd'] for b in large_bids[:TOP_N_ORDERS_TO_ANALYZE])
    total_asks_usd = sum(a['value_usd'] for a in large_asks[:TOP_N_ORDERS_TO_ANALYZE])
    if (total_bids_usd + total_asks_usd) < min_total_liquidity: return
    imbalance_ratio = (max(total_bids_usd, total_asks_usd) / min(total_bids_usd, total_asks_usd) if total_bids_usd > 0 and total_asks_usd > 0 else float('inf'))
    if imbalance_ratio < min_imbalance_ratio: return
    
    dominant_side_is_bids = total_bids_usd > total_asks_usd
    dominant_side = "ПОКУПАТЕЛЕЙ" if dominant_side_is_bids else "ПРОДАВЦОВ"
    largest_order = (large_bids[0] if large_bids else None) if dominant_side_is_bids else (large_asks[0] if large_asks else None)
    expected_direction = "ВВЕРХ" if dominant_side_is_bids else "ВНИЗ"
    
    signal_msg = (f"🔥 <b>АЛГО-СИГНАЛ! (Режим: {market_mode}, ADX: {adx_value:.1f})</b>\n"
                  f"Сильный перевес на стороне {dominant_side} (дисбаланс {imbalance_ratio:.1f}x).\n")
    if largest_order:
        signal_msg += f"Ключевой ордер: ${largest_order['value_usd']/1e6:.2f} млн на уровне {largest_order['price']}.\n"
    signal_msg += f"Ожидание: вероятно движение {expected_direction}."
    
    await broadcast_func(app, signal_msg)
    
    side = "LONG" if dominant_side_is_bids else "SHORT"
    cvd_analysis = await get_cvd_analysis(exchange, PAIR_TO_SCAN, side)
    if not cvd_analysis['confirmed']:
        await broadcast_func(app, f"⚠️ <b>Сигнал отфильтрован по CVD!</b>\n<i>({cvd_analysis['reason']})</i>\n<b>Кулдаун 3 мин.</b>")
        state['cvd_cooldown_until'] = time.time() + 180
        save_state_func()
        return
    else:
        await broadcast_func(app, f"✅ <b>Сигнал подтвержден по CVD.</b>\n<i>({cvd_analysis['reason']})</i>")
    try:
        current_price = order_book['asks'][0][0] if side == "LONG" else order_book['bids'][0][0]
    except (IndexError, KeyError) as e:
        await broadcast_func(app, f"⚠️ Не удалось извлечь цену из стакана ({e}). Сделка пропущена.")
        return

    sl_delta = current_price * SL_PERCENT
    tp_delta = current_price * TP_PERCENT

    if side == "LONG":
        sl_price = current_price - sl_delta
        tp_price = current_price + tp_delta
    else: # SHORT
        sl_price = current_price + sl_delta
        tp_price = current_price - tp_delta

    # --- ИЗМЕНЕНИЕ: Возвращен столбец Trigger_Order_USD ---
    decision = {
        "Signal_ID": f"signal_{int(time.time() * 1000)}",
        "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        "Pair": PAIR_TO_SCAN,
        "Algorithm_Type": f"Imbalance + CVD (ADX: {adx_value:.1f})",
        "Strategy_Idea": f"Дисбаланс {imbalance_ratio:.1f}x в пользу {dominant_side}",
        "Entry_Price": current_price,
        "SL_Price": sl_price,
        "TP_Price": tp_price,
        "side": side,
        "Deposit": state.get('deposit', 50),
        "Leverage": state.get('leverage', 100),
        "Trigger_Order_USD": largest_order['value_usd'] if largest_order else 0
    }
    # --- КОНЕЦ ИЗМЕНЕНИЯ ---
    
    rr_ratio = TP_PERCENT / SL_PERCENT if SL_PERCENT > 0 else 0
    msg = (f"<b>ВХОД В СДЕЛКУ</b>\n\n<b>Тип:</b> Pure Quant Entry (Fixed %)\n"
           f"<b>Депозит:</b> ${decision['Deposit']} | <b>Плечо:</b> x{decision['Leverage']}\n"
           f"<b>Рассчитанный план (RR ~{rr_ratio:.1f}:1):</b>\n"
           f" - Вход (<b>{side}</b>): <code>{current_price:.4f}</code>\n"
           f" - SL: <code>{sl_price:.4f}</code>\n"
           f" - TP: <code>{tp_price:.4f}</code>")
    await broadcast_func(app, msg)
    state['monitored_signals'].append(decision)
    save_state_func()
    await broadcast_func(app, "✅ Сделка взята на мониторинг.")
    if await log_trade_to_sheet(decision):
        await broadcast_func(app, "✅ ...успешно залогирована в Google Sheets.")
    else:
        await broadcast_func(app, "⚠️ Не удалось сохранить сделку в Google Sheets.")

async def scanner_main_loop(app, broadcast_func, state, save_state_func):
    bot_version = "25.5"
    app.bot_version = bot_version
    print(f"Main Engine loop started (v{bot_version}).")
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}})
    scan_interval = 15
    while state.get("bot_on", True):
        try:
            await monitor_active_trades(exchange, app, broadcast_func, state, save_state_func)
            if not state.get('monitored_signals'):
                await scan_for_new_opportunities(exchange, app, broadcast_func, state, save_state_func)
            await asyncio.sleep(scan_interval)
        except asyncio.CancelledError:
            print("Main Engine loop cancelled.")
            break
        except Exception as e:
            print(f"CRITICAL Error in Main Engine loop: {e}", exc_info=True)
            await broadcast_func(app, f"Критическая ошибка в главном цикле: {e}")
            await asyncio.sleep(60)
    print("Main Engine loop stopped.")
    await exchange.close()
