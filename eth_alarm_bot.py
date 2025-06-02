"""
Alarm-Bot: EURC/USDC (Bitstamp)
────────────────────────────────────────────────────────────────────────────
Команды: /start  /set <цена>  /step <0.01>  /status  /reset
"""

import asyncio, json, os, aiohttp
from typing import Dict, Any
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ── конфиг ────────────────────────────────────────────────────────────────
TOKEN           = os.getenv("BOT_TOKEN")          # TG-token
TICKER_URL      = "https://www.bitstamp.net/api/v2/ticker/eurocusdc/"  # ✔
CHECK_INTERVAL  = 60          # сек
DEFAULT_STEP    = 0.01        # %
DECIMALS_SHOW   = 6
DATA_FILE       = "data.json"
# ──────────────────────────────────────────────────────────────────────────


# ╭─ helpers ───────────────────────────────────────────────────────────────╮
def load() -> Dict[str, Any]:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"base": None, "last": None, "step": DEFAULT_STEP, "chats": []}


def save(d: Dict[str, Any]):  # pylint: disable=invalid-name
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


data = load()
watcher_task: asyncio.Task | None = None


async def fetch_price() -> float:
    async with aiohttp.ClientSession() as s:
        async with s.get(TICKER_URL, timeout=15) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            js = await r.json()
            return float(js["last"])


# ╭─ watcher ───────────────────────────────────────────────────────────────╮
async def watcher(app):
    print("[watcher] started")
    while True:
        try:
            price = await fetch_price()
            print(f"[watcher] price {price:.{DECIMALS_SHOW}f}")

            base = data["base"]
            if base is None:
                await asyncio.sleep(CHECK_INTERVAL); continue

            step = data.get("step", DEFAULT_STEP)
            last = data.get("last")

            diff_now  = (price - base) / base * 100
            diff_last = (last  - base) / base * 100 if last else 0

            if abs(diff_now) >= step and abs(diff_now - diff_last) >= step:
                data["last"] = price; save(data)
                text = (
                    f"💶 EURC/USDC изменился на {diff_now:+.4f}%\n"
                    f"Текущая цена: {price:.{DECIMALS_SHOW}f}"
                )
                for cid in data["chats"]:
                    try: await app.bot.send_message(cid, text)
                    except Exception as e: print(f"[send] {e}")

        except Exception as e:
            print(f"[watcher] {e}")

        await asyncio.sleep(CHECK_INTERVAL)


async def ensure_watcher(ctx: ContextTypes.DEFAULT_TYPE):
    global watcher_task
    if watcher_task is None or watcher_task.done():
        watcher_task = asyncio.create_task(watcher(ctx.application))


# ╭─ команды ───────────────────────────────────────────────────────────────╮
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid not in data["chats"]:
        data["chats"].append(cid); save(data)
    await ensure_watcher(ctx)
    await update.message.reply_text(
        "👋 Бот следит за EURC/USDC (Bitstamp).\n"
        "• /set <цена> – база\n• /step <процент> – порог\n• /status – статус"
    )

async def set_base(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_watcher(ctx)
    try:  base = float(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ /set 1.140000"); return
    data.update({"base": base, "last": None}); save(data)
    await update.message.reply_text(f"✅ База: {base:.{DECIMALS_SHOW}f}")

async def set_step(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:  step = float(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ /step 0.01"); return
    data["step"] = step; save(data)
    await update.message.reply_text(f"✅ Порог: {step} %")

async def status(update: Update, _):
    await update.message.reply_text(
        f"ℹ️ База: {data['base']}\n📉 Порог: {data['step']} %"
    )

async def reset(update: Update, _):
    data.update({"base": None, "last": None, "step": DEFAULT_STEP}); save(data)
    await update.message.reply_text("♻️ Настройки сброшены.")


# ╭─ bootstrap ─────────────────────────────────────────────────────────────╮
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN env var missing")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("set",    set_base))
    app.add_handler(CommandHandler("step",   set_step))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("reset",  reset))
    app.run_polling()
