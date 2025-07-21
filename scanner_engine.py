# scanner_engine.py
# ============================================================================
# v37.0 - DIRECTIONAL ADX FILTER + TAKE PROFIT
# - Добавлен фильтр направления тренда (+DI > -DI для LONG, -DI > +DI для SHORT).
# - Введен Take Profit с RR=1.5.
# - Скорректированы пороги: ADX_TREND_THRESHOLD=20, ADX_FLAT_THRESHOLD=15.
# - Увеличен dominance_lost_counter до 3.
# - MIN_IMBALANCE_RATIO динамический: 3.0 если ADX <20.
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
PAIR_TO_SCAN = 'BTC/USDT'
TIMEFRAME = '5m'  # Таймфрейм для анализа тренда
LARGE_ORDER_USD = 150000
TOP_N_ORDERS_TO_ANALYZE = 20
SCAN_INTERVAL = 5
SL_BUFFER_PERCENT = 0.0005
MIN_SL_DISTANCE_PCT = 0.0008

# --- Параметры стратегии ---
MIN_IMBALANCE_RATIO = 2.0  # Базовый; динамически повышается во флэте
AGGRESSION_TIMEFRAME_SEC = 30
AGGRESSION_RATIO = 1.5
RISK_REWARD_RATIO = 1.5  # Новый: RR для TP
DOMINANCE_LOST_MAX_COUNTER = 3  # Новый: Увеличено для подтверждения потери доминации

# --- Параметры режимного фильтра ---
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 20  # Изменено: Захватываем серую зону
ADX_FLAT_THRESHOLD = 15   # Изменено: Избегаем ультра-флэта

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
    """Рассчитывает ADX, +DI (DMP), -DI (DMN) по данным свечей."""
    if not ohlcv or len(ohlcv) < ADX_PERIOD:
        return None, None, None
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df.ta.adx(length=ADX_PERIOD, append=True)
    indicators = df[['ADX_14', 'DMP_14', 'DMN_14']].iloc[-1]  # Изменено: Возвращаем ADX, +DI, -DI
    return indicators['ADX_14'], indicators['DMP_14'], indicators['DMN_14']

# === Логика сканирования (ИЗМЕНЕНА) =============================================
async def scan_for_new_opportunities(exchange, app: Application, broadcast_func, adx, pdi, mdi):
    bot_data = app.bot_data
    status_code, status_message = None, None
    try:
        # 1. ГЛАВНЫЙ ФИЛЬТР: ПРОВЕРКА РЕЖИМА РЫНКА
        if adx is None:
            status_code, status_message = "WAIT_ADX", "Ожидание данных для расчета ADX..."
        elif adx < ADX_FLAT_THRESHOLD:
            status_code, status_message = "MARKET_IS_FLAT", f"ADX ({adx:.1f}) < {ADX_FLAT_THRESHOLD}. Рынок во флэте, торговля на паузе."
        elif adx < ADX_TREND_THRESHOLD:
            status_code, status_message = "MARKET_IS_WEAK", f"ADX ({adx:.1f}) в 'серой зоне' ({ADX_FLAT_THRESHOLD}-{ADX_TREND_THRESHOLD}). Жду сильного тренда."
        else:
            # РЫНОК В ТРЕНДЕ - НАЧИНАЕМ ПОИСК СИГНАЛА
            status_code, status_message = "SCANNING_IN_TREND", f"ADX ({adx:.1f}) > {ADX_TREND_THRESHOLD}. Поиск сигнала в тренде..."
            order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=100, params={'type': 'swap'})
            imbalance_ratio, large_bids, large_asks, top_bids_usd, top_asks_usd = get_imbalance_and_walls(order_book)

            # Динамический MIN_IMBALANCE_RATIO: Повышаем во флэте (если ADX близко к flat)
            min_imbalance = 3.0 if adx < 20 else MIN_IMBALANCE_RATIO

            if imbalance_ratio >= min_imbalance:
                dominant_side_is_bids = top_bids_usd > top_asks_usd
                side_to_trade = "LONG" if dominant_side_is_bids else "SHORT"
                
                # Новый: Фильтр направления тренда
                trend_dir = "LONG" if pdi > mdi else "SHORT" if mdi > pdi else None
                if trend_dir is None or side_to_trade != trend_dir:
                    status_message += f" Сигнал пропущен: направление ({side_to_trade}) не совпадает с трендом ({trend_dir})."
                    return  # Пропускаем, если не совпадает
                
                now_ms, since = exchange.milliseconds(), exchange.milliseconds() - AGGRESSION_TIMEFRAME_SEC * 1000
                trades = await exchange.fetch_trades(PAIR_TO_SCAN, since=since, limit=100, params={'type': 'swap', 'until': now_ms})
                
                if trades:
                    buy_volume = sum(t['cost'] for t in trades if t['side'] == 'buy')
                    sell_volume = sum(t['cost'] for t in trades if t['side'] == 'sell')
                    aggression_side = "LONG" if buy_volume > sell_volume * AGGRESSION_RATIO else "SHORT" if sell_volume > buy_volume * AGGRESSION_RATIO else None

                    if aggression_side == side_to_trade:
                        entry_price = trades[-1]['price']
                        support_wall, resistance_wall = large_bids[0], large_asks[0]
                        sl_price = support_wall['price'] * (1 - SL_BUFFER_PERCENT) if side_to_trade == "LONG" else resistance_wall['price'] * (1 + SL_BUFFER_PERCENT)
                        
                        if abs(entry_price - sl_price) / entry_price >= MIN_SL_DISTANCE_PCT:
                            # Новый: Расчет TP
                            sl_distance = abs(entry_price - sl_price)
                            if side_to_trade == "LONG":
                                tp_price = entry_price + sl_distance * RISK_REWARD_RATIO
                            else:
                                tp_price = entry_price - sl_distance * RISK_REWARD_RATIO
                            
                            idea = f"ADX {adx:.1f} (Dir: {trend_dir}). Дисбаланс {imbalance_ratio:.1f}x + Агрессия {side_to_trade}"
                            decision = {"Signal_ID": f"signal_{int(time.time() * 1000)}", 
                                        "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                                        "Pair": PAIR_TO_SCAN, "Algorithm_Type": "Directional ADX Imbalance", 
                                        "Strategy_Idea": idea, "Entry_Price": entry_price, "SL_Price": sl_price, 
                                        "TP_Price": tp_price, "side": side_to_trade, "Deposit": bot_data.get('deposit', 50), 
                                        "Leverage": bot_data.get('leverage', 100), "dominance_lost_counter": 0}
                            msg = f"🔥 <b>ВХОД В СДЕЛКУ ({side_to_trade})</b>\n\n<b>Тип:</b> <code>{idea}</code>\n<b>Вход:</b> <code>{entry_price:.2f}</code> | <b>SL:</b> <code>{sl_price:.2f}</code> | <b>TP:</b> <code>{tp_price:.2f}</code>"
                            await broadcast_func(app, msg)
                            await log_trade_to_sheet(decision)
                            bot_data['monitored_signals'].append(decision)
                            save_state(app)

    except Exception as e:
        status_code, status_message = "SCANNER_ERROR", f"КРИТИЧЕСКАЯ ОШИБКА: {e}"
        log.error(status_message, exc_info=True)
    
    # Логика отправки диагностики
    last_code = bot_data.get('last_debug_code', '')
    if status_code and status_code != last_code:
        bot_data['last_debug_code'] = status_code
        if bot_data.get('debug_mode_on', False):
            await broadcast_func(app, f"<code>{status_message}</code>")

# === Логика мониторинга (ИЗМЕНЕНА: Добавлен TP) ==============================
async def monitor_active_trades(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    if not bot_data.get('monitored_signals'): return
    signal = bot_data['monitored_signals'][0]
    pair, entry_price, sl_price, tp_price, side = (signal['Pair'], signal['Entry_Price'], signal['SL_Price'], signal.get('TP_Price'), signal['side'])  # Новый: tp_price
    try:
        order_book = await exchange.fetch_order_book(pair, limit=100, params={'type': 'swap'})
        if not (order_book.get('bids') and order_book['bids'][0] and order_book.get('asks') and order_book['asks'][0]): return
        best_bid, best_ask = order_book['bids'][0][0], order_book['asks'][0][0]
        last_price = (best_bid + best_ask) / 2
        _, _, _, top_bids_usd, top_asks_usd = get_imbalance_and_walls(order_book)
        exit_status, exit_price, reason = None, None, None
        
        # Новый: Проверка TP перед SL
        if (side == 'LONG' and last_price >= tp_price) or (side == 'SHORT' and last_price <= tp_price):
            exit_status, exit_price, reason = "TP_HIT", tp_price if side == 'LONG' else tp_price, "Take Profit достигнут"
        
        if not exit_status:
            if (side == 'LONG' and last_price <= sl_price) or (side == 'SHORT' and last_price >= sl_price):
                exit_status, exit_price, reason = "SL_HIT", sl_price, "Аварийный стоп-лосс"
        
        if not exit_status:
            dominance_is_lost = (side == 'LONG' and top_bids_usd <= top_asks_usd) or (side == 'SHORT' and top_asks_usd <= top_bids_usd)
            if dominance_is_lost:
                signal['dominance_lost_counter'] = signal.get('dominance_lost_counter', 0) + 1
                if signal['dominance_lost_counter'] >= DOMINANCE_LOST_MAX_COUNTER:  # Изменено: >=3
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

# === Главный цикл (ИЗМЕНЕН: Передаем pdi, mdi) ============================================
async def scanner_main_loop(app: Application, broadcast_func):
    bot_version = getattr(app, 'bot_version', 'N/A')
    log.info(f"Main Engine loop starting (v{bot_version})...")
    exchange = None
    adx, pdi, mdi = None, None, None  # Новый: pdi, mdi
    last_adx_update_time = 0

    try:
        exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
        await exchange.load_markets()
        log.info("Exchange connection and markets loaded.")

        while app.bot_data.get("bot_on", False):
            try:
                # Обновляем ADX раз в минуту, чтобы не нагружать API
                if time.time() - last_adx_update_time > 60:
                    ohlcv = await exchange.fetch_ohlcv(PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=50)
                    adx, pdi, mdi = calculate_indicators(ohlcv)  # Изменено: Получаем adx, pdi, mdi
                    last_adx_update_time = time.time()
                
                if not app.bot_data.get('monitored_signals'):
                    # Передаем adx, pdi, mdi в сканер
                    await scan_for_new_opportunities(exchange, app, broadcast_func, adx, pdi, mdi)
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
