#!/usr/bin/env python3
# ============================================================================
# v3.0 - Sniper Assistant
# • Новая логика: бот ищет готовый сетап (EMA Cross) и сразу отправляет в LLM.
# • Убран режим "фокуса". LLM сразу генерирует SL/TP с проверкой R:R >= 1.5.
# • Значительно ускорен цикл "поиск-сигнал".
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
COIN_LIST_SIZE = int(os.getenv("COIN_LIST_SIZE", "300"))
WORKSHEET_NAME = "Trading_Logs_v10"

LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_API_URL = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_MODEL_ID = os.getenv("LLM_MODEL_ID", "gpt-4.1")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for n in ("httpx", "httpcore"): logging.getLogger(n).setLevel(logging.WARNING)

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
STATE_FILE = "assistant_bot_state.json"
state = {}
scanner_task = None
def save_state():
    with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2)
def load_state():
    global state
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f: state = json.load(f)
    if 'mode' not in state:
        state.update({
            "bot_on": False,
            "mode": "SEARCHING", # SEARCHING, AWAITING_ENTRY, POSITION_OPEN
            "current_position": None,
            "last_signal": None
        })
    log.info(f"State loaded: {state}")

# === EXCHANGE ===
exchange = ccxt.mexc({'options': {'defaultType': 'swap'}})

# === LLM PROMPTS & FUNCTION ===
PROMPT_GENERATE_SETUP = (
    "Ты — трейдер-аналитик. Тебе поступил сигнал о пересечении 9/21 EMA на 5-минутном графике. "
    "Твоя задача — провести финальный анализ и, если все условия выполнены, сгенерировать готовый торговый сетап.\n\n"
    "**Чек-лист для анализа:**\n"
    "1.  **Глобальный тренд (H1):** Соответствует ли направление пересечения (LONG/SHORT) тренду на часовом графике (цена выше/ниже 50 EMA)?\n"
    "2.  **Качество сигнала:** Пересечение уверенное? Сопровождается ли оно увеличением объема? Нет ли рядом сильных уровней, которые могут помешать движению?\n"
    "3.  **Риск/Прибыль (R:R):** Рассчитай логичный SL (за локальным экстремумом) и TP (перед следующим уровнем). "
    "Соотношение R:R должно быть **строго >= 1.5**.\n\n"
    "**Решение:**\n"
    "- Если ВСЕ пункты чек-листа пройдены, ответь: {'decision': 'APPROVE', 'side': '...', 'reason': 'краткое обоснование', 'sl': число, 'tp': число}.\n"
    "- Если хотя бы один пункт не пройден (особенно R:R), ответь: {'decision': 'REJECT', 'reason': 'укажи главную причину отказа'}."
)
PROMPT_MANAGE_POSITION = (
    "Ты — риск-менеджер. Ты ведешь открытую позицию {side} по {asset} от цены {entry_price}. "
    "Анализируй каждую новую свечу. Если позиция развивается по плану, ответь {'decision': 'HOLD'}. "
    "Если видишь тревожные сигналы (например, резкий разворот, появление сильного противоположного паттерна), немедленно дай команду на закрытие: "
    "{'decision': 'CLOSE', 'reason': 'обоснование'}."
)

async def ask_llm(final_prompt: str):
    if not LLM_API_KEY: return None
    payload = {"model": LLM_MODEL_ID, "messages": [{"role": "user", "content": final_prompt}], "temperature": 0.4, "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, json=payload, headers=headers, timeout=120) as r:
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

# === MAIN BOT LOGIC ===
async def main_loop(app):
    while state.get('bot_on', False):
        try:
            if state['mode'] == 'SEARCHING':
                await run_searching_phase(app)
                await asyncio.sleep(60 * 5) # Ищем сетапы каждые 5 минут
            elif state['mode'] == 'AWAITING_ENTRY':
                await run_awaiting_entry_phase(app)
                await asyncio.sleep(60) # Проверяем таймаут раз в минуту
            elif state['mode'] == 'POSITION_OPEN':
                await run_monitoring_phase(app)
                await asyncio.sleep(45) # Мониторим позицию каждые 45 секунд
            else:
                log.error(f"Unknown bot mode: {state['mode']}. Resetting.")
                state['mode'] = 'SEARCHING'; save_state()
        except Exception as e:
            log.error(f"Critical error in main_loop: {e}", exc_info=True)
            await broadcast_message(app, f"⚠️ Критическая ошибка в главном цикле: {e}")
            await asyncio.sleep(60)

async def run_searching_phase(app):
    log.info("--- Mode: SEARCHING for EMA Crossovers ---")
    await broadcast_message(app, f"<b>Этап 1:</b> Ищу монеты с пересечением 9/21 EMA...")
    try:
        tickers = await exchange.fetch_tickers()
        usdt_pairs = {s: t for s, t in tickers.items() if s.endswith(':USDT') and t.get('quoteVolume')}
        sorted_pairs = sorted(usdt_pairs.items(), key=lambda item: item[1]['quoteVolume'], reverse=True)
        coin_list = [item[0] for item in sorted_pairs[:COIN_LIST_SIZE]]
        
        for pair in coin_list:
            if not state.get('bot_on'): break
            try:
                ohlcv_5m = await exchange.fetch_ohlcv(pair, timeframe='5m', limit=50)
                df_5m = pd.DataFrame(ohlcv_5m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                if len(df_5m) < 22: continue

                df_5m.ta.ema(length=9, append=True); df_5m.ta.ema(length=21, append=True)
                prev_5m = df_5m.iloc[-2]; last_5m = df_5m.iloc[-1]

                ema_short = last_5m.get('EMA_9'); ema_long = last_5m.get('EMA_21')
                prev_ema_short = prev_5m.get('EMA_9'); prev_ema_long = prev_5m.get('EMA_21')
                if any(v is None for v in [ema_short, ema_long, prev_ema_short, prev_ema_long]): continue

                is_cross_up = prev_ema_short <= prev_ema_long and ema_short > ema_long
                is_cross_down = prev_ema_short >= prev_ema_long and ema_short < ema_long

                if is_cross_up or is_cross_down:
                    log.info(f"Found EMA Crossover on {pair}. Collecting deep data...")
                    await broadcast_message(app, f"Нашел пересечение на <code>{pair}</code>. Анализирую глубже...")
                    
                    ohlcv_h1 = await exchange.fetch_ohlcv(pair, timeframe='1h', limit=100)
                    df_h1 = pd.DataFrame(ohlcv_h1, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df_h1.ta.ema(length=50, append=True)
                    
                    llm_data_str = json.dumps({"h1_data": df_h1.to_dict('records'), "m5_data": df_5m.to_dict('records')})
                    prompt_text = PROMPT_GENERATE_SETUP + "\n\nДАННЫЕ ДЛЯ АНАЛИЗА:\n" + llm_data_str
                    llm_response = await ask_llm(prompt_text)
                    
                    if llm_response and llm_response.get('decision') == 'APPROVE':
                        state['last_signal'] = llm_response
                        state['last_signal']['pair'] = pair
                        state['last_signal']['timestamp'] = datetime.now().timestamp()
                        state['mode'] = 'AWAITING_ENTRY'
                        save_state()
                        
                        side = llm_response.get('side', 'N/A'); reason = llm_response.get('reason', 'N/A')
                        sl = llm_response.get('sl', 0); tp = llm_response.get('tp', 0)
                        
                        message = (
                            f"🔔 <b>ГОТОВЫЙ СЕТАП!</b> 🔔\n\n"
                            f"<b>Монета:</b> <code>{pair}</code>\n<b>Направление:</b> <b>{side}</b>\n"
                            f"<b>Take Profit:</b> <code>{tp}</code>\n<b>Stop Loss:</b> <code>{sl}</code>\n\n"
                            f"<b>Обоснование LLM:</b> <i>{reason}</i>\n\n"
                            f"👉 Откройте сделку и подтвердите вход командой <code>/entry</code>. Сетап актуален 15 минут."
                        )
                        await broadcast_message(app, message)
                        return # Выходим из функции, чтобы главный цикл перешел в режим AWAITING_ENTRY
                    else:
                        reason = llm_response.get('reason', 'Причина не указана.') if llm_response else "LLM не ответил."
                        await broadcast_message(app, f"🚫 LLM отклонил сетап по <code>{pair}</code>. Причина: <i>{reason}</i>")

                await asyncio.sleep(0.5)
            except Exception as e:
                log.warning(f"Could not process {pair}: {e}")
    except Exception as e:
        log.error(f"Critical error in Searching phase: {e}", exc_info=True)
        await broadcast_message(app, f"⚠️ Ошибка на этапе сканирования: {e}")

async def run_awaiting_entry_phase(app):
    log.info(f"--- Mode: AWAITING_ENTRY for {state.get('last_signal', {}).get('pair')} ---")
    signal_time = state.get('last_signal', {}).get('timestamp', 0)
    if (datetime.now().timestamp() - signal_time) > 60 * 15: # 15 минут таймаут
        pair = state['last_signal']['pair']
        log.info(f"Signal for {pair} expired.")
        state['last_signal'] = None
        state['mode'] = 'SEARCHING'
        save_state()
        await broadcast_message(app, f"ℹ️ Сигнал по <code>{pair}</code> истек. Возобновляю поиск.")

async def run_monitoring_phase(app):
    log.info(f"--- Mode: POSITION_OPEN on {state.get('current_position', {}).get('pair')} ---")
    pos = state.get('current_position')
    if not pos:
        state['mode'] = 'SEARCHING'; save_state(); return
    try:
        prompt_text = PROMPT_MANAGE_POSITION.format(asset=pos['pair'], side=pos['side'], entry_price=pos['entry_price'])
        llm_response = await ask_llm(prompt_text)
        if llm_response and llm_response.get('decision') == 'CLOSE':
            message = (
                f"⚠️ <b>РЕКОМЕНДАЦИЯ: ЗАКРЫТЬ ПОЗИЦИЮ!</b> ⚠️\n\n"
                f"<b>Монета:</b> <code>{pos['pair']}</code>\n"
                f"<b>Причина от LLM:</b> <i>{llm_response.get('reason', 'N/A')}</i>\n\n"
                f"👉 Закройте сделку и подтвердите выход командой <code>/exit</code>."
            )
            await broadcast_message(app, message)
    except Exception as e:
        log.error(f"Error in monitoring phase for {pos['pair']}: {e}", exc_info=True)

# === COMMANDS and RUN ===
async def broadcast_message(app, text):
    chat_ids = getattr(app, 'chat_ids', CHAT_IDS)
    for chat_id in chat_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception as e:
            log.error(f"Failed to send message to {chat_id}: {e}")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global scanner_task
    chat_id = update.effective_chat.id
    if not hasattr(ctx.application, 'chat_ids'):
        ctx.application.chat_ids = set()
    ctx.application.chat_ids.add(chat_id)
    
    if not state.get('bot_on'):
        state['bot_on'] = True; state['mode'] = 'SEARCHING'; save_state()
        await update.message.reply_text("✅ Ассистент запущен. Начинаю поиск сетапов...")
        if scanner_task is None or scanner_task.done():
            scanner_task = asyncio.create_task(main_loop(ctx.application))
    else:
        await update.message.reply_text("ℹ️ Ассистент уже запущен.")

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state.get('bot_on'):
        state['bot_on'] = False; save_state()
        await update.message.reply_text("❌ Ассистент остановлен. Все циклы завершатся.")
    else:
        await update.message.reply_text("ℹ️ Ассистент уже был остановлен.")

async def cmd_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state['mode'] != 'AWAITING_ENTRY' or not state.get('last_signal'):
        await update.message.reply_text("⚠️ Нет активного сигнала для подтверждения входа.")
        return
    try:
        entry_price = float(ctx.args[0]); deposit = float(ctx.args[1])
        signal = state['last_signal']
        
        state['current_position'] = {
            "entry_time": datetime.now(timezone.utc).isoformat(), "pair": signal.get('pair'),
            "side": signal.get('side'), "deposit": deposit, "entry_price": entry_price,
            "sl": signal.get('sl'), "tp": signal.get('tp')
        }
        state['mode'] = 'POSITION_OPEN'; state['last_signal'] = None; save_state()
        
        pos = state['current_position']
        await update.message.reply_text(f"✅ Позиция <b>{pos.get('side')}</b> по <b>{pos.get('pair')}</b> зафиксирована.\nНачинаю сопровождение.")
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат: <code>/entry &lt;цена_входа&gt; &lt;депозит&gt;</code>", parse_mode="HTML")

async def cmd_exit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state['mode'] != 'POSITION_OPEN' or not state.get('current_position'):
        await update.message.reply_text("⚠️ Нет открытой позиции для закрытия.")
        return
    try:
        exit_deposit = float(ctx.args[0])
        pos = state['current_position']

        initial_deposit = pos.get('deposit', 0)
        pnl = exit_deposit - initial_deposit
        pct_change = (pnl / initial_deposit) * 100 if initial_deposit != 0 else 0
        
        if LOGS_WS:
            row = [
                datetime.fromisoformat(pos['entry_time']).strftime('%Y-%m-%d %H:%M:%S'),
                pos.get('pair'), pos.get("side"), initial_deposit,
                pos.get('entry_price'), pos.get('sl'), pos.get('tp'),
                round(pnl, 2), round(pct_change, 2)
            ]
            await asyncio.to_thread(LOGS_WS.append_row, row, value_input_option='USER_ENTERED')

        await update.message.reply_text(f"✅ Сделка по <b>{pos.get('pair')}</b> закрыта. P&L: <b>{pnl:+.2f} USDT ({pct_change:+.2f}%)</b>")
        
        state['current_position'] = None; state['mode'] = 'SEARCHING'; save_state()
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат: <code>/exit &lt;итоговый_депозит&gt;</code>", parse_mode="HTML")

if __name__ == "__main__":
    load_state()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = CHAT_IDS
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("entry", cmd_entry))
    app.add_handler(CommandHandler("exit", cmd_exit))

    log.info("Sniper Assistant starting...")
    if state.get('bot_on', False):
        scanner_task = asyncio.create_task(main_loop(app))
    app.run_polling()
