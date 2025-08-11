from __future__ import annotations
import asyncio, time, logging, json, os
from datetime import datetime, timezone
from typing import Optional, Tuple, List, Dict

import numpy as np
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from telegram.ext import Application
import gspread

import trade_executor

log = logging.getLogger("wick_spike_engine")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
class CONFIG:
    TIMEFRAME = "3m"
    POSITION_SIZE_USDT = 10.0
    LEVERAGE = 20
    MAX_CONCURRENT_POSITIONS = 10
    
    # Параметры производительности
    CONCURRENCY_SEMAPHORE = 12
    FETCH_TIMEOUT = 8
    CHUNK_SIZE = 60
    TIME_BUDGET_SEC = 58 # ПАТЧ: Увеличен бюджет времени
    SCAN_TOP_N_SYMBOLS = 300

    MIN_QUOTE_VOLUME_USD = 150_000
    MIN_PRICE = 0.001
    SCAN_INTERVAL_SECONDS = 5

    # Параметры признаков и гейтов под 3m
    ATR_PERIOD = 5
    VOL_WINDOW = 17
    GATE_WICK_RATIO = 1.7
    GATE_ATR_SPIKE_MULT = 1.4
    GATE_VOL_Z = 1.2

    # SL/TP
    ENTRY_TAIL_FRACTION = 0.3
    SL_PCT = 0.35
    TP_PCT = 0.70

    # Адаптивный порог
    SCORE_BASE = 1.55
    SCORE_MIN = 1.35
    SCORE_MAX = 2.4
    SCORE_ADAPT_STEP = 0.1
    TARGET_SIGNALS_PER_SCAN = 1
    MAX_TRADES_PER_SCAN = 2
    SYMBOL_COOLDOWN_SEC = 180

    ATR_SL_MULT = 0
    RISK_REWARD = TP_PCT / SL_PCT if SL_PCT > 0 else 0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# ПАТЧ: Хелпер больше не вызывает load_markets() и использует безопасную конвертацию времени
async def fetch_ohlcv_tf(exchange: ccxt.Exchange, symbol: str, tf: str, limit: int, supported_timeframes: dict | None = None):
    """
    Загружает OHLCV. Если ТФ не поддерживается, ресемплит из 1m.
    Использует переданный кэш поддерживаемых таймфреймов.
    """
    supported = supported_timeframes or (exchange.timeframes or {})
    
    if tf == "3m" and "3m" not in supported and "1m" in supported:
        raw = await asyncio.wait_for(
            exchange.fetch_ohlcv(symbol, "1m", limit=limit * 3 + 5),
            timeout=CONFIG.FETCH_TIMEOUT
        )
        if not raw: return []
        
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = (df.set_index("dt")
              .resample("3T", label="left", closed="left")
              .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
              .dropna()
              .reset_index())
        # Безопасная конвертация времени
        df["ts"] = (df["dt"].astype("int64") // 1_000_000).astype(np.int64)
        return df[["ts", "open", "high", "low", "close", "volume"]].tail(limit).values.tolist()
    
    return await asyncio.wait_for(exchange.fetch_ohlcv(symbol, tf, limit=limit), timeout=CONFIG.FETCH_TIMEOUT)

# ... (ensure_new_log_sheet, format_price, tf_seconds остаются без изменений) ...
async def ensure_new_log_sheet(gfile: gspread.Spreadsheet):
    loop = asyncio.get_running_loop()
    title = "Trading_Log_v2"
    required_headers = [
        "Signal_ID", "Timestamp_UTC", "Pair", "Side", "Status",
        "Entry_Price", "Exit_Price", "Exit_Time_UTC",
        "Exit_Reason", "PNL_USD", "PNL_Percent",
        "Score", "Score_Threshold", "Wick_Ratio", "Spike_ATR", "Vol_Z",
        "Dist_Mean", "HTF_Slope", "BTC_ctx",
        "MFE_Price", "MAE_Price", "MFE_pct", "MAE_pct", "Time_to_TP_SL"
    ]
    ws = None
    try:
        ws = await loop.run_in_executor(None, lambda: gfile.worksheet(title))
        current_headers = await loop.run_in_executor(None, lambda: ws.row_values(1))
        if current_headers != required_headers:
            archive_title = f"{title}_archive_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
            await loop.run_in_executor(None, lambda: ws.update_title(archive_title))
            ws = await loop.run_in_executor(None, lambda: gfile.add_worksheet(title=title, rows=1000, cols=len(required_headers)))
            await loop.run_in_executor(None, lambda: ws.append_row(required_headers, value_input_option="USER_ENTERED"))
    except gspread.exceptions.WorksheetNotFound:
        ws = await loop.run_in_executor(None, lambda: gfile.add_worksheet(title=title, rows=1000, cols=len(required_headers)))
        await loop.run_in_executor(None, lambda: ws.append_row(required_headers, value_input_option="USER_ENTERED"))
    trade_executor.TRADE_LOG_WS = ws
    trade_executor.clear_headers_cache()

def format_price(p: float) -> str:
    if p < 0.01: return f"{p:.6f}"
    if p < 1.0: return f"{p:.5f}"
    return f"{p:.4f}"

def tf_seconds(tf: str) -> int:
    n, unit = int(tf[:-1]), tf[-1].lower()
    return n * (60 if unit == "m" else 3600 if unit == "h" else 86400)


# ---------------------------------------------------------------------------
# Core loops
# ---------------------------------------------------------------------------
async def scanner_main_loop(app: Application, broadcast):
    log.info("Wick-Spike loop starting…")
    app.bot_data.setdefault("active_trades", [])
    app.bot_data.setdefault("symbol_cooldown", {})
    app.bot_data['broadcast_func'] = broadcast
    if 'score_threshold' not in app.bot_data:
        app.bot_data['score_threshold'] = CONFIG.SCORE_BASE
    
    try:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        sheet_key  = os.environ.get("SHEET_ID")
        if creds_json and sheet_key:
            gc = gspread.service_account_from_dict(json.loads(creds_json))
            sheet = gc.open_by_key(sheet_key)
            await ensure_new_log_sheet(sheet)
    except Exception as e:
        log.error(f"Sheets init error: {e}", exc_info=True)

    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True, 'rateLimit': 150})
    
    # ПАТЧ: Загружаем рынки и кэшируем таймфреймы один раз при старте
    await exchange.load_markets(True)
    app.bot_data['supported_timeframes'] = (exchange.timeframes or {}).copy()
    log.info("Рынки и поддерживаемые таймфреймы загружены и закэшированы.")

    last_flush = 0
    scan_triggered_for_ts = 0
    TF_SECONDS = tf_seconds(CONFIG.TIMEFRAME)

    while app.bot_data.get("bot_on", False):
        log.info("Wick-Spike loop heartbeat…")
        try:
            current_time = time.time()
            if not app.bot_data.get("scan_paused", False):
                current_candle_start_ts = int(current_time // TF_SECONDS) * TF_SECONDS
                if current_candle_start_ts > scan_triggered_for_ts:
                    log.info(f"New {CONFIG.TIMEFRAME} candle detected. Wick-Scan start: thr={app.bot_data.get('score_threshold', CONFIG.SCORE_BASE):.2f}")
                    await _run_scan(exchange, app)
                    scan_triggered_for_ts = current_candle_start_ts
            
            await _monitor_trades(exchange, app)
            if trade_executor.PENDING_TRADES and current_time - last_flush >= 15:
                await trade_executor.flush_log_buffers()
                last_flush = current_time
            await asyncio.sleep(CONFIG.SCAN_INTERVAL_SECONDS)
        except Exception as e:
            log.exception("FATAL in main loop")
            await asyncio.sleep(5)
    await exchange.close()

# ---------------------------------------------------------------------------
async def _run_scan(exchange: ccxt.Exchange, app: Application):
    bt = app.bot_data
    if len(bt.get("active_trades", [])) >= CONFIG.MAX_CONCURRENT_POSITIONS:
        return

    # Получаем кэш таймфреймов
    supported_tfs = bt.get('supported_timeframes', {})

    try:
        tickers = await exchange.fetch_tickers()
        liquid_tickers = [t for t in tickers.values() if (t.get('quoteVolume') or 0) > CONFIG.MIN_QUOTE_VOLUME_USD and exchange.market(t['symbol']).get("type") == "swap" and t['symbol'].endswith("USDT:USDT") and "UP" not in t['symbol'] and "DOWN" not in t['symbol']]
        liquid_tickers.sort(key=lambda x: x['quoteVolume'], reverse=True)
        top_n_symbols = {t['symbol'] for t in liquid_tickers[:CONFIG.SCAN_TOP_N_SYMBOLS]}
        top_n_symbols.add("BTC/USDT:USDT")
        liquid = list(top_n_symbols)
        log.info(f"Wick-Scan: получено {len(tickers)} пар; отобрано топ-{len(liquid)} по объему.")
    except Exception as e:
        log.warning(f"ticker fetch failed: {e}")
        return

    try:
        # ПАТЧ: Используем хелпер с переданным кэшем
        btc_ohlcv = await fetch_ohlcv_tf(exchange, "BTC/USDT:USDT", CONFIG.TIMEFRAME, 10, supported_tfs)
        btc_df = pd.DataFrame(btc_ohlcv, columns=["ts","open","high","low","close","volume"])
        if btc_df.empty: btc_df = None
    except Exception as e:
        log.warning(f"BTC fetch failed ({e}). Continuing without BTC context.")
        btc_df = None

    sem = asyncio.Semaphore(CONFIG.CONCURRENCY_SEMAPHORE)
    limit_tf, limit_5m = CONFIG.VOL_WINDOW + CONFIG.ATR_PERIOD + 2, 50 + 6
    
    # ПАТЧ: fetch_data теперь тоже использует кэш таймфреймов
    async def fetch_data(symbol):
        async with sem:
            try:
                ohlc_tf = await fetch_ohlcv_tf(exchange, symbol, CONFIG.TIMEFRAME, limit_tf, supported_tfs)
                ohlc_5m = await asyncio.wait_for(exchange.fetch_ohlcv(symbol, '5m', limit=limit_5m), timeout=CONFIG.FETCH_TIMEOUT)
                return symbol, {"tf": ohlc_tf, "5m": ohlc_5m}
            except Exception:
                return symbol, None

    scan_started = time.time()
    candidates, opened_count = [], 0
    now = time.time()
    TF_SECONDS = tf_seconds(CONFIG.TIMEFRAME)
    current_candle_start_ts = int(now // TF_SECONDS) * TF_SECONDS
    cooldown = bt.get("symbol_cooldown", {})
    if int(now) % 300 == 0:
        bt["symbol_cooldown"] = {s:t for s,t in cooldown.items() if t > now}

    for i in range(0, len(liquid), CONFIG.CHUNK_SIZE):
        if time.time() - scan_started > CONFIG.TIME_BUDGET_SEC:
            log.warning("Scan aborted – time budget exceeded")
            break
        
        chunk = liquid[i:i+CONFIG.CHUNK_SIZE]
        all_data = await asyncio.gather(*[fetch_data(s) for s in chunk])
        
        gate_passed_symbols = []
        for symbol, data_dict in all_data:
            if not data_dict or not data_dict.get('tf'): continue
            if cooldown.get(symbol, 0) > now: continue
            df_tf = pd.DataFrame(data_dict["tf"], columns=["ts","open","high","low","close","volume"])
            if df_tf.empty or len(df_tf) < 2: continue
            last_idx = -1
            if df_tf.iloc[-1]["ts"] == current_candle_start_ts * 1000: last_idx = -2
            last = df_tf.iloc[last_idx]
            if last["ts"] < (current_candle_start_ts - TF_SECONDS) * 1000: continue
            if last["close"] < CONFIG.MIN_PRICE: continue
            df_tf["atr"] = ta.atr(df_tf["high"], df_tf["low"], df_tf["close"], length=CONFIG.ATR_PERIOD)
            vol_mu = df_tf["volume"].rolling(CONFIG.VOL_WINDOW).mean()
            vol_sigma = df_tf["volume"].rolling(CONFIG.VOL_WINDOW).std().replace(0, 1e-9)
            df_tf["vol_z"] = (df_tf["volume"] - vol_mu) / vol_sigma
            if pd.isna(df_tf.iloc[last_idx]["atr"]) or pd.isna(df_tf.iloc[last_idx]["vol_z"]): continue
            h, l, o, c = last['high'], last['low'], last['open'], last['close']
            body = max(abs(c-o), 1e-9)
            wick_ratio = max(h - max(o, c), min(o, c) - l) / body
            spike_atr_mult = (h - l) / max(df_tf.iloc[last_idx]["atr"], 1e-9)
            vol_z = df_tf.iloc[last_idx]["vol_z"]

            if (wick_ratio >= CONFIG.GATE_WICK_RATIO and spike_atr_mult >= CONFIG.GATE_ATR_SPIKE_MULT and vol_z >= CONFIG.GATE_VOL_Z):
                gate_passed_symbols.append({'symbol': symbol, 'df_tf': df_tf, 'df_5m': pd.DataFrame(data_dict["5m"], columns=["ts","open","high","low","close","volume"]), 'last_idx': last_idx, 'gate_features': {'wick_ratio': wick_ratio, 'spike_atr_mult': spike_atr_mult, 'vol_z': vol_z}})
        
        if not gate_passed_symbols:
            log.info(f"Scan progress: {min(i + CONFIG.CHUNK_SIZE, len(liquid))}/{len(liquid)} symbols, no gates passed.")
            continue
            
        log.info(f"Chunk progress: {len(gate_passed_symbols)} symbols passed gate checks. Now calculating score...")

        for item in gate_passed_symbols:
            try:
                symbol, df_tf, df_5m, li, gf = item["symbol"], item["df_tf"], item["df_5m"], item["last_idx"], item["gate_features"]
                last, c = df_tf.iloc[li], df_tf.iloc[li]["close"]
                df_tf["ema20"] = ta.ema(df_tf["close"], length=20)
                dist_from_mean = abs(c - df_tf.iloc[li]["ema20"]) / max(df_tf.iloc[li]["atr"], 1e-9) if pd.notna(df_tf.iloc[li]["ema20"]) else 0
                ema50_m5 = ta.ema(df_5m["close"], length=50)
                atr_m5 = ta.atr(df_5m["high"], df_5m["low"], df_5m["close"], length=14)
                htf_m5_slope = (ema50_m5.iloc[-1] - ema50_m5.iloc[-6]) / max(atr_m5.iloc[-1], 1e-9) if len(ema50_m5) > 6 else 0
                btc_ret_context = 0 if btc_df is None or btc_df.empty or len(btc_df) <= 6 else (btc_df["close"].iloc[-1] / btc_df["close"].iloc[-6]) - 1
                upper_wick, lower_wick = last["high"] - max(last["open"], c), min(last["open"], c) - last["low"]
                side = "SHORT" if upper_wick >= lower_wick else "LONG"
                btc_context_bonus = 0.0
                if side == "LONG" and btc_ret_context < -0.005: btc_context_bonus = -0.15
                if side == "SHORT" and btc_ret_context > 0.005: btc_context_bonus = -0.15

                score = (0.35 * min(max(gf["wick_ratio"] - 1.5, 0), 2.5) + 0.25 * min(max(gf["spike_atr_mult"] - 1.8, 0), 2.0) + 0.20 * min(max(gf["vol_z"] - 2.0, 0), 3.0) + 0.15 * min(dist_from_mean, 3.0) - 0.15 * min(abs(htf_m5_slope), 2.0) + 0.10 * btc_context_bonus)
                
                score_threshold = bt.get("score_threshold", CONFIG.SCORE_BASE)
                if score >= score_threshold:
                    candidates.append({'symbol': symbol, 'side': side, 'score': score, 'last_ohlc': {'o': last["open"], 'h': last["high"], 'l': last["low"], 'c': c}, 'features': {'Score': round(score, 2), 'Wick_Ratio': round(gf["wick_ratio"], 2), 'Spike_ATR': round(gf["spike_atr_mult"], 2), 'Vol_Z': round(gf["vol_z"], 2), 'Dist_Mean': round(dist_from_mean, 2), 'HTF_Slope': round(htf_m5_slope, 2), 'BTC_ctx': round(btc_ret_context * 100, 3)}})
            except Exception:
                log.exception(f"Score calculation for {item.get('symbol')} failed")

        log.info(f"Scan progress: {min(i + CONFIG.CHUNK_SIZE, len(liquid))}/{len(liquid)} symbols, found_so_far={len(candidates)}, thr={bt.get('score_threshold', 0):.2f}, elapsed={time.time()-scan_started:.1f}s")
    
    found_count = len(candidates)
    if found_count > 0:
        candidates.sort(key=lambda x: x['score'], reverse=True)
        for cand in candidates:
            if opened_count >= CONFIG.MAX_TRADES_PER_SCAN: break
            if len(bt.get("active_trades", [])) + opened_count >= CONFIG.MAX_CONCURRENT_POSITIONS: break
            if cand['symbol'] in bt.get("symbol_cooldown", {}): continue
            await _open_trade(cand, exchange, app)
            opened_count += 1
    
    current_threshold = bt.get('score_threshold')
    if found_count < CONFIG.TARGET_SIGNALS_PER_SCAN:
        bt['score_threshold'] = max(CONFIG.SCORE_MIN, current_threshold - CONFIG.SCORE_ADAPT_STEP)
    elif found_count > CONFIG.TARGET_SIGNALS_PER_SCAN * 2:
        bt['score_threshold'] = min(CONFIG.SCORE_MAX, current_threshold + CONFIG.SCORE_ADAPT_STEP)

    if found_count == 0 and opened_count == 0:
        log.info(f"Scan finished, no suitable signals found. New threshold: {bt['score_threshold']:.2f}")
    else:
        log.info(f"Scan finished: found {found_count} candidates, opened {opened_count}. New threshold: {bt['score_threshold']:.2f}")

# ... (Функция _open_trade остается без изменений) ...
async def _open_trade(candidate: dict, exchange: ccxt.Exchange, app: Application):
    bt = app.bot_data
    symbol, side, score = candidate['symbol'], candidate['side'], candidate['score']
    
    entry_tail_fraction, take_profit_pct = CONFIG.ENTRY_TAIL_FRACTION, CONFIG.TP_PCT
    score_threshold = bt.get('score_threshold', CONFIG.SCORE_BASE)
    if score >= score_threshold + 0.6: entry_tail_fraction = 0.35
    if score >= score_threshold + 0.8: take_profit_pct = 0.8
    else: take_profit_pct = CONFIG.TP_PCT

    last_ohlc = candidate['last_ohlc']
    o, h, l, c = last_ohlc['o'], last_ohlc['h'], last_ohlc['l'], last_ohlc['c']
    
    if side == "LONG":
        lower_tail = min(o, c) - l
        entry_price = l + entry_tail_fraction * lower_tail
    else: # SHORT
        upper_tail = h - max(o, c)
        entry_price = h - entry_tail_fraction * upper_tail

    sl_off, tp_off = CONFIG.SL_PCT / 100 * entry_price, take_profit_pct / 100 * entry_price
    sl, tp = (entry_price - sl_off, entry_price + tp_off) if side == "LONG" else (entry_price + sl_off, entry_price - tp_off)
    sl, tp, entry_price = float(exchange.price_to_precision(symbol, sl)), float(exchange.price_to_precision(symbol, tp)), float(exchange.price_to_precision(symbol, entry_price))

    trade = {
        "Signal_ID": f"{symbol}_{int(time.time())}", "Pair": symbol, "Side": side,
        "Entry_Price": entry_price, "SL_Price": sl, "TP_Price": tp, "Status": "ACTIVE", 
        "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        **candidate['features'], "Score_Threshold": round(score_threshold, 2),
        "MFE_Price": entry_price, "MAE_Price": entry_price
    }
    bt.setdefault("active_trades", []).append(trade)
    bt["symbol_cooldown"][symbol] = time.time() + CONFIG.SYMBOL_COOLDOWN_SEC
    log.info(f"Opened {side} {symbol} @ {entry_price} with score {trade['Score']:.2f}. Cooldown until {datetime.fromtimestamp(bt['symbol_cooldown'][symbol]).strftime('%H:%M:%S')}")

    if bc := bt.get('broadcast_func'):
        msg = (f"⚡ <b>Wick-Spike {side} (Score: {trade['Score']:.2f})</b>\n\n"
               f"<b>Пара:</b> {symbol}\n<b>Вход:</b> <code>{format_price(entry_price)}</code>\n"
               f"<b>SL:</b> <code>{format_price(sl)}</code> (-{CONFIG.SL_PCT:.2f}%)\n"
               f"<b>TP:</b> <code>{format_price(tp)}</code> (+{take_profit_pct:.2f}%)")
        await bc(app, msg)
    await trade_executor.log_open_trade(trade)

async def _monitor_trades(exchange: ccxt.Exchange, app: Application):
    bt = app.bot_data
    act = bt.get("active_trades", [])
    if not act: return
    
    supported_tfs = bt.get('supported_timeframes', {})
    sem = asyncio.Semaphore(CONFIG.CONCURRENCY_SEMAPHORE)
    async def fetch(symbol):
        async with sem:
            try:
                ohlc_list = await fetch_ohlcv_tf(exchange, symbol, CONFIG.TIMEFRAME, 2, supported_tfs)
                return symbol, ohlc_list[-1] if ohlc_list else None
            except Exception: return symbol, None

    latest_bars = dict(await asyncio.gather(*[fetch(t['Pair']) for t in act]))
    close_list: List[Tuple[dict,str,float]] = []

    for tr in act:
        bar = latest_bars.get(tr['Pair'])
        if bar is None: continue
        last_high, last_low = bar[2], bar[3]
        
        entry, mfe_price, mae_price = tr['Entry_Price'], tr.get('MFE_Price', tr['Entry_Price']), tr.get('MAE_Price', tr['Entry_Price'])
        if tr['Side'] == "LONG":
            tr['MFE_Price'], tr['MAE_Price'] = max(mfe_price, last_high), min(mae_price, last_low)
            if last_low <= tr['SL_Price']: close_list.append((tr, "STOP_LOSS", tr['SL_Price']))
            elif last_high >= tr['TP_Price']: close_list.append((tr, "TAKE_PROFIT", tr['TP_Price']))
        else: # SHORT
            tr['MFE_Price'], tr['MAE_Price'] = min(mfe_price, last_low), max(mae_price, last_high)
            if last_high >= tr['SL_Price']: close_list.append((tr, "STOP_LOSS", tr['SL_Price']))
            elif last_low <= tr['TP_Price']: close_list.append((tr, "TAKE_PROFIT", tr['TP_Price']))

    if not close_list: return

    for tr, reason, exit_p in close_list:
        tp_pct_actual = abs(tr['TP_Price'] / tr['Entry_Price'] - 1) * 100
        sl_pct_actual = abs(tr['SL_Price'] / tr['Entry_Price'] - 1) * 100
        pnl_pct = tp_pct_actual if reason == "TAKE_PROFIT" else -sl_pct_actual

        pnl_lever, pnl_usd = pnl_pct * CONFIG.LEVERAGE, CONFIG.POSITION_SIZE_USDT * pnl_pct * CONFIG.LEVERAGE / 100
        if bc := bt.get('broadcast_func'):
            emoji = "✅" if reason=="TAKE_PROFIT" else "❌"
            await bc(app, f"{emoji} <b>{reason}</b> {tr['Pair']} {pnl_lever:+.2f}% (price {format_price(exit_p)})")
        
        entry_time = datetime.strptime(tr['Timestamp_UTC'], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        time_to_close = (now_utc - entry_time).total_seconds() / 60
        
        mfe_pct = abs(tr['MFE_Price'] / tr['Entry_Price'] - 1) * 100
        mae_pct = abs(tr['MAE_Price'] / tr['Entry_Price'] - 1) * 100
        extra_fields = {"MFE_pct": round(mfe_pct, 2), "MAE_pct": round(mae_pct, 2), "Time_to_TP_SL": round(time_to_close, 2)}
        await trade_executor.update_closed_trade(tr['Signal_ID'], "CLOSED", exit_p, pnl_usd, pnl_lever, reason, extra_fields=extra_fields)

    closed_ids = {t['Signal_ID'] for t,_,_ in close_list}
    bt['active_trades'] = [t for t in act if t['Signal_ID'] not in closed_ids]
