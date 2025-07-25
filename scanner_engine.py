# scanner_engine.py

import asyncio
import time
import logging
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from telegram.ext import Application
from datetime import datetime, timezone
from trade_executor import log_open_trade, update_closed_trade
# <<< Убрали debug_executor, т.к. вероятности ML больше не используются >>>

log = logging.getLogger("bot")

# <<< НОВАЯ КОНФИГУРАЦИЯ СТРАТЕГИИ >>>
PAIR_TO_SCAN = 'SOL/USDT'
TIMEFRAME = '5m' # <<< Изменен таймфрейм
SCAN_INTERVAL = 10 # Увеличим интервал, чтобы не делать лишних запросов на 5м свечах

# --- Параметры стратегии RSI Momentum ---
RSI_PERIOD = 14
RSI_OVERBOUGHT = 80
RSI_OVERSOLD = 20
RSI_MID_LINE = 50
PRICE_STOP_LOSS_PERCENT = 0.005 # 0.5%

# <<< Убрали загрузку ML Модели >>>

# <<< Функция теперь возвращает весь DataFrame, чтобы мы могли видеть предыдущие значения RSI >>>
def calculate_features(ohlcv):
    if len(ohlcv) < RSI_PERIOD + 2: # Нужно как минимум 2 последних значения RSI
        return None
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df.ta.rsi(length=RSI_PERIOD, append=True)
    df.dropna(inplace=True)
    return df

async def monitor_active_trades(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    signal = bot_data['monitored_signals'][0]
    
    try:
        # Получаем новые данные для расчета текущего RSI
        ohlcv = await exchange.fetch_ohlcv(PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=RSI_PERIOD + 5)
        features_df = calculate_features(ohlcv)
        if features_df is None or len(features_df) < 2:
            return # Недостаточно данных для анализа

        current_rsi = features_df[f'RSI_{RSI_PERIOD}'].iloc[-1]
        prev_rsi = features_df[f'RSI_{RSI_PERIOD}'].iloc[-2]
        
        # Получаем последнюю цену для проверки Stop Loss
        order_book = await exchange.fetch_order_book(signal['Pair'], limit=1)
        last_price = (order_book['bids'][0][0] + order_book['asks'][0][0]) / 2

        exit_status, exit_price = None, last_price
        
        # <<< НОВАЯ ЛОГИКА ВЫХОДА ИЗ СДЕЛКИ >>>
        if signal['side'] == 'LONG':
            if last_price <= signal['SL_Price']:
                exit_status = "SL_HIT"
            elif current_rsi < prev_rsi:
                exit_status = "RSI_REVERSAL"
            elif current_rsi >= RSI_OVERBOUGHT:
                exit_status = "RSI_LIMIT_HIT"
                
        elif signal['side'] == 'SHORT':
            if last_price >= signal['SL_Price']:
                exit_status = "SL_HIT"
            elif current_rsi > prev_rsi:
                exit_status = "RSI_REVERSAL"
            elif current_rsi <= RSI_OVERSOLD:
                exit_status = "RSI_LIMIT_HIT"

        if exit_status:
            pnl_pct_raw = ((exit_price - signal['Entry_Price']) / signal['Entry_Price']) * (1 if signal['side'] == 'LONG' else -1)
            deposit = bot_data.get('deposit', 50)
            leverage = bot_data.get('leverage', 100)
            pnl_usd = deposit * leverage * pnl_pct_raw
            pnl_percent_display = pnl_pct_raw * 100 * leverage

            emoji = "✅" if pnl_usd > 0 else "❌"
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                   f"<b>Пара:</b> {signal['Pair']}\n"
                   f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent_display:+.2f}%)</b>")
            await broadcast_func(app, msg)
            
            # <<< Передаем None вместо PNL, т.к. он уже не используется в этой функции >>>
            await update_closed_trade(signal['Signal_ID'], exit_status, exit_price, pnl_usd, pnl_percent_display)
            bot_data['monitored_signals'] = []

    except Exception as e:
        log.error(f"Ошибка мониторинга: {e}", exc_info=True)


async def scan_for_signals(exchange, app: Application, broadcast_func):
    try:
        ohlcv = await exchange.fetch_ohlcv(PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=RSI_PERIOD + 5)
        features_df = calculate_features(ohlcv)
        if features_df is None or len(features_df) < 2:
            return # Недостаточно данных для анализа

        current_rsi = features_df[f'RSI_{RSI_PERIOD}'].iloc[-1]
        prev_rsi = features_df[f'RSI_{RSI_PERIOD}'].iloc[-2]

        side = None
        
        # <<< НОВАЯ ЛОГИКА ВХОДА В СДЕЛКУ >>>
        if prev_rsi < RSI_MID_LINE and current_rsi > RSI_MID_LINE:
            side = "LONG"
        elif prev_rsi > RSI_MID_LINE and current_rsi < RSI_MID_LINE:
            side = "SHORT"
        
        if side:
            # Передаем последнюю цену закрытия из DataFrame
            await execute_trade(app, broadcast_func, features_df.iloc[-1], side)
        
        # Информационное сообщение в live-режиме
        if app.bot_data.get('live_info_on', False):
            info_msg = (f"<b>[RSI INFO]</b> | Current: <code>{current_rsi:.2f}</code> | "
                        f"Close: <code>{features_df['close'].iloc[-1]:.2f}</code>")
            await broadcast_func(app, info_msg)

    except Exception as e:
        log.error(f"Ошибка сканирования: {e}", exc_info=True)


async def execute_trade(app, broadcast_func, features, side):
    # <<< Убрали 'probability' из аргументов >>>
    entry_price = features['close']
    sl_price = entry_price * (1 - PRICE_STOP_LOSS_PERCENT) if side == "LONG" else entry_price * (1 + PRICE_STOP_LOSS_PERCENT)
    signal_id = f"rsi_{int(time.time() * 1000)}"

    decision = {
        "Signal_ID": signal_id, "Pair": PAIR_TO_SCAN, "side": side,
        "Entry_Price": entry_price, "SL_Price": sl_price, 
        "Status": "ACTIVE",
        "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        # <<< Упростили словарь, убрав лишние данные >>>
        "RSI_at_Entry": features.get(f'RSI_{RSI_PERIOD}'),
    }
    
    app.bot_data.setdefault('monitored_signals', []).append(decision)
    # <<< TP_Price больше нет, он определяется динамически по RSI >>>
    await log_open_trade(decision)

    msg = (f"🔥 <b>RSI СИГНАЛ НА ВХОД ({side})</b>\n\n"
           f"<b>Пара:</b> {PAIR_TO_SCAN}\n"
           f"<b>Вход:</b> <code>{entry_price:.4f}</code>\n"
           f"<b>SL:</b> <code>{sl_price:.4f}</code>\n"
           f"<i>(TP будет определен по развороту RSI)</i>")
    await broadcast_func(app, msg)


async def scanner_main_loop(app: Application, broadcast_func):
    log.info("RSI Momentum Engine loop starting...")
    
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    await exchange.load_markets()
    
    while app.bot_data.get("bot_on", False):
        if not app.bot_data.get('monitored_signals'):
            await scan_for_signals(exchange, app, broadcast_func)
        else:
            await monitor_active_trades(exchange, app, broadcast_func)
        
        await asyncio.sleep(SCAN_INTERVAL)
        
    await exchange.close()
    log.info("RSI Momentum Engine loop stopped.")
