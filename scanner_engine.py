# scanner_engine.py
# ============================================================================
# v38.0 - УПРОЩЕННАЯ СТРАТЕГИЯ НА ОСНОВЕ PDI, MDI И ADX
# - Вход: |PDI - MDI| > DI_DIFF_THRESHOLD, ADX > ADX_TREND_THRESHOLD, ATR > ATR_MIN
# - Подтверждение на CONFIRM_CANDLES свечах
# - Закрытие: Кроссовер DI или |PDI - MDI| < DI_CLOSE_THRESHOLD, ADX < ADX_FLAT_THRESHOLD или ADX упал на ADX_DROP_DELTA от входа
# - SL/TP на основе ATR
# - Убраны дисбаланс, агрессия, стены ордеров
# ============================================================================
import asyncio
import time
import logging
from datetime import datetime, timezone
import pandas as pd
import pandas_ta as ta

import ccxt.async_support as ccxt
from telegram.ext import Application

from trade_executor import log_trade_to_sheet, update_trade_in_sheet, log_debug_data
from state_utils import save_state

log = logging.getLogger("bot")

# === Конфигурация сканера ==================================================
PAIR_TO_SCAN = 'SOL/USDT'
TIMEFRAME = '5m'
SCAN_INTERVAL = 5
SL_BUFFER_PERCENT = 0.0005
MIN_SL_DISTANCE_PCT = 0.005  # 0.5%

# --- Параметры стратегии ---
DI_DIFF_THRESHOLD = 5.0
DI_CLOSE_THRESHOLD = 2.0  # Для закрытия при сужении разницы
ADX_PERIOD = 14
ATR_PERIOD = 14
ADX_TREND_THRESHOLD = 20
ADX_FLAT_THRESHOLD = 15
ADX_DROP_DELTA = 5  # Закрыть, если ADX упал на это значение от входа
ATR_MIN = 1.0
TP_ATR_MULTIPLIER = 2.0
CONFIRM_CANDLES = 2  # Подтверждение сигнала на N свечах

# === Функции-помощники =====================================================
def calculate_indicators(ohlcv):
    """Рассчитывает ADX, +DI (DMP), -DI (DMN), ATR по данным свечей. Возвращает последние и предыдущие для подтверждения."""
    if not ohlcv or len(ohlcv) < max(ADX_PERIOD, ATR_PERIOD) + CONFIRM_CANDLES - 1:
        return None, None, None, None, None, None, None, None
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df.ta.adx(length=ADX_PERIOD, append=True)
    df.ta.atr(length=ATR_PERIOD, append=True)
    last = df.iloc[-1]
    prev = df.iloc[-CONFIRM_CANDLES] if len(df) >= CONFIRM_CANDLES else None
    return (
        last['ADX_14'], last['DMP_14'], last['DMN_14'], last['ATRr_14'],
        prev['ADX_14'] if prev is not None else None,
        prev['DMP_14'] if prev is not None else None,
        prev['DMN_14'] if prev is not None else None,
        prev['ATRr_14'] if prev is not None else None
    )

# === Логика сканирования =============================================
async def scan_for_new_opportunities(exchange, app: Application, broadcast_func, ohlcv):
    bot_data = app.bot_data
    status_code, status_message = None, None
    extended_message = "" 
    reason_prop = ""
    side = ""
    di_diff = 0
    atr = 0
    adx = None
    try:
        adx, pdi, mdi, atr, prev_adx, prev_pdi, prev_mdi, prev_atr = calculate_indicators(ohlcv)
        if adx is None:
            status_code, status_message = "WAIT_ADX", "Ожидание данных для расчета ADX..."
            reason_prop = "WAIT_ADX"
            return

        extended_message = f"ADX: {adx:.1f}, PDI: {pdi:.1f}, MDI: {mdi:.1f}, ATR: {atr:.2f}. "
        
        if adx < ADX_FLAT_THRESHOLD or atr < ATR_MIN:
            status_code, status_message = "MARKET_IS_FLAT", f"ADX ({adx:.1f}) < {ADX_FLAT_THRESHOLD} или ATR {atr:.2f} < {ATR_MIN}. Рынок во флэте или низкая волатильность."
            reason_prop = "FLAT_OR_LOW_ATR"
            return
        
        elif adx < ADX_TREND_THRESHOLD:
            status_code, status_message = "MARKET_IS_WEAK", f"ADX ({adx:.1f}) в 'серой зоне' ({ADX_FLAT_THRESHOLD}-{ADX_TREND_THRESHOLD}). Жду сильного тренда."
            reason_prop = "WEAK"
            return
        
        else:
            status_code, status_message = "SCANNING_IN_TREND", f"ADX ({adx:.1f}) > {ADX_TREND_THRESHOLD}. Поиск сигнала в тренде..."
            
            di_diff = abs(pdi - mdi)
            side = "LONG" if pdi > mdi else "SHORT"
            
            # Подтверждение на CONFIRM_CANDLES свечах
            if prev_pdi is None:
                extended_message += "Недостаточно данных для подтверждения."
                reason_prop = "NO_CONFIRM_DATA"
                return
            
            prev_di_diff = abs(prev_pdi - prev_mdi)
            prev_side = "LONG" if prev_pdi > prev_mdi else "SHORT"
            if di_diff <= DI_DIFF_THRESHOLD or prev_di_diff <= DI_DIFF_THRESHOLD or side != prev_side:
                extended_message += "Сигнал не подтвержден на последовательных свечах."
                reason_prop = "NO_CONFIRM"
                return
            
            # Получение entry_price (из последней цены)
            trades = await exchange.fetch_trades(PAIR_TO_SCAN, limit=1, params={'type': 'swap'})
            if not trades:
                return
            entry_price = trades[0]['price']
            
            # SL и TP
            sl_distance = atr  # 1x ATR
            sl_price = entry_price - sl_distance * (1 + SL_BUFFER_PERCENT) if side == "LONG" else entry_price + sl_distance * (1 + SL_BUFFER_PERCENT)
            if abs(entry_price - sl_price) / entry_price < MIN_SL_DISTANCE_PCT:
                extended_message += f"SL distance: {abs(entry_price - sl_price) / entry_price:.4f} < {MIN_SL_DISTANCE_PCT}. Пропущен."
                reason_prop = "SMALL_SL"
                return
            
            tp_price = entry_price + atr * TP_ATR_MULTIPLIER if side == "LONG" else entry_price - atr * TP_ATR_MULTIPLIER
            
            idea = f"ADX {adx:.1f} (Dir: {side}). DI diff: {di_diff:.1f} (подтверждено)"
            decision = {"Signal_ID": f"signal_{int(time.time() * 1000)}", 
                        "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                        "Pair": PAIR_TO_SCAN, "Algorithm_Type": "Directional ADX DMI", 
                        "Strategy_Idea": idea, "Entry_Price": entry_price, "SL_Price": sl_price, 
                        "TP_Price": tp_price, "side": side, "Deposit": bot_data.get('deposit', 50), 
                        "Leverage": bot_data.get('leverage', 100),
                        "ADX": adx, "PDI": pdi, "MDI": mdi, "ATR": atr, "entry_adx": adx
                    }
            msg = f"🔥 <b>ВХОД В СДЕЛКУ ({side})</b>\n\n<b>Тип:</b> <code>{idea}</code>\n<b>Вход:</b> <code>{entry_price:.2f}</code> | <b>SL:</b> <code>{sl_price:.2f}</code> | <b>TP:</b> <code>{tp_price:.2f}</code>"
            await broadcast_func(app, msg)
            await log_trade_to_sheet(decision)
            bot_data['monitored_signals'].append(decision)
            save_state(app)
            extended_message += "Сигнал найден и отправлен!"
            reason_prop = "SIGNAL_FOUND"

        # Log в debug sheet если debug_on
        if bot_data.get('debug_mode_on', False):
            debug_data = {
                "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                "ADX": adx, "PDI": pdi, "MDI": mdi, "ATR": atr,
                "Side": side, "DI_Diff": di_diff, "Reason_Prop": reason_prop
            }
            await log_debug_data(debug_data)

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

# === Логика мониторинга =============================================
async def monitor_active_trades(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    if not bot_data.get('monitored_signals'): return
    signal = bot_data['monitored_signals'][0]
    pair, entry_price, sl_price, tp_price, side, entry_adx = (signal['Pair'], signal['Entry_Price'], signal['SL_Price'], signal.get('TP_Price'), signal['side'], signal.get('entry_adx'))
    try:
        # Fetch current price
        order_book = await exchange.fetch_order_book(pair, limit=5, params={'type': 'swap'})
        if not (order_book.get('bids') and order_book['bids'][0] and order_book.get('asks') and order_book['asks'][0]): return
        best_bid, best_ask = order_book['bids'][0][0], order_book['asks'][0][0]
        last_price = (best_bid + best_ask) / 2
        
        # Fetch recent OHLCV for indicators
        recent_ohlcv = await exchange.fetch_ohlcv(pair, timeframe=TIMEFRAME, limit=50, params={'type': 'swap'})
        current_adx, current_pdi, current_mdi, current_atr, _, _, _, _ = calculate_indicators(recent_ohlcv)
        
        exit_status, exit_price, reason = None, None, None
        
        # Проверка TP/SL
        if (side == 'LONG' and last_price >= tp_price) or (side == 'SHORT' and last_price <= tp_price):
            exit_status, exit_price, reason = "TP_HIT", tp_price if side == 'LONG' else tp_price, "Take Profit достигнут"
        
        if not exit_status:
            if (side == 'LONG' and last_price <= sl_price) or (side == 'SHORT' and last_price >= sl_price):
                exit_status, exit_price, reason = "SL_HIT", sl_price, "Аварийный стоп-лосс"
        
        # Проверка DMI и ADX для закрытия
        if not exit_status:
            current_di_diff = abs(current_pdi - current_mdi)
            current_trend_dir = "LONG" if current_pdi > current_mdi else "SHORT"
            if current_di_diff < DI_CLOSE_THRESHOLD or current_trend_dir != side:
                exit_status, exit_price, reason = "DI_CROSS_OR_CLOSE", last_price, "Кроссовер DI или сужение разницы"
            elif current_adx < ADX_FLAT_THRESHOLD or current_adx < entry_adx - ADX_DROP_DELTA:
                exit_status, exit_price, reason = "ADX_DROP", last_price, "Падение ADX ниже порога"
        
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

# === Главный цикл ============================================
async def scanner_main_loop(app: Application, broadcast_func):
    bot_version = getattr(app, 'bot_version', 'N/A')
    log.info(f"Main Engine loop starting (v{bot_version})...")
    exchange = None

    try:
        exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
        await exchange.load_markets()
        log.info("Exchange connection and markets loaded.")

        last_ohlcv_update_time = 0

        while app.bot_data.get("bot_on", False):
            try:
                if time.time() - last_ohlcv_update_time > 60:
                    ohlcv = await exchange.fetch_ohlcv(PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=50, params={'type': 'swap'})
                    last_ohlcv_update_time = time.time()
                if not app.bot_data.get('monitored_signals'):
                    await scan_for_new_opportunities(exchange, app, broadcast_func, ohlcv)
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
