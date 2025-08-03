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

from trade_executor import log_open_trade, update_closed_trade, flush_log_buffers

log = logging.getLogger("swing_bot_engine")

# ===========================================================================
# CONFIGURATION
# ===========================================================================
class CONFIG:
    TIMEFRAME = "15m"
    POSITION_SIZE_USDT = 10.0
    LEVERAGE = 20
    MAX_CONCURRENT_POSITIONS = 10
    MAX_TRADES_PER_SCAN = 3 # ИЗМЕНЕНО: Увеличен лимит для гибкости
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
    STOCH_EXIT_ZONE = 60
    ATR_PERIOD = 14
    ATR_SPIKE_MULT = 2.5
    ATR_COOLDOWN_BARS = 2
    ATR_SL_MULT = 1.8
    RISK_REWARD = 2
    SL_MIN_PCT = 1.0
    SL_MAX_PCT = 5.0
    SCANNER_INTERVAL_SECONDS = 300
    TICK_MONITOR_INTERVAL_SECONDS = 15
    OHLCV_LIMIT = 250

# ===========================================================================
# HELPERS & RISK MANAGEMENT
# ===========================================================================
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
async def filter_volatile_pairs(exchange: ccxt.Exchange) -> List[str]:
    # ... без изменений ...
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

def check_entry_conditions(df: pd.DataFrame) -> Optional[str]:
    # ... без изменений ...
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
    # ... без изменений ...
    bot_data = app.bot_data
    if len(bot_data.get("active_trades", [])) >= CONFIG.MAX_CONCURRENT_POSITIONS:
        log.info("Position limit reached. Skipping scan."); return
    volatile_pairs = await filter_volatile_pairs(exchange)
    if not volatile_pairs: return
    sem = asyncio.Semaphore(8)
    async def safe_fetch_ohlcv(symbol):
        async with sem:
            try:
                return await exchange.fetch_ohlcv(symbol, CONFIG.TIMEFRAME, limit=CONFIG.OHLCV_LIMIT)
            except Exception:
                return None
    tasks = [safe_fetch_ohlcv(symbol) for symbol in volatile_pairs]
    log.info(f"Fetching OHLCV data for {len(volatile_pairs)} pairs concurrently...")
    ohlcv_results = await asyncio.gather(*tasks, return_exceptions=True)
    log.info("Finished fetching OHLCV data. Processing results...")
    opened_trades_this_scan = 0
    for i, ohlcv in enumerate(ohlcv_results):
        if opened_trades_this_scan >= CONFIG.MAX_TRADES_PER_SCAN:
            log.info(f"Reached max trades per scan ({CONFIG.MAX_TRADES_PER_SCAN}). Halting.")
            break
        symbol = volatile_pairs[i]
        try:
            if time.time() - bot_data.get("trade_cooldown", {}).get(symbol, 0) < tf_seconds(CONFIG.TIMEFRAME) * 2: continue
            if isinstance(ohlcv, Exception) or not ohlcv or len(ohlcv) < 50:
                continue
            if len(bot_data.get("active_trades", [])) >= CONFIG.MAX_CONCURRENT_POSITIONS:
                log.info("Position limit reached during signal processing. Halting."); break
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            if time.time() * 1000 - df['timestamp'].iloc[-1] < tf_seconds(CONFIG.TIMEFRAME) * 1000:
                df = df.iloc[:-1]
            if len(df) < CONFIG.EMA_TREND_PERIOD: continue
            if df.iloc[-1]['close'] < CONFIG.MIN_PRICE: continue
            df.ta.ema(length=CONFIG.EMA_FAST_PERIOD, append=True); df.ta.ema(length=CONFIG.EMA_SLOW_PERIOD, append=True)
            df.ta.ema(length=CONFIG.EMA_TREND_PERIOD, append=True)
            df.ta.stochrsi(length=CONFIG.STOCH_RSI_PERIOD, k=CONFIG.STOCH_RSI_K, d=CONFIG.STOCH_RSI_D, append=True)
            df.ta.atr(length=CONFIG.ATR_PERIOD, append=True)
            atr_col = next((c for c in df.columns if c.startswith("ATR") and c.endswith(str(CONFIG.ATR_PERIOD))), None)
            if atr_col:
                df['range'] = df['high'] - df['low']
                df['is_spike'] = df['range'] > df[atr_col] * CONFIG.ATR_SPIKE_MULT
                if df['is_spike'].iloc[-1] or df['is_spike'].iloc[-CONFIG.ATR_COOLDOWN_BARS:].any(): continue
            side = check_entry_conditions(df.copy())
            if side:
                if any(t["Pair"] == symbol for t in bot_data.get("active_trades", [])): continue
                atr_last = df[atr_col].iloc[-1] if atr_col else 0
                await open_new_trade(symbol, side, df.iloc[-1]['close'], atr_last, app)
                opened_trades_this_scan += 1
        except Exception as e:
            log.error(f"Error processing symbol {symbol}: {e}")

async def open_new_trade(symbol: str, side: str, entry_price: float, atr_last: float, app: Application):
    # ... без изменений ...
    bot_data = app.bot_data
    sl_price, tp_price, sl_pct = atr_based_levels(entry_price, atr_last, side)
    tp_pct = sl_pct * CONFIG.RISK_REWARD
    trade = {
        "Signal_ID": f"{symbol}_{int(time.time())}", "Pair": symbol, "Side": side,
        "Entry_Price": entry_price, "SL_Price": sl_price, "TP_Price": tp_price,
        "Status": "ACTIVE", "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "sl_pct": sl_pct, "tp_pct": tp_pct,
    }
    bot_data.setdefault("active_trades", []).append(trade)
    log.info(f"New trade signal: {trade}")
    if broadcast := bot_data.get('broadcast_func'):
        sl_disp = round(sl_pct * CONFIG.LEVERAGE); tp_disp = round(tp_pct * CONFIG.LEVERAGE)
        msg = (f"🔥 <b>НОВЫЙ СИГНАЛ ({side})</b>\n\n"
               f"<b>Пара:</b> {symbol}\n<b>Вход:</b> <code>{entry_price:.4f}</code>\n"
               f"<b>SL:</b> <code>{sl_price:.4f}</code> (-{sl_disp}%)\n"
               f"<b>TP:</b> <code>{tp_price:.4f}</code> (+{tp_disp}%)")
        await broadcast(app, msg)
    await log_open_trade(trade)

# ===========================================================================
# TRADE MANAGER
# ===========================================================================
async def monitor_active_trades(exchange: ccxt.Exchange, app: Application):
    """ИЗМЕНЕНО: Убрано лишнее логирование."""
    bot_data = app.bot_data
    active_trades = bot_data.get("active_trades", [])
    if not active_trades: return

    trades_to_close = []
    
    symbols = [t['Pair'] for t in active_trades]
    sem = asyncio.Semaphore(8)
    async def safe_fetch_ohlcv(symbol):
        async with sem:
            try:
                return await exchange.fetch_ohlcv(symbol, CONFIG.TIMEFRAME, limit=CONFIG.OHLCV_LIMIT)
            except Exception:
                return None
    tasks = [safe_fetch_ohlcv(s) for s in symbols]
    ohlcv_results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, trade in enumerate(active_trades):
        try:
            ohlcv = ohlcv_results[i]
            # ИЗМЕНЕНО: Убрано лишнее предупреждение
            if isinstance(ohlcv, Exception) or not ohlcv:
                continue

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df_indicators = df.copy()

            if len(df_indicators) < CONFIG.EMA_TREND_PERIOD: continue

            df_indicators.ta.ema(length=CONFIG.EMA_FAST_PERIOD, append=True); df_indicators.ta.ema(length=CONFIG.EMA_SLOW_PERIOD, append=True)
            df_indicators.ta.ema(length=CONFIG.EMA_TREND_PERIOD, append=True)
            df_indicators.ta.stochrsi(length=CONFIG.STOCH_RSI_PERIOD, k=CONFIG.STOCH_RSI_K, d=CONFIG.STOCH_RSI_D, append=True)

            last = df_indicators.iloc[-1]
            prev = df_indicators.iloc[-2]
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
                if trade['Side'] == 'LONG':
                    progress_to_tp = (current_price - trade['Entry_Price']) / (trade['TP_Price'] - trade['Entry_Price'])
                else:
                    progress_to_tp = (trade['Entry_Price'] - current_price) / (trade['Entry_Price'] - trade['TP_Price'])

                if progress_to_tp < 0.5:
                    continue

                ema_fast = last[f"EMA_{CONFIG.EMA_FAST_PERIOD}"]; ema_slow = last[f"EMA_{CONFIG.EMA_SLOW_PERIOD}"]
                k_now = last[stoch_k_col]; k_prev = prev[stoch_k_col]
                d_now = last[stoch_d_col]; d_prev = prev[stoch_d_col]
                
                if trade['Side'] == 'LONG':
                    if ema_fast < ema_slow: exit_reason = "INVALIDATION_EMA_CROSS"
                    elif k_prev > d_prev and k_now <= d_now and k_now < CONFIG.STOCH_EXIT_ZONE:
                        exit_reason = "INVALIDATION_STOCH_RSI"
                else: # SHORT
                    if ema_fast > ema_slow: exit_reason = "INVALIDATION_EMA_CROSS"
                    elif k_prev < d_prev and k_now >= d_now and k_now > (100 - CONFIG.STOCH_EXIT_ZONE):
                        exit_reason = "INVALIDATION_STOCH_RSI"

            if exit_reason:
                trades_to_close.append((trade, exit_reason, current_price))

        except Exception as e:
            log.error(f"Error monitoring trade for {trade['Pair']}: {e}", exc_info=True)

    if trades_to_close:
        # ... без изменений ...
        broadcast = app.bot_data.get('broadcast_func')
        for trade, reason, exit_price in trades_to_close:
            pnl_pct_raw = ((exit_price - trade['Entry_Price']) / trade['Entry_Price']) * (1 if trade['Side'] == "LONG" else -1)
            pnl_display = pnl_pct_raw * 100 * CONFIG.LEVERAGE
            if reason == "STOP_LOSS": pnl_display = -trade['sl_pct'] * CONFIG.LEVERAGE
            if reason == "TAKE_PROFIT": pnl_display = trade['tp_pct'] * CONFIG.LEVERAGE
            pnl_usd = CONFIG.POSITION_SIZE_USDT * pnl_display / 100
            app.bot_data.setdefault("trade_cooldown", {})[trade['Pair']] = time.time()
            if broadcast:
                emoji = "✅" if pnl_usd > 0 else ("❌" if pnl_usd < 0 else "⚪️")
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
    # ... без изменений ...
    log.info("Scanner Engine loop starting…")
    app.bot_data.setdefault("active_trades", [])
    app.bot_data.setdefault("trade_cooldown", {})
    app.bot_data['broadcast_func'] = broadcast
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
                await flush_log_buffers()
                last_flush_time = current_time
            
            await asyncio.sleep(CONFIG.TICK_MONITOR_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            log.info("Main loop cancelled."); break
        except Exception as e:
            log.error(f"Error in main loop: {e}", exc_info=True)
            await asyncio.sleep(30)
    
    await flush_log_buffers()
    await exchange.close()
    log.info("Scanner Engine loop stopped.")
