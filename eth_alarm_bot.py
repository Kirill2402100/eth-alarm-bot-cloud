import os, json, asyncio, logging, math
from datetime import datetime, timezone

import ccxt.async_support as ccxt
import gspread, numpy as np, pandas as pd
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ─────────── логгер
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)8s | %(message)s")
log = logging.getLogger("bot")

# ─────────── env
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS  = {int(x) for x in os.getenv("CHAT_IDS", "").split(",") if x}
PAIR      = os.getenv("PAIR", "BTC/USDT:USDT")
LEVERAGE  = int(float(os.getenv("LEVERAGE", 1)))
DEP_USDT  = float(os.getenv("DEPOSIT_USDT", 10))
SHEET_ID  = os.getenv("SHEET_ID")

# ─────────── google sheets
scope = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(
    json.loads(os.getenv("GOOGLE_CREDENTIALS")), scope)
gs = gspread.authorize(creds)
ws = gs.open_by_key(SHEET_ID).worksheet("LP_Logs")

HEADERS = ["DATE - TIME","POSITION","DEPOSIT","ENTRY","STOP LOSS",
           "TAKE PROFIT","RR","P&L (USDT)","APR (%)"]
if ws.row_values(1) != HEADERS:
    ws.resize(rows=1)
    ws.update(range_name='A1', values=[HEADERS])   # ← фикс

# ─────────── okx
exchange = ccxt.okx({
    "apiKey":   os.getenv("OKX_API_KEY"),
    "secret":   os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "enableRateLimit": True,
    "options":  {"defaultType": "swap"}
})

async def close_exchange():
    try:
        await exchange.close()
    except Exception:
        pass

# ─────────── стратегия
def ssl_channel(df: pd.DataFrame) -> pd.Series:
    sma = df['close'].rolling(13).mean()
    hlv = (df['close'] > sma).astype(int)
    ssl_up, ssl_dn, sig = [], [], [None]*len(df)
    for i in range(len(df)):
        if i < 12:
            ssl_up.append(np.nan); ssl_dn.append(np.nan); continue
        hi = df['high'].iloc[i-12:i+1]; lo = df['low'].iloc[i-12:i+1]
        if hlv.iat[i]:
            ssl_up.append(hi.max()); ssl_dn.append(lo.min())
        else:
            ssl_up.append(lo.min()); ssl_dn.append(hi.max())
        if pd.notna(ssl_up[-2]) and pd.notna(ssl_dn[-2]):
            if   ssl_up[-2] < ssl_dn[-2] and ssl_up[-1] > ssl_dn[-1]: sig[i] = "LONG"
            elif ssl_up[-2] > ssl_dn[-2] and ssl_up[-1] < ssl_dn[-1]: sig[i] = "SHORT"
    df['ssl_up'], df['ssl_dn'] = ssl_up, ssl_dn
    return pd.Series(sig, index=df.index)

async def get_signal():
    ohlcv = await exchange.fetch_ohlcv(PAIR, '15m', limit=100)
    df = pd.DataFrame(ohlcv, columns=['ts','open','high','low','close','vol'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms'); df.set_index('ts', inplace=True)
    sig = ssl_channel(df).dropna()
    price = df['close'].iat[-1]
    return (None, price) if len(sig) < 2 or sig.iat[-1]==sig.iat[-2] else (sig.iat[-1], price)

# ─────────── глобальный state
monitoring, current_sig, pos = False, None, None

# ─────────── ордера
async def open_trade(direction:str, price:float):
    global pos
    m = await exchange.load_markets()
    mkt = m[PAIR]; amt = round(DEP_USDT/price, mkt['precision']['amount'])
    await exchange.set_leverage(LEVERAGE, PAIR)
    side = "buy" if direction=="LONG" else "sell"
    await exchange.create_order(PAIR, "market", side, amt)

    sl = round(price*(1-0.005 if direction=="LONG" else 1+0.005), mkt['precision']['price'])
    tp = round(price*(1+0.005 if direction=="LONG" else 1-0.005), mkt['precision']['price'])
    pos = dict(dir=direction, entry=price, qty=amt,
               sl=sl, tp=tp, t0=datetime.now(timezone.utc))
    log.info("OPEN %s @ %.2f", direction, price)

async def close_trade(price:float, why:str):
    global pos
    if not pos: return
    side = "sell" if pos['dir']=="LONG" else "buy"
    await exchange.create_order(PAIR, "market", side, pos['qty'])
    pnl = (price-pos['entry'])*(1 if pos['dir']=="LONG" else -1)*pos['qty']
    apr = (pnl/DEP_USDT)*100*365/((datetime.now(timezone.utc)-pos['t0']).total_seconds()/86400)
    ws.append_row([datetime.now().strftime('%Y-%m-%d %H:%M'),
                   pos['dir'], DEP_USDT, pos['entry'], pos['sl'], pos['tp'],
                   1, round(pnl,2), round(apr,2)], value_input_option="USER_ENTERED")
    pos = None
    log.info("CLOSE (%s) PnL=%.2f", why, pnl)

# ─────────── telegram-handlers
async def cmd_start(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global monitoring
    c.application.chat_ids.add(u.effective_chat.id)
    if monitoring: return await u.message.reply_text("✔️ Уже запущено")
    monitoring=True; await u.message.reply_text("✅ Monitoring ON")
    c.application.create_task(monitor_loop(c.application))

async def cmd_stop(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global monitoring; monitoring=False; await u.message.reply_text("🛑 Monitoring OFF")

async def cmd_lev(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global LEVERAGE
    try:
        LEVERAGE=max(1,min(100,int(c.args[0])))
        await u.message.reply_text(f"Leverage set to {LEVERAGE}x")
    except: await u.message.reply_text("Usage: /leverage 3")

# ─────────── основной цикл
async def monitor_loop(app):
    global current_sig
    while monitoring:
        try:
            sig, price = await get_signal()
            if sig and sig!=current_sig:
                current_sig=sig
                for cid in app.chat_ids:
                    await app.bot.send_message(cid,f"📡 {sig} @ {price:.2f}")
                await open_trade(sig,price)
        except Exception as e:
            log.error("[err] %s", e)
        await asyncio.sleep(30)

# ─────────── запускаем без asyncio.run()
loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)

async def bootstrap():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = set(CHAT_IDS)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop",  cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_lev))
    bal = await exchange.fetch_balance()
    log.info("USDT balance: %s", bal['total'].get('USDT',0))
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        await app.updater.idle()         # block here
    finally:
        await app.stop()
        await app.shutdown()
        await close_exchange()

loop.create_task(bootstrap())
loop.run_forever()
