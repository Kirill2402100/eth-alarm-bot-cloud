# -*- coding: utf-8 -*-
"""
EMA-Cross strategy bot (MEXC perpetuals, 1-min)
Version: 2025-07-31  — tick-monitoring edition
"""

import asyncio, time, logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import pandas as pd, pandas_ta as ta
import ccxt.async_support as ccxt
from telegram.ext import Application

from trade_executor import log_open_trade, log_tsl_update, update_closed_trade

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
PAIR_TO_SCAN        = "SOL/USDT:USDT"
TIMEFRAME           = "1m"
EMA_PERIOD          = 200
TRAILING_STOP_STEP  = 0.002       # 0.2 %
API_BUFFER          = 2
PRICE_SOURCE        = "mark"
HIST_MULTIPLIER     = 5
TICK_INTERVAL       = 3           # сек; как часто опрашиваем цену при открытой сделке

FIXED_SL_DEP_PCT    = 0.1          # ← жёсткий стоп: 15 % депозита

log = logging.getLogger("ema_cross_bot")

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def calculate_features(ohlcv: List[list], drop_last: bool = True) -> Optional[pd.DataFrame]:
    """Return DataFrame with EMA; optionally cut unfinished bar."""
    if len(ohlcv) < EMA_PERIOD:
        return None
    if drop_last:
        ohlcv = ohlcv[:-1]
    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
    df[f"EMA_{EMA_PERIOD}"] = df.ta.ema(length=EMA_PERIOD)
    return df

def calc_fixed_sl(entry: float, side: str, deposit: float, leverage: int) -> float:
    """Цена, при которой убыток составит FIXED_SL_DEP_PCT депозита."""
    loss_usd = deposit * FIXED_SL_DEP_PCT
    pos_size = deposit * leverage        # сумма позиции
    loss_pct = loss_usd / pos_size       # доля цены
    return entry * (1 - loss_pct) if side == "LONG" else entry * (1 + loss_pct)

# ---------------------------------------------------------------------------
# ACTIVE TRADE MONITOR
# ---------------------------------------------------------------------------
async def monitor_active_trades(exchange, app: Application, broadcast):
    bot_data = app.bot_data
    signal   = bot_data["monitored_signals"][0]
    try:
        # При тиковом мониторинге используем fetch_ticker вместо fetch_ohlcv для скорости
        ticker = await exchange.fetch_ticker(PAIR_TO_SCAN, params={"price": PRICE_SOURCE})
        last_price = ticker["last"]
        
        # Для EMA и данных свечи продолжаем использовать OHLCV, но реже
        # В реальной реализации можно было бы обновлять OHLCV в отдельном таске
        ohlcv = await exchange.fetch_ohlcv(
            PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=EMA_PERIOD,
            params={"type": "swap", "price": PRICE_SOURCE}
        )
        df = calculate_features(ohlcv, drop_last=False)
        if df is None or df.empty: return

        row         = df.iloc[-1]
        last_ema    = row[f"EMA_{EMA_PERIOD}"]
        last_open   = row["open"]
        exit_status = None

        # 1) FIXED-SL — 15 % депозита
        if (signal["side"] == "LONG"  and last_price <= signal["SL_Price"]) or \
           (signal["side"] == "SHORT" and last_price >= signal["SL_Price"]):
            exit_status = "FIXED_SL"

        # 2) EMA-touch
        if exit_status is None and (
            (signal["side"] == "LONG"  and row["low"]  <= last_ema) or
            (signal["side"] == "SHORT" and row["high"] >= last_ema)
        ):
            exit_status = "EMA_TOUCH"

        # 3) trailing-stop — как раньше
        tsl = signal["trailing_stop"]
        if not tsl["activated"]:
            activation = signal["Entry_Price"] * (1 + TRAILING_STOP_STEP) if signal["side"] == "LONG" \
                         else signal["Entry_Price"] * (1 - TRAILING_STOP_STEP)
            if (signal["side"] == "LONG"  and last_price >= activation) or \
               (signal["side"] == "SHORT" and last_price <= activation):
                # Для TSL используем цену открытия последней закрытой свечи, чтобы избежать "распилов"
                tsl_price = df.iloc[-2]['open'] 
                tsl.update({"activated":True,"stop_price":tsl_price,"last_trail_price":activation})
                signal["SL_Price"] = tsl["stop_price"]
                await broadcast(app,f"🛡️ <b>СТОП-ЛОСС АКТИВИРОВАН</b>\n\nУровень: <code>{tsl['stop_price']:.4f}</code>")
                await log_tsl_update(signal["Signal_ID"], tsl["stop_price"])
        else:
            next_trail = tsl["last_trail_price"] * (1 + TRAILING_STOP_STEP) if signal["side"] == "LONG" \
                         else tsl["last_trail_price"] * (1 - TRAILING_STOP_STEP)
            if (signal["side"] == "LONG"  and last_price >= next_trail) or \
               (signal["side"] == "SHORT" and last_price <= next_trail):
                tsl_price = df.iloc[-2]['open']
                tsl["stop_price"]       = tsl_price
                tsl["last_trail_price"] = next_trail
                signal["SL_Price"]      = tsl["stop_price"]
                await broadcast(app,f"⚙️ <b>СТОП-ЛОСС ПЕРЕДВИНУТ</b>\n\nНовый уровень: <code>{tsl['stop_price']:.4f}</code>")
                await log_tsl_update(signal["Signal_ID"], tsl["stop_price"])

        if tsl["activated"] and exit_status is None and (
            (signal["side"] == "LONG"  and last_price <= tsl["stop_price"]) or
            (signal["side"] == "SHORT" and last_price >= tsl["stop_price"])
        ):
            exit_status = "TSL_HIT"

        # --- finalize trade
        if exit_status:
            pnl_pct = ((last_price - signal["Entry_Price"]) / signal["Entry_Price"]) * (1 if signal["side"]=="LONG" else -1)
            deposit  = bot_data.get("deposit", 50.0)
            leverage = bot_data.get("leverage", 100)
            pnl_usd  = deposit * leverage * pnl_pct
            pnl_pct_display = pnl_pct * 100 * leverage
            emoji = "✅" if pnl_usd > 0 else "❌"
            await broadcast(app,
                f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                f"<b>Результат: ${pnl_usd:+.2f} ({pnl_pct_display:+.2f}%)</b>"
            )
            await update_closed_trade(signal["Signal_ID"],"CLOSED",last_price,pnl_usd,pnl_pct_display,exit_status)
            bot_data["monitored_signals"].clear()

    except Exception as e:
        log.error(f"Monitor error: {e}", exc_info=True)

# ---------------------------------------------------------------------------
# SCAN FOR NEW SIGNALS
# ---------------------------------------------------------------------------
async def scan_for_signals(exchange, app: Application, broadcast):
    bot_data = app.bot_data
    try:
        ohlcv = await exchange.fetch_ohlcv(
            PAIR_TO_SCAN,
            timeframe=TIMEFRAME,
            limit=EMA_PERIOD * HIST_MULTIPLIER + 6,
            params={"type": "swap", "price": PRICE_SOURCE},
        )
        df = calculate_features(ohlcv, drop_last=True)
        if df is None or len(df) < 3:
            return

        last, prev = df.iloc[-1], df.iloc[-2]
        cur_ema, prev_ema = last[f"EMA_{EMA_PERIOD}"], prev[f"EMA_{EMA_PERIOD}"]

        state         = bot_data.get("trade_state", "SEARCHING_CROSS")
        
        if state == "SEARCHING_CROSS":
            cross_up   = prev["low"]  < prev_ema and last["high"] > cur_ema
            cross_down = prev["high"] > prev_ema and last["low"]  < cur_ema
            if cross_up or cross_down:
                bot_data.update({
                    "trade_state":         "WAITING_CONFIRMATION",
                    "cross_direction":      "UP" if cross_up else "DOWN",
                })
                side_hint = "LONG" if cross_up else "SHORT"
                await broadcast(app,
                    f"🔔 EMA {EMA_PERIOD} CROSS DETECTED → предположительный {side_hint} (ждём подтверждение)"
                )
                log.info(f"EMA cross detected ({bot_data['cross_direction']}). Waiting confirmation …")

        elif state == "WAITING_CONFIRMATION":
            body_min = min(last["open"], last["close"])
            body_max = max(last["open"], last["close"])
            if body_min <= cur_ema <= body_max:
                log.info("Second candle touches EMA — still waiting …")
                return

            side = None
            if bot_data["cross_direction"] == "UP" and last["close"] > cur_ema:
                side = "LONG"
            elif bot_data["cross_direction"] == "DOWN" and last["close"] < cur_ema:
                side = "SHORT"

            if side:
                log.info(f"Confirmation received. Executing {side} trade.")
                await execute_trade(app, broadcast, last, side)
                bot_data["trade_state"] = "SEARCHING_CROSS"
                return
            
            new_dir = "DOWN" if bot_data["cross_direction"] == "UP" else "UP"
            bot_data.update({
                "trade_state":        "WAITING_CONFIRMATION",
                "cross_direction":     new_dir,
            })
            side_hint = "SHORT" if new_dir == "DOWN" else "LONG"
            await broadcast(
                app,
                f"🔔 EMA {EMA_PERIOD} CROSS DETECTED → предположительный {side_hint} (ждём подтверждение)"
            )
            log.info(f"Reversal cross detected ({new_dir}). Waiting new confirmation …")
            return

    except Exception as e:
        log.error(f"Scanner error: {e}", exc_info=True)

# ---------------------------------------------------------------------------
# EXECUTE TRADE
# ---------------------------------------------------------------------------
async def execute_trade(app: Application, broadcast, row: pd.Series, side: str):
    entry_price = float(row["close"])
    signal_id   = f"ema_cross_strat_{int(time.time()*1000)}"

    deposit  = app.bot_data.get("deposit", 50)
    leverage = app.bot_data.get("leverage", 100)

    decision = {
        "Signal_ID":       signal_id,
        "Pair":            PAIR_TO_SCAN,
        "side":            side,
        "Entry_Price":     entry_price,
        "Status":          "ACTIVE",
        "Timestamp_UTC":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "trailing_stop":   {"activated": False, "stop_price": 0.0, "last_trail_price": 0.0},
        "SL_Price":        calc_fixed_sl(entry_price, side, deposit, leverage),
        "TP_Price":        0.0,
    }

    app.bot_data.setdefault("monitored_signals", []).append(decision)
    await log_open_trade(decision)

    await broadcast(
        app,
        (
            f"🔥 <b>НОВЫЙ СИГНАЛ ({side})</b>\n\n"
            f"<b>Пара:</b> {PAIR_TO_SCAN}\n"
            f"<b>Вход:</b> <code>{entry_price:.4f}</code>\n"
            f"<b>Стоп:</b> <code>{decision['SL_Price']:.4f}</code> (15 % депозита)\n"
            f"<b>Выход:</b> Касание EMA {EMA_PERIOD} или трейлинг-стоп."
        ),
    )

# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
async def scanner_main_loop(app: Application, broadcast):
    log.info("EMA Cross Strategy Engine loop starting …")
    app.bot_data["trade_state"] = "SEARCHING_CROSS"

    exchange = ccxt.mexc({
        "options": {"defaultType": "swap"}, 
        "enableRateLimit": True,
        "rateLimit": 100,  # 100 ms = 10 req/sec
    })
    await exchange.load_markets()

    while app.bot_data.get("bot_on", False):
        try:
            if not app.bot_data.get("monitored_signals"):
                await scan_for_signals(exchange, app, broadcast)
            else:
                await monitor_active_trades(exchange, app, broadcast)
        finally:
            if app.bot_data.get("monitored_signals"):
                # есть позиция → быстрый опрос
                await asyncio.sleep(TICK_INTERVAL)
            else:
                # ждём закрытия следующей минуты + буфер
                now       = datetime.now(timezone.utc)
                next_min  = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
                sleep_sec = (next_min - now).total_seconds() + API_BUFFER
                if sleep_sec > 0:
                    await asyncio.sleep(sleep_sec)

    await exchange.close()
    log.info("EMA Cross Strategy Engine loop stopped.")
