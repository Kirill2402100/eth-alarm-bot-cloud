# scanner_engine.py
import asyncio
import time
import logging
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from telegram.ext import Application
import joblib
from datetime import datetime, timezone

# Импортируем нашу новую функцию
from trade_executor import log_trade_to_sheet

log = logging.getLogger("bot")

# --- Конфигурация ---
PAIR_TO_SCAN = 'SOL/USDT'
TIMEFRAME = '1m'
SCAN_INTERVAL = 5
PROBABILITY_THRESHOLD = 0.70
TP_PERCENT = 0.01
SL_PERCENT = 0.005

# --- Загрузка ML модели ---
try:
    ML_MODEL = joblib.load('trading_model.pkl')
    log.info("ML модель 'trading_model.pkl' успешно загружена.")
except FileNotFoundError:
    log.error("Файл модели 'trading_model.pkl' не найден! Бот будет работать без ML.")
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

async def execute_trade(app, broadcast_func, entry_price, side, probability):
    sl_price = entry_price * (1 - SL_PERCENT) if side == "LONG" else entry_price * (1 + SL_PERCENT)
    tp_price = entry_price * (1 + TP_PERCENT) if side == "LONG" else entry_price * (1 - TP_PERCENT)
    
    # --- Собираем данные для записи в таблицу ---
    signal_id = f"ml_{int(time.time() * 1000)}"
    trade_data = {
        "Signal_ID": signal_id,
        "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        "Pair": PAIR_TO_SCAN,
        "Algorithm_Type": "ML-XGBoost",
        "Strategy_Idea": f"Prob: {probability:.1%}",
        "Entry_Price": entry_price,
        "SL_Price": sl_price,
        "TP_Price": tp_price,
        "side": side,
        "Probability": f"{probability:.2%}",
        "Status": "SIGNALED"
    }

    # --- Логируем в таблицу ---
    await log_trade_to_sheet(trade_data)

    # --- Отправляем сообщение в Telegram ---
    msg = (f"🔥 <b>ML СИГНАЛ НА ВХОД ({side})</b>\n\n"
           f"<b>Вероятность успеха:</b> <code>{probability:.1%}</code> (ID: {signal_id})\n"
           f"<b>Вход:</b> <code>{entry_price:.4f}</code>\n"
           f"<b>SL:</b> <code>{sl_price:.4f}</code> | <b>TP:</b> <code>{tp_price:.4f}</code>")
    await broadcast_func(app, msg)
    
async def scanner_main_loop(app: Application, broadcast_func):
    bot_version = getattr(app, 'bot_version', 'N/A')
    log.info(f"Main Engine loop starting (v{bot_version})...")
    exchange = None
    
    if ML_MODEL is None:
        log.error("ML модель не загружена. Работа невозможна.")
        await broadcast_func(app, "<b>ОШИБКА: ML модель не найдена. Бот не может работать.</b>")
        return

    try:
        exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
        await exchange.load_markets()
        log.info("Exchange connection and markets loaded.")

        while app.bot_data.get("bot_on", False):
            try:
                ohlcv = await exchange.fetch_ohlcv(PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=300)
                features_series = calculate_features(ohlcv)

                if features_series is not None:
                    features_for_model = ['RSI_14', 'STOCHk_14_3_3', 'EMA_50', 'EMA_200', 'close', 'volume']
                    current_features = pd.DataFrame([features_series[features_for_model]])
                    
                    prediction_prob = ML_MODEL.predict_proba(current_features)[0]
                    success_probability = prediction_prob[1]

                    if success_probability > PROBABILITY_THRESHOLD:
                        await execute_trade(app, broadcast_func, features_series['close'], "LONG", success_probability)
                    
                    if app.bot_data.get('live_info_on', False):
                        info_msg = (f"<b>[ML INFO]</b> | Prob (Long): <code>{success_probability:.1%}</code> | "
                                    f"Close: <code>{features_series['close']:.2f}</code>")
                        await broadcast_func(app, info_msg)

                await asyncio.sleep(SCAN_INTERVAL)
            except Exception as e:
                log.critical(f"CRITICAL Error in loop: {e}", exc_info=True)
                await asyncio.sleep(20)
    finally:
        if exchange: await exchange.close()
        log.info("Main Engine loop stopped.")
