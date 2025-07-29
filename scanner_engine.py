import asyncio
import time
import logging
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from telegram.ext import Application
from datetime import datetime, timezone
from trade_executor import log_open_trade, log_tsl_update, update_closed_trade

log = logging.getLogger("bot")

# --- НАСТРОЙКИ СТРАТЕГИИ ---
PAIR_TO_SCAN = 'SOL/USDT:USDT' 
TIMEFRAME = '1m'
SCAN_INTERVAL = 5 
EMA_PERIOD = 200
TRAILING_STOP_STEP = 0.003

def calculate_features(ohlcv):
    if len(ohlcv) < EMA_PERIOD: return None
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df[f'EMA_{EMA_PERIOD}'] = df.ta.ema(length=EMA_PERIOD)
    return df

async def monitor_active_trades(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    signal = bot_data['monitored_signals'][0]
    try:
        ohlcv = await exchange.fetch_ohlcv(PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=EMA_PERIOD, params={'type': 'swap'})
        features_df = calculate_features(ohlcv)
        if features_df is None or features_df.empty: return

        last_row = features_df.iloc[-1]
        last_price = last_row['close']
        last_ema = last_row[f'EMA_{EMA_PERIOD}']
        # Получаем цену открытия для установки стопа
        last_open_price = last_row['open'] 
        
        exit_status = None
        if (signal['side'] == 'LONG' and last_row['low'] <= last_ema) or \
           (signal['side'] == 'SHORT' and last_row['high'] >= last_ema):
            exit_status = 'EMA_TOUCH'

        tsl = signal['trailing_stop']
        
        # Активация трейлинг-стопа
        if not tsl['activated']:
            activation_price = signal['Entry_Price'] * (1 + TRAILING_STOP_STEP) if signal['side'] == 'LONG' else signal['Entry_Price'] * (1 - TRAILING_STOP_STEP)
            if (signal['side'] == 'LONG' and last_price >= activation_price) or \
               (signal['side'] == 'SHORT' and last_price <= activation_price):
                tsl['activated'] = True
                # ИСПРАВЛЕНИЕ: Ставим стоп на цену ОТКРЫТИЯ (open) свечи-триггера
                tsl['stop_price'] = last_open_price 
                tsl['last_trail_price'] = activation_price
                signal['SL_Price'] = tsl['stop_price']
                msg = f"🛡️ <b>СТОП-ЛОСС АКТИВИРОВАН</b>\n\nУровень: <code>{tsl['stop_price']:.4f}</code>"
                await broadcast_func(app, msg)
                await log_tsl_update(signal['Signal_ID'], tsl['stop_price'])
        # Перемещение (трейлинг) стопа
        else:
            next_trail_price = tsl['last_trail_price'] * (1 + TRAILING_STOP_STEP) if signal['side'] == 'LONG' else tsl['last_trail_price'] * (1 - TRAILING_STOP_STEP)
            if (signal['side'] == 'LONG' and last_price >= next_trail_price) or \
               (signal['side'] == 'SHORT' and last_price <= next_trail_price):
                # ИСПРАВЛЕНИЕ: Двигаем стоп на цену ОТКРЫТИЯ (open) свечи-триггера
                tsl['stop_price'] = last_open_price
                tsl['last_trail_price'] = next_trail_price
                signal['SL_Price'] = tsl['stop_price']
                msg = f"⚙️ <b>СТОП-ЛОСС ПЕРЕДВИНУТ</b>\n\nНовый уровень: <code>{tsl['stop_price']:.4f}</code>"
                await broadcast_func(app, msg)
                await log_tsl_update(signal['Signal_ID'], tsl['stop_price'])

        if tsl['activated']:
            if (signal['side'] == 'LONG' and last_price <= tsl['stop_price']) or \
               (signal['side'] == 'SHORT' and last_price >= tsl['stop_price']):
                exit_status = 'TSL_HIT'

        if exit_status:
            pnl_pct_raw = ((last_price - signal['Entry_Price']) / signal['Entry_Price']) * (1 if signal['side'] == 'LONG' else -1)
            deposit = bot_data.get('deposit', 50)
            leverage = bot_data.get('leverage', 100)
            pnl_usd = deposit * leverage * pnl_pct_raw
            pnl_percent_display = pnl_pct_raw * 100 * leverage

            emoji = "✅" if pnl_usd > 0 else "❌"
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                   f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent_display:+.2f}%)</b>\n")

            await broadcast_func(app, msg)
            await update_closed_trade(signal['Signal_ID'], 'CLOSED', last_price, pnl_usd, pnl_percent_display, exit_status)
            bot_data['monitored_signals'] = []

    except Exception as e:
        log.error(f"Ошибка мониторинга: {e}", exc_info=True)

async def scan_for_signals(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    try:
        ohlcv = await exchange.fetch_ohlcv(PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=EMA_PERIOD + 5, params={'type': 'swap'})
        features_df = calculate_features(ohlcv)
        if features_df is None or len(features_df) < 3: return

        last_row = features_df.iloc[-1]
        prev_row = features_df.iloc[-2]

        current_price = last_row['close']
        current_ema = last_row[f'EMA_{EMA_PERIOD}']
        prev_price = prev_row['close']
        prev_ema = prev_row[f'EMA_{EMA_PERIOD}']

        if pd.isna(current_ema) or pd.isna(prev_ema): return

        state = bot_data.get('trade_state', 'SEARCHING_CROSS')

        if state == 'SEARCHING_CROSS':
            is_crossing_up = prev_price < prev_ema and current_price > current_ema
            is_crossing_down = prev_price > prev_ema and current_price < prev_ema
            if is_crossing_up or is_crossing_down:
                bot_data['trade_state'] = 'WAITING_CONFIRMATION'
                bot_data['candles_after_cross'] = 1
                bot_data['cross_direction'] = 'UP' if is_crossing_up else 'DOWN'
                log.info(f"EMA cross detected ({bot_data['cross_direction']}). Waiting for 2nd candle.")
        
        elif state == 'WAITING_CONFIRMATION':
            bot_data['candles_after_cross'] += 1
            if bot_data['candles_after_cross'] >= 2:
                touches_ema = last_row['low'] <= current_ema <= last_row['high']
                if touches_ema:
                    log.info(f"Candle {bot_data['candles_after_cross']} touches EMA. Waiting.")
                    return

                side = None
                if bot_data['cross_direction'] == 'UP' and current_price > current_ema:
                    side = 'LONG'
                elif bot_data['cross_direction'] == 'DOWN' and current_price < current_ema:
                    side = 'SHORT'
                
                if side:
                    log.info(f"Confirmation received. Executing {side} trade.")
                    await execute_trade(app, broadcast_func, last_row, side)
                
                bot_data['trade_state'] = 'SEARCHING_CROSS'

    except Exception as e:
        log.error(f"Ошибка сканирования: {e}", exc_info=True)

async def execute_trade(app, broadcast_func, features, side):
    entry_price = features['close']
    signal_id = f"ema_cross_strat_{int(time.time() * 1000)}"

    decision = {
        "Signal_ID": signal_id, "Pair": PAIR_TO_SCAN, "side": side,
        "Entry_Price": entry_price, "Status": "ACTIVE",
        "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        "trailing_stop": {"activated": False, "stop_price": 0, "last_trail_price": 0},
        "SL_Price": 0, "TP_Price": 0 
    }
    
    app.bot_data.setdefault('monitored_signals', []).append(decision)
    await log_open_trade(decision)

    msg = (f"🔥 <b>НОВЫЙ СИГНАЛ ({side})</b>\n\n"
           f"<b>Пара:</b> {PAIR_TO_SCAN}\n"
           f"<b>Вход:</b> <code>{entry_price:.4f}</code>\n"
           f"<b>Выход:</b> Касание EMA {EMA_PERIOD} или трейлинг-стоп.")
    await broadcast_func(app, msg)


async def scanner_main_loop(app: Application, broadcast_func):
    log.info("EMA Cross Strategy Engine loop starting...")
    
    app.bot_data['trade_state'] = 'SEARCHING_CROSS'
    
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    await exchange.load_markets()
    
    while app.bot_data.get("bot_on", False):
        if not app.bot_data.get('monitored_signals'):
            await scan_for_signals(exchange, app, broadcast_func)
        else:
            await monitor_active_trades(exchange, app, broadcast_func)
        
        await asyncio.sleep(SCAN_INTERVAL)
        
    await exchange.close()
    log.info("EMA Cross Strategy Engine loop stopped.")
