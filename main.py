# main.py
# ============================================================================
# v39.1 - РЕЖИМ LIVE-ЛОГИРОВАНИЯ
# - Команда /info теперь включает/выключает отправку статуса индикаторов каждые 5с.
# - Старая функция /info (включение диагностики для Sheets) переименована в /debug.
# ============================================================================

import os
import asyncio
import json
import logging
from telegram import Update, constants
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Локальные импорты
log = logging.getLogger("bot")
import trade_executor
from scanner_engine import scanner_main_loop, calculate_indicators, PAIR_TO_SCAN, TIMEFRAME
from state_utils import load_state, save_state

# === Конфигурация =========================================================
BOT_VERSION       = "39.1"
BOT_TOKEN         = os.getenv("BOT_TOKEN")
CHAT_IDS          = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
SHEET_ID          = os.getenv("SHEET_ID")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

# === Google-Sheets =========================================================
TRADE_LOG_WS = None
DEBUG_LOG_WS = None
SHEET_NAME      = f"Trading_Log_v{BOT_VERSION.split('.')[0]}.0" # v39.0
DEBUG_SHEET_NAME = f"Debug_Log_v{BOT_VERSION.split('.')[0]}.0" # v39.0
HEADERS = [
    "Signal_ID", "Timestamp_UTC", "Pair", "Algorithm_Type", "Strategy_Idea",
    "Entry_Price", "SL_Price", "TP_Price", "side", "Deposit", "Leverage",
    "RSI", "Stoch_K", "Stoch_D",
    "Status", "Exit_Time_UTC", "Exit_Price", "PNL_USD", "PNL_Percent",
    "Exit_Reason", "Time_In_Trade", "Intermediate_Triggered"
]
DEBUG_HEADERS = [
    "Timestamp_UTC", "RSI", "Stoch_K", "Stoch_D",
    "Side", "Reason_Prop"
]

def setup_sheets():
    global TRADE_LOG_WS, DEBUG_LOG_WS
    if not SHEET_ID:
        log.warning("SHEET_ID не задан. Логирование в Google Sheets отключено.")
        return
    try:
        scope = ["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gs = gspread.authorize(creds)
        ss = gs.open_by_key(SHEET_ID)
        try:
            TRADE_LOG_WS = ss.worksheet(SHEET_NAME)
        except gspread.WorksheetNotFound:
            log.info(f"Лист '{SHEET_NAME}' не найден. Создаю новый.")
            TRADE_LOG_WS = ss.add_worksheet(title=SHEET_NAME, rows="1000", cols=len(HEADERS))
            TRADE_LOG_WS.update(range_name="A1", values=[HEADERS])
            TRADE_LOG_WS.format(f"A1:{chr(ord('A')+len(HEADERS)-1)}1", {"textFormat":{"bold":True}})
        log.info("Google-Sheets ready. Logging to '%s'.", SHEET_NAME)
        trade_executor.TRADE_LOG_WS = TRADE_LOG_WS
        
        try:
            DEBUG_LOG_WS = ss.worksheet(DEBUG_SHEET_NAME)
        except gspread.WorksheetNotFound:
            log.info(f"Лист '{DEBUG_SHEET_NAME}' не найден. Создаю новый.")
            DEBUG_LOG_WS = ss.add_worksheet(title=DEBUG_SHEET_NAME, rows="10000", cols=len(DEBUG_HEADERS))
            DEBUG_LOG_WS.update(range_name="A1", values=[DEBUG_HEADERS])
            DEBUG_LOG_WS.format(f"A1:{chr(ord('A')+len(DEBUG_HEADERS)-1)}1", {"textFormat":{"bold":True}})
        log.info("Debug-Sheets ready. Logging to '%s'.", DEBUG_SHEET_NAME)
        trade_executor.DEBUG_LOG_WS = DEBUG_LOG_WS
    except Exception as e:
        log.error("Sheets init failed: %s", e)
        TRADE_LOG_WS, trade_executor.TRADE_LOG_WS = None, None
        DEBUG_LOG_WS, trade_executor.DEBUG_LOG_WS = None, None

# === Общие функции =========================================================
async def broadcast(app: Application, txt:str):
    for cid in getattr(app,"chat_ids", CHAT_IDS):
        try:
            await app.bot.send_message(chat_id=cid, text=txt, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.error("Send fail %s: %s", cid, e)

# === Команды Telegram =====================================================
async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    ctx.application.chat_ids.add(update.effective_chat.id)
    ctx.bot_data["bot_on"] = True
    save_state(ctx.application)
    await update.message.reply_text(f"✅ <b>Бот v{BOT_VERSION} запущен.</b>\nИспользуйте /run для запуска основного цикла.", parse_mode=constants.ParseMode.HTML)

async def cmd_stop(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    ctx.bot_data["bot_on"] = False
    save_state(ctx.application)
    await update.message.reply_text("🛑 <b>Бот остановлен.</b> Основной цикл завершится и остановится.", parse_mode=constants.ParseMode.HTML)

async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    app = ctx.application
    if hasattr(app, '_main_loop_task') and not app._main_loop_task.done():
        await update.message.reply_text("ℹ️ Основной цикл уже запущен.")
    else:
        app.bot_data["bot_on"] = True
        save_state(app)
        await update.message.reply_text(f"🚀 Запускаю основной цикл (v{BOT_VERSION})...")
        app._main_loop_task = asyncio.create_task(scanner_main_loop(app, broadcast))

async def cmd_status(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    bot_data = ctx.bot_data
    is_running = hasattr(ctx.application, '_main_loop_task') and not ctx.application._main_loop_task.done()
    active_signals = bot_data.get('monitored_signals', [])
    msg = (f"<b>Состояние бота v{BOT_VERSION}</b>\n"
           f"<b>Статус:</b> {'✅ ON' if bot_data.get('bot_on') else '🛑 OFF'}\n"
           f"<b>Основной цикл:</b> {'⚡️ RUNNING' if is_running else '🔌 STOPPED'}\n"
           f"<b>Live-логи (/info):</b> {'✅ ВКЛ' if bot_data.get('live_info_on') else '❌ ВЫКЛ'}\n"
           f"<b>Диагностика (/debug):</b> {'✅ ВКЛ' if bot_data.get('debug_mode_on') else '❌ ВЫКЛ'}\n"
           f"<b>Активных сделок:</b> {len(active_signals)}\n"
           f"<b>Депозит:</b> ${bot_data.get('deposit', 50)}\n"
           f"<b>Плечо:</b> x{bot_data.get('leverage', 100)}\n")
    if active_signals:
        signal = active_signals[0]
        msg += f"<b>Активная сделка:</b> <code>{signal.get('Pair')} {signal.get('side')}</code>\n"
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)

async def cmd_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Включает/выключает режим live-логирования."""
    current_state = ctx.bot_data.get("live_info_on", False)
    new_state = not current_state
    ctx.bot_data["live_info_on"] = new_state
    save_state(ctx.application)

    if new_state:
        msg = ("✅ <b>Live-логирование ВКЛЮЧЕНО.</b>\n\n"
               "Каждые 5 секунд вы будете получать данные по индикаторам. "
               "Для отключения снова введите /info.")
    else:
        msg = "❌ <b>Live-логирование ВЫКЛЮЧЕНО.</b>"
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)

async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Включает/выключает режим диагностики для Google Sheets."""
    current_state = ctx.bot_data.get("debug_mode_on", False)
    new_state = not current_state
    ctx.bot_data["debug_mode_on"] = new_state
    save_state(ctx.application)
    if new_state:
        await update.message.reply_text("✅ <b>Диагностика для Google Sheets ВКЛЮЧЕНА.</b>", parse_mode=constants.ParseMode.HTML)
    else:
        await update.message.reply_text("❌ <b>Диагностика для Google Sheets ВЫКЛЮЧЕНА.</b>", parse_mode=constants.ParseMode.HTML)

async def cmd_deposit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        new_deposit = float(ctx.args[0])
        ctx.bot_data['deposit'] = new_deposit
        save_state(ctx.application)
        await update.message.reply_text(f"✅ Депозит установлен: <b>${new_deposit}</b>", parse_mode=constants.ParseMode.HTML)
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат. Используйте: /deposit <сумма>", parse_mode=constants.ParseMode.HTML)

async def cmd_leverage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        new_leverage = int(ctx.args[0])
        ctx.bot_data['leverage'] = new_leverage
        save_state(ctx.application)
        await update.message.reply_text(f"✅ Плечо установлено: <b>x{new_leverage}</b>", parse_mode=constants.ParseMode.HTML)
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат. Используйте: /leverage <число>", parse_mode=constants.ParseMode.HTML)

# === Точка входа =========================================================
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = set(CHAT_IDS)
    app.bot_version = BOT_VERSION
    load_state(app)
    setup_sheets()

    # Регистрация команд
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("deposit", cmd_deposit))
    app.add_handler(CommandHandler("leverage", cmd_leverage))

    log.info(f"Bot v{BOT_VERSION} started polling.")
    app.run_polling()
