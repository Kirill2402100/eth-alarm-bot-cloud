#!/usr/bin/env python3
# ============================================================================
# v11.1 - Single Target R:R
# • Возвращена логика с одним Take Profit.
# • TP теперь жестко рассчитывается от SL для обеспечения R:R 1:2.
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
WORKSHEET_NAME = "Trading_Logs_v10"

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
STATE_FILE = "scanner_v10_state.json"
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
ADX_LEN = 14
# Ваши "мягкие" настройки для ADX
ADX_MIN_THRESHOLD = 20.0 
ADX_MAX_THRESHOLD = 40.0 
# Ваши "мягкие" настройки для BB и RSI
BBANDS_LEN, BBANDS_STD = 20, 2.0
MIN_BB_WIDTH_PCT = 1.0 
RSI_LEN, RSI_OVERSOLD = 14, 40
# Новые обязательные параметры для расчета SL и TP
ATR_LEN_FOR_SL, SL_ATR_MUL = 14, 0.5 
MIN_RR_RATIO = 0.5  # Наша цель по соотношению Риск/Прибыль 1:2

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
                
                if "```json" in content_str: clean_msg = content_str.split("```json")[1].split("```")[0]
                else: clean_msg = content_str.strip().strip("`")
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
        candidates = []
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
                is_in_perfect_range = (adx_value > ADX_MIN_THRESHOLD) and (adx_value < ADX_MAX_THRESHOLD)
                is_wide_enough = bb_width_pct > MIN_BB_WIDTH_PCT
                is_oversold_at_bottom = last['close'] <= bb_lower and rsi_value < RSI_OVERSOLD
                
                if is_in_perfect_range and is_wide_enough and is_oversold_at_bottom:
                    if (datetime.now().timestamp() - state["last_alert_times"].get(pair, 0)) < 3600 * 4: continue
                    
                    # ---> ЛОГИКА БЕЗ ФИЛЬТРА R:R <---
                    # Просто рассчитываем механические уровни и добавляем в кандидаты
                    entry_price = last['close']
                    take_profit = last[f'BBM_{BBANDS_LEN}_{BBANDS_STD}']
                    stop_loss = bb_lower - (last[f'ATRr_{ATR_LEN_FOR_SL}'] * SL_ATR_MUL)

                    candidates.append({
                        "pair": pair, "price": entry_price, "rsi": round(rsi_value, 1),
                        "sl": stop_loss, "tp": take_profit
                    })

            except Exception as e:
                log.error(f"Error processing pair {pair}: {e}")

        if candidates:
            await broadcast_message(app, f"🔍 Найдено {len(candidates)} кандидатов. Отправляю на ранжирование в LLM...")
            
            llm_candidates_data = [{"asset": c["pair"], "rsi": c["rsi"]} for c in candidates]
            top_rated_assets = await ask_llm_to_rank(llm_candidates_data)

            if top_rated_assets:
                await broadcast_message(app, f"🏆 LLM отобрал топ-{len(top_rated_assets)} лучших сигналов:")
                for ranked_asset in top_rated_assets:
                    asset_name = ranked_asset.get("asset")
                    original_candidate = next((c for c in candidates if c['pair'] == asset_name), None)
                    if not original_candidate: continue
                    
                    message = (
                        f"🔔 **СИГНАЛ: LONG (Range Trade)**\n\n"
                        f"**Монета:** `{asset_name}`\n"
                        f"**Цена входа:** `~{original_candidate['price']:.4f}`\n\n"
                        f"--- **Параметры сделки** ---\n"
                        f"**Take Profit:** `{original_candidate['tp']:.4f}` (Средняя BB)\n"
                        f"**Stop Loss:** `{original_candidate['sl']:.4f}`\n\n"
                        f"--- **Анализ LLM** ---\n"
                        f"_{ranked_asset.get('reasoning')}_"
                    )
                    await broadcast_message(app, message)
                    state["last_alert_times"][asset_name] = datetime.now().timestamp()
                    save_state()
                    await asyncio.sleep(1)
            else: 
                await broadcast_message(app, "ℹ️ LLM не одобрил ни одного кандидата.")
        else:
            log.info("No valid candidates found in this scan cycle.")
            await broadcast_message(app, "ℹ️ Сканирование завершено. Подходящих кандидатов не найдено.")
        
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
        
# === COMMANDS and RUN ===
async def broadcast_message(app, text):
    for chat_id in app.chat_ids:
        # MarkdownV2 требует экранирования спецсимволов
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
    if not hasattr(ctx.application, 'chat_ids'):
        ctx.application.chat_ids = set()
    ctx.application.chat_ids.add(chat_id)
    
    if scanner_task is None or scanner_task.done():
        state['monitoring'] = True
        save_state()
        await update.message.reply_text("✅ Сканер запущен (v10.1 Balance P&L).")
        scanner_task = asyncio.create_task(scanner_loop(ctx.application))
    else:
        await update.message.reply_text("ℹ️ Сканер уже запущен.")

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global scanner_task
    if scanner_task and not scanner_task.done():
        scanner_task.cancel()
        scanner_task = None
    state['monitoring'] = False
    save_state()
    await update.message.reply_text("❌ Мониторинг остановлен.")

async def cmd_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global state
    try:
        # Формат: /entry ТИКЕР депозит цена_входа sl tp
        pair = ctx.args[0].upper()
        if "/" not in pair: pair += "/USDT"
        
        deposit, entry_price, sl, tp = map(float, ctx.args[1:5])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат. Используйте:\n`/entry <ТИКЕР> <депозит> <цена> <sl> <tp>`\n\n*Пример:*\n`/entry SOL/USDT 500 135.5 134.8 138.0`", parse_mode="MarkdownV2")
        return

    state["manual_position"] = {
        "entry_time": datetime.now(timezone.utc).isoformat(), # <-- Здесь была ошибка
        "deposit": deposit,
        "entry_price": entry_price,
        "sl": sl,
        "tp": tp,
        "pair": pair,
        "side": "LONG"
    }
    save_state()
    await update.message.reply_text(f"✅ Вход вручную зафиксирован: **{pair}** @ **{entry_price}**", parse_mode="HTML")

async def cmd_exit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global state
    pos = state.get("manual_position")
    if not pos:
        await update.message.reply_text("⚠️ Нет открытой ручной позиции для закрытия.")
        return
    try:
        # Ожидаем только итоговый депозит
        exit_deposit = float(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат. Используйте: `/exit <итоговый_депозит>`", parse_mode="MarkdownV2")
        return

    # --- ЕДИНЫЙ И ПРАВИЛЬНЫЙ РАСЧЕТ ---
    initial_deposit = pos.get('deposit', 0)
    pnl = exit_deposit - initial_deposit
    pct_change = (pnl / initial_deposit) * 100 if initial_deposit != 0 else 0
    # --- КОНЕЦ РАСЧЕТА ---
    
    if LOGS_WS:
        try:
            rr = abs((pos['tp'] - pos['entry_price']) / (pos['sl'] - pos['entry_price'])) if (pos.get('sl') and pos.get('entry_price') and (pos['sl'] - pos['entry_price']) != 0) else 0
            row = [
                datetime.fromisoformat(pos['entry_time']).strftime('%Y-%m-%d %H:%M:%S'),
                pos.get('pair', 'N/A'),
                pos.get("side", "N/A"),
                initial_deposit,
                pos.get('entry_price', 'N/A'),
                pos.get('sl', 'N/A'),
                pos.get('tp', 'N/A'),
                round(rr, 2),
                round(pnl, 2),
                round(pct_change, 2)
            ]
            await asyncio.to_thread(LOGS_WS.append_row, row, value_input_option='USER_ENTERED')
        except Exception as e:
            log.error("Failed to write to Google Sheets: %s", e)
            await update.message.reply_text("⚠️ Ошибка записи в Google Таблицу.")

    await update.message.reply_text(f"✅ Сделка по **{pos.get('pair', 'N/A')}** закрыта и записана.\n**P&L: {pnl:+.2f} USDT ({pct_change:+.2f}%)**", parse_mode="HTML")
    
    state["manual_position"] = None
    save_state()

if __name__ == "__main__":
    load_state()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("entry", cmd_entry))
    app.add_handler(CommandHandler("exit", cmd_exit))

    log.info("Bot started...")
    app.run_polling()
