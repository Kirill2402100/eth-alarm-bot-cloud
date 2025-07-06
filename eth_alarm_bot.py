#!/usr/bin/env python3
# ============================================================================
# v1.0 - Interactive Trading Assistant
# • Полностью новая архитектура на основе состояний (поиск, фокус, позиция).
# • Бот выбирает один инструмент и следит за ним в реальном времени.
# • Взаимодействие через команды /entry и /exit для управления позицией.
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for n in ("httpx",): logging.getLogger(n).setLevel(logging.WARNING)

# === GOOGLE SHEETS ===
def setup_google_sheets():
    try:
        # ... (код без изменений)
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
    # Устанавливаем начальное состояние по умолчанию
    if 'mode' not in state:
        state.update({
            "bot_on": False,
            "mode": "SEARCHING", # SEARCHING, FOCUSED, POSITION_OPEN
            "focus_coin": None,
            "current_position": None,
            "last_signal": None,
            "last_focus_time": 0
        })
    log.info(f"State loaded: {state}")

# === EXCHANGE ===
exchange = ccxt.mexc({'options': {'defaultType': 'swap'}})

# === LLM PROMPTS & FUNCTION ===
PROMPT_SELECT_FOCUS = (
    "Ты — трейдер-аналитик. Тебе предоставлен список криптовалютных пар с высокой волатильностью. "
    "Твоя задача — выбрать из этого списка ОДНУ наиболее перспективную монету для наблюдения в ближайший час с целью поиска входа в LONG или SHORT. "
    "Ищи монеты, которые приближаются к сильным уровням или показывают явные признаки подготовки к импульсу. "
    "Ответь ТОЛЬКО в виде JSON-объекта с одним полем: 'focus_coin', например: {'focus_coin': 'BTC/USDT:USDT'}."
)
PROMPT_FIND_ENTRY = (
    "Ты — снайпер, следящий за монетой {asset}. Твоя задача — дать сигнал на вход в тот самый момент, когда формируется идеальный сетап. "
    "Анализируй последние свечи, объемы, ищи паттерны, дивергенции, пробои или отскоки от уровней. "
    "Если идеального момента для входа нет, ответь {'decision': 'WAIT'}. "
    "Если нашел идеальный сетап, ответь {'decision': 'ENTER', 'side': 'LONG' или 'SHORT', 'reason': 'краткое обоснование', 'sl': число, 'tp': число}."
)
PROMPT_MANAGE_POSITION = (
    "Ты — риск-менеджер. Ты ведешь открытую позицию {side} по {asset} от цены {entry_price}. "
    "Анализируй каждую новую свечу. Если позиция развивается по плану, ответь {'decision': 'HOLD'}. "
    "Если видишь тревожные сигналы (например, резкий разворот, появление сильного противоположного паттерна), немедленно дай команду на закрытие: "
    "{'decision': 'CLOSE', 'reason': 'краткое обоснование почему нужно закрыться'}. Не жди стоп-лосса, если видишь опасность."
)

async def ask_llm(prompt, data):
    # ... (код функции ask_llm без изменений, только промпт передается как аргумент)
    final_prompt = prompt.format(**data)
    payload = {"model": LLM_MODEL_ID, "messages": [{"role": "user", "content": final_prompt}], "temperature": 0.4, "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, json=payload, headers=headers, timeout=90) as r:
                # ... (остальной код функции без изменений)
                txt = await r.text()
                if r.status != 200:
                    log.error(f"LLM HTTP Error {r.status}: {txt}")
                    return None
                response_json = json.loads(txt)
                content_str = response_json["choices"][0]["message"]["content"]
                return json.loads(content_str.strip().strip("`"))
    except Exception as e:
        log.error(f"LLM request/parse err: %s", e)
        return None

# === MAIN BOT LOGIC ===
async def main_loop(app):
    while state.get('bot_on', False):
        try:
            if state['mode'] == 'SEARCHING':
                await run_searching_phase(app)
                await asyncio.sleep(60 * 15) # Ищем новую цель раз в 15 минут
            elif state['mode'] == 'FOCUSED':
                await run_focused_phase(app)
                await asyncio.sleep(30) # Следим за целью каждые 30 секунд
            elif state['mode'] == 'POSITION_OPEN':
                await run_monitoring_phase(app)
                await asyncio.sleep(30) # Мониторим позицию каждые 30 секунд
            else:
                log.error(f"Unknown bot mode: {state['mode']}")
                state['mode'] = 'SEARCHING' # Сброс в безопасное состояние
                await asyncio.sleep(60)
        except Exception as e:
            log.error(f"Error in main_loop: {e}")
            await broadcast_message(app, f"⚠️ Критическая ошибка в главном цикле: {e}")
            await asyncio.sleep(60)

async def run_searching_phase(app):
    log.info("--- Mode: SEARCHING ---")
    await broadcast_message(app, "🔍 Ищу новую перспективную монету для наблюдения...")
    try:
        tickers = await exchange.fetch_tickers()
        usdt_pairs = {s: t for s, t in tickers.items() if s.endswith(':USDT') and t.get('quoteVolume')}
        sorted_pairs = sorted(usdt_pairs.items(), key=lambda item: item[1]['quoteVolume'], reverse=True)
        top_coins = [item[0] for item in sorted_pairs[:50]]
        
        # Запрашиваем фокусную монету у LLM
        llm_response = await ask_llm(PROMPT_SELECT_FOCUS, {"asset_list": top_coins})
        
        # ---> НАЧАЛО НОВОГО БЛОКА ПРОВЕРОК И ЛОГИРОВАНИЯ <---
        
        # Логируем сырой ответ от LLM, чтобы видеть, что он присылает
        log.info(f"Raw LLM response for focus selection: {llm_response}")

        # Делаем проверки более надежными
        if llm_response and isinstance(llm_response, dict) and 'focus_coin' in llm_response:
            state['focus_coin'] = llm_response['focus_coin']
            state['mode'] = 'FOCUSED'
            state['last_focus_time'] = datetime.now().timestamp()
            log.info(f"LLM selected new focus coin: {state['focus_coin']}")
            await broadcast_message(app, f"🎯 Новая цель для наблюдения: <b>{state['focus_coin']}</b>. Начинаю слежение в реальном времени.")
            save_state()
        else:
            log.error(f"Failed to get a valid focus coin from LLM. Response was: {llm_response}")
            await broadcast_message(app, "⚠️ Не удалось получить валидную цель от LLM. Попробую снова через некоторое время.")

        # ---> КОНЕЦ НОВОГО БЛОКА <---

    except Exception as e:
        log.error(f"Critical error in searching phase: {e}", exc_info=True) # Добавляем полный traceback для лучшей диагностики
        
async def run_focused_phase(app):
    log.info(f"--- Mode: FOCUSED on {state['focus_coin']} ---")
    if not state['focus_coin']:
        state['mode'] = 'SEARCHING'
        return
        
    # Таймаут: если за 15 минут не было сигнала, ищем новую цель
    if (datetime.now().timestamp() - state.get('last_focus_time', 0)) > 60 * 15:
        await broadcast_message(app, f"⚠️ Не найдено подходящего сетапа по {state['focus_coin']} за 15 минут. Ищу новую цель.")
        state['mode'] = 'SEARCHING'
        return

    try:
        ohlcv = await exchange.fetch_ohlcv(state['focus_coin'], timeframe='5m', limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        llm_data = {"asset": state['focus_coin'], "candles_data": df.to_dict('records')}
        llm_response = await ask_llm(PROMPT_FIND_ENTRY, llm_data)

        if llm_response and llm_response.get('decision') == 'ENTER':
            state['last_signal'] = llm_response
            side = llm_response.get('side')
            reason = llm_response.get('reason')
            sl = llm_response.get('sl')
            tp = llm_response.get('tp')
            
            message = (
                f"🔔 <b>ВНИМАНИЕ, СЕТАП!</b> 🔔\n\n"
                f"<b>Монета:</b> <code>{state['focus_coin']}</code>\n"
                f"<b>Направление:</b> <b>{side}</b>\n"
                f"<b>Take Profit:</b> <code>{tp}</code>\n"
                f"<b>Stop Loss:</b> <code>{sl}</code>\n\n"
                f"<b>Обоснование LLM:</b> <i>{reason}</i>\n\n"
                f"👉 Откройте сделку и подтвердите вход командой <code>/entry</code>."
            )
            await broadcast_message(app, message)
            # Мы не меняем режим, ждем команду /entry от пользователя
            save_state()

    except Exception as e:
        log.error(f"Error in focused phase for {state['focus_coin']}: {e}")

async def run_monitoring_phase(app):
    log.info(f"--- Mode: POSITION_OPEN on {state['current_position']['pair']} ---")
    pos = state['current_position']
    try:
        ohlcv = await exchange.fetch_ohlcv(pos['pair'], timeframe='1m', limit=100) # Мониторим на минутках
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        llm_data = {"asset": pos['pair'], "side": pos['side'], "entry_price": pos['entry_price'], "candles_data": df.to_dict('records')}
        llm_response = await ask_llm(PROMPT_MANAGE_POSITION, llm_data)

        if llm_response and llm_response.get('decision') == 'CLOSE':
            reason = llm_response.get('reason')
            message = (
                f"⚠️ <b>РЕКОМЕНДАЦИЯ: ЗАКРЫТЬ ПОЗИЦИЮ!</b> ⚠️\n\n"
                f"<b>Монета:</b> <code>{pos['pair']}</code>\n"
                f"<b>Причина от LLM:</b> <i>{reason}</i>\n\n"
                f"👉 Закройте сделку и подтвердите выход командой <code>/exit</code>."
            )
            await broadcast_message(app, message)
            # Режим не меняем, ждем /exit
    except Exception as e:
        log.error(f"Error in monitoring phase for {pos['pair']}: {e}")

# === COMMANDS and RUN ===
async def broadcast_message(app, text):
    for chat_id in app.chat_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception as e:
            log.error(f"Failed to send message to {chat_id}: {e}")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global scanner_task
    chat_id = update.effective_chat.id
    if not hasattr(ctx.application, 'chat_ids'):
        ctx.application.chat_ids = CHAT_IDS
    ctx.application.chat_ids.add(chat_id)
    
    if not state['bot_on']:
        state['bot_on'] = True
        state['mode'] = 'SEARCHING' # Начинаем всегда с поиска
        save_state()
        await update.message.reply_text("✅ Ассистент запущен. Начинаю поиск цели...")
        if scanner_task is None or scanner_task.done():
            scanner_task = asyncio.create_task(main_loop(ctx.application))
    else:
        await update.message.reply_text("ℹ️ Ассистент уже запущен.")

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state['bot_on']:
        state['bot_on'] = False
        save_state()
        await update.message.reply_text("❌ Ассистент остановлен. Все циклы завершатся.")
    else:
        await update.message.reply_text("ℹ️ Ассистент уже был остановлен.")

async def cmd_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state['mode'] != 'FOCUSED' or not state['last_signal']:
        await update.message.reply_text("⚠️ Нет активного сигнала для подтверждения входа. Дождитесь сигнала.")
        return
    try:
        # Формат: /entry цена_входа депозит
        entry_price = float(ctx.args[0])
        deposit = float(ctx.args[1])
        
        signal = state['last_signal']
        state['current_position'] = {
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "pair": state['focus_coin'],
            "side": signal['side'],
            "deposit": deposit,
            "entry_price": entry_price,
            "sl": signal['sl'],
            "tp": signal['tp']
        }
        state['mode'] = 'POSITION_OPEN'
        state['last_signal'] = None
        save_state()
        await update.message.reply_text(
            f"✅ Позиция <b>{state['current_position']['side']}</b> по <b>{state['current_position']['pair']}</b> зафиксирована.\n"
            f"Начинаю сопровождение."
        )
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат. Используйте: <code>/entry &lt;цена_входа&gt; &lt;депозит&gt;</code>", parse_mode="HTML")

async def cmd_exit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state['mode'] != 'POSITION_OPEN' or not state['current_position']:
        await update.message.reply_text("⚠️ Нет открытой позиции для закрытия.")
        return
    try:
        # Формат: /exit итоговый_депозит
        exit_deposit = float(ctx.args[0])
        pos = state['current_position']

        initial_deposit = pos.get('deposit', 0)
        pnl = exit_deposit - initial_deposit
        pct_change = (pnl / initial_deposit) * 100 if initial_deposit != 0 else 0
        
        if LOGS_WS:
            # ... (код логирования в Google Sheets без изменений)
            row = [
                datetime.fromisoformat(pos['entry_time']).strftime('%Y-%m-%d %H:%M:%S'),
                pos.get('pair'), pos.get("side"), initial_deposit,
                pos.get('entry_price'), pos.get('sl'), pos.get('tp'),
                round(pnl, 2), round(pct_change, 2)
            ]
            await asyncio.to_thread(LOGS_WS.append_row, row, value_input_option='USER_ENTERED')

        await update.message.reply_text(
            f"✅ Сделка по <b>{pos.get('pair')}</b> закрыта и записана.\n"
            f"<b>P&L: {pnl:+.2f} USDT ({pct_change:+.2f}%)</b>"
        )
        
        state['current_position'] = None
        state['focus_coin'] = None
        state['mode'] = 'SEARCHING' # Возвращаемся к поиску
        save_state()
        
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат. Используйте: <code>/exit &lt;итоговый_депозит&gt;</code>", parse_mode="HTML")

if __name__ == "__main__":
    load_state()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = CHAT_IDS
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("entry", cmd_entry))
    app.add_handler(CommandHandler("exit", cmd_exit))

    log.info("Bot assistant started...")
    # Запускаем основной цикл, если бот должен быть включен при старте
    if state.get('bot_on', False):
        asyncio.ensure_future(main_loop(app))

    app.run_polling()
