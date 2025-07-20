# main_bot.py
# ============================================================================
# v28.0 - СТАБИЛЬНАЯ ВЕРСИЯ
# Архитектура переведена на встроенный механизм состояния `app.bot_data`
# для максимальной надежности в асинхронной среде.
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
import trade_executor
from scanner_engine import scanner_main_loop

# === Конфигурация =========================================================
BOT_VERSION        = "28.0"
BOT_TOKEN          = os.getenv("BOT_TOKEN")
CHAT_IDS           = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
SHEET_ID           = os.getenv("SHEET_ID")
STATE_FILE         = "bot_state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# === Google-Sheets =========================================================
TRADE_LOG_WS = None
SHEET_NAME   = f"Trading_Log_v27.2" # Оставим старое имя, чтобы продолжить лог

HEADERS = [
    "Signal_ID", "Timestamp_UTC", "Pair", "Algorithm_Type", "Strategy_Idea",
    "Entry_Price", "SL_Price", "TP_Price", "side", "Deposit", "Leverage",
    "Status", "Exit_Time_UTC", "Exit_Price", "PNL_USD", "PNL_Percent",
    "Trigger_Order_USD", "Exit_Reason"
]

def setup_sheets():
    global TRADE_LOG_WS
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
    except Exception as e:
        log.error("Sheets init failed: %s", e)
        TRADE_LOG_WS = None
        trade_executor.TRADE_LOG_WS = None


# === Управление состоянием (Новая архитектура) ============================
def load_state(app: Application):
    """Загружает состояние из файла в app.bot_data."""
    bot_data = app.bot_data
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                loaded_state = json.load(f)
                bot_data.update(loaded_state)
        except json.JSONDecodeError:
            log.error("Ошибка чтения bot_state.json, использую значения по умолчанию.")

    # Устанавливаем значения по умолчанию, если их нет
    bot_data.setdefault("bot_on", False)
    bot_data.setdefault("monitored_signals", [])
    bot_data.setdefault("deposit", 50)
    bot_data.setdefault("leverage", 100)
    log.info("State loaded into bot_data. Active signals: %d. Deposit: %s, Leverage: %s",
             len(bot_data.get("monitored_signals", [])), bot_data.get('deposit'), bot_data.get('leverage'))

def save_state(app: Application):
    """Сохраняет app.bot_data в файл."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(app.bot_data, f, indent=2)
    except Exception as e:
        log.error(f"Failed to save state: {e}")


# === Общие функции =========================================================
async def broadcast(app: Application, txt:str):
    for cid in getattr(app,"chat_ids", CHAT_IDS):
        try:
            await app.bot.send_message(chat_id=cid, text=txt, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.error("Send fail %s: %s", cid, e)

# === Команды Telegram (Работают через bot_data) ============================
async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    ctx.application.chat_ids.add(update.effective_chat.id)
    ctx.bot_data["bot_on"] = True
    save_state(ctx.application)
    await update.message.reply_text(f"✅ <b>Бот v{BOT_VERSION} запущен.</b>\n"
                                      f"<b>Стратегия:</b> Агрессия + Дисбаланс\n"
                                      f"Логирование в лист: <b>{SHEET_NAME}</b>\n"
                                      "Используйте /run для запуска и /status для статуса.",
                                      parse_mode=constants.ParseMode.HTML)

async def cmd_stop(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    ctx.bot_data["bot_on"] = False
    save_state(ctx.application)
    await update.message.reply_text("🛑 <b>Бот остановлен.</b> Основной цикл завершится после текущей итерации.",
                                      parse_mode=constants.ParseMode.HTML)
    if hasattr(ctx.application, '_main_loop_task'):
        # Мягкая остановка - цикл сам завершится по флагу bot_on = False
        # Принудительная отмена: ctx.application._main_loop_task.cancel()
        pass

async def cmd_status(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    bot_data = ctx.bot_data
    is_running = hasattr(ctx.application, '_main_loop_task') and not ctx.application._main_loop_task.done()
    active_signals = bot_data.get('monitored_signals', [])

    msg = (f"<b>Состояние бота v{BOT_VERSION}</b>\n"
           f"<b>Стратегия:</b> Агрессия + Дисбаланс\n"
           f"<b>Статус:</b> {'✅ ON' if bot_data.get('bot_on') else '🛑 OFF'}\n"
           f"<b>Основной цикл:</b> {'🚀 RUNNING' if is_running else '🔌 STOPPED'}\n"
           f"<b>Активных сделок:</b> {len(active_signals)}\n"
           f"<b>Депозит:</b> ${bot_data.get('deposit', 50)}\n"
           f"<b>Плечо:</b> x{bot_data.get('leverage', 100)}\n\n")

    if active_signals:
        signal = active_signals[0]
        msg += (f"<b>Активная сделка:</b> <code>{signal.get('Pair')} {signal.get('side')}</code>\n"
                f"<b>Вход:</b> {signal.get('Entry_Price')}\n"
                f"<b>SL:</b> {signal.get('SL_Price')}\n"
                f"<b>Текущий дисбаланс:</b> {signal.get('current_imbalance_ratio', 'N/A'):.1f}x\n")

    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)

async def cmd_deposit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        new_deposit = float(ctx.args[0])
        if new_deposit <= 0:
            await update.message.reply_text("❌ Депозит должен быть положительным числом.")
            return
        ctx.bot_data['deposit'] = new_deposit
        save_state(ctx.application)
        await update.message.reply_text(f"✅ Депозит установлен: <b>${new_deposit}</b>", parse_mode=constants.ParseMode.HTML)
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат. Используйте: <code>/deposit &lt;сумма&gt;</code>", parse_mode=constants.ParseMode.HTML)

async def cmd_leverage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        new_leverage = int(ctx.args[0])
        if not 1 <= new_leverage <= 200:
            await update.message.reply_text("❌ Плечо должно быть целым числом от 1 до 200.")
            return
        ctx.bot_data['leverage'] = new_leverage
        save_state(ctx.application)
        await update.message.reply_text(f"✅ Плечо установлено: <b>x{new_leverage}</b>", parse_mode=constants.ParseMode.HTML)
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат. Используйте: <code>/leverage &lt;число&gt;</code>", parse_mode=constants.ParseMode.HTML)

async def cmd_run(update: Update, ctx:ContextTypes.DEFAULT_TYPE):
    app = ctx.application
    if hasattr(app, '_main_loop_task') and not app._main_loop_task.done():
        await update.message.reply_text("ℹ️ Основной цикл уже запущен.")
    else:
        app.bot_data["bot_on"] = True
        save_state(app)
        await update.message.reply_text(f"🚀 Запускаю основной цикл (v{BOT_VERSION})...")
        app._main_loop_task = asyncio.create_task(scanner_main_loop(app, broadcast))

# === Точка входа =========================================================
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = set(CHAT_IDS)

    # Сначала инициализируем bot_data, потом загружаем состояние из файла
    load_state(app)
    setup_sheets()

    # Регистрация команд
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("deposit", cmd_deposit))
    app.add_handler(CommandHandler("leverage", cmd_leverage))

    log.info(f"Bot v{BOT_VERSION} started polling.")
    app.run_polling()
