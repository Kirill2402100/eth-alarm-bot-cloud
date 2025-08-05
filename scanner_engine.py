# scanner_engine.py

import asyncio
import time
import logging
import numpy as np
import os
import json
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from telegram.ext import Application
import gspread

import trade_executor

log = logging.getLogger("swing_bot_engine")

# ===========================================================================
# CONFIGURATION
# ===========================================================================
class CONFIG:
    MARKET_REGIME_FILTER = True
    MARKET_REGIME_CACHE_TTL_SECONDS = 1800
    TIMEFRAME = "15m"
    POSITION_SIZE_USDT = 10.0
    LEVERAGE = 20
    MAX_CONCURRENT_POSITIONS = 10
    MAX_TRADES_PER_SCAN = 4
    MIN_VOL_USD = 700_000
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
    RISK_REWARD = 1.0
    SL_MIN_PCT = 1.0
    SL_MAX_PCT = 5.0
    SCANNER_INTERVAL_SECONDS = 300
    TICK_MONITOR_INTERVAL_SECONDS = 15
    OHLCV_LIMIT = 250
    CONCURRENCY_SEMAPHORE = 8
    MAX_FUNDING_RATE_PCT = 0.075

# ===========================================================================
# HELPERS
# ===========================================================================

async def ensure_new_log_sheet(gfile: gspread.Spreadsheet):
    """Создаёт лист 'Trading_Log_v2', обеспечивая безопасное завершение при ошибках."""
    loop = asyncio.get_running_loop()
    title = "Trading_Log_v2"
    ws = None
    try:
        ws = await loop.run_in_executor(None, lambda: gfile.worksheet(title))
        log.info(f"Worksheet '{title}' already exists.")
    except gspread.WorksheetNotFound:
        log.warning(f"Worksheet '{title}' not found. Creating...")
        try:
            # ИЗМЕНЕНО: Увеличен лимит колонок до 30
            ws = await loop.run_in_executor(None, lambda: gfile.add_worksheet(title=title, rows=1000, cols=30))
            headers = [
                "Signal_ID", "Timestamp_UTC", "Pair", "Side", "Status",
                "Entry_Price", "Exit_Price", "Exit_Time_UTC",
                "Exit_Reason", "PNL_USD", "PNL_Percent",
                "MFE_Price", "MFE_ATR", "MFE_TP_Pct", "Time_in_Trade"
            ]
            await loop.run_in_executor(None, lambda: ws.append_row(headers, value_input_option="USER_ENTERED"))
            log.info(f"Worksheet '{title}' created with all required headers.")
        except Exception as e:
            log.critical(f"Failed to create new worksheet '{title}': {e}")
            raise
    except Exception as e:
        log.critical(f"An unexpected error occurred with Google Sheets: {e}")
        raise

    trade_executor.TRADE_LOG_WS = ws
    trade_executor.TRADING_HEADERS_CACHE = None

async def is_daily_bearish(symbol: str, exchange: ccxt.Exchange) -> bool:
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, "1d", limit=201)
        if len(ohlcv) < 201: return False
        df = pd.DataFrame(ohlcv, columns=['ts','o','h','l','c','v'])
        df["ema200"] = ta.ema(df["c"], length=200)
        df.dropna(inplace=True)
        if df.empty: return False
        last = df.iloc[-1]
        return last["c"] < last["ema200"] and last["c"] < last["o"]
    except Exception:
        # Не логируем ошибку, чтобы не спамить, просто считаем тренд неопределенным
        return False

def format_price(price: float) -> str:
    if price < 0.01: return f"{price:.6f}"
    elif price < 1.0: return f"{price:.5f}"
    else: return f"{price:.4f}"
async def get_market_regime(exchange: ccxt.Exchange, app: Application) -> Optional[bool]:
    bot_data = app.bot_data
    now = time.time()
    cache = bot_data.get("market_regime_cache")
    if cache and (now - cache.get('timestamp', 0) < CONFIG.MARKET_REGIME_CACHE_TTL_SECONDS):
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

# ===========================================================================
# MARKET SCANNER & TRADE MANAGER
# ===========================================================================
# ИЗМЕНЕНО: Функция переработана для параллельной проверки дневного тренда
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
        liquid_pairs = [s for s, t in tickers.items() if (t.get('quoteVolume') or 0) > CONFIG.MIN_VOL_USD and exchange.market(s).get('type') == 'swap' and s.endswith("USDT:USDT")]
        log.info(f"Found {len(liquid_pairs)} liquid pairs.")
    except Exception as e:
        log.error(f"Could not fetch tickers or filter by volume: {e}"); return
    if not liquid_pairs: return
    
    sem = asyncio.Semaphore(CONFIG.CONCURRENCY_SEMAPHORE)
    async def safe_fetch_ohlcv(symbol):
        async with sem:
            try: return await exchange.fetch_ohlcv(symbol, CONFIG.TIMEFRAME, limit=CONFIG.OHLCV_LIMIT)
            except Exception: return None
    
    tasks = [safe_fetch_ohlcv(symbol) for symbol in liquid_pairs]
    ohlcv_results = await asyncio.gather(*tasks)
    
    # --- Шаг 1: Первичный отбор кандидатов ---
    pre_long_candidates, pre_short_candidates = [], []
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
                candidate_data = {'symbol': symbol, 'side': side, 'df': df}
                if side == "LONG":
                    if market_is_bull is not False: pre_long_candidates.append(candidate_data)
                else: # SHORT
                    if market_is_bull is not True: pre_short_candidates.append(candidate_data)
        except Exception as e:
            log.error(f"Error pre-processing symbol {symbol}: {e}")

    # --- Шаг 2: Параллельная проверка доп. фильтров ---
    # Для LONG: проверка funding rate
    final_long_candidates = []
    for cand in pre_long_candidates:
        try:
            funding_rate_data = await exchange.fetch_funding_rate(cand['symbol'])
            funding_rate = float(funding_rate_data.get('fundingRate', 0))
            if funding_rate * 100 <= CONFIG.MAX_FUNDING_RATE_PCT:
                final_long_candidates.append(cand)
            else:
                log.info(f"Skipping LONG for {cand['symbol']} due to high funding rate: {funding_rate*100:.4f}%")
        except (KeyError, TypeError, ValueError) as e:
            log.warning(f"Could not fetch or parse funding rate for {cand['symbol']}, skipping: {e}")

    # Для SHORT: проверка дневного тренда
    daily_check_tasks = [is_daily_bearish(c['symbol'], exchange) for c in pre_short_candidates]
    daily_results = await asyncio.gather(*daily_check_tasks)
    final_short_candidates = []
    for i, cand in enumerate(pre_short_candidates):
        if daily_results[i]:
            final_short_candidates.append(cand)
        else:
            log.info(f"Skip SHORT {cand['symbol']}: daily trend is not decisively bearish.")

    # --- Шаг 3: Финальный скоринг и отбор ---
    all_candidates = []
    for cand in final_long_candidates + final_short_candidates:
        try:
            df = cand['df']
            atr_col = next((c for c in df.columns if c.startswith("ATR")), None)
            entry_price = df.iloc[-1]['close']
            atr = df[atr_col].iloc[-1]
            if atr == 0: continue
            _, tp_price, sl_pct = atr_based_levels(entry_price, atr, cand['side'])
            edge = max(abs(tp_price - entry_price) / atr, 1.0)
            risk_usd_raw = (sl_pct / 100) * CONFIG.POSITION_SIZE_USDT * CONFIG.LEVERAGE
            risk_norm = np.tanh(risk_usd_raw / CONFIG.RISK_SCALE)
            quote_volume = tickers.get(cand['symbol'], {}).get('quoteVolume') or CONFIG.MIN_VOL_USD
            # ИЗМЕНЕНО: Вес объема в скоринге снижен
            score = (0.35 * np.log10(quote_volume) - 0.25 * risk_norm + 0.30 * edge)
            all_candidates.append({'symbol': cand['symbol'], 'side': cand['side'], 'entry_price': entry_price, 'atr': atr, 'score': score})
        except Exception as e:
            log.error(f"Error scoring candidate {cand['symbol']}: {e}")

    long_cand_sorted = sorted([c for c in all_candidates if c['side'] == 'LONG'], key=lambda x: x['score'], reverse=True)
    short_cand_sorted = sorted([c for c in all_candidates if c['side'] == 'SHORT'], key=lambda x: x['score'], reverse=True)
    
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
    if pre_long_candidates or pre_short_candidates:
        msg = (f"🔍 <b>SCAN ({CONFIG.TIMEFRAME})</b>\n\n"f"Найдено сигналов: LONG-<b>{len(long_cand_sorted)}</b> | SHORT-<b>{len(short_cand_sorted)}</b>\n"f"Открыто (лучшие по score): LONG-<b>{opened_long}</b> | SHORT-<b>{opened_short}</b>")
        if broadcast := app.bot_data.get('broadcast_func'):
            await broadcast(app, msg)

async def open_new_trade(symbol: str, side: str, entry_price: float, atr_last: float, app: Application):
    bot_data = app.bot_data
    sl_price, tp_price, sl_pct = atr_based_levels(entry_price, atr_last, side)
    sl_shift_pct = sl_pct * 0.5 / 100
    tp_pct = sl_pct * CONFIG.RISK_REWARD
    trade = {
        "Signal_ID": f"{symbol}_{int(time.time())}", "Pair": symbol, "Side": side,
        "Entry_Price": entry_price, "SL_Price": sl_price, "TP_Price": tp_price,
        "Status": "ACTIVE", "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "sl_pct": sl_pct, "tp_pct": tp_pct, "ATR_Entry": atr_last, "MFE_Price": entry_price,
        "SL_shift_pct": sl_shift_pct,
        "sl_shifted": False
    }
    bot_data.setdefault("active_trades", []).append(trade)
    log.info(f"New trade signal: {trade}")
    if broadcast := app.bot_data.get('broadcast_func'):
        sl_disp = round(sl_pct * CONFIG.LEVERAGE); tp_disp = round(tp_pct * CONFIG.LEVERAGE)
        msg = (f"🔥 <b>НОВЫЙ СИГНАЛ ({side})</b>\n\n"f"<b>Пара:</b> {symbol}\n<b>Вход:</b> <code>{format_price(entry_price)}</code>\n"f"<b>SL:</b> <code>{format_price(sl_price)}</code> (-{sl_disp}%)\n"f"<b>TP:</b> <code>{format_price(tp_price)}</code> (+{tp_disp}%)")
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
    broadcast = app.bot_data.get('broadcast_func')
    for i, trade in enumerate(active_trades):
        try:
            ohlcv = ohlcv_results[i]
            if not ohlcv: continue
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            current_price = df.iloc[-1]['close']
            if trade['Side'] == 'LONG':
                trade['MFE_Price'] = max(trade.get('MFE_Price', current_price), current_price)
            else:
                trade['MFE_Price'] = min(trade.get('MFE_Price', current_price), current_price)

            if not trade.get("sl_shifted"):
                progress = 0
                tp_diff = trade['TP_Price'] - trade['Entry_Price']
                if abs(tp_diff) > 0:
                    progress = (current_price - trade['Entry_Price']) / tp_diff if trade['Side'] == 'LONG' else \
                               (trade['Entry_Price'] - current_price) / abs(tp_diff)
                if progress >= 0.50:
                    if trade['Side'] == 'LONG':
                        trade['SL_Price'] = trade['Entry_Price'] * (1 + trade['SL_shift_pct'])
                    else:
                        trade['SL_Price'] = trade['Entry_Price'] * (1 - trade['SL_shift_pct'])
                    trade['sl_shifted'] = True
                    log.info(f"SL moved to +0.5R for {trade['Pair']}. New SL: {trade['SL_Price']}")

            df_indicators = df.copy()
            if len(df_indicators) < CONFIG.EMA_TREND_PERIOD: continue
            df_indicators.ta.ema(length=CONFIG.EMA_FAST_PERIOD, append=True); df_indicators.ta.ema(length=CONFIG.EMA_SLOW_PERIOD, append=True)
            df_indicators.ta.ema(length=CONFIG.EMA_TREND_PERIOD, append=True)
            df_indicators.ta.stochrsi(length=CONFIG.STOCH_RSI_PERIOD, k=CONFIG.STOCH_RSI_K, d=CONFIG.STOCH_RSI_D, append=True)
            last = df_indicators.iloc[-1]; prev = df_indicators.iloc[-2]
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
                ema_fast = last[f"EMA_{CONFIG.EMA_FAST_PERIOD}"]; ema_slow = last[f"EMA_{CONFIG.EMA_SLOW_PERIOD}"]
                k_now = last[stoch_k_col]; k_prev = prev[stoch_k_col]
                d_now = last[stoch_d_col]; d_prev = prev[stoch_d_col]
                if trade['Side'] == 'LONG':
                    if ema_fast < ema_slow: exit_reason = "INVALIDATION_EMA_CROSS"
                    elif k_prev > d_prev and k_now <= d_now and k_now < CONFIG.STOCH_OVERBOUGHT_LEVEL: exit_reason = "INVALIDATION_STOCH_RSI"
                else:
                    if ema_fast > ema_slow: exit_reason = "INVALIDATION_EMA_CROSS"
                    elif k_prev < d_prev and k_now >= d_now and k_now > CONFIG.STOCH_OVERSOLD_LEVEL: exit_reason = "INVALIDATION_STOCH_RSI"
            
            if exit_reason: trades_to_close.append((trade, exit_reason, df_indicators))
        except Exception as e:
            log.error(f"Error monitoring trade for {trade['Pair']}: {e}", exc_info=True)
            
    if trades_to_close:
        for trade, reason, df_final in trades_to_close:
            exit_price = df_final.iloc[-1]['close']
            
            # ИЗМЕНЕНО: Исправлен баг с расчетом PnL для STOP_LOSS
            if reason == "TAKE_PROFIT":
                pnl_display = trade['tp_pct'] * CONFIG.LEVERAGE
            else: # STOP_LOSS или INVALIDATION
                pnl_pct_raw = ((exit_price - trade['Entry_Price']) / trade['Entry_Price']) * (1 if trade['Side'] == "LONG" else -1)
                pnl_display = pnl_pct_raw * 100 * CONFIG.LEVERAGE
            
            pnl_usd = CONFIG.POSITION_SIZE_USDT * pnl_display / 100
            
            app.bot_data.setdefault("trade_cooldown", {})[trade['Pair']] = time.time()
            mfe_atr, mfe_tp_pct = 0, 0
            df_final.ta.atr(length=CONFIG.ATR_PERIOD, append=True)
            atr_col = next((c for c in df_final.columns if c.startswith("ATR")), None)
            current_atr = df_final[atr_col].iloc[-1] if atr_col and not pd.isna(df_final[atr_col].iloc[-1]) else trade.get("ATR_Entry")
            if current_atr and current_atr > 0: mfe_atr = abs(trade['MFE_Price'] - trade['Entry_Price']) / current_atr
            tp_diff = abs(trade['TP_Price'] - trade['Entry_Price'])
            if tp_diff > 0: mfe_tp_pct = abs(trade['MFE_Price'] - trade['Entry_Price']) / tp_diff
            else: mfe_tp_pct = 0
            
            time_in_trade_str = "N/A"
            try:
                FMT = '%Y-%m-%d %H:%M:%S'
                t_entry = datetime.strptime(trade['Timestamp_UTC'], FMT).replace(tzinfo=timezone.utc)
                t_exit = datetime.now(timezone.utc)
                time_in_trade = t_exit - t_entry
                days = time_in_trade.days
                hours, remainder = divmod(time_in_trade.seconds, 3600)
                minutes, _ = divmod(remainder, 60)
                if days > 0: time_in_trade_str = f"{days}d {hours}h {minutes}m"
                else: time_in_trade_str = f"{hours}h {minutes}m"
            except (ValueError, KeyError) as e:
                log.warning(f"Could not calculate Time-in-Trade: {e}")
            
            extra_fields = {
                "MFE_Price": trade['MFE_Price'], "MFE_ATR": round(mfe_atr, 2),
                "MFE_TP_Pct": round(mfe_tp_pct, 3), "Time_in_Trade": time_in_trade_str
            }
            if broadcast:
                emoji = {"STOP_LOSS":"❌", "TAKE_PROFIT":"✅"}.get(reason, "⚠️")
                mfe_info = f"MFE: {mfe_tp_pct:.1%} of TP ({mfe_atr:.1f} ATR)"
                msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({reason})</b>\n\n"f"<b>Пара:</b> {trade['Pair']}\n"f"<b>Выход:</b> <code>{format_price(exit_price)}</code>\n"f"<b>Результат: ${pnl_usd:+.2f} ({pnl_display:+.2f}%)</b>\n"f"<i>{mfe_info}</i>")
                await broadcast(app, msg)
            await trade_executor.update_closed_trade(trade['Signal_ID'], "CLOSED", exit_price, pnl_usd, pnl_display, reason, extra_fields=extra_fields)
        closed_ids = {t['Signal_ID'] for t, _, _ in trades_to_close}
        bot_data["active_trades"] = [t for t in active_trades if t['Signal_ID'] not in closed_ids]

# ===========================================================================
# MAIN LOOP
# ===========================================================================
async def scanner_main_loop(app: Application, broadcast):
    log.info("Scanner Engine loop starting…")
    app.bot_data.setdefault("active_trades", [])
    app.bot_data.setdefault("trade_cooldown", {})
    app.bot_data.setdefault("market_regime_cache", {})
    app.bot_data['broadcast_func'] = broadcast
    try:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        sheet_key = os.environ.get("SHEET_ID")
        if not creds_json or not sheet_key:
            log.critical("GOOGLE_CREDENTIALS or SHEET_ID environment variables not set. Cannot start.")
            return
        creds_dict = json.loads(creds_json)
        gc = gspread.service_account_from_dict(creds_dict)
        sheet = gc.open_by_key(sheet_key)
        await ensure_new_log_sheet(sheet)
        trade_executor.get_headers(trade_executor.TRADE_LOG_WS)
        log.info("Google Sheets initialized successfully.")
    except Exception as e:
        log.critical(f"Could not initialize Google Sheets during startup: {e}", exc_info=True)
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
