# eth_alarm_bot.py
import os, asyncio, json, logging, math, time
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
# ─── Константы окружения ─────────────────────────────────────────────────────
###############################################################################
BOT_TOKEN      = os.getenv("BOT_TOKEN")
CHAT_IDS       = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
PAIR_RAW       = os.getenv("PAIR", "BTC-USDT-SWAP")
SHEET_ID       = os.getenv("SHEET_ID")
INIT_LEVERAGE  = int(os.getenv("LEVERAGE", 1))

###############################################################################
# ─── Логирование ─────────────────────────────────────────────────────────────
###############################################################################
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("bot")

# глушим подробный вывод httpx (telegram)
for noisy in ("httpx", "telegram.vendor.httpx"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

###############################################################################
# ─── Google Sheets helper ────────────────────────────────────────────────────
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

###############################################################################
# ─── OKX биржа ───────────────────────────────────────────────────────────────
###############################################################################
exchange = ccxt.okx({
    "apiKey":   os.getenv("OKX_API_KEY"),
    "secret":   os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "options":  {"defaultType": "swap"},
    "enableRateLimit": True,
    # "verbose": True,     # включайте при отладке
})

PAIR = PAIR_RAW.replace("/", "-").replace(":USDT", "").upper()
if "-SWAP" not in PAIR:
    PAIR += "-SWAP"
log.info(f"Using trading pair: {PAIR}")

async def safe_load_okx_markets():
    """Обходит старую ошибку parse_market (ccxt < 4.4.87)."""
    try:
        return await exchange.load_markets()
    except TypeError as e:
        if "NoneType" in str(e) and "symbol = base" in str(e):
            log.warning("OKX parse_market bug caught — retry with SWAP only")
            return await exchange.load_markets({"instType": "SWAP"})
        raise

###############################################################################
# ─── Стратегия SSL-канал + RSI ───────────────────────────────────────────────
###############################################################################
def _calc_rsi(series: pd.Series, length=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(length).mean()
    loss  = (-delta.clip(upper=0)).rolling(length).mean()
    return 100 - 100 / (1 + gain / loss)

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
# ─── Состояние бота ──────────────────────────────────────────────────────────
###############################################################################
state = {
    "monitoring": False,
    "leverage":   INIT_LEVERAGE,
    "position":   None,   # dict | None
}

###############################################################################
# ─── Telegram-команды ────────────────────────────────────────────────────────
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
# ─── Торговые функции ────────────────────────────────────────────────────────
###############################################################################
async def get_free_usdt():
    bal = await exchange.fetch_balance()
    # OKX иногда отдаёт free/available в разных ключах
    return bal["USDT"].get("available") or bal["USDT"].get("free") or 0

async def open_position(side: str, price: float, ctx: ContextTypes.DEFAULT_TYPE):
    """Открывает позицию целым депозитом × плечо, сохраняет её в state."""
    usdt = await get_free_usdt()
    if usdt <= 0:
        await ctx.application.bot.send_message(list(ctx.application.chat_ids)[0],
            "❗ Нет доступного USDT для открытия позиции")
        return
    qty = round((usdt * state["leverage"]) / price, 4)  # округляем до 0.0001
    if qty <= 0:
        return
    await exchange.set_leverage(state["leverage"], symbol=PAIR)

    # buy для LONG, sell для SHORT
    side_order = "buy" if side == "LONG" else "sell"
    order = await exchange.create_market_order(PAIR, side_order, qty)
    entry = order["average"] or price

    tp = entry * (1.005 if side == "LONG" else 0.995)
    sl = entry * (0.995 if side == "LONG" else 1.005)

    state["position"] = {
        "side": side,
        "amount": qty,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "deposit": usdt,
        "opened": time.time(),
    }

    txt = (f"🟢 <b>Позиция открыта {side}</b>\n"
           f"💰 Депозит: <code>{usdt:.2f}</code>\n"
           f"🎯 Entry: <code>{entry:.2f}</code>\n"
           f"⛔ SL: <code>{sl:.2f}</code>\n"
           f"🏁 TP: <code>{tp:.2f}</code>")
    await broadcast(ctx, txt)

async def close_position(reason: str, price: float, ctx: ContextTypes.DEFAULT_TYPE):
    """Закрывает текущую позицию по рынку, пишет в Sheets, очищает state."""
    pos = state["position"]
    if not pos:
        return
    side_order = "sell" if pos["side"] == "LONG" else "buy"
    order = await exchange.create_market_order(PAIR, side_order, pos["amount"], params={"reduceOnly": True})
    close_price = order["average"] or price
    pnl = (close_price - pos["entry"]) * pos["amount"]
    if pos["side"] == "SHORT":
        pnl = -pnl
    days = max((time.time() - pos["opened"]) / 86400, 1e-9)
    apr = (pnl / pos["deposit"]) * (365 / days) * 100

    txt = (f"🔴 <b>Позиция закрыта: {reason}</b>\n"
           f"💰 Депозит: <code>{pos['deposit']:.2f}</code>\n"
           f"📈 Close: <code>{close_price:.2f}</code>\n"
           f"🧮 P&L: <code>{pnl:.2f}</code>\n"
           f"📅 APR: <code>{apr:.2f}%</code>")
    await broadcast(ctx, txt)

    # запись в Google Sheets
    if WS:
        rr = round(abs((pos['tp'] - pos['entry']) / (pos['entry'] - pos['sl'])), 2)
        WS.append_row([
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            pos["side"],
            pos["deposit"],
            pos["entry"],
            pos["sl"],
            pos["tp"],
            rr,
            pnl,
            round(apr, 2),
        ])

    state["position"] = None

async def broadcast(ctx, text: str):
    for cid in ctx.application.chat_ids:
        try:
            await ctx.application.bot.send_message(cid, text)
        except Exception as e:
            log.warning("broadcast: %s", e)

###############################################################################
# ─── Основной мониторинг ────────────────────────────────────────────────────
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
            price = df['close'].iloc[-1]
            rsi   = df['rsi'  ].iloc[-1]

            # 1) Проверка TP/SL
            pos = state["position"]
            if pos:
                hit_tp = price >= pos["tp"] if pos["side"]=="LONG" else price <= pos["tp"]
                hit_sl = price <= pos["sl"] if pos["side"]=="LONG" else price >= pos["sl"]
                if hit_tp:
                    await close_position("TP", price, ctx)
                elif hit_sl:
                    await close_position("SL", price, ctx)

            # 2) Сигналы стратегии
            sigs = df.dropna(subset=['ssl_sig'])
            if len(sigs) >= 2 and sigs.iloc[-1]['ssl_sig'] != sigs.iloc[-2]['ssl_sig']:
                sig = sigs.iloc[-1]['ssl_sig']
                cond_price = (
                    price >= 1.002*df['close'].iloc[-2] if sig=="LONG"
                    else price <= 0.998*df['close'].iloc[-2]
                )
                cond_rsi = (rsi > 55) if sig=="LONG" else (rsi < 45)
                if cond_price and cond_rsi:
                    if pos:
                        # если тренд сменился — закрываем и переворачиваемся
                        if pos["side"] != sig:
                            await close_position("смена тренда", price, ctx)
                            await open_position(sig, price, ctx)
                    else:
                        await open_position(sig, price, ctx)

        except Exception as e:
            log.exception("monitor-loop error: %s", e)
        await asyncio.sleep(30)

###############################################################################
# ─── Graceful shutdown ──────────────────────────────────────────────────────
###############################################################################
async def post_shutdown_hook(app: Application):
    log.info("post_shutdown_hook → closing OKX client…")
    await exchange.close()

###############################################################################
# ─── Точка входа ────────────────────────────────────────────────────────────
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
