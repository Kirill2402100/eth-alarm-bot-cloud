"""
Alarm-bot: EURC/USDC (Bitstamp)
────────────────────────────────────────────────────────────────────────────
Команды:
  /start                – добавить чат и запустить слежение
  /set   <цена>         – установить базовую цену
  /step  <0.01>         – порог отклонения в % (по умолчанию 0.01)
  /status               – показать базу и порог
  /reset                – сброс настроек
"""

import asyncio
import json
import os
from typing import Dict, Any

import aiohttp
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ── конфиг ────────────────────────────────────────────────────────────────
TOKEN           = os.getenv("BOT_TOKEN")              # TG-токен
DATA_FILE       = "data.json"
TICKER_URL      = "https://www.bitstamp.net/api/v2/ticker/eurcusdc/"  # EURC/USDC
CHECK_INTERVAL  = 60          # секунд между запросами
DEFAULT_STEP    = 0.01        # % по умолчанию
DECIMALS_SHOW   = 6           # знаков после запятой
# ──────────────────────────────────────────────────────────────────────────


# ╭─ helpers для хранения состояния ───────────────────────────────────────╮
def load_data() -> Dict[str, Any]:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {
        "base_price": None,
        "last_notified_price": None,
        "step": DEFAULT_STEP,
        "chat_ids": [],
    }


def save_data(d: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


data = load_data()
watcher_task: asyncio.Task | None = None


# ╭─ получение цены ───────────────────────────────────────────────────────╮
async def get_eurc_usdc_price() -> float:
    """
    Берёт последний курс EURC/USDC с Bitstamp.
    Bitstamp возвращает JSON вида:
        {"last":"1.14022800", "high":"...", ...}
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(TICKER_URL, timeout=15) as resp:
            ticker = await resp.json()
            return float(ticker["last"])


# ╭─ фоновый цикл ──────────────────────────────────────────────────────────╮
async def price_watcher(app):
    print("[watcher] started")
    while True:
        try:
            price = await get_eurc_usdc_price()
            print(f"[watcher] price {price:.{DECIMALS_SHOW}f}")

            base = data.get("base_price")
            if base is not None:
                step = data.get("step", DEFAULT_STEP)
                last = data.get("last_notified_price")

                diff_now = ((price - base) / base) * 100  # % от базы
                diff_last = (
                    ((last - base) / base) * 100 if last is not None else 0
                )

                if abs(diff_now) >= step and abs(diff_now - diff_last) >= step:
                    # фиксируем новое срабатывание
                    data["last_notified_price"] = price
                    save_data(data)

                    text = (
                        f"💶 EURC/USDC изменилась на {diff_now:+.4f}%\n"
                        f"Текущий курс: <code>{price:.{DECIMALS_SHOW}f}</code>"
                    )
                    for chat_id in data["chat_ids"]:
                        try:
                            await app.bot.send_message(
                                chat_id, text, parse_mode="HTML"
                            )
                        except Exception as e:
                            print(f"[send] {e}")

        except Exception as e:
            print(f"[watcher] {e}")

        await asyncio.sleep(CHECK_INTERVAL)


async def ensure_watcher(ctx: ContextTypes.DEFAULT_TYPE):
    """
    Запускаем watcher один раз на всё приложение.
    """
    global watcher_task
    if watcher_task is None or watcher_task.done():
        watcher_task = asyncio.create_task(price_watcher(ctx.application))


# ╭─ команды Telegram ─────────────────────────────────────────────────────╮
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid not in data["chat_ids"]:
        data["chat_ids"].append(cid)
        save_data(data)

    await ensure_watcher(ctx)

    await update.message.reply_text(
        "👋 Слежу за <b>EURC / USDC</b> (Bitstamp).\n\n"
        "• /set <цена>  – задать базу\n"
        "• /step <0.01> – порог в %\n"
        "• /status      – статус\n"
        "• /reset       – сброс",
        parse_mode="HTML",
    )


async def cmd_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_watcher(ctx)
    try:
        base = float(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Используй: /set 1.140000")
        return

    data["base_price"] = base
    data["last_notified_price"] = None
    save_data(data)
    await update.message.reply_text(f"✅ База установлена: {base:.{DECIMALS_SHOW}f}")


async def cmd_step(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        step = float(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Используй: /step 0.01")
        return

    data["step"] = step
    save_data(data)
    await update.message.reply_text(f"✅ Порог установлен: {step} %")


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"ℹ️ База: {data.get('base_price')}\n"
        f"📉 Порог: {data.get('step', DEFAULT_STEP)} %",
    )


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


# ╭─ bootstrap ─────────────────────────────────────────────────────────────╮
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN env var is missing")

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    # handlers
    application.add_handler(CommandHandler("start",  cmd_start))
    application.add_handler(CommandHandler("set",    cmd_set))
    application.add_handler(CommandHandler("step",   cmd_step))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("reset",  cmd_reset))

    application.run_polling()
