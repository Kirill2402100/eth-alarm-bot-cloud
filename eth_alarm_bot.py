
import os
import json
import time
import requests
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DATA_FILE = "data.json"
CHECK_INTERVAL = 60

def load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            "base_price": None,
            "step_percent": 0.5,
            "notified_steps": [],
            "chat_ids": []
        }

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

data = load_data()

def get_eth_price():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"
        response = requests.get(url)
        return response.json()["ethereum"]["usd"]
    except:
        return None

async def price_checker(app):
    while True:
        if "chat_ids" not in data:
            data["chat_ids"] = []
        if data["base_price"] is not None:
            current_price = get_eth_price()
            if current_price is None:
                for chat_id in data["chat_ids"]:
                    await app.bot.send_message(chat_id=chat_id, text="⚠️ Не удалось получить цену ETH.")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            percent_change = ((current_price - data["base_price"]) / data["base_price"]) * 100
            step = int(percent_change / data["step_percent"])

            if step != 0 and step not in data["notified_steps"]:
                data["notified_steps"].append(step)
                save_data(data)
                direction = "🔺 вырос" if step > 0 else "🔻 упал"
                message = f"ETH {direction} на {abs(step * data['step_percent']):.2f}%: {current_price:.2f} USD"
                for chat_id in data["chat_ids"]:
                    await app.bot.send_message(chat_id=chat_id, text=message)

        await asyncio.sleep(CHECK_INTERVAL)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "chat_ids" not in data:
        data["chat_ids"] = []
    chat_id = update.effective_chat.id
    if chat_id not in data["chat_ids"]:
        data["chat_ids"].append(chat_id)
        save_data(data)
    await update.message.reply_text(
        "👋 Привет! Ты добавлен в список пользователей.

Команды:
/set <цена>
/step <процент>
/status
/reset"
    )

async def set_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(context.args[0])
        data["base_price"] = price
        data["notified_steps"] = []
        save_data(data)
        await update.message.reply_text(f"✅ Базовая цена установлена: {price} USD")
    except:
        await update.message.reply_text("⚠️ Используй: /set 3100")

async def set_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        step = float(context.args[0])
        data["step_percent"] = step
        data["notified_steps"] = []
        save_data(data)
        await update.message.reply_text(f"✅ Шаг установлен: {step}%")
    except:
        await update.message.reply_text("⚠️ Используй: /step 0.5")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    base = data.get("base_price", "не установлена")
    step = data.get("step_percent", "не задан")
    notified = data.get("notified_steps", [])
    await update.message.reply_text(
        f"📊 Настройки:
Базовая цена: {base}
Шаг: {step}%
Уведомленные шаги: {notified}"
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data["base_price"] = None
    data["step_percent"] = 0.5
    data["notified_steps"] = []
    save_data(data)
    await update.message.reply_text("🔄 Настройки сброшены.")

async def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set", set_price))
    app.add_handler(CommandHandler("step", set_step))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("reset", reset))

    asyncio.create_task(price_checker(app))
    print("✅ Бот запущен...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
