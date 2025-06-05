"""
Alarm-bot  •  EURC/USDC  (Coinbase)
────────────────────────────────────────────────────────────────────────────
Команды:
/start
/set  <цена>                – установить базовую цену
/step <u> [d]               – пороги, % (u – вверх, d – вниз, d опционально)
/status                     – текущие настройки
/reset                      – сброс
"""

import asyncio, json, os, aiohttp
from typing import Dict, Any, Optional
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ── конфиг ────────────────────────────────────────────────────────────────
TOKEN              = os.getenv("BOT_TOKEN")                        # TG-токен
TICKER_URL         = "https://api.exchange.coinbase.com/products/EURC-USDC/ticker"
CHECK_INTERVAL     = 60            # сек
DEFAULT_STEP_UP    = 0.01          # % вверх от базы
DEFAULT_STEP_DOWN  = 0.01          # % вниз от базы
DECIMALS_SHOW      = 6
DATA_FILE          = "data.json"
# ──────────────────────────────────────────────────────────────────────────


# ── работа с файлом состояния ────────────────────────────────────────────
def load() -> Dict[str, Any]:
    """Читает JSON или создаёт структуру по умолчанию"""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            d = json.load(f)
            # миграция старого формата
            if "step_up" not in d:
                val = d.pop("step", DEFAULT_STEP_UP)
                d["step_up"] = d["step_down"] = val
            if "last_diff" not in d:
                d["last_diff"] = None
            return d
    return {
        "base": None,
        "last": None,         # цена последней отправки
        "last_diff": None,    # %-отклонение последней отправки
        "step_up":    DEFAULT_STEP_UP,
        "step_down":  DEFAULT_STEP_DOWN,
        "chats": []
    }


def save(d: Dict[str, Any]):                                  # pylint: disable=invalid-name
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


data = load()
watcher_task: Optional[asyncio.Task] = None


# ── получение цены с Coinbase ────────────────────────────────────────────
async def fetch_price() -> float:
    async with aiohttp.ClientSession() as s:
        async with s.get(TICKER_URL, timeout=15,
                         headers={"Accept": "application/json"}) as r:
            js = await r.json()
            return float(js["price"])


# ── фоновый цикл наблюдателя ─────────────────────────────────────────────
async def watcher(app):
    print("[watcher] started")
    while True:
        try:
            price = await fetch_price()
            print(f"[watcher] price {price:.{DECIMALS_SHOW}f}")

            base = data["base"]
            if base is not None:
                step_up   = data.get("step_up",   DEFAULT_STEP_UP)
                step_down = data.get("step_down", DEFAULT_STEP_DOWN)
                last_diff = data.get("last_diff")

                diff_now = (price - base) / base * 100  # текущее отклонение, %

                triggered = False
                # вверх
                if diff_now >= step_up:
                    cond = (last_diff is None) or (last_diff < step_up) \
                           or ((diff_now - last_diff) >= step_up)
                    triggered = cond
                # вниз
                elif diff_now <= -step_down:
                    cond = (last_diff is None) or (last_diff > -step_down) \
                           or ((last_diff - diff_now) >= step_down)
                    triggered = cond

                if triggered:
                    data.update({"last": price, "last_diff": diff_now}); save(data)
                    text = (
                        f"❗ EURC/USDC изменилась на {diff_now:+.4f}%\n"
                        f"Текущая цена: {price:.{DECIMALS_SHOW}f}"
                    )
                    for cid in data["chats"]:
                        try:
                            await app.bot.send_message(cid, text)
                        except Exception as e:
                            print(f"[send] {e}")

        except Exception as e:
            print(f"[watcher] {e}")

        await asyncio.sleep(CHECK_INTERVAL)


async def ensure_watcher(ctx: ContextTypes.DEFAULT_TYPE):
    global watcher_task
    if watcher_task is None or watcher_task.done():
        watcher_task = asyncio.create_task(watcher(ctx.application))


# ── команды ───────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid not in data["chats"]:
        data["chats"].append(cid); save(data)
    await ensure_watcher(ctx)

    await update.message.reply_text(
        "👋 Бот следит за EURC/USDC (Coinbase).\n"
        "• /set <цена>            – установить базу\n"
        "• /step <u> [d]          – пороги вверх/вниз, %\n"
        "   · /step 0.05          – одинаковые\n"
        "   · /step 0.05 0.15     – 0.05% вверх и 0.15% вниз\n"
        "• /status                – показать статус\n"
        "• /reset                 – сбросить всё"
    )


async def set_base(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_watcher(ctx)
    try:
        base = float(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ /set 1.142000")
        return
    data.update({"base": base, "last": None, "last_diff": None}); save(data)
    await update.message.reply_text(f"✅ База: {base:.{DECIMALS_SHOW}f}")


async def set_step(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ /step <up> [down]  – установить пороги """
    try:
        if len(ctx.args) == 1:
            up = down = float(ctx.args[0])
        elif len(ctx.args) == 2:
            up, down = map(float, ctx.args)
        else:
            raise ValueError
        if up <= 0 or down <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Пример: /step 0.05 0.15")
        return

    data["step_up"]   = up
    data["step_down"] = down
    data["last"] = None; data["last_diff"] = None  # сбросить историю алёртов
    save(data)
    await update.message.reply_text(f"✅ Порог вверх: {up}%  |  вниз: {down}%")


async def status(update: Update, _):
    await update.message.reply_text(
        "ℹ️ Текущий статус:\n"
        f"• База: {data['base']}\n"
        f"• Порог вверх: {data['step_up']} %\n"
        f"• Порог вниз:  {data['step_down']} %"
    )


async def reset(update: Update, _):
    data.update({
        "base": None,
        "last": None,
        "last_diff": None,
        "step_up":   DEFAULT_STEP_UP,
        "step_down": DEFAULT_STEP_DOWN
    }); save(data)
    await update.message.reply_text("♻️ Всё сброшено к настройкам по умолчанию.")


# ── bootstrap ─────────────────────────────────────────────────────────────
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
