#!/usr/bin/env python3
# ============================================================================
# v17.0 - Bi-Directional Momentum Strategy
# • Добавлена логика для поиска и анализа SHORT-сигналов.
# • Стратегия теперь работает в обе стороны в зависимости от тренда на H1.
# • Промпт LLM и сообщения в Telegram стали универсальными (LONG/SHORT).
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
exchange = ccxt.mexc({
    'options': {
        'defaultType': 'swap',
    },
})

# === STRATEGY PARAMS ===
TIMEFRAME_TREND = '1h'
TIMEFRAME_ENTRY = '5m'
SCAN_INTERVAL_SECONDS = 60 * 15
EMA_TREND_LEN = 50
EMA_SHORT_LEN = 9
EMA_LONG_LEN = 21
ADX_LEN = 14
ADX_MOMENTUM_THRESHOLD = 20
MIN_RR_RATIO = 1.2

# === INDICATORS ===
def calculate_indicators(df: pd.DataFrame, is_trend_tf: bool):
    df.ta.adx(length=ADX_LEN, append=True)
    if is_trend_tf:
        df.ta.ema(length=EMA_TREND_LEN, append=True)
    else:
        df.ta.ema(length=EMA_SHORT_LEN, append=True)
        df.ta.ema(length=EMA_LONG_LEN, append=True)
    return df.dropna()

# === LLM ===
LLM_PROMPT = (
    "Ты — элитный трейдер-аналитик 'Сигма'. Тебе поступил кандидат на открытие позиции. Твоя задача — провести полный анализ и принять взвешенное решение.\n\n"
    "АЛГОРИТМ АНАЛИЗА:\n"
    "1.  **Направление сделки:** Кандидат предложен на `{trade_data['side']}`. \n"
    "2.  **Глобальный тренд (H1):** Соответствует ли направление сделки глобальному тренду (цена выше EMA для LONG, ниже EMA для SHORT)? Это обязательное условие.\n"
    "3.  **Сигнал входа (5M):** Сигнал основан на пересечении EMA. Оцени, насколько 'чистым' и уверенным было пересечение. Не является ли оно ложным?\n"
    "4.  **Потенциал движения:** Определи ближайшие ключевые уровни поддержки и сопротивления. Есть ли у цены 'пространство для движения' до следующего уровня?\n"
    "5.  **Risk/Reward:** Установи логичный SL (для LONG — под минимумом, для SHORT — над максимумом). Установи TP перед следующим уровнем поддержки/сопротивления. Рассчитай R:R.\n"
    f"6.  **Решение:** Одобряй (`APPROVE`) только если все условия выполнены и R:R >= {MIN_RR_RATIO}. В остальных случаях — `REJECT`.\n\n"
    "Дай ответ ТОЛЬКО в виде JSON-объекта с полями: 'decision', 'confidence_score', 'reasoning', 'suggested_tp', 'suggested_sl'.\n\n"
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
        log.error(f"LLM request/parse err: %s", e)
        return {"decision": "ERROR", "reasoning": str(e)}

# === MAIN SCANNER LOOP ===
async def scanner_loop(app):
    await broadcast_message(app, "🤖 Сканер запущен. Стратегия: Импульс (LONG/SHORT).")
    try:
        await exchange.load_markets()
        tickers = await exchange.fetch_tickers()
        
        # --- НАЧАЛО БЛОКА ДЛЯ ЗАМЕНЫ ---
        
        # Фильтр для фьючерсных пар вида "BTC/USDT:USDT"
        usdt_pairs = {s: t for s, t in tickers.items() if s.endswith(':USDT') and t.get('quoteVolume') and all(kw not in s for kw in ['UP/', 'DOWN/', 'BEAR/', 'BULL/'])}
        
        sorted_pairs = sorted(usdt_pairs.items(), key=lambda item: item[1]['quoteVolume'], reverse=True)
        coin_list = [item[0] for item in sorted_pairs[:COIN_LIST_SIZE]]
        
        # --- КОНЕЦ БЛОКА ДЛЯ ЗАМЕНЫ ---

        await broadcast_message(app, f"✅ Список из {len(coin_list)} монет для сканирования сформирован.")
    except Exception as e:
        log.error(f"Failed to fetch dynamic coin list: %s", e)
        await broadcast_message(app, "⚠️ Не удалось сформировать динамический список монет. Проверьте лог."); return
        
    while state.get('monitoring', False):
        log.info(f"Starting new scan for {len(coin_list)} coins...")
        candidates = []
        for pair in coin_list:
            try:
                # 1. Получаем данные со старшего ТФ для определения тренда
                ohlcv_h1 = await exchange.fetch_ohlcv(pair, timeframe=TIMEFRAME_TREND, limit=100)
                df_h1 = pd.DataFrame(ohlcv_h1, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df_h1_with_indicators = calculate_indicators(df_h1.copy(), is_trend_tf=True)
                if len(df_h1_with_indicators) < 2: continue
                
                last_h1 = df_h1_with_indicators.iloc[-1]
                ema_trend = last_h1.get(f'EMA_{EMA_TREND_LEN}')
                if ema_trend is None: continue

                is_global_uptrend = last_h1['close'] > ema_trend
                is_global_downtrend = last_h1['close'] < ema_trend

                if not is_global_uptrend and not is_global_downtrend:
                    if DEBUG_MODE: log.info(f"[DEBUG] {pair:<12} | Trend Filter: NEUTRAL")
                    continue

                # 2. Получаем данные с младшего ТФ для точки входа
                ohlcv_5m = await exchange.fetch_ohlcv(pair, timeframe=TIMEFRAME_ENTRY, limit=100)
                df_5m = pd.DataFrame(ohlcv_5m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df_5m_with_indicators = calculate_indicators(df_5m.copy(), is_trend_tf=False)
                if len(df_5m_with_indicators) < 2: continue

                prev_5m = df_5m_with_indicators.iloc[-2]
                last_5m = df_5m_with_indicators.iloc[-1]

                ema_short = last_5m.get(f'EMA_{EMA_SHORT_LEN}')
                ema_long = last_5m.get(f'EMA_{EMA_LONG_LEN}')
                prev_ema_short = prev_5m.get(f'EMA_{EMA_SHORT_LEN}')
                prev_ema_long = prev_5m.get(f'EMA_{EMA_LONG_LEN}')
                adx_value = last_5m.get(f'ADX_{ADX_LEN}')

                if any(v is None for v in [ema_short, ema_long, prev_ema_short, prev_ema_long, adx_value]): continue
                
                is_momentum_strong = adx_value > ADX_MOMENTUM_THRESHOLD
                side = None
                
                if is_global_uptrend:
                    ema_crossed_up = prev_ema_short <= prev_ema_long and ema_short > ema_long
                    if is_momentum_strong and ema_crossed_up:
                        side = 'LONG'
                        if DEBUG_MODE: log.info(f"[DEBUG] {pair:<12} | H1 Trend: UP | Found LONG candidate (EMA Cross Up)")
                elif is_global_downtrend:
                    ema_crossed_down = prev_ema_short >= prev_ema_long and ema_short < ema_long
                    if is_momentum_strong and ema_crossed_down:
                        side = 'SHORT'
                        if DEBUG_MODE: log.info(f"[DEBUG] {pair:<12} | H1 Trend: DOWN | Found SHORT candidate (EMA Cross Down)")

                if side:
                    now = datetime.now().timestamp()
                    if (now - state["last_alert_times"].get(pair, 0)) < 3600 * 4: continue
                    
                    candidates.append({
                        "pair": pair, "price": last_5m['close'], 
                        "side": side,
                        "ema_trend_h1": ema_trend,
                        "last_candles": df_5m_with_indicators.tail(10)[['open', 'high', 'low', 'close', 'volume']].to_dict('records')
                    })
            except Exception as e:
                log.error(f"Error processing pair {pair}: {e}")
        
        if candidates:
            log.info(f"Found {len(candidates)} candidates. Sending to LLM for analysis...")
            await broadcast_message(app, f"🔍 Найдено {len(candidates)} кандидатов. Отправляю на анализ в LLM...")
            
            approved_signals_count = 0
            for candidate in candidates:
                try:
                    trade_data_for_llm = {
                        "asset": candidate["pair"], 
                        "entry_price": candidate["price"],
                        "side": candidate["side"],
                        "ema_trend_h1": candidate["ema_trend_h1"],
                        "last_candles": candidate["last_candles"]
                    }
                    llm_response = await ask_llm(trade_data_for_llm)
                    log.info(f"LLM response for {candidate['pair']}: {llm_response.get('decision')}")

                    if llm_response and llm_response.get('decision') == 'APPROVE':
                        approved_signals_count += 1
                        asset_name = candidate['pair']
                        side = candidate.get('side', 'N/A')
                        suggested_tp = llm_response.get('suggested_tp', 0)
                        suggested_sl = llm_response.get('suggested_sl', 0)

                        message = (
                            f"🔔 <b>СИГНАЛ: {side}</b>\n\n"
                            f"<b>Монета:</b> <code>{asset_name}</code>\n"
                            f"<b>Цена входа:</b> <code>{candidate['price']:.4f}</code>\n\n"
                            f"--- <b>Параметры сделки (от LLM)</b> ---\n"
                            f"<b>Take Profit:</b> <code>{suggested_tp:.4f}</code>\n"
                            f"<b>Stop Loss:</b> <code>{suggested_sl:.4f}</code>\n\n"
                            f"--- <b>Анализ LLM</b> ---\n"
                            f"<i>{llm_response.get('reasoning', 'Нет обоснования.')}</i>"
                        )
                        await broadcast_message(app, message)
                        state["last_alert_times"][asset_name] = datetime.now().timestamp()
                        save_state()
                    
                    elif llm_response and llm_response.get('decision') == 'REJECT':
                        reason = llm_response.get('reasoning', 'Причина не указана.')
                        side = candidate.get('side', 'N/A')
                        message = (
                            f"🚫 <b>СИГНАЛ {side} ОТКЛОНЕН LLM</b>\n\n"
                            f"<b>Монета:</b> <code>{candidate['pair']}</code>\n"
                            f"<b>Причина:</b> <i>{reason}</i>"
                        )
                        await broadcast_message(app, message)

                    await asyncio.sleep(1)
                except Exception as e:
                    log.error(f"Error during LLM analysis for {candidate['pair']}: {e}")

            if approved_signals_count == 0:
                 await broadcast_message(app, f"ℹ️ Анализ {len(candidates)} кандидатов завершен. LLM не одобрил ни одного.")
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
