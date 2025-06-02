"""
EURC/USDC volatility bot (asyncio task)
────────────────────────────────────────────────────────────────────────────
• Берёт EURC и USDC (в USD) с CoinGecko, делит → курс EURC/USDC.
• Алёрт, если |Δ| ≥ step % (по умолчанию 0,01 %).
• Фоновая проверка запускается через asyncio.create_task() после /start.
"""

import asyncio, json, os, aiohttp
from typing import Dict, Any
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ── конфиг ────────────────────────────────────────────────────────────────
TOKEN          = os.getenv("BOT_TOKEN")          # TG-токен
DATA_FILE      = "data.json"
COINGECKO_IDS  = "euro-coin,usd-coin"            # CoinGecko-ID’ы
CHECK_INTERVAL = 60                              # сек
DEFAULT_STEP   = 0.01                            # %
DECIMALS_SHOW  = 6                               # знаков после запятой
# ──────────────────────────────────────────────────────────────────────────


# ╭─ helpers ───────────────────────────────────────────────────────────────╮
def load_data() -> Dict[str, Any]:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as fp:
            return json.load(fp)
    return {"base_price": None,
            "last_notified_price": None,
            "step": DEFAULT_STEP,
            "chat_ids": []}


def save_data(d: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w") as fp:
        json.dump(d, fp, ensure_ascii=False, indent=2)


data = load_data()
watcher_task: asyncio.Task | None = None      # глобальная ссылка на фон-таск


async def get_eurc_usdc_ratio() -> float:
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={COINGECKO_IDS}&vs_currencies=usd"
    )
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=15) as r:
            prices = await r.json()
            eurc = prices["euro-coin"]["usd"]
            usdc = prices["usd-coin"]["usd"]
            return eurc / usdc


# ╭─ watcher ───────────────────────────────────────────────────────────────╮
async def price_watcher(app):
    print("[watcher] started")
    while True:
        try:
            price = await get_eurc_usdc_ratio()
            print(f"[watcher] price {price:.{DECIMALS_SHOW}f}")

            base = data.get("base_price")
            if base is not None:
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
                    for cid in data["chat_ids"]:
                        try:
                            await app.bot.send_message(cid, text, parse_mode="HTML")
                        except Exception as e:
                            print(f"[send] {e}")

        except Exception as e:
            print(f"[watcher] {e}")

        await asyncio.sleep(CHECK_INTERVAL)


# ╭─ telegram-команды ─────────────────────────────────────────────────────╮
async def ensure_watcher(ctx: ContextTypes.DEFAULT_TYPE):
    global watcher_task
    if watcher_task is None or watcher_task.done():
        watcher_task = asyncio.create_task(price_watcher(ctx.application))


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid not in data["chat_ids"]:
        data["chat_ids"].append(cid); save_data(data)

    await ensure_watcher(ctx)

    await update.message.reply_text(
        "👋 Слежу за <b>EURC / USDC</b>.\n\n"
        "Команды:\n"
        "• /set <цена>  — задать базу (пример 1.140000)\n"
        "• /step <0.01> — порог в % (по умолч. 0.01)\n"
        "• /status      — статус\n"
        "• /reset       — сброс",
        parse_mode="HTML",
    )


async def cmd_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        base = float(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ /set 1.140000")
        return

    data["base_price"] = base
    data["last_notified_price"] = base
    save_data(data)
    await update.message.reply_text(f"✅ База: {base:.{DECIMALS_SHOW}f}")


async def cmd_step(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        step = float(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ /step 0.01")
        return

    data["step"] = step; save_data(data)
    await update.message.reply_text(f"✅ Порог: {step} %")


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"ℹ️ База: {data.get('base_price')}\n"
        f"📉 Порог: {data.get('step', DEFAULT_STEP)} %",
    )


async def cmd_reset(update: Update, _: ContextTypes.DEFAULT_TYPE):
    data.update({"base_price": None, "last_notified_price": None, "step": DEFAULT_STEP})
    save_data(data)
    await update.message.reply_text("♻️ Настройки сброшены.")


# ╭─ bootstrap ─────────────────────────────────────────────────────────────╮
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN env var missing")

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("set",    cmd_set))
    app.add_handler(CommandHandler("step",   cmd_step))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("reset",  cmd_reset))

    app.run_polling()
