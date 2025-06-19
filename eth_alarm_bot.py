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
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = [int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",")]
PAIR = os.getenv("PAIR", "BTC/USDT")
SHEET_ID = os.getenv("SHEET_ID")
LEVERAGE = int(os.getenv("LEVERAGE", 1))

# === GOOGLE SHEETS ===
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gs = gspread.authorize(creds)
LOGS_WS = gs.open_by_key(SHEET_ID).worksheet("LP_Logs")

HEADERS = [
    "DATE - TIME", "POSITION", "DEPOSIT", "ENTRY", "STOP LOSS", "TAKE PROFIT",
    "RR", "P&L (USDT)", "APR (%)"
]
if LOGS_WS.row_values(1) != HEADERS:
    LOGS_WS.resize(rows=1)
    LOGS_WS.update('A1', [HEADERS])

# === STATE ===
current_signal = None
last_cross = None
position = None
log = []
monitoring = False
leverage = LEVERAGE

# === EXCHANGE: OKX ===
exchange = ccxt.okx({
    "apiKey": os.getenv("OKX_API_KEY"),
    "secret": os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "enableRateLimit": True,
    "options": {
        "defaultType": "future"
    }
})

# === STRATEGY ===
def calculate_ssl(df):
    sma = df['close'].rolling(13).mean()
    hlv = (df['close'] > sma).astype(int)
    ssl_up, ssl_down = [], []
    for i in range(len(df)):
        if i < 12:
            ssl_up.append(None)
            ssl_down.append(None)
        else:
            if hlv.iloc[i] == 1:
                ssl_up.append(df['high'].iloc[i-12:i+1].max())
                ssl_down.append(df['low'].iloc[i-12:i+1].min())
            else:
                ssl_up.append(df['low'].iloc[i-12:i+1].min())
                ssl_down.append(df['high'].iloc[i-12:i+1].max())
    df['ssl_up'], df['ssl_down'], df['ssl_channel'] = ssl_up, ssl_down, None
    for i in range(1, len(df)):
        if pd.notna(df['ssl_up'].iloc[i]) and pd.notna(df['ssl_down'].iloc[i]):
            prev, curr = df.iloc[i - 1], df.iloc[i]
            if prev['ssl_up'] < prev['ssl_down'] and curr['ssl_up'] > curr['ssl_down']:
                df.at[df.index[i], 'ssl_channel'] = 'LONG'
            elif prev['ssl_up'] > prev['ssl_down'] and curr['ssl_up'] < curr['ssl_down']:
                df.at[df.index[i], 'ssl_channel'] = 'SHORT'
    return df

def calculate_rsi(df, period=14):
    delta = df['close'].diff()
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gain).rolling(window=period).mean()
    avg_loss = pd.Series(loss).rolling(window=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

async def fetch_signal():
    ohlcv = exchange.fetch_ohlcv(PAIR, timeframe='15m', limit=100)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    df = calculate_ssl(df)
    df['rsi'] = calculate_rsi(df)
    valid_signals = df['ssl_channel'].dropna()
    if len(valid_signals) < 2:
        return None, df['close'].iloc[-1]
    prev, curr = valid_signals.iloc[-2], valid_signals.iloc[-1]
    price = df['close'].iloc[-1]
    rsi = df['rsi'].iloc[-1]
    base_price = df[df['ssl_channel'].notna()].iloc[-1]['close']
    
    if prev != curr:
        if curr == 'LONG' and price >= base_price * 1.002 and rsi > 55:
            return 'LONG', price
        elif curr == 'SHORT' and price <= base_price * 0.998 and rsi < 45:
            return 'SHORT', price
    return None, price

async def monitor_signal(app):
    global current_signal, last_cross, position
    while monitoring:
        try:
            signal, price = await fetch_signal()
            if signal and signal != current_signal:
                current_signal = signal
                last_cross = datetime.now(timezone.utc)
                sl = price * (0.995 if signal == 'LONG' else 1.005)
                tp = price * (1.005 if signal == 'LONG' else 0.995)
                deposit = 100  # Условный депозит для расчёта
                rr = abs(tp - price) / abs(price - sl)

                for chat_id in app.chat_ids:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🚀 Открытие позиции: {signal}\n"
                            f"💰 Депозит: {deposit}$\n"
                            f"🎯 Цена входа: {price:.2f}\n"
                            f"🛑 SL: {sl:.2f}\n"
                            f"✅ TP: {tp:.2f}"
                        )
                    )

                LOGS_WS.append_row([
                    last_cross.strftime('%Y-%m-%d %H:%M:%S'), signal, deposit,
                    price, sl, tp, round(rr, 2), '', ''
                ])

                # В данной версии мы не открываем реальную позицию на бирже — добавим позже

        except Exception as e:
            print("[error]", e)
        await asyncio.sleep(30)

# === COMMANDS ===
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global monitoring
    chat_id = update.effective_chat.id
    ctx.application.chat_ids.add(chat_id)
    monitoring = True
    await update.message.reply_text("✅ Мониторинг запущен.")
    asyncio.create_task(monitor_signal(ctx.application))

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global monitoring
    monitoring = False
    await update.message.reply_text("❌ Мониторинг остановлен.")

async def cmd_leverage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global leverage
    try:
        args = ctx.args
        if args:
            leverage = int(args[0])
            await update.message.reply_text(f"📈 Кредитное плечо установлено: {leverage}x")
        else:
            await update.message.reply_text(f"Текущее плечо: {leverage}x")
    except:
        await update.message.reply_text("Ошибка: укажи число — например /leverage 3")

# === RUN ===
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = set(CHAT_IDS)

    print("\n🔌 Проверка подключения к OKX...")
    balance = exchange.fetch_balance()
    print("✅ Подключено. Баланс USDT:", balance['total'].get('USDT', '—'))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_leverage))

    app.run_polling()
