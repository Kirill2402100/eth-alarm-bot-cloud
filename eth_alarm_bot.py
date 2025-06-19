"""
Telegram-бот для мониторинга SSL-13 + RSI
и автотрейда на OKX Futures (perpetual swap).

⛔️ ТРЕБУЕТ переменные окружения:
    BOT_TOKEN            — токен Telegram-бота
    CHAT_IDS             — id чатов через запятую (пример: "-100111,...,123456")
    OKX_API_KEY
    OKX_SECRET
    OKX_PASSWORD
    GOOGLE_CREDENTIALS   — JSON service-account одним куском
    SHEET_ID             — id таблицы Google Sheets
––  НЕОБЯЗАТЕЛЬНО:
    PAIR       (по умолчанию  "BTC/USDT:USDT")
    LEVERAGE   (по умолчанию  1)
    DEPOSIT_USDT (по умолчанию 10) — сколько USDT на одну сделку
"""

import os, json, asyncio, logging, math
from datetime import datetime, timezone

import ccxt.async_support as ccxt     # 🔸 асинхронный модуль!
import gspread
import numpy as np
import pandas as pd
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    ContextTypes
)

# ──────────────────────────── ЛОГИ ───────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)8s | %(message)s"
)
log = logging.getLogger("bot")

# ─────────────────────── ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_IDS    = {int(x) for x in os.getenv("CHAT_IDS", "0").split(",") if x}
PAIR        = os.getenv("PAIR", "BTC/USDT:USDT")          # OKX формат
LEVERAGE    = int(float(os.getenv("LEVERAGE", 1)))
DEPOSIT_USDT= float(os.getenv("DEPOSIT_USDT", 10))
SHEET_ID    = os.getenv("SHEET_ID")

# sanity-check
if not BOT_TOKEN:
    raise RuntimeError("⛔️ BOT_TOKEN не задан")
if not CHAT_IDS:
    log.warning("CHAT_IDS пуст – бот будет отвечать только отправителю команды /start")

# ──────────────────────── GOOGLE SHEETS ──────────────────────────
scope = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(
    json.loads(os.getenv("GOOGLE_CREDENTIALS")), scope
)
gs = gspread.authorize(creds)
ws = gs.open_by_key(SHEET_ID).worksheet("LP_Logs")

HEADERS = ["DATE - TIME","POSITION","DEPOSIT","ENTRY","STOP LOSS",
           "TAKE PROFIT","RR","P&L (USDT)","APR (%)"]
if ws.row_values(1) != HEADERS:
    ws.resize(rows=1)
    ws.update('A1', [HEADERS])

# ────────────────────────── ОБМЕН OKX ────────────────────────────
exchange = ccxt.okx({
    "apiKey":    os.getenv("OKX_API_KEY"),
    "secret":    os.getenv("OKX_SECRET"),
    "password":  os.getenv("OKX_PASSWORD"),
    "enableRateLimit": True,
    "options":   {"defaultType": "swap"}
})

# будем закрывать коннектор аккуратно
async def close_exchange():
    try:
        await exchange.close()
    except Exception:
        pass

# ────────────────────────── СТРАТЕГИЯ SSL ───────────────────────
def ssl_channel(df: pd.DataFrame) -> pd.Series:
    sma = df['close'].rolling(13).mean()
    hlv = (df['close'] > sma).astype(int)
    ssl_up, ssl_dn, signal = [], [], [None]*len(df)

    for i in range(len(df)):
        if i < 12:
            ssl_up.append(np.nan); ssl_dn.append(np.nan)
            continue
        box_hi = df['high'].iloc[i-12:i+1]
        box_lo = df['low'].iloc[i-12:i+1]
        if hlv.iat[i]:
            ssl_up.append(box_hi.max()); ssl_dn.append(box_lo.min())
        else:
            ssl_up.append(box_lo.min()); ssl_dn.append(box_hi.max())

        if pd.notna(ssl_up[-2]) and pd.notna(ssl_dn[-2]):
            if ssl_up[-2] < ssl_dn[-2] and ssl_up[-1] > ssl_dn[-1]:
                signal[i] = "LONG"
            elif ssl_up[-2] > ssl_dn[-2] and ssl_up[-1] < ssl_dn[-1]:
                signal[i] = "SHORT"

    df["ssl_up"], df["ssl_dn"] = ssl_up, ssl_dn
    return pd.Series(signal, index=df.index)

async def get_signal():
    ohlcv = await exchange.fetch_ohlcv(PAIR, timeframe='15m', limit=100)
    df = pd.DataFrame(ohlcv, columns=['ts','open','high','low','close','vol'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    df.set_index('ts', inplace=True)

    sig = ssl_channel(df).dropna()
    if len(sig) < 2:                                 # мало истории
        return None, df['close'].iat[-1]

    prev, curr = sig.iloc[-2], sig.iloc[-1]
    price = df['close'].iat[-1]
    return (curr if curr != prev else None), price

# ──────────────────────────── STATE ──────────────────────────────
current_sig = None
monitoring  = False
pos         = None   # хранит dict позиции (direction, entry, etc)

# ─────────────── ФУНКЦИИ TRADE + ЛОГИРОВАНИЕ ─────────────────────
async def open_trade(direction:str, price:float):
    global pos
    m = await exchange.load_markets()
    mkt = m[PAIR]
    # amount = DEPOSIT_USDT / price  с учётом минимального количества
    amt_prec = mkt['precision']['amount']
    amount   = round(DEPOSIT_USDT / price, amt_prec)
    side     = "buy" if direction=="LONG" else "sell"

    await exchange.set_leverage(LEVERAGE, PAIR)
    order = await exchange.create_order(PAIR, "market", side, amount)
    log.info("Открыта позиция %s @ %.4f, qty=%s", direction, price, amount)

    sl = round(price * (1-0.005) if direction=="LONG" else price * (1+0.005), mkt['precision']['price'])
    tp = round(price * (1+0.005) if direction=="LONG" else price * (1-0.005), mkt['precision']['price'])

    pos = dict(direction=direction, entry=price, sl=sl, tp=tp,
               entry_time=datetime.now(timezone.utc), qty=amount)

    return order, sl, tp

async def close_trade(price:float, reason:str):
    global pos
    if not pos: return
    side = "sell" if pos['direction']=="LONG" else "buy"
    await exchange.create_order(PAIR, "market", side, pos['qty'])
    pnl = (price-pos['entry'])*(1 if pos['direction']=="LONG" else -1)*pos['qty']
    apr = (pnl/DEPOSIT_USDT)*100*365/( (datetime.now(timezone.utc)-pos['entry_time']).total_seconds()/86400 )
    row = [datetime.now().strftime('%Y-%m-%d %H:%M'),
           pos['direction'], DEPOSIT_USDT, pos['entry'],
           pos['sl'], pos['tp'], 1, round(pnl,2), round(apr,2)]
    ws.append_row(row, value_input_option="USER_ENTERED")
    pos = None
    log.info("Позиция закрыта (%s), PnL=%.2f", reason, pnl)

# ──────────────────────── TELEGRAM COMMANDS ──────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global monitoring
    ctx.application.chat_ids.add(update.effective_chat.id)
    if not monitoring:
        monitoring = True
        await update.message.reply_text("✅ Monitoring ON")
        ctx.application.create_task(monitor_loop(ctx.application))
    else:
        await update.message.reply_text("Уже запущен ✔️")

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global monitoring
    monitoring = False
    await update.message.reply_text("🛑 Monitoring OFF")

async def cmd_leverage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global LEVERAGE
    try:
        new_lev = int(ctx.args[0])
        if not 1 <= new_lev <= 100:
            raise ValueError
        LEVERAGE = new_lev
        await update.message.reply_text(f"Leverage set ➜ {LEVERAGE}x")
    except Exception:
        await update.message.reply_text("Usage: /leverage 3")

# ────────────────────────── MONITOR LOOP ─────────────────────────
async def monitor_loop(app):
    global current_sig
    log.info("Старт мониторинга …")
    while monitoring:
        try:
            sig, price = await get_signal()
            if sig and sig != current_sig:
                current_sig = sig
                txt = f"📡 Signal: {sig}\n💰 Price: {price:.2f}"
                for cid in app.chat_ids:
                    await app.bot.send_message(cid, txt)

                # подтверждения цены/RSI
                await asyncio.sleep(30)
                rsipass = True        # TODO: добавить реальную проверку RSI
                p2      = price*1.002 if sig=="LONG" else price*0.998
                if rsipass:
                    await open_trade(sig, price)

        except Exception as e:
            log.error("[err] %s", e)
        await asyncio.sleep(30)
    log.info("Мониторинг остановлен.")

# ──────────────────────────── MAIN ───────────────────────────────
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = set(CHAT_IDS)

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("stop",      cmd_stop))
    app.add_handler(CommandHandler("leverage",  cmd_leverage))

    # Test OKX connectivity once on startup
    bal = await exchange.fetch_balance()
    log.info("OKX USDT balance: %s", bal['total'].get('USDT', 0))

    try:
        await app.run_polling(drop_pending_updates=True,
                              close_loop=False)        # не трогаем loop
    finally:
        await close_exchange()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutdown …")
