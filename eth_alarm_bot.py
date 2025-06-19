#!/usr/bin/env python3
# coding: utf-8
"""
15-минутный BTC/USDT swap-бот на OKX
SSL-13/13 + RSI + confirm ±0.2 %, SL/TP ±0.5 %
"""

import os, json, math, asyncio
from datetime import datetime, timezone

import pandas as pd
import ccxt.async_support as ccxt
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ───────────── env ───────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS  = {int(cid) for cid in os.getenv("CHAT_IDS","").split(",") if cid}
PAIR      = os.getenv("PAIR", "BTC/USDT:USDT")
SHEET_ID  = os.getenv("SHEET_ID")
LEVERAGE  = int(os.getenv("LEVERAGE", 1))
CAPITAL   = float(os.getenv("CAPITAL_USDT", 10))

# sanity-check
if not (BOT_TOKEN and CHAT_IDS and PAIR and SHEET_ID):
    raise RuntimeError("⛔  BOT_TOKEN, CHAT_IDS, PAIR, SHEET_ID must be set!")

# ───────────── google sheet ──────────────────────────────────────
scope = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(
    json.loads(os.getenv("GOOGLE_CREDENTIALS")), scope)
ws = gspread.authorize(creds).open_by_key(SHEET_ID).worksheet("LP_Logs")
HEAD = ["DATE-TIME","POSITION","DEPOSIT","ENTRY","STOP LOSS",
        "TAKE PROFIT","RR","P&L (USDT)","APR (%)"]
if ws.row_values(1) != HEAD:
    ws.resize(rows=1);  ws.update('A1', [HEAD])

# ───────────── OKX ───────────────────────────────────────────────
exchange = ccxt.okx({
    "apiKey":   os.getenv("OKX_API_KEY"),
    "secret":   os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "enableRateLimit": True,
    "options": {"defaultType":"swap"}
})

async def okx_ready():
    await exchange.load_markets(params={"instType":"SWAP"})
    if PAIR not in exchange.markets:
        raise ValueError(f"{PAIR} not on OKX")
    await exchange.set_leverage(LEVERAGE, PAIR)
    bal = await exchange.fetch_balance()
    print("✅  OKX connected. USDT =", bal['total'].get('USDT','–'))

# ───────────── индикаторы ───────────────────────────────────────
def ssl_channel(df:pd.DataFrame)->pd.Series:
    sma = df.close.rolling(13).mean()
    hlv = (df.close > sma).astype(int)
    ssl_u, ssl_d = [], []
    for i in range(len(df)):
        if i<12: ssl_u.append(None); ssl_d.append(None); continue
        win = slice(i-12, i+1)
        if hlv[i]:
            ssl_u.append(df.high.iloc[win].max())
            ssl_d.append(df.low.iloc [win].min())
        else:
            ssl_u.append(df.low.iloc [win].min())
            ssl_d.append(df.high.iloc[win].max())
    df['u'], df['d'] = ssl_u, ssl_d
    sig = [None]
    for i in range(1,len(df)):
        p,c = df.iloc[i-1], df.iloc[i]
        if pd.notna(c.u):
            if p.u<p.d and c.u>c.d: sig.append("LONG")
            elif p.u>p.d and c.u<c.d: sig.append("SHORT")
            else: sig.append(None)
        else: sig.append(None)
    return pd.Series(sig,index=df.index,name="signal")

# ───────────── global state ─────────────────────────────────────
monitoring   = False
position     = None   # dict when trade open

# ───────────── telegram commands ────────────────────────────────
async def cmd_start(upd:Update, ctx:ContextTypes.DEFAULT_TYPE):
    global monitoring
    CHAT_IDS.add(upd.effective_chat.id)
    monitoring = True
    await upd.message.reply_text("✅  Monitoring ON")
    asyncio.create_task(watch(ctx.application))

async def cmd_stop(upd:Update, ctx:ContextTypes.DEFAULT_TYPE):
    global monitoring
    monitoring = False
    await upd.message.reply_text("🛑  Monitoring OFF")

async def cmd_leverage(upd:Update, ctx:ContextTypes.DEFAULT_TYPE):
    global LEVERAGE
    try:
        lv = int(ctx.args[0]);  assert 1<=lv<=50
        await exchange.set_leverage(lv,PAIR)
        LEVERAGE = lv
        await upd.message.reply_text(f"🔧  Leverage set x{lv}")
    except Exception as e:
        await upd.message.reply_text(f"⚠  {e}")

# ───────────── core helpers ─────────────────────────────────────
async def market_df():
    ohlcv = await exchange.fetch_ohlcv(PAIR,'15m',limit=100)
    df = pd.DataFrame(ohlcv,columns=["ts","open","high","low","close","vol"])
    df['ts']=pd.to_datetime(df.ts,unit='ms'); df.set_index('ts',inplace=True)
    rsi = pd.Series(df.close).diff().pipe(
        lambda x:100-100/(1+x.clip(lower=0).rolling(14).mean()/
                             -x.clip(upper=0).rolling(14).mean()))
    df['rsi']=rsi
    df['signal']=ssl_channel(df)
    return df

async def new_signal(df):
    sig = df.signal.dropna()
    if sig.empty: return None, df.close.iat[-1]
    last = sig.iat[-1]; price = df.close.iat[-1]
    ref  = df.close[df.signal==last].iat[-1]
    if (last=="LONG" and (price<ref*1.002 or df.rsi.iat[-1]<55)) or \
       (last=="SHORT" and (price>ref*0.998 or df.rsi.iat[-1]>45)):
        return None, price
    return last, price

async def open_trade(side, price):
    size = max(1, int((CAPITAL*LEVERAGE)/price))
    order = await exchange.create_order(PAIR,'market',side,size)
    fill  = float(order['average'] or price)
    sl,tp = (fill*0.995, fill*1.005) if side=="buy" else (fill*1.005, fill*0.995)
    return {"side": "LONG" if side=="buy" else "SHORT",
            "entry":fill,"sl":sl,"tp":tp,"contracts":size,
            "time":datetime.now(timezone.utc)}

async def close_trade(pos, price):
    side = "sell" if pos['side']=="LONG" else "buy"
    await exchange.create_order(PAIR,'market',side,pos['contracts'])
    pnl  = (price-pos['entry'])*pos['contracts']*(1 if pos['side']=="LONG" else -1)
    apr  = pnl/CAPITAL*100
    row  = [pos['time'].strftime("%Y-%m-%d %H:%M:%S"),pos['side'],CAPITAL,
            pos['entry'],pos['sl'],pos['tp'],
            round(abs(pos['tp']-pos['entry'])/abs(pos['sl']-pos['entry']),2),
            round(pnl,2),round(apr,2)]
    await asyncio.to_thread(ws.append_row,row,value_input_option='USER_ENTERED')
    return pnl,apr

# ───────────── monitor loop ─────────────────────────────────────
async def watch(app):
    global position
    try:
        while monitoring:
            df      = await market_df()
            signal, price = await new_signal(df)
            if signal and not position:
                position = await open_trade("buy" if signal=="LONG" else "sell", price)
                txt=(f"🟢 OPEN {position['side']} {price:.2f}\n"
                     f"SL {position['sl']:.2f} | TP {position['tp']:.2f}")
                for cid in CHAT_IDS: await app.bot.send_message(cid,txt)
            if position:
                hit_sl = position['side']=="LONG" and price<=position['sl'] or \
                         position['side']=="SHORT" and price>=position['sl']
                hit_tp = position['side']=="LONG" and price>=position['tp'] or \
                         position['side']=="SHORT"and price<=position['tp']
                if hit_sl or hit_tp:
                    pnl,apr = await close_trade(position,price)
                    txt=(f"🔴 CLOSE {position['side']} {price:.2f}\n"
                         f"P&L {pnl:.2f} | APR {apr:.2f}%")
                    for cid in CHAT_IDS: await app.bot.send_message(cid,txt)
                    position=None
            await asyncio.sleep(30)
    finally:
        await exchange.close()

# ───────────── main ─────────────────────────────────────────────
async def main():
    await okx_ready()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop",  cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_leverage))
    print("🤖  Bot up — waiting for /start …")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
