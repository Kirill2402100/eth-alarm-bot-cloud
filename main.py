#!/usr/bin/env python3
# ============================================================================
# v8.0.0 - Pure Quant Model (No LLM)
# Changelog 17-Jul-2025 (Europe/Belgrade):
# • Полный отказ от LLM в пользу 100% алгоритмической модели.
# • Удалена функция ask_llm и все связанные с ней зависимости.
# ============================================================================

import os
import asyncio
import json
import logging
from telegram import Update, constants
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- Импортируем наши модули ---
import trade_executor
from scanner_engine import scanner_main_loop

# === Конфигурация =========================================================
BOT_VERSION       = "8.0.0 (Pure Quant)"
BOT_TOKEN         = os.getenv("BOT_TOKEN")
CHAT_IDS          = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
SHEET_ID          = os.getenv("SHEET_ID")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# === Google-Sheets =========================================================
TRADE_LOG_WS = None
SHEET_NAME   = "BTC_Strategy_Log_v1"
HEADERS = [
    "Signal_ID", "Timestamp_UTC", "Pair", "Confidence_Score", "Algorithm_Type", 
    "Strategy_Idea", "LLM_Reason", "Entry_Price", "SL_Price", "TP_Price",
    "Status", "Exit_Time_UTC", "Exit_Price", "Entry_ATR", "PNL_USD", "PNL_Percent"
]

def setup_sheets():
    global TRADE_LOG_WS
    if not SHEET_ID: return
    try:
        scope = ["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gs = gspread.authorize(creds)
        ss = gs.open_by_key(SHEET_ID)
        try:
            ws = ss.worksheet(SHEET_NAME)
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title=SHEET_NAME, rows="1000", cols=len(HEADERS))
        if ws.row_values(1) != HEADERS:
            ws.clear()
            ws.update("A1",[HEADERS])
            ws.format(f"A1:{chr(ord('A')+len(HEADERS)-1)}1",{"textFormat":{"bold":True}})
        TRADE_LOG_WS = ws
        log.info("Google-Sheets ready – logging to '%s'.", SHEET_NAME)
    except Exception as e:
        log.error("Sheets init failed: %s", e)

# === Состояние ================================================================
STATE_FILE = "bot_state.json"
state = {}
def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
        except json.JSONDecodeError: state = {}
    state.setdefault("bot_on", False)
    state.setdefault("monitored_signals", [])
    log.info("State loaded. Active signals: %d", len(state.get("monitored_signals", [])))

def save_state():
    with open(STATE_FILE,"w") as f:
        json.dump(state, f, indent=2)

# === Вспомогательные функции ============================================
async def broadcast(app, txt:str):
    for cid in getattr(app,"chat_ids", CHAT_IDS):
        try:
            await app.bot.send_message(chat_id=cid, text=txt, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.error("Send fail %s: %s", cid, e)

# === Команды Telegram ============================================
async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid not in ctx.application.chat_ids:
        ctx.application.chat_ids.add(cid)
    state["bot_on"] = True
    save_state()
    await update.message.reply_text(f"✅ <b>Бот v{BOT_VERSION} запущен.</b>\n"
                                      "Используйте /run для запуска основного цикла.", parse_mode=constants.ParseMode.HTML)

async def cmd_stop(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    state["bot_on"] = False
    save_state()
    await update.message.reply_text("🛑 <b>Бот остановлен.</b> Основной цикл будет остановлен.", parse_mode=constants.ParseMode.HTML)
    if hasattr(ctx.application, '_main_loop_task'):
        ctx.application._main_loop_task.cancel()

async def cmd_status(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    is_running = hasattr(update.application, '_main_loop_task') and not update.application._main_loop_task.done()
    msg = (f"<b>Состояние бота:</b> {'✅ ON' if state.get('bot_on') else '🛑 OFF'}\n"
           f"<b>Основной цикл:</b> {'🚀 RUNNING' if is_running else '🔌 STOPPED'}\n"
           f"<b>Активных сделок:</b> {len(state.get('monitored_signals', []))}")
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)

async def cmd_run(update: Update, ctx:ContextTypes.DEFAULT_TYPE):
    app = ctx.application
    is_running = hasattr(app, '_main_loop_task') and not app._main_loop_task.done()

    if is_running:
        await update.message.reply_text("ℹ️ Основной цикл уже запущен.")
    else:
        if not state.get("bot_on", False):
            state["bot_on"] = True
        await update.message.reply_text("🚀 Запускаю основной цикл (100% алгоритмический сканер)...")
        app._main_loop_task = asyncio.create_task(scanner_main_loop(app, broadcast, TRADE_LOG_WS, state, save_state))

if __name__ == "__main__":
    load_state()
    setup_sheets()
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = set(CHAT_IDS)
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("run", cmd_run))
    
    log.info(f"Bot v{BOT_VERSION} started polling.")
    app.run_polling()
