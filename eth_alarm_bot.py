#!/usr/bin/env python3
# ============================================================================
# v9.3 - LLM Profit Filter
# • Фильтр минимальной прибыльности теперь применяется к уровням,
#   предложенным LLM, а не к механическим.
# • Обновлен промпт для LLM для более точного анализа.
# ============================================================================

import os
import asyncio
import json
import logging
from datetime import datetime, timezone
import pandas as pd
import ccxt.async_support as ccxt
import gspread
import aiohttp
import pandas_ta as ta
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.error import BadRequest

# === ENV / Logging ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
SHEET_ID = os.getenv("SHEET_ID")
COIN_LIST_SIZE = int(os.getenv("COIN_LIST_SIZE", "200"))
WORKSHEET_NAME = "Trading_Logs_v9"

LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_API_URL = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_MODEL_ID = os.getenv("LLM_MODEL_ID", "gpt-4.1")
LLM_THRESHOLD = float(os.getenv("LLM_CONFIDENCE_THRESHOLD", 7.0))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for n in ("httpx",): logging.getLogger(n).setLevel(logging.WARNING)

# === GOOGLE SHEETS ===
def setup_google_sheets():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gs = gspread.authorize(creds)
        spreadsheet = gs.open_by_key(SHEET_ID)
        try:
            worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
        except gspread.WorksheetNotFound:
            log.warning(f"Worksheet '{WORKSHEET_NAME}' not found. Creating it...")
            worksheet = spreadsheet.add_worksheet(title=WORKSHEET_NAME, rows="1000", cols="20")
        HEADERS = ["Дата входа", "Инструмент", "Направление", "Депозит", "Цена входа", "Stop Loss", "Take Profit", "P&L сделки (USDT)", "% к депозиту"]
        if worksheet.row_values(1) != HEADERS:
            worksheet.clear(); worksheet.update('A1', [HEADERS]); worksheet.format('A1:I1', {'textFormat': {'bold': True}})
        return worksheet
    except Exception as e:
        log.error("Google Sheets init failed: %s", e)
        return None
LOGS_WS = setup_google_sheets()

# === STATE MANAGEMENT ===
STATE_FILE = "scanner_v9_state.json"
state = {"monitoring": False, "manual_position": None, "last_alert_times": {}}
scanner_task = None
def save_state():
    with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2)
def load_state():
    global state
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f: state = json.load(f)
    log.info("State loaded. Manual Position: %s", state.get("manual_position"))

# === EXCHANGE ===
exchange = ccxt.mexc()

# === STRATEGY PARAMS ===
TIMEFRAME = '5m'
SCAN_INTERVAL_SECONDS = 60 * 15
ADX_LEN, ADX_THRESHOLD = 14, 25 # Возвращаем более адекватный ADX
BBANDS_LEN, BBANDS_STD = 20, 2.0
MIN_BB_WIDTH_PCT = 2.0 # И ширина канала
RSI_LEN, RSI_OVERSOLD = 14, 30
MIN_PROFIT_TARGET_PCT = 3.0 # Наш главный фильтр

# === INDICATORS ===
def calculate_indicators(df: pd.DataFrame):
    df.ta.adx(length=ADX_LEN, append=True)
    df.ta.bbands(length=BBANDS_LEN, std=BBANDS_STD, append=True)
    df.ta.rsi(length=RSI_LEN, append=True)
    return df.dropna()

# === LLM ===
LLM_PROMPT = (
    "Ты — профессиональный трейдер-аналитик 'Сигма'. Твоя задача — проанализировать предоставленный торговый сетап "
    "для входа в LONG на откате (покупка на падении). Ищи дополнительные подтверждения силы покупателей и потенциальные риски. "
    "Дай ответ ТОЛЬКО в виде JSON-объекта с полями: 'decision' ('APPROVE'/'REJECT'), 'confidence_score' (0-10), "
    "'reasoning' (RU, краткое обоснование), 'suggested_tp' (число), 'suggested_sl' (число).\n\n"
    "Анализируй сетап:\n{trade_data}"
)
async def ask_llm(trade_data):
    if not LLM_API_KEY: return {"decision": "N/A", "reasoning": "LLM не настроен."}
    prompt = LLM_PROMPT.format(trade_data=json.dumps(trade_data, indent=2, ensure_ascii=False))
    payload = {"model": LLM_MODEL_ID, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, json=payload, headers=headers, timeout=90) as r:
                txt = await r.text()
                if r.status != 200:
                    log.error(f"LLM HTTP Error {r.status}: {txt}")
                    return {"decision": "ERROR", "reasoning": f"HTTP {r.status}"}
                
                response_json = json.loads(txt)
                content_str = response_json["choices"][0]["message"]["content"]
                
                if "```json" in content_str:
                    clean_msg = content_str.split("```json")[1].split("```")[0]
                else:
                    clean_msg = content_str.strip().strip("`")

                return json.loads(clean_msg)
    except Exception as e:
        log.error("LLM request/parse err: %s", e)
        return {"decision": "ERROR", "reasoning": str(e)}

# === MAIN SCANNER LOOP ===
async def scanner_loop(app):
    await broadcast_message(app, "🤖 Сканер запущен. Начинаю формирование списка топ-монет по объему...")
    try:
        await exchange.load_markets()
        tickers = await exchange.fetch_tickers()
        usdt_pairs = {s: t for s, t in tickers.items() if s.endswith('/USDT') and t.get('quoteVolume') and all(kw not in s for kw in ['UP/', 'DOWN/', 'BEAR/', 'BULL/'])}
        sorted_pairs = sorted(usdt_pairs.items(), key=lambda item: item[1]['quoteVolume'], reverse=True)
        coin_list = [item[0] for item in sorted_pairs[:COIN_LIST_SIZE]]
        await broadcast_message(app, f"✅ Список из {len(coin_list)} монет для сканирования сформирован.")
    except Exception as e:
        log.error(f"Failed to fetch dynamic coin list: %s", e)
        await broadcast_message(app, "⚠️ Не удалось сформировать динамический список монет. Проверьте лог."); return

    while state.get('monitoring', False):
        log.info(f"Starting new scan for {len(coin_list)} coins...")
        found_signals = 0
        for pair in coin_list:
            try:
                ohlcv = await exchange.fetch_ohlcv(pair, timeframe=TIMEFRAME, limit=100)
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                if df.empty: continue
                df_with_indicators = calculate_indicators(df.copy())
                if len(df_with_indicators) < 2: continue
                
                last = df_with_indicators.iloc[-1]
                adx_value = last.get(f'ADX_{ADX_LEN}')
                bb_upper = last.get(f'BBU_{BBANDS_LEN}_{BBANDS_STD}')
                bb_lower = last.get(f'BBL_{BBANDS_LEN}_{BBANDS_STD}')
                rsi_value = last.get(f'RSI_{RSI_LEN}')

                if any(v is None for v in [adx_value, bb_upper, bb_lower, rsi_value]): continue

                bb_width_pct = ((bb_upper - bb_lower) / bb_lower) * 100
                is_ranging = adx_value < ADX_THRESHOLD
                is_wide_enough = bb_width_pct > MIN_BB_WIDTH_PCT
                is_oversold_at_bottom = last['close'] <= bb_lower and rsi_value < RSI_OVERSOLD
                
                if is_ranging and is_wide_enough and is_oversold_at_bottom:
                    now = datetime.now().timestamp()
                    if (now - state["last_alert_times"].get(pair, 0)) < 3600 * 4: continue

                    await broadcast_message(app, f"🔍 Найден кандидат: `{pair}`. Отправляю на анализ в LLM...")
                    
                    trade_data = { "asset": pair, "tf": TIMEFRAME, "price": last['close'], "rsi": round(rsi_value, 1), "bb_width_pct": round(bb_width_pct, 1), "adx": round(adx_value, 1) }
                    llm_decision = await ask_llm(trade_data)
                    
                    if llm_decision and llm_decision.get("decision") == "APPROVE" and llm_decision.get("confidence_score", 0) >= LLM_THRESHOLD:
                        suggested_tp = llm_decision.get('suggested_tp')
                        entry_price = last['close']

                        if suggested_tp and isinstance(suggested_tp, (int, float)):
                            profit_potential_pct = ((float(suggested_tp) - entry_price) / entry_price) * 100
                            if profit_potential_pct >= MIN_PROFIT_TARGET_PCT:
                                found_signals += 1
                                stop_loss = llm_decision.get('suggested_sl', 'N/A')
                                message = (
                                    f"🔔 **СИГНАЛ: LONG (Buy The Dip) - ОДОБРЕН**\n\n"
                                    f"**Монета:** `{pair}`\n"
                                    f"**Текущая цена:** `{entry_price:.4f}`\n\n"
                                    f"--- **Параметры от LLM** ---\n"
                                    f"**Take Profit:** `{suggested_tp}` (Потенциал: {profit_potential_pct:.1f}%)\n"
                                    f"**Stop Loss:** `{stop_loss}`\n\n"
                                    f"--- **Анализ LLM ({llm_decision.get('confidence_score')}/10)** ---\n"
                                    f"_{llm_decision.get('reasoning')}_"
                                )
                                await broadcast_message(app, message)
                                state["last_alert_times"][pair] = now; save_state()
                            else:
                                await broadcast_message(app, f"ℹ️ LLM одобрил `{pair}`, но потенциал прибыли ({profit_potential_pct:.1f}%) меньше цели в {MIN_PROFIT_TARGET_PCT}%. Сигнал отфильтрован.")
                        else:
                            await broadcast_message(app, f"ℹ️ LLM одобрил `{pair}`, но не дал корректную цель по прибыли. Сигнал отфильтрован.")
                    else:
                        await broadcast_message(app, f"ℹ️ LLM отклонил сигнал по `{pair}`. Причина: _{llm_decision.get('reasoning', 'Нет')}_")
            except Exception as e:
                log.error(f"Error processing pair {pair}: {e}")
        
        log.info(f"Scan finished. Found {found_signals} approved signals.")
        if found_signals == 0:
            await broadcast_message(app, "ℹ️ Сканирование завершено. Подходящих кандидатов не найдено.")
        
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)

# === COMMANDS and RUN ===
async def broadcast_message(app, text):
    for chat_id in app.chat_ids:
        safe_text = text.replace('.', r'\.').replace('-', r'\-').replace('(', r'\(').replace(')', r'\)').replace('>', r'\>').replace('+', r'\+').replace('=', r'\=').replace('*', r'\*').replace('_', r'\_').replace('`', r'\`').replace('[', r'\[').replace(']', r'\]')
        try:
            await app.bot.send_message(chat_id=chat_id, text=safe_text, parse_mode="MarkdownV2")
        except BadRequest as e:
            if 'can\'t parse entities' in str(e):
                log.warning(f"Markdown parse failed. Sending as plain text. Error: {e}")
                await app.bot.send_message(chat_id=chat_id, text=text)
            else:
                log.error(f"Failed to send message to {chat_id}: {e}")
        except Exception as e:
            log.error(f"An unexpected error occurred in broadcast_message: {e}")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global scanner_task
    chat_id = update.effective_chat.id
    if not hasattr(ctx.application, 'chat_ids'): ctx.application.chat_ids = set()
    ctx.application.chat_ids.add(chat_id)
    
    if scanner_task is None or scanner_task.done():
        state['monitoring'] = True; save_state()
        await update.message.reply_text("✅ Сканер запущен (v9.3 LLM Profit Filter).")
        scanner_task = asyncio.create_task(scanner_loop(ctx.application))
    else:
        await update.message.reply_text("ℹ️ Сканер уже запущен.")

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global scanner_task
    if scanner_task and not scanner_task.done():
        scanner_task.cancel()
        scanner_task = None
    state['monitoring'] = False; save_state()
    await update.message.reply_text("❌ Мониторинг остановлен.")
# ... (Команды /entry и /exit без изменений)

if __name__ == "__main__":
    load_state()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    # app.add_handler(CommandHandler("entry", cmd_entry))
    # app.add_handler(CommandHandler("exit", cmd_exit))
    log.info("Bot started...")
    app.run_polling()
