import os
import asyncio
import logging
from telegram import Update, constants
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, PicklePersistence

import scanner_wick_spike as scanner_engine 
import trade_executor

# --- Конфигурация ---
BOT_VERSION = "SwingBot-WickSpike-2.0-Scoring" # Версия обновлена
BOT_TOKEN = os.getenv("BOT_TOKEN")

log = logging.getLogger("bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

async def post_init(app: Application):
    """Выполняется после запуска приложения."""
    log.info("Бот запущен. Проверяем, нужно ли запускать основной цикл...")
    if app.bot_data.get('run_loop_on_startup', False):
        log.info("Обнаружен флаг 'run_loop_on_startup'. Запускаю основной цикл.")
        task = asyncio.create_task(scanner_engine.scanner_main_loop(app, broadcast))
        app.bot_data['main_loop_task'] = task
    
    await app.bot.set_my_commands([
        ('start', 'Запустить/перезапустить бота'),
        ('run', 'Запустить сканер'),
        ('stop', 'Остановить сканер'),
        ('status', 'Показать текущий статус и параметры'),
        ('pause', 'Приостановить поиск новых сигналов'),
        ('resume', 'Возобновить поиск новых сигналов'),
    ])

async def broadcast(app: Application, txt: str):
    """Отправляет сообщение всем подписанным пользователям."""
    chat_ids = app.bot_data.get('chat_ids', set())
    for cid in chat_ids:
        try:
            await app.bot.send_message(chat_id=cid, text=txt, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.error(f"Не удалось отправить сообщение в чат {cid}: {e}")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start."""
    chat_id = update.effective_chat.id
    ctx.bot_data.setdefault('chat_ids', set()).add(chat_id)
    log.info(f"Пользователь {chat_id} запустил бота.")
    await update.message.reply_text(
        f"✅ <b>Бот v{BOT_VERSION} запущен.</b>\nИспользуйте /run для запуска сканера.",
        parse_mode=constants.ParseMode.HTML
    )

async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /run."""
    app = ctx.application
    is_running = not (app.bot_data.get('main_loop_task') is None or app.bot_data['main_loop_task'].done())
    if is_running:
        await update.message.reply_text("ℹ️ Сканер уже запущен. Для остановки используйте /stop.")
        return
    
    app.bot_data['bot_on'] = True
    app.bot_data['run_loop_on_startup'] = True
    app.bot_data['scan_paused'] = False
    log.info("Команда /run: запускаем основной цикл.")
    await update.message.reply_text("🚀 <b>Запускаю сканер...</b>")
    task = asyncio.create_task(scanner_engine.scanner_main_loop(app, broadcast))
    app.bot_data['main_loop_task'] = task

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /stop."""
    app = ctx.application
    is_running = not (app.bot_data.get('main_loop_task') is None or app.bot_data['main_loop_task'].done())
    if not is_running:
        await update.message.reply_text("ℹ️ Сканер уже остановлен.")
        return
        
    app.bot_data['bot_on'] = False
    if app.bot_data.get('main_loop_task'):
        app.bot_data['main_loop_task'].cancel()
    app.bot_data['run_loop_on_startup'] = False
    log.info("Команда /stop: останавливаем основной цикл.")
    await update.message.reply_text("🛑 <b>Сканер остановлен.</b>")
    
async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Приостанавливает поиск новых сигналов."""
    is_running = ctx.bot_data.get('bot_on', False)
    if not is_running:
        await update.message.reply_text("ℹ️ Сканер не запущен, нечего ставить на паузу.")
        return

    ctx.bot_data["scan_paused"] = True
    log.info("Команда /pause: поиск новых сигналов приостановлен.")
    await update.message.reply_text("⏸️ <b>Поиск новых сигналов приостановлен.</b>\nСопровождение открытых сделок продолжается. Для возобновления используйте /resume.")

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Возобновляет поиск новых сигналов."""
    is_running = ctx.bot_data.get('bot_on', False)
    if not is_running:
        await update.message.reply_text("ℹ️ Сканер не запущен.")
        return

    ctx.bot_data["scan_paused"] = False
    log.info("Команда /resume: поиск новых сигналов возобновлен.")
    await update.message.reply_text("▶️ <b>Поиск новых сигналов возобновлён.</b>")
    
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показывает текущий статус бота и параметры стратегии."""
    bot_data = ctx.bot_data
    is_running = not (bot_data.get('main_loop_task') is None or bot_data['main_loop_task'].done())
    is_paused = bot_data.get("scan_paused", False)
    active_trades = bot_data.get('active_trades', [])
    
    cfg = scanner_engine.CONFIG
    
    scanner_status = "🔌 ОСТАНОВЛЕН"
    if is_running:
        scanner_status = "⏸️ НА ПАУЗЕ" if is_paused else "⚡️ РАБОТАЕТ"

    # ИЗМЕНЕНО: Отображаем новые параметры
    score_threshold = bot_data.get('score_threshold', cfg.SCORE_BASE)
    params_msg = (
        f"• <b>Стратегия:</b> Wick-Spike (Scoring v2)\n"
        f"• <b>SL/TP:</b> {cfg.SL_PCT:.2f}% / {cfg.TP_PCT:.2f}%\n"
        f"• <b>Адаптивный порог:</b> {score_threshold:.2f} (диапазон: {cfg.SCORE_MIN}-{cfg.SCORE_MAX})\n"
        f"• <b>Цель за скан:</b> {cfg.TARGET_SIGNALS_PER_SCAN} сигнал(а)\n"
    )

    msg = (
        f"<b>Состояние бота v{BOT_VERSION}</b>\n\n"
        f"<b>Статус сканера:</b> {scanner_status}\n"
        f"<b>Активных сделок:</b> {len(active_trades)} / {cfg.MAX_CONCURRENT_POSITIONS}\n\n"
        f"<b><u>Параметры стратегии:</u></b>\n"
        f"{params_msg}"
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
    
    log.info(f"Bot v{BOT_VERSION} starting...")
    app.run_polling()
