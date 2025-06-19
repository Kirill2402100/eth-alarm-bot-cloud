# eth_alarm_bot.py
import os, asyncio, json, logging
from datetime import datetime

import numpy as np
import pandas as pd
import ccxt.async_support as ccxt
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    ContextTypes, Defaults
)

###############################################################################
# Константы окружения
###############################################################################
BOT_TOKEN      = os.getenv("BOT_TOKEN")
CHAT_IDS       = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
PAIR_RAW       = os.getenv("PAIR", "BTC-USDT-SWAP")
SHEET_ID       = os.getenv("SHEET_ID")
INIT_LEVERAGE  = int(os.getenv("LEVERAGE", 1))

###############################################################################
# Логирование
###############################################################################
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("bot")

###############################################################################
# Google-Sheets helper (без изменений)
###############################################################################
_GS_SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
if os.getenv("GOOGLE_CREDENTIALS"):
    creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    _creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, _GS_SCOPE)
    _gs = gspread.authorize(_creds)
else:
    _gs = None
    log.warning("GOOGLE_CREDENTIALS not set — Sheets logging disabled.")

def _open_worksheet(sheet_id: str, title: str):
    if not _gs:
        return None
    ss = _gs.open_by_key(sheet_id)
    try:
        return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        return ss.add_worksheet(title, rows=1000, cols=20)

HEADERS = ["DATE-TIME", "POSITION", "DEPOSIT", "ENTRY",
           "STOP LOSS", "TAKE PROFIT", "RR", "P&L (USDT)", "APR (%)"]
WS = None
if SHEET_ID:
    WS = _open_worksheet(SHEET_ID, "AI")
    if WS and WS.row_values(1) != HEADERS:
        WS.clear(); WS.append_row(HEADERS)
elif SHEET_ID is None:
    log.warning("SHEET_ID not set — Sheets logging disabled.")

###############################################################################
# Биржа OKX
###############################################################################
exchange = ccxt.okx({
    "apiKey":    os.getenv("OKX_API_KEY"),
    "secret":    os.getenv("OKX_SECRET"),
    "password":  os.getenv("OKX_PASSWORD"),
    "options":   {"defaultType": "swap"},
    "enableRateLimit": True,
    # "verbose": True,  # включайте при отладке
})

PAIR = PAIR_RAW.replace("/", "-").replace(":USDT", "").upper()
if "-SWAP" not in PAIR:
    PAIR += "-SWAP"
log.info(f"Using trading pair: {PAIR}")

###############################################################################
# ВРЕМЕННЫЙ патч загрузки рынков (обходит баг старых ccxt)
###############################################################################
async def safe_load_okx_markets():
    """Пытаемся загрузить рынки OKX, обходя известный parse_market-баг."""
    try:
        return await exchange.load_markets()       # обычный путь
    except TypeError as e:
        if "NoneType" in str(e) and "symbol = base" in str(e):
            log.warning("OKX parse_market bug caught — retry with SWAP only")
            return await exchange.load_markets({"instType": "SWAP"})
        raise  # любая другая ошибка — прокидываем выше

###############################################################################
# Стратегия: SSL-канал + RSI (без изменений)
###############################################################################
def _calc_rsi(series: pd.Series, length=14):
    delta = series.diff()
    gain = (delta.clip(lower=0)).rolling(length).mean()
    loss = (-delta.clip(upper=0)).rolling(length).mean()
    rs   = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_ssl(df: pd.DataFrame):
    sma = df['close'].rolling(13).mean()
    ssl_up, ssl_dn = [], []
    for i in range(len(df)):
        if i < 12:
            ssl_up.append(None); ssl_dn.append(None); continue
        h = df['high'].iloc[i-12:i+1].max()
        l = df['low' ].iloc[i-12:i+1].min()
        if df['close'].iloc[i] > sma.iloc[i]:
            ssl_up.append(h); ssl_dn.append(l)
        else:
            ssl_up.append(l); ssl_dn.append(h)
    df['ssl_up'], df['ssl_dn'] = ssl_up, ssl_dn
    df['ssl_sig'] = None
    for i in range(1, len(df)):
        if pd.notna(df['ssl_up'].iloc[i]) and pd.notna(df['ssl_dn'].iloc[i-1]):
            if df['ssl_up'].iloc[i-1] < df['ssl_dn'].iloc[i-1] and df['ssl_up'].iloc[i] > df['ssl_dn'].iloc[i]:
                df.at[df.index[i], 'ssl_sig'] = "LONG"
            elif df['ssl_up'].iloc[i-1] > df['ssl_dn'].iloc[i-1] and df['ssl_up'].iloc[i] < df['ssl_dn'].iloc[i]:
                df.at[df.index[i], 'ssl_sig'] = "SHORT"
    df['rsi'] = _calc_rsi(df['close'])
    return df

###############################################################################
# Текущие настройки стратегии
###############################################################################
state = {
    "monitoring": False,
    "leverage":   INIT_LEVERAGE,
}

###############################################################################
# Telegram-команды
###############################################################################
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.application.chat_ids.add(update.effective_chat.id)
    state["monitoring"] = True
    await update.message.reply_text("✅ Monitoring ON")
    if not ctx.chat_data.get("task"):
        ctx.chat_data["task"] = asyncio.create_task(monitor(ctx))

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["monitoring"] = False
    await update.message.reply_text("⛔ Monitoring OFF")

async def cmd_leverage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Использование: <code>/leverage 3</code>")
        return
    lev = int(parts[1])
    state["leverage"] = max(1, min(100, lev))
    await update.message.reply_text(f"🛠 Leverage set → {lev}x")

###############################################################################
# Основной мониторинг
###############################################################################
async def monitor(ctx: ContextTypes.DEFAULT_TYPE):
    log.info("monitor() loop started")
    while True:
        if not state["monitoring"]:
            await asyncio.sleep(2); continue
        try:
            ohlcv = await exchange.fetch_ohlcv(PAIR, timeframe="15m", limit=150)
            df = pd.DataFrame(ohlcv, columns=['ts','open','high','low','close','vol'])
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df = calculate_ssl(df)
            sigs = df.dropna(subset=['ssl_sig'])
            if len(sigs) >= 2 and sigs.iloc[-1]['ssl_sig'] != sigs.iloc[-2]['ssl_sig']:
                sig   = sigs.iloc[-1]['ssl_sig']
                price = df['close'].iloc[-1]
                rsi   = df['rsi'  ].iloc[-1]
                cond_price = price >= 1.002*df['close'].iloc[-2] if sig=="LONG" else price <= 0.998*df['close'].iloc[-2]
                cond_rsi   = rsi > 55 if sig=="LONG" else rsi < 45
                if cond_price and cond_rsi:
                    await send_signal(ctx, sig, price, rsi)
        except Exception as e:
            log.exception("monitor-loop error: %s", e)
        await asyncio.sleep(30)

async def send_signal(ctx: ContextTypes.DEFAULT_TYPE, sig: str, price: float, rsi: float):
    txt = (f"📡 <b>Signal → {sig}</b>\n"
           f"💰 Price: <code>{price:.2f}</code>\n"
           f"📈 RSI: {rsi:.1f}\n"
           f"⏰ {datetime.utcnow():%H:%M:%S UTC}")
    for cid in ctx.application.chat_ids:
        try:
            await ctx.application.bot.send_message(cid, txt)
        except Exception as e:
            log.warning("send_signal: %s", e)

###############################################################################
# post_shutdown — гарантированно закрываем биржу
###############################################################################
async def post_shutdown_hook(app: Application):
    log.info("post_shutdown_hook → closing OKX client…")
    await exchange.close()

###############################################################################
# Точка входа
###############################################################################
async def main():
    defaults = Defaults(parse_mode="HTML")
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .defaults(defaults)
        .post_shutdown(post_shutdown_hook)
        .build()
    )
    app.chat_ids = set(CHAT_IDS)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop",  cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_leverage))

    async with app:
        await app.initialize()
        await safe_load_okx_markets()
        bal = await exchange.fetch_balance()
        log.info("USDT balance: %s", bal["total"].get("USDT", "N/A"))

        await app.start()
        await app.updater.start_polling()
        log.info("Bot polling started.")

        try:
            await asyncio.Event().wait()  # run forever
        finally:
            await app.updater.stop()
            await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
