#!/usr/bin/env python3
# ============================================================================
# v7.3 - Debug Mode
# • Добавлен отладочный режим для анализа работы фильтров.
# ============================================================================

import os
import asyncio
import json
import logging
from datetime import datetime
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
COIN_LIST_SIZE = int(os.getenv("COIN_LIST_SIZE", "400")) 
WORKSHEET_NAME = "Trading_Logs_v7"
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

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
STATE_FILE = "scanner_v7_state.json"
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
ADX_LEN, ADX_THRESHOLD = 14, 40
BBANDS_LEN, BBANDS_STD = 20, 2.0
MIN_BB_WIDTH_PCT = 1.0
RSI_LEN, RSI_OVERSOLD = 14, 35
ATR_LEN_FOR_SL, SL_ATR_MUL = 14, 0.5

# === INDICATORS ===
def calculate_indicators(df: pd.DataFrame):
    df.ta.adx(length=ADX_LEN, append=True)
    df.ta.bbands(length=BBANDS_LEN, std=BBANDS_STD, append=True)
    df.ta.rsi(length=RSI_LEN, append=True)
    df.ta.atr(length=ATR_LEN_FOR_SL, append=True)
    return df.dropna()

# === LLM ===
LLM_RANKING_PROMPT = (
    "Ты — главный аналитик инвестфонда. Тебе предоставлен список активов, показывающих механический сигнал на покупку в боковике (лонг в 'пиле'). "
    "Твоя задача — проанализировать все сетапы и выбрать из них до 5 самых качественных и надежных. "
    "Верни ответ ТОЛЬКО в виде JSON-списка. Каждый элемент списка должен быть JSON-объектом с полями 'asset' (тикер актива) и 'reasoning' (RU, краткое и убедительное обоснование, почему этот актив лучше других кандидатов). "
    "Ранжируй список от лучшего к худшему. Если качественных сетапов нет, верни пустой список [].\n\n"
    "Кандидаты:\n{candidates}"
)
async def ask_llm_to_rank(candidates_data):
    if not LLM_API_KEY: return []
    prompt = LLM_RANKING_PROMPT.format(candidates=json.dumps(candidates_data, indent=2, ensure_ascii=False))
    payload = {"model": LLM_MODEL_ID, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, json=payload, headers=headers, timeout=180) as r:
                txt = await r.text()
                if r.status != 200:
                    log.error(f"LLM Ranking HTTP Error {r.status}: {txt}"); return []
                
                response_json = json.loads(txt)
                content_str = response_json["choices"][0]["message"]["content"]
                
                if "```json" in content_str:
                    json_part = content_str.split("```json")[1].split("```")[0]
                elif "```" in content_str:
                    json_part = content_str.split("```")[1]
                else:
                    json_part = content_str
                
                if '[' in json_part and ']' in json_part:
                    list_part = json_part[json_part.find('['):json_part.rfind(']')+1]
                    return json.loads(list_part)
                return []
    except Exception as e:
        log.error("LLM Ranking request/parse err: %s", e)
        return []

# === MAIN SCANNER LOOP ===
async def scanner_loop(app):
    await broadcast_message(app, "🤖 Сканер запущен. Начинаю формирование списка топ-монет по объему...")
    try:
        await exchange.load_markets()
        tickers = await exchange.fetch_tickers()
        usdt_pairs = {s: t for s, t in tickers.items() if s.endswith('/USDT') and t.get('quoteVolume') and all(kw not in s for kw in ['UP/', 'DOWN/', 'BEAR/', 'BULL/', '3S/', '3L/', '2S/', '2L/', '4S/', '4L/', '5S/', '5L/'])}
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
                if len(df_with_indicators) < 2:
                    if DEBUG_MODE: log.warning(f"Not enough data for {pair} after indicator calculation, skipping.")
                    continue
                
                last = df_with_indicators.iloc[-1]
                adx_value = last[f'ADX_{ADX_LEN}']
                bb_upper = last[f'BBU_{BBANDS_LEN}_{BBANDS_STD}']
                bb_lower = last[f'BBL_{BBANDS_LEN}_{BBANDS_STD}']
                bb_width_pct = ((bb_upper - bb_lower) / bb_lower) * 100
                rsi_value = last[f'RSI_{RSI_LEN}']

                # --- ЛОГИКА С РЕЖИМОМ ОТЛАДКИ ---
                is_ranging = adx_value < ADX_THRESHOLD
                if not is_ranging:
                    if DEBUG_MODE: await broadcast_message(app, f"🚫 `{pair}` отброшен: ADX слишком высокий ({adx_value:.1f})")
                    continue

                is_wide_enough = bb_width_pct > MIN_BB_WIDTH_PCT
                if not is_wide_enough:
                    if DEBUG_MODE: await broadcast_message(app, f"🚫 `{pair}` отброшен: канал слишком узкий ({bb_width_pct:.1f}%)")
                    continue
                
                is_oversold_at_bottom = last['close'] <= bb_lower and rsi_value < RSI_OVERSOLD
                if not is_oversold_at_bottom:
                    if DEBUG_MODE and rsi_value >= RSI_OVERSOLD : await broadcast_message(app, f"🚫 `{pair}` отброшен: RSI не в зоне перепроданности ({rsi_value:.1f})")
                    continue
                # --- КОНЕЦ ЛОГИКИ ---

                # Если все проверки пройдены, монета - кандидат
                if (datetime.now().timestamp() - state["last_alert_times"].get(pair, 0)) < 3600 * 4: continue
                candidates.append({"pair": pair, "price": last['close'], "atr": last[f'ATRr_{ATR_LEN_FOR_SL}'], "rsi": round(rsi_value, 1), "bb_lower": bb_lower, "bb_middle": last[f'BBM_{BBANDS_LEN}_{BBANDS_STD}']})
                
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
                    entry_price = original_candidate['price']
                    take_profit = original_candidate['bb_middle']
                    stop_loss = original_candidate['bb_lower'] - (original_candidate['atr'] * SL_ATR_MUL)
                    
                    message = (
                        f"🔔 **СИГНАЛ: LONG (Range Trade)**\n\n"
                        f"**Монета:** `{asset_name}`\n"
                        f"**Текущая цена:** `{entry_price:.4f}`\n\n"
                        f"--- **Параметры сделки** ---\n"
                        f"**Вход:** `~{entry_price:.4f}`\n"
                        f"**Take Profit:** `{take_profit:.4f}` (Средняя BB)\n"
                        f"**Stop Loss:** `{stop_loss:.4f}`\n\n"
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
        safe_text = text.replace('.', r'\.').replace('-', r'\-').replace('(', r'\(').replace(')', r'\)').replace('>', r'\>').replace('+', r'\+').replace('=', r'\=').replace('*', r'\*').replace('_', r'\_').replace('`', r'\`').replace('[', r'\[').replace(']', r'\]')
        await app.bot.send_message(chat_id=chat_id, text=safe_text, parse_mode="MarkdownV2")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global scanner_task
    chat_id = update.effective_chat.id
    if not hasattr(ctx.application, 'chat_ids'): ctx.application.chat_ids = set()
    ctx.application.chat_ids.add(chat_id)
    
    if scanner_task is None or scanner_task.done():
        state['monitoring'] = True; save_state()
        await update.message.reply_text("✅ Сканер запущен (v7.3 Debug).")
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

async def cmd_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global state
    try:
        pair = ctx.args[0].upper()
        if "/" not in pair: pair += "/USDT"
        deposit, entry_price, sl, tp = map(float, ctx.args[1:5])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ /entry <ТИКЕР> <депозит> <цена> <sl> <tp>"); return

    state["manual_position"] = {
        "entry_time": datetime.now(timezone.utc).isoformat(), "deposit": deposit,
        "entry_price": entry_price, "sl": sl, "tp": tp, "pair": pair, "side": "LONG"
    }
    save_state()
    await update.message.reply_text(f"✅ Вход вручную зафиксирован: {pair} @ {entry_price}")

async def cmd_exit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global state
    pos = state.get("manual_position")
    if not pos: await update.message.reply_text("⚠️ Нет открытой ручной позиции."); return
    try:
        exit_price = float(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Использование: /exit <цена_выхода>"); return

    pnl = (exit_price - pos['entry_price']) / pos['entry_price'] * pos['deposit']
    if "SHORT" in pos.get("side", "LONG").upper(): pnl = -pnl
    pct_change = (pnl / pos['deposit']) * 100
    
    if LOGS_WS:
        rr = abs((pos['tp'] - pos['entry_price']) / (pos['sl'] - pos['entry_price'])) if (pos['sl'] - pos['entry_price']) != 0 else 0
        row = [
            datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), pos['pair'],
            pos.get("side", "N/A"), pos['deposit'], pos['entry_price'], pos['sl'], pos['tp'], 
            round(rr, 2), round(pnl, 2), round(pct_change, 2)
        ]
        await asyncio.to_thread(LOGS_WS.append_row, row, value_input_option='USER_ENTERED')
    
    await update.message.reply_text(f"✅ Сделка по {pos['pair']} закрыта и записана.\nP&L: {pnl:.2f} USDT ({pct_change:.2f}%)")
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
