#!/usr/bin/env python3
# ============================================================================
# v4.3.0 - Trade Simulation Integration
# Changelog 15‑Jul‑2025 (Europe/Belgrade):
# • Integrated trade_executor to log simulated trades to Google Sheets.
# • Updated HEADERS and SHEET_NAME for the new microstructure strategy.
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
from scanner_engine import scanner_main_loop

# === ENV / Logging =========================================================
BOT_VERSION               = "4.3.0"
BOT_TOKEN                 = os.getenv("BOT_TOKEN")
CHAT_IDS                  = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
SHEET_ID                  = os.getenv("SHEET_ID")

LLM_API_KEY  = os.getenv("LLM_API_KEY")
LLM_API_URL  = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_MODEL_ID = os.getenv("LLM_MODEL_ID", "gpt-4o-mini")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for n in ("httpx", "httpcore", "gspread"):
    logging.getLogger(n).setLevel(logging.WARNING)

# === Helper ================================================================
def fmt(price: float | None) -> str:
    if price is None:       return "N/A"
    if price > 10:          return f"{price:,.2f}"
    elif price > 0.1:       return f"{price:.4f}"
    elif price > 0.001:     return f"{price:.6f}"
    else:                   return f"{price:.8f}"

# === Google‑Sheets =========================================================
TRADE_LOG_WS = None
# НОВАЯ СТРУКТУРА ДЛЯ ЛОГИРОВАНИЯ СДЕЛОК
SHEET_NAME   = "Microstructure_Log_v1"
HEADERS = [
    "Signal_ID", "Timestamp_UTC", "Pair", "Confidence_Score", "Algorithm_Type", 
    "Strategy_Idea", "LLM_Reason", "Entry_Price", "SL_Price", "TP_Price"
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
    log.info("State loaded.")

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
    await update.message.reply_text(f"✅ <b>Бот v{BOT_VERSION} (Microstructure) запущен.</b>\n"
                                    "Используйте /feed для запуска потока данных и сканера.")

async def cmd_stop(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    state["bot_on"] = False
    data_feeder.stop_data_feed()
    save_state()
    await update.message.reply_text("🛑 <b>Бот остановлен.</b> Все задачи остановлены.")
    if hasattr(ctx.application, '_feed_task'): ctx.application._feed_task.cancel()
    if hasattr(ctx.application, '_scanner_task'): ctx.application._scanner_task.cancel()


async def cmd_status(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    is_feed_running = hasattr(update.application, '_feed_task') and not update.application._feed_task.done()
    is_scanner_running = hasattr(update.application, '_scanner_task') and not update.application._scanner_task.done()
    
    msg = (f"<b>Состояние бота:</b> {'✅ ON' if state.get('bot_on') else '🛑 OFF'}\n"
           f"<b>Поток данных:</b> {'🛰️ ACTIVE' if is_feed_running else '🔌 OFF'}\n"
           f"<b>Сканер:</b> {'🧠 ACTIVE' if is_scanner_running else '🔌 OFF'}")
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)


async def cmd_feed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Управляет потоком данных WebSocket и сканером."""
    app = ctx.application
    is_feed_task_running = hasattr(app, '_feed_task') and not app._feed_task.done()

    if is_feed_task_running:
        data_feeder.stop_data_feed()
        await update.message.reply_text("🛑 Команда на остановку потока данных и сканера отправлена...")
        
        await asyncio.sleep(2) 
        if hasattr(app, '_feed_task'): app._feed_task.cancel()
        if hasattr(app, '_scanner_task'): app._scanner_task.cancel()
        await update.message.reply_text("Все фоновые задачи остановлены.")
    else:
        await update.message.reply_text("🛰️ Запускаю поток данных и сканер...")
        
        app._feed_task = asyncio.create_task(data_feeder.data_feed_main_loop(app, app.chat_ids))
        # Передаем объект таблицы TRADE_LOG_WS напрямую в сканер
        app._scanner_task = asyncio.create_task(scanner_main_loop(app, ask_llm, broadcast, TRADE_LOG_WS))
        
        await update.message.reply_text("✅ Поток данных и сканер запущены.")


async def post_init(app: Application):
    log.info("Bot application initialized.")


if __name__ == "__main__":
    load_state()
    setup_sheets() # Инициализируем Google-таблицы при старте
    
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.chat_ids = set(CHAT_IDS)
    
    # --- РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ ---
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("feed", cmd_feed))
    
    log.info(f"Bot v{BOT_VERSION} (Microstructure) started polling.")
    app.run_polling()
