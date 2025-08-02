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
BOT_VERSION = "SwingBot-1.3"
BOT_TOKEN = os.getenv("BOT_TOKEN")
# Переменные для Google Sheets
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")

log = logging.getLogger("bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("gspread").setLevel(logging.WARNING)


def setup_sheets():
    """Настраивает доступ к Google Sheets и находит/создает оба рабочих листа."""
    if not SHEET_ID or not GOOGLE_CREDENTIALS:
        log.warning("Переменные для Google Sheets не найдены. Логирование отключено.")
        return
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gs = gspread.authorize(creds)
        ss = gs.open_by_key(SHEET_ID)

        # --- Настройка листа 'Trading_Log' ---
        trade_sheet_name = "Trading_Log"
        headers_trade = ["Signal_ID", "Timestamp_UTC", "Pair", "Side", "Status", "Entry_Price", "Exit_Price", "Exit_Time_UTC", "Exit_Reason", "PNL_USD", "PNL_Percent"]
        try:
            worksheet_trade = ss.worksheet(trade_sheet_name)
            if worksheet_trade.row_values(1) != headers_trade:
                worksheet_trade.clear()
                worksheet_trade.append_row(headers_trade)
                worksheet_trade.format("A1:K1", {"textFormat": {"bold": True}})
                log.info(f"Headers for '{trade_sheet_name}' have been updated.")
        except gspread.WorksheetNotFound:
            log.info(f"Лист '{trade_sheet_name}' не найден. Создаю новый.")
            worksheet_trade = ss.add_worksheet(title=trade_sheet_name, rows="2000", cols=len(headers_trade))
            worksheet_trade.append_row(headers_trade)
            worksheet_trade.format("A1:K1", {"textFormat": {"bold": True}})
        
        trade_executor.TRADE_LOG_WS = worksheet_trade
        log.info(f"Google Sheets готов. Логирование сделок в лист '{trade_sheet_name}'.")

        # --- Настройка листа 'Diagnostic_Log' ---
        diag_sheet_name = "Diagnostic_Log"
        headers_diag = ["Timestamp_UTC", "Pair", "Side", "EMA_State_OK", "Trend_OK", "Stoch_OK", "Reason_For_Fail"]
        try:
            worksheet_diag = ss.worksheet(diag_sheet_name)
        except gspread.WorksheetNotFound:
            log.info(f"Лист '{diag_sheet_name}' не найден. Создаю новый.")
            worksheet_diag = ss.add_worksheet(title=diag_sheet_name, rows="5000", cols=len(headers_diag))
            worksheet_diag.append_row(headers_diag)
            worksheet_diag.format("A1:G1", {"textFormat": {"bold": True}})
            
        trade_executor.DIAGNOSTIC_LOG_WS = worksheet_diag
        log.info(f"Google Sheets готов. Диагностическое логирование в лист '{diag_sheet_name}'.")

    except Exception as e:
        log.error(f"Ошибка инициализации Google Sheets: {e}", exc_info=True)


async def post_init(app: Application):
    log.info("Бот запущен. Проверяем, нужно ли запускать основной цикл...")
    if app.bot_data.get('run_loop_on_startup', False):
        log.info("Обнаружен флаг 'run_loop_on_startup'. Запускаю основной цикл.")
        asyncio.create_task(scanner_engine.scanner_main_loop(app, broadcast))
    
    # ИСПРАВЛЕНО: Убраны неиспользуемые команды
    await app.bot.set_my_commands([
        ('start', 'Запустить/перезапустить бота'),
        ('run', 'Запустить сканер'),
        ('stop', 'Остановить сканер'),
        ('status', 'Показать текущий статус'),
    ])

async def broadcast(app: Application, txt: str):
    chat_ids = app.bot_data.get('chat_ids', set())
    for cid in chat_ids:
        try:
            await app.bot.send_message(chat_id=cid, text=txt, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.error(f"Send fail to {cid}: {e}")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ctx.bot_data.setdefault('chat_ids', set()).add(chat_id)
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
    active_trades = bot_data.get('active_trades', [])
    
    # ИСПРАВЛЕНО: Отображение актуальных параметров ATR-стратегии
    cfg = scanner_engine.CONFIG
    
    msg = (f"<b>Состояние бота v{BOT_VERSION}</b>\n\n"
           f"<b>Основной цикл:</b> {'⚡️ RUNNING' if is_running else '🔌 STOPPED'}\n"
           f"<b>Активных сделок:</b> {len(active_trades)} / {cfg.MAX_CONCURRENT_POSITIONS}\n\n"
           f"<b><u>Параметры стратегии:</u></b>\n"
           f"<b>Таймфрейм:</b> {cfg.TIMEFRAME}\n"
           f"<b>Плечо:</b> x{cfg.LEVERAGE}\n"
           f"<b>SL:</b> {cfg.ATR_SL_MULT} × ATR (мин {cfg.SL_MIN_PCT}%, макс {cfg.SL_MAX_PCT}%)\n"
           f"<b>RR:</b> 1:{cfg.RISK_REWARD}")
           
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)


if __name__ == "__main__":
    setup_sheets()
    persistence = PicklePersistence(filepath="bot_persistence")
    app = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    
    log.info(f"Bot v{BOT_VERSION} starting...")
    app.run_polling()
