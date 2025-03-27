from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import json
import os

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
    await update.message.reply_text(
        "Привет! Я ETH-бот. Вот мои команды:\n\n"
        "/set <цена> — установить базовую цену\n"
        "/step <процент> — установить процент отклонения\n"
        "/status — текущие настройки\n"
        "/reset — сбросить настройки"
    )

# Команда /set
async def set_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(context.args[0])
        data["base_price"] = price
        data["notified_steps"] = []
        save_data(data)
        await update.message.reply_text(f"✅ Базовая цена установлена: {price} $")
    except:
        await update.message.reply_text("⚠️ Используй: /set 3100")

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
    await update.message.reply_text("♻️ Настройки сброшены!")

# Запуск бота
app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("set", set_price))
app.add_handler(CommandHandler("step", set_step))
app.add_handler(CommandHandler("status", status))
app.add_handler(CommandHandler("reset", reset))

app.run_polling()
