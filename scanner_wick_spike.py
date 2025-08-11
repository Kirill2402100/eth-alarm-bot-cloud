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
# CONFIG - ИЗМЕНЕНО
# ---------------------------------------------------------------------------
class CONFIG:
    TIMEFRAME = "1m"
    POSITION_SIZE_USDT = 10.0
    LEVERAGE = 20
    MAX_CONCURRENT_POSITIONS = 10
    
    # ПАТЧ: Новые параметры для производительности и стабильности
    CONCURRENCY_SEMAPHORE = 8       # Уменьшаем параллелизм
    FETCH_TIMEOUT = 10              # Таймаут на один запрос OHLCV в секундах
    SCAN_CADENCE_SEC = 30           # Целевая частота запусков _run_scan
    CHUNK_SIZE = 120                # Сколько символов обрабатываем за один под-проход
    TIME_BUDGET_SEC = 27            # Максимум времени на один полный запуск сканера

    MIN_QUOTE_VOLUME_USD = 200_000
    MIN_PRICE = 0.001
    SCAN_INTERVAL_SECONDS = 3

    # --- Параметры признаков и жестких гейтов ---
    ATR_PERIOD = 14
    VOL_WINDOW = 50
    GATE_WICK_RATIO = 1.7
    GATE_ATR_SPIKE_MULT = 1.6
    GATE_VOL_Z = 1.5

    # --- Параметры для входа и выхода ---
    ENTRY_TAIL_FRACTION = 0.3
    SL_PCT = 0.20
    TP_PCT = 0.40

    # --- Управление адаптивным порогом и частотой ---
    SCORE_BASE = 1.6
    SCORE_MIN = 1.4
    SCORE_MAX = 2.4
    SCORE_ADAPT_STEP = 0.1
    TARGET_SIGNALS_PER_SCAN = 1
    MAX_TRADES_PER_SCAN = 2
    SYMBOL_COOLDOWN_SEC = 120

    # Заглушки для обратной совместимости
    ATR_SL_MULT = 0
    RISK_REWARD = TP_PCT / SL_PCT if SL_PCT > 0 else 0


# ... (Функции ensure_new_log_sheet, format_price, tf_seconds остаются без изменений) ...
async def ensure_new_log_sheet(gfile: gspread.Spreadsheet):
    """
    Проверяет, что лист 'Trading_Log_v2' существует и имеет правильные заголовки.
    Если заголовки устарели, архивирует старый лист и создает новый.
    """
    loop = asyncio.get_running_loop()
    title = "Trading_Log_v2"
    
    required_headers = [
        "Signal_ID", "Timestamp_UTC", "Pair", "Side", "Status",
        "Entry_Price", "Exit_Price", "Exit_Time_UTC",
        "Exit_Reason", "PNL_USD", "PNL_Percent",
        "Score", "Score_Threshold", "Wick_Ratio", "Spike_ATR", "Vol_Z",
        "Dist_Mean", "HTF_Slope", "BTC_5m",
        "MFE_Price", "MAE_Price", "MFE_pct", "MAE_pct", "Time_to_TP_SL"
    ]

    ws = None
    try:
        ws = await loop.run_in_executor(None, lambda: gfile.worksheet(title))
        log.info(f"Worksheet '{title}' already exists. Checking headers...")
        
        current_headers = await loop.run_in_executor(None, lambda: ws.row_values(1))
        if current_headers != required_headers:
            log.warning(f"Headers in '{title}' are outdated! Archiving old sheet and creating a new one.")
            archive_title = f"{title}_archive_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
            await loop.run_in_executor(None, lambda: ws.update_title(archive_title))
            
            ws = await loop.run_in_executor(None, lambda: gfile.add_worksheet(title=title, rows=1000, cols=len(required_headers)))
            await loop.run_in_executor(None, lambda: ws.append_row(required_headers, value_input_option="USER_ENTERED"))
            log.info(f"Archived old sheet to '{archive_title}' and created a new '{title}'.")
        else:
            log.info(f"Headers in '{title}' are up to date.")

    except gspread.exceptions.WorksheetNotFound:
        log.warning(f"Worksheet '{title}' not found. Creating...")
        try:
            ws = await loop.run_in_executor(None, lambda: gfile.add_worksheet(title=title, rows=1000, cols=len(required_headers)))
            await loop.run_in_executor(None, lambda: ws.append_row(required_headers, value_input_option="USER_ENTERED"))
            log.info(f"Worksheet '{title}' created with all required headers.")
        except Exception as e:
            log.critical(f"Failed to create new worksheet '{title}': {e}")
            raise
    except Exception as e:
        log.critical(f"An unexpected error occurred with Google Sheets: {e}")
        raise

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
        log.info(f"Инициализация порога: score_threshold = {app.bot_data['score_threshold']}")

    # ... (код инициализации Google Sheets остается без изменений) ...
    try:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        sheet_key  = os.environ.get("SHEET_ID")
        if not creds_json or not sheet_key:
            log.critical("GOOGLE_CREDENTIALS / SHEET_ID не заданы, логирование в таблицу отключено.")
        else:
            gc = gspread.service_account_from_dict(json.loads(creds_json))
            sheet = gc.open_by_key(sheet_key)
            await ensure_new_log_sheet(sheet)
            log.info("Google Sheets инициализированы")
    except Exception as e:
        log.error(f"Sheets init error: {e}", exc_info=True)

    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True, 'rateLimit': 150})
    
    last_flush = 0
    last_scan_time = 0

    while app.bot_data.get("bot_on", False):
        log.info("Wick-Spike loop heartbeat…")
        try:
            current_time = time.time()
            if not app.bot_data.get("scan_paused", False):
                # ПАТЧ: Используем SCAN_CADENCE_SEC и добавляем лог начала скана
                if current_time - last_scan_time >= CONFIG.SCAN_CADENCE_SEC:
                    log.info(f"Wick-Scan start: thr={app.bot_data.get('score_threshold', CONFIG.SCORE_BASE):.2f}")
                    await _run_scan(exchange, app)
                    last_scan_time = current_time
            
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

    try:
        tickers = await exchange.fetch_tickers()
        liquid_symbols = {s for s, t in tickers.items()
                         if (t.get("quoteVolume") or 0) > CONFIG.MIN_QUOTE_VOLUME_USD
                         and exchange.market(s).get("type") == "swap"
                         and s.endswith("USDT:USDT")
                         and "UP" not in s and "DOWN" not in s}
        liquid_symbols.add("BTC/USDT:USDT")
        liquid = list(liquid_symbols)
        log.info(f"Wick-Scan: получено {len(tickers)} пар; после фильтра ликвидности – {len(liquid)}")
    except Exception as e:
        log.warning(f"ticker fetch failed: {e}")
        return

    try:
        btc_ohlcv = await exchange.fetch_ohlcv("BTC/USDT:USDT", '1m', limit=10)
        btc_df = pd.DataFrame(btc_ohlcv, columns=["ts","open","high","low","close","volume"])
    except Exception as e:
        log.warning(f"BTC fetch failed, skipping scan: {e}")
        return

    sem = asyncio.Semaphore(CONFIG.CONCURRENCY_SEMAPHORE)
    limit_1m = CONFIG.VOL_WINDOW + CONFIG.ATR_PERIOD + 1
    limit_5m = 50 + 6

    # ПАТЧ: fetch_data с индивидуальными таймаутами на каждый запрос
    async def fetch_data(symbol):
        async with sem:
            try:
                ohlc1 = await asyncio.wait_for(
                    exchange.fetch_ohlcv(symbol, '1m', limit=limit_1m),
                    timeout=CONFIG.FETCH_TIMEOUT
                )
                ohlc5 = await asyncio.wait_for(
                    exchange.fetch_ohlcv(symbol, '5m', limit=limit_5m),
                    timeout=CONFIG.FETCH_TIMEOUT
                )
                return symbol, {"1m": ohlc1, "5m": ohlc5}
            except Exception as e:
                log.debug(f"OHLCV timeout/err {symbol}: {e.__class__.__name__}")
                return symbol, None

    scan_started = time.time()
    candidates = []
    opened_count = 0
    now = time.time()
    cooldown = bt.get("symbol_cooldown", {})
    if now % 60 < 5: 
        bt["symbol_cooldown"] = {s: until for s, until in cooldown.items() if until > now}

    # ПАТЧ: Обработка символов чанками
    for i in range(0, len(liquid), CONFIG.CHUNK_SIZE):
        if time.time() - scan_started > CONFIG.TIME_BUDGET_SEC:
            log.warning("Scan aborted – time budget exceeded")
            break
        
        if opened_count >= CONFIG.MAX_TRADES_PER_SCAN:
            log.info("Max trades per scan reached, stopping early.")
            break

        chunk = liquid[i:i+CONFIG.CHUNK_SIZE]
        all_data = await asyncio.gather(*[fetch_data(s) for s in chunk])

        # --- Обработка данных внутри чанка ---
        for symbol, data_dict in all_data:
            if not data_dict: continue
            if cooldown.get(symbol, 0) > now: continue
            
            # ... (здесь вся логика расчета признаков и скоринга, она остается прежней) ...
            df_1m = pd.DataFrame(data_dict['1m'], columns=["ts","open","high","low","close","volume"])
            df_5m = pd.DataFrame(data_dict['5m'], columns=["ts","open","high","low","close","volume"])

            if df_1m.iloc[-1]['ts'] < (time.time() - tf_seconds('1m')) * 1000: continue
            if df_1m.iloc[-1]['close'] < CONFIG.MIN_PRICE: continue
            df_1m['ema20'] = ta.ema(df_1m['close'], length=20)
            df_1m['atr'] = ta.atr(df_1m['high'], df_1m['low'], df_1m['close'], length=CONFIG.ATR_PERIOD)
            vol_mu = df_1m['volume'].rolling(CONFIG.VOL_WINDOW).mean()
            vol_sigma = df_1m['volume'].rolling(CONFIG.VOL_WINDOW).std().replace(0, 1e-9)
            df_1m['vol_z'] = (df_1m['volume'] - vol_mu) / vol_sigma
            last = df_1m.iloc[-1]
            o, h, l, c = last['open'], last['high'], last['low'], last['close']
            if pd.isna(last["vol_z"]) or pd.isna(last["atr"]) or pd.isna(last["ema20"]): continue
            body = max(abs(c-o), 1e-9)
            upper_wick, lower_wick = h - max(o, c), min(o, c) - l
            wick_ratio, spike_atr_mult = max(upper_wick, lower_wick) / body, (h - l) / max(last['atr'], 1e-9)
            vol_z, dist_from_mean = last['vol_z'], abs(c - last['ema20']) / max(last['atr'], 1e-9)
            if not (wick_ratio >= CONFIG.GATE_WICK_RATIO and spike_atr_mult >= CONFIG.GATE_ATR_SPIKE_MULT and vol_z >= CONFIG.GATE_VOL_Z):
                continue
            ema50_m5 = ta.ema(df_5m['close'], length=50)
            atr_m5 = ta.atr(df_5m['high'], df_5m['low'], df_5m['close'], length=14)
            htf_m5_slope = (ema50_m5.iloc[-1] - ema50_m5.iloc[-6]) / max(atr_m5.iloc[-1], 1e-9) if len(ema50_m5) > 5 else 0
            btc_ret_5m = (btc_df['close'].iloc[-1] / btc_df['close'].iloc[-6]) - 1 if len(btc_df) > 5 else 0
            side = "SHORT" if upper_wick >= lower_wick else "LONG"
            btc_context_bonus = 0.0
            if side == "LONG" and btc_ret_5m < -0.005: btc_context_bonus = -0.15
            elif side == "SHORT" and btc_ret_5m > 0.005: btc_context_bonus = -0.15
            elif side == "LONG" and btc_ret_5m > -0.002: btc_context_bonus = 0.05
            elif side == "SHORT" and btc_ret_5m < 0.002: btc_context_bonus = 0.05
            score = (0.35 * min(max(wick_ratio - 1.5, 0), 2.5) + 0.25 * min(max(spike_atr_mult - 1.8, 0), 2.0) + 0.20 * min(max(vol_z - 2.0, 0), 3.0) + 0.15 * min(dist_from_mean, 3.0) - 0.15 * min(abs(htf_m5_slope), 2.0) + 0.10 * btc_context_bonus)
            score_threshold = bt.get('score_threshold', CONFIG.SCORE_BASE)
            if score >= score_threshold:
                candidates.append({
                    'symbol': symbol, 'side': side, 'score': score,
                    'last_ohlc': {'o': o, 'h': h, 'l': l, 'c': c},
                    'features': {
                        'Score': round(score, 2), 'Wick_Ratio': round(wick_ratio, 2),
                        'Spike_ATR': round(spike_atr_mult, 2), 'Vol_Z': round(vol_z, 2),
                        'Dist_Mean': round(dist_from_mean, 2), 'HTF_Slope': round(htf_m5_slope, 2),
                        'BTC_5m': round(btc_ret_5m * 100, 3)
                    }
                })

        # ПАТЧ: Логирование прогресса после каждого чанка
        log.info(f"Scan progress: {min(i + CONFIG.CHUNK_SIZE, len(liquid))}/{len(liquid)} symbols, "
                 f"found={len(candidates)} opened={opened_count} elapsed={time.time()-scan_started:.1f}s")
    
    # --- Отбор и адаптация порога (после всех чанков) ---
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
        log.info(f"Scan finished, no suitable signals found. Current threshold: {bt['score_threshold']:.2f}")
    else:
        log.info(f"Scan finished: found {found_count} candidates, opened {opened_count}. New threshold: {bt['score_threshold']:.2f}")

# ... (Функции _open_trade и _monitor_trades остаются такими же, как в предыдущем ответе, все патчи в них уже были учтены) ...

async def _open_trade(candidate: dict, exchange: ccxt.Exchange, app: Application):
    bt = app.bot_data
    symbol, side, score = candidate['symbol'], candidate['side'], candidate['score']
    
    entry_tail_fraction, take_profit_pct = CONFIG.ENTRY_TAIL_FRACTION, CONFIG.TP_PCT
    score_threshold = bt.get('score_threshold', CONFIG.SCORE_BASE)
    if score >= score_threshold + 0.6: entry_tail_fraction = 0.35
    if score >= score_threshold + 0.8: take_profit_pct = 0.5

    last_ohlc = candidate['last_ohlc']
    o, h, l = last_ohlc['o'], last_ohlc['h'], last_ohlc['l']
    
    if side == "LONG":
        entry_price = l + entry_tail_fraction * (o - l)
    else: # SHORT
        entry_price = h - entry_tail_fraction * (h - o)

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

    sem = asyncio.Semaphore(CONFIG.CONCURRENCY_SEMAPHORE)
    async def fetch(symbol):
        async with sem:
            try:
                ohlc = await asyncio.wait_for(exchange.fetch_ohlcv(symbol, CONFIG.TIMEFRAME, limit=2), timeout=12)
                return symbol, ohlc[-1] if ohlc else None
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
        tp_pct = abs(tr['TP_Price'] / tr['Entry_Price'] - 1) * 100
        pnl_pct = tp_pct if reason == "TAKE_PROFIT" else -CONFIG.SL_PCT
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
