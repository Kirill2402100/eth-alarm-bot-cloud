# scanner_wick_spike.py

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
    TIMEFRAME = "1m"
    POSITION_SIZE_USDT = 10.0
    LEVERAGE = 20
    MAX_CONCURRENT_POSITIONS = 10
    CONCURRENCY_SEMAPHORE   = 10
    MIN_QUOTE_VOLUME_USD = 300_000
    MIN_PRICE = 0.001
    ATR_PERIOD = 14
    ATR_SPIKE_MULT = 1.8
    WICK_RATIO = 2.0
    VOL_WINDOW = 50
    VOL_Z_THRESHOLD = 2.0
    ENTRY_TAIL_FRACTION = 0.25
    SL_PCT = 0.2
    TP_PCT = 0.4
    SCAN_INTERVAL_SECONDS  = 5
    # Заглушки для обратной совместимости с командой /status в main.py
    ATR_SL_MULT = 0
    RISK_REWARD = TP_PCT / SL_PCT if SL_PCT > 0 else 0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def ensure_new_log_sheet(gfile: gspread.Spreadsheet):
    """Создаёт лист 'Trading_Log_v2', если он не существует."""
    loop = asyncio.get_running_loop()
    title = "Trading_Log_v2"
    ws = None
    try:
        ws = await loop.run_in_executor(None, lambda: gfile.worksheet(title))
        log.info(f"Worksheet '{title}' already exists.")
    except gspread.exceptions.WorksheetNotFound:
        log.warning(f"Worksheet '{title}' not found. Creating...")
        try:
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

def format_price(p: float) -> str:
    if p < 0.01: return f"{p:.6f}"
    if p < 1.0: return f"{p:.5f}"
    return f"{p:.4f}"

def tf_seconds(tf: str) -> int:
    n, unit = int(tf[:-1]), tf[-1].lower()
    return n * (60 if unit == "m" else 3600 if unit == "h" else 86400)

# ---------------------------------------------------------------------------
# Wick detection
# ---------------------------------------------------------------------------

def is_spike(candle: pd.Series, atr: float) -> tuple[bool, str|None]:
    """Определяет шпильку и возвращает сторону для контр-трейда."""
    o,h,l,c = candle["open"], candle["high"], candle["low"], candle["close"]
    body = abs(c-o)
    rng = h-l

    # ИЗМЕНЕНО: Защита от деления на ноль и ложных срабатываний на доджи
    if body == 0:
        body = (h + l) * 1e-6  # Присваиваем минимальную ненулевую ширину

    if atr <= 0 or rng < CONFIG.ATR_SPIKE_MULT * atr:
        return (False, None)

    upper = h - max(o,c)
    lower = min(o,c) - l
    
    cond_lo = lower >= CONFIG.WICK_RATIO * body and lower > 0
    cond_up = upper >= CONFIG.WICK_RATIO * body and upper > 0

    if cond_lo and cond_up:
        return (True, "LONG"  if lower > upper else "SHORT")

    if cond_lo: return (True, "LONG")
    if cond_up: return (True, "SHORT")
    return (False, None)

# ---------------------------------------------------------------------------
# Core loops
# ---------------------------------------------------------------------------
async def scanner_main_loop(app: Application, broadcast):
    log.info("Wick-Spike loop starting …")
    app.bot_data.setdefault("active_trades", [])
    app.bot_data['broadcast_func'] = broadcast

    try:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        sheet_key  = os.environ.get("SHEET_ID")
        if not creds_json or not sheet_key:
            log.critical("GOOGLE_CREDENTIALS / SHEET_ID не заданы, логирование в таблицу отключено.")
        else:
            gc = gspread.service_account_from_dict(json.loads(creds_json))
            sheet = gc.open_by_key(sheet_key)
            await ensure_new_log_sheet(sheet)
            trade_executor.get_headers(trade_executor.TRADE_LOG_WS)
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
                if current_time - last_scan_time >= 30:
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
        liquid = [s for s, t in tickers.items()
                  if (t.get("quoteVolume") or 0) > CONFIG.MIN_QUOTE_VOLUME_USD
                  and exchange.market(s).get("type") == "swap"
                  and s.endswith("USDT:USDT")]
        log.info(f"Wick-Scan: получено {len(tickers)} пар; после фильтра ликвидности – {len(liquid)}")
    except Exception as e:
        log.warning(f"ticker fetch failed: {e}")
        return

    sem = asyncio.Semaphore(CONFIG.CONCURRENCY_SEMAPHORE)

    async def fetch_tf(symbol):
        async with sem:
            try:
                return symbol, await asyncio.wait_for(
                    exchange.fetch_ohlcv(symbol, CONFIG.TIMEFRAME, limit=CONFIG.VOL_WINDOW + CONFIG.ATR_PERIOD + 1),
                    timeout=12
                )
            except Exception as e:
                log.debug(f"ohlcv {symbol} failed: {e.__class__.__name__}")
                return symbol, None

    bars = await asyncio.gather(*[fetch_tf(s) for s in liquid])

    found_count = 0
    opened_count = 0
    scan_started = time.time()

    for symbol, ohlcv in bars:
        if time.time() - scan_started > 27:
            log.warning("Scan aborted – time budget exceeded")
            break

        if not ohlcv: continue
        df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
        if df.iloc[-1]['ts'] < (time.time()-tf_seconds(CONFIG.TIMEFRAME))*1000: continue
        if df.iloc[-1]['close'] < CONFIG.MIN_PRICE: continue

        df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=CONFIG.ATR_PERIOD)
        vol_mu = df["volume"].rolling(CONFIG.VOL_WINDOW).mean()
        vol_sigma = df["volume"].rolling(CONFIG.VOL_WINDOW).std()
        df["vol_z"] = (df["volume"] - vol_mu) / vol_sigma
        last = df.iloc[-1]
        
        if pd.isna(last["vol_z"]) or pd.isna(last["atr"]): continue
        if last["vol_z"] < CONFIG.VOL_Z_THRESHOLD: continue

        ok, side = is_spike(last, last["atr"])
        if not ok: continue
        
        found_count += 1
        if len(bt.get("active_trades", [])) + opened_count < CONFIG.MAX_CONCURRENT_POSITIONS:
            await _open_trade(symbol, side, last, exchange, app)
            opened_count += 1
    
    log.info(f"Wick-Scan завершён: найдено {found_count} шпилей, открыто {opened_count} сделок.")

# ---------------------------------------------------------------------------
async def _open_trade(symbol: str, side: str, candle: pd.Series,
                      exchange: ccxt.Exchange, app: Application):
    bt = app.bot_data
    entry_price = candle["close"]
    tail_frac = CONFIG.ENTRY_TAIL_FRACTION
    if tail_frac > 0:
        if side == "LONG":
            entry_price = candle["low"] + tail_frac * (candle["open"] - candle["low"])
        else:
            entry_price = candle["high"] - tail_frac * (candle["high"] - candle["open"])

    sl_off = CONFIG.SL_PCT / 100 * entry_price
    tp_off = CONFIG.TP_PCT / 100 * entry_price
    sl  = entry_price - sl_off if side == "LONG" else entry_price + sl_off
    tp  = entry_price + tp_off if side == "LONG" else entry_price - tp_off

    sl = float(exchange.price_to_precision(symbol, sl))
    tp = float(exchange.price_to_precision(symbol, tp))
    entry_price = float(exchange.price_to_precision(symbol, entry_price))

    trade = {
        "Signal_ID": f"{symbol}_{int(time.time())}",
        "Pair": symbol, "Side": side,
        "Entry_Price": entry_price, "SL_Price": sl, "TP_Price": tp,
        "Status": "ACTIVE",
        "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "sl_pct": CONFIG.SL_PCT, "tp_pct": CONFIG.TP_PCT,
        "MFE_Price": entry_price
    }
    bt.setdefault("active_trades", []).append(trade)
    log.info(f"Opened {side} {symbol} @ {entry_price}")

    if bc := bt.get('broadcast_func'):
        msg = (f"⚡ <b>Wick-Spike {side}</b>\n\n"
               f"<b>Пара:</b> {symbol}\n"
               f"<b>Вход:</b> <code>{format_price(entry_price)}</code>\n"
               f"<b>SL:</b> <code>{format_price(sl)}</code> (-{CONFIG.SL_PCT} %)\n"
               f"<b>TP:</b> <code>{format_price(tp)}</code> (+{CONFIG.TP_PCT} %)")
        await bc(app, msg)

    await trade_executor.log_open_trade(trade)

# ---------------------------------------------------------------------------
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
            except Exception as e:
                log.debug(f"monitor fetch {symbol} failed: {e.__class__.__name__}")
                return symbol, None

    latest_bars = dict(await asyncio.gather(*[fetch(t['Pair']) for t in act]))
    close_list: List[Tuple[dict,str,float]] = []

    for tr in act:
        bar = latest_bars.get(tr['Pair'])
        if bar is None: continue
        
        last_high = bar[2]
        last_low = bar[3]
        
        tr['MFE_Price'] = max(tr['MFE_Price'], last_high) if tr['Side']=="LONG" else min(tr['MFE_Price'], last_low)
        
        if tr['Side']=="LONG":
            if last_low <= tr['SL_Price']:
                close_list.append((tr, "STOP_LOSS", tr['SL_Price']))
            elif last_high >= tr['TP_Price']:
                close_list.append((tr, "TAKE_PROFIT", tr['TP_Price']))
        else: # SHORT
            if last_high >= tr['SL_Price']:
                close_list.append((tr, "STOP_LOSS", tr['SL_Price']))
            elif last_low <= tr['TP_Price']:
                close_list.append((tr, "TAKE_PROFIT", tr['TP_Price']))

    if not close_list: return

    for tr, reason, exit_p in close_list:
        pnl_pct = CONFIG.TP_PCT if reason=="TAKE_PROFIT" else -CONFIG.SL_PCT
        pnl_lever = pnl_pct * CONFIG.LEVERAGE
        pnl_usd = CONFIG.POSITION_SIZE_USDT * pnl_lever / 100
        if bc := bt.get('broadcast_func'):
            emoji = "✅" if reason=="TAKE_PROFIT" else "❌"
            await bc(app, f"{emoji} <b>{reason}</b> {tr['Pair']} {pnl_lever:+.2f}% (price {format_price(exit_p)})")
        
        extra_fields = {"MFE_Price": tr['MFE_Price']}
        await trade_executor.update_closed_trade(tr['Signal_ID'], "CLOSED", exit_p, pnl_usd, pnl_lever, reason, extra_fields=extra_fields)

    closed_ids = {t['Signal_ID'] for t,_,_ in close_list}
    bt['active_trades'] = [t for t in act if t['Signal_ID'] not in closed_ids]
