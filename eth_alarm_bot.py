import os, asyncio, json
from datetime import datetime, timezone
import pandas as pd
import ccxt, gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)

# ──────────────────── ENV ────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS  = {int(cid) for cid in os.getenv("CHAT_IDS", "").split(",") if cid}
PAIR      = os.getenv("PAIR", "BTC/USDT")
SHEET_ID  = os.getenv("SHEET_ID")
LEVERAGE  = int(os.getenv("LEVERAGE", 1))

# ────────── Google Sheets ──────────
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
creds = ServiceAccountCredentials.from_json_keyfile_dict(
    json.loads(os.getenv("GOOGLE_CREDENTIALS")), scope
)
gs = gspread.authorize(creds)
LOGS_WS = gs.open_by_key(SHEET_ID).worksheet("LP_Logs")
HEADERS = [
    "DATE - TIME","POSITION","DEPOSIT USDT","ENTRY","STOP LOSS",
    "TAKE PROFIT","RR","P&L (USDT)","APR (%)"
]
if LOGS_WS.row_values(1) != HEADERS:
    LOGS_WS.resize(rows=1); LOGS_WS.update("A1",[HEADERS])

# ────────── OKX ──────────
exchange = ccxt.okx({
    "apiKey" : os.getenv("OKX_API_KEY"),
    "secret" : os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "enableRateLimit": True,
    "options": { "defaultType": "swap" }
})
async def set_leverage():
    try: exchange.set_leverage(LEVERAGE, PAIR, {"marginMode":"isolated"})
    except Exception as e: print("[warn] leverage", e)

# ────────── utils ──────────
def ta_rsi(series: pd.Series, n:int=14) -> pd.Series:
    d = series.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0)
    rs = up.ewm(alpha=1/n, adjust=False).mean() / dn.ewm(alpha=1/n,adjust=False).mean()
    return 100-100/(1+rs)

async def safe_close(ex):
    try: await ex.close()
    except RuntimeError as e:
        if "event loop is already running" in str(e): pass
        else: raise

# ────────── стратегия (SSL + RSI + price confirm) ──────────
def ssl(df:pd.DataFrame)->pd.Series:
    sma = df.close.rolling(13).mean()
    hlv = (df.close>sma).astype(int)
    up, dn, sig = [], [], [None]*len(df)
    for i in range(len(df)):
        if i<12: up.append(None); dn.append(None); continue
        if hlv[i]==1:
            up.append(df.high[i-12:i+1].max())
            dn.append(df.low [i-12:i+1].min())
        else:
            up.append(df.low [i-12:i+1].min())
            dn.append(df.high[i-12:i+1].max())
        if i>0 and up[i-1] and dn[i-1]:
            if up[i-1]<dn[i-1] and up[i]>dn[i]:  sig[i]="LONG"
            if up[i-1]>dn[i-1] and up[i]<dn[i]:  sig[i]="SHORT"
    return pd.Series(sig,index=df.index)

async def fetch_signal():
    try:
        ohlcv = exchange.fetch_ohlcv(PAIR,"15m",limit=200)
    except Exception as e:
        print("[err] fetch_ohlcv",e); return None,0
    df = pd.DataFrame(ohlcv,columns=["ts","o","h","l","c","v"])
    df.ts = pd.to_datetime(df.ts,unit="ms"); df.set_index("ts",inplace=True)
    df.rename(columns={"o":"open","h":"high","l":"low","c":"close"},inplace=True)
    df["sig"]=ssl(df); df["rsi"]=ta_rsi(df.close)
    if df.sig.dropna().empty: return None, df.close.iat[-1]
    sig   = df.sig.dropna().iat[-1]
    price = df.close.iat[-1]; rsi=df.rsi.iat[-1]
    base  = df[df.sig.notna()].close.iat[-1]

    if sig=="LONG" and (rsi<55 or price<base*1.002): return None,price
    if sig=="SHORT" and (rsi>45 or price>base*0.998):return None,price
    return sig,price

# ────────── Telegram handlers ──────────
monitoring=False
async def open_trade(sig:str, price:float):
    side="buy" if sig=="LONG" else "sell"
    amt = 0.01
    return exchange.create_order(PAIR,"market",side,amt)

async def monitor(app):
    global monitoring
    while monitoring:
        sig,price = await fetch_signal()
        if sig:
            for cid in CHAT_IDS:
                await app.bot.send_message(cid,f"🎯 {sig} @ {price:.2f}")
            try:
                o=await open_trade(sig,price)
                for cid in CHAT_IDS:
                    await app.bot.send_message(cid,f"✅ Открыт ордер {o['id']}")
            except Exception as e:
                for cid in CHAT_IDS:
                    await app.bot.send_message(cid,f"⛔️ Ошибка ордера: {e}")
        await asyncio.sleep(30)

async def cmd_start(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global monitoring
    CHAT_IDS.add(u.effective_chat.id); monitoring=True
    await u.message.reply_text("▶️ Monitoring ON")
    asyncio.create_task(monitor(c.application))

async def cmd_stop(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global monitoring; monitoring=False
    await u.message.reply_text("⏹ Monitoring OFF")
    await safe_close(exchange)

async def cmd_leverage(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global LEVERAGE
    try:
        LEVERAGE=max(1,min(int(c.args[0]),50)); await set_leverage()
        await u.message.reply_text(f"⚙️ leverage {LEVERAGE}x")
    except Exception as e:
        await u.message.reply_text(f"err: {e}")

# ────────── main ──────────
async def main():
    await set_leverage()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("leverage",cmd_leverage))

    try:
        await app.run_polling(drop_pending_updates=True)
    finally:
        await safe_close(exchange)

if __name__=="__main__":
    asyncio.run(main())
