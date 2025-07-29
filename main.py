import os
import asyncio
import logging
import json
from telegram import Update, constants
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, PicklePersistence
import gspread
from oauth2client.service_account import ServiceAccountCredentials

import scanner_engine
import trade_executor

# --- Конфигурация ---
BOT_VERSION = "EMACross-1.1" 
BOT_TOKEN = os.getenv("BOT_TOKEN")
# Переменные для Google Sheets
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")

log = logging.getLogger("bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

def setup_sheets():
    """Настраивает доступ к Google Sheets и находит/создает рабочий лист."""
    if not SHEET_ID or not GOOGLE_CREDENTIALS:
        log.warning("Переменные для Google Sheets не найдены. Логирование отключено.")
        return
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gs = gspread.authorize(creds)
        ss = gs.open_by_key(SHEET_ID)

        trade_sheet_name = "Trading_Log"
        try:
            worksheet = ss.worksheet(trade_sheet_name)
        except gspread.WorksheetNotFound:
            log.info(f"Лист '{trade_sheet_name}' не найден. Создаю новый.")
            headers = [
                "Signal_ID", "Timestamp_UTC", "Pair", "Side", "Status", 
                "Entry_Price", "Exit_Price", "Exit_Time_UTC", "Exit_Reason", 
                "PNL_USD", "PNL_Percent", "TSL_History"
            ]
            worksheet = ss.add_worksheet(title=trade_sheet_name, rows="2000", cols=len(headers))
            worksheet.append_row(headers)
            worksheet.format("A1:L1", {"textFormat": {"bold": True}})
        
        # Передаем объект листа в trade_executor
        trade_executor.TRADE_LOG_WS = worksheet
        log.info(f"Google Sheets готов. Логирование в лист '{trade_sheet_name}'.")

    except Exception as e:
        log.error(f"Ошибка инициализации Google Sheets: {e}", exc_info=True)


async def post_init(app: Application):
    log.info("Бот запущен. Проверяем, нужно ли запускать основной цикл...")
    if app.bot_data.get('run_loop_on_startup', False):
        log.info("Обнаружен флаг 'run_loop_on_startup'. Запускаю основной цикл.")
        asyncio.create_task(scanner_engine.scanner_main_loop(app, broadcast))
    
    await app.bot.set_my_commands([
        ('start', 'Запустить/перезапустить бота'),
        ('run', 'Запустить сканер'),
        ('stop', 'Остановить сканер'),
        ('status', 'Показать текущий статус'),
        ('deposit', 'Установить депозит'),
        ('leverage', 'Установить плечо')
    ])

async def broadcast(app: Application, txt: str):
    chat_ids = app.bot_data.get('chat_ids', [])
    for cid in chat_ids:
        try:
            await app.bot.send_message(chat_id=cid, text=txt, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.error(f"Send fail to {cid}: {e}")

# ... (остальные команды без изменений) ...
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ctx.bot_data.setdefault('chat_ids', set()).add(chat_id)
    ctx.bot_data.setdefault('deposit', 50)
    ctx.bot_data.setdefault('leverage', 100)
    log.info(f"Пользователь {chat_id} запустил бота.")
    await update.message.reply_text(f"✅ <b>Бот v{BOT_VERSION} запущен.</b>\nИспользуйте /run для запуска сканера.", parse_mode=constants.ParseMode.HTML)

async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    app = ctx.application
    is_running = not (app.bot_data.get('main_loop_task') is None or app.bot_data['main_loop_task'].done())
    if is_running:
        await update.message.reply_text("ℹ️ Сканер уже запущен. Для остановки используйте /stop.")
        return
    
    app.bot_data['bot_on'] = True
    app.bot_data['run_loop_on_startup'] = True
    log.info("Команда /run: запускаем основной цикл.")
    await update.message.reply_text(f"🚀 <b>Запускаю сканер...</b>")
    task = asyncio.create_task(scanner_engine.scanner_main_loop(app, broadcast))
    app.bot_data['main_loop_task'] = task

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
    
async def cmd_status(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    bot_data = ctx.bot_data
    is_running = not (bot_data.get('main_loop_task') is None or bot_data['main_loop_task'].done())
    active_signals = bot_data.get('monitored_signals', [])
    msg = (f"<b>Состояние бота v{BOT_VERSION}</b>\n"
           f"<b>Основной цикл:</b> {'⚡️ RUNNING' if is_running else '🔌 STOPPED'}\n"
           f"<b>Активных сделок:</b> {len(active_signals)}\n"
           f"<b>Депозит:</b> ${bot_data.get('deposit', 50)}\n"
           f"<b>Плечо:</b> x{bot_data.get('leverage', 100)}\n")
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)

async def cmd_deposit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(ctx.args[0])
        ctx.bot_data['deposit'] = amount
        await update.message.reply_text(f"✅ Депозит для расчета PNL установлен: <b>${amount}</b>", parse_mode=constants.ParseMode.HTML)
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат. Используйте /deposit <сумма>")

async def cmd_leverage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        leverage = int(ctx.args[0])
        ctx.bot_data['leverage'] = leverage
        await update.message.reply_text(f"✅ Плечо для расчета PNL установлено: <b>x{leverage}</b>", parse_mode=constants.ParseMode.HTML)
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат. Используйте /leverage <число>")


if __name__ == "__main__":
    setup_sheets() # Вызываем настройку таблиц при старте
    persistence = PicklePersistence(filepath="bot_persistence")
    app = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("deposit", cmd_deposit))
    app.add_handler(CommandHandler("leverage", cmd_leverage))
    
    log.info(f"Bot v{BOT_VERSION} starting...")
    app.run_polling()
