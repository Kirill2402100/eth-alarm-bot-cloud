import os
import asyncio
import logging
from telegram import Update, constants, BotCommand
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, PicklePersistence

import scanner_bmr_dca as scanner_engine
import trade_executor

# --- Конфигурация ---
BOT_VERSION = "BMR-DCA EURC v0.1"
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is not set")

DEFAULT_BANK_USDT = 1000.0
DEFAULT_BUFFER_OVER_EDGE = 0.30

log = logging.getLogger("bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- Утилиты ---
def is_loop_running(app: Application) -> bool:
    """Проверяет, запущен ли основной цикл сканера."""
    task = getattr(app, "_main_loop_task", None)
    return task is not None and not task.done()

async def post_init(app: Application):
    """Выполняется после запуска приложения."""
    log.info("Бот запущен. Проверяем, нужно ли запускать основной цикл...")
    if app.bot_data.get('run_loop_on_startup', False):
        log.info("Обнаружен флаг 'run_loop_on_startup'. Запускаю основной цикл.")
        task = asyncio.create_task(scanner_engine.scanner_main_loop(app, broadcast))
        setattr(app, "_main_loop_task", task)

    # ИЗМЕНЕНО: Добавлена команда /close
    await app.bot.set_my_commands([
        BotCommand("start", "Запустить/перезапустить бота"),
        BotCommand("run", "Запустить сканер"),
        BotCommand("stop", "Остановить сканер"),
        BotCommand("status", "Показать текущий статус и параметры"),
        BotCommand("pause", "Приостановить поиск новых сигналов"),
        BotCommand("resume", "Возобновить поиск новых сигналов"),
        BotCommand("close", "Закрыть текущую позицию по рынку"),
        BotCommand("setbank", "Установить общий банк позиции, USDT"),
        BotCommand("setbuf", "Установить буфер за границей (напр. 0.3 или 30%)"),
    ])

async def broadcast(app: Application, txt: str):
    """Отправляет сообщение всем подписанным пользователям с очисткой заблокировавших."""
    chat_ids = set(app.bot_data.get('chat_ids', set()))
    for cid in list(chat_ids):
        try:
            await app.bot.send_message(chat_id=cid, text=txt, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.error(f"Не удалось отправить сообщение в чат {cid}: {e}")
            if "bot was blocked" in str(e):
                chat_ids.discard(cid)
                log.info(f"Чат {cid} удален из списка рассылки (бот заблокирован).")
    app.bot_data['chat_ids'] = chat_ids

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start."""
    chat_id = update.effective_chat.id
    bd = ctx.bot_data
    bd.setdefault('chat_ids', set()).add(chat_id)
    bd.setdefault('run_loop_on_startup', False)
    bd.setdefault('scan_paused', False)
    log.info(f"Пользователь {chat_id} запустил бота.")
    await update.message.reply_text(
        f"✅ <b>Бот {BOT_VERSION} запущен.</b>\nИспользуйте /run для запуска сканера.",
        parse_mode=constants.ParseMode.HTML
    )

async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /run."""
    app = ctx.application
    if is_loop_running(app):
        await update.message.reply_text("ℹ️ Сканер уже запущен. Для остановки используйте /stop.")
        return

    app.bot_data['bot_on'] = True
    app.bot_data['run_loop_on_startup'] = True
    app.bot_data['scan_paused'] = False
    log.info("Команда /run: запускаем основной цикл.")
    task = asyncio.create_task(scanner_engine.scanner_main_loop(app, broadcast))
    setattr(app, "_main_loop_task", task)
    await update.message.reply_text("🚀 <b>Запускаю сканер...</b>", parse_mode=constants.ParseMode.HTML)

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /stop."""
    app = ctx.application
    if not is_loop_running(app):
        await update.message.reply_text("ℹ️ Сканер уже остановлен.")
        return

    app.bot_data['bot_on'] = False
    task = getattr(app, "_main_loop_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            log.info("Основной цикл успешно остановлен.")
        setattr(app, "_main_loop_task", None)
        
    app.bot_data['run_loop_on_startup'] = False
    log.info("Команда /stop: останавливаем основной цикл.")
    await update.message.reply_text("🛑 <b>Сканер остановлен.</b>", parse_mode=constants.ParseMode.HTML)

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Приостанавливает поиск новых сигналов."""
    if not is_loop_running(ctx.application):
        await update.message.reply_text("ℹ️ Сканер не запущен, нечего ставить на паузу.")
        return
    ctx.bot_data["scan_paused"] = True
    log.info("Команда /pause: поиск новых сигналов приостановлен.")
    await update.message.reply_text("⏸️ <b>Поиск новых сигналов приостановлен.</b>\nСопровождение открытых сделок продолжается. Для возобновления используйте /resume.", parse_mode=constants.ParseMode.HTML)

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Возобновляет поиск новых сигналов."""
    if not is_loop_running(ctx.application):
        await update.message.reply_text("ℹ️ Сканер не запущен.")
        return
    ctx.bot_data["scan_paused"] = False
    log.info("Команда /resume: поиск новых сигналов возобновлен.")
    await update.message.reply_text("▶️ <b>Поиск новых сигналов возобновлён.</b>", parse_mode=constants.ParseMode.HTML)

# ДОБАВЛЕНО: Команда ручного закрытия
async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.bot_data.get("position"):
        await update.message.reply_text("ℹ️ Нет активной позиции.")
        return
    ctx.bot_data["force_close"] = True
    await update.message.reply_text("🧰 Закрываю позицию по рынку на ближайшем цикле…")

async def cmd_setbank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(ctx.args[0])
        if val <= 0: raise ValueError
        ctx.bot_data["safety_bank_usdt"] = val
        await update.message.reply_text(f"💰 Банк на позицию установлен: {val:.2f} USDT")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /setbank 1000")

async def cmd_setbuf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        raw = ctx.args[0].strip().replace('%','')
        v = float(raw)
        buf = v/100.0 if v > 1 else v
        if not (0.0 < buf < 0.9): raise ValueError
        ctx.bot_data["buffer_over_edge"] = buf
        await update.message.reply_text(f"🛡️ Буфер за границей установлен: {buf:.2%}")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /setbuf 0.30 (или 30%)")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показывает текущий статус бота и параметры стратегии."""
    bot_data = ctx.bot_data
    is_running = is_loop_running(ctx.application)
    is_paused = bot_data.get("scan_paused", False)
    
    active_position = bot_data.get('position', None)
    cfg = scanner_engine.CONFIG

    scanner_status = "🔌 ОСТАНОВЛЕН"
    if is_running:
        scanner_status = "⏸️ НА ПАУЗЕ" if is_paused else "⚡️ РАБОТАЕТ"

    position_status = "Нет активной позиции."
    if active_position:
        pos = active_position
        sl_show = f"{pos.sl_price:.6f}" if pos.sl_price is not None else "N/A"
        tp_show = f"{pos.tp_price:.6f}" if getattr(pos, "tp_price", None) else "N/A"
        avg_show = f"{pos.avg:.6f}" if getattr(pos, "avg", None) else "N/A"

        max_steps = getattr(pos, "max_steps", (len(getattr(pos, "step_margins", [])) or scanner_engine.CONFIG.DCA_LEVELS))
        lev_show = getattr(pos, "leverage", getattr(scanner_engine.CONFIG, "LEVERAGE", "N/A"))
        
        reserved = getattr(pos, "reserved_one", False)
        remaining = max(0, getattr(pos, "max_steps", 0) - getattr(pos, "steps_filled", 0))
        
        position_status = (
            f"• <b>Сигнал ID:</b> {pos.signal_id}\n"
            f"• <b>Сторона:</b> {pos.side}\n"
            f"• <b>Плечо:</b> {lev_show}x\n"
            f"• <b>Ступеней:</b> {pos.steps_filled} / {max_steps}\n"
            f"• <b>Средняя цена:</b> <code>{avg_show}</code>\n"
            f"• <b>TP/SL:</b> <code>{tp_show}</code> / <code>{sl_show}</code>\n"
            f"• <b>Резерв активирован:</b> {'Да' if reserved else 'Нет'}\n"
            f"• <b>Осталось (обычных | резерв):</b> {remaining if not reserved else 0} | {1 if reserved and remaining > 0 else (1 if not reserved else 0)}"
        )
    
    bank = bot_data.get("safety_bank_usdt", getattr(cfg, "SAFETY_BANK_USDT", DEFAULT_BANK_USDT))
    buf  = bot_data.get("buffer_over_edge", getattr(cfg, "BUFFER_OVER_EDGE", DEFAULT_BUFFER_OVER_EDGE))
    step = bot_data.get("base_step_margin", getattr(cfg, "BASE_STEP_MARGIN", 10.0))
    
    msg = (
        f"<b>Состояние бота {BOT_VERSION}</b>\n\n"
        f"<b>Статус сканера:</b> {scanner_status}\n\n"
        f"<b><u>Риск/банк:</u></b>\n"
        f"• Банк позиции: <b>{bank:.2f} USDT</b>\n"
        f"• Буфер за границей: <b>{buf:.2%}</b> (неактивно в MARGIN-режиме)\n"
        f"• Депозит на шаг: <b>{step:.2f} USDT</b> (неактивно при bank-first)\n"
        f"• DCA: 4 внутр. + 1 резерв\n\n"
        f"<b><u>Активная позиция:</u></b>\n{position_status}"
    )

    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)


if __name__ == "__main__":
    persistence = PicklePersistence(filepath="bot_persistence")
    app = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("setbank", cmd_setbank))
    app.add_handler(CommandHandler("setbuf", cmd_setbuf))
    # ДОБАВЛЕНО: Регистрация хендлера /close
    app.add_handler(CommandHandler("close", cmd_close))

    log.info(f"Bot {BOT_VERSION} starting...")
    app.run_polling()
