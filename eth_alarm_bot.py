#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, math, asyncio, warnings
from datetime import datetime, timezone

import pandas as pd
import ccxt.async_support as ccxt
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

warnings.filterwarnings("ignore", category=FutureWarning,
        message="Series.__getitem__.*positions is deprecated")

# ───── env ─────
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS  = {int(x) for x in os.getenv("CHAT_IDS","").split(",") if x}
PAIR      = os.getenv("PAIR", "BTC/USDT:USDT")
SHEET_ID  = os.getenv("SHEET_ID")
LEVERAGE  = int(os.getenv("LEVERAGE", 1))
CAPITAL   = float(os.getenv("CAPITAL_USDT", 10))

# ───── sheet ───
scope = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]
creds  = ServiceAccountCredentials.from_json_keyfile_dict(
            json.loads(os.getenv("GOOGLE_CREDENTIALS")), scope)
ws     = gspread.authorize(creds).open_by_key(SHEET_ID).worksheet("LP_Logs")
HEAD   = ["DATE-TIME","POSITION","DEPOSIT","ENTRY","STOP LOSS",
          "TAKE PROFIT","RR","P&L (USDT)","APR (%)"]
if ws.row_values(1) != HEAD:
    ws.resize(rows=1); ws.update("A1",[HEAD])

# ───── okx ─────
exchange = ccxt.okx({
    "apiKey":   os.getenv("OKX_API_KEY"),
    "secret":   os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "enableRateLimit": True,
    "options": {"defaultType":"swap"}
})

async def okx_ready():
    await exchange.load_markets(params={"instType":"SWAP"})
    await exchange.set_leverage(LEVERAGE, PAIR)
    bal = await exchange.fetch_balance()
    print("✅ OKX ready. USDT:", bal['total'].get('USDT','—'))

# ───── индикатор SSL + RSI ─────
def ssl_channel(df):
    sma = df.close.rolling(13).mean()
    hlv = (df.close > sma).astype(int)
    up, dn = [], []
    for i in range(len(df)):
        if i<12:
            up.append(dn.append(None))
        win = slice(i-12,i+1)
        if hlv[i]:
            up.append(df.high.iloc[win].max())
            dn.append(df.low .iloc[win].min())
        else:
            up.append(df.low .iloc[win].min())
            dn.append(df.high.iloc[win].max())
    sig=[None]
    for i in range(1,len(df)):
        if pd.notna(up[i]) and pd.notna(dn[i]):
            if up[i-1]<dn[i-1] and up[i]>dn[i]:  sig.append("LONG")
            elif up[i-1]>dn[i-1] and up[i]<dn[i]:sig.append("SHORT")
            else: sig.append(None)
        else: sig.append(None)
    return pd.Series(sig,index=df.index)

async def market_df():
    ohl = await exchange.fetch_ohlcv(PAIR,'15m',limit=100)
    df  = pd.DataFrame(ohl,columns=["ts","o","h","l","c","v"])
    df.ts=pd.to_datetime(df.ts,unit='ms'); df.set_index("ts",inplace=True)
    df['rsi'] = df.c.diff().pipe(lambda x:
               100-100/(1+x.clip(lower=0).rolling(14).mean()/
                         -x.clip(upper=0).rolling(14).mean()))
    df['sig']=ssl_channel(df[['h','l','c']].rename(
                          columns={'h':'high','l':'low','c':'close'}))
    return df

# ───── state ───
monitoring=False; position=None

async def open_trade(side, price):
    qty=max(1,int(CAPITAL*LEVERAGE/price))
    order=await exchange.create_order(PAIR,'market',side,qty)
    fill=float(order['average'] or price)
    sl,tp=(fill*0.995, fill*1.005) if side=="buy" else (fill*1.005, fill*0.995)
    return dict(side="LONG" if side=="buy" else "SHORT",
                entry=fill, sl=sl, tp=tp, qty=qty,
                time=datetime.now(timezone.utc))

async def close_trade(pos, price):
    side="sell" if pos['side']=="LONG" else "buy"
    await exchange.create_order(PAIR,'market',side,pos['qty'])
    pnl=(price-pos['entry'])*pos['qty']*(1 if pos['side']=="LONG" else -1)
    apr=pnl/CAPITAL*100
    ws.append_row([pos['time'].strftime("%Y-%m-%d %H:%M:%S"),pos['side'],CAPITAL,
                   pos['entry'],pos['sl'],pos['tp'],
                   round(abs(pos['tp']-pos['entry'])/abs(pos['sl']-pos['entry']),2),
                   round(pnl,2),round(apr,2)],
                   value_input_option='USER_ENTERED')
    return pnl,apr

async def watcher(app):
    global position
    while monitoring:
        df = await market_df()
        price=df.c.iat[-1]
        sig  = df.sig.dropna()
        last = sig.iat[-1] if not sig.empty else None
        ref  = df.c[df.sig==last].iat[-1] if last else None
        ok_price = last=="LONG" and price>=ref*1.002 or \
                   last=="SHORT"and price<=ref*0.998
        ok_rsi   = last=="LONG" and df.rsi.iat[-1]>55 or \
                   last=="SHORT"and df.rsi.iat[-1]<45
        if last and ok_price and ok_rsi and not position:
            position=await open_trade("buy" if last=="LONG" else "sell",price)
            txt=(f"🟢 OPEN {position['side']} {price:.2f}\n"
                 f"SL {position['sl']:.2f} | TP {position['tp']:.2f}")
            await asyncio.gather(*[app.bot.send_message(cid,txt) for cid in CHAT_IDS])

        if position:
            hit_sl = position['side']=="LONG" and price<=position['sl'] or \
                     position['side']=="SHORT"and price>=position['sl']
            hit_tp = position['side']=="LONG" and price>=position['tp'] or \
                     position['side']=="SHORT"and price<=position['tp']
            if hit_sl or hit_tp:
                pnl,apr=await close_trade(position,price)
                txt=(f"🔴 CLOSE {position['side']} {price:.2f}\n"
                     f"P&L {pnl:.2f} | APR {apr:.2f}%")
                await asyncio.gather(*[app.bot.send_message(cid,txt) for cid in CHAT_IDS])
                position=None
        await asyncio.sleep(30)

# ───── telegram ─────
async def cmd_start(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global monitoring
    CHAT_IDS.add(u.effective_chat.id)
    if not monitoring:
        monitoring=True; asyncio.create_task(watcher(c.application))
        await u.message.reply_text("✅ Monitoring ON")

async def cmd_stop(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global monitoring, position
    monitoring=False; position=None
    await exchange.close()
    await u.message.reply_text("🛑 Monitoring OFF")

async def cmd_leverage(u:Update,c:ContextTypes.DEFAULT_TYPE):
    try:
        lv=int(c.args[0]); assert 1<=lv<=50
        await exchange.set_leverage(lv,PAIR)
        await u.message.reply_text(f"🔧 Leverage x{lv} set")
    except Exception as e: await u.message.reply_text(f"⚠ {e}")

# ───── main ─────
async def main():
    await okx_ready()
    app=ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("leverage",cmd_leverage))
    print("🤖 Bot up — waiting for /start …")
    await app.run_polling(close_loop=False)   # loop остаётся живым

if __name__=="__main__":
    asyncio.run(main())
