# -*- coding: utf-8 -*-
"""
EMA-Cross strategy bot (MEXC perpetuals, 1-min)
Version: 2025-07-29
Improvements vs original
-----------------------
1. Works **only with закрытыми свечами**: последнюю, ещё forming, убираем из расчётов.
2. Пересечение ищем по high/low, а не по close → ловит истинный пробой внутри бара.
3. Добавлено оповещение в Telegram, когда кросс обнаружен (ещё до подтверждения).
4. Фильтр touch-EMA сделан мягче (проверяем тело бара, а не тень).
5. Подробное логирование всех переходов состояний.
6. «Умная» пауза синхронизирует цикл со свечами.
"""

import asyncio
import time
import logging
from datetime import datetime, timezone, timedelta

import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from telegram.ext import Application

from trade_executor import (
    log_open_trade,
    log_tsl_update,
    update_closed_trade,
)

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
PAIR_TO_SCAN = "SOL/USDT:USDT"
TIMEFRAME = "1m"
EMA_PERIOD = 200
TRAILING_STOP_STEP = 0.003  # 0.3 %
API_BUFFER = 2  # seconds after the minute has closed before we query the API

log = logging.getLogger("ema_cross_bot")

# ---------------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------------

def calculate_features(ohlcv: list, drop_last: bool = True) -> pd.DataFrame | None:
    """Build DataFrame + EMA; optionally remove the last (unclosed) candle."""
    if len(ohlcv) < EMA_PERIOD:
        return None

    if drop_last:
        ohlcv = ohlcv[:-1]  # cut away unfinished bar

    df = pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df[f"EMA_{EMA_PERIOD}"] = df.ta.ema(length=EMA_PERIOD)
    return df


async def monitor_active_trades(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    signal = bot_data["monitored_signals"][0]
    try:
        ohlcv = await exchange.fetch_ohlcv(
            PAIR_TO_SCAN, timeframe=TIMEFRAME, limit=EMA_PERIOD, params={"type": "swap"}
        )
        features_df = calculate_features(ohlcv, drop_last=False)  # allow realtime price
        if features_df is None or features_df.empty:
            return

        last_row = features_df.iloc[-1]
        last_price = last_row["close"]
        last_ema = last_row[f"EMA_{EMA_PERIOD}"]
        last_open_price = last_row["open"]

        exit_status = None
        if (
            signal["side"] == "LONG" and last_row["low"] <= last_ema
        ) or (
            signal["side"] == "SHORT" and last_row["high"] >= last_ema
        ):
            exit_status = "EMA_TOUCH"

        tsl = signal["trailing_stop"]

        # --- TRAILING-STOP activation
        if not tsl["activated"]:
            activation_price = (
                signal["Entry_Price"] * (1 + TRAILING_STOP_STEP)
                if signal["side"] == "LONG"
                else signal["Entry_Price"] * (1 - TRAILING_STOP_STEP)
            )
            if (
                signal["side"] == "LONG" and last_price >= activation_price
            ) or (
                signal["side"] == "SHORT" and last_price <= activation_price
            ):
                tsl.update(
                    {
                        "activated": True,
                        "stop_price": last_open_price,
                        "last_trail_price": activation_price,
                    }
                )
                signal["SL_Price"] = tsl["stop_price"]
                await broadcast_func(
                    app,
                    f"🛡️ <b>СТОП-ЛОСС АКТИВИРОВАН</b>\n\nУровень: <code>{tsl['stop_price']:.4f}</code>",
                )
                await log_tsl_update(signal["Signal_ID"], tsl["stop_price"])
        # --- TRAILING-STOP trail
        elif (
            (signal["side"] == "LONG")
            and last_price >= tsl["last_trail_price"] * (1 + TRAILING_STOP_STEP)
        ) or (
            (signal["side"] == "SHORT")
            and last_price <= tsl["last_trail_price"] * (1 - TRAILING_STOP_STEP)
        ):
            tsl["stop_price"] = last_open_price
            tsl["last_trail_price"] *= 1 + TRAILING_STOP_STEP if signal["side"] == "LONG" else 1 - TRAILING_STOP_STEP
            signal["SL_Price"] = tsl["stop_price"]
            await broadcast_func(
                app,
                f"⚙️ <b>СТОП-ЛОСС ПЕРЕДВИНУТ</b>\n\nНовый уровень: <code>{tsl['stop_price']:.4f}</code>",
            )
            await log_tsl_update(signal["Signal_ID"], tsl["stop_price"])

        # --- EXIT by trailing stop
        if tsl["activated"] and (
            (signal["side"] == "LONG" and last_price <= tsl["stop_price"])
            or (signal["side"] == "SHORT" and last_price >= tsl["stop_price"])
        ):
            exit_status = "TSL_HIT"

        # finalize trade
        if exit_status:
            pnl_pct_raw = (
                (last_price - signal["Entry_Price"]) / signal["Entry_Price"]
            ) * (1 if signal["side"] == "LONG" else -1)
            deposit = bot_data.get("deposit", 50.0)
            leverage = bot_data.get("leverage", 100)
            pnl_usd = deposit * leverage * pnl_pct_raw
            pnl_percent_display = pnl_pct_raw * 100 * leverage

            emoji = "✅" if pnl_usd > 0 else "❌"
            await broadcast_func(
                app,
                f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent_display:+.2f}%)</b>",
            )
            await update_closed_trade(
                signal["Signal_ID"], "CLOSED", last_price, pnl_usd, pnl_percent_display, exit_status
            )
            bot_data["monitored_signals"].clear()

    except Exception as e:
        log.error(f"Monitor error: {e}", exc_info=True)


async def scan_for_signals(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    try:
        ohlcv = await exchange.fetch_ohlcv(
            PAIR_TO_SCAN,
            timeframe=TIMEFRAME,
            limit=EMA_PERIOD + 6,  # couple of extra candles for safety
            params={"type": "swap"},
        )
        df = calculate_features(ohlcv, drop_last=True)
        if df is None or len(df) < 3:
            return

        last_row, prev_row = df.iloc[-1], df.iloc[-2]
        current_ema, prev_ema = last_row[f"EMA_{EMA_PERIOD}"], prev_row[f"EMA_{EMA_PERIOD}"]

        state = bot_data.get("trade_state", "SEARCHING_CROSS")
        candles_after = bot_data.get("candles_after_cross", 0)

        # -------------------------------------------------------------------
        # 1) SEARCH FOR CROSS
        # -------------------------------------------------------------------
        if state == "SEARCHING_CROSS":
            is_crossing_up = (prev_row["low"] < prev_ema) and (last_row["high"] > current_ema)
            is_crossing_down = (prev_row["high"] > prev_ema) and (last_row["low"] < current_ema)

            if is_crossing_up or is_crossing_down:
                bot_data.update(
                    {
                        "trade_state": "WAITING_CONFIRMATION",
                        "candles_after_cross": 1,
                        "cross_direction": "UP" if is_crossing_up else "DOWN",
                    }
                )
                # notify about the cross itself
                side_preview = "LONG" if is_crossing_up else "SHORT"
                await broadcast_func(
                    app,
                    f"🔔 EMA {EMA_PERIOD} CROSS DETECTED → предположительный {side_preview} (ждём подтверждение)",
                )
                log.info(f"EMA cross detected ({bot_data['cross_direction']}). Waiting for confirmation.")

        # -------------------------------------------------------------------
        # 2) WAIT FOR SECOND CLOSED CANDLE
        # -------------------------------------------------------------------
        elif state == "WAITING_CONFIRMATION":
            candles_after += 1
            bot_data["candles_after_cross"] = candles_after

            # мягкий фильтр: если тело второй свечи касается EMA, ждём дальше
            body_min = min(last_row["open"], last_row["close"])
            body_max = max(last_row["open"], last_row["close"])
            touches_ema = body_min <= current_ema <= body_max
            if touches_ema:
                log.info("Second candle touches EMA — still waiting …")
                return

            side = None
            if bot_data["cross_direction"] == "UP" and last_row["close"] > current_ema:
                side = "LONG"
            elif bot_data["cross_direction"] == "DOWN" and last_row["close"] < current_ema:
                side = "SHORT"

            if side:
                log.info(f"Confirmation received. Executing {side} trade.")
                await execute_trade(app, broadcast_func, last_row, side)

            # reset regardless of whether trade was opened
            bot_data["trade_state"] = "SEARCHING_CROSS"

    except Exception as e:
        log.error(f"Scan error: {e}", exc_info=True)


async def execute_trade(app: Application, broadcast_func, features: pd.Series, side: str):
    entry_price = float(features["close"])
    signal_id = f"ema_cross_strat_{int(time.time() * 1000)}"

    decision = {
        "Signal_ID": signal_id,
        "Pair": PAIR_TO_SCAN,
        "side": side,
        "Entry_Price": entry_price,
        "Status": "ACTIVE",
        "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "trailing_stop": {"activated": False, "stop_price": 0.0, "last_trail_price": 0.0},
        "SL_Price": 0.0,
        "TP_Price": 0.0,
    }

    app.bot_data.setdefault("monitored_signals", []).append(decision)
    await log_open_trade(decision)

    await broadcast_func(
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
async def scanner_main_loop(app: Application, broadcast_func):
    log.info("EMA Cross Strategy Engine loop starting …")

    app.bot_data["trade_state"] = "SEARCHING_CROSS"

    exchange = ccxt.mexc({"options": {"defaultType": "swap"}, "enableRateLimit": True})
    await exchange.load_markets()

    while app.bot_data.get("bot_on", False):
        try:
            if not app.bot_data.get("monitored_signals"):
                await scan_for_signals(exchange, app, broadcast_func)
            else:
                await monitor_active_trades(exchange, app, broadcast_func)
        finally:
            # smart sleep until a few seconds AFTER the next minute closes
            now = datetime.now(timezone.utc)
            next_minute = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
            sleep_duration = (next_minute - now).total_seconds() + API_BUFFER
            if sleep_duration > 0:
                log.info(f"Sync … sleeping {sleep_duration:.2f} s")
                await asyncio.sleep(sleep_duration)

    await exchange.close()
    log.info("EMA Cross Strategy Engine loop stopped.")
