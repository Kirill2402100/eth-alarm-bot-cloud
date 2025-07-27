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
STOCHRSI_PERIOD = 14
STOCHRSI_K_PERIOD = 3
STOCHRSI_D_PERIOD = 3
EMA_PERIOD = 200
ATR_PERIOD = 14
STOCHRSI_UPPER_BAND = 20
STOCHRSI_LOWER_BAND = 70
PRICE_STOP_LOSS_PERCENT = 0.0005
TAKE_PROFIT_PERCENT = 0.001
BREAK_EVEN_TRIGGER_PERCENT = 0.0005
KD_CROSS_BUFFER = 3
SIGNAL_COOLDOWN_SECONDS = 75

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
        # ИЗМЕНЕНИЕ: Явно указываем, что нужны данные с фьючерсного рынка (swap)
        ohlcv = await exchange.fetch_ohlcv(PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=300, params={'type': 'swap'})
        
        features_df = calculate_features(ohlcv)
        if features_df is None or len(features_df.tail(1)) < 1: return

        last_row = features_df.iloc[-1]
        last_price = last_row['close']
        
        if not signal.get('break_even_activated', False):
            break_even_price_trigger = 0
            if signal['side'] == 'LONG':
                break_even_price_trigger = signal['Entry_Price'] * (1 + BREAK_EVEN_TRIGGER_PERCENT)
                if last_price >= break_even_price_trigger:
                    signal['SL_Price'] = break_even_price_trigger
                    signal['break_even_activated'] = True
                    msg = (f"🛡️ <b>БЕЗУБЫТОК</b>\n\n"
                           f"<b>SL для {signal['side']} изменен на:</b> <code>{signal['SL_Price']:.4f}</code>")
                    await broadcast_func(app, msg)
            
            elif signal['side'] == 'SHORT':
                break_even_price_trigger = signal['Entry_Price'] * (1 - BREAK_EVEN_TRIGGER_PERCENT)
                if last_price <= break_even_price_trigger:
                    signal['SL_Price'] = break_even_price_trigger
                    signal['break_even_activated'] = True
                    msg = (f"🛡️ <b>БЕЗУБЫТОК</b>\n\n"
                           f"<b>SL для {signal['side']} изменен на:</b> <code>{signal['SL_Price']:.4f}</code>")
                    await broadcast_func(app, msg)

        current_k, current_d = last_row['stochrsi_k'], last_row['stochrsi_d']
        current_atr = last_row.get(f'ATRr_{ATR_PERIOD}')

        if pd.isna(current_k) or pd.isna(current_d): return

        exit_status, exit_price, exit_detail = None, last_price, None
        
        if signal['side'] == 'LONG':
            if last_price <= signal['SL_Price']:
                exit_status = "SL_HIT"
            elif last_price >= signal['TP_Price']:
                exit_status = "TP_HIT"
            elif current_k < current_d and (current_d - current_k) >= KD_CROSS_BUFFER:
                exit_status = "STOCHRSI_CROSS"
                exit_detail = f"K:{current_k:.2f} | D:{current_d:.2f}"

        elif signal['side'] == 'SHORT':
            if last_price >= signal['SL_Price']:
                exit_status = "SL_HIT"
            elif last_price <= signal['TP_Price']:
                exit_status = "TP_HIT"
            elif current_k > current_d and (current_k - current_d) >= KD_CROSS_BUFFER:
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
            
            if exit_detail is None:
                exit_detail = "" 

            if current_atr:
                exit_detail += f" | ATR: {current_atr:.4f}"
            
            exit_detail = exit_detail.lstrip(" | ")

            if exit_detail:
                msg += f"<b>Детали:</b> {exit_detail}"

            await broadcast_func(app, msg)
            await update_closed_trade(signal['Signal_ID'], exit_status, exit_price, pnl_usd, pnl_percent_display, exit_detail, current_atr)
            bot_data['monitored_signals'] = []
            
            cooldown_end_time = time.time() + SIGNAL_COOLDOWN_SECONDS
            bot_data['cooldown_until'] = cooldown_end_time
            
            cooldown_msg = f"⏱️ <b>ПАУЗА</b>\n\nПоиск сигналов возобновится через {SIGNAL_COOLDOWN_SECONDS} сек."
            await broadcast_func(app, cooldown_msg)

    except Exception as e:
        log.error(f"Ошибка мониторинга: {e}", exc_info=True)

async def scan_for_signals(exchange, app: Application, broadcast_func):
    try:
        # ИЗМЕНЕНИЕ: Явно указываем, что нужны данные с фьючерсного рынка (swap)
        ohlcv = await exchange.fetch_ohlcv(PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=300, params={'type': 'swap'})
        
        features_df = calculate_features(ohlcv)
        if features_df is None or len(features_df.tail(2)) < 2: return 

        last_row = features_df.iloc[-1]
        prev_row = features_df.iloc[-2]
        
        current_price = last_row['close']
        current_k = last_row['stochrsi_k']
        prev_k = prev_row['stochrsi_k']
        current_ema = last_row[f'EMA_{EMA_PERIOD}']
        current_atr = last_row.get(f'ATRr_14')

        if pd.isna(current_k) or pd.isna(prev_k) or pd.isna(current_ema) or pd.isna(current_atr): return

        if current_price > current_ema: trend = "UP"
        elif current_price < current_ema: trend = "DOWN"
        else: trend = "FLAT"
        
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
    tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT) if side == "LONG" else entry_price * (1 - TAKE_PROFIT_PERCENT)
    
    signal_id = f"stochrsi_momentum_{int(time.time() * 1000)}"

    decision = {
        "Signal_ID": signal_id, "Pair": PAIR_TO_SCAN, "side": side,
        "Entry_Price": entry_price, 
        "SL_Price": sl_price,
        "TP_Price": tp_price,
        "Status": "ACTIVE",
        "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        "StochRSI_at_Entry": features.get('stochrsi_k'),
        "ATR_at_Entry": features.get(f'ATRr_14'),
        "break_even_activated": False
    }
    
    app.bot_data.setdefault('monitored_signals', []).append(decision)
    await log_open_trade(decision)

    msg = (f"🔥 <b>СИГНАЛ ПО ИМПУЛЬСУ ({side})</b>\n\n"
           f"<b>Вход:</b> <code>{entry_price:.4f}</code>\n"
           f"<b>TP:</b>   <code>{tp_price:.4f}</code> ({TAKE_PROFIT_PERCENT * 100}%)\n"
           f"<b>SL:</b>   <code>{sl_price:.4f}</code> ({PRICE_STOP_LOSS_PERCENT * 100}%)")
    await broadcast_func(app, msg)


async def scanner_main_loop(app: Application, broadcast_func):
    log.info("StochRSI Momentum K/D Cross Engine loop starting...")
    
    # Опция defaultType здесь остается, она не мешает, но явное указание в запросе надежнее
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    await exchange.load_markets()
    
    while app.bot_data.get("bot_on", False):
        cooldown_until = app.bot_data.get('cooldown_until', 0)
        current_time = time.time()

        if not app.bot_data.get('monitored_signals'):
            if current_time > cooldown_until:
                await scan_for_signals(exchange, app, broadcast_func)
            else:
                pass
        else:
            await monitor_active_trades(exchange, app, broadcast_func)
        
        await asyncio.sleep(SCAN_INTERVAL)
        
    await exchange.close()
    log.info("StochRSI Momentum K/D Cross Engine loop stopped.")
