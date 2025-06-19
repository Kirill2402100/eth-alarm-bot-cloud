# -*- coding: utf-8 -*-
"""
Telegram-бот для автоматической торговли бессрочными фьючерсами OKX
Стратегия: пересечение SSL-канала (13/13) → подтверждение ценой ±0.2 % → фильтр RSI (>55 /<45)

| Событие | Действие |
|---------|----------|
| Сигнал выполнен | открываем позицию маркет-ордером |
| Цена ±0.5 % | авто-SL / TP, закрытие |
| Закрытие | считаем P&L, APR и пишем в Google Sheets |

Обязательные переменные окружения (Railway → Variables)
-------------------------------------------------------
BOT_TOKEN, CHAT_IDS, PAIR (BTC-USDT), OKX_API_KEY / OKX_SECRET / OKX_PASSWORD,
SHEET_ID, GOOGLE_CREDENTIALS, LEVERAGE (по умолчанию 1)
"""

import os, asyncio, json
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import ccxt, gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# === ENV ===
BOT_TOKEN  = os.getenv("BOT_TOKEN")
CHAT_IDS   = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid.strip().isdigit()}
PAIR       = os.getenv("PAIR", "").strip()        # ожидаем BTC-USDT
if not PAIR:
    raise ValueError("PAIR env var is empty (пример BTC-USDT)")
LEVERAGE   = int(os.getenv("LEVERAGE", 1))
SHEET_ID   = os.getenv("SHEET_ID")

# === GOOGLE SHEETS ===
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
creds      = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
client     = gspread.authorize(creds)
LOGS_WS    = client.open_by_key(SHEET_ID).worksheet("LP_Logs")
HEADERS = [
    "DATE - TIME", "POSITION", "DEPOSIT", "ENTRY", "STOP LOSS", "TAKE PROFIT",
    "RR", "P&L (USDT)", "APR (%)"
]
if LOGS_WS.row_values(1) != HEADERS:
    LOGS_WS.resize(rows=1); LOGS_WS.update("A1", [HEADERS])

# === OKX ===
exchange = ccxt.okx({
    "apiKey": os.getenv("OKX_API_KEY"),
    "secret": os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})
exchange.load_markets()              # ← сначала маркеты!
exchange.set_leverage(LEVERAGE, PAIR) # isolated по умолчанию

# === Вспомогательные функции ===

def calc_rsi(series: pd.Series, period: int = 14):
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)

def calc_ssl(df: pd.DataFrame):
    sma = df['close'].rolling(13).mean()
    hlv = (df['close'] > sma).astype(int)
    up, dn = [], []
    for i in range(len(df)):
        if i < 12:
            up.append(None); dn.append(None)
        else:
            if hlv.iloc[i]:
                up.append(df['high'].iloc[i-12:i+1].max())
                dn.append(df['low'] .iloc[i-12:i+1].min())
            else:
                up.append(df['low'] .iloc[i-12:i+1].min())
                dn.append(df['high'].iloc[i-12:i+1].max())
    df['ssl_up'] = up; df['ssl_dn'] = dn; df['signal'] = None
    for i in range(1, len(df)):
        if pd.notna(df['ssl_up'].iloc[i]):
            prev_up, prev_dn = df['ssl_up'].iloc[i-1], df['ssl_dn'].iloc[i-1]
            curr_up, curr_dn = df['ssl_up'].iloc[i]  , df['ssl_dn'].iloc[i]
            if prev_up < prev_dn and curr_up > curr_dn:
                df.at[df.index[i], 'signal'] = 'LONG'
            elif prev_up > prev_dn and curr_up < curr_dn:
                df.at[df.index[i], 'signal'] = 'SHORT'
    return df

# === Глобальное состояние ===
monitoring = False
open_trade = None    # dict с текущей позицией

async def fetch_signal():
    ohl = exchange.fetch_ohlcv(PAIR, '15m', limit=100)
    df = pd.DataFrame(ohl, columns=['ts','open','high','low','close','vol'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms'); df.set_index('ts', inplace=True)
    df = calc_ssl(df)
    df['rsi'] = calc_rsi(df['close'])
    sig = df['signal'].dropna()
    if sig.empty:
        return None, df
    signal = sig.iloc[-1]; price = df['close'].iloc[-1]
    base_price = df.loc[df['signal'].notna()].iloc[-1]['close']
    cond_price = price >= base_price*1.002 if signal=='LONG' else price <= base_price*0.998
    cond_rsi   = df['rsi'].iloc[-1] > 55 if signal=='LONG' else df['rsi'].iloc[-1] < 45
    return (signal if cond_price and cond_rsi else None), df

async def open_position(signal: str, price: float):
    side   = 'buy' if signal=='LONG' else 'sell'
    # объём ≈ 10 USDT * плечо
    amount = round((10*LEVERAGE)/price, 3)
    return exchange.create_order(PAIR, 'market', side, amount)

async def monitor(app):
    global monitoring, open_trade
    while monitoring:
        try:
            sig, df = await fetch_signal()
            price = df['close'].iloc[-1]
            if sig and not open_trade:
                order = await open_position(sig, price)
                sl = round(price*(0.995 if sig=='LONG' else 1.005),2)
                tp = round(price*(1.005 if sig=='LONG' else 0.995),2)
                bal = exchange.fetch_balance()['total'].get('USDT',0)
                open_trade = {
                    'side': sig, 'entry': price, 'sl': sl, 'tp': tp,
                    'amount': order['amount'], 'deposit': bal,
                    'time': datetime.utcnow()
                }
                txt = (f"🚀 {sig} OPENED\nEntry: {price}\nSL {sl} | TP {tp} | Lev {LEVERAGE}x")
                for cid in app.chat_ids: await app.bot.send_message(cid, txt)
                RR = 1; LOGS_WS.append_row([open_trade['time'].strftime('%Y-%m-%d %H:%M:%S'), sig, bal, price, sl, tp, RR, '', ''])
            # === SL / TP ===
            if open_trade:
                last = exchange.fetch_ticker(PAIR)['last']
                hit_tp = last>=open_trade['tp'] if open_trade['side']=='LONG' else last<=open_trade['tp']
                hit_sl = last<=open_trade['sl'] if open_trade['side']=='LONG' else last>=open_trade['sl']
                if hit_tp or hit_sl:
                    close_side = 'sell' if open_trade['side']=='LONG' else 'buy'
                    exchange.create_order(PAIR,'market',close_side,open_trade['amount'])
                    bal2 = exchange.fetch_balance()['total'].get('USDT',0)
                    pnl = bal2 - open_trade['deposit']; apr = pnl/open_trade['deposit']*100 if open_trade['deposit'] else 0
                    for cid in app.chat_ids:
                        await app.bot.send_message(cid, f"✅ POSITION CLOSED via {'TP' if hit_tp else 'SL'}\nP&L {pnl:.2f} USDT | APR {apr:.2f}%")
                    LOGS_WS.append_row([datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), f"CLOSE {open_trade['side']}", bal2, last,'','','', pnl, apr])
                    open_trade = None
        except Exception as e:
            print('[monitor]', e)
        await asyncio.sleep(30)

# === Telegram Commands ===
async def start_cmd(u:Update, c:ContextTypes.DEFAULT_TYPE):
    global monitoring
    c.application.chat_ids.add(u.effective_chat.id)
    if not monitoring:
        monitoring=True; asyncio.create_task(monitor(c.application))
        await u.message.reply_text('Monitoring ON ✅')
    else:
        await u.message.reply_text('Уже запущено.')
async def stop_cmd(u:Update, c:ContextTypes.DEFAULT_TYPE):
    global monitoring
    monitoring=False; await u.message.reply
