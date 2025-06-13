import os
import asyncio
from datetime import datetime
import numpy as np
import pandas as pd
import ccxt
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# === ENV ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
LEVERAGE = int(os.getenv("LEVERAGE", 50))
PAIR = os.getenv("PAIR", "EUR/USDT")

# === STATE ===
current_signal = None
last_cross = None
position = None
log = []
monitoring = False

# === EXCHANGE ===
exchange = ccxt.mexc()


def calculate_ssl(df):
    sma = df['close'].rolling(13).mean()
    hlv = (df['close'] > sma).astype(int)

    ssl_up = []
    ssl_down = []

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

    df['ssl_up'] = ssl_up
    df['ssl_down'] = ssl_down
    df['ssl_channel'] = None

    for i in range(1, len(df)):
        if pd.notna(df['ssl_up'].iloc[i]) and pd.notna(df['ssl_down'].iloc[i]):
            prev = df.iloc[i - 1]
            curr = df.iloc[i]
            if prev['ssl_up'] < prev['ssl_down'] and curr['ssl_up'] > curr['ssl_down']:
                df.at[df.index[i], 'ssl_channel'] = 'LONG'
            elif prev['ssl_up'] > prev['ssl_down'] and curr['ssl_up'] < curr['ssl_down']:
                df.at[df.index[i], 'ssl_channel'] = 'SHORT'
    return df


async def fetch_ssl_signal():
    ohlcv = exchange.fetch_ohlcv(PAIR, timeframe='5m', limit=100)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)

    df = calculate_ssl(df)
    valid_signals = df['ssl_channel'].dropna()
    if len(valid_signals) < 2:
        return None, df['close'].iloc[-1]

    prev = valid_signals.iloc[-2]
    curr = valid_signals.iloc[-1]
    price = df['close'].iloc[-1]

    if prev != curr:
        return curr, price
    return None, price


async def monitor_signal(app):
    global current_signal, last_cross
    while monitoring:
        try:
            signal, price = await fetch_ssl_signal()
            if signal and signal != current_signal:
                current_signal = signal
                last_cross = datetime.utcnow()
                for chat_id in app.chat_ids:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=f"📡 Сигнал: {signal}\n💰 Цена: {price:.4f}\n⏰ Время: {last_cross.strftime('%H:%M UTC')}"
                    )
        except Exception as e:
            print("[error]", e)
        await asyncio.sleep(30)


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


async def cmd_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global position
    try:
        price = float(ctx.args[0])
        deposit = float(ctx.args[1])
        position = {
            "entry_price": price,
            "entry_deposit": deposit,
            "entry_time": datetime.utcnow(),
            "direction": current_signal
        }
        await update.message.reply_text(f"✅ Вход зафиксирован: {current_signal} @ {price:.4f} | Баланс: {deposit}$")
    except:
        await update.message.reply_text("⚠️ Использование: /entry <цена> <депозит>")


async def cmd_exit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global position
    try:
        exit_price = float(ctx.args[0])
        exit_deposit = float(ctx.args[1])
        if position is None:
            await update.message.reply_text("⚠️ Позиция не открыта.")
            return

        pnl = exit_deposit - position['entry_deposit']
        apr = (pnl / position['entry_deposit']) * 100
        duration = datetime.utcnow() - position['entry_time']
        minutes = int(duration.total_seconds() // 60)

        log.append({
            "entry": position,
            "exit_price": exit_price,
            "exit_deposit": exit_deposit,
            "pnl": pnl,
            "apr": apr,
            "duration_min": minutes
        })
        position = None

        await update.message.reply_text(
            f"✅ Сделка закрыта\n📈 P&L: {pnl:.2f} USDT\n📊 APR: {apr:.2f}%\n⏰ Время в позиции: {minutes} мин"
        )
    except:
        await update.message.reply_text("⚠️ Использование: /exit <цена> <депозит>")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if position:
        await update.message.reply_text(f"🔍 Позиция: {position['direction']} от {position['entry_price']}\nБаланс: {position['entry_deposit']}$")
    else:
        await update.message.reply_text("❌ Позиция не открыта.")


async def cmd_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not log:
        await update.message.reply_text("⚠️ Сделок пока нет.")
        return
    text = "📊 История сделок:\n"
    for i, trade in enumerate(log[-5:], 1):
        text += f"{i}. {trade['entry']['direction']} | P&L: {trade['pnl']:.2f}$ | APR: {trade['apr']:.2f}% | {trade['duration_min']} мин\n"
    await update.message.reply_text(text)


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global position, log
    position = None
    log.clear()
    await update.message.reply_text("♻️ История и позиция сброшены.")


if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = set()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("entry", cmd_entry))
    app.add_handler(CommandHandler("exit", cmd_exit))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("reset", cmd_reset))

    app.run_polling()
