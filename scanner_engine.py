# scanner_engine.py

import asyncio
import time
import logging
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from telegram.ext import Application
from datetime import datetime, timezone
# <<< Убираем импорт log_analysis_data >>>
from trade_executor import log_open_trade, update_closed_trade

log = logging.getLogger("bot")

# --- Конфигурация стратегии ---
PAIR_TO_SCAN = 'SOL/USDT'
TIMEFRAME = '1m'
SCAN_INTERVAL = 5 

# --- Параметры стратегии ---
STOCHRSI_PERIOD = 14
STOCHRSI_K_PERIOD = 3
STOCHRSI_D_PERIOD = 3
EMA_PERIOD = 200
ATR_PERIOD = 14
STOCHRSI_UPPER_BAND = 70
STOCHRSI_LOWER_BAND = 30
PRICE_STOP_LOSS_PERCENT = 0.002

def calculate_features(ohlcv):
    if len(ohlcv) < EMA_PERIOD: return None
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    stoch_rsi_df = df.ta.stochrsi(length=STOCHRSI_PERIOD, rsi_length=STOCHRSI_PERIOD, k=STOCHRSI_K_PERIOD, d=STOCHRSI_D_PERIOD)
    df['stochrsi_k'] = stoch_rsi_df.iloc[:, 0]
    df['stochrsi_d'] = stoch_rsi_df.iloc[:, 1]
    df[f'EMA_{EMA_PERIOD}'] = df.ta.ema(length=EMA_PERIOD)
    df.ta.atr(length=ATR_PERIOD, append=True)
    return df

async def monitor_active_trades(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    signal = bot_data['monitored_signals'][0]
    try:
        ohlcv = await exchange.fetch_ohlcv(PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=300)
        features_df = calculate_features(ohlcv)
        if features_df is None or len(features_df.tail(2)) < 2: return

        last_row = features_df.iloc[-1]
        prev_row = features_df.iloc[-2]
        last_price = last_row['close']
        
        current_k, current_d = last_row['stochrsi_k'], last_row['stochrsi_d']
        prev_k, prev_d = prev_row['stochrsi_k'], prev_row['stochrsi_d']
        current_atr = last_row.get(f'ATRr_{ATR_PERIOD}') # <<< Получаем ATR для лога

        if pd.isna(current_k) or pd.isna(current_d) or pd.isna(prev_k) or pd.isna(prev_d): return

        exit_status, exit_price, exit_detail = None, last_price, None
        
        if signal['side'] == 'LONG':
            if last_price <= signal['SL_Price']: exit_status = "SL_HIT"
            elif prev_k > prev_d and current_k <= current_d:
                exit_status = "STOCHRSI_CROSS"
                exit_detail = f"K:{current_k:.2f} | D:{current_d:.2f}"
        elif signal['side'] == 'SHORT':
            if last_price >= signal['SL_Price']: exit_status = "SL_HIT"
            elif prev_k < prev_d and current_k >= current_d:
                exit_status = "STOCHRSI_CROSS"
                exit_detail = f"K:{current_k:.2f} | D:{current_d:.2f}"

        if exit_status:
            pnl_pct_raw = ((exit_price - signal['Entry_Price']) / signal['Entry_Price']) * (1 if signal['side'] == 'LONG' else -1)
            deposit = bot_data.get('deposit', 50)
            leverage = bot_data.get('leverage', 100)
            pnl_usd = deposit * leverage * pnl_pct_raw
            pnl_percent_display = pnl_pct_raw * 100 * leverage

            emoji = "✅" if pnl_usd > 0 else "❌"
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                   f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent_display:+.2f}%)</b>\n")
            if exit_detail: msg += f"<b>Детали:</b> {exit_detail}"

            await broadcast_func(app, msg)
            # <<< Передаем ATR при закрытии в функцию логгера >>>
            await update_closed_trade(signal['Signal_ID'], exit_status, exit_price, pnl_usd, pnl_percent_display, exit_detail, current_atr)
            bot_data['monitored_signals'] = []
    except Exception as e:
        log.error(f"Ошибка мониторинга: {e}", exc_info=True)

async def scan_for_signals(exchange, app: Application, broadcast_func):
    try:
        ohlcv = await exchange.fetch_ohlcv(PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=300)
        features_df = calculate_features(ohlcv)
        if features_df is None or len(features_df.tail(2)) < 2: return 

        last_row = features_df.iloc[-1]
        prev_row = features_df.iloc[-2]
        
        current_price = last_row['close']
        current_k = last_row['stochrsi_k']
        prev_k = prev_row['stochrsi_k']
        current_ema = last_row[f'EMA_{EMA_PERIOD}']

        if pd.isna(current_k) or pd.isna(prev_k) or pd.isna(current_ema): return

        if current_price > current_ema: trend = "UP"
        elif current_price < current_ema: trend = "DOWN"
        else: trend = "FLAT"
            
        # <<< УБРАН ВЫЗОВ log_analysis_data >>>
        
        side = None
        if trend == "UP" and (prev_k < STOCHRSI_UPPER_BAND and current_k >= STOCHRSI_UPPER_BAND):
            side = "LONG"
        elif trend == "DOWN" and (prev_k > STOCHRSI_LOWER_BAND and current_k <= STOCHRSI_LOWER_BAND):
            side = "SHORT"
        
        if side:
            await execute_trade(app, broadcast_func, last_row, side)
        
        if app.bot_data.get('live_info_on', False):
            info_msg = (f"<b>[INFO]</b> Trend: {trend}\n"
                        f"StochRSI: <code>{current_k:.2f}</code> | "
                        f"Close: <code>{current_price:.2f}</code> | EMA: <code>{current_ema:.2f}</code>")
            await broadcast_func(app, info_msg)

    except Exception as e:
        log.error(f"Ошибка сканирования: {e}", exc_info=True)


async def execute_trade(app, broadcast_func, features, side):
    entry_price = features['close']
    sl_price = entry_price * (1 - PRICE_STOP_LOSS_PERCENT) if side == "LONG" else entry_price * (1 + PRICE_STOP_LOSS_PERCENT)
    signal_id = f"stochrsi_momentum_{int(time.time() * 1000)}"

    decision = {
        "Signal_ID": signal_id, "Pair": PAIR_TO_SCAN, "side": side,
        "Entry_Price": entry_price, "SL_Price": sl_price,
        "Status": "ACTIVE",
        "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        "StochRSI_at_Entry": features.get('stochrsi_k'),
    }
    
    app.bot_data.setdefault('monitored_signals', []).append(decision)
    await log_open_trade(decision)

    msg = (f"🔥 <b>СИГНАЛ ПО ИМПУЛЬСУ ({side})</b>\n\n"
           f"<b>Вход:</b> <code>{entry_price:.4f}</code>\n"
           f"<b>SL:</b> <code>{sl_price:.4f}</code> (Выход по пересечению K/D)")
    await broadcast_func(app, msg)


async def scanner_main_loop(app: Application, broadcast_func):
    log.info("StochRSI Momentum K/D Cross Engine loop starting...")
    
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    await exchange.load_markets()
    
    while app.bot_data.get("bot_on", False):
        if not app.bot_data.get('monitored_signals'):
            await scan_for_signals(exchange, app, broadcast_func)
        else:
            await monitor_active_trades(exchange, app, broadcast_func)
        
        await asyncio.sleep(SCAN_INTERVAL)
        
    await exchange.close()
    log.info("StochRSI Momentum K/D Cross Engine loop stopped.")
