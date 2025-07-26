# scanner_engine.py

import asyncio
import time
import logging
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from telegram.ext import Application
from datetime import datetime, timezone
from trade_executor import log_open_trade, update_closed_trade, log_analysis_data

log = logging.getLogger("bot")

# --- Конфигурация стратегии ---
PAIR_TO_SCAN = 'SOL/USDT'
TIMEFRAME = '1m'
SCAN_INTERVAL = 5 

# --- Параметры стратегии StochRSI с фильтром тренда ---
STOCHRSI_PERIOD = 14
STOCHRSI_ENTRY_LONG = 5
STOCHRSI_ENTRY_SHORT = 95
EMA_PERIOD = 200 # <<< Период для фильтра тренда
PRICE_TAKE_PROFIT_PERCENT = 0.003 # <<< Возвращаем 0.3%
PRICE_STOP_LOSS_PERCENT = 0.003 # <<< Возвращаем 0.3%

def calculate_features(ohlcv):
    if len(ohlcv) < EMA_PERIOD: # Убедимся, что данных достаточно для EMA
        return None
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    # Расчет StochRSI
    stoch_rsi_df = df.ta.stochrsi(length=STOCHRSI_PERIOD, rsi_length=STOCHRSI_PERIOD, k=3, d=3)
    df['stochrsi_k'] = stoch_rsi_df.iloc[:, 0]
    # Расчет EMA для фильтра тренда
    df[f'EMA_{EMA_PERIOD}'] = df.ta.ema(length=EMA_PERIOD)
    return df

async def monitor_active_trades(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    signal = bot_data['monitored_signals'][0]
    try:
        order_book = await exchange.fetch_order_book(signal['Pair'], limit=1)
        last_price = (order_book['bids'][0][0] + order_book['asks'][0][0]) / 2

        exit_status, exit_price = None, last_price
        
        if signal['side'] == 'LONG':
            if last_price >= signal['TP_Price']: exit_status = "TP_HIT"
            elif last_price <= signal['SL_Price']: exit_status = "SL_HIT"
        elif signal['side'] == 'SHORT':
            if last_price <= signal['TP_Price']: exit_status = "TP_HIT"
            elif last_price >= signal['SL_Price']: exit_status = "SL_HIT"

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
            
            await update_closed_trade(signal['Signal_ID'], exit_status, exit_price, pnl_usd, pnl_percent_display)
            bot_data['monitored_signals'] = []
    except Exception as e:
        log.error(f"Ошибка мониторинга: {e}", exc_info=True)

async def scan_for_signals(exchange, app: Application, broadcast_func):
    try:
        ohlcv = await exchange.fetch_ohlcv(PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=300)
        features_df = calculate_features(ohlcv)
        if features_df is None or len(features_df.tail(2)) < 2: return 

        # Получаем последние значения
        last_row = features_df.iloc[-1]
        prev_row = features_df.iloc[-2]
        
        current_price = last_row['close']
        current_stochrsi = last_row['stochrsi_k']
        prev_stochrsi = prev_row['stochrsi_k']
        current_ema = last_row[f'EMA_{EMA_PERIOD}']

        if pd.isna(current_stochrsi) or pd.isna(prev_stochrsi) or pd.isna(current_ema): return

        # <<< ЛОГИКА ФИЛЬТРА ТРЕНДА >>>
        if current_price > current_ema:
            trend = "UP"
        elif current_price < current_ema:
            trend = "DOWN"
        else:
            trend = "FLAT"
            
        # Логируем данные для анализа
        analysis_data = {
            "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            "Close_Price": f"{current_price:.4f}",
            "StochRSI_k": f"{current_stochrsi:.2f}",
            "EMA_200": f"{current_ema:.4f}",
            "Trend_Direction": trend
        }
        await log_analysis_data(analysis_data)
        
        side = None
        # Ищем LONG только в восходящем тренде
        if trend == "UP" and (prev_stochrsi < STOCHRSI_ENTRY_LONG and current_stochrsi >= STOCHRSI_ENTRY_LONG):
            side = "LONG"
        # Ищем SHORT только в нисходящем тренде
        elif trend == "DOWN" and (prev_stochrsi > STOCHRSI_ENTRY_SHORT and current_stochrsi <= STOCHRSI_ENTRY_SHORT):
            side = "SHORT"
        
        if side:
            await execute_trade(app, broadcast_func, last_row, side)
        
        if app.bot_data.get('live_info_on', False):
            info_msg = (f"<b>[INFO]</b> Trend: {trend}\n"
                        f"StochRSI: <code>{current_stochrsi:.2f}</code> | "
                        f"Close: <code>{current_price:.2f}</code> | EMA: <code>{current_ema:.2f}</code>")
            await broadcast_func(app, msg)

    except Exception as e:
        log.error(f"Ошибка сканирования: {e}", exc_info=True)


async def execute_trade(app, broadcast_func, features, side):
    entry_price = features['close']
    tp_price = entry_price * (1 + PRICE_TAKE_PROFIT_PERCENT) if side == "LONG" else entry_price * (1 - PRICE_TAKE_PROFIT_PERCENT)
    sl_price = entry_price * (1 - PRICE_STOP_LOSS_PERCENT) if side == "LONG" else entry_price * (1 + PRICE_STOP_LOSS_PERCENT)
    signal_id = f"stochrsi_ema_{int(time.time() * 1000)}"

    decision = {
        "Signal_ID": signal_id, "Pair": PAIR_TO_SCAN, "side": side,
        "Entry_Price": entry_price, "SL_Price": sl_price, "TP_Price": tp_price,
        "Status": "ACTIVE",
        "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        "StochRSI_at_Entry": features.get('stochrsi_k'),
    }
    
    app.bot_data.setdefault('monitored_signals', []).append(decision)
    await log_open_trade(decision)

    msg = (f"🔥 <b>СИГНАЛ С ФИЛЬТРОМ ТРЕНДА ({side})</b>\n\n"
           f"<b>Пара:</b> {PAIR_TO_SCAN}\n"
           f"<b>Вход:</b> <code>{entry_price:.4f}</code>\n"
           f"<b>SL:</b> <code>{sl_price:.4f}</code> | <b>TP:</b> <code>{tp_price:.4f}</code>")
    await broadcast_func(app, msg)


async def scanner_main_loop(app: Application, broadcast_func):
    log.info("StochRSI + EMA Trend Filter Engine loop starting...")
    
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    await exchange.load_markets()
    
    while app.bot_data.get("bot_on", False):
        if not app.bot_data.get('monitored_signals'):
            await scan_for_signals(exchange, app, broadcast_func)
        else:
            await monitor_active_trades(exchange, app, broadcast_func)
        
        await asyncio.sleep(SCAN_INTERVAL)
        
    await exchange.close()
    log.info("StochRSI + EMA Trend Filter Engine loop stopped.")
