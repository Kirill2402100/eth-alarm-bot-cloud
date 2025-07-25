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

# --- Параметры стратегии ---
RSI_PERIOD = 6
PRICE_TAKE_PROFIT_PERCENT = 0.001
PRICE_STOP_LOSS_PERCENT = 0.001

# <<< НОВЫЕ ПАРАМЕТРЫ ДЛЯ РАЗНЫХ РЕЖИМОВ РЫНКА >>>
# --- Активный рынок (высокая волатильность) ---
ACTIVE_RSI_ENTRY_LONG = 25
ACTIVE_RSI_ENTRY_SHORT = 75

# --- Неактивный рынок (низкая волатильность) ---
INACTIVE_RSI_ENTRY_LONG = 40
INACTIVE_RSI_ENTRY_SHORT = 65

# --- Настройки индикатора активности рынка (ATR) ---
ATR_PERIOD = 6
ATR_AVG_PERIOD = 100 # Период для скользящей средней ATR

def calculate_features(ohlcv):
    if len(ohlcv) < ATR_AVG_PERIOD: # Убедимся, что данных достаточно для всех расчетов
        return None
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df.ta.rsi(length=RSI_PERIOD, append=True)
    # <<< ДОБАВЛЕН РАСЧЕТ ATR ДЛЯ ОПРЕДЕЛЕНИЯ АКТИВНОСТИ >>>
    df.ta.atr(length=ATR_PERIOD, append=True)
    # Считаем скользящую среднюю от ATR, чтобы понимать, выше или ниже нормы текущая волатильность
    df[f'ATR_AVG_{ATR_AVG_PERIOD}'] = df[f'ATRr_{ATR_PERIOD}'].rolling(window=ATR_AVG_PERIOD).mean()
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

        # <<< ЛОГИКА ПЕРЕКЛЮЧЕНИЯ ПАРАМЕТРОВ >>>
        current_atr = features_df[f'ATRr_{ATR_PERIOD}'].iloc[-1]
        avg_atr = features_df[f'ATR_AVG_{ATR_AVG_PERIOD}'].iloc[-1]
        
        market_is_active = current_atr > avg_atr
        
        if market_is_active:
            rsi_entry_long = ACTIVE_RSI_ENTRY_LONG
            rsi_entry_short = ACTIVE_RSI_ENTRY_SHORT
            market_status_str = "АКТИВНЫЙ"
        else:
            rsi_entry_long = INACTIVE_RSI_ENTRY_LONG
            rsi_entry_short = INACTIVE_RSI_ENTRY_SHORT
            market_status_str = "НЕАКТИВНЫЙ"
        
        # --- Основная логика входа ---
        current_rsi = features_df[f'RSI_{RSI_PERIOD}'].iloc[-1]
        prev_rsi = features_df[f'RSI_{RSI_PERIOD}'].iloc[-2]

        if pd.isna(current_rsi) or pd.isna(prev_rsi): return

        side = None
        if prev_rsi < rsi_entry_long and current_rsi >= rsi_entry_long:
            side = "LONG"
        elif prev_rsi > rsi_entry_short and current_rsi <= rsi_entry_short:
            side = "SHORT"
        
        if side:
            await execute_trade(app, broadcast_func, features_df.iloc[-1], side)
        
        if app.bot_data.get('live_info_on', False):
            info_msg = (f"<b>[INFO]</b> Рынок: {market_status_str}\n"
                        f"RSI: <code>{current_rsi:.2f}</code> | "
                        f"ATR: <code>{current_atr:.4f}</code> (Avg: <code>{avg_atr:.4f}</code>)")
            await broadcast_func(app, info_msg)

    except Exception as e:
        log.error(f"Ошибка сканирования: {e}", exc_info=True)


async def execute_trade(app, broadcast_func, features, side):
    entry_price = features['close']
    tp_price = entry_price * (1 + PRICE_TAKE_PROFIT_PERCENT) if side == "LONG" else entry_price * (1 - PRICE_TAKE_PROFIT_PERCENT)
    sl_price = entry_price * (1 - PRICE_STOP_LOSS_PERCENT) if side == "LONG" else entry_price * (1 + PRICE_STOP_LOSS_PERCENT)
    signal_id = f"rsi_{int(time.time() * 1000)}"

    decision = {
        "Signal_ID": signal_id, "Pair": PAIR_TO_SCAN, "side": side,
        "Entry_Price": entry_price, "SL_Price": sl_price, "TP_Price": tp_price,
        "Status": "ACTIVE",
        "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        "RSI_at_Entry": features.get(f'RSI_{RSI_PERIOD}'),
    }
    
    app.bot_data.setdefault('monitored_signals', []).append(decision)
    await log_open_trade(decision)

    msg = (f"🔥 <b>RSI СИГНАЛ НА ВХОД ({side})</b>\n\n"
           f"<b>Пара:</b> {PAIR_TO_SCAN}\n"
           f"<b>Вход:</b> <code>{entry_price:.4f}</code>\n"
           f"<b>SL:</b> <code>{sl_price:.4f}</code> | <b>TP:</b> <code>{tp_price:.4f}</code>")
    await broadcast_func(app, msg)


async def scanner_main_loop(app: Application, broadcast_func):
    log.info("Adaptive RSI Engine loop starting...")
    
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    await exchange.load_markets()
    
    while app.bot_data.get("bot_on", False):
        if not app.bot_data.get('monitored_signals'):
            await scan_for_signals(exchange, app, broadcast_func)
        else:
            await monitor_active_trades(exchange, app, broadcast_func)
        
        await asyncio.sleep(SCAN_INTERVAL)
        
    await exchange.close()
    log.info("Adaptive RSI Engine loop stopped.")
