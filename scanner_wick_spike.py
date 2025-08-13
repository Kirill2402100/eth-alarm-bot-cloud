from __future__ import annotations
import asyncio, time, logging, json, os
import contextlib
from datetime import datetime, timezone
from typing import Optional, Tuple, List, Dict
from collections import Counter

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
    TIMEFRAME = "1m"
    POSITION_SIZE_USDT = 10.0
    LEVERAGE = 20
    MAX_CONCURRENT_POSITIONS = 10
    
    CONCURRENCY_SEMAPHORE = 12
    FETCH_TIMEOUT = 8
    SCAN_CADENCE_SEC = 15
    CHUNK_SIZE = 60
    TIME_BUDGET_SEC = 75
    SCAN_TOP_N_SYMBOLS = 200

    MIN_QUOTE_VOLUME_USD = 150_000
    MIN_PRICE = 0.001
    SCAN_INTERVAL_SECONDS = 3

    ATR_PERIOD = 14
    VOL_WINDOW = 50
    GATE_WICK_RATIO = 1.5
    GATE_ATR_SPIKE_MULT = 1.5
    GATE_VOL_Z = 1.2
    GATE_BODY_ATR_MIN = 0.03
    GATE_ATR_SPIKE_MAX = 2.5
    # <<< ИЗМЕНЕНО: Добавляем XAUT в список исключений
    EXCLUDE_STABLE_BASES = {
        "USDC", "USDT", "BUSD", "FDUSD", "DAI", "TUSD", "USDD", "PYUSD",
        "USDE", "USDP", "GUSD", "USD+", "USDX", "EOSDT", "XAUT"
    }
    MAX_POSITIONS_PER_SYMBOL = 1

    HTF_STRONG_TREND = 1.0
    BTC_5M_TREND_BLOCK = 0.006
    # <<< ИЗМЕНЕНО: Смягчаем гейт для лонгов
    GATE_WICK_RATIO_LONG = 1.6
    GATE_ATR_SPIKE_MAX_LONG = 2.4

    ENTRY_TAIL_FRACTION = 0.3
    SL_PCT = 0.20
    TP_PCT = 0.40

    SCORE_BASE = 1.6
    SCORE_MIN = 1.4
    SCORE_MAX = 2.4
    SCORE_ADAPT_STEP = 0.1
    TARGET_SIGNALS_PER_SCAN = 1
    MAX_TRADES_PER_SCAN = 2
    SYMBOL_COOLDOWN_SEC = 120

    ATR_SL_MULT = 0.6
    RISK_REWARD = TP_PCT / SL_PCT if SL_PCT > 0 else 0

    THRESHOLD_MODE = "adaptive"
    THR_Q_MAIN = 0.98
    THR_Q_MED = 0.95
    THR_Q_SMALL = 0.90
    THR_PAD = 0.02
    THR_MIN_SAMPLES = 12
    THR_SMOOTH_ALPHA = 0.4
    THR_MAX_JUMP = 0.15


# ... (Функции ensure_new_log_sheet, format_price, tf_seconds остаются без изменений) ...
async def ensure_new_log_sheet(gfile: gspread.Spreadsheet):
    loop = asyncio.get_running_loop()
    title = "Trading_Log_v2"
    required_headers = [
        "Signal_ID", "Timestamp_UTC", "Pair", "Side", "Status",
        "Entry_Price", "Exit_Price", "Exit_Time_UTC",
        "Exit_Reason", "PNL_USD", "PNL_Percent",
        "Score", "Score_Threshold", "Wick_Ratio", "Spike_ATR", "Vol_Z",
        "Dist_Mean", "HTF_Slope", "BTC_5m", "ATR_last", "Entry_Bar_TS",
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
    app.bot_data.setdefault("scan_task", None)
    app.bot_data.setdefault("scan_offset", 0)
    app.bot_data.setdefault('last_thr_bump_at', 0)
    app.bot_data.setdefault('last_heartbeat_log', 0)
    app.bot_data.setdefault('stat_no_touch', 0)
    app.bot_data.setdefault("reserved_symbols", set())
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
    await exchange.load_markets(True) 
    last_flush = 0
    last_scan_time = 0

    while app.bot_data.get("bot_on", False):
        hb_key = "last_heartbeat_log"
        last_hb = app.bot_data.get(hb_key, 0)
        current_time = time.time()
        if current_time - last_hb >= 60:
            log.info("Wick-Spike loop heartbeat…")
            app.bot_data[hb_key] = current_time

        try:
            if not app.bot_data.get("scan_paused", False):
                t = app.bot_data.get("scan_task")
                if (not t or t.done()) and (current_time - last_scan_time >= CONFIG.SCAN_CADENCE_SEC):
                    log.info(f"Wick-Scan start: thr={app.bot_data.get('score_threshold', CONFIG.SCORE_BASE):.2f}")
                    app.bot_data["scan_task"] = asyncio.create_task(_run_scan(exchange, app))
                    last_scan_time = current_time
            
            await _monitor_trades(exchange, app)
            if trade_executor.PENDING_TRADES and current_time - last_flush >= 15:
                await trade_executor.flush_log_buffers()
                last_flush = current_time
            await asyncio.sleep(CONFIG.SCAN_INTERVAL_SECONDS)
        except Exception as e:
            log.exception("FATAL in main loop")
            await asyncio.sleep(5)

    task = app.bot_data.get("scan_task")
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(Exception):
            await task
    
    await exchange.close()

# ---------------------------------------------------------------------------
async def _run_scan(exchange: ccxt.Exchange, app: Application):
    bt = app.bot_data
    if len(bt.get("active_trades", [])) >= CONFIG.MAX_CONCURRENT_POSITIONS:
        return
    
    active_symbol_counts = Counter(t["Pair"] for t in bt.get("active_trades", []) if t.get("Status") == "ACTIVE")
    
    # <<< ИЗМЕНЕНО: Инициализация раздельных счётчиков
    bt['open_attempts'] = 0
    bt['open_success'] = 0
    
    total_passed_gate = 0
    all_scores = []

    def p90(xs):
        return "-" if not xs else round(np.percentile(xs, 90), 2)

    try:
        universe = [
            s for s, m in exchange.markets.items()
            if m.get("type") == "swap"
            and s.endswith("USDT:USDT")
            and "UP" not in s and "DOWN" not in s
            and (m.get("base","").upper() not in CONFIG.EXCLUDE_STABLE_BASES)
        ]

        tickers = await exchange.fetch_tickers()
        ranked = []
        for s in universe:
            t = tickers.get(s) or {}
            qv = t.get("quoteVolume") \
                or (t.get("info") or {}).get("quote_volume") \
                or ((t.get("baseVolume") or 0) * (t.get("last") or 0))
            vol = float(qv or 0)
            
            if vol > CONFIG.MIN_QUOTE_VOLUME_USD:
                 ranked.append((s, vol))

        ranked.sort(key=lambda x: x[1], reverse=True)
        
        liquid = [s for s, _ in ranked[:CONFIG.SCAN_TOP_N_SYMBOLS]]
        if "BTC/USDT:USDT" not in liquid:
            liquid.insert(0, "BTC/USDT:USDT")

        offset = bt.get("scan_offset", 0)
        liquid_rotated = liquid[offset:] + liquid[:offset]
        processed_symbols_count = 0
        
        log.info(f"Wick-Scan: найдено {len(universe)} своп-пар; отобрано топ-{len(liquid)} по объему. Начинаем с офсета {offset}")

    except Exception as e:
        log.warning(f"Ticker processing failed: {e}", exc_info=True)
        return

    try:
        btc_ohlcv = await exchange.fetch_ohlcv("BTC/USDT:USDT", '1m', limit=10)
        btc_df = pd.DataFrame(btc_ohlcv, columns=["ts","open","high","low","close","volume"])
    except Exception as e:
        log.warning(f"BTC fetch failed ({e}). Continue with neutral BTC context.")
        btc_df = None

    sem = asyncio.Semaphore(CONFIG.CONCURRENCY_SEMAPHORE)
    tot_scan = Counter()
    
    async def fetch_1m_data(symbol):
        async with sem:
            if symbol not in exchange.markets: return symbol, None
            try:
                return symbol, await asyncio.wait_for(
                    exchange.fetch_ohlcv(symbol, '1m', limit=CONFIG.VOL_WINDOW + CONFIG.ATR_PERIOD + 2),
                    timeout=CONFIG.FETCH_TIMEOUT)
            except Exception: return symbol, None
            
    async def fetch_5m_data(symbol):
        async with sem:
            if symbol not in exchange.markets: return symbol, None
            try:
                return symbol, await asyncio.wait_for(
                    exchange.fetch_ohlcv(symbol, '5m', limit=50 + 6), timeout=CONFIG.FETCH_TIMEOUT)
            except Exception: return symbol, None

    scan_started = time.time()
    
    if int(time.time()) % 60 < 5: 
        current_ts_for_cleanup = time.time()
        bt["symbol_cooldown"] = {s: until for s, until in bt.get("symbol_cooldown", {}).items() if until > current_ts_for_cleanup}

    for i in range(0, len(liquid_rotated), CONFIG.CHUNK_SIZE):
        now_ts = time.time()
        if now_ts - scan_started > CONFIG.TIME_BUDGET_SEC:
            log.warning("Scan aborted – time budget exceeded")
            break
        
        tot_chunk = Counter()
        near = {"wick": [], "atr": [], "vol": []}
        tot_chunk.update({"pass_WA":0, "pass_WV":0, "pass_AV":0})
        
        opened_syms_in_chunk = set()
        chunk_raw = liquid_rotated[i:i+CONFIG.CHUNK_SIZE]
        
        res = bt.get("reserved_symbols", set())
        chunk = [
            s for s in chunk_raw
            if active_symbol_counts.get(s, 0) < CONFIG.MAX_POSITIONS_PER_SYMBOL
            and s not in res
        ]
        if len(chunk) < len(chunk_raw):
            tot_chunk["already_active"] += (len(chunk_raw) - len(chunk))
        
        processed_symbols_count += len(chunk_raw)
        
        ohlcv_1m_data = await asyncio.gather(*[fetch_1m_data(s) for s in chunk])
        await asyncio.sleep(0)
        
        gate_passed_symbols = []
        for symbol, ohlcv in ohlcv_1m_data:
            need_len = max(CONFIG.VOL_WINDOW, CONFIG.ATR_PERIOD) + 2
            if not ohlcv or len(ohlcv) < need_len: 
                tot_chunk["no_ohlcv"] += 1; continue
            
            if bt.get("symbol_cooldown", {}).get(symbol, 0) > now_ts:
                tot_chunk["cooldown"] += 1; continue
            
            if active_symbol_counts.get(symbol, 0) >= CONFIG.MAX_POSITIONS_PER_SYMBOL:
                tot_chunk["already_active"] += 1; continue

            df_1m = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
            
            now_ms = time.time() * 1000
            use_last = df_1m.iloc[-1]
            if now_ms - use_last['ts'] < tf_seconds('1m') * 1000 * 0.95:
                if len(df_1m) < 2: continue
                use_last = df_1m.iloc[-2]
            
            last = use_last
            bar_ts = int(last['ts'])

            if last['close'] < CONFIG.MIN_PRICE:
                tot_chunk["min_price"] += 1; continue
            
            df_slice = df_1m[df_1m['ts'] <= bar_ts]
            if len(df_slice) < need_len - 1:
                tot_chunk["nan_ind"] += 1; continue

            last_atr = ta.atr(df_slice['high'], df_slice['low'], df_slice['close'], length=CONFIG.ATR_PERIOD).iloc[-1]
            vol_series = df_slice['volume'].rolling(CONFIG.VOL_WINDOW)
            vol_mu = vol_series.mean().iloc[-1]
            vol_sigma = vol_series.std().iloc[-1]
            last_vol = df_slice['volume'].iloc[-1]
            last_vol_z = (last_vol - vol_mu) / max(vol_sigma, 1e-9)

            if pd.isna(last_vol_z) or pd.isna(last_atr):
                tot_chunk["nan_ind"] += 1; continue
            
            h, l, o, c = last['high'], last['low'], last['open'], last['close']
            body = abs(c - o)
            safe_atr = max(float(last_atr), 1e-9)

            if (body / safe_atr) < CONFIG.GATE_BODY_ATR_MIN:
                tot_chunk["micro_body"] += 1; continue
            
            body_safe = max(body, 1e-9)
            
            wick_ratio = max(h - max(o, c), min(o, c) - l) / body_safe
            spike_atr_mult = (h - l) / safe_atr
            vol_z = last_vol_z
            upper_wick = h - max(o, c)
            side = "SHORT" if upper_wick >= (min(o, c) - l) else "LONG"

            if side == "LONG":
                if wick_ratio < CONFIG.GATE_WICK_RATIO_LONG:
                    tot_chunk["long_wick_reject"] += 1; continue
                if spike_atr_mult > CONFIG.GATE_ATR_SPIKE_MAX_LONG:
                    tot_chunk["long_atr_hi_reject"] += 1; continue
            
            pass_wick = (wick_ratio >= CONFIG.GATE_WICK_RATIO)
            pass_atr = (
                CONFIG.GATE_ATR_SPIKE_MULT <= spike_atr_mult <= CONFIG.GATE_ATR_SPIKE_MAX
            ) or (
                CONFIG.GATE_ATR_SPIKE_MULT * 0.95 <= spike_atr_mult <= CONFIG.GATE_ATR_SPIKE_MAX
                and wick_ratio >= CONFIG.GATE_WICK_RATIO + 0.3
            )

            pass_vol_hard = (vol_z >= CONFIG.GATE_VOL_Z)
            pass_vol_soft = (vol_z >= 0.9 and wick_ratio >= CONFIG.GATE_WICK_RATIO + 0.2)
            pass_vol = pass_vol_hard or pass_vol_soft

            tot_chunk["gate_wick_pass"] += int(pass_wick)
            tot_chunk["gate_atr_pass"]  += int(pass_atr)
            tot_chunk["gate_vol_pass"]  += int(pass_vol)
            
            if pass_wick and pass_atr: tot_chunk["pass_WA"] += 1
            if pass_wick and pass_vol: tot_chunk["pass_WV"] += 1
            if pass_atr and pass_vol:  tot_chunk["pass_AV"] += 1

            passes = int(pass_wick) + int(pass_atr) + int(pass_vol)
            if pass_atr and passes >= 2:
                tot_chunk["gate_all_pass"] += 1
                gate_passed_symbols.append({'symbol': symbol, 'df_1m': df_1m, 'bar_ts': bar_ts, 
                                            'gate_features': {'wick_ratio': wick_ratio, 'spike_atr_mult': spike_atr_mult, 
                                                              'vol_z': vol_z, 'atr': last_atr, 'passes': passes}})
            else:
                if not pass_wick: near["wick"].append(round(wick_ratio, 2))
                if not pass_atr:
                    near["atr"].append(round(spike_atr_mult, 2))
                    if spike_atr_mult > CONFIG.GATE_ATR_SPIKE_MAX:
                        tot_chunk["atr_too_high"] += 1
                    elif spike_atr_mult < CONFIG.GATE_ATR_SPIKE_MULT:
                        tot_chunk["atr_too_low"] += 1
                if not pass_vol:  near["vol"].append(round(vol_z, 2))
                tot_chunk["gate_all_fail"] += 1
                continue
        
        total_passed_gate += len(gate_passed_symbols)
        
        for k, v in tot_chunk.items():
            tot_scan[k] += v

        log.info(
            f"GateDiag ch={i//CONFIG.CHUNK_SIZE+1}: "
            f"ok_1m={sum(1 for _,o in ohlcv_1m_data if o)}/{len(chunk)} (cd={tot_chunk['cooldown']}, act={tot_chunk['already_active']}), "
            f"micro={tot_chunk['micro_body']}, "
            f"passes>=2(ATR_req)={tot_chunk['gate_all_pass']}, "
            f"combos(WA/WV/AV)={tot_chunk['pass_WA']}/{tot_chunk['pass_WV']}/{tot_chunk['pass_AV']}, "
            f"near_p90(wick={p90(near['wick'])} atr={p90(near['atr'])} vol={p90(near['vol'])})"
        )

        if not gate_passed_symbols:
            await asyncio.sleep(0)
            continue
            
        ohlcv_5m_data = dict(await asyncio.gather(*[fetch_5m_data(s['symbol']) for s in gate_passed_symbols]))
        await asyncio.sleep(0)
        
        top_scores = []

        for item in gate_passed_symbols:
            try:
                symbol, df_1m, bar_ts = item['symbol'], item['df_1m'], item['bar_ts']
                df_slice = df_1m[df_1m['ts'] <= bar_ts]
                
                htf_m5_slope, htf_penalty = 0, 0
                ohlcv_5m = ohlcv_5m_data.get(symbol)
                if ohlcv_5m:
                    df_5m = pd.DataFrame(ohlcv_5m, columns=["ts", "open", "high", "low", "close", "volume"])
                    if len(df_5m) >= 56:
                        ema50_m5 = ta.ema(df_5m['close'], length=50)
                        atr_m5 = ta.atr(df_5m['high'], df_5m['low'], df_5m['close'], length=14)
                        if pd.notna(ema50_m5.iloc[-1]) and pd.notna(ema50_m5.iloc[-6]) and pd.notna(atr_m5.iloc[-1]):
                            htf_m5_slope = (ema50_m5.iloc[-1] - ema50_m5.iloc[-6]) / max(float(atr_m5.iloc[-1]), 1e-9)
                        else: htf_penalty = 0.1
                    else: htf_penalty = 0.1
                else: htf_penalty = 0.1 

                gate_features = item['gate_features']
                wick_ratio, spike_atr_mult, vol_z, last_atr = gate_features['wick_ratio'], gate_features['spike_atr_mult'], gate_features['vol_z'], gate_features['atr']
                passes = gate_features.get('passes', 0)

                last = df_slice.iloc[-1]
                c = last['close']
                
                ema20_last = ta.ema(df_slice['close'], length=20).iloc[-1]
                if pd.isna(ema20_last): 
                    continue
                
                safe_atr = max(float(last_atr), 1e-9)
                dist_from_mean = abs(c - ema20_last) / safe_atr
                
                btc_ret_5m = 0 if (btc_df is None or len(btc_df) < 6) else (btc_df['close'].iloc[-1] / btc_df['close'].iloc[-6] - 1)
                upper_wick = last['high'] - max(last['open'], c)
                side = "SHORT" if upper_wick >= (min(last['open'], c) - last['low']) else "LONG"

                strong_ctr = (side == "LONG" and htf_m5_slope < -CONFIG.HTF_STRONG_TREND) or \
                             (side == "SHORT" and htf_m5_slope > CONFIG.HTF_STRONG_TREND)
                if strong_ctr:
                    tot_chunk["strong_ctr_reject"] += 1; continue

                btc_block = (side == "LONG" and btc_ret_5m < -CONFIG.BTC_5M_TREND_BLOCK) or \
                            (side == "SHORT" and btc_ret_5m > CONFIG.BTC_5M_TREND_BLOCK)
                if btc_block:
                    tot_chunk["btc_block_reject"] += 1; continue
                
                ctr = (side == "LONG" and htf_m5_slope < 0) or (side == "SHORT" and htf_m5_slope > 0)
                trend_term = (-0.25 * min(abs(htf_m5_slope), 2.0)) if ctr else (0.15 if abs(htf_m5_slope) > 0.3 else 0.0)

                btc_context_penalty = 0.0
                if side == "LONG" and btc_ret_5m < -0.005: btc_context_penalty = -0.12
                elif side == "SHORT" and btc_ret_5m > 0.005: btc_context_penalty = -0.12

                dist_penalty = -0.05 if dist_from_mean > 3.0 else 0.0
                
                score = (
                    0.60 * min(max(wick_ratio - 1.5, 0), 3.0) +
                    0.40 * min(max(spike_atr_mult - 1.5, 0), 2.5) +
                    trend_term +
                    btc_context_penalty +
                    dist_penalty -
                    htf_penalty +
                    0.10 * max(passes - 2, 0)
                ) - (0.1 if side == "LONG" else 0.0)
                
                top_scores.append((symbol, side, round(score,2)))
                
                final_thr = bt.get('score_threshold', CONFIG.SCORE_BASE)
                if side == "LONG":
                    final_thr += 0.10
                
                if score >= final_thr and symbol not in opened_syms_in_chunk:
                    if len(bt.get("active_trades", [])) + bt.get('open_attempts', 0) >= CONFIG.MAX_CONCURRENT_POSITIONS:
                        log.info(f"Skip open {symbol}: would exceed MAX_CONCURRENT_POSITIONS (reserved).")
                        continue
                    
                    res = bt.get("reserved_symbols", set())
                    if symbol in res:
                        continue
                    res.add(symbol)
                    
                    opened_syms_in_chunk.add(symbol)
                    cand = {'symbol': symbol, 'side': side, 'score': score, 
                            'last_ohlc': {'o': last['open'], 'h': last['high'], 'l': last['low'], 'c': c},
                            'bar_ts': bar_ts, 
                            'features': {'Score': round(score, 2), 'Wick_Ratio': round(wick_ratio, 2), 'Spike_ATR': round(spike_atr_mult, 2), 
                                         'Vol_Z': round(vol_z, 2), 'Dist_Mean': round(dist_from_mean, 2), 'HTF_Slope': round(htf_m5_slope, 2), 
                                         'BTC_5m': round(btc_ret_5m * 100, 3), 'ATR_last': last_atr}}
                    
                    bt['open_attempts'] = bt.get('open_attempts', 0) + 1
                    task = asyncio.create_task(_open_trade(cand, exchange, app))
                    
                    def _cleanup_reservation(t, sym=cand['symbol']):
                        bt["reserved_symbols"].discard(sym)
                        ex = t.exception()
                        if ex:
                            log.error(f"Error in background open_trade for {sym}",
                                      exc_info=(type(ex), ex, ex.__traceback__))
                    task.add_done_callback(_cleanup_reservation)

                    await asyncio.sleep(0) 

                    if bt.get('open_attempts', 0) >= CONFIG.MAX_TRADES_PER_SCAN:
                        if time.time() - bt.get('last_thr_bump_at', 0) > 10:
                            bt['score_threshold'] = min(CONFIG.SCORE_MAX, bt.get('score_threshold', CONFIG.SCORE_BASE) + (CONFIG.SCORE_ADAPT_STEP / 2))
                            bt['last_thr_bump_at'] = time.time()
                            log.info(f"Early stop & thr bump: opened {bt.get('open_attempts', 0)} trades; thr -> {bt['score_threshold']:.2f}")
                        else:
                            log.info(f"Early stop: opened {bt.get('open_attempts', 0)} trades; threshold bump skipped due to anti-jitter.")
                        
                        if len(liquid) > 0:
                            bt["scan_offset"] = (offset + processed_symbols_count) % len(liquid)
                        return

            except Exception:
                log.exception(f"Score calculation for {item.get('symbol')} failed")
        
        if top_scores:
            all_scores.extend((sd, s) for _, sd, s in top_scores)
            thr = bt.get('score_threshold', CONFIG.SCORE_BASE)
            ge_thr_chunk = sum(
                1 for _, sd, s in top_scores
                if s >= (thr + 0.10 if sd == "LONG" else thr)
            )
            best_score_chunk = max(s for _, _, s in top_scores)
            log.info(f"ScoreDiag ch={i//CONFIG.CHUNK_SIZE+1}: best={best_score_chunk:.2f}, >=thr={ge_thr_chunk}/{len(top_scores)} (thr={thr:.2f} [+0.1 LONG])")
        
        await asyncio.sleep(0)
    
    if len(liquid) > 0:
        bt["scan_offset"] = (offset + processed_symbols_count) % len(liquid)

    log.info(
        "GateDiag scan: "
        f"processed={processed_symbols_count}, no_ohlcv={tot_scan['no_ohlcv']}, cooldown={tot_scan['cooldown']}, "
        f"already_active={tot_scan['already_active']}, min_price={tot_scan['min_price']}, nan={tot_scan['nan_ind']}, "
        f"micro={tot_scan['micro_body']}, strong_ctr_reject={tot_scan['strong_ctr_reject']}, btc_block_reject={tot_scan['btc_block_reject']}, "
        f"long_wick_reject={tot_scan['long_wick_reject']}, long_atr_hi_reject={tot_scan['long_atr_hi_reject']}, "
        f"atr_rejects(H/L)={tot_scan['atr_too_high']}/{tot_scan['atr_too_low']}, "
        f"passed_total(ATR_req)={total_passed_gate}, "
        f"no_touch_rejects={bt.get('stat_no_touch',0)}"
    )
    if all_scores:
        thr = bt.get('score_threshold', CONFIG.SCORE_BASE)
        best_overall = max(s for _, s in all_scores) if all_scores else 0
        p90_score = np.percentile([s for _, s in all_scores], 90) if all_scores else 0
        cnt_ge_thr = sum(
            1 for sd, s in all_scores
            if s >= (thr + 0.10 if sd == "LONG" else thr)
        )
        log.info(f"ScoreDiag scan: best={best_overall:.2f}, p90={p90_score:.2f} "
                 f">=thr={cnt_ge_thr}/{len(all_scores)} (thr={thr:.2f} [+0.1 LONG])")

    bt['stat_no_touch'] = 0
    
    if CONFIG.THRESHOLD_MODE == "adaptive" and all_scores:
        n = len(all_scores)
        old_thr = bt.get('score_threshold', CONFIG.SCORE_BASE)
        if n >= CONFIG.THR_MIN_SAMPLES:
            scores_only = [s for _, s in all_scores]
            if n < 20:
                q = float(np.quantile(scores_only, CONFIG.THR_Q_SMALL))
            elif n < 50:
                q = float(np.quantile(scores_only, CONFIG.THR_Q_MED))
            else:
                q = float(np.quantile(scores_only, CONFIG.THR_Q_MAIN))
            target = q + CONFIG.THR_PAD
            target = max(CONFIG.SCORE_MIN, min(CONFIG.SCORE_MAX, target))
            
            proposed = (1 - CONFIG.THR_SMOOTH_ALPHA) * old_thr + CONFIG.THR_SMOOTH_ALPHA * target
            delta = max(-CONFIG.THR_MAX_JUMP, min(CONFIG.THR_MAX_JUMP, proposed - old_thr))
            new_thr = round(old_thr + delta, 2)
            bt['score_threshold'] = new_thr
            log.info(f"Thr(adapt): n={n}, q={q:.2f}, target={target:.2f} → {new_thr:.2f} (old={old_thr:.2f})")
        else:
            hard_rejects = tot_scan['strong_ctr_reject'] + tot_scan['btc_block_reject'] \
                         + tot_scan['long_wick_reject'] + tot_scan['long_atr_hi_reject']
            if bt.get('open_success', 0) == 0 and hard_rejects < 3:
                bt['score_threshold'] = max(CONFIG.SCORE_MIN, old_thr - CONFIG.SCORE_ADAPT_STEP)
                log.info(f"Thr(step-fallback): few samples (n={n}). thr -> {bt['score_threshold']:.2f}")
            else:
                log.info(f"Thr(step-fallback): skip lowering (n={n}, hard_rejects={hard_rejects})")
    
    log.info(f"Scan finished: attempts={bt.get('open_attempts',0)}, effective={bt.get('open_success',0)}, thr_now={bt.get('score_threshold', CONFIG.SCORE_BASE):.2f}")


# ---------------------------------------------------------------------------
async def _open_trade(candidate: dict, exchange: ccxt.Exchange, app: Application):
    bt = app.bot_data
    symbol, side, score = candidate['symbol'], candidate['side'], candidate['score']
    
    active_cnt = sum(1 for t in bt.get("active_trades", []) if t["Pair"] == symbol and t.get("Status") == "ACTIVE")
    if active_cnt >= CONFIG.MAX_POSITIONS_PER_SYMBOL:
        log.info(f"Skip {symbol}: already have {active_cnt} ACTIVE positions (limit={CONFIG.MAX_POSITIONS_PER_SYMBOL}).")
        return

    delay_ms = 1500
    wait_sec = max(0, (candidate['bar_ts'] + tf_seconds('1m')*1000 + delay_ms - int(time.time()*1000)) / 1000)
    if wait_sec > 0:
        log.debug(f"Wait before entry {symbol}: {wait_sec:.2f}s (bar_ts={candidate['bar_ts']})")
        await asyncio.sleep(wait_sec)
    
    if len(bt.get("active_trades", [])) >= CONFIG.MAX_CONCURRENT_POSITIONS:
        log.warning(f"Skipping {symbol} open, MAX_CONCURRENT_POSITIONS reached.")
        return
    if bt.get("symbol_cooldown", {}).get(symbol, 0) > time.time():
        log.info(f"Skipping {symbol} open, symbol is on cooldown.")
        return

    entry_tail_fraction, take_profit_pct = CONFIG.ENTRY_TAIL_FRACTION, CONFIG.TP_PCT
    thr_now = bt.get('score_threshold', CONFIG.SCORE_BASE)
    
    adj = 0.05 * min(max(candidate['features']['Wick_Ratio'] - 1.5, 0), 3.0) \
        + 0.05 * min(max(candidate['features']['Spike_ATR'] - 1.6, 0), 1.0)
    entry_tail_fraction = max(entry_tail_fraction, min(CONFIG.ENTRY_TAIL_FRACTION + adj, 0.5))

    if score >= thr_now + 0.6: entry_tail_fraction = max(entry_tail_fraction, 0.35)
    if score >= thr_now + 0.8: take_profit_pct = 0.5

    last_ohlc = candidate['last_ohlc']
    o, h, l, c = last_ohlc['o'], last_ohlc['h'], last_ohlc['l'], last_ohlc['c']
    
    if side == "LONG":
        lower_tail = min(o, c) - l
        entry_price = l + entry_tail_fraction * max(lower_tail, 0)
    else: # SHORT
        upper_tail = h - max(o, c)
        entry_price = h - entry_tail_fraction * max(upper_tail, 0)

    entry_price_q = float(exchange.price_to_precision(symbol, entry_price))

    try:
        tail = (min(o, c) - l) if side == "LONG" else (h - max(o, c))
        safe_atr = max(float(candidate['features'].get('ATR_last', 0)), 1e-9)
        m = exchange.market(symbol)
        min_tick = (((m.get('limits') or {}).get('price') or {}).get('min')) or 0.0

        tol_abs = max(min_tick * 1.1, 0.10 * tail, 0.10 * safe_atr)
        
        atr_rel = safe_atr / max(entry_price_q, 1e-9)
        base_min_pct = 0.03
        if entry_price_q < 0.05:
            base_min_pct = 0.15
        elif atr_rel < 0.004:
            base_min_pct = 0.10

        touch_tol_pct = max(
            base_min_pct,
            min(0.30, (tol_abs / entry_price_q * 100) if entry_price_q > 0 else 0)
        )

        tk = await exchange.fetch_ticker(symbol)
        last_px = tk.get('last') or tk.get('close') or ((tk.get('bid', 0) + tk.get('ask', 0)) / 2)
        
        lo = entry_price_q * (1 - touch_tol_pct/100)
        hi = entry_price_q * (1 + touch_tol_pct/100)
        if (last_px is None) or not (lo <= last_px <= hi):
            log.info(f"Skip {symbol}: no touch (live={last_px}, entry={entry_price_q}, tol=±{touch_tol_pct:.3f}%)")
            bt['stat_no_touch'] = bt.get('stat_no_touch', 0) + 1
            return
    except Exception as e:
        log.warning(f"Could not fetch ticker for touch check on {symbol}: {e}")
        return

    if entry_price_q == l or entry_price_q == h:
        log.warning(f"Skipping {symbol} {side} due to zero-size tail entry calculation.")
        return

    sl_dist_pct = entry_price_q * CONFIG.SL_PCT / 100
    sl_dist_atr = candidate['features'].get('ATR_last', 0) * CONFIG.ATR_SL_MULT
    sl_dist_price = max(sl_dist_pct, sl_dist_atr)
    tp_dist_price = entry_price_q * take_profit_pct / 100
    sl = (entry_price_q - sl_dist_price) if side == "LONG" else (entry_price_q + sl_dist_price)
    tp = (entry_price_q + tp_dist_price) if side == "LONG" else (entry_price_q - tp_dist_price)
    sl, tp = float(exchange.price_to_precision(symbol, sl)), float(exchange.price_to_precision(symbol, tp))
    
    m = exchange.market(symbol)
    tick = (((m.get('limits') or {}).get('price') or {}).get('min')) or 0.0
    if tick:
        if abs(sl - entry_price_q) < tick:
            sl = entry_price_q - tick if side=="LONG" else entry_price_q + tick
            sl = float(exchange.price_to_precision(symbol, sl))
        if abs(tp - entry_price_q) < tick:
            tp = entry_price_q + tick if side=="LONG" else entry_price_q - tick
            tp = float(exchange.price_to_precision(symbol, tp))

    trade = {
        "Signal_ID": f"{symbol}_{int(time.time())}", "Pair": symbol, "Side": side,
        "Entry_Price": entry_price_q, "SL_Price": sl, "TP_Price": tp, "Status": "ACTIVE", 
        "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        **candidate['features'],
        "Score_Threshold": round(thr_now, 2),
        "Entry_Bar_TS": candidate.get("bar_ts"),
        "MFE_Price": entry_price_q, "MAE_Price": entry_price_q
    }
    bt.setdefault("active_trades", []).append(trade)
    bt['open_success'] = bt.get('open_success', 0) + 1
    
    bt["symbol_cooldown"][symbol] = time.time() + CONFIG.SYMBOL_COOLDOWN_SEC
    log.info(f"Opened {side} {symbol} @ {entry_price_q} with score {trade['Score']:.2f}. Cooldown until {datetime.fromtimestamp(bt['symbol_cooldown'][symbol]).strftime('%H:%M:%S')}")

    if bc := bt.get('broadcast_func'):
        sl_actual_pct = abs(sl / entry_price_q - 1) * 100
        tp_actual_pct = abs(tp / entry_price_q - 1) * 100
        msg = (f"⚡ <b>Wick-Spike {side} (Score: {trade['Score']:.2f})</b>\n\n"
               f"<b>Пара:</b> {symbol}\n<b>Вход:</b> <code>{format_price(entry_price_q)}</code>\n"
               f"<b>SL:</b> <code>{format_price(sl)}</code> (-{sl_actual_pct:.2f}%)\n"
               f"<b>TP:</b> <code>{format_price(tp)}</code> (+{tp_actual_pct:.2f}%)")
        await bc(app, msg)

    await trade_executor.log_open_trade(trade)

async def _monitor_trades(exchange: ccxt.Exchange, app: Application):
    bt = app.bot_data
    act = bt.get("active_trades", [])
    if not act: return

    sem = asyncio.Semaphore(CONFIG.CONCURRENCY_SEMAPHORE)
    async def fetch(symbol):
        async with sem:
            try:
                ohlc = await asyncio.wait_for(exchange.fetch_ohlcv(symbol, CONFIG.TIMEFRAME, limit=2), timeout=12)
                return symbol, ohlc[-1] if ohlc else None
            except Exception: return symbol, None

    latest_bars = dict(await asyncio.gather(*[fetch(t['Pair']) for t in act]))
    close_list: List[Tuple[dict,str,float]] = []

    bar_ts_map = {s: b[0] for s, b in latest_bars.items() if b}
    need_live_price = [t for t in act if bar_ts_map.get(t['Pair']) == t.get('Entry_Bar_TS')]

    async def fetch_last(symbol):
        async with sem:
            try:
                tk = await asyncio.wait_for(exchange.fetch_ticker(symbol), timeout=6)
                last = tk.get('last') or tk.get('close') \
                       or ( (tk.get('bid') and tk.get('ask')) and (tk['bid']+tk['ask'])/2 )
                return symbol, last
            except Exception:
                return symbol, None

    last_prices = dict(await asyncio.gather(*[fetch_last(t['Pair']) for t in need_live_price]))
    
    for tr in act:
        bar = latest_bars.get(tr['Pair'])
        if bar is None: continue
        
        bar_ts, last_high, last_low = bar[0], bar[2], bar[3]

        if bar_ts == tr.get('Entry_Bar_TS'):
            px = last_prices.get(tr['Pair'])
            if px is None: continue
            
            if tr['Side'] == "LONG":
                tr['MFE_Price'] = max(tr.get('MFE_Price', tr['Entry_Price']), px)
                tr['MAE_Price'] = min(tr.get('MAE_Price', tr['Entry_Price']), px)
                if px <= tr['SL_Price']: close_list.append((tr, "STOP_LOSS", px))
                elif px >= tr['TP_Price']: close_list.append((tr, "TAKE_PROFIT", px))
            else: # SHORT
                tr['MFE_Price'] = min(tr.get('MFE_Price', tr['Entry_Price']), px)
                tr['MAE_Price'] = max(tr.get('MAE_Price', tr['Entry_Price']), px)
                if px >= tr['SL_Price']: close_list.append((tr, "STOP_LOSS", px))
                elif px <= tr['TP_Price']: close_list.append((tr, "TAKE_PROFIT", px))
        
        else:
            if tr['Side'] == "LONG":
                tr['MFE_Price'] = max(tr.get('MFE_Price', tr['Entry_Price']), last_high)
                tr['MAE_Price'] = min(tr.get('MAE_Price', tr['Entry_Price']), last_low)
                if last_low <= tr['SL_Price']: close_list.append((tr, "STOP_LOSS", tr['SL_Price']))
                elif last_high >= tr['TP_Price']: close_list.append((tr, "TAKE_PROFIT", tr['TP_Price']))
            else: # SHORT
                tr['MFE_Price'] = min(tr.get('MFE_Price', tr['Entry_Price']), last_low)
                tr['MAE_Price'] = max(tr.get('MAE_Price', tr['Entry_Price']), last_high)
                if last_high >= tr['SL_Price']: close_list.append((tr, "STOP_LOSS", tr['SL_Price']))
                elif last_low <= tr['TP_Price']: close_list.append((tr, "TAKE_PROFIT", tr['TP_Price']))


    if not close_list: return

    for tr, reason, exit_p in close_list:
        tp_pct = abs(tr['TP_Price'] / tr['Entry_Price'] - 1) * 100
        sl_pct_actual = abs(tr['SL_Price'] / tr['Entry_Price'] - 1) * 100
        pnl_pct = tp_pct if reason == "TAKE_PROFIT" else -sl_pct_actual

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
