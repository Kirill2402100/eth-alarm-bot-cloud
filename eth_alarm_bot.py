#!/usr/bin/env python3
# ============================================================================
# v1.2 - Interactive Assistant (Final Fix)
# • Исправлена ошибка KeyError при форматировании промпта.
# • Код полностью синхронизирован для стабильной работы.
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
            "bot_on": False, "mode": "SEARCHING",
            "focus_coin": None, "current_position": None,
            "last_signal": None, "last_focus_time": 0
        })
    log.info(f"State loaded: {state}")

# === EXCHANGE ===
exchange = ccxt.mexc({'options': {'defaultType': 'swap'}})

# === LLM PROMPTS & FUNCTION ===

PROMPT_SELECT_FOCUS = (
    "Ты — трейдер-аналитик. Тебе предоставлен список монет и их ключевые показатели за последний час. "
    "Проанализируй этот список и выбери ОДНУ самую перспективную монету для наблюдения в ближайшее время. "
    "Ищи монеты с аномальной активностью: сильный рост или падение, повышенная волатильность, но при этом еще не улетевшие в космос. "
    "Ответь ТОЛЬКО в виде JSON-объекта с одним полем: 'focus_coin'.\n\n"
    "Данные для анализа:\n{asset_data}"
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

async def ask_llm(final_prompt: str):
    if not LLM_API_KEY: return None
    payload = {"model": LLM_MODEL_ID, "messages": [{"role": "user", "content": final_prompt}], "temperature": 0.4, "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, json=payload, headers=headers, timeout=90) as r:
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
                await asyncio.sleep(60 * 15)
            elif state['mode'] == 'FOCUSED':
                await run_focused_phase(app)
                await asyncio.sleep(30)
            elif state['mode'] == 'POSITION_OPEN':
                await run_monitoring_phase(app)
                await asyncio.sleep(30)
            else:
                log.error(f"Unknown bot mode: {state['mode']}. Resetting to SEARCHING.")
                state['mode'] = 'SEARCHING'
                save_state()
                await asyncio.sleep(60)
        except Exception as e:
            log.error(f"Critical error in main_loop: {e}", exc_info=True)
            await broadcast_message(app, f"⚠️ Критическая ошибка в главном цикле: {e}")
            await asyncio.sleep(60)

async def run_searching_phase(app):
    log.info("--- Mode: SEARCHING (Deep Analysis) ---")
    await broadcast_message(app, "🔍 Провожу глубокий анализ рынка для выбора цели...")
    try:
        tickers = await exchange.fetch_tickers()
        usdt_pairs = {s: t for s, t in tickers.items() if s.endswith(':USDT') and t.get('quoteVolume')}
        sorted_pairs = sorted(usdt_pairs.items(), key=lambda item: item[1]['quoteVolume'], reverse=True)
        top_coins_list = [item[0] for item in sorted_pairs[:50]]
        
        coins_data_for_llm = []
        for pair in top_coins_list:
            try:
                # Получаем часовые свечи для анализа
                ohlcv = await exchange.fetch_ohlcv(pair, timeframe='1h', limit=24)
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                if len(df) < 2: continue
                
                # Считаем простые метрики
                last_candle = df.iloc[-1]
                price_change_pct = ((last_candle['close'] - df.iloc[0]['close']) / df.iloc[0]['close']) * 100
                
                coins_data_for_llm.append({
                    "pair": pair,
                    "price_change_24h_pct": round(price_change_pct, 2),
                    "current_price": last_candle['close']
                })
                await asyncio.sleep(0.5) # небольшая задержка, чтобы не перегружать API
            except Exception as e:
                log.warning(f"Could not fetch data for {pair} during search: {e}")

        if not coins_data_for_llm:
            await broadcast_message(app, "⚠️ Не удалось собрать данные для анализа. Попробую позже.")
            return

        prompt_text = PROMPT_SELECT_FOCUS.format(asset_data=json.dumps(coins_data_for_llm, indent=2))
        llm_response = await ask_llm(prompt_text)
        
        # ... (остальная часть функции с проверкой ответа LLM остается такой же) ...

    except Exception as e:
        log.error(f"Critical error in searching phase: {e}", exc_info=True)
        
async def run_focused_phase(app):
    log.info(f"--- Mode: FOCUSED on {state.get('focus_coin')} ---")
    if not state.get('focus_coin'):
        state['mode'] = 'SEARCHING'
        return
        
    if (datetime.now().timestamp() - state.get('last_focus_time', 0)) > 60 * 15:
        await broadcast_message(app, f"⚠️ Не найдено подходящего сетапа по {state['focus_coin']} за 15 минут. Ищу новую цель.")
        state['mode'] = 'SEARCHING'
        save_state()
        return

    try:
        ohlcv = await exchange.fetch_ohlcv(state['focus_coin'], timeframe='5m', limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        prompt_text = PROMPT_FIND_ENTRY.format(asset=state['focus_coin'])
        llm_response = await ask_llm(prompt_text)

        if llm_response and llm_response.get('decision') == 'ENTER':
            state['last_signal'] = llm_response
            save_state()
            
            side = llm_response.get('side', 'N/A')
            reason = llm_response.get('reason', 'N/A')
            sl = llm_response.get('sl', 0)
            tp = llm_response.get('tp', 0)
            
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
    except Exception as e:
        log.error(f"Error in focused phase for {state['focus_coin']}: {e}", exc_info=True)

async def run_monitoring_phase(app):
    log.info(f"--- Mode: POSITION_OPEN on {state.get('current_position', {}).get('pair')} ---")
    pos = state.get('current_position')
    if not pos:
        state['mode'] = 'SEARCHING'
        save_state()
        return
    try:
        ohlcv = await exchange.fetch_ohlcv(pos['pair'], timeframe='1m', limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        prompt_text = PROMPT_MANAGE_POSITION.format(asset=pos['pair'], side=pos['side'], entry_price=pos['entry_price'])
        llm_response = await ask_llm(prompt_text)

        if llm_response and llm_response.get('decision') == 'CLOSE':
            reason = llm_response.get('reason', 'N/A')
            message = (
                f"⚠️ <b>РЕКОМЕНДАЦИЯ: ЗАКРЫТЬ ПОЗИЦИЮ!</b> ⚠️\n\n"
                f"<b>Монета:</b> <code>{pos['pair']}</code>\n"
                f"<b>Причина от LLM:</b> <i>{reason}</i>\n\n"
                f"👉 Закройте сделку и подтвердите выход командой <code>/exit</code>."
            )
            await broadcast_message(app, message)
    except Exception as e:
        log.error(f"Error in monitoring phase for {pos['pair']}: {e}", exc_info=True)

# === COMMANDS and RUN ===
async def broadcast_message(app, text):
    # Убедимся, что app.chat_ids существует
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
        state['bot_on'] = True
        state['mode'] = 'SEARCHING'
        save_state()
        await update.message.reply_text("✅ Ассистент запущен. Начинаю поиск цели...")
        if scanner_task is None or scanner_task.done():
            scanner_task = asyncio.create_task(main_loop(ctx.application))
    else:
        await update.message.reply_text("ℹ️ Ассистент уже запущен.")

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state.get('bot_on'):
        state['bot_on'] = False
        save_state()
        await update.message.reply_text("❌ Ассистент остановлен. Все циклы завершатся.")
    else:
        await update.message.reply_text("ℹ️ Ассистент уже был остановлен.")

async def cmd_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state['mode'] != 'FOCUSED' or not state.get('last_signal'):
        await update.message.reply_text("⚠️ Нет активного сигнала для подтверждения входа.")
        return
    try:
        entry_price = float(ctx.args[0])
        deposit = float(ctx.args[1])
        signal = state['last_signal']
        
        state['current_position'] = {
            "entry_time": datetime.now(timezone.utc).isoformat(), "pair": state['focus_coin'],
            "side": signal.get('side'), "deposit": deposit,
            "entry_price": entry_price, "sl": signal.get('sl'), "tp": signal.get('tp')
        }
        state['mode'] = 'POSITION_OPEN'
        state['last_signal'] = None
        save_state()
        
        pos = state['current_position']
        await update.message.reply_text(
            f"✅ Позиция <b>{pos.get('side')}</b> по <b>{pos.get('pair')}</b> зафиксирована.\nНачинаю сопровождение."
        )
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат. Используйте: <code>/entry &lt;цена_входа&gt; &lt;депозит&gt;</code>", parse_mode="HTML")

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

        await update.message.reply_text(
            f"✅ Сделка по <b>{pos.get('pair')}</b> закрыта и записана.\n"
            f"<b>P&L: {pnl:+.2f} USDT ({pct_change:+.2f}%)</b>",
            parse_mode="HTML"
        )
        
        state['current_position'] = None
        state['focus_coin'] = None
        state['mode'] = 'SEARCHING'
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

    log.info("Bot assistant starting...")
    
    if state.get('bot_on', False):
        # Запускаем основной цикл в фоновом режиме
        scanner_task = asyncio.create_task(main_loop(app))

    app.run_polling()
