# -*- coding: utf-8 -*-
"""
EMA-Cross strategy bot (MEXC perpetuals, 1-min)
Version: 2025-07-30

Changelog vs 2025-07-29 build
-----------------------------
1. `price="mark"` — бот использует те же свечи, что TradingView.
2. История для расчёта EMA увеличена (`EMA_PERIOD * 5`) — «прогрев» скользящей.
3. Все запросы OHLCV используют единые константы `PRICE_SOURCE` и `HIST_MULTIPLIER`.
4. Логика сигналов, трейлинга и smart-sleep сохранена.
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
TRAILING_STOP_STEP  = 0.003        # 0.3 %
API_BUFFER          = 2            # seconds after minute close before querying
PRICE_SOURCE        = "mark"       # 'trade' | 'mark' | 'index'
HIST_MULTIPLIER     = 5            # history length multiplier for EMA warm-up

log = logging.getLogger("ema_cross_bot")

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def calculate_features(ohlcv: List[list], drop_last: bool = True) -> Optional[pd.DataFrame]:
    """Return DataFrame with an EMA column; drop unfinished bar if requested."""
    if len(ohlcv) < EMA_PERIOD:
        return None
    if drop_last:
        ohlcv = ohlcv[:-1]
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df[f"EMA_{EMA_PERIOD}"] = df.ta.ema(length=EMA_PERIOD)
    return df

# ---------------------------------------------------------------------------
# ACTIVE TRADE MONITOR
# ---------------------------------------------------------------------------
async def monitor_active_trades(exchange, app: Application, broadcast):
    bot_data = app.bot_data
    signal   = bot_data["monitored_signals"][0]
    try:
        ohlcv = await exchange.fetch_ohlcv(
            PAIR_TO_SCAN,
            timeframe=TIMEFRAME,
            limit=EMA_PERIOD,
            params={"type": "swap", "price": PRICE_SOURCE},
        )
        df = calculate_features(ohlcv, drop_last=False)
        if df is None or df.empty:
            return

        row         = df.iloc[-1]
        last_price  = row["close"]
        last_ema    = row[f"EMA_{EMA_PERIOD}"]
        last_open   = row["open"]
        exit_status = None

        # Touch EMA → закрытие по сценарию
        if (signal["side"] == "LONG"  and row["low"]  <= last_ema) or \
           (signal["side"] == "SHORT" and row["high"] >= last_ema):
            exit_status = "EMA_TOUCH"

        tsl = signal["trailing_stop"]

        # --- Activation of trailing-stop
        if not tsl["activated"]:
            activation_price = (
                signal["Entry_Price"] * (1 + TRAILING_STOP_STEP)
                if signal["side"] == "LONG"
                else signal["Entry_Price"] * (1 - TRAILING_STOP_STEP)
            )
            if (signal["side"] == "LONG"  and last_price >= activation_price) or \
               (signal["side"] == "SHORT" and last_price <= activation_price):
                tsl.update({"activated": True, "stop_price": last_open, "last_trail_price": activation_price})
                signal["SL_Price"] = tsl["stop_price"]
                await broadcast(app, f"🛡️ <b>СТОП-ЛОСС АКТИВИРОВАН</b>\n\nУровень: <code>{tsl['stop_price']:.4f}</code>")
                await log_tsl_update(signal["Signal_ID"], tsl["stop_price"])

        # --- Trailing the stop
        elif (
            (signal["side"] == "LONG"  and last_price >= tsl["last_trail_price"] * (1 + TRAILING_STOP_STEP)) or
            (signal["side"] == "SHORT" and last_price <= tsl["last_trail_price"] * (1 - TRAILING_STOP_STEP))
        ):
            tsl["stop_price"]      = last_open
            tsl["last_trail_price"]*= 1 + TRAILING_STOP_STEP if signal["side"] == "LONG" else 1 - TRAILING_STOP_STEP
            signal["SL_Price"]     = tsl["stop_price"]
            await broadcast(app, f"⚙️ <b>СТОП-ЛОСС ПЕРЕДВИНУТ</b>\n\nНовый уровень: <code>{tsl['stop_price']:.4f}</code>")
            await log_tsl_update(signal["Signal_ID"], tsl["stop_price"])

        # --- Exit via trailing stop
        if tsl["activated"] and (
            (signal["side"] == "LONG"  and last_price <= tsl["stop_price"]) or
            (signal["side"] == "SHORT" and last_price >= tsl["stop_price"])
        ):
            exit_status = "TSL_HIT"

        # --- Finalise trade
        if exit_status:
            pnl_pct_raw = ((last_price - signal["Entry_Price"]) / signal["Entry_Price"]) * (1 if signal["side"] == "LONG" else -1)
            deposit     = float(bot_data.get("deposit", 50))
            leverage    = int(bot_data.get("leverage", 100))
            pnl_usd     = deposit * leverage * pnl_pct_raw
            pnl_percent = pnl_pct_raw * 100 * leverage
            emoji = "✅" if pnl_usd > 0 else "❌"
            await broadcast(app, f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n<b>Результат: ${pnl_usd:+.2f} ({pnl_percent:+.2f}%)</b>")
            await update_closed_trade(signal["Signal_ID"], "CLOSED", last_price, pnl_usd, pnl_percent, exit_status)
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
            limit=EMA_PERIOD * HIST_MULTIPLIER + 6,  # warm-up + safety margin
            params={"type": "swap", "price": PRICE_SOURCE},
        )
        df = calculate_features(ohlcv, drop_last=True)
        if df is None or len(df) < 3:
            return

        last, prev = df.iloc[-1], df.iloc[-2]
        cur_ema, prev_ema = last[f"EMA_{EMA_PERIOD}"], prev[f"EMA_{EMA_PERIOD}"]

        state          = bot_data.get("trade_state", "SEARCHING_CROSS")
        candles_after  = bot_data.get("candles_after_cross", 0)

        # -- 1) Detect cross
        if state == "SEARCHING_CROSS":
            cross_up   = prev["low"]  < prev_ema and last["high"] > cur_ema
            cross_down = prev["high"] > prev_ema and last["low"]  < cur_ema
            if cross_up or cross_down:
                bot_data.update({
                    "trade_state": "WAITING_CONFIRMATION",
                    "candles_after_cross": 1,
                    "cross_direction": "UP" if cross_up else "DOWN",
                })
                side_preview = "LONG" if cross_up else "SHORT"
                await broadcast(app, f"🔔 EMA {EMA_PERIOD} CROSS DETECTED → предположительный {side_preview} (ждём подтверждение)")
                log.info(f"EMA cross detected ({bot_data['cross_direction']}). Waiting confirmation …")

        # -- 2) Wait for confirmation candle
        elif state == "WAITING_CONFIRMATION":
            candles_after += 1
            bot_data["candles_after_cross"] = candles_after

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

            # reset FSM regardless of outcome
            bot_data["trade_state"] = "SEARCHING_CROSS"

    except Exception as e:
        log.error(f"Scan error: {e}", exc_info=True)

# ---------------------------------------------------------------------------
# EXECUTE TRADE
# ---------------------------------------------------------------------------
async def execute_trade(app: Application, broadcast, row: pd.Series, side: str):
    entry_price = float(row["close"])
    signal_id   = f"ema_cross_strat_{int(time.time()*1000)}"

    decision = {
        "Signal_ID":     signal_id,
        "Pair":          PAIR_TO_SCAN,
        "side":          side,
        "Entry_Price":   entry_price,
        "Status":        "ACTIVE",
        "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "trailing_stop": {"activated": False, "stop_price": 0.0, "last_trail_price": 0.0},
        "SL_Price":      0.0,
        "TP_Price":      0.0,
    }

    app.bot_data.setdefault("monitored_signals", []).append(decision)
    await log_open_trade(decision)

    await broadcast(
        app,
        (
            f"🔥 <b>НОВЫЙ СИГНАЛ ({side})</b>\n\n"
            f"<b>Пара:</b> {PAIR_TO_SCAN}\n"
            f"<b>Вход:</b> <code>{entry_price:.4f}</code>\n"
            f"<b>Выход:</b> Касание EMA {EMA_PERIOD} или трейлинг-стоп."
        ),
    )

# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
async def scanner_main_loop(app: Application, broadcast):
    log.info("EMA Cross Strategy Engine loop starting …")
    app.bot_data["trade_state"] = "SEARCHING_CROSS"

    exchange = ccxt.mexc({"options": {"defaultType": "swap"}, "enableRateLimit": True})
    await exchange.load_markets()

    while app.bot_data.get("bot_on", False):
        try:
            if not app.bot_data.get("monitored_signals"):
                await scan_for_signals(exchange, app, broadcast)
            else:
                await monitor_active_trades(exchange, app, broadcast)
        finally:
            # sleep until a couple of seconds AFTER the next minute closes
            now          = datetime.now(timezone.utc)
            next_minute  = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
            sleep_secs   = (next_minute - now).total_seconds() + API_BUFFER
            if sleep_secs > 0:
                log.info(f"Sync … sleeping {sleep_secs:.2f} s")
                await asyncio.sleep(sleep_secs)

    await exchange.close()
    log.info("EMA Cross Strategy Engine loop stopped.")
