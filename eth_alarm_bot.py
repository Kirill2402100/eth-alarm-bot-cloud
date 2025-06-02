from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import json
import os
import asyncio
import aiohttp

# ── КОНФИГ ────────────────────────────────────────────────────────────────
TOKEN          = os.getenv("BOT_TOKEN")          # TG-токен
DATA_FILE      = "data.json"
COINGECKO_IDS  = "euro-coin,usd-coin"            # EURC и USDC
CHECK_INTERVAL = 60                              # сек.
DEFAULT_STEP   = 0.01                            # 0,01 % порог
DECIMALS_SHOW  = 6                               # сколько знаков выводить
# ──────────────────────────────────────────────────────────────────────────


# ── хранилище ─────────────────────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"base_price": None,
            "last_notified_price": None,
            "step": DEFAULT_STEP,
            "chat_ids": []}

def save_data(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

data = load_data()


# ── функции курса ─────────────────────────────────────────────────────────
async def get_eurc_usdc_ratio() -> float:
    """
    Возвращает отношение EURC/USDC с CoinGecko
    (оба токена в USD, затем EURC_USD / USDC_USD).
    """
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={COINGECKO_IDS}&vs_currencies=usd"
    )
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=15) as r:
            prices = await r.json()
            eurc_usd = prices["euro-coin"]["usd"]
            usdc_usd = prices["usd-coin"]["usd"]
            return eurc_usd / usdc_usd


# ── telegram-команды ──────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in data["chat_ids"]:
        data["chat_ids"].append(chat_id)
        save_data(data)

    await update.message.reply_text(
        "👋 Я слежу за курсом EURC/USDC.\n"
        "Команды:\n"
        "• /set <цена>   — задать базовую цену\n"
        "• /step <0.01>  — порог отклонения в % (по умолч. 0.01)\n"
        "• /status       — текущее состояние\n"
        "• /reset        — сброс настроек"
    )

async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        base = float(context.args[0])
        data["base_price"] = base
        data["last_notified_price"] = base
        save_data(data)
        await update.message.reply_text(f"✅ База установлена: {base:.{DECIMALS_SHOW}f}")
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Используй: /set 1.140000")

async def cmd_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        step = float(context.args[0])
        data["step"] = step
        save_data(data)
        await update.message.reply_text(f"✅ Порог установлен: {step}%")
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Используй: /step 0.01")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    base = data.get("base_price")
    step = data.get("step")
    await update.message.reply_text(
        f"ℹ️ Базовая цена: {base}\n"
        f"📉 Порог: {step}%"
    )

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data.update({"base_price": None,
                 "last_notified_price": None,
                 "step": DEFAULT_STEP})
    save_data(data)
    await update.message.reply_text("♻️ Настройки сброшены.")


# ── watcher ───────────────────────────────────────────────────────────────
async def price_watcher(app):
    await app.bot.initialize()
    while True:
        try:
            price = await get_eurc_usdc_ratio()

            base = data.get("base_price")
            if base is None:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            step = data.get("step", DEFAULT_STEP)
            last = data.get("last_notified_price")
            diff_now = ((price - base) / base) * 100
            diff_last = ((last - base) / base) * 100 if last else 0

            if abs(diff_now - diff_last) >= step:
                data["last_notified_price"] = price
                save_data(data)

                text = (
                    f"💶 EURC/USDC изменилась на {diff_now:+.4f}% "
                    f"(текущая {price:.{DECIMALS_SHOW}f})"
                )
                for chat in data["chat_ids"]:
                    try:
                        await app.bot.send_message(chat, text)
                    except Exception as e:
                        print(f"Send error: {e}")

        except Exception as e:
            print(f"Watcher error: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


# ── main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("set",    cmd_set))
    app.add_handler(CommandHandler("step",   cmd_step))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("reset",  cmd_reset))

    async def on_startup(app_):
        asyncio.create_task(price_watcher(app_))

    app.post_init = on_startup
    app.run_polling()
