# scanner_engine.py

import asyncio
import time
import logging
import numpy as np
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from telegram.ext import Application

import trade_executor

log = logging.getLogger("swing_bot_engine")

# ===========================================================================
# CONFIGURATION
# ===========================================================================
class CONFIG:
    MARKET_REGIME_FILTER = True
    MARKET_REGIME_CACHE_TTL_SECONDS = 1800 # ИЗМЕНЕНО: Константа восстановлена
    TIMEFRAME = "15m"
    POSITION_SIZE_USDT = 10.0
    LEVERAGE = 20
    MAX_CONCURRENT_POSITIONS = 10
    MAX_TRADES_PER_SCAN = 4
    MIN_VOL_USD = 200_000
    RISK_SCALE = POSITION_SIZE_USDT / 2
    MIN_PRICE = 0.001
    EMA_FAST_PERIOD = 9
    EMA_SLOW_PERIOD = 21
    EMA_TREND_PERIOD = 200
    EMA_TREND_BUFFER_PCT = 0.5
    STOCH_RSI_PERIOD = 14
    STOCH_RSI_K = 3
    STOCH_RSI_D = 3
    STOCH_RSI_MID = 50
    STOCH_OVERBOUGHT_LEVEL = 70
    STOCH_OVERSOLD_LEVEL = 30
    ATR_PERIOD = 14
    ATR_SPIKE_MULT = 2.5
    ATR_COOLDOWN_BARS = 3
    ATR_SL_MULT = 1.8
    RISK_REWARD = 2
    SL_MIN_PCT = 1.0
    SL_MAX_PCT = 5.0
    SCANNER_INTERVAL_SECONDS = 300
    TICK_MONITOR_INTERVAL_SECONDS = 15
    OHLCV_LIMIT = 250
    CONCURRENCY_SEMAPHORE = 8

# ===========================================================================
# HELPERS & MARKET REGIME
# ===========================================================================
# ... без изменений ...
def format_price(price: float) -> str:
    if price < 0.01: return f"{price:.6f}"
    elif price < 1.0: return f"{price:.5f}"
    else: return f"{price:.4f}"

async def get_market_regime(exchange: ccxt.Exchange, app: Application) -> Optional[bool]:
    bot_data = app.bot_data
    now = time.time()
    cache = bot_data.get("market_regime_cache")
    if cache and (now - cache.get('timestamp', 0) < CONFIG.MARKET_REGIME_CACHE_TTL_SECONDS):
        log.info("Using cached Market Regime.")
        return cache.get('regime')
    log.info("Fetching new Market Regime from exchange...")
    regime = await _is_bull_market_uncached(exchange)
    bot_data["market_regime_cache"] = {'regime': regime, 'timestamp': now}
    return regime

async def _is_bull_market_uncached(exchange: ccxt.Exchange) -> Optional[bool]:
    try:
        btc_ohlcv = await exchange.fetch_ohlcv("BTC/USDT:USDT", "4h", limit=250)
        df = pd.DataFrame(btc_ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
        df["ema200"] = ta.ema(df["c"], length=200)
        df.dropna(inplace=True)
        last_price = df["c"].iloc[-1]
        last_ema = df["ema200"].iloc[-1]
        ema_slope = df["ema200"].diff().iloc[-5:].mean()
        if last_price > last_ema and ema_slope > 0: return True
        if last_price < last_ema and ema_slope < 0: return False
        return None
    except Exception as e:
        log.error(f"Could not determine market regime: {e}")
        return None

def tf_seconds(tf: str) -> int:
    unit = tf[-1].lower(); n = int(tf[:-1])
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
# ... без изменений ...
def check_entry_conditions(df: pd.DataFrame) -> Optional[str]:
    if len(df) < 2: return None
    last = df.iloc[-1]
    ema_fast = last[f"EMA_{CONFIG.EMA_FAST_PERIOD}"]
    ema_slow = last[f"EMA_{CONFIG.EMA_SLOW_PERIOD}"]
    ema_trend = last[f"EMA_{CONFIG.EMA_TREND_PERIOD}"]
    stoch_k_col = next((c for c in df.columns if c.startswith("STOCHRSIk_")), None)
    if not stoch_k_col: return None
    k_now = last[stoch_k_col]
    k_prev = df[stoch_k_col].iloc[-2]
    buffer = 1 + CONFIG.EMA_TREND_BUFFER_PCT / 100
    is_uptrend = last['close'] > ema_trend * buffer
    is_downtrend = last['close'] < ema_trend / buffer
    if is_uptrend and ema_fast > ema_slow and k_prev < CONFIG.STOCH_RSI_MID and k_now >= CONFIG.STOCH_RSI_MID:
        return "LONG"
    if is_downtrend and ema_fast < ema_slow and k_prev > CONFIG.STOCH_RSI_MID and k_now <= CONFIG.STOCH_RSI_MID:
        return "SHORT"
    return None

async def find_trade_signals(exchange: ccxt.Exchange, app: Application) -> None:
    bot_data = app.bot_data
    if len(bot_data.get("active_trades", [])) >= CONFIG.MAX_CONCURRENT_POSITIONS:
        log.info("Position limit reached. Skipping scan."); return
    
    market_is_bull = None
    if CONFIG.MARKET_REGIME_FILTER:
        market_is_bull = await get_market_regime(exchange, app)
        if market_is_bull is True: log.info("Market Regime: BULL. Penalizing SHORT signals.")
        elif market_is_bull is False: log.info("Market Regime: BEAR. Penalizing LONG signals.")
        else: log.info("Market Regime: NEUTRAL/FLAT. All signals allowed.")

    try:
        tickers = await exchange.fetch_tickers()
        liquid_pairs = [ s for s, t in tickers.items() if (t.get('quoteVolume') or 0) > CONFIG.MIN_VOL_USD and exchange.market(s).get('type') == 'swap' and s.endswith("USDT:USDT") ]
        log.info(f"Found {len(liquid_pairs)} liquid pairs.")
    except Exception as e:
        log.error(f"Could not fetch tickers or filter by volume: {e}"); return
    if not liquid_pairs: return

    long_candidates, short_candidates = [], []
    sem = asyncio.Semaphore(CONFIG.CONCURRENCY_SEMAPHORE)
    async def safe_fetch_ohlcv(symbol):
        async with sem:
            try: return await exchange.fetch_ohlcv(symbol, CONFIG.TIMEFRAME, limit=CONFIG.OHLCV_LIMIT)
            except Exception: return None
    tasks = [safe_fetch_ohlcv(symbol) for symbol in liquid_pairs]
    ohlcv_results = await asyncio.gather(*tasks)
    
    for i, ohlcv in enumerate(ohlcv_results):
        symbol = liquid_pairs[i]
        try:
            if time.time() - bot_data.get("trade_cooldown", {}).get(symbol, 0) < tf_seconds(CONFIG.TIMEFRAME) * 2: continue
            if not ohlcv or len(ohlcv) < CONFIG.EMA_TREND_PERIOD: continue
            if any(t["Pair"] == symbol for t in bot_data.get("active_trades", [])): continue
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            if time.time() * 1000 - df['timestamp'].iloc[-1] < tf_seconds(CONFIG.TIMEFRAME) * 1000: df = df.iloc[:-1]
            if df.empty or df.iloc[-1]['close'] < CONFIG.MIN_PRICE: continue
            df.ta.ema(length=CONFIG.EMA_FAST_PERIOD, append=True); df.ta.ema(length=CONFIG.EMA_SLOW_PERIOD, append=True)
            df.ta.ema(length=CONFIG.EMA_TREND_PERIOD, append=True)
            df.ta.stochrsi(length=CONFIG.STOCH_RSI_PERIOD, k=CONFIG.STOCH_RSI_K, d=CONFIG.STOCH_RSI_D, append=True)
            df.ta.atr(length=CONFIG.ATR_PERIOD, append=True)
            atr_col = next((c for c in df.columns if c.startswith("ATR")), None)
            if not atr_col or (df['high'] - df['low']).tail(CONFIG.ATR_COOLDOWN_BARS).max() > df[atr_col].tail(10).mean() * CONFIG.ATR_SPIKE_MULT: continue

            side = check_entry_conditions(df.copy())
            if side:
                entry_price = df.iloc[-1]['close']
                atr = df[atr_col].iloc[-1]
                if atr == 0: continue
                _, tp_price, sl_pct = atr_based_levels(entry_price, atr, side)
                edge = max(abs(tp_price - entry_price) / atr, 1.0)
                risk_usd_raw = (sl_pct / 100) * CONFIG.POSITION_SIZE_USDT * CONFIG.LEVERAGE
                risk_norm = np.tanh(risk_usd_raw / CONFIG.RISK_SCALE)
                quote_volume = tickers.get(symbol, {}).get('quoteVolume') or CONFIG.MIN_VOL_USD
                score = (0.45 * np.log10(quote_volume) - 0.25 * risk_norm + 0.30 * edge)
                if side == "SHORT" and market_is_bull is True: score *= 0.6
                if side == "LONG" and market_is_bull is False: score *= 0.6
                candidate_data = {'symbol': symbol, 'side': side, 'entry_price': entry_price, 'atr': atr, 'score': score}
                if side == "LONG": long_candidates.append(candidate_data)
                else: short_candidates.append(candidate_data)
        except Exception as e:
            log.error(f"Error processing symbol {symbol}: {e}")
            
    long_cand_sorted = sorted(long_candidates, key=lambda x: x['score'], reverse=True)
    short_cand_sorted = sorted(short_candidates, key=lambda x: x['score'], reverse=True)
    n_long = min(len(long_cand_sorted), CONFIG.MAX_TRADES_PER_SCAN // 2)
    n_short = min(len(short_cand_sorted), CONFIG.MAX_TRADES_PER_SCAN - n_long)
    selected_candidates = long_cand_sorted[:n_long] + short_cand_sorted[:n_short]
    remaining_slots = CONFIG.MAX_TRADES_PER_SCAN - len(selected_candidates)
    if remaining_slots > 0:
        remaining_pool = sorted(long_cand_sorted[n_long:] + short_cand_sorted[n_short:], key=lambda x: x['score'], reverse=True)
        selected_candidates.extend(remaining_pool[:remaining_slots])

    opened_long, opened_short = 0, 0
    trades_left_to_open = CONFIG.MAX_CONCURRENT_POSITIONS - len(bot_data.get("active_trades", []))
    
    for candidate in selected_candidates[:trades_left_to_open]:
        await open_new_trade(candidate['symbol'], candidate['side'], candidate['entry_price'], candidate['atr'], app)
        if candidate['side'] == "LONG": opened_long += 1
        else: opened_short += 1

    if long_candidates or short_candidates:
        msg = (f"🔍 <b>SCAN ({CONFIG.TIMEFRAME})</b>\n\n"f"Найдено сигналов: LONG-<b>{len(long_candidates)}</b> | SHORT-<b>{len(short_candidates)}</b>\n"f"Открыто (лучшие по score): LONG-<b>{opened_long}</b> | SHORT-<b>{opened_short}</b>")
        if broadcast := app.bot_data.get('broadcast_func'):
            await broadcast(app, msg)

async def open_new_trade(symbol: str, side: str, entry_price: float, atr_last: float, app: Application):
    bot_data = app.bot_data
    sl_price, tp_price, sl_pct = atr_based_levels(entry_price, atr_last, side)
    tp_pct = sl_pct * CONFIG.RISK_REWARD
    trade = {"Signal_ID": f"{symbol}_{int(time.time())}", "Pair": symbol, "Side": side, "Entry_Price": entry_price, "SL_Price": sl_price, "TP_Price": tp_price, "Status": "ACTIVE", "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "sl_pct": sl_pct, "tp_pct": tp_pct}
    bot_data.setdefault("active_trades", []).append(trade)
    log.info(f"New trade signal: {trade}")
    if broadcast := app.bot_data.get('broadcast_func'):
        sl_disp = round(sl_pct * CONFIG.LEVERAGE); tp_disp = round(tp_pct * CONFIG.LEVERAGE)
        msg = (f"🔥 <b>НОВЫЙ СИГНАЛ ({side})</b>\n\n"
               f"<b>Пара:</b> {symbol}\n<b>Вход:</b> <code>{format_price(entry_price)}</code>\n"
               f"<b>SL:</b> <code>{format_price(sl_price)}</code> (-{sl_disp}%)\n"
               f"<b>TP:</b> <code>{format_price(tp_price)}</code> (+{tp_disp}%)")
        await broadcast(app, msg)
    await trade_executor.log_open_trade(trade)

async def monitor_active_trades(exchange: ccxt.Exchange, app: Application):
    bot_data = app.bot_data
    active_trades = bot_data.get("active_trades", [])
    if not active_trades: return
    trades_to_close = []
    symbols = [t['Pair'] for t in active_trades]
    sem = asyncio.Semaphore(CONFIG.CONCURRENCY_SEMAPHORE)
    async def safe_fetch_ohlcv(symbol):
        async with sem:
            try: return await exchange.fetch_ohlcv(symbol, CONFIG.TIMEFRAME, limit=CONFIG.OHLCV_LIMIT)
            except Exception: return None
    tasks = [safe_fetch_ohlcv(s) for s in symbols]
    ohlcv_results = await asyncio.gather(*tasks)
    for i, trade in enumerate(active_trades):
        try:
            ohlcv = ohlcv_results[i]
            if not ohlcv: continue
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df_indicators = df.copy()
            if len(df_indicators) < CONFIG.EMA_TREND_PERIOD: continue
            df_indicators.ta.ema(length=CONFIG.EMA_FAST_PERIOD, append=True); df_indicators.ta.ema(length=CONFIG.EMA_SLOW_PERIOD, append=True)
            df_indicators.ta.ema(length=CONFIG.EMA_TREND_PERIOD, append=True)
            df_indicators.ta.stochrsi(length=CONFIG.STOCH_RSI_PERIOD, k=CONFIG.STOCH_RSI_K, d=CONFIG.STOCH_RSI_D, append=True)
            last = df_indicators.iloc[-1]; prev = df_indicators.iloc[-2]
            current_price = df.iloc[-1]['close']
            exit_reason = None
            stoch_k_col = next((c for c in df_indicators.columns if c.startswith("STOCHRSIk_")), None)
            stoch_d_col = next((c for c in df_indicators.columns if c.startswith("STOCHRSId_")), None)
            if not stoch_k_col or not stoch_d_col: continue
            if trade['Side'] == 'LONG':
                if current_price <= trade['SL_Price']: exit_reason = "STOP_LOSS"
                elif current_price >= trade['TP_Price']: exit_reason = "TAKE_PROFIT"
            else:
                if current_price >= trade['SL_Price']: exit_reason = "STOP_LOSS"
                elif current_price <= trade['TP_Price']: exit_reason = "TAKE_PROFIT"
            if not exit_reason:
                if trade['Side'] == 'LONG': progress_to_tp = (current_price - trade['Entry_Price']) / (trade['TP_Price'] - trade['Entry_Price'])
                else: progress_to_tp = (trade['Entry_Price'] - current_price) / (trade['Entry_Price'] - trade['TP_Price'])
                if progress_to_tp < 0.25: continue
                ema_fast = last[f"EMA_{CONFIG.EMA_FAST_PERIOD}"]; ema_slow = last[f"EMA_{CONFIG.EMA_SLOW_PERIOD}"]
                k_now = last[stoch_k_col]; k_prev = prev[stoch_k_col]
                d_now = last[stoch_d_col]; d_prev = prev[stoch_d_col]
                if trade['Side'] == 'LONG':
                    if ema_fast < ema_slow: exit_reason = "INVALIDATION_EMA_CROSS"
                    elif k_prev > d_prev and k_now <= d_now and k_now < CONFIG.STOCH_OVERBOUGHT_LEVEL:
                        exit_reason = "INVALIDATION_STOCH_RSI"
                else: # SHORT
                    if ema_fast > ema_slow: exit_reason = "INVALIDATION_EMA_CROSS"
                    # ИЗМЕНЕНО: Исправлена логика выхода для SHORT на симметричную
                    elif k_prev < d_prev and k_now >= d_now and k_now > CONFIG.STOCH_OVERBOUGHT_LEVEL:
                        exit_reason = "INVALIDATION_STOCH_RSI"
            if exit_reason: trades_to_close.append((trade, exit_reason, current_price))
        except Exception as e:
            log.error(f"Error monitoring trade for {trade['Pair']}: {e}", exc_info=True)
    if trades_to_close:
        broadcast = app.bot_data.get('broadcast_func')
        for trade, reason, exit_price in trades_to_close:
            pnl_pct_raw = ((exit_price - trade['Entry_Price']) / trade['Entry_Price']) * (1 if trade['Side'] == "LONG" else -1)
            pnl_display = pnl_pct_raw * 100 * CONFIG.LEVERAGE
            if reason == "STOP_LOSS": pnl_display = -trade['sl_pct'] * CONFIG.LEVERAGE
            if reason == "TAKE_PROFIT": pnl_display = trade['tp_pct'] * CONFIG.LEVERAGE
            pnl_usd = CONFIG.POSITION_SIZE_USDT * pnl_display / 100
            app.bot_data.setdefault("trade_cooldown", {})[trade['Pair']] = time.time()
            if broadcast:
                emoji = {"STOP_LOSS":"❌", "TAKE_PROFIT":"✅"}.get(reason, "⚠️")
                msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({reason})</b>\n\n"
                       f"<b>Пара:</b> {trade['Pair']}\n"
                       f"<b>Выход:</b> <code>{format_price(exit_price)}</code>\n"
                       f"<b>Результат: ${pnl_usd:+.2f} ({pnl_display:+.2f}%)</b>")
                await broadcast(app, msg)
            await trade_executor.update_closed_trade(trade['Signal_ID'], "CLOSED", exit_price, pnl_usd, pnl_display, reason)
        closed_ids = {t['Signal_ID'] for t, _, _ in trades_to_close}
        bot_data["active_trades"] = [t for t in active_trades if t['Signal_ID'] not in closed_ids]

async def scanner_main_loop(app: Application, broadcast):
    log.info("Scanner Engine loop starting…")
    app.bot_data.setdefault("active_trades", [])
    app.bot_data.setdefault("trade_cooldown", {})
    app.bot_data.setdefault("market_regime_cache", {})
    app.bot_data['broadcast_func'] = broadcast
    try:
        import gspread
        gc = gspread.service_account() 
        sheet = gc.open_by_key("YOUR_GOOGLE_SHEET_KEY_HERE")
        trade_executor.TRADE_LOG_WS = sheet.worksheet("Trading_Log")
        trade_executor.get_headers(trade_executor.TRADE_LOG_WS)
        log.info("Google Sheets initialized successfully.")
    except Exception as e:
        log.critical(f"Could not initialize Google Sheets: {e}")
        return
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True, 'rateLimit': 200})
    last_scan_time = 0; last_flush_time = 0; FLUSH_INTERVAL = 15
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
                await trade_executor.flush_log_buffers()
                last_flush_time = current_time
            await asyncio.sleep(CONFIG.TICK_MONITOR_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            log.info("Main loop cancelled."); break
        except Exception as e:
            log.error(f"Error in main loop: {e}", exc_info=True)
            await asyncio.sleep(30)
    await trade_executor.flush_log_buffers()
    await exchange.close()
    log.info("Scanner Engine loop stopped.")
