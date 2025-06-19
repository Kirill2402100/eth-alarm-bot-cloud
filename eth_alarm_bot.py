import os, json, asyncio, logging
from datetime import datetime, timezone

import pandas as pd
import numpy as np
import ccxt.async_support as ccxt       # --------------- async! ------------
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# ─────────────────────── ENV
BOT_TOKEN  = os.getenv("BOT_TOKEN")
CHAT_IDS   = {int(x) for x in os.getenv("CHAT_IDS", "").split(",") if x}
PAIR       = os.getenv("PAIR")               # BTC-USDT-SWAP  (strict!)
LEVERAGE   = int(os.getenv("LEVERAGE", 1))
SHEET_ID   = os.getenv("SHEET_ID")

if not (BOT_TOKEN and CHAT_IDS and PAIR and SHEET_ID):
    raise RuntimeError("❌ One or more required env vars are missing")

# ─────────────────────── Google Sheets
scope = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
creds   = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gs      = gspread.authorize(creds)
ws      = gs.open_by_key(SHEET_ID).worksheet("AI")

HEADERS = [
    "DATE-TIME", "POSITION", "DEPOSIT", "ENTRY", "STOP LOSS", "TAKE PROFIT",
    "RR", "P&L (USDT)", "APR (%)",
]
if ws.row_values(1) != HEADERS:
    ws.clear()
    ws.update("A1", [HEADERS])

# ─────────────────────── Exchange (OKX async)
exchange = ccxt.okx({
    "apiKey":   os.getenv("OKX_API_KEY"),
    "secret":   os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "options":  {"defaultType": "swap"},
    "enableRateLimit": True,
})

# ensure the pair really exists
markets = asyncio.run(exchange.load_markets(params={"instType": "SWAP"}))
if PAIR not in markets:
    raise ValueError(f"PAIR '{PAIR}' not found on OKX; "
                     f"did you mean e.g. BTC-USDT-SWAP ?")

# ─────────────────────── Strategy helpers
def calculate_ssl(df: pd.DataFrame) -> pd.DataFrame:
    sma = df.close.rolling(13).mean()
    hlv = (df.close > sma).astype(int)

    ssl_up, ssl_dn = [], []
    for i in range(len(df)):
        if i < 12:
            ssl_up.append(np.nan)
            ssl_dn.append(np.nan)
            continue
        window_high = df.high[i-12:i+1]
        window_low  = df.low [i-12:i+1]
        if hlv.iloc[i]:
            ssl_up.append(window_high.max())
            ssl_dn.append(window_low.min())
        else:
            ssl_up.append(window_low.min())
            ssl_dn.append(window_high.max())

    df["ssl_up"] = ssl_up
    df["ssl_dn"] = ssl_dn
    df["signal"] = np.nan

    for i in range(1, len(df)):
        pre, cur = df.iloc[i-1], df.iloc[i]
        if pd.notna(cur.ssl_up) and pd.notna(cur.ssl_dn):
            if pre.ssl_up < pre.ssl_dn and cur.ssl_up > cur.ssl_dn:
                df.at[df.index[i], "signal"] = "LONG"
            elif pre.ssl_up > pre.ssl_dn and cur.ssl_up < cur.ssl_dn:
                df.at[df.index[i], "signal"] = "SHORT"
    return df

async def fetch_signal() -> tuple[str|None, float]:
    ohlcv = await exchange.fetch_ohlcv(PAIR, timeframe="15m", limit=100)
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","vol"])
    df["ts"] = pd.to_datetime(df.ts, unit="ms"); df.set_index("ts", inplace=True)

    calculate_ssl(df)
    valid = df.signal.dropna()
    if len(valid) < 2:             # нет двух последовательных сигналов
        return None, df.close.iloc[-1]

    prev, cur = valid.iloc[-2], valid.iloc[-1]
    price     = df.close.iloc[-1]
    return (cur, price) if prev != cur else (None, price)

# ─────────────────────── Telegram commands
monitoring   = False
current_pos  = None      # dict with keys: dir, entry, sl, tp
log_buffer   = []

async def cmd_start(u:Update, c:ContextTypes.DEFAULT_TYPE):
    global monitoring
    CHAT_IDS.add(u.effective_chat.id)
    await u.message.reply_text("✅ Monitoring ON")
    monitoring = True
    asyncio.create_task(monitor_loop(c.application))

async def cmd_stop(u:Update,*_):
    global monitoring
    monitoring = False
    await u.message.reply_text("⏹️ Monitoring stopped")

async def cmd_leverage(u:Update,*_):
    await u.message.reply_text(
        "ℹ️ leverage регулируется через переменную LEVERAGE в Railway",
    )

# ─────────────────────── Monitoring loop
async def monitor_loop(app):
    global current_pos
    while monitoring:
        try:
            sig, price = await fetch_signal()
            if sig:
                await open_trade(sig, price)
                for cid in CHAT_IDS:
                    await app.bot.send_message(
                        cid,
                        f"📡 Signal {sig}\nPrice {price:.2f}",
                    )
        except Exception as e:
            logging.error("err %s", e)
        await asyncio.sleep(30)

async def open_trade(sig:str, price:float):
    global current_pos
    if current_pos:                       # позиция уже открыта
        return

    side   = "buy" if sig=="LONG" else "sell"
    amount = 0.01                         # минимальный шаг 0.01 BTC
    try:
        await exchange.set_leverage(LEVERAGE, PAIR)
        order = await exchange.create_order(
            PAIR, "market", side, amount
        )
        sl = price * (0.995 if sig=="LONG" else 1.005)
        tp = price * (1.005 if sig=="LONG" else 0.995)
        current_pos = dict(dir=sig, entry=price, sl=sl, tp=tp,
                           entry_time=datetime.now(timezone.utc))
        logging.info("opened %s %.2f", sig, price)
    except Exception as e:
        logging.warning("leverage %s", e)

async def shutdown():
    await exchange.close()

# ─────────────────────── main
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    app.add_handler(CommandHandler("leverage",cmd_leverage))

    async with app:
        await app.initialize()
        logging.info("bot up — waiting for /start …")
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        try:
            await asyncio.Event().wait()      # run forever
        finally:
            await app.updater.stop()
            await app.stop()
            await shutdown()

if __name__ == "__main__":
    asyncio.run(main())
