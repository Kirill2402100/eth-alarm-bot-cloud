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
PAIR = os.getenv("PAIR")
if not PAIR:
    raise ValueError("❌ Ошибка: переменная PAIR не задана.")SHEET_ID = os.getenv("SHEET_ID")
LEVERAGE = int(os.getenv("LEVERAGE", 1))

# === GOOGLE SHEETS ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
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
monitoring = False
log = []

# === EXCHANGE: OKX ===
exchange = ccxt.okx({
    "apiKey": os.getenv("OKX_API_KEY"),
    "secret": os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
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

def check_rsi(df, signal):
    rsi_period = 14
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(rsi_period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(rsi_period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    df['rsi'] = rsi
    last_rsi = rsi.iloc[-1]
    return (signal == 'LONG' and last_rsi > 55) or (signal == 'SHORT' and last_rsi < 45)

async def fetch_ssl_signal():
    ohlcv = exchange.fetch_ohlcv(PAIR, timeframe='15m', limit=100)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    df = calculate_ssl(df)
    signal_series = df['ssl_channel'].dropna()
    if len(signal_series) < 2:
        return None, df
    prev, curr = signal_series.iloc[-2], signal_series.iloc[-1]
    if prev == curr:
        return None, df
    price_now = df['close'].iloc[-1]
    return curr, df

async def monitor_signal(app):
    global current_signal, last_cross, position, monitoring
    while monitoring:
        try:
            signal, df = await fetch_ssl_signal()
            price_now = df['close'].iloc[-1]
            if signal and signal != current_signal:
                confirmation_price = price_now * (1.002 if signal == 'LONG' else 0.998)
                if check_rsi(df, signal):
                    await asyncio.sleep(5)  # delay for next candle confirm
                    current_price = exchange.fetch_ticker(PAIR)['last']
                    if (signal == 'LONG' and current_price >= confirmation_price) or \
                       (signal == 'SHORT' and current_price <= confirmation_price):
                        current_signal = signal
                        last_cross = datetime.now(timezone.utc)
                        entry_price = current_price
                        stop_loss = round(entry_price * (0.995 if signal == 'LONG' else 1.005), 2)
                        take_profit = round(entry_price * (1.005 if signal == 'LONG' else 0.995), 2)

                        position = {
                            'direction': signal,
                            'entry_price': entry_price,
                            'stop_loss': stop_loss,
                            'take_profit': take_profit,
                            'entry_deposit': exchange.fetch_balance()['total'].get('USDT', 0)
                        }

                        # Telegram
                        for chat_id in app.chat_ids:
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=f"\u2705 Открыта позиция {signal}\nВход: {entry_price}\nSL: {stop_loss}\nTP: {take_profit}\nВремя: {last_cross.strftime('%H:%M:%S')}"
                            )

                        # Google Sheets
                        LOGS_WS.append_row([
                            last_cross.strftime('%Y-%m-%d %H:%M:%S'),
                            signal,
                            position['entry_deposit'],
                            entry_price,
                            stop_loss,
                            take_profit,
                            1, '', ''
                        ])
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
    leverage = ctx.args[0] if ctx.args else str(LEVERAGE)
    await update.message.reply_text(f"Текущее плечо установлено: x{leverage}")

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
