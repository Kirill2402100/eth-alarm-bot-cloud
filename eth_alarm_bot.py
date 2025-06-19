import os
import asyncio
import json
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import ccxt
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ──────────────────── ENV ────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")                       # токен Telegram-бота
CHAT_IDS  = {int(cid) for cid in os.getenv("CHAT_IDS", "").split(",") if cid}

PAIR      = os.getenv("PAIR", "BTC/USDT")               # торгуемая пара
SHEET_ID  = os.getenv("SHEET_ID")                       # таблица логов

LEVERAGE  = int(os.getenv("LEVERAGE", 1))               # плечо ( /leverage )

########################################################################
#                    ─────  Google Sheets настройка  ─────             #
########################################################################
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gs = gspread.authorize(creds)
LOGS_WS = gs.open_by_key(SHEET_ID).worksheet("LP_Logs")

HEADERS = [
    "DATE - TIME", "POSITION", "DEPOSIT USDT", "ENTRY", "STOP LOSS",
    "TAKE PROFIT", "RR", "P&L (USDT)", "APR (%)"
]
if LOGS_WS.row_values(1) != HEADERS:
    LOGS_WS.resize(rows=1)
    LOGS_WS.update("A1", [HEADERS])

########################################################################
#                           ─────  state  ─────                       #
########################################################################
current_signal : str|None = None     # 'LONG' | 'SHORT'
position_open  : bool     = False
monitoring     : bool     = False

# ключевые параметры сделки
entry_price  = sl_price = tp_price = 0.0
entry_equity = 0.0                   # депозит до входа

########################################################################
#                       ─────  EXCHANGE OKX  ─────                     #
########################################################################
exchange = ccxt.okx({
    "apiKey"        : os.getenv("OKX_API_KEY"),
    "secret"        : os.getenv("OKX_SECRET"),
    "password"      : os.getenv("OKX_PASSWORD"),
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",       # бессрочные фьючи
    }
})

# применяем плечо для аккаунта swap
async def set_leverage():
    try:
        exchange.set_leverage(LEVERAGE, PAIR, {"marginMode": "isolated"})
    except Exception as e:
        print("[warn] leverage:", e)

########################################################################
#                     ─────  стратегия SSL + RSI  ─────                #
########################################################################
def calculate_ssl(df: pd.DataFrame) -> pd.DataFrame:
    sma = df["close"].rolling(13).mean()
    hlv = (df["close"] > sma).astype(int)

    ssl_up, ssl_down = [], []
    for i in range(len(df)):
        if i < 12:
            ssl_up.append(None)
            ssl_down.append(None)
            continue
        if hlv.iloc[i] == 1:
            ssl_up.append(df["high"].iloc[i - 12:i + 1].max())
            ssl_down.append(df["low"].iloc[i - 12:i + 1].min())
        else:
            ssl_up.append(df["low"].iloc[i - 12:i + 1].min())
            ssl_down.append(df["high"].iloc[i - 12:i + 1].max())

    df["ssl_up"], df["ssl_down"], df["ssl_channel"] = ssl_up, ssl_down, None
    for i in range(1, len(df)):
        if pd.notna(df["ssl_up"].iloc[i]) and pd.notna(df["ssl_down"].iloc[i]):
            prev_up, prev_dn = df["ssl_up"].iloc[i - 1], df["ssl_down"].iloc[i - 1]
            curr_up, curr_dn = df["ssl_up"].iloc[i], df["ssl_down"].iloc[i]
            if prev_up < prev_dn and curr_up > curr_dn:
                df.at[df.index[i], "ssl_channel"] = "LONG"
            elif prev_up > prev_dn and curr_up < curr_dn:
                df.at[df.index[i], "ssl_channel"] = "SHORT"
    return df


async def fetch_signal() -> tuple[str|None, float]:
    """Возвращает ('LONG'|'SHORT'|None, last_price)."""
    try:
        ohlcv = exchange.fetch_ohlcv(PAIR, timeframe="15m", limit=200)
    except Exception as e:
        print("[error] fetch_ohlcv:", e)
        return None, 0.0

    df = pd.DataFrame(
        ohlcv, columns=["ts", "open", "high", "low", "close", "vol"]
    )
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    df.set_index("ts", inplace=True)

    df = calculate_ssl(df)
    df["rsi"] = ta_rsi(df["close"], 14)
    df["price_confirm"] = (
        df["close"].pct_change().abs().rolling(1).sum()  # просто маркер строки
    )

    valid = df[df["ssl_channel"].notna()]
    if len(valid) < 1:
        return None, df.close.iat[-1]

    sig = valid["ssl_channel"].iat[-1]
    price = df.close.iat[-1]
    rsi = df.rsi.iat[-1]

    # фильтры
    if sig == "LONG" and rsi < 55:
        return None, price
    if sig == "SHORT" and rsi > 45:
        return None, price

    # подтверждение цены ±0.2 %
    base_price = df.close[df["ssl_channel"].notna()].iat[-1]
    if sig == "LONG" and price < base_price * 1.002:
        return None, price
    if sig == "SHORT" and price > base_price * 0.998:
        return None, price

    return sig, price


########################################################################
#                 ─────   Telegram command-handlers  ─────             #
########################################################################
async def safe_close(ex):
    try:
        await ex.close()
    except RuntimeError as e:
        if "event loop is already running" in str(e):
            pass
        else:
            raise

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global monitoring
    CHAT_IDS.add(update.effective_chat.id)
    monitoring = True
    await update.message.reply_text("✅ Мониторинг ON")
    asyncio.create_task(monitor(ctx.application))

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global monitoring
    monitoring = False
    await update.message.reply_text("🛑 Мониторинг OFF")
    await safe_close(exchange)

async def cmd_leverage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global LEVERAGE
    try:
        lev = int(ctx.args[0])
        LEVERAGE = max(1, min(lev, 50))
        await set_leverage()
        await update.message.reply_text(f"⚙️ Плечо установлено: {LEVERAGE}x")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

########################################################################
#                    ─────   мониторинг сигналов  ─────                #
########################################################################
async def open_trade(sig: str, price: float):
    """Маркет-ордер 0.01 BTC / эквив. (можно менять)."""
    side = "buy" if sig == "LONG" else "sell"
    amount = 0.01
    order = exchange.create_order(PAIR, "market", side, amount)
    return order

async def monitor(app):
    while monitoring:
        sig, price = await fetch_signal()
        if sig:
            txt = f"🎯 {sig} signal @ {price:.2f}"
            for cid in CHAT_IDS:
                await app.bot.send_message(cid, txt)
            try:
                order = await open_trade(sig, price)
                for cid in CHAT_IDS:
                    await app.bot.send_message(cid, f"✅ Сделка открыта: {order['id']}")
            except Exception as e:
                for cid in CHAT_IDS:
                    await app.bot.send_message(cid, f"⛔️ Ошибка ордера: {e}")
        await asyncio.sleep(30)

########################################################################
#                              ─────  run  ─────                       #
########################################################################
def ta_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """простая RSI без сторонних либ."""
    delta = series.diff()
    up, down = delta.clip(lower=0), -delta.clip(upper=0)
    roll_up = up.ewm(span=period, adjust=False).mean()
    roll_dn = down.ewm(span=period, adjust=False).mean()
    rs = roll_up / roll_dn
    return 100 - (100 / (1 + rs))

async def main():
    await set_leverage()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .drop_pending_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("stop",      cmd_stop))
    app.add_handler(CommandHandler("leverage",  cmd_leverage))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await app.updater.idle()

    # корректное закрытие при Ctrl-C / SIGTERM
    await safe_close(exchange)
    await app.stop()
    await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
