#!/usr/bin/env python3
# ============================================================================
# v4.5.0 - P&L Simulation and Portfolio Logic
# Changelog 15-Jul-2025 (Europe/Belgrade):
# • Added P&L calculation for simulated trades ($50 size, 100x lev).
# • Scanner now prevents opening trades on pairs already in the portfolio.
# ============================================================================

import os, asyncio, json, logging, uuid
from datetime import datetime, timezone, timedelta

import pandas as pd
import pandas_ta as ta
import aiohttp, gspread, ccxt.async_support as ccxt
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, constants
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

# --- Импортируем наши модули ---
import data_feeder
import trade_executor
from scanner_engine import scanner_main_loop
from trade_monitor import monitor_main_loop, init_monitor

# === ENV / Logging =========================================================
BOT_VERSION               = "4.5.0"
BOT_TOKEN                 = os.getenv("BOT_TOKEN")
CHAT_IDS                  = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
SHEET_ID                  = os.getenv("SHEET_ID")
MAX_PORTFOLIO_SIZE        = 10 # Максимальное количество одновременных сделок

LLM_API_KEY  = os.getenv("LLM_API_KEY")
LLM_API_URL  = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_MODEL_ID = os.getenv("LLM_MODEL_ID", "gpt-4o-mini")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for n in ("httpx", "httpcore", "gspread"):
    logging.getLogger(n).setLevel(logging.WARNING)

# === Google‑Sheets =========================================================
TRADE_LOG_WS = None
SHEET_NAME   = "Microstructure_Log_v2" # Обновили имя для новой структуры
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
        gs = gspread.authorize(ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope))
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
        log.info("Google‑Sheets ready – logging to '%s'.", SHEET_NAME)
    except Exception as e:
        log.error("Sheets init failed: %s", e)

# === State ================================================================
STATE_FILE = "bot_state_v6_2.json"
state = {}
def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
        except json.JSONDecodeError:
            state = {}
    state.setdefault("bot_on", False)
    state.setdefault("monitored_signals", [])
    log.info("State loaded. Active signals: %d", len(state["monitored_signals"]))

def save_state():
    with open(STATE_FILE,"w") as f:
        json.dump(state, f, indent=2)

# === LLM & Broadcast Functions ============================================
async def broadcast(app, txt:str):
    for cid in getattr(app,"chat_ids", CHAT_IDS):
        try:
            await app.bot.send_message(chat_id=cid, text=txt, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.error("Send fail %s: %s", cid, e)

async def ask_llm(prompt: str):
    if not LLM_API_KEY: return None
    payload = { "model": LLM_MODEL_ID, "messages":[{"role":"user","content":prompt}], "temperature":0.4, "response_format":{"type":"json_object"} }
    hdrs = {"Authorization":f"Bearer {LLM_API_KEY}","Content-Type":"application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, json=payload, headers=hdrs, timeout=180) as r:
                r.raise_for_status()
                data = await r.json()
                return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.error("LLM API request failed: %s", e, exc_info=True)
        return None

# === КОМАНДЫ TELEGRAM ===

async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid not in ctx.application.chat_ids:
        ctx.application.chat_ids.add(cid)
    state["bot_on"] = True
    save_state()
    await update.message.reply_text(f"✅ <b>Бот v{BOT_VERSION} (P&L Simulator) запущен.</b>\n"
                                    "Используйте /feed для запуска всех модулей.")

async def cmd_stop(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    state["bot_on"] = False
    data_feeder.stop_data_feed()
    save_state()
    await update.message.reply_text("🛑 <b>Бот остановлен.</b> Все задачи остановлены.")
    if hasattr(ctx.application, '_feed_task'): ctx.application._feed_task.cancel()
    if hasattr(ctx.application, '_scanner_task'): ctx.application._scanner_task.cancel()
    if hasattr(ctx.application, '_monitor_task'): ctx.application._monitor_task.cancel()

async def cmd_status(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    is_feed_running = hasattr(update.application, '_feed_task') and not update.application._feed_task.done()
    is_scanner_running = hasattr(update.application, '_scanner_task') and not update.application._scanner_task.done()
    is_monitor_running = hasattr(update.application, '_monitor_task') and not update.application._monitor_task.done()
    msg = (f"<b>Состояние бота:</b> {'✅ ON' if state.get('bot_on') else '🛑 OFF'}\n"
           f"<b>Поток данных:</b> {'🛰️ ACTIVE' if is_feed_running else '🔌 OFF'}\n"
           f"<b>Сканер:</b> {'🧠 ACTIVE' if is_scanner_running else '🔌 OFF'}\n"
           f"<b>Монитор:</b> {'📈 ACTIVE' if is_monitor_running else '🔌 OFF'}\n"
           f"<b>Активных сделок:</b> {len(state.get('monitored_signals', []))}/{MAX_PORTFOLIO_SIZE}")
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)

async def cmd_feed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    app = ctx.application
    is_running = hasattr(app, '_feed_task') and not app._feed_task.done()
    if is_running:
        data_feeder.stop_data_feed()
        await update.message.reply_text("🛑 Команда на остановку всех задач...")
        await asyncio.sleep(2) 
        if hasattr(app, '_feed_task'): app._feed_task.cancel()
        if hasattr(app, '_scanner_task'): app._scanner_task.cancel()
        if hasattr(app, '_monitor_task'): app._monitor_task.cancel()
        await update.message.reply_text("Все фоновые задачи остановлены.")
    else:
        await update.message.reply_text("🛰️ Запускаю все модули: Поток данных, Сканер и Монитор...")
        app._feed_task = asyncio.create_task(data_feeder.data_feed_main_loop(app, app.chat_ids))
        app._scanner_task = asyncio.create_task(scanner_main_loop(app, ask_llm, broadcast, TRADE_LOG_WS, state))
        app._monitor_task = asyncio.create_task(monitor_main_loop(app))
        await update.message.reply_text("✅ Все модули запущены.")

async def post_init(app: Application):
    log.info("Bot application initialized.")

if __name__ == "__main__":
    load_state()
    setup_sheets()

    trade_executor.set_state_object(state)
    trade_executor.save_state_func = save_state
    init_monitor(state, save_state, broadcast, TRADE_LOG_WS)
    
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.chat_ids = set(CHAT_IDS)
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("feed", cmd_feed))
    
    log.info(f"Bot v{BOT_VERSION} (P&L Simulator) started polling.")
    app.run_polling()
