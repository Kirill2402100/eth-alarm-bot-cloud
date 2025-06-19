# -*- coding: utf-8 -*-
"""ssl_rsi_okx_bot.py — финальная версия
--------------------------------------
Telegram‑бот для автоматической торговли бессрочными фьючерсами OKX
Стратегия: пересечение SSL‑канала 13/13  →  подтверждение ценой ±0.2 %  →  фильтр RSI (55/45)
SL / TP = ±0.5 %;  плечо configurable.

Переменные Railway ➜ Variables (⚠️ обязательны)
------------------------------------------------
BOT_TOKEN , CHAT_IDS , OKX_API_KEY / OKX_SECRET / OKX_PASSWORD ,
SHEET_ID , GOOGLE_CREDENTIALS , PAIR (любой из двух форматов!) , LEVERAGE (opt)
     • Формат 1 (raw):  BTC-USDT       → бот сконвертирует   ➜  BTC/USDT:USDT
     • Формат 2 (ccxt): BTC/USDT:USDT  → используется напрямую
"""

import os, asyncio, json, traceback
from datetime import datetime
import pandas as pd
import ccxt, gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# === ENV ===
BOT_TOKEN = os.getenv("BOT_TOKEN");   assert BOT_TOKEN, "BOT_TOKEN missing"
CHAT_IDS  = {int(cid) for cid in os.getenv("CHAT_IDS", "").split(',') if cid.strip().isdigit()}
RAW_PAIR  = os.getenv("PAIR", "").strip();         assert RAW_PAIR, "PAIR missing"
# ► авто‑конвертируем, если передан формат BTC-USDT
if "-" in RAW_PAIR and "/" not in RAW_PAIR:
    base, quote = RAW_PAIR.split("-")
    PAIR = f"{base}/{quote}:{quote}"
else:
    PAIR = RAW_PAIR        # уже ccxt‑формат BTC/USDT:USDT
LEVERAGE = int(os.getenv("LEVERAGE", 1))
SHEET_ID = os.getenv("SHEET_ID");     assert SHEET_ID, "SHEET_ID missing"

# === GOOGLE SHEETS ===
SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(os.getenv("GOOGLE_CREDENTIALS")), SCOPES)
LOGS_WS = gspread.authorize(creds).open_by_key(SHEET_ID).worksheet("LP_Logs")
HEAD = ["DATE - TIME","POSITION","DEPOSIT","ENTRY","STOP LOSS","TAKE PROFIT","RR","P&L (USDT)","APR (%)"]
if LOGS_WS.row_values(1) != HEAD:
    LOGS_WS.resize(rows=1); LOGS_WS.update("A1", [HEAD])

# === OKX ===
exchange = ccxt.okx({
    "apiKey": os.getenv("OKX_API_KEY"),
    "secret": os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})
# ⚠️ некоторые старые рынки ломают parse_market(); грузим безопасно
try:
    try:
    # загружаем ТОЛЬКО swap-рынки; общий вызов иногда падает на устаревших инструментах
    exchange.load_markets(params={"instType": "SWAP"})
except Exception as e:
    # fallback: fetch_markets только для swap и формируем словарь вручную
    print("[warn] load_markets failed → fallback", e)
    swap_markets = exchange.fetch_markets(params={"instType": "SWAP"})
    exchange.markets = {m['symbol']: m for m in swap_markets}

exchange.set_leverage(LEVERAGE, PAIR)(LEVERAGE, PAIR)

# === Индикаторы ===
WINDOW_SSL = 13

def rsi(series: pd.Series, period: int = 14):
    delta = series.diff(); gain = delta.clip(lower=0); loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean(); avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)

def ssl_signal(df: pd.DataFrame):
    sma = df['close'].rolling(WINDOW_SSL).mean(); hlv = (df['close'] > sma).astype(int)
    up, dn = [], []
    for i in range(len(df)):
        if i < WINDOW_SSL-1:
            up.append(None); dn.append(None)
        else:
            high_sw = df['high'].iloc[i-WINDOW_SSL+1:i+1]
            low_sw  = df['low'] .iloc[i-WINDOW_SSL+1:i+1]
            if hlv.iloc[i]:
                up.append(high_sw.max()); dn.append(low_sw.min())
            else:
                up.append(low_sw.min());  dn.append(high_sw.max())
    df['ssl_up'] = up; df['ssl_dn'] = dn; df['sig'] = None
    for i in range(1, len(df)):
        if pd.notna(df['ssl_up'].iloc[i]):
            prev_up, prev_dn = df['ssl_up'].iloc[i-1], df['ssl_dn'].iloc[i-1]
            curr_up, curr_dn = df['ssl_up'].iloc[i], df['ssl_dn'].iloc[i]
            if prev_up < prev_dn and curr_up > curr_dn: df.at[df.index[i],'sig']='LONG'
            if prev_up > prev_dn and curr_up < curr_dn: df.at[df.index[i],'sig']='SHORT'
    return df

# === Глобальное состояние ===
monitoring = False; trade = None

async def get_signal():
    ohl = exchange.fetch_ohlcv(PAIR, '15m', limit=100)
    df  = pd.DataFrame(ohl, columns=['ts','open','high','low','close','vol'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms'); df.set_index('ts', inplace=True)
    df = ssl_signal(df); df['rsi'] = rsi(df['close'])
    sigs = df['sig'].dropna()
    if sigs.empty:
        return None, df
    sig  = sigs.iloc[-1]; price = df['close'].iloc[-1]
    base_price = df.loc[df['sig'].notna()].iloc[-1]['close']
    cond_price = price >= base_price*1.002 if sig=='LONG' else price <= base_price*0.998
    cond_rsi   = df['rsi'].iloc[-1] > 55 if sig=='LONG' else df['rsi'].iloc[-1] < 45
    return (sig if cond_price and cond_rsi else None), df

async def open_trade(signal:str, price:float):
    side = 'buy' if signal=='LONG' else 'sell'; amount = round((10*LEVERAGE)/price, 3)
    return exchange.create_order(PAIR,'market',side,amount)

async def monitor(app):
    global trade, monitoring
    while monitoring:
        try:
            sig, df = await get_signal(); price = df['close'].iloc[-1]
            if sig and not trade:
                order = await open_trade(sig, price)
                sl = round(price*(0.995 if sig=='LONG' else 1.005),2)
                tp = round(price*(1.005 if sig=='LONG' else 0.995),2)
                dep0 = exchange.fetch_balance()['total'].get('USDT',0)
                trade = {'side':sig,'entry':price,'amount':order['amount'],'sl':sl,'tp':tp,'dep':dep0,'time':datetime.utcnow()}
                txt=f"🚀 OPEN {sig}\nEntry {price}\nSL {sl} TP {tp} Lev {LEVERAGE}x"; [await app.bot.send_message(cid,txt) for cid in app.chat_ids]
                LOGS_WS.append_row([trade['time'].strftime('%Y-%m-%d %H:%M:%S'),sig,dep0,price,sl,tp,1,'',''])
            if trade:
                last = exchange.fetch_ticker(PAIR)['last']
                hit_tp = last>=trade['tp'] if trade['side']=='LONG' else last<=trade['tp']
                hit_sl = last<=trade['sl'] if trade['side']=='LONG' else last>=trade['sl']
                if hit_tp or hit_sl:
                    close_side = 'sell' if trade['side']=='LONG' else 'buy'
                    exchange.create_order(PAIR,'market',close_side,trade['amount'])
                    dep1 = exchange.fetch_balance()['total'].get('USDT',0)
                    pnl = dep1-trade['dep']; apr = pnl/trade['dep']*100 if trade['dep'] else 0
                    txt=f"✅ CLOSE via {'TP'if hit_tp else'SL'}\nP&L {pnl:.2f} USDT | APR {apr:.2f}%"; [await app.bot.send_message(cid,txt) for cid in app.chat_ids]
                    LOGS_WS.append_row([datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),f"CLOSE {trade['side']}",dep1,last,'','','',pnl,apr])
                    trade=None
        except Exception as e:
            print('[monitor]',e); traceback.print_exc()
        await asyncio.sleep(30)

# === Telegram ===
async def cmd_start(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global monitoring
    c.application.chat_ids.add(u.effective_chat.id)
    if not monitoring:
        monitoring=True; asyncio.create_task(monitor(c.application)); await u.message.reply_text('Monitoring ON ✅')
    else:
        await u.message.reply_text('Уже запущен.')
async def cmd_stop(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global monitoring; monitoring=False; await u.message.reply_text('⏹️ Monitoring OFF')
async def cmd_leverage(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global LEVERAGE
    try:
        lev=int(c.args[0]); exchange.set_leverage(lev,PAIR); LEVERAGE=lev
        await u.message.reply_text(f'Leverage set to {lev}x')
    except Exception as e:
        await u.message.reply
