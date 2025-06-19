# -*- coding: utf-8 -*-
"""
Telegram-бот для автоматической торговли бессрочными фьючерсами OKX
Стратегия: пересечение SSL-канала (13/13) + подтверждение ценой ±0.2 % + фильтр RSI (55/45)
Параметры по умолчанию:
    • SL / TP = ±0.5 %
    • Плечо = переменная LEVERAGE (по умолчанию 1)
    • Таймфрейм = 15 м.

Требуемые переменные окружения (Railway → Variables):
    BOT_TOKEN          — токен Telegram-бота
    CHAT_IDS           — comma-separated chat_id списка администраторов
    PAIR               — символ OKX, НАПР.:  BTC-USDT  (дефис!)
    OKX_API_KEY        — ключ OKX
    OKX_SECRET         — secret OKX
    OKX_PASSWORD       — passphrase OKX
    SHEET_ID           — ID Google-таблицы
    GOOGLE_CREDENTIALS — JSON сервисного аккаунта (вся строка)
    LEVERAGE (opt)     — начальное плечо (int)
"""

import os
import asyncio
import json
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import ccxt
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# === ENV ===
BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_IDS    = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid.strip().isdigit()}
PAIR        = os.getenv("PAIR")      # формат BTC-USDT
if not PAIR:
    raise ValueError("Переменная PAIR не задана (пример BTC-USDT)")
SHEET_ID    = os.getenv("SHEET_ID")
LEVERAGE    = int(os.getenv("LEVERAGE", 1))

# === GOOGLE SHEETS ===
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict  = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
creds       = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
client      = gspread.authorize(creds)
LOGS_WS     = client.open_by_key(SHEET_ID).worksheet("LP_Logs")
HEADERS = [
    "DATE - TIME", "POSITION", "DEPOSIT", "ENTRY", "STOP LOSS", "TAKE PROFIT",
    "RR", "P&L (USDT)", "APR (%)"
]
if LOGS_WS.row_values(1) != HEADERS:
    LOGS_WS.resize(rows=1)
    LOGS_WS.update("A1", [HEADERS])

# === OKX EXCHANGE ===
exchange = ccxt.okx({
    "apiKey":    os.getenv("OKX_API_KEY"),
    "secret":    os.getenv("OKX_SECRET"),
    "password":  os.getenv("OKX_PASSWORD"),
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})
exchange.set_leverage(LEVERAGE, PAIR)  # isolated по умолчанию

# === STATE ===
monitoring      = False       # глобальный переключатель
open_trade      = None        # dict информации по текущей позиции
current_signal  = None        # последняя сработавшая сторона

# === UTILS ===
def rsi(series: pd.Series, period: int = 14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_ssl(df: pd.DataFrame):
    sma = df['close'].rolling(13).mean()
    hlv = (df['close'] > sma).astype(int)
    ssl_up, ssl_dn = [], []
    for i in range(len(df)):
        if i < 12:
            ssl_up.append(None); ssl_dn.append(None)
        else:
            if hlv.iloc[i]:
                ssl_up.append(df['high'].iloc[i-12:i+1].max())
                ssl_dn.append(df['low'] .iloc[i-12:i+1].min())
            else:
                ssl_up.append(df['low'] .iloc[i-12:i+1].min())
                ssl_dn.append(df['high'].iloc[i-12:i+1].max())
    df['ssl_up'] = ssl_up; df['ssl_dn'] = ssl_dn; df['ssl_sig'] = None
    for i in range(1, len(df)):
        if pd.notna(df['ssl_up'].iloc[i]):
            prev_up, prev_dn = df['ssl_up'].iloc[i-1], df['ssl_dn'].iloc[i-1]
            curr_up, curr_dn = df['ssl_up'].iloc[i]  , df['ssl_dn'].iloc[i]
            if prev_up < prev_dn and curr_up > curr_dn:
                df.iloc[i, df.columns.get_loc('ssl_sig')] = 'LONG'
            elif prev_up > prev_dn and curr_up < curr_dn:
                df.iloc[i, df.columns.get_loc('ssl_sig')] = 'SHORT'
    return df

async def fetch_signal():
    ohlcv = exchange.fetch_ohlcv(PAIR, timeframe='15m', limit=100)
    df = pd.DataFrame(ohlcv, columns=['ts','open','high','low','close','vol'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms'); df.set_index('ts', inplace=True)
    df = calculate_ssl(df)
    df['rsi'] = rsi(df['close'])
    sig_series = df['ssl_sig'].dropna()
    if sig_series.empty:
        return None, df
    signal = sig_series.iloc[-1]
    price  = df['close'].iloc[-1]
    last_rsi = df['rsi'].iloc[-1]
    base_price = df.loc[df['ssl_sig'].notna()].iloc[-1]['close']  # цена в момент сигнала SSL
    # подтверждение ценой
    cond_price = price >= base_price*1.002 if signal=='LONG' else price <= base_price*0.998
    # подтверждение RSI
    cond_rsi   = last_rsi>55 if signal=='LONG' else last_rsi<45
    if cond_price and cond_rsi:
        return signal, df
    return None, df

async def open_position(signal: str, price: float):
    side   = 'buy'  if signal=='LONG' else 'sell'
    amount = round((LEVERAGE*10)/price, 3)   # ≈10 USDT позиции на плече; настрой по желанию
    order  = exchange.create_order(PAIR, 'market', side, amount)
    return order

async def monitor(app):
    global monitoring, current_signal, open_trade
    while monitoring:
        try:
            signal, df = await fetch_signal()
            price = df['close'].iloc[-1]
            if signal and not open_trade:
                # открываем позицию
                sl = round(price*(0.995 if signal=='LONG' else 1.005),2)
                tp = round(price*(1.005 if signal=='LONG' else 0.995),2)
                order = await open_position(signal, price)
                entry_dep = exchange.fetch_balance()['total'].get('USDT',0)
                open_trade = {
                    'side': signal, 'entry': price, 'sl': sl, 'tp': tp,
                    'time': datetime.utcnow(), 'deposit': entry_dep,
                    'order_id': order['id']
                }
                txt = (f"🚀 {signal} OPENED\nEntry: {price}\nSL: {sl} TP: {tp}\nLev: {LEVERAGE}x")
                for cid in app.chat_ids: await app.bot.send_message(cid, txt)
                RR = 1
                LOGS_WS.append_row([
                    open_trade['time'].strftime('%Y-%m-%d %H:%M:%S'), signal, entry_dep,
                    price, sl, tp, RR, '', ''
                ])
            # === проверка закрытия по SL/TP ===
            if open_trade:
                last_price = exchange.fetch_ticker(PAIR)['last']
                hit_tp = last_price>=open_trade['tp'] if open_trade['side']=='LONG' else last_price<=open_trade['tp']
                hit_sl = last_price<=open_trade['sl'] if open_trade['side']=='LONG' else last_price>=open_trade['sl']
                if hit_tp or hit_sl:
                    # закрываем противоположным маркет-ордером
                    side_close = 'sell' if open_trade['side']=='LONG' else 'buy'
                    exchange.create_order(PAIR,'market',side_close, order['amount'])
                    exit_dep = exchange.fetch_balance()['total'].get('USDT',0)
                    pnl = exit_dep - open_trade['deposit']
                    apr = pnl/open_trade['deposit']*100 if open_trade['deposit'] else 0
                    for cid in app.chat_ids:
                        await app.bot.send_message(cid, f"✅ POSITION CLOSED via {'TP' if hit_tp else 'SL'}\nP&L: {pnl:.2f} USDT | APR: {apr:.2f}%")
                    # лог в таблицу
                    LOGS_WS.append_row([
                        datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                        f"CLOSE {open_trade['side']}", exit_dep, last_price, '', '', '', pnl, apr
                    ])
                    open_trade.clear()
                    current_signal = None
        except Exception as e:
            print('[monitor error]', e)
        await asyncio.sleep(30)

# === TELEGRAM COMMANDS ===
async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    global monitoring
    ctx.application.chat_ids.add(update.effective_chat.id)
    if not monitoring:
        monitoring=True
        asyncio.create_task(monitor(ctx.application))
        await update.message.reply_text('✅ Monitoring ON')
    else:
        await update.message.reply_text('Уже запущен.')

async def cmd_stop(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    global monitoring
    monitoring=False
    await update.message.reply_text('🛑 Monitoring OFF')

async def cmd_leverage(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    global LEVERAGE
    try:
        new_lev = int(ctx.args[0])
        exchange.set_leverage(new_lev, PAIR)
        LEVERAGE = new_lev
        await update.message.reply_text(f'Leverage set to {new_lev}x')
    except Exception as e:
        await update.message.reply_text(f'Ошибка: {e}')

# === MAIN ===
if __name__=='__main__':
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = CHAT_IDS
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('stop', cmd_stop))
    app.add_handler(CommandHandler('leverage', cmd_leverage))
    app.run_polling()
