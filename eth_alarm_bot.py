#!/usr/bin/env python3
# coding: utf-8
"""
Trading-bot: 15m BTCUSDT swap on OKX
Strategy: SSL-13/13 + RSI + price confirm ±0.2 %, SL/TP ±0.5 %
author: Kirill & ChatGPT
"""

import os, asyncio, json, math, time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import ccxt.async_support as ccxt          # асинхронная версия
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)

# ──────────────────── env ─────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS   = {int(cid) for cid in os.getenv("CHAT_IDS", "").split(",") if cid}
PAIR       = os.getenv("PAIR", "BTC/USDT:USDT")   # формат OKX
SHEET_ID   = os.getenv("SHEET_ID")
LEVERAGE   = int(os.getenv("LEVERAGE", 1))
CAPITAL    = float(os.getenv("CAPITAL_USDT", 10)) # тест-кэш, USDT

if not (BOT_TOKEN and CHAT_IDS and PAIR and SHEET_ID):
    raise ValueError("⛔  Не заданы обязательные переменные окружения.")

# ──────────────────── google sheets ──────────────────────────────
scope = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(
    json.loads(os.getenv("GOOGLE_CREDENTIALS")), scope)
gs = gspread.authorize(creds)
ws = gs.open_by_key(SHEET_ID).worksheet("LP_Logs")

HEADERS = ["DATE-TIME","POSITION","DEPOSIT","ENTRY","STOP LOSS",
           "TAKE PROFIT","RR","P&L (USDT)","APR (%)"]
if ws.row_values(1) != HEADERS:
    ws.resize(rows=1);  ws.update('A1',[HEADERS])

# ──────────────────── OKX ────────────────────────────────────────
exchange = ccxt.okx({
    "apiKey":   os.getenv("OKX_API_KEY"),
    "secret":   os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})

# ─────────── check connection, leverage ──────────────────────────
async def okx_ready():
    await exchange.load_markets(params={"instType":"SWAP"})
    if PAIR not in exchange.markets:
        raise ValueError(f"⛔ Market {PAIR} not found on OKX.")
    await exchange.set_leverage(LEVERAGE, PAIR)
    bal = await exchange.fetch_balance()
    print("✅ OKX connected. USDT =", bal['total'].get('USDT',"—"))

# ─────────── indicators ──────────────────────────────────────────
def calc_ssl(df:pd.DataFrame)->pd.Series:
    sma = df['close'].rolling(13).mean()
    hlv = (df['close']>sma).astype(int)
    ssl_up, ssl_dn = [], []
    for i in range(len(df)):
        if i<12: ssl_up.append(None); ssl_dn.append(None); continue
        window = slice(i-12,i+1)
        if hlv.iloc[i]:
            ssl_up.append(df['high'].iloc[window].max())
            ssl_dn.append(df['low' ].iloc[window].min())
        else:
            ssl_up.append(df['low' ].iloc[window].min())
            ssl_dn.append(df['high'].iloc[window].max())
    df['ssl_up'], df['ssl_dn'] = ssl_up, ssl_dn
    chan = []
    for i in range(1,len(df)):
        prev, curr = df.iloc[i-1], df.iloc[i]
        if pd.notna(curr['ssl_up']):
            if prev.ssl_up<prev.ssl_dn and curr.ssl_up>curr.ssl_dn: chan.append("LONG")
            elif prev.ssl_up>prev.ssl_dn and curr.ssl_up<curr.ssl_dn: chan.append("SHORT")
            else: chan.append(None)
        else: chan.append(None)
    df['signal'] = [None]+chan
    return df

# ─────────── bot state ───────────────────────────────────────────
current_pos   = None   # {"side":LONG/SHORT, "entry":price, "sl":.., "tp":..}
monitoring    = False
last_signal_t = None

# ─────────── telegram commands ───────────────────────────────────
async def cmd_start(upd:Update, ctx:ContextTypes.DEFAULT_TYPE):
    global monitoring
    CHAT_IDS.add(upd.effective_chat.id)
    monitoring = True
    await upd.message.reply_text("✅ Monitoring ON")
    asyncio.create_task(monitor(ctx.application))

async def cmd_stop(upd:Update, ctx:ContextTypes.DEFAULT_TYPE):
    global monitoring
    monitoring = False
    await upd.message.reply_text("🛑 Monitoring OFF")

async def cmd_leverage(upd:Update, ctx:ContextTypes.DEFAULT_TYPE):
    global LEVERAGE
    try:
        lv = int(ctx.args[0]);  assert 1<=lv<=50
        await exchange.set_leverage(lv, PAIR)
        LEVERAGE = lv
        await upd.message.reply_text(f"🔧 Leverage set to x{lv}")
    except Exception as e:
        await upd.message.reply_text(f"⚠ {e}")

# ─────────── core ────────────────────────────────────────────────
async def fetch_signal():
    ohlcv = await exchange.fetch_ohlcv(PAIR,'15m',limit=100)
    df = pd.DataFrame(ohlcv,columns=["ts","open","high","low","close","vol"])
    df['ts']=pd.to_datetime(df.ts,unit='ms'); df.set_index('ts',inplace=True)
    rsi = pd.Series(df.close).diff().pipe(lambda x:
        100-100/(1+ x.clip(lower=0).rolling(14).mean() /
                     -x.clip(upper=0).rolling(14).mean()))
    df['rsi']=rsi
    df = calc_ssl(df)
    sig = df.signal.dropna()
    if len(sig)<1: return None, df.close.iloc[-1]
    last = sig.iloc[-1]
    price= df.close.iloc[-1]
    # подтверждение ценой ±0.2 %
    ref   = df.close[df.signal==last].iloc[-1]
    if last=="LONG"  and price < ref*1.002: return None, price
    if last=="SHORT" and price > ref*0.998: return None, price
    # подтверждение RSI
    if last=="LONG"  and df.rsi.iloc[-1] < 55: return None, price
    if last=="SHORT" and df.rsi.iloc[-1] > 45: return None, price
    return last, price

async def open_trade(side:str, price:float):
    usdt = CAPITAL * LEVERAGE
    contracts = max(1, int(usdt/price))
    order = await exchange.create_order(PAIR,'market',side,contracts)
    entry = float(order['average'] or price)
    sl = entry*(0.995 if side=="long" else 1.005)
    tp = entry*(1.005 if side=="long" else 0.995)
    return {"side":side.upper(),"entry":entry,"sl":sl,"tp":tp,
            "contracts":contracts,"time":datetime.now(timezone.utc)}

async def close_trade(pos, price):
    side = "sell" if pos['side']=="LONG" else "buy"
    await exchange.create_order(PAIR,'market',side,pos['contracts'])
    pnl  = (price-pos['entry'])*pos['contracts']*(1 if pos['side']=="LONG" else -1)
    apr  = pnl/CAPITAL*100
    row = [pos['time'].strftime("%Y-%m-%d %H:%M:%S"), pos['side'], CAPITAL,
           pos['entry'], pos['sl'], pos['tp'],
           round(abs(pos['tp']-pos['entry'])/abs(pos['sl']-pos['entry']),2),
           round(pnl,2), round(apr,2)]
    await asyncio.to_thread(ws.append_row,row,value_input_option='USER_ENTERED')
    return pnl, apr

async def monitor(app):
    global current_pos
    while monitoring:
        try:
            sig, price = await fetch_signal()
            # открытие
            if sig and current_pos is None:
                current_pos = await open_trade(
                    "long" if sig=="LONG" else "sell", price)
                txt=(f"🟢 OPEN {current_pos['side']}  {price:.2f}\n"
                     f"SL {current_pos['sl']:.2f} | TP {current_pos['tp']:.2f}")
                for cid in CHAT_IDS: await app.bot.send_message(cid,txt)
            # сопровождение позиции
            if current_pos:
                if (current_pos['side']=="LONG"  and price<=current_pos['sl']) or\
                   (current_pos['side']=="SHORT" and price>=current_pos['sl']) or\
                   (current_pos['side']=="LONG"  and price>=current_pos['tp']) or\
                   (current_pos['side']=="SHORT" and price<=current_pos['tp']):
                    pnl,apr = await close_trade(current_pos,price)
                    txt=(f"🔴 CLOSE {current_pos['side']}  {price:.2f}\n"
                         f"P&L {pnl:.2f}  | APR {apr:.2f}%")
                    for cid in CHAT_IDS: await app.bot.send_message(cid,txt)
                    current_pos=None
        except Exception as e:
            print("[error]",e)
        await asyncio.sleep(30)

# ─────────── main ────────────────────────────────────────────────
async def main():
    await okx_ready()
    app = (ApplicationBuilder().token(BOT_TOKEN)
           .drop_pending_updates(True).build())
    app.chat_ids = CHAT_IDS
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_leverage))
    print("🤖 Bot up — waiting for /start …")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
