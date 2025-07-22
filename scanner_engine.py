# scanner_engine.py
# ============================================================================
# v37.5 - SOL/USDT + ПРИСТРЕЛОЧНЫЕ НАСТРОЙКИ
# - PAIR_TO_SCAN = 'SOL/USDT' (волатильный).
# - Убедились в futures (swap).
# ============================================================================
import asyncio
import time
import logging
from datetime import datetime, timezone
import pandas as pd
import pandas_ta as ta

import ccxt.async_support as ccxt
from telegram.ext import Application

from trade_executor import log_trade_to_sheet, update_trade_in_sheet
from state_utils import save_state

log = logging.getLogger("bot")

# === Конфигурация сканера ==================================================
PAIR_TO_SCAN = 'SOL/USDT'  # Смена на SOL/USDT
TIMEFRAME = '5m'  # Таймфрейм для анализа тренда
LARGE_ORDER_USD = 150000
TOP_N_ORDERS_TO_ANALYZE = 20
SCAN_INTERVAL = 5
SL_BUFFER_PERCENT = 0.0005
MIN_SL_DISTANCE_PCT = 0.0008

# --- Параметры стратегии (пристрелочные) ---
MIN_IMBALANCE_RATIO = 1.5  # Уменьшено для теста
AGGRESSION_TIMEFRAME_SEC = 30
AGGRESSION_RATIO = 1.5
TP_ATR_MULTIPLIER = 1.5
DOMINANCE_LOST_MAX_COUNTER = 3
DI_DIFF_THRESHOLD = 5.0

# --- Параметры режимного фильтра ---
ADX_PERIOD = 14
ATR_PERIOD = 14
ADX_TREND_THRESHOLD = 20
ADX_FLAT_THRESHOLD = 15

# === Функции-помощники =====================================================
def get_imbalance_and_walls(order_book):
    bids, asks = order_book.get('bids', []), order_book.get('asks', [])
    if not bids or not asks: return 1.0, None, None, 0, 0
    large_bids, large_asks = [], []
    for bid in bids:
        if len(bid) == 2:
            price, amount = bid
            if price * amount > LARGE_ORDER_USD:
                large_bids.append({'price': price, 'value_usd': round(price * amount)})
    for ask in asks:
        if len(ask) == 2:
            price, amount = ask
            if price * amount > LARGE_ORDER_USD:
                large_asks.append({'price': price, 'value_usd': round(price * amount)})
    if not large_bids or not large_asks: return 1.0, None, None, 0, 0
    top_bids_usd = sum(b['value_usd'] for b in large_bids[:TOP_N_ORDERS_TO_ANALYZE])
    top_asks_usd = sum(a['value_usd'] for a in large_asks[:TOP_N_ORDERS_TO_ANALYZE])
    imbalance_ratio = (max(top_bids_usd, top_asks_usd) / min(top_bids_usd, top_asks_usd)) if top_bids_usd > 0 and top_asks_usd > 0 else float('inf')
    return imbalance_ratio, large_bids, large_asks, top_bids_usd, top_asks_usd

def calculate_indicators(ohlcv):
    """Рассчитывает ADX, +DI (DMP), -DI (DMN), ATR по данным свечей."""
    if not ohlcv or len(ohlcv) < max(ADX_PERIOD, ATR_PERIOD):
        return None, None, None, None
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df.ta.adx(length=ADX_PERIOD, append=True)
    df.ta.atr(length=ATR_PERIOD, append=True)
    indicators = df[['ADX_14', 'DMP_14', 'DMN_14', 'ATRr_14']].iloc[-1]
    return indicators['ADX_14'], indicators['DMP_14'], indicators['DMN_14'], indicators['ATRr_14']

# === Логика сканирования (без изменений, но с SOL) =============================================
async def scan_for_new_opportunities(exchange, app: Application, broadcast_func, adx, pdi, mdi, atr):
    bot_data = app.bot_data
    status_code, status_message = None, None
    extended_message = "" 
    try:
        if adx is None:
            status_code, status_message = "WAIT_ADX", "Ожидание данных для расчета ADX..."
        else:
            extended_message = f"PDI: {pdi:.1f}, MDI: {mdi:.1f}. ATR: {atr:.2f}. "
            if adx < ADX_FLAT_THRESHOLD:
                status_code, status_message = "MARKET_IS_FLAT", f"ADX ({adx:.1f}) < {ADX_FLAT_THRESHOLD}. Рынок во флэте, торговля на паузе."
            elif adx < ADX_TREND_THRESHOLD:
                status_code, status_message = "MARKET_IS_WEAK", f"ADX ({adx:.1f}) в 'серой зоне' ({ADX_FLAT_THRESHOLD}-{ADX_TREND_THRESHOLD}). Жду сильного тренда."
            else:
                status_code, status_message = "SCANNING_IN_TREND", f"ADX ({adx:.1f}) > {ADX_TREND_THRESHOLD}. Поиск сигнала в тренде..."
                order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=100, params={'type': 'swap'})
                imbalance_ratio, large_bids, large_asks, top_bids_usd, top_asks_usd = get_imbalance_and_walls(order_book)

                min_imbalance = 3.0 if adx < 20 else MIN_IMBALANCE_RATIO
                extended_message += f"Imbalance ratio: {imbalance_ratio:.1f} (min: {min_imbalance:.1f}). "

                if imbalance_ratio < min_imbalance:
                    extended_message += "Пропущен по imbalance."
                else:
                    dominant_side_is_bids = top_bids_usd > top_asks_usd
                    side_to_trade = "LONG" if dominant_side_is_bids else "SHORT"
                    trend_dir = "LONG" if pdi > mdi else "SHORT" if mdi > pdi else None
                    di_diff = abs(pdi - mdi)
                    extended_message += f"Side: {side_to_trade}, Trend dir: {trend_dir}, DI diff: {di_diff:.1f}. "
                    if trend_dir is None or (di_diff < DI_DIFF_THRESHOLD) or side_to_trade == trend_dir:
                        pass
                    else:
                        extended_message += "Пропущен по тренду (strong diff)."
                        return
                    now_ms, since = exchange.milliseconds(), exchange.milliseconds() - AGGRESSION_TIMEFRAME_SEC * 1000
                    trades = await exchange.fetch_trades(PAIR_TO_SCAN, since=since, limit=100, params={'type': 'swap', 'until': now_ms})
                    
                    if trades:
                        buy_volume = sum(t['cost'] for t in trades if t['side'] == 'buy')
                        sell_volume = sum(t['cost'] for t in trades if t['side'] == 'sell')
                        aggression_side = "LONG" if buy_volume > sell_volume * AGGRESSION_RATIO else "SHORT" if sell_volume > buy_volume * AGGRESSION_RATIO else None
                        extended_message += f"Aggression side: {aggression_side}. "
                        if aggression_side != side_to_trade:
                            extended_message += "Пропущен по aggression."
                        else:
                            entry_price = trades[-1]['price']
                            support_wall, resistance_wall = large_bids[0], large_asks[0]
                            sl_price = support_wall['price'] * (1 - SL_BUFFER_PERCENT) if side_to_trade == "LONG" else resistance_wall['price'] * (1 + SL_BUFFER_PERCENT)
                            
                            if abs(entry_price - sl_price) / entry_price < MIN_SL_DISTANCE_PCT:
                                extended_message += f"SL distance: {abs(entry_price - sl_price) / entry_price:.4f} < {MIN_SL_DISTANCE_PCT}. Пропущен по SL."
                            else:
                                tp_price = entry_price + atr * TP_ATR_MULTIPLIER if side_to_trade == "LONG" else entry_price - atr * TP_ATR_MULTIPLIER
                                
                                idea = f"ADX {adx:.1f} (Dir: {trend_dir}). Дисбаланс {imbalance_ratio:.1f}x + Агрессия {side_to_trade}"
                                decision = {"Signal_ID": f"signal_{int(time.time() * 1000)}", 
                                            "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                                            "Pair": PAIR_TO_SCAN, "Algorithm_Type": "Directional ADX Imbalance", 
                                            "Strategy_Idea": idea, "Entry_Price": entry_price, "SL_Price": sl_price, 
                                            "TP_Price": tp_price, "side": side_to_trade, "Deposit": bot_data.get('deposit', 50), 
                                            "Leverage": bot_data.get('leverage', 100), "dominance_lost_counter": 0,
                                            "ADX": adx, "PDI": pdi, "MDI": mdi, 
                                            "Imbalance_Ratio": imbalance_ratio, "Aggression_Side": aggression_side, 
                                            "Trigger_Order_USD": max(top_bids_usd, top_asks_usd),
                                            "ATR": atr
                                            }
                                msg = f"🔥 <b>ВХОД В СДЕЛКУ ({side_to_trade})</b>\n\n<b>Тип:</b> <code>{idea}</code>\n<b>Вход:</b> <code>{entry_price:.2f}</code> | <b>SL:</b> <code>{sl_price:.2f}</code> | <b>TP:</b> <code>{tp_price:.2f}</code>"
                                await broadcast_func(app, msg)
                                await log_trade_to_sheet(decision)
                                bot_data['monitored_signals'].append(decision)
                                save_state(app)
                                extended_message += "Сигнал найден и отправлен!"

    except Exception as e:
        status_code, status_message = "SCANNER_ERROR", f"КРИТИЧЕСКАЯ ОШИБКА: {e}"
        extended_message = ""
        log.error(status_message, exc_info=True)
    
    # Логика отправки диагностики
    if bot_data.get('debug_mode_on', False):
        full_msg = f"<code>{status_message} {extended_message}</code>"
        await broadcast_func(app, full_msg)
    else:
        last_code = bot_data.get('last_debug_code', '')
        if status_code and status_code != last_code:
            bot_data['last_debug_code'] = status_code
            await broadcast_func(app, f"<code>{status_message}</code>")

# === Логика мониторинга (без изменений) ==============================
async def monitor_active_trades(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    if not bot_data.get('monitored_signals'): return
    signal = bot_data['monitored_signals'][0]
    pair, entry_price, sl_price, tp_price, side = (signal['Pair'], signal['Entry_Price'], signal['SL_Price'], signal.get('TP_Price'), signal['side'])
    try:
        order_book = await exchange.fetch_order_book(pair, limit=100, params={'type': 'swap'})
        if not (order_book.get('bids') and order_book['bids'][0] and order_book.get('asks') and order_book['asks'][0]): return
        best_bid, best_ask = order_book['bids'][0][0], order_book['asks'][0][0]
        last_price = (best_bid + best_ask) / 2
        _, _, _, top_bids_usd, top_asks_usd = get_imbalance_and_walls(order_book)
        exit_status, exit_price, reason = None, None, None
        
        if (side == 'LONG' and last_price >= tp_price) or (side == 'SHORT' and last_price <= tp_price):
            exit_status, exit_price, reason = "TP_HIT", tp_price if side == 'LONG' else tp_price, "Take Profit достигнут"
        
        if not exit_status:
            if (side == 'LONG' and last_price <= sl_price) or (side == 'SHORT' and last_price >= sl_price):
                exit_status, exit_price, reason = "SL_HIT", sl_price, "Аварийный стоп-лосс"
        
        if not exit_status:
            dominance_is_lost = (side == 'LONG' and top_bids_usd <= top_asks_usd) or (side == 'SHORT' and top_asks_usd <= top_bids_usd)
            if dominance_is_lost:
                signal['dominance_lost_counter'] = signal.get('dominance_lost_counter', 0) + 1
                if signal['dominance_lost_counter'] >= DOMINANCE_LOST_MAX_COUNTER:
                    reason_text = "Потеря доминации покупателей" if side == 'LONG' else "Потеря доминации продавцов"
                    exit_status, exit_price, reason = "DOMINANCE_LOST", last_price, f"{reason_text} (подтверждено)"
            else:
                signal['dominance_lost_counter'] = 0
        
        if exit_status:
            pnl_percent_raw = ((exit_price - entry_price) / entry_price) * (-1 if side == 'SHORT' else 1)
            pnl_usd = signal['Deposit'] * signal['Leverage'] * pnl_percent_raw
            pnl_percent_display = pnl_percent_raw * 100 * signal['Leverage']
            await update_trade_in_sheet(signal, exit_status, exit_price, pnl_usd, pnl_percent_display, reason=reason)
            emoji = "✅" if pnl_usd > 0 else "❌"
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n<b>Причина:</b> {reason}\n<b>Результат: ${pnl_usd:+.2f} ({pnl_percent_display:+.2f}%)</b>")
            await broadcast_func(app, msg)
            bot_data['monitored_signals'] = []
            save_state(app)
    except Exception as e:
        log.error(f"CRITICAL MONITORING ERROR: {e}", exc_info=True)
        await broadcast_func(app, f"⚠️ <b>Критическая ошибка мониторинга!</b>\n<code>Ошибка: {e}</code>")

# === Главный цикл (без изменений) ============================================
async def scanner_main_loop(app: Application, broadcast_func):
    bot_version = getattr(app, 'bot_version', 'N/A')
    log.info(f"Main Engine loop starting (v{bot_version})...")
    exchange = None
    adx, pdi, mdi, atr = None, None, None, None
    last_adx_update_time = 0

    try:
        exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
        await exchange.load_markets()
        log.info("Exchange connection and markets loaded.")

        while app.bot_data.get("bot_on", False):
            try:
                if time.time() - last_adx_update_time > 60:
                    ohlcv = await exchange.fetch_ohlcv(PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=50, params={'type': 'swap'})  # Убедились в swap для OHLCV
                    adx, pdi, mdi, atr = calculate_indicators(ohlcv)
                    last_adx_update_time = time.time()
                
                if not app.bot_data.get('monitored_signals'):
                    await scan_for_new_opportunities(exchange, app, broadcast_func, adx, pdi, mdi, atr)
                else:
                    await monitor_active_trades(exchange, app, broadcast_func)

                await asyncio.sleep(SCAN_INTERVAL)
            except Exception as e:
                log.critical(f"CRITICAL Error in loop iteration: {e}", exc_info=True)
                await broadcast_func(app, f"Критическая ошибка в цикле: {e}")
                await asyncio.sleep(20)
    except Exception as e:
        log.critical(f"CRITICAL STARTUP ERROR: {e}", exc_info=True)
        await broadcast_func(app, f"<b>КРИТИЧЕСКАЯ ОШИБКА ЗАПУСКА!</b>\n<code>Ошибка: {e}</code>")
    finally:
        if exchange:
            await exchange.close()
        log.info("Main Engine loop stopped.")
