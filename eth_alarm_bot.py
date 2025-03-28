from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import json
import os
import asyncio
import aiohttp

# Загружаем токен бота из переменной окружения
TOKEN = os.getenv("BOT_TOKEN")

DATA_FILE = "data.json"

# Функции для сохранения и загрузки данных
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"base_price": None, "notified_steps": []}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

data = load_data()

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if "chat_ids" not in data:
        data["chat_ids"] = []
    if chat_id not in data["chat_ids"]:
        data["chat_ids"].append(chat_id)
        save_data(data)
    await update.message.reply_text(
        "👋 Бот запущен. Используй:\n/set <цена>\n/step <процент>\n/status\n/reset"
    )

# Команда /set
async def set_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(context.args[0])
        data["base_price"] = price
        data["last_notified_price"] = price  # <== ОБЯЗАТЕЛЬНО!
        data["notified_steps"] = []
        save_data(data)
        await update.message.reply_text(f"✅ Базовая цена установлена: {price} $")
    except:
        await update.message.reply_text("⚠️ Используй: /set 1000")

# Команда /step
async def set_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        step = float(context.args[0])
        data["step"] = step
        save_data(data)
        await update.message.reply_text(f"✅ Процент отклонения установлен: {step}%")
    except:
        await update.message.reply_text("⚠️ Используй: /step 3")

# Команда /status
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    base = data.get("base_price")
    step = data.get("step")
    await update.message.reply_text(
        f"ℹ️ Базовая цена: {base}\n"
        f"📉 Отклонение: {step}%"
    )

# Команда /reset
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data["base_price"] = None
    data["step"] = None
    data["notified_steps"] = []
    save_data(data)
    await update.message.reply_text("♻️ Настройки сброшен!")

async def get_eth_price():
    url = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            price = await response.json()
            return price["ethereum"]["usd"]

async def check_price(app):
    print("[check_price] Функция стартовала")
    await app.bot.initialize()

    # Загружаем последнее уведомлённое значение
    last_notified_price = data.get("last_notified_price", data.get("base_price"))

    while True:
        try:
            price = await get_eth_price()
            print(f"[check_price] ETH сейчас: {price}")
            base = data.get("base_price")
            step = data.get("step", 1)

            if base and last_notified_price:
                change_percent = abs(price - last_notified_price) / last_notified_price * 100
                print(f"[check_price] Последнее уведомление: {last_notified_price}, изменение: {change_percent:.2f}%")

                if change_percent >= step:
                    data["last_notified_price"] = price
                    save_data(data)
                    for user_id in data.get("chat_ids", []):
                        try:
                            print(f"[check_price] 🔔 Уведомляем {user_id}")
                            await app.bot.send_message(
                                chat_id=user_id,
                                text=f"💸 ETH изменился на {change_percent:.2f}%: {price} $"
                            )
                        except Exception as e:
                            print(f"[check_price] ❌ Ошибка при отправке: {e}")
            else:
                print("[check_price] ❗ Базовая цена или последнее уведомление не установлены")
        except Exception as e:
            print(f"[check_price] ❗ Ошибка во время проверки цены: {e}")

        await asyncio.sleep(60)

if __name__ == "__main__":
    import asyncio

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set", set_price))
    app.add_handler(CommandHandler("step", set_step))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("reset", reset))

    async def on_startup(app):
        print("[check_price] Функция стартовала")
        asyncio.create_task(check_price(app))

    app.post_init = on_startup
    app.run_polling()
