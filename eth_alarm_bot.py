from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import json
import os
import asyncio
import aiohttp

# ── НАСТРОЙКИ ──────────────────────────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN")                 # TG-токен бота
DATA_FILE = "data.json"                       # хранение базовой цены
COINGECKO_ID = "usd-coin"                     # 👉 пара с экрана: USDC/EUR
VS_CURRENCY  = "eur"                          #              └── 0,88 EUR
CHECK_INTERVAL = 60                           # секунд между проверками
# ────────────────────────────────────────────────────────────────────────────

# ── UTILS: работа с файлом данных ──────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"base_price": None, "notified_steps": [], "chat_ids": []}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

data = load_data()

# ── TG-КОМАНДЫ ─────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in data["chat_ids"]:
        data["chat_ids"].append(chat_id)
        save_data(data)

    await update.message.reply_text(
        "👋 Бот запущен.\n"
        "Команды:\n"
        "• /set <цена>   — задать базовую EUR-цену (пример 0.8821)\n"
        "• /step <%>      — отклонение для тревоги\n"
        "• /status        — текущие настройки\n"
        "• /reset         — сброс"
    )

async def set_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(context.args[0])
        data["base_price"] = price
        data["last_notified_price"] = price
        data["notified_steps"] = []
        save_data(data)
        await update.message.reply_text(f"✅ Базовая цена установлена: {price} EUR")
    except:
        await update.message.reply_text("⚠️ Используй: /set 0.8821")

async def set_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        step = float(context.args[0])
        data["step"] = step
        save_data(data)
        await update.message.reply_text(f"✅ Процент отклонения установлен: {step}%")
    except:
        await update.message.reply_text("⚠️ Используй: /step 3")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    base = data.get("base_price")
    step = data.get("step")
    await update.message.reply_text(
        f"ℹ️ Базовая цена: {base} EUR\n"
        f"📉 Отклонение: {step}%"
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for k in ("base_price", "step", "notified_steps", "last_notified_price"):
        data[k] = None if k != "notified_steps" else []
    save_data(data)
    await update.message.reply_text("♻️ Настройки сброшены!")

# ── КУРС С COINGECKO ───────────────────────────────────────────────────────
async def get_token_price():
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={COINGECKO_ID}&vs_currencies={VS_CURRENCY}"
    )  # id=usd-coin, vs=eur → USDC/EUR  [oai_citation:0‡CoinGecko](https://www.coingecko.com/en/coins/usdc?utm_source=chatgpt.com)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=15) as response:
            price = await response.json()
            return price[COINGECKO_ID][VS_CURRENCY]

# ── ФОНОВЫЙ МОНИТОР ────────────────────────────────────────────────────────
async def check_price(app):
    await app.bot.initialize()
    while True:
        try:
            price = await get_token_price()

            base = data.get("base_price")
            step = data.get("step", 5)            # по умолчанию 5 %
            last_notified = data.get("last_notified_price")

            if base is None:
                # пользователь ещё не задал /set
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            percent_change = ((price - base) / base) * 100
            abs_change = abs(percent_change)

            # тревожим, если превысили step со времени последней тревоги
            if (last_notified is None or
                abs(((price - base) / base) * 100 -
                    ((last_notified - base) / base) * 100) >= step):

                data["last_notified_price"] = price
                save_data(data)

                for user_id in data.get("chat_ids", []):
                    try:
                        await app.bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"💸 EURC изменился на {percent_change:.2f}% "
                                f"от базовой: {price:.4f} EUR"
                            )
                        )
                    except Exception as e:
                        print(f"[check_price] ❌ Ошибка отправки: {e}")
        except Exception as e:
            print(f"[check_price] ❗ Ошибка в check_price: {e}")

        await asyncio.sleep(CHECK_INTERVAL)

# ── ЗАПУСК ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set", set_price))
    app.add_handler(CommandHandler("step", set_step))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("reset", reset))

    async def on_startup(app):
        asyncio.create_task(check_price(app))

    app.post_init = on_startup
    app.run_polling()
