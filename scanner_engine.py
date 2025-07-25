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

log = logging.getLogger("bot")

# --- Конфигурация стратегии ---
PAIR_TO_SCAN = 'SOL/USDT'
TIMEFRAME = '1m'
SCAN_INTERVAL = 5 

# <<< НОВЫЕ ПАРАМЕТРЫ ДЛЯ СТРАТЕГИИ StochRSI >>>
STOCHRSI_PERIOD = 14
STOCHRSI_ENTRY_LONG = 5
STOCHRSI_ENTRY_SHORT = 95
PRICE_TAKE_PROFIT_PERCENT = 0.001 # <<< Изменено на 0.1%
PRICE_STOP_LOSS_PERCENT = 0.001 # <<< Изменено на 0.1%

def calculate_features(ohlcv):
    # <<< МЕНЯЕМ ИНДИКАТОР НА StochRSI >>>
    if len(ohlcv) < STOCHRSI_PERIOD * 2: # StochRSI требует больше данных для разогрева
        return None
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    # Расчет StochRSI. Нам нужна основная линия 'k'
    stoch_rsi_df = df.ta.stochrsi(length=STOCHRSI_PERIOD, rsi_length=STOCHRSI_PERIOD, k=3, d=3)
    # Переименовываем колонку для удобства
    df['stochrsi_k'] = stoch_rsi_df.iloc[:, 0]
    return df

async def monitor_active_trades(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    signal = bot_data['monitored_signals'][0]
    
    try:
        # <<< Мониторинг упрощен - теперь он только следит за ценой >>>
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

        current_stochrsi = features_df['stochrsi_k'].iloc[-1]
        prev_stochrsi = features_df['stochrsi_k'].iloc[-2]

        if pd.isna(current_stochrsi) or pd.isna(prev_stochrsi): return

        side = None
        # <<< НОВАЯ ЛОГИКА ВХОДА ПО StochRSI >>>
        if prev_stochrsi < STOCHRSI_ENTRY_LONG and current_stochrsi >= STOCHRSI_ENTRY_LONG:
            side = "LONG"
        elif prev_stochrsi > STOCHRSI_ENTRY_SHORT and current_stochrsi <= STOCHRSI_ENTRY_SHORT:
            side = "SHORT"
        
        if side:
            await execute_trade(app, broadcast_func, features_df.iloc[-1], side)
        
        if app.bot_data.get('live_info_on', False):
            info_msg = (f"<b>[StochRSI INFO]</b> | Current: <code>{current_stochrsi:.2f}</code> | "
                        f"Close: <code>{features_df['close'].iloc[-1]:.2f}</code>")
            await broadcast_func(app, info_msg)

    except Exception as e:
        log.error(f"Ошибка сканирования: {e}", exc_info=True)


async def execute_trade(app, broadcast_func, features, side):
    entry_price = features['close']
    tp_price = entry_price * (1 + PRICE_TAKE_PROFIT_PERCENT) if side == "LONG" else entry_price * (1 - PRICE_TAKE_PROFIT_PERCENT)
    sl_price = entry_price * (1 - PRICE_STOP_LOSS_PERCENT) if side == "LONG" else entry_price * (1 + PRICE_STOP_LOSS_PERCENT)
    signal_id = f"stochrsi_{int(time.time() * 1000)}"

    decision = {
        "Signal_ID": signal_id, "Pair": PAIR_TO_SCAN, "side": side,
        "Entry_Price": entry_price, "SL_Price": sl_price, "TP_Price": tp_price,
        "Status": "ACTIVE",
        "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        "StochRSI_at_Entry": features.get('stochrsi_k'),
    }
    
    app.bot_data.setdefault('monitored_signals', []).append(decision)
    await log_open_trade(decision)

    msg = (f"🔥 <b>StochRSI СИГНАЛ НА ВХОД ({side})</b>\n\n"
           f"<b>Пара:</b> {PAIR_TO_SCAN}\n"
           f"<b>Вход:</b> <code>{entry_price:.4f}</code>\n"
           f"<b>SL:</b> <code>{sl_price:.4f}</code> | <b>TP:</b> <code>{tp_price:.4f}</code>")
    await broadcast_func(app, msg)


async def scanner_main_loop(app: Application, broadcast_func):
    log.info("StochRSI Reversal Engine loop starting...")
    
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    await exchange.load_markets()
    
    while app.bot_data.get("bot_on", False):
        if not app.bot_data.get('monitored_signals'):
            await scan_for_signals(exchange, app, broadcast_func)
        else:
            await monitor_active_trades(exchange, app, broadcast_func)
        
        await asyncio.sleep(SCAN_INTERVAL)
        
    await exchange.close()
    log.info("StochRSI Reversal Engine loop stopped.")
