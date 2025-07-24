# scanner_engine.py
import asyncio
import time
import logging
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from telegram.ext import Application
import xgboost as xgb
from datetime import datetime, timezone
from trade_executor import log_open_trade, update_closed_trade
from debug_executor import log_debug_data

log = logging.getLogger("bot")

# --- Конфигурация ---
PAIR_TO_SCAN = 'SOL/USDT'
TIMEFRAME = '1m'
SCAN_INTERVAL = 5
PROBABILITY_THRESHOLD = 0.65 
TP_PERCENT = 0.01
SL_PERCENT = 0.005

# --- Загрузка ML модели ---
try:
    ML_MODEL = xgb.Booster()
    ML_MODEL.load_model('trading_model.json')
    log.info("ML модель 'trading_model.json' успешно загружена.")
except Exception as e:
    log.error(f"Ошибка загрузки модели: {e}")
    ML_MODEL = None

def calculate_features(ohlcv):
    if len(ohlcv) < 201: return None
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df.ta.rsi(length=14, append=True)
    df.ta.stoch(k=14, d=3, smooth_k=3, append=True)
    df.ta.ema(length=50, append=True)
    df.ta.ema(length=200, append=True)
    df.dropna(inplace=True)
    return df.iloc[-1]

async def monitor_active_trades(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    signal = bot_data['monitored_signals'][0]
    
    try:
        order_book = await exchange.fetch_order_book(signal['Pair'], limit=1)
        last_price = (order_book['bids'][0][0] + order_book['asks'][0][0]) / 2

        exit_status, exit_price = None, None
        
        if signal['side'] == 'LONG':
            if last_price >= signal['TP_Price']: exit_status, exit_price = "TP_HIT", signal['TP_Price']
            elif last_price <= signal['SL_Price']: exit_status, exit_price = "SL_HIT", signal['SL_Price']
        elif signal['side'] == 'SHORT':
            if last_price <= signal['TP_Price']: exit_status, exit_price = "TP_HIT", signal['TP_Price']
            elif last_price >= signal['SL_Price']: exit_status, exit_price = "SL_HIT", signal['SL_Price']

        if exit_status:
            pnl_pct_raw = ((exit_price - signal['Entry_Price']) / signal['Entry_Price']) * (1 if signal['side'] == 'LONG' else -1)
            deposit = bot_data.get('deposit', 50)
            leverage = bot_data.get('leverage', 100)
            pnl_usd = deposit * leverage * pnl_pct_raw
            pnl_percent_display = pnl_pct_raw * 100 * leverage

            emoji = "✅" if pnl_usd > 0 else "❌"
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                   f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent_display:+.2f}%)</b>")
            await broadcast_func(app, msg)
            
            await update_closed_trade(signal['Signal_ID'], exit_status, exit_price, pnl_usd, pnl_percent_display)
            bot_data['monitored_signals'] = []

    except Exception as e:
        log.error(f"Ошибка мониторинга: {e}", exc_info=True)

async def scan_for_signals(exchange, app: Application, broadcast_func):
    try:
        ohlcv = await exchange.fetch_ohlcv(PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=300)
        features_series = calculate_features(ohlcv)
        if features_series is None: return

        features_for_model = ['RSI_14', 'STOCHk_14_3_3', 'EMA_50', 'EMA_200', 'close', 'volume']
        current_features = pd.DataFrame([features_series[features_for_model]])
        
        prediction_prob = ML_MODEL.predict(xgb.DMatrix(current_features))[0]
        prob_long = prediction_prob[1]
        prob_short = prediction_prob[2]
        
        debug_info = {
            "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            "Close_Price": features_series['close'],
            "Prob_Long": f"{prob_long:.2%}",
            "Prob_Short": f"{prob_short:.2%}",
            "RSI_14": features_series['RSI_14'],
            "STOCHk_14_3_3": features_series['STOCHk_14_3_3']
        }
        await log_debug_data(debug_info)

        side, probability = None, 0
        if prob_long > PROBABILITY_THRESHOLD and prob_long > prob_short:
            side, probability = "LONG", prob_long
        elif prob_short > PROBABILITY_THRESHOLD and prob_short > prob_long:
            side, probability = "SHORT", prob_short
        
        if side:
            await execute_trade(app, broadcast_func, features_series, side, probability)
        
        if app.bot_data.get('live_info_on', False):
            info_msg = (f"<b>[ML INFO]</b> | Prob (L/S): <code>{prob_long:.1%} / {prob_short:.1%}</code> | "
                        f"Close: <code>{features_series['close']:.2f}</code>")
            await broadcast_func(app, info_msg)

    except Exception as e:
        log.error(f"Ошибка сканирования: {e}", exc_info=True)

async def execute_trade(app, broadcast_func, features, side, probability):
    entry_price = features['close']
    sl_price = entry_price * (1 - SL_PERCENT) if side == "LONG" else entry_price * (1 + SL_PERCENT)
    tp_price = entry_price * (1 + TP_PERCENT) if side == "LONG" else entry_price * (1 - TP_PERCENT)
    signal_id = f"ml_{int(time.time() * 1000)}"

    decision = {
        "Signal_ID": signal_id, "Pair": PAIR_TO_SCAN, "side": side,
        "Entry_Price": entry_price, "SL_Price": sl_price, "TP_Price": tp_price,
        "Probability": f"{probability:.2%}", "Status": "ACTIVE",
        "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        "Algorithm_Type": "ML-XGBoost-v2", "RSI_14": features.get('RSI_14'),
        "STOCHk_14_3_3": features.get('STOCHk_14_3_3'), "EMA_50": features.get('EMA_50'),
        "EMA_200": features.get('EMA_200'), "close": features.get('close'),
        "volume": features.get('volume')
    }
    
    app.bot_data.setdefault('monitored_signals', []).append(decision)
    await log_open_trade(decision)

    msg = (f"🔥 <b>ML СИГНАЛ НА ВХОД ({side})</b>\n\n"
           f"<b>Вероятность успеха:</b> <code>{probability:.1%}</code> (ID: {signal_id})\n"
           f"<b>Вход:</b> <code>{entry_price:.4f}</code>\n"
           f"<b>SL:</b> <code>{sl_price:.4f}</code> | <b>TP:</b> <code>{tp_price:.4f}</code>")
    await broadcast_func(app, msg)

async def scanner_main_loop(app: Application, broadcast_func):
    log.info("Main Engine loop starting...")
    if ML_MODEL is None:
        await broadcast_func(app, "<b>ОШИБКА: ML модель не найдена.</b>")
        return

    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    await exchange.load_markets()
    
    while app.bot_data.get("bot_on", False):
        if not app.bot_data.get('monitored_signals'):
            await scan_for_signals(exchange, app, broadcast_func)
        else:
            await monitor_active_trades(exchange, app, broadcast_func)
        
        await asyncio.sleep(SCAN_INTERVAL)
        
    await exchange.close()
    log.info("Main Engine loop stopped.")
