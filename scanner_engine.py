# scanner_engine.py

import asyncio
import time
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from telegram.ext import Application

# Импортируем все необходимые функции, включая новую
from trade_executor import log_open_trade, update_closed_trade, log_diagnostic_entry, flush_log_buffers

log = logging.getLogger("swing_bot_engine")

# ===========================================================================
# CONFIGURATION
# ===========================================================================
class CONFIG:
    TIMEFRAME = "15m"
    POSITION_SIZE_USDT = 10.0
    LEVERAGE = 20
    MAX_CONCURRENT_POSITIONS = 10
    MIN_DAILY_VOLATILITY_PCT = 3.0
    MAX_DAILY_VOLATILITY_PCT = 10.0
    MIN_PRICE = 0.001
    EMA_FAST_PERIOD = 9
    EMA_SLOW_PERIOD = 21
    EMA_TREND_PERIOD = 200
    EMA_TREND_BUFFER_PCT = 0.5
    STOCH_RSI_PERIOD = 14
    STOCH_RSI_K = 3
    STOCH_RSI_D = 3
    STOCH_RSI_MID = 50
    ATR_PERIOD = 14
    ATR_SPIKE_MULT = 2.5
    ATR_COOLDOWN_BARS = 2
    ATR_SL_MULT = 1.8
    RISK_REWARD = 2
    SL_MIN_PCT = 1.0
    SL_MAX_PCT = 5.0
    SCANNER_INTERVAL_SECONDS = 300
    TICK_MONITOR_INTERVAL_SECONDS = 2
    OHLCV_LIMIT = 1000

# ===========================================================================
# HELPERS & RISK MANAGEMENT
# ===========================================================================

def tf_seconds(tf: str) -> int:
    unit = tf[-1].lower()
    n = int(tf[:-1])
    if unit == "m": return n * 60
    if unit == "h": return n * 3600
    if unit == "d": return n * 86400
    return 0

def atr_based_levels(entry: float, atr: float, side: str) -> tuple[float, float, float]:
    sl_pct = CONFIG.ATR_SL_MULT * atr / entry * 100
    sl_pct = min(max(sl_pct, CONFIG.SL_MIN_PCT), CONFIG.SL_MAX_PCT)
    if side == "LONG":
        sl_price = entry * (1 - sl_pct / 100)
        tp_price = entry * (1 + sl_pct * CONFIG.RISK_REWARD / 100)
    else:
        sl_price = entry * (1 + sl_pct / 100)
        tp_price = entry * (1 - sl_pct * CONFIG.RISK_REWARD / 100)
    return sl_price, tp_price, sl_pct

# ===========================================================================
# MARKET SCANNER
# ===========================================================================
async def filter_volatile_pairs(exchange: ccxt.Exchange) -> List[str]:
    log.info("Filtering pairs by daily volatility using fetch_tickers()...")
    try:
        await exchange.load_markets()
        tickers = await exchange.fetch_tickers()
        volatile_pairs = []
        for symbol, data in tickers.items():
            if symbol not in exchange.markets: continue
            market = exchange.market(symbol)
            if market.get('type') == 'swap' and market.get('quote') == 'USDT':
                vol = data.get('percentage')
                if vol is None and data.get('open') and data.get('last') and data['open'] > 0:
                    vol = abs(data['last'] - data['open']) / data['open'] * 100
                if vol is not None and CONFIG.MIN_DAILY_VOLATILITY_PCT <= vol <= CONFIG.MAX_DAILY_VOLATILITY_PCT:
                    volatile_pairs.append(symbol)
        log.info(f"Found {len(volatile_pairs)} volatile pairs.")
        return volatile_pairs
    except Exception as e:
        log.error(f"Error filtering volatile pairs: {e}", exc_info=True)
        return []

def check_entry_conditions(df: pd.DataFrame) -> Tuple[Optional[str], Dict]:
    if len(df) < 2: return None, {}
    ema_fast = f"EMA_{CONFIG.EMA_FAST_PERIOD}"
    ema_slow = f"EMA_{CONFIG.EMA_SLOW_PERIOD}"
    ema_trend = f"EMA_{CONFIG.EMA_TREND_PERIOD}"
    stoch_k = next((c for c in df.columns if c.startswith("STOCHRSIk_")), None)
    if not stoch_k: return None, {"Reason_For_Fail": "No StochRSI column"}
    last = df.iloc[-1]
    long_ema_ok = last[ema_fast] > last[ema_slow]
    short_ema_ok = last[ema_fast] < last[ema_slow]
    ema_trend_val = last[ema_trend]
    if pd.isna(ema_trend_val):
        long_trend_ok = short_trend_ok = True
    else:
        buf = 1 + CONFIG.EMA_TREND_BUFFER_PCT / 100
        long_trend_ok = last['close'] > ema_trend_val * buf
        short_trend_ok = last['close'] < ema_trend_val / buf
    k_now = last[stoch_k]
    k_prev = df[stoch_k].iloc[-2]
    long_stoch_ok = k_prev < CONFIG.STOCH_RSI_MID <= k_now
    short_stoch_ok = k_prev > CONFIG.STOCH_RSI_MID >= k_now
    if sum([long_ema_ok, long_trend_ok, long_stoch_ok]) == 3:
        return "LONG", {}
    if sum([short_ema_ok, short_trend_ok, short_stoch_ok]) == 3:
        return "SHORT", {}
    return None, {}

async def find_trade_signals(exchange: ccxt.Exchange, app: Application) -> None:
    bot_data = app.bot_data
    if len(bot_data.get("active_trades", [])) >= CONFIG.MAX_CONCURRENT_POSITIONS:
        log.info("Position limit reached. Skipping scan.")
        return
    volatile_pairs = await filter_volatile_pairs(exchange)
    if not volatile_pairs: return
    sem = asyncio.Semaphore(8)
    async def safe_fetch_ohlcv(symbol):
        async with sem:
            return await exchange.fetch_ohlcv(symbol, CONFIG.TIMEFRAME, limit=CONFIG.OHLCV_LIMIT)
    tasks = [safe_fetch_ohlcv(symbol) for symbol in volatile_pairs]
    log.info(f"Fetching OHLCV data for {len(volatile_pairs)} pairs concurrently...")
    ohlcv_results = await asyncio.gather(*tasks, return_exceptions=True)
    log.info("Finished fetching OHLCV data. Processing results...")
    for i, ohlcv in enumerate(ohlcv_results):
        symbol = volatile_pairs[i]
        try:
            if time.time() - bot_data.get("trade_cooldown", {}).get(symbol, 0) < tf_seconds(CONFIG.TIMEFRAME) * 2:
                continue
            if isinstance(ohlcv, Exception) or not ohlcv or len(ohlcv) < 50:
                if isinstance(ohlcv, Exception): log.warning(f"Could not fetch OHLCV for {symbol}: {ohlcv}")
                continue
            if len(bot_data.get("active_trades", [])) >= CONFIG.MAX_CONCURRENT_POSITIONS:
                log.info("Position limit reached during signal processing. Halting.")
                break
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            if time.time() * 1000 - df['timestamp'].iloc[-1] < tf_seconds(CONFIG.TIMEFRAME) * 1000:
                df = df.iloc[:-1]
            if len(df) < CONFIG.EMA_TREND_PERIOD: continue
            if df.iloc[-1]['close'] < CONFIG.MIN_PRICE: continue
            df.ta.ema(length=CONFIG.EMA_FAST_PERIOD, append=True)
            df.ta.ema(length=CONFIG.EMA_SLOW_PERIOD, append=True)
            df.ta.ema(length=CONFIG.EMA_TREND_PERIOD, append=True)
            df.ta.stochrsi(length=CONFIG.STOCH_RSI_PERIOD, k=CONFIG.STOCH_RSI_K, d=CONFIG.STOCH_RSI_D, append=True)
            df.ta.atr(length=CONFIG.ATR_PERIOD, append=True)
            atr_col = next((c for c in df.columns if c.startswith("ATR") and c.endswith(str(CONFIG.ATR_PERIOD))), None)
            if atr_col:
                df['range'] = df['high'] - df['low']
                df['is_spike'] = df['range'] > df[atr_col] * CONFIG.ATR_SPIKE_MULT
                if df['is_spike'].iloc[-1] or df['is_spike'].iloc[-CONFIG.ATR_COOLDOWN_BARS:].any():
                    continue
            side, _ = check_entry_conditions(df.copy())
            if side:
                if any(t["Pair"] == symbol for t in bot_data.get("active_trades", [])): continue
                atr_last = df[atr_col].iloc[-1] if atr_col else 0
                await open_new_trade(symbol, side, df.iloc[-1]['close'], atr_last, app)
        except Exception as e:
            log.error(f"Error processing symbol {symbol}: {e}")

# ===========================================================================
# TRADE MANAGER
# ===========================================================================
async def open_new_trade(symbol: str, side: str, entry_price: float, atr_last: float, app: Application):
    bot_data = app.bot_data
    sl_price, tp_price, sl_pct = atr_based_levels(entry_price, atr_last, side)
    tp_pct = sl_pct * CONFIG.RISK_REWARD
    trade = {
        "Signal_ID": f"{symbol}_{int(time.time())}", "Pair": symbol, "Side": side,
        "Entry_Price": entry_price, "SL_Price": sl_price, "TP_Price": tp_price,
        "Status": "ACTIVE", "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "sl_pct": sl_pct, "tp_pct": tp_pct
    }
    bot_data.setdefault("active_trades", []).append(trade)
    log.info(f"New trade signal: {trade}")
    if broadcast := bot_data.get('broadcast_func'):
        sl_disp = round(sl_pct * CONFIG.LEVERAGE)
        tp_disp = round(tp_pct * CONFIG.LEVERAGE)
        msg = (f"🔥 <b>НОВЫЙ СИГНАЛ ({side})</b>\n\n"
               f"<b>Пара:</b> {symbol}\n<b>Вход:</b> <code>{entry_price:.4f}</code>\n"
               f"<b>SL:</b> <code>{sl_price:.4f}</code> (-{sl_disp}%)\n"
               f"<b>TP:</b> <code>{tp_price:.4f}</code> (+{tp_disp}%)")
        await broadcast(app, msg)
    await log_open_trade(trade)

async def monitor_active_trades(exchange: ccxt.Exchange, app: Application):
    bot_data = app.bot_data
    active_trades = bot_data.get("active_trades", [])
    if not active_trades: return
    symbols = [t['Pair'] for t in active_trades]
    try:
        tickers = await exchange.fetch_tickers(symbols)
    except Exception as e:
        log.error(f"Failed to fetch tickers for monitoring: {e}")
        return
    trades_to_close = []
    for trade in active_trades:
        if trade['Pair'] not in tickers or tickers[trade['Pair']].get('last') is None: continue
        ticker_data = tickers[trade['Pair']]
        last_price = ticker_data.get('bid' if trade['Side'] == 'LONG' else 'ask', ticker_data['last'])
        exit_reason = None
        if trade['Side'] == 'LONG':
            if last_price <= trade['SL_Price']: exit_reason = "STOP_LOSS"
            elif last_price >= trade['TP_Price']: exit_reason = "TAKE_PROFIT"
        else:
            if last_price >= trade['SL_Price']: exit_reason = "STOP_LOSS"
            elif last_price <= trade['TP_Price']: exit_reason = "TAKE_PROFIT"
        if exit_reason:
            trades_to_close.append((trade, exit_reason, last_price))
    if trades_to_close:
        broadcast = app.bot_data.get('broadcast_func')
        for trade, reason, exit_price in trades_to_close:
            pnl_display = trade['tp_pct'] * CONFIG.LEVERAGE if reason == "TAKE_PROFIT" else -trade['sl_pct'] * CONFIG.LEVERAGE
            pnl_usd = CONFIG.POSITION_SIZE_USDT * pnl_display / 100
            bot_data.setdefault("trade_cooldown", {})[trade['Pair']] = time.time()
            if broadcast:
                emoji = "✅" if pnl_usd > 0 else "❌"
                msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({reason})</b>\n\n"
                       f"<b>Пара:</b> {trade['Pair']}\n<b>Результат: ${pnl_usd:+.2f} ({pnl_display:+.2f}%)</b>")
                await broadcast(app, msg)
            await update_closed_trade(trade['Signal_ID'], "CLOSED", exit_price, pnl_usd, pnl_display, reason)
        closed_ids = {t['Signal_ID'] for t, _, _ in trades_to_close}
        bot_data["active_trades"] = [t for t in active_trades if t['Signal_ID'] not in closed_ids]

# ===========================================================================
# MAIN LOOP
# ===========================================================================
async def scanner_main_loop(app: Application, broadcast):
    log.info("Scanner Engine loop starting…")
    app.bot_data.setdefault("active_trades", [])
    app.bot_data.setdefault("trade_cooldown", {})
    app.bot_data['broadcast_func'] = broadcast
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True, 'rateLimit': 200})
    
    last_scan_time = 0
    last_flush_time = 0
    FLUSH_INTERVAL = 15

    while app.bot_data.get("bot_on", False):
        try:
            current_time = time.time()
            
            if current_time - last_scan_time >= CONFIG.SCANNER_INTERVAL_SECONDS:
                log.info(f"--- Running Market Scan (every {CONFIG.SCANNER_INTERVAL_SECONDS // 60} mins) ---")
                await find_trade_signals(exchange, app)
                last_scan_time = current_time
                log.info("--- Scan Finished ---")

            await monitor_active_trades(exchange, app)
            
            if current_time - last_flush_time >= FLUSH_INTERVAL:
                await flush_log_buffers()
                last_flush_time = current_time
            
            await asyncio.sleep(CONFIG.TICK_MONITOR_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            log.info("Main loop cancelled.")
            break
        except Exception as e:
            log.error(f"Error in main loop: {e}", exc_info=True)
            await asyncio.sleep(30)
    
    await flush_log_buffers()
    await exchange.close()
    log.info("Scanner Engine loop stopped.")
