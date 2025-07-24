# main.py
import os
import asyncio
import json
import logging
from telegram import Update, constants
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials

log = logging.getLogger("bot")
import scanner_engine
import trade_executor

# --- Конфигурация ---
BOT_VERSION = "ML-2.0-Pro"
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

def setup_sheets():
    if not SHEET_ID or not GOOGLE_CREDENTIALS:
        log.warning("Логирование в Google Sheets отключено.")
        return
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gs = gspread.authorize(creds)
        ss = gs.open_by_key(SHEET_ID)
        
        sheet_name = "ML_Trading_Log_v2"
        try:
            worksheet = ss.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            log.info(f"Лист '{sheet_name}' не найден. Создаю новый.")
            headers = [
                "Signal_ID", "Timestamp_UTC", "Pair", "Algorithm_Type", "side", "Probability", 
                "Entry_Price", "Exit_Price", "SL_Price", "TP_Price", 
                "Status", "Exit_Time_UTC", "PNL_USD", "PNL_Percent",
                "RSI_14", "STOCHk_14_3_3", "EMA_50", "EMA_200", "close", "volume"
            ]
            worksheet = ss.add_worksheet(title=sheet_name, rows="2000", cols=len(headers))
            worksheet.update(range_name="A1", values=[headers])
            worksheet.format(f"A1:{chr(ord('A')+len(headers)-1)}1", {"textFormat": {"bold": True}})
        
        trade_executor.TRADE_LOG_WS = worksheet
        log.info(f"Google-Sheets ready. Logging to '{sheet_name}'.")
    except Exception as e:
        log.error(f"Ошибка инициализации Google Sheets: {e}")

async def broadcast(app: Application, txt: str):
    for cid in getattr(app, "chat_ids", []):
        try:
            await app.bot.send_message(chat_id=cid, text=txt, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.error(f"Send fail to {cid}: {e}")

async def cmd_deposit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(ctx.args[0])
        ctx.bot_data['deposit'] = amount
        await update.message.reply_text(f"✅ Депозит для расчета PNL установлен: <b>${amount}</b>", parse_mode=constants.ParseMode.HTML)
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат. Используйте: /deposit <сумма>")

async def cmd_leverage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        leverage = int(ctx.args[0])
        ctx.bot_data['leverage'] = leverage
        await update.message.reply_text(f"✅ Плечо для расчета PNL установлено: <b>x{leverage}</b>", parse_mode=constants.ParseMode.HTML)
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат. Используйте: /leverage <число>")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.application.chat_ids.add(update.effective_chat.id)
    ctx.bot_data["bot_on"] = True
    ctx.bot_data.setdefault('deposit', 50)
    ctx.bot_data.setdefault('leverage', 100)
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
    bot_data = ctx.bot_data
    is_running = hasattr(ctx.application, '_main_loop_task') and not ctx.application._main_loop_task.done()
    active_signals = bot_data.get('monitored_signals', [])
    msg = (f"<b>Состояние бота v{BOT_VERSION}</b>\n"
           f"<b>Основной цикл:</b> {'⚡️ RUNNING' if is_running else '🔌 STOPPED'}\n"
           f"<b>Активных сделок:</b> {len(active_signals)}\n"
           f"<b>Депозит:</b> ${bot_data.get('deposit', 50)}\n"
           f"<b>Плечо:</b> x{bot_data.get('leverage', 100)}\n")
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)

if __name__ == "__main__":
    setup_sheets()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = set()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("deposit", cmd_deposit))
    app.add_handler(CommandHandler("leverage", cmd_leverage))
    log.info(f"Bot v{BOT_VERSION} started polling.")
    app.run_polling()
