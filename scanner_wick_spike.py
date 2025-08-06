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

    SCAN_INTERVAL_SECONDS  = 30
    TICK_MONITOR_SECONDS   = 5

    # ДОБАВЛЕНО: Заглушки для обратной совместимости с main.py
    ATR_SL_MULT = 1.0  # Не используется в логике, но нужно для /status
    RISK_REWARD = 2.0  # Не используется в логике, но нужно для /status

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_price(p: float) -> str:
    if p < 0.01:
        return f"{p:.6f}"
    if p < 1.0:
        return f"{p:.5f}"
    return f"{p:.4f}"


def tf_seconds(tf: str) -> int:
    n, unit = int(tf[:-1]), tf[-1].lower()
    return n * (60 if unit == "m" else 3600 if unit == "h" else 86400)

# ---------------------------------------------------------------------------
# Wick detection
# ---------------------------------------------------------------------------

def is_spike(candle: pd.Series, atr: float) -> Tuple[bool, Optional[str]]:
    """Return (True, side) where side is LONG/SHORT to take *counter* spike."""
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body = abs(c - o)
    range_ = h - l
    if atr <= 0:
        return (False, None)
    if range_ < CONFIG.ATR_SPIKE_MULT * atr:
        return (False, None)

    upper_tail = h - max(o, c)
    lower_tail = min(o, c) - l

    if lower_tail >= CONFIG.WICK_RATIO * body and lower_tail > 0:
        return (True, "LONG")
    if upper_tail >= CONFIG.WICK_RATIO * body and upper_tail > 0:
        return (True, "SHORT")
    return (False, None)

# ---------------------------------------------------------------------------
# Core loops
# ---------------------------------------------------------------------------
async def scanner_main_loop(app: Application, broadcast):
    log.info("Wick-Spike loop starting …")
    app.bot_data.setdefault("active_trades", [])
    app.bot_data['broadcast_func'] = broadcast

    exchange = ccxt.mexc({'options': {'defaultType': 'swap'},
                          'enableRateLimit': True, 'rateLimit': 150})

    while app.bot_data.get("bot_on", False):
        try:
            await _run_scan(exchange, app)
            await _monitor_trades(exchange, app)
            await asyncio.sleep(CONFIG.SCAN_INTERVAL_SECONDS)
        except Exception as e:
            log.error(f"main loop error: {e}", exc_info=True)
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
    except Exception as e:
        log.warning(f"ticker fetch failed: {e}")
        return

    sem = asyncio.Semaphore(CONFIG.CONCURRENCY_SEMAPHORE)

    async def fetch_tf(symbol):
        async with sem:
            try:
                return symbol, await exchange.fetch_ohlcv(symbol, CONFIG.TIMEFRAME, limit=CONFIG.VOL_WINDOW + CONFIG.ATR_PERIOD + 1)
            except Exception:
                return symbol, None

    bars = await asyncio.gather(*[fetch_tf(s) for s in liquid])

    for symbol, ohlcv in bars:
        if not ohlcv:
            continue
        df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
        if df.iloc[-1]['ts'] < (time.time()-tf_seconds(CONFIG.TIMEFRAME))*1000:
            continue
        if df.iloc[-1]['close'] < CONFIG.MIN_PRICE:
            continue

        df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=CONFIG.ATR_PERIOD)
        vol_mu = df["volume"].rolling(CONFIG.VOL_WINDOW).mean()
        vol_sigma = df["volume"].rolling(CONFIG.VOL_WINDOW).std()
        df["vol_z"] = (df["volume"] - vol_mu) / vol_sigma
        last = df.iloc[-1]
        if last["vol_z"] < CONFIG.VOL_Z_THRESHOLD:
            continue

        ok, side = is_spike(last, last["atr"])
        if not ok:
            continue

        await _open_trade(symbol, side, last, exchange, app)

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
    if not act:
        return

    sem = asyncio.Semaphore(CONFIG.CONCURRENCY_SEMAPHORE)
    async def fetch(symbol):
        async with sem:
            try:
                ohlc = await exchange.fetch_ohlcv(symbol, CONFIG.TIMEFRAME, limit=2)
                return symbol, ohlc[-1][4] if ohlc else None
            except Exception:
                return symbol, None

    latest = dict(await asyncio.gather(*[fetch(t['Pair']) for t in act]))
    close_list: List[Tuple[dict,str,float]] = []

    for tr in act:
        price = latest.get(tr['Pair'])
        if price is None:
            continue
        tr['MFE_Price'] = max(tr['MFE_Price'], price) if tr['Side']=="LONG" else min(tr['MFE_Price'], price)
        if (tr['Side']=="LONG" and price <= tr['SL_Price']) or \
           (tr['Side']=="SHORT" and price >= tr['SL_Price']):
            close_list.append((tr, "STOP_LOSS", price))
        elif (tr['Side']=="LONG" and price >= tr['TP_Price']) or \
             (tr['Side']=="SHORT" and price <= tr['TP_Price']):
            close_list.append((tr, "TAKE_PROFIT", price))

    if not close_list:
        return

    for tr, reason, exit_p in close_list:
        pnl_pct = CONFIG.TP_PCT if reason=="TAKE_PROFIT" else -CONFIG.SL_PCT
        pnl_lever = pnl_pct * CONFIG.LEVERAGE
        pnl_usd = CONFIG.POSITION_SIZE_USDT * pnl_lever / 100
        if bc := bt.get('broadcast_func'):
            emoji = "✅" if reason=="TAKE_PROFIT" else "❌"
            await bc(app, f"{emoji} <b>{reason}</b> {tr['Pair']} {pnl_lever:+.2f}% (price {format_price(exit_p)})")
        await trade_executor.update_closed_trade(tr['Signal_ID'], "CLOSED", exit_p, pnl_usd, pnl_lever, reason)

    closed_ids = {t['Signal_ID'] for t,_,_ in close_list}
    bt['active_trades'] = [t for t in act if t['Signal_ID'] not in closed_ids]
