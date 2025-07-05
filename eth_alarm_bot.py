#!/usr/bin/env python3
# ============================================================================
# v12.0 - Advanced Analysis
# • Добавлен индикатор Stochastic для более точного определения перепроданности.
# • Улучшен промпт LLM для анализа дивергенций, свечных паттернов и объемов.
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
ADX_THRESHOLD = 25.0
BBANDS_LEN, BBANDS_STD = 20, 2.0
MIN_BB_WIDTH_PCT = 0.8
RSI_LEN, RSI_OVERSOLD = 14, 45 
STOCH_OVERSOLD = 25.0 # Новый параметр для Стохастика
ATR_LEN_FOR_SL, SL_ATR_MUL = 14, 0.5
MIN_RR_RATIO = 0.5

# === INDICATORS ===
def calculate_indicators(df: pd.DataFrame):
    df.ta.adx(length=ADX_LEN, append=True)
    df.ta.bbands(length=BBANDS_LEN, std=BBANDS_STD, append=True)
    df.ta.rsi(length=RSI_LEN, append=True)
    df.ta.atr(length=ATR_LEN_FOR_SL, append=True)
    df.ta.stoch(append=True) # Добавлен расчет стохастика
    return df.dropna()

# === LLM ===
LLM_PROMPT = (
    "Ты — элитный трейдер-аналитик 'Сигма'. Твоя задача — провести глубокий технический анализ предоставленного сетапа "
    "для входа в LONG на отскоке. Используй все предоставленные данные.\n\n"
    "ПЛАН АНАЛИЗА:\n"
    "1.  **Индикаторы:** RSI и Stochastic (STOCHk) в зоне перепроданности? Есть ли признаки выхода из нее?\n"
    "2.  **Бычья дивергенция:** Сравни последние 2-3 минимума цены с минимумами на RSI и Stochastic. Есть ли скрытая или явная бычья дивергенция (цена падает, а индикатор растет)?\n"
    "3.  **Свечной анализ:** Проанализируй данные `last_candles`. Есть ли на последней или предпоследней свече бычьи разворотные паттерны (молот, дожи, бычье поглощение)?\n"
    "4.  **Объемы:** Сопровождается ли падение цены снижением объема, а потенциальный разворот — его увеличением?\n"
    "5.  **Риски:** Какие очевидные риски для входа в сделку прямо сейчас?\n\n"
    "Дай ответ ТОЛЬКО в виде JSON-объекта с полями: 'decision' ('APPROVE'/'REJECT'), 'confidence_score' (0-10, где 10 - максимальная уверенность), "
    "'reasoning' (RU, краткое, но емкое обоснование на основе твоего плана анализа), 'suggested_tp' (число), 'suggested_sl' (число).\n\n"
    "АНАЛИЗИРУЙ СЕТАП:\n{trade_data}"
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
                if len(df) < 50: continue

                df_with_indicators = calculate_indicators(df.copy())
                if len(df_with_indicators) < 2: continue
                
                last = df_with_indicators.iloc[-1]
                
                atr_col_name = next((col for col in last.index if 'ATRr' in col), None)
                if not atr_col_name: continue

                adx_value = last.get(f'ADX_{ADX_LEN}')
                bb_upper = last.get(f'BBU_{BBANDS_LEN}_{BBANDS_STD}')
                bb_lower = last.get(f'BBL_{BBANDS_LEN}_{BBANDS_STD}')
                rsi_value = last.get(f'RSI_{RSI_LEN}')
                atr_value = last.get(atr_col_name)

                if any(v is None for v in [adx_value, bb_upper, bb_lower, rsi_value, atr_value]): continue

                bb_width_pct = ((bb_upper - bb_lower) / bb_lower) * 100
                is_ranging = adx_value < ADX_THRESHOLD
                is_wide_enough = bb_width_pct > MIN_BB_WIDTH_PCT
                is_oversold_at_bottom = last['close'] <= bb_lower and rsi_value < RSI_OVERSOLD

                stoch_k_col = next((col for col in last.index if 'STOCHk' in col), None)
                if not stoch_k_col: continue
                stoch_k_value = last.get(stoch_k_col)
                is_stoch_oversold = stoch_k_value < STOCH_OVERSOLD

                if DEBUG_MODE:
                    log.info(
                        f"[DEBUG] {pair:<12} | "
                        f"Ranging (ADX < {ADX_THRESHOLD}): {is_ranging} (ADX={adx_value:.1f}) | "
                        f"Oversold (RSI < {RSI_OVERSOLD}): {is_oversold_at_bottom} (RSI={rsi_value:.1f}) | "
                        f"Stoch Oversold (STOCHk < {STOCH_OVERSOLD}): {is_stoch_oversold} (STOCHk={stoch_k_value:.1f})"
                    )
                
                if is_ranging and is_wide_enough and is_oversold_at_bottom and is_stoch_oversold:
                    now = datetime.now().timestamp()
                    if (now - state["last_alert_times"].get(pair, 0)) < 3600 * 4: continue

                    candidates.append({
                        "pair": pair, "price": last['close'], "atr": atr_value, 
                        "rsi": round(rsi_value, 1), "bb_lower": bb_lower, 
                        "bb_middle": last[f'BBM_{BBANDS_LEN}_{BBANDS_STD}'],
                        "stoch_k": round(stoch_k_value, 1),
                        "last_candles": df.tail(3)[['open', 'high', 'low', 'close', 'volume']].to_dict('records')
                    })
            except Exception as e:
                log.error(f"Error processing pair {pair}: {e}")
        
        if candidates:
            log.info(f"Found {len(candidates)} candidates. Sending to LLM for analysis...")
            await broadcast_message(app, f"🔍 Найдено {len(candidates)} кандидатов. Отправляю на анализ в LLM...")
            
            approved_signals = []
            for candidate in candidates:
                try:
                    trade_data_for_llm = {
                        "asset": candidate["pair"], "entry_price": candidate["price"],
                        "rsi": candidate["rsi"], "stochastic_k": candidate["stoch_k"],
                        "bb_lower": candidate["bb_lower"], "atr": candidate["atr"],
                        "last_candles": candidate["last_candles"]
                    }
                    llm_response = await ask_llm(trade_data_for_llm)
                    log.info(f"LLM response for {candidate['pair']}: {llm_response.get('decision')}")

                    if llm_response and llm_response.get('decision') == 'APPROVE':
                        candidate['llm_analysis'] = llm_response
                        approved_signals.append(candidate)
                        
                except Exception as e:
                    log.error(f"Error during LLM analysis for {candidate['pair']}: {e}")

            if approved_signals:
                await broadcast_message(app, f"🏆 LLM одобрил {len(approved_signals)} сигнал(а):")
                for signal in approved_signals:
                    asset_name = signal['pair']
                    llm_analysis = signal['llm_analysis']
                    suggested_tp = llm_analysis.get('suggested_tp', signal['bb_middle'])
                    suggested_sl = llm_analysis.get('suggested_sl', signal['price'] - (signal['atr'] * SL_ATR_MUL))

                    message = (
                        f"🔔 <b>СИГНАЛ: LONG (Range Trade)</b>\n\n"
                        f"<b>Монета:</b> <code>{asset_name}</code>\n"
                        f"<b>Цена входа:</b> <code>{signal['price']:.4f}</code>\n\n"
                        f"--- <b>Параметры сделки (от LLM)</b> ---\n"
                        f"<b>Take Profit:</b> <code>{suggested_tp:.4f}</code>\n"
                        f"<b>Stop Loss:</b> <code>{suggested_sl:.4f}</code>\n\n"
                        f"--- <b>Анализ LLM</b> ---\n"
                        f"<i>{llm_analysis.get('reasoning', 'Нет обоснования.')}</i>"
                    )
                    await broadcast_message(app, message)
                    state["last_alert_times"][asset_name] = datetime.now().timestamp()
                    save_state()
                    await asyncio.sleep(1)
            else:
                log.info("LLM did not approve any candidates.")
                await broadcast_message(app, "ℹ️ LLM не одобрил ни одного кандидата.")
        else:
            log.info("No valid candidates found in this scan cycle.")
            await broadcast_message(app, "ℹ️ Сканирование завершено. Подходящих кандидатов не найдено.")
            
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)

# === COMMANDS and RUN ===
async def broadcast_message(app, text):
    for chat_id in app.chat_ids:
        try:
            if hasattr(app, 'chat_ids') and chat_id in app.chat_ids:
                 await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except BadRequest as e:
            log.error(f"HTML parse failed or other BadRequest for chat {chat_id}: {e}")
            await app.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            log.error(f"An unexpected error occurred in broadcast_message to {chat_id}: {e}")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global scanner_task
    chat_id = update.effective_chat.id
    if not hasattr(ctx.application, 'chat_ids'):
        ctx.application.chat_ids = CHAT_IDS
    ctx.application.chat_ids.add(chat_id)
    
    if scanner_task is None or scanner_task.done():
        state['monitoring'] = True
        save_state()
        await update.message.reply_text("✅ Сканер запущен.")
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
        pair = ctx.args[0].upper()
        if "/" not in pair: pair += "/USDT"
        deposit, entry_price, sl, tp = map(float, ctx.args[1:5])
    except (IndexError, ValueError):
        await update.message.reply_text(
            "⚠️ Неверный формат. Используйте:\n"
            "<code>/entry &lt;ТИКЕР&gt; &lt;депозит&gt; &lt;цена&gt; &lt;sl&gt; &lt;tp&gt;</code>\n\n"
            "<b>Пример:</b>\n"
            "<code>/entry SOL/USDT 500 135.5 134.8 138.0</code>", 
            parse_mode="HTML"
        )
        return

    state["manual_position"] = {
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "deposit": deposit, "entry_price": entry_price,
        "sl": sl, "tp": tp, "pair": pair, "side": "LONG"
    }
    save_state()
    await update.message.reply_text(f"✅ Вход вручную зафиксирован: <b>{pair}</b> @ <b>{entry_price}</b>", parse_mode="HTML")

async def cmd_exit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global state
    pos = state.get("manual_position")
    if not pos:
        await update.message.reply_text("⚠️ Нет открытой ручной позиции для закрытия.")
        return
    try:
        exit_deposit = float(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text(
            "⚠️ Неверный формат. Используйте: <code>/exit &lt;итоговый_депозит&gt;</code>", 
            parse_mode="HTML"
        )
        return

    initial_deposit = pos.get('deposit', 0)
    pnl = exit_deposit - initial_deposit
    pct_change = (pnl / initial_deposit) * 100 if initial_deposit != 0 else 0
    
    if LOGS_WS:
        try:
            rr = abs((pos['tp'] - pos['entry_price']) / (pos['sl'] - pos['entry_price'])) if (pos.get('sl') and pos.get('entry_price') and (pos['sl'] - pos['entry_price']) != 0) else 0
            row = [
                datetime.fromisoformat(pos['entry_time']).strftime('%Y-%m-%d %H:%M:%S'),
                pos.get('pair', 'N/A'), pos.get("side", "N/A"),
                initial_deposit, pos.get('entry_price', 'N/A'),
                pos.get('sl', 'N/A'), pos.get('tp', 'N/A'),
                round(rr, 2), round(pnl, 2), round(pct_change, 2)
            ]
            await asyncio.to_thread(LOGS_WS.append_row, row, value_input_option='USER_ENTERED')
        except Exception as e:
            log.error("Failed to write to Google Sheets: %s", e)
            await update.message.reply_text("⚠️ Ошибка записи в Google Таблицу.")

    await update.message.reply_text(
        f"✅ Сделка по <b>{pos.get('pair', 'N/A')}</b> закрыта и записана.\n"
        f"<b>P&L: {pnl:+.2f} USDT ({pct_change:+.2f}%)</b>", 
        parse_mode="HTML"
    )
    state["manual_position"] = None
    save_state()

if __name__ == "__main__":
    load_state()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = CHAT_IDS
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("entry", cmd_entry))
    app.add_handler(CommandHandler("exit", cmd_exit))

    log.info("Bot started...")
    app.run_polling()
