# main.py
import os
import asyncio
import logging
import json
from telegram import Update, constants
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials

log = logging.getLogger("bot")
import scanner_engine
import trade_executor # <-- Импортируем новый файл

# --- Конфигурация ---
BOT_VERSION = "ML-1.1-Sheets"
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
SHEET_ID = os.getenv("SHEET_ID") # <-- Переменная для ID таблицы
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS") # <-- Переменная для ключей

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- Инициализация Google Sheets ---
def setup_sheets():
    if not SHEET_ID or not GOOGLE_CREDENTIALS:
        log.warning("SHEET_ID или GOOGLE_CREDENTIALS не заданы. Логирование в Google Sheets отключено.")
        return
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gs = gspread.authorize(creds)
        ss = gs.open_by_key(SHEET_ID)
        
        sheet_name = "ML_Trading_Log"
        try:
            worksheet = ss.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            log.info(f"Лист '{sheet_name}' не найден. Создаю новый.")
            headers = [
                "Signal_ID", "Timestamp_UTC", "Pair", "Algorithm_Type", "Strategy_Idea",
                "Entry_Price", "SL_Price", "TP_Price", "side", "Probability", "Status"
            ]
            worksheet = ss.add_worksheet(title=sheet_name, rows="1000", cols=len(headers))
            worksheet.update(range_name="A1", values=[headers])
            worksheet.format(f"A1:{chr(ord('A')+len(headers)-1)}1", {"textFormat": {"bold": True}})
        
        trade_executor.TRADE_LOG_WS = worksheet # <-- Передаем лист в наш executor
        log.info(f"Google-Sheets ready. Logging to '{sheet_name}'.")

    except Exception as e:
        log.error(f"Ошибка инициализации Google Sheets: {e}")

# --- Команды и запуск бота (остаются без изменений) ---
async def broadcast(app: Application, txt: str):
    for cid in getattr(app, "chat_ids", CHAT_IDS):
        try:
            await app.bot.send_message(chat_id=cid, text=txt, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.error(f"Send fail to {cid}: {e}")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.application.chat_ids.add(update.effective_chat.id)
    ctx.bot_data["bot_on"] = True
    await update.message.reply_text(f"✅ <b>Бот v{BOT_VERSION} запущен.</b>\nИспользуйте /run для запуска.", parse_mode=constants.ParseMode.HTML)

async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    app = ctx.application
    if hasattr(app, '_main_loop_task') and not app._main_loop_task.done():
        await update.message.reply_text("ℹ️ Основной цикл уже запущен.")
    else:
        app.bot_data["bot_on"] = True
        await update.message.reply_text(f"🚀 Запускаю ML-сканер (v{BOT_VERSION})...")
        app._main_loop_task = asyncio.create_task(scanner_engine.scanner_main_loop(app, broadcast))

async def cmd_status(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    is_running = hasattr(ctx.application, '_main_loop_task') and not ctx.application._main_loop_task.done()
    msg = (f"<b>Состояние бота v{BOT_VERSION}</b>\n"
           f"<b>Основной цикл:</b> {'⚡️ RUNNING' if is_running else '🔌 STOPPED'}\n"
           f"<b>Live-логи:</b> {'✅ ВКЛ' if ctx.bot_data.get('live_info_on') else '❌ ВЫКЛ'}\n")
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)

async def cmd_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    current_state = ctx.bot_data.get("live_info_on", False)
    new_state = not current_state
    ctx.bot_data["live_info_on"] = new_state
    msg = "✅ <b>Live-логирование включено.</b>" if new_state else "❌ <b>Live-логирование выключено.</b>"
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)

if __name__ == "__main__":
    setup_sheets() # <-- Вызываем инициализацию таблиц при старте
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = set(CHAT_IDS)
    app.bot_version = BOT_VERSION
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("info", cmd_info))
    log.info(f"Bot v{BOT_VERSION} started polling.")
    app.run_polling()
