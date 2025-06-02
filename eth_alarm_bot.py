"""
EURC/USDC volatility bot
────────────────────────────────────────────────────────────────────────────
• Берёт цены EURC и USDC (в USD) с CoinGecko и считает отношение ─ курс
  EURC/USDC.
• Шлёт алёрт, если курс ушёл дальше, чем на заданный % (по умолчанию 0,01 %).
• Точность вывода – 6 знаков после запятой.
"""

import asyncio
import json
import os
from typing import Dict, Any

import aiohttp
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ──────────────────────────── К О Н Ф И Г ────────────────────────────────
TOKEN            = os.getenv("BOT_TOKEN")         # TG-токен
DATA_FILE        = "data.json"
COINGECKO_IDS    = "euro-coin,usd-coin"           # ID’ы EURC и USDC
CHECK_INTERVAL   = 60                             # сек между запросами
DEFAULT_STEP     = 0.01                           # порог 0,01 %
DECIMALS_SHOW    = 6                              # знаков после запятой
# ──────────────────────────────────────────────────────────────────────────


# ╭─ helpers ───────────────────────────────────────────────────────────────╮
def load_data() -> Dict[str, Any]:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as fp:
            return json.load(fp)
    return {
        "base_price": None,
        "last_notified_price": None,
        "step": DEFAULT_STEP,
        "chat_ids": [],
    }


def save_data(d: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w") as fp:
        json.dump(d, fp, ensure_ascii=False, indent=2)


data = load_data()


async def get_eurc_usdc_ratio() -> float:
    """Отношение EURC/USDC (оба в USD) через CoinGecko."""
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={COINGECKO_IDS}&vs_currencies=usd"
    )
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, timeout=15) as resp:
            prices = await resp.json()
            eurc_usd = prices["euro-coin"]["usd"]
            usdc_usd = prices["usd-coin"]["usd"]
            return eurc_usd / usdc_usd


# ╭─ telegram-handlers ─────────────────────────────────────────────────────╮
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    if chat not in data["chat_ids"]:
        data["chat_ids"].append(chat)
        save_data(data)

    await update.message.reply_text(
        "👋 Слежу за <b>EURC / USDC</b>.\n\n"
        "Команды:\n"
        "• /set &lt;цена&gt;  — задать базу (пример 1.140000)\n"
        "• /step &lt;0.01&gt; — порог в % (по умолч. 0.01)\n"
        "• /status          — текущее состояние\n"
        "• /reset           — сброс настроек",
        parse_mode="HTML",
    )


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        base = float(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Используй: /set 1.140000")
        return

    data["base_price"] = base
    data["last_notified_price"] = base
    save_data(data)
    await update.message.reply_text(f"✅ База установлена: {base:.{DECIMALS_SHOW}f}")


async def cmd_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        step = float(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Используй: /step 0.01")
        return

    data["step"] = step
    save_data(data)
    await update.message.reply_text(f"✅ Порог установлен: {step} %")


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE):
    base = data.get("base_price")
    step = data.get("step", DEFAULT_STEP)
    await update.message.reply_text(f"ℹ️ База: {base}\n📉 Порог: {step} %")


async def cmd_reset(update: Update, _: ContextTypes.DEFAULT_TYPE):
    data.update(
        {
            "base_price": None,
            "last_notified_price": None,
            "step": DEFAULT_STEP,
        }
    )
    save_data(data)
    await update.message.reply_text("♻️ Настройки сброшены.")


# ╭─ watcher ───────────────────────────────────────────────────────────────╮
async def price_watcher(app):
    """Фоновая корутина: проверяет курс и шлёт алёрт."""
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
                    f"💶 EURC/USDC изменилась на {diff_now:+.4f}%\n"
                    f"Текущий курс: <code>{price:.{DECIMALS_SHOW}f}</code>"
                )
                for chat in data["chat_ids"]:
                    try:
                        await app.bot.send_message(chat, text, parse_mode="HTML")
                    except Exception as e:
                        print(f"[send] {e}")

        except Exception as e:
            print(f"[watcher] {e}")

        await asyncio.sleep(CHECK_INTERVAL)


# ╭─ bootstrap ─────────────────────────────────────────────────────────────╮
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN env var is not set")

    async def on_startup(app):
        # запускаем фоновый watcher
        asyncio.create_task(price_watcher(app))

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(on_startup)          # ← регистрируем колбэк ЗДЕСЬ
        .build()
    )

    # handlers
    application.add_handler(CommandHandler("start",  cmd_start))
    application.add_handler(CommandHandler("set",    cmd_set))
    application.add_handler(CommandHandler("step",   cmd_step))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("reset",  cmd_reset))

    application.run_polling()           # без параметра post_init
