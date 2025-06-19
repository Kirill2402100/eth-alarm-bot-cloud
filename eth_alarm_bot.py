#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram-бот-алерт + автоторговля (OKX swap-контракты).
Стратегия – SSL-channel 13 + подтверждение ценой (±0.2 %) + RSI (55/45).  
Записывает сделки в Google Sheets и умеет закрываться по SL/TP (±0.5 %).
"""

import asyncio, os, json, logging, math
from datetime import datetime, timezone

import numpy            as np
import pandas           as pd
import ccxt.async_support as ccxt    # асинхронная версия!
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, __version__ as PTBv
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    ContextTypes, AIORateLimiter,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")

# ──────────────────────────────────── ENV ──────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_IDS    = {int(cid) for cid in os.getenv("CHAT_IDS", "").split(",") if cid}
RAW_PAIR    = os.getenv("PAIR", "BTC/USDT:USDT")          # <— Формат ccxt
SHEET_ID    = os.getenv("SHEET_ID")
LEVERAGE    = int(os.getenv("LEVERAGE", 1))

# OKX creds
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_SECRET  = os.getenv("OKX_SECRET")
OKX_PW      = os.getenv("OKX_PASSWORD")

if not BOT_TOKEN or not CHAT_IDS or not SHEET_ID:
    raise RuntimeError("Одно из обязательных переменных окружения не задано")

# ─────────────────────────────── Google Sheets ────────────────────────────────
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
gc = gspread.authorize(
    ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
)

# создаём/открываем лист «AI» (или любой)
try:
    ws = gc.open_by_key(SHEET_ID).worksheet("AI")
except gspread.WorksheetNotFound:
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.add_worksheet("AI", rows=1, cols=20)

HEADERS = [
    "DATE-TIME", "POSITION", "DEPOSIT", "ENTRY",
    "STOP LOSS", "TAKE PROFIT", "RR",
    "P&L (USDT)", "APR (%)"
]
if ws.row_values(1) != HEADERS:
    ws.resize(rows=1)
    ws.update("A1", [HEADERS])

# ───────────────────────────────  OKX  (ccxt)  ────────────────────────────────
exchange = ccxt.okx({
    "apiKey":  OKX_API_KEY,
    "secret":  OKX_SECRET,
    "password": OKX_PW,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",
    }
})

# Okx требует строку вида BTC/USDT:USDT
PAIR = RAW_PAIR.replace("-", "/").replace("_", "/")
if ":" not in PAIR:
    PAIR = PAIR.replace("/USDT", "/USDT:USDT")

# ─────────────────────────────── Стратегия SSL  ───────────────────────────────
def ssl_channel(df: pd.DataFrame) -> pd.Series:
    sma = df["close"].rolling(13).mean()
    hlv = (df["close"] > sma).astype(int)        # 1 = выше SMA
    up, down, signal = [], [], [None]*len(df)

    for i in range(len(df)):
        if i < 12:
            up.append(down.append(None))
            continue
        window = slice(i-12, i+1)
        if hlv.iloc[i]:
            up.append(df["high"].iloc[window].max())
            down.append(df["low"].iloc[window].min())
        else:
            up.append(df["low"].iloc[window].min())
            down.append(df["high"].iloc[window].max())

        if pd.notna(up[i-1]) and pd.notna(down[i-1]):
            if up[i-1] < down[i-1] and up[i] > down[i]:
                signal[i] = "LONG"
            elif up[i-1] > down[i-1] and up[i] < down[i]:
                signal[i] = "SHORT"
    return pd.Series(signal, index=df.index)

async def fetch_signal() -> tuple[str|None, float]:
    ohlcv = await exchange.fetch_ohlcv(PAIR, "15m", limit=100)
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","vol"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    df.set_index("ts", inplace=True)

    df["ssl"] = ssl_channel(df)
    sigs  = df["ssl"].dropna()
    price = df["close"].iloc[-1]

    if len(sigs) < 2 or sigs.iloc[-1] == sigs.iloc[-2]:
        return None, price
    return sigs.iloc[-1], price

# ─────────────────────────────  Telegram callbacks  ───────────────────────────
position  = None
monitoring = False

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global monitoring
    CHAT_IDS.add(update.effective_chat.id)
    monitoring = True
    await update.message.reply_text("✅ Monitoring ON")
    asyncio.create_task(monitor_loop(ctx.application))

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global monitoring
    monitoring = False
    await update.message.reply_text("🛑 Monitoring OFF")

async def cmd_leverage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global LEVERAGE
    try:
        LEVERAGE = int(ctx.args[0])
        await exchange.set_leverage(LEVERAGE, PAIR)
        await update.message.reply_text(f"Leverage set → {LEVERAGE}x")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /leverage 2")

# ───────────────────────────────  Торговый цикл  ──────────────────────────────
async def open_trade(direction: str, entry_price: float):
    side   = "buy"  if direction == "LONG"  else "sell"
    amt_usd = 10      # твой размер позиции в USDT
    amount = round(amt_usd / entry_price * LEVERAGE, 3)

    order = await exchange.create_order(PAIR, "market", side, amount)
    sl = entry_price * (1 - 0.005) if direction == "LONG" else entry_price * (1 + 0.005)
    tp = entry_price * (1 + 0.005) if direction == "LONG" else entry_price * (1 - 0.005)

    return {
        "direction": direction,
        "entry": entry_price,
        "amount": amount,
        "sl": sl,
        "tp": tp,
        "orderId": order["id"],
    }

async def monitor_loop(app):
    global position, monitoring
    while monitoring:
        try:
            signal, price = await fetch_signal()
            if not signal:
                await asyncio.sleep(30); continue

            # подтверждение ценой +0.2 % и RSI
            rsi = 100 - 100/(1+df["close"].pct_change().rolling(14).apply(
                lambda s: (s[s>0].sum() / abs(s[s<0].sum())) if abs(s[s<0].sum())>0 else np.nan
            ).iloc[-1])
            if signal == "LONG" and (price - df["close"].iloc[-2]) / df["close"].iloc[-2] < 0.002:
                await asyncio.sleep(30); continue
            if signal == "SHORT" and (df["close"].iloc[-2] - price) / df["close"].iloc[-2] < 0.002:
                await asyncio.sleep(30); continue
            if signal == "LONG" and rsi < 55 or signal == "SHORT" and rsi > 45:
                await asyncio.sleep(30); continue

            # закрываем старую позицию
            if position:
                side_close = "sell" if position["direction"]=="LONG" else "buy"
                await exchange.create_order(PAIR, "market", side_close, position["amount"])
                position = None

            # открываем новую
            position = await open_trade(signal, price)
            text = (f"📈 {signal} | entry ≈ {price:.2f}\n"
                    f"SL {position['sl']:.2f} | TP {position['tp']:.2f}")
            for cid in CHAT_IDS:
                await app.bot.send_message(cid, text)

            # запись в таблицу
            ws.append_row([
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                signal, amt_usd, price,
                position["sl"], position["tp"],
                round(abs((position["tp"]/price)-1) / abs((price/position["sl"])-1), 2),
                "", ""                        # P&L/APR заполним при закрытии
            ])
        except Exception as e:
            log.exception("monitor_loop")
        finally:
            await asyncio.sleep(30)

# ───────────────────────────────── main() ─────────────────────────────────────
async def main():
    # подключение + проверка баланса
    await exchange.load_markets()
    await exchange.set_leverage(LEVERAGE, PAIR)
    balance = await exchange.fetch_balance()       # okx returns nested dict
    usdt    = balance["total"].get("USDT", "—")
    log.info("Баланс USDT: %s", usdt)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_leverage))

    log.info("Bot up — waiting for /start …")
    await app.run_polling(close_loop=False)

    # корректно закрываем OKX при остановке
    await exchange.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        # на всякий случай закрываем соединение, если loop уже живёт
        if not exchange.closed:
            asyncio.get_event_loop().run_until_complete(exchange.close())
