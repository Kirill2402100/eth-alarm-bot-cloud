# scanner_engine.py
# ============================================================================
# v39.1 - РЕЖИМ LIVE-ЛОГИРОВАНИЯ
# - В главный цикл добавлена логика отправки статуса индикаторов,
#   если включен режим live-логирования (/info).
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
TIMEFRAME = '1m'
SCAN_INTERVAL = 5

# --- Параметры стратегии ---
RSI_PERIOD = 14
STOCH_K = 14
STOCH_D = 3
STOCH_SMOOTH = 3
RSI_LONG_THRESHOLD = 35
RSI_SHORT_THRESHOLD = 75
STOCH_LONG_THRESHOLD = 20
STOCH_SHORT_THRESHOLD = 80

TP_PERCENT = 0.01  # 1%
SL_PERCENT = 0.0025  # 0.25%
INTERMEDIATE_PERCENT = 0.005  # 0.5% для обновления SL

# === Функции-помощники =====================================================
def calculate_indicators(ohlcv):
    """Рассчитывает RSI и Stochastic по данным свечей."""
    if not ohlcv or len(ohlcv) < max(RSI_PERIOD, STOCH_K) + STOCH_SMOOTH:
        return None, None, None
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df.ta.rsi(length=RSI_PERIOD, append=True)
    df.ta.stoch(high=df['high'], low=df['low'], close=df['close'], k=STOCH_K, d=STOCH_D, smooth_k=STOCH_SMOOTH, append=True)
    last = df.iloc[-1]
    return last[f'RSI_{RSI_PERIOD}'], last[f'STOCHk_{STOCH_K}_{STOCH_D}_{STOCH_SMOOTH}'], last[f'STOCHd_{STOCH_K}_{STOCH_D}_{STOCH_SMOOTH}']

# === Логика сканирования =============================================
async def scan_for_new_opportunities(exchange, app: Application, broadcast_func, ohlcv):
    bot_data = app.bot_data
    reason_prop = ""
    side = ""
    try:
        rsi, stoch_k, stoch_d = calculate_indicators(ohlcv)
        if rsi is None:
            return # Просто выходим, основная петля обработает сообщение для live-лога

        trades = await exchange.fetch_trades(PAIR_TO_SCAN, limit=1, params={'type': 'swap'})
        if not trades:
            return
        entry_price = trades[0]['price']

        if rsi < RSI_LONG_THRESHOLD and stoch_k > stoch_d and stoch_k < STOCH_LONG_THRESHOLD:
            side = "LONG"
        elif rsi > RSI_SHORT_THRESHOLD and stoch_k < stoch_d and stoch_k > STOCH_SHORT_THRESHOLD:
            side = "SHORT"
        else:
            reason_prop = "NO_SIGNAL"
            # Если сигнала нет, просто выходим. Не нужно ничего логировать здесь.
            return

        sl_price = entry_price * (1 - SL_PERCENT) if side == "LONG" else entry_price * (1 + SL_PERCENT)
        tp_price = entry_price * (1 + TP_PERCENT) if side == "LONG" else entry_price * (1 - TP_PERCENT)
        intermediate_sl = entry_price * (1 + INTERMEDIATE_PERCENT) if side == "LONG" else entry_price * (1 - INTERMEDIATE_PERCENT)

        idea = f"RSI {rsi:.1f}, Stoch K/D {stoch_k:.1f}/{stoch_d:.1f}"
        decision = {"Signal_ID": f"signal_{int(time.time() * 1000)}",
                      "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                      "Pair": PAIR_TO_SCAN, "Algorithm_Type": "RSI Stoch 1m",
                      "Strategy_Idea": idea, "Entry_Price": entry_price, "SL_Price": sl_price,
                      "TP_Price": tp_price, "Intermediate_SL": intermediate_sl, "side": side,
                      "Deposit": bot_data.get('deposit', 50), "Leverage": bot_data.get('leverage', 100),
                      "RSI": rsi, "Stoch_K": stoch_k, "Stoch_D": stoch_d,
                      "intermediate_triggered": False
                      }
        msg = f"🔥 <b>ВХОД В СДЕЛКУ ({side})</b>\n\n<b>Тип:</b> <code>{idea} ({side})</code>\n<b>Вход:</b> <code>{entry_price:.4f}</code> | <b>SL:</b> <code>{sl_price:.4f}</code> | <b>TP:</b> <code>{tp_price:.4f}</code>"
        await broadcast_func(app, msg)
        await log_trade_to_sheet(decision)
        bot_data['monitored_signals'].append(decision)
        save_state(app)
        reason_prop = "SIGNAL_FOUND"

        if bot_data.get('debug_mode_on', False):
            await log_debug_data({
                "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                "RSI": rsi, "Stoch_K": stoch_k, "Stoch_D": stoch_d,
                "Side": side, "Reason_Prop": reason_prop
            })

    except Exception as e:
        log.error(f"КРИТИЧЕСКАЯ ОШИБКА СКАНЕРА: {e}", exc_info=True)
        # Отправляем сообщение об ошибке только если это не live-логи
        if not bot_data.get('live_info_on', False):
            await broadcast_func(app, f"Критическая ошибка в сканере: {e}")

# === Логика мониторинга =============================================
async def monitor_active_trades(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    if not bot_data.get('monitored_signals'): return
    signal = bot_data['monitored_signals'][0]
    pair, entry_price, sl_price, tp_price, intermediate_sl, side = (signal['Pair'], signal['Entry_Price'], signal['SL_Price'], signal['TP_Price'], signal['Intermediate_SL'], signal['side'])
    intermediate_triggered = signal.get('intermediate_triggered', False)
    try:
        order_book = await exchange.fetch_order_book(pair, limit=1, params={'type': 'swap'})
        last_price = (order_book['bids'][0][0] + order_book['asks'][0][0]) / 2
        
        # Live-мониторинг для активной сделки
        if bot_data.get('live_info_on', False):
             pnl_percent_raw = ((last_price - entry_price) / entry_price) * (-1 if side == 'SHORT' else 1)
             pnl_percent_display = pnl_percent_raw * 100 * signal['Leverage']
             info_msg = (
                 f"<b>[MONITOR]</b> | {side} {pair}\n"
                 f"Цена: <code>{last_price:.4f}</code> (вход {entry_price:.4f})\n"
                 f"PnL: <code>{pnl_percent_display:+.2f}%</code>"
             )
             await broadcast_func(app, info_msg)

        exit_status, exit_price, reason = None, None, None
        
        if not intermediate_triggered:
            if (side == 'LONG' and last_price >= intermediate_sl) or (side == 'SHORT' and last_price <= intermediate_sl):
                msg = f"📈 <b>Профит 0.5% ({side})</b>\nSL обновлен на цену входа: {intermediate_sl:.4f}."
                await broadcast_func(app, msg)
                signal['intermediate_triggered'] = True
                signal['SL_Price'] = intermediate_sl
                sl_price = intermediate_sl
                save_state(app)
        
        if (side == 'LONG' and last_price >= tp_price) or (side == 'SHORT' and last_price <= tp_price):
            exit_status, exit_price, reason = "TP_HIT", tp_price, "Take Profit"
        elif (side == 'LONG' and last_price <= sl_price) or (side == 'SHORT' and last_price >= sl_price):
            exit_status, exit_price, reason = "SL_HIT", sl_price, "Stop Loss"
        
        if exit_status:
            pnl_percent_raw = ((exit_price - entry_price) / entry_price) * (-1 if side == 'SHORT' else 1)
            pnl_usd = signal['Deposit'] * signal['Leverage'] * pnl_percent_raw
            pnl_percent_display = pnl_percent_raw * 100 * signal['Leverage']
            await update_trade_in_sheet(signal, exit_status, exit_price, pnl_usd, pnl_percent_display, reason=reason)
            emoji = "✅" if pnl_usd >= 0 else "❌"
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({reason})</b>\n\n"
                   f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent_display:+.2f}%)</b>")
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
    ohlcv = []
    last_ohlcv_update_time = 0

    try:
        exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
        await exchange.load_markets()
        log.info("Exchange connection and markets loaded.")

        while app.bot_data.get("bot_on", False):
            try:
                current_time = time.time()
                # Обновляем свечи раз в минуту
                if current_time - last_ohlcv_update_time > 60:
                    ohlcv = await exchange.fetch_ohlcv(PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=100, params={'type': 'swap'})
                    last_ohlcv_update_time = current_time
                    log.info("OHLCV data updated.")

                # Если нет активных сделок, ищем новые
                if not app.bot_data.get('monitored_signals'):
                    # --- Логика для Live-логов ---
                    if app.bot_data.get('live_info_on', False):
                        rsi, stoch_k, stoch_d = calculate_indicators(ohlcv)
                        if rsi is not None:
                            info_msg = (
                                f"<b>[INFO]</b> | "
                                f"RSI: <code>{rsi:.2f}</code>, "
                                f"Stoch K/D: <code>{stoch_k:.2f}</code>/<code>{stoch_d:.2f}</code>"
                            )
                            await broadcast_func(app, info_msg)
                        else:
                            await broadcast_func(app, "<b>[INFO]</b> | Ожидание данных для расчета индикаторов...")
                    
                    await scan_for_new_opportunities(exchange, app, broadcast_func, ohlcv)
                
                # Если есть активная сделка, мониторим ее
                else:
                    await monitor_active_trades(exchange, app, broadcast_func)

                await asyncio.sleep(SCAN_INTERVAL)
            except Exception as e:
                log.critical(f"CRITICAL Error in loop iteration: {e}", exc_info=True)
                await broadcast_func(app, f"Критическая ошибка в цикле: {e}")
                await asyncio.sleep(20) # Пауза в случае серьезной ошибки
    except Exception as e:
        log.critical(f"CRITICAL STARTUP ERROR: {e}", exc_info=True)
        await broadcast_func(app, f"<b>КРИТИЧЕСКАЯ ОШИБКА ЗАПУСКА!</b>\n<code>Ошибка: {e}</code>")
    finally:
        if exchange:
            await exchange.close()
        log.info("Main Engine loop stopped.")
