#!/usr/bin/env python3
# ============================================================================
# v7.1 - Concurrent Architecture with Smart Pause
# • Bot is now fully autonomous. No more /entry or /exit commands.
# • Two independent loops:
#   1. Signal Scanner: Finds the best setup via LLM. Pauses if the monitoring buffer is full.
#   2. Position Monitor: Tracks all active signals for SL/TP hits every 60s.
# • All outcomes are automatically logged to Google Sheets.
# • Added a limit for max concurrent monitored signals.
# ============================================================================

import os
import asyncio
import json
import logging
from datetime import datetime, timezone
import uuid
import pandas as pd
import ccxt.async_support as ccxt
import gspread
import aiohttp
import pandas_ta as ta
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# === ENV / Logging ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
SHEET_ID = os.getenv("SHEET_ID")
COIN_LIST_SIZE = int(os.getenv("COIN_LIST_SIZE", "100"))
MAX_CONCURRENT_SIGNALS = int(os.getenv("MAX_CONCURRENT_SIGNALS", "10")) # Лимит одновременных сделок

LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_API_URL = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_MODEL_ID = os.getenv("LLM_MODEL_ID", "gpt-4.1")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for n in ("httpx", "httpcore"): logging.getLogger(n).setLevel(logging.WARNING)

# === GOOGLE SHEETS ===
TRADE_LOG_WS = None
def setup_google_sheets():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gs = gspread.authorize(creds)
        spreadsheet = gs.open_by_key(SHEET_ID)
        
        headers = [
            "Signal_ID", "Pair", "Side", "Status", "Entry_Time_UTC", "Exit_Time_UTC",
            "Entry_Price", "Exit_Price", "SL_Price", "TP_Price", "Reason"
        ]
        
        sheet_name = "Autonomous_Trade_Log"
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=sheet_name, rows="1000", cols=len(headers))
        
        if worksheet.row_values(1) != headers:
            worksheet.clear()
            worksheet.update('A1', [headers])
            worksheet.format(f'A1:{chr(ord("A")+len(headers)-1)}1', {'textFormat': {'bold': True}})
        
        log.info(f"Google Sheets setup complete. Logging to '{sheet_name}'.")
        return worksheet
    except Exception as e:
        log.error("Google Sheets init failed: %s", e)
        return None
TRADE_LOG_WS = setup_google_sheets()

# === STATE MANAGEMENT ===
STATE_FILE = "concurrent_bot_state.json"
state = {}
def save_state():
    with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2)
def load_state():
    global state
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f: state = json.load(f)
    
    if 'bot_on' not in state:
        state.update({"bot_on": False, "monitored_signals": []}) # Основное хранилище - список
    log.info(f"State loaded: {len(state.get('monitored_signals', []))} signals are being monitored.")

# === EXCHANGE & STRATEGY PARAMS ===
exchange = ccxt.mexc({'options': {'defaultType': 'swap'}})
TIMEFRAME_ENTRY = os.getenv("TF_ENTRY", "15m")
ATR_LEN = 14
SL_ATR_MULTIPLIER = 1.0
RR_RATIO = 2.0

# === LLM PROMPT ===
PROMPT_FINAL_APPROVAL = (
    "Ты — главный трейдер-аналитик. Тебе предоставлен список кандидатов, у которых 9 и 21 EMA на 15-минутном графике недавно пересеклись. "
    "Твоя задача — выбрать ОДИН, САМЫЙ ЛУЧШИЙ сетап для входа в сделку. "
    "Оценивай силу тренда на H1, ADX, RSI. Выбери самый перспективный вариант. "
    "Твой ответ ОБЯЗАТЕЛЬНО должен быть в формате JSON и содержать полный объект выбранного тобой лучшего кандидата. "
    "Добавь в него поле `reason` с кратким обоснованием твоего выбора."
)
async def ask_llm(final_prompt: str):
    # ... (код без изменений)
    if not LLM_API_KEY: return None
    payload = {"model": LLM_MODEL_ID, "messages": [{"role": "user", "content": final_prompt}], "temperature": 0.4, "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, json=payload, headers=headers, timeout=180) as r:
                txt = await r.text()
                if r.status != 200:
                    log.error(f"LLM HTTP Error {r.status}: {txt}")
                    return None
                response_json = json.loads(txt)
                content_str = response_json["choices"][0]["message"]["content"]
                return json.loads(content_str.strip().strip("`"))
    except Exception as e:
        log.error(f"LLM request/parse err: {e}", exc_info=True)
        return None

# === BACKGROUND LOOPS ===

# --- LOOP 1: Signal Scanner ---
async def signal_scanner_loop(app):
    """Ищет новые сигналы, ставя себя на паузу, если достигнут лимит."""
    while state.get('bot_on', False):
        try:
            # --- ИЗМЕНЕННАЯ ЛОГИКА ПАУЗЫ ---
            # Проверяем лимит ПЕРЕД началом сканирования
            if len(state.get("monitored_signals", [])) >= MAX_CONCURRENT_SIGNALS:
                log.warning(f"Max concurrent signals ({MAX_CONCURRENT_SIGNALS}) reached. Scanner is pausing for 5 minutes.")
                await asyncio.sleep(60 * 5) # Ждем 5 минут и проверяем снова
                continue # Перезапускаем цикл, чтобы снова проверить лимит, а не ждать 20 минут

            # Если лимит не достигнут, выполняем полный цикл сканирования
            log.info("--- SCANNER: Starting new scan cycle. ---")
            
            # Этап 1: Поиск кандидатов
            log.info("SCANNER: Searching for fresh EMA crossovers...")
            pre_candidates = []
            tickers = await exchange.fetch_tickers()
            usdt_pairs = {s: t for s, t in tickers.items() if s.endswith(':USDT') and t.get('quoteVolume')}
            sorted_pairs = sorted(usdt_pairs.items(), key=lambda item: item[1]['quoteVolume'], reverse=True)
            coin_list = [item[0] for item in sorted_pairs[:COIN_LIST_SIZE]]
            
            for pair in coin_list:
                if len(pre_candidates) >= 10: break
                if not state.get('bot_on'): return
                try:
                    ohlcv = await exchange.fetch_ohlcv(pair, timeframe=TIMEFRAME_ENTRY, limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    if len(df) < 22: continue
                    df.ta.ema(length=9, append=True)
                    df.ta.ema(length=21, append=True)
                    
                    for i in range(len(df) - 1, len(df) - 6, -1):
                        if i < 1: break
                        last, prev = df.iloc[i], df.iloc[i-1]
                        if prev.get('EMA_9') <= prev.get('EMA_21') and last.get('EMA_9') > last.get('EMA_21'):
                            pre_candidates.append({"pair": pair, "side": "LONG"}); break
                        elif prev.get('EMA_9') >= prev.get('EMA_21') and last.get('EMA_9') < last.get('EMA_21'):
                            pre_candidates.append({"pair": pair, "side": "SHORT"}); break
                except Exception: continue
            
            if not pre_candidates:
                log.info("SCANNER: No fresh crossovers found. Waiting 20 minutes.")
                await asyncio.sleep(60 * 20)
                continue

            # Этап 2: Сбор данных и расчет SL/TP
            log.info(f"SCANNER: Found {len(pre_candidates)} candidates. Collecting deep data...")
            setups_for_llm = []
            for candidate in pre_candidates:
                try:
                    pair, side = candidate['pair'], candidate['side']
                    ohlcv_h1 = await exchange.fetch_ohlcv(pair, '1h', limit=100)
                    ohlcv_entry = await exchange.fetch_ohlcv(pair, TIMEFRAME_ENTRY, limit=100)
                    
                    df_h1 = pd.DataFrame(ohlcv_h1, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']); df_h1.ta.ema(length=50, append=True)
                    df_entry = pd.DataFrame(ohlcv_entry, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']); df_entry.ta.atr(length=ATR_LEN, append=True); df_entry.ta.rsi(length=14, append=True); df_entry.ta.adx(length=14, append=True)
                    
                    last_entry, last_h1 = df_entry.iloc[-1], df_h1.iloc[-1]
                    atr_value, entry_price = last_entry.get(f'ATRr_{ATR_LEN}'), last_entry['close']
                    if any(v is None for v in [atr_value, entry_price]) or atr_value == 0: continue
                    
                    risk = atr_value * SL_ATR_MULTIPLIER
                    sl, tp = (entry_price - risk, entry_price + risk * RR_RATIO) if side == 'LONG' else (entry_price + risk, entry_price - risk * RR_RATIO)
                    
                    setups_for_llm.append({
                        "pair": pair, "side": side, "entry_price": entry_price, "sl": sl, "tp": tp,
                        "h1_trend": "UP" if last_h1['close'] > last_h1.get('EMA_50') else "DOWN",
                        "adx": round(last_entry.get('ADX_14'), 2), "rsi": round(last_entry.get('RSI_14'), 2)
                    })
                except Exception as e: log.warning(f"SCANNER: Could not process candidate {candidate['pair']}: {e}")

            if not setups_for_llm:
                log.info("SCANNER: Failed to build any setups for LLM. Waiting 20 minutes.")
                await asyncio.sleep(60 * 20)
                continue

            # Этап 3: Выбор LLM
            log.info(f"SCANNER: Sending {len(setups_for_llm)} setups to LLM for final choice...")
            prompt_text = PROMPT_FINAL_APPROVAL + "\n\nКандидаты для выбора (JSON):\n" + json.dumps({"candidates": setups_for_llm})
            final_setup = await ask_llm(prompt_text)

            if final_setup and final_setup.get('pair'):
                final_setup['signal_id'] = str(uuid.uuid4())[:8]
                final_setup['entry_time_utc'] = datetime.now(timezone.utc).isoformat()
                state['monitored_signals'].append(final_setup)
                save_state()
                
                log.info(f"SCANNER: LLM chose {final_setup['pair']}. Added to monitoring list.")
                message = (f"🔔 <b>НОВЫЙ СИГНАЛ! (ID: {final_setup['signal_id']})</b> 🔔\n\n"
                           f"<b>Монета:</b> <code>{final_setup.get('pair')}</code>\n<b>Направление:</b> <b>{final_setup.get('side')}</b>\n"
                           f"<b>Цена входа (расчетная):</b> <code>{final_setup.get('entry_price'):.6f}</code>\n"
                           f"<b>Take Profit:</b> <code>{final_setup.get('tp'):.6f}</code>\n<b>Stop Loss:</b> <code>{final_setup.get('sl'):.6f}</code>\n\n"
                           f"<b>Обоснование LLM:</b> <i>{final_setup.get('reason')}</i>\n\n"
                           f"<i>Бот автоматически отслеживает эту позицию.</i>")
                await broadcast_message(app, message)
            else:
                log.info("SCANNER: LLM rejected all candidates.")

            # После полного цикла сканирования ждем 20 минут
            log.info("--- SCANNER: Full scan cycle finished. Waiting 20 minutes. ---")
            await asyncio.sleep(60 * 20)

        except Exception as e:
            log.error(f"CRITICAL ERROR in Signal Scanner Loop: {e}", exc_info=True)
            await asyncio.sleep(60 * 5) # Пауза в случае критической ошибки

# --- LOOP 2: Position Monitor ---
async def position_monitor_loop(app):
    """Проверяет все активные сигналы на предмет закрытия по SL/TP."""
    while state.get('bot_on', False):
        if not state.get('monitored_signals'):
            await asyncio.sleep(30) # Нет активных сигналов, проверяем реже
            continue
        
        log.info(f"--- MONITOR: Checking {len(state['monitored_signals'])} active signals... ---")
        
        signals_to_check = list(state['monitored_signals'])
        closed_signals_ids = []

        for signal in signals_to_check:
            try:
                ticker = await exchange.fetch_ticker(signal['pair'])
                current_price = ticker.get('last')
                if not current_price: continue

                side, sl, tp = signal['side'], signal['sl'], signal['tp']
                outcome = None
                
                if side == 'LONG' and current_price >= tp: outcome = "TP_HIT"
                elif side == 'LONG' and current_price <= sl: outcome = "SL_HIT"
                elif side == 'SHORT' and current_price <= tp: outcome = "TP_HIT"
                elif side == 'SHORT' and current_price >= sl: outcome = "SL_HIT"

                if outcome:
                    log.info(f"MONITOR: Signal {signal['signal_id']} for {signal['pair']} closed by {outcome} at price {current_price}.")
                    
                    if TRADE_LOG_WS:
                        try:
                            row = [
                                signal.get('signal_id'), signal.get('pair'), signal.get('side'), outcome,
                                signal.get('entry_time_utc'), datetime.now(timezone.utc).isoformat(),
                                signal.get('entry_price'), current_price, signal.get('sl'), signal.get('tp'),
                                signal.get('reason', 'N/A')
                            ]
                            await asyncio.to_thread(TRADE_LOG_WS.append_row, row, value_input_option='USER_ENTERED')
                        except Exception as e:
                            log.error(f"MONITOR: Failed to write to Google Sheets for {signal['signal_id']}: {e}")

                    status_emoji = "✅" if outcome == "TP_HIT" else "❌"
                    message = (f"{status_emoji} <b>СДЕЛКА ЗАКРЫТА ({outcome})</b> {status_emoji}\n\n"
                               f"<b>ID:</b> {signal['signal_id']}\n"
                               f"<b>Монета:</b> <code>{signal['pair']}</code>\n"
                               f"<b>Направление:</b> {signal['side']}\n"
                               f"<b>Цена выхода:</b> <code>{current_price:.6f}</code>")
                    await broadcast_message(app, message)
                    
                    closed_signals_ids.append(signal['signal_id'])

            except Exception as e:
                log.error(f"MONITOR: Error checking signal {signal.get('signal_id', 'N/A')}: {e}")
        
        if closed_signals_ids:
            state['monitored_signals'] = [s for s in state['monitored_signals'] if s['signal_id'] not in closed_signals_ids]
            save_state()
            log.info(f"MONITOR: Removed {len(closed_signals_ids)} closed signals from state.")

        await asyncio.sleep(60) # Проверяем позиции раз в минуту

# === COMMANDS & LIFECYCLE ===
async def broadcast_message(app, text):
    chat_ids = getattr(app, 'chat_ids', CHAT_IDS)
    for chat_id in chat_ids:
        try: await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception as e: log.error(f"Failed to send message to {chat_id}: {e}")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not hasattr(ctx.application, 'chat_ids'): ctx.application.chat_ids = set()
    ctx.application.chat_ids.add(chat_id)
    
    if not state.get('bot_on'):
        state['bot_on'] = True
        save_state()
        await update.message.reply_text("✅ Бот запущен в автономном режиме. Начинаю поиск и мониторинг.")
        asyncio.create_task(signal_scanner_loop(ctx.application))
        asyncio.create_task(position_monitor_loop(ctx.application))
    else:
        await update.message.reply_text("ℹ️ Бот уже запущен.")

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state.get('bot_on'):
        state['bot_on'] = False
        save_state()
        await update.message.reply_text("❌ Бот остановлен. Фоновые задачи завершат текущий цикл и остановятся.")
    else:
        await update.message.reply_text("ℹ️ Бот уже был остановлен.")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    monitored_count = len(state.get('monitored_signals', []))
    msg = f"<b>Статус бота:</b> {'АКТИВЕН' if state.get('bot_on') else 'ОСТАНОВЛЕН'}\n\n"
    msg += f"<b>Отслеживается сигналов:</b> {monitored_count} / {MAX_CONCURRENT_SIGNALS}\n\n"
    
    if monitored_count > 0:
        msg += "<b><u>Активные сигналы:</u></b>\n"
        for signal in state['monitored_signals']:
            msg += (f"  - <code>{signal['pair']}</code> <b>{signal['side']}</b> (ID: {signal['signal_id']})\n"
                    f"    TP: <code>{signal['tp']:.4f}</code>, SL: <code>{signal['sl']:.4f}</code>\n")
    else:
        msg += "<i>Нет активных сигналов для отслеживания.</i>"
        
    await update.message.reply_text(msg, parse_mode="HTML")

if __name__ == "__main__":
    load_state()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = CHAT_IDS
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))

    log.info("Autonomous Bot starting...")
    if state.get('bot_on', False):
        asyncio.create_task(signal_scanner_loop(app))
        asyncio.create_task(position_monitor_loop(app))
        
    app.run_polling()
