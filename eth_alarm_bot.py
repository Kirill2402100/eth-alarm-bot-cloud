#!/usr/bin/env python3
# ============================================================================
# v5.0 - Final Hybrid Architecture
# • Финальная версия: LLM предлагает стратегию (sl_pct, rr_ratio),
#   а бот производит точный расчет цен на основе реальных данных с биржи.
# • Этот подход полностью решает проблему "галлюцинаций" с ценами.
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
TRADE_LOG_SHEET = "Trading_Logs"
SIGNAL_LOG_SHEET = "Signal_Logs"

LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_API_URL = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_MODEL_ID = os.getenv("LLM_MODEL_ID", "gpt-4.1")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for n in ("httpx", "httpcore"): logging.getLogger(n).setLevel(logging.WARNING)

# === GOOGLE SHEETS ===
def setup_google_sheet(spreadsheet, sheet_name, headers):
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows="1000", cols="20")
    if worksheet.row_values(1) != headers:
        worksheet.clear()
        worksheet.update('A1', [headers])
        worksheet.format(f'A1:{chr(ord("A")+len(headers)-1)}1', {'textFormat': {'bold': True}})
    return worksheet

def setup_google_sheets():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gs = gspread.authorize(creds)
        spreadsheet = gs.open_by_key(SHEET_ID)
        
        trade_headers = ["Дата входа", "Инструмент", "Направление", "Депозит", "Цена входа", "Stop Loss", "Take Profit", "P&L сделки (USDT)", "% к депозиту"]
        signal_headers = ["Дата сигнала", "Инструмент", "Направление", "Цена входа", "Stop Loss", "Take Profit", "Обоснование"]
        
        trade_ws = setup_google_sheet(spreadsheet, TRADE_LOG_SHEET, trade_headers)
        signal_ws = setup_google_sheet(spreadsheet, SIGNAL_LOG_SHEET, signal_headers)
        
        return trade_ws, signal_ws
    except Exception as e:
        log.error("Google Sheets init failed: %s", e)
        return None, None
TRADE_LOG_WS, SIGNAL_LOG_WS = setup_google_sheets()

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
            "current_position": None, "last_signal": None
        })
    log.info(f"State loaded: {state}")

# === EXCHANGE ===
exchange = ccxt.mexc({'options': {'defaultType': 'swap'}})

# === LLM PROMPTS & FUNCTION ===
PROMPT_ANALYZE_AND_SELECT = (
    "Ты — главный трейдер-аналитик. Тебе предоставлен список кандидатов, отобранных по сигналу 'свежее пересечение EMA'.\n\n"
    "**КРИТИЧЕСКИ ВАЖНО:** Твой анализ должен быть основан **ИСКЛЮЧИТЕЛЬНО** на предоставленных ниже данных по каждому кандидату. Не используй свою внутреннюю базу знаний о ценах.\n\n"
    "**ТВОЙ АЛГОРИТМ ВЫБОРА:**\n"
    "1.  Проанализируй **каждого** кандидата в списке `candidates`.\n"
    "2.  Сравни их между собой по совокупности факторов: соответствие глобальному тренду на H1, сила импульса (ADX), адекватный RSI (не в экстремальной зоне).\n"
    "3.  **Обязательный выбор:** Выбери **ОДНОГО, самого лучшего кандидата**.\n"
    "4.  **Проверка направления:** Убедись, что направление (`side`) твоего финального сетапа **строго совпадает** с направлением, указанным для выбранного кандидата.\n"
    "5.  **Генерация сетапа:** Для этого лучшего кандидата сгенерируй SL и TP с R:R >= 1.5.\n\n"
    "**ТРЕБОВАНИЯ К ОТВЕТУ:**\n"
    "Твой ответ **обязательно** должен быть в формате **JSON** и содержать ТОЛЬКО сетап лучшего кандидата. Пример: "
    "`{ 'pair': 'BTC/USDT:USDT', 'side': 'LONG', 'reason': 'Свежее пересечение...', 'sl_pct': 1.5, 'rr_ratio': 2.0 }`."
)
PROMPT_MANAGE_POSITION = (
    "Ты — риск-менеджер. Ты ведешь открытую позицию {side} по {asset} от цены {entry_price}. "
    "Анализируй каждую новую свечу. Если позиция развивается по плану, ответь {'decision': 'HOLD'}. "
    "Если видишь тревожные сигналы, немедленно дай команду на закрытие: {'decision': 'CLOSE', 'reason': 'обоснование'}."
)

async def ask_llm(final_prompt: str):
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

# === MAIN BOT LOGIC ===
async def main_loop(app):
    while state.get('bot_on', False):
        try:
            if state['mode'] == 'SEARCHING':
                await run_searching_phase(app)
                await asyncio.sleep(60 * 10)
            elif state['mode'] == 'AWAITING_ENTRY':
                await run_awaiting_entry_phase(app)
                await asyncio.sleep(60)
            elif state['mode'] == 'POSITION_OPEN':
                await run_monitoring_phase(app)
                await asyncio.sleep(45)
            else:
                log.error(f"Unknown bot mode: {state['mode']}. Resetting.")
                state['mode'] = 'SEARCHING'; save_state()
        except Exception as e:
            log.error(f"Critical error in main_loop: {e}", exc_info=True)
            await broadcast_message(app, f"⚠️ Критическая ошибка в главном цикле: {e}")
            await asyncio.sleep(60)

async def run_searching_phase(app):
    log.info("--- Mode: SEARCHING for Fresh EMA Crossovers ---")
    await broadcast_message(app, f"<b>Этап 1:</b> Ищу монеты со свежим пересечением 9/21 EMA (не старше 2 свечей)...")
    pre_candidates = []
    try:
        tickers = await exchange.fetch_tickers()
        usdt_pairs = {s: t for s, t in tickers.items() if s.endswith(':USDT') and t.get('quoteVolume')}
        sorted_pairs = sorted(usdt_pairs.items(), key=lambda item: item[1]['quoteVolume'], reverse=True)
        coin_list = [item[0] for item in sorted_pairs[:COIN_LIST_SIZE]]
        
        for pair in coin_list:
            if len(pre_candidates) >= 10: break
            if not state.get('bot_on'): return
            try:
                ohlcv_5m = await exchange.fetch_ohlcv(pair, timeframe='5m', limit=50)
                df_5m = pd.DataFrame(ohlcv_5m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                if len(df_5m) < 22: continue

                df_5m.ta.ema(length=9, append=True); df_5m.ta.ema(length=21, append=True)

                for i in range(len(df_5m) - 1, len(df_5m) - 6, -1):
                    if i < 1: break
                    
                    last = df_5m.iloc[i]; prev = df_5m.iloc[i-1]
                    ema_short = last.get('EMA_9'); ema_long = last.get('EMA_21')
                    prev_ema_short = prev.get('EMA_9'); prev_ema_long = prev.get('EMA_21')
                    if any(v is None for v in [ema_short, ema_long, prev_ema_short, prev_ema_long]): continue
                    
                    candles_since_cross = (len(df_5m) - 1) - i
                    
                    # ---> ЖЕСТКИЙ ФИЛЬТР ПО СВЕЖЕСТИ СИГНАЛА <---
                    if candles_since_cross > 2:
                        break # Если пересечение было давно, дальше не ищем

                    side = None
                    if prev_ema_short <= prev_ema_long and ema_short > ema_long: side = 'LONG'
                    elif prev_ema_short >= prev_ema_long and ema_short < ema_long: side = 'SHORT'
                    
                    if side:
                        pre_candidates.append({"pair": pair, "side": side, "candles_since_cross": candles_since_cross})
                        log.info(f"Found pre-candidate: {pair}, Side: {side}, Freshness: {candles_since_cross} candles ago.")
                        break
                
                await asyncio.sleep(1.5)
            except Exception as e:
                log.warning(f"Could not process {pair} in initial scan: {e}")
    # ... (остальная часть функции остается без изменений) ...
        
    except Exception as e:
        log.error(f"Critical error in Stage 1 (Indicator Scan): {e}", exc_info=True)
        return

    if not pre_candidates:
        log.info("No candidates with EMA crossover found."); return

    await broadcast_message(app, f"<b>Этап 2:</b> Найдено {len(pre_candidates)} кандидатов. Собираю и отправляю данные в LLM для выбора лучшего...")
    deep_data_for_llm = []
    try:
        for candidate in pre_candidates:
            pair = candidate['pair']
            ohlcv_h1 = await exchange.fetch_ohlcv(pair, timeframe='1h', limit=100)
            df_h1 = pd.DataFrame(ohlcv_h1, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df_h1.ta.ema(length=50, append=True)
            last_h1 = df_h1.iloc[-1]; ema_h1 = last_h1.get('EMA_50')
            
            ohlcv_5m = await exchange.fetch_ohlcv(pair, timeframe='5m', limit=100)
            df_5m = pd.DataFrame(ohlcv_5m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df_5m.ta.rsi(length=14, append=True); df_5m.ta.adx(length=14, append=True)
            last_5m = df_5m.iloc[-1]
            
            if ema_h1 is None: continue
            deep_data_for_llm.append({
                "pair": pair, "side": candidate['side'], "candles_since_cross": candidate['candles_since_cross'],
                "h1_trend": "UP" if last_h1['close'] > ema_h1 else "DOWN",
                "m5_rsi": round(last_5m.get('RSI_14'), 2), "m5_adx": round(last_5m.get('ADX_14'), 2)
            })
            await asyncio.sleep(0.5)
    except Exception as e:
        log.error(f"Critical error in Stage 2 (Deep Data): {e}", exc_info=True)
        return

    prompt_text = PROMPT_ANALYZE_AND_SELECT + "\n\nКандидаты:\n" + json.dumps({"candidates": deep_data_for_llm})
    llm_response = await ask_llm(prompt_text)
    log.info(f"LLM decision on batch: {llm_response}")

    if llm_response and 'pair' in llm_response:
        setup_strategy = llm_response
        pair = setup_strategy.get('pair'); side = setup_strategy.get('side')
        try:
            ticker = await exchange.fetch_ticker(pair)
            entry_price = ticker.get('last')
            if not entry_price:
                log.error(f"Could not fetch current price for {pair}"); return

            sl_pct = float(setup_strategy.get('sl_pct', 2.0)); rr_ratio = float(setup_strategy.get('rr_ratio', 1.5))
            
            if side == 'LONG':
                stop_loss_price = entry_price * (1 - sl_pct / 100)
                take_profit_price = entry_price + (entry_price - stop_loss_price) * rr_ratio
            elif side == 'SHORT':
                stop_loss_price = entry_price * (1 + sl_pct / 100)
                take_profit_price = entry_price - (stop_loss_price - entry_price) * rr_ratio
            else: return

            final_setup = {
                "pair": pair, "side": side, "reason": setup_strategy.get('reason'),
                "entry_price": entry_price, "sl": stop_loss_price, "tp": take_profit_price
            }

            state['last_signal'] = final_setup; state['last_signal']['timestamp'] = datetime.now().timestamp()
            state['mode'] = 'AWAITING_ENTRY'; save_state()
            
            await log_signal_to_gs(final_setup)
            
            message = (f"🔔 <b>ЛУЧШИЙ СЕТАП!</b> 🔔\n\n<b>Монета:</b> <code>{final_setup.get('pair')}</code>\n<b>Направление:</b> <b>{final_setup.get('side')}</b>\n"
                       f"<b>Take Profit:</b> <code>{final_setup.get('tp'):.6f}</code>\n<b>Stop Loss:</b> <code>{final_setup.get('sl'):.6f}</code>\n\n"
                       f"<b>Обоснование LLM:</b> <i>{final_setup.get('reason')}</i>\n\n"
                       f"👉 Откройте сделку и подтвердите вход командой <code>/entry</code>. Сетап актуален 20 минут.")
            await broadcast_message(app, message)
        except Exception as e:
            log.error(f"Error calculating final prices for {pair}: {e}", exc_info=True)
    else:
        reason = llm_response.get('reason', 'LLM не смог выбрать кандидата.') if llm_response else "LLM не ответил."
        await broadcast_message(app, f"ℹ️ Анализ завершен. LLM не выбрал ни одного достойного кандидата. Причина: <i>{reason}</i>")

async def run_awaiting_entry_phase(app):
    log.info(f"--- Mode: AWAITING_ENTRY for {state.get('last_signal', {}).get('pair')} ---")
    signal_time = state.get('last_signal', {}).get('timestamp', 0)
    if (datetime.now().timestamp() - signal_time) > 60 * 20:
        pair = state['last_signal']['pair']
        log.info(f"Signal for {pair} expired.")
        state['last_signal'] = None; state['mode'] = 'SEARCHING'; save_state()
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
            message = (f"⚠️ <b>РЕКОМЕНДАЦИЯ: ЗАКРЫТЬ ПОЗИЦИЮ!</b> ⚠️\n\n"
                       f"<b>Монета:</b> <code>{pos['pair']}</code>\n"
                       f"<b>Причина от LLM:</b> <i>{llm_response.get('reason', 'N/A')}</i>\n\n"
                       f"👉 Закройте сделку и подтвердите выход командой <code>/exit</code>.")
            await broadcast_message(app, message)
    except Exception as e:
        log.error(f"Error in monitoring phase for {pos['pair']}: {e}", exc_info=True)

# === HELPER and COMMANDS ===
async def broadcast_message(app, text):
    chat_ids = getattr(app, 'chat_ids', CHAT_IDS)
    for chat_id in chat_ids:
        try: await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception as e: log.error(f"Failed to send message to {chat_id}: {e}")

async def log_signal_to_gs(setup):
    if not SIGNAL_LOG_WS: return
    try:
        row = [datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), setup.get('pair'), setup.get('side'),
               setup.get('entry_price'), setup.get('sl'), setup.get('tp'), setup.get('reason')]
        await asyncio.to_thread(SIGNAL_LOG_WS.append_row, row, value_input_option='USER_ENTERED')
    except Exception as e:
        log.error(f"Failed to write signal to Google Sheets: {e}")

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
        await update.message.reply_text("❌ Ассистент остановлен.")
    else:
        await update.message.reply_text("ℹ️ Ассистент уже был остановлен.")

async def cmd_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state['mode'] != 'AWAITING_ENTRY' or not state.get('last_signal'):
        await update.message.reply_text("⚠️ Нет активного сигнала для подтверждения входа.")
        return
    try:
        entry_price = float(ctx.args[0]); deposit = float(ctx.args[1])
        signal = state['last_signal']
        state['current_position'] = {"entry_time": datetime.now(timezone.utc).isoformat(), "pair": signal.get('pair'),
                                     "side": signal.get('side'), "deposit": deposit, "entry_price": entry_price,
                                     "sl": signal.get('sl'), "tp": signal.get('tp')}
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
        
        if TRADE_LOG_WS:
            row = [datetime.fromisoformat(pos['entry_time']).strftime('%Y-%m-%d %H:%M:%S'), pos.get('pair'), pos.get("side"),
                   initial_deposit, pos.get('entry_price'), pos.get('sl'), pos.get('tp'), round(pnl, 2), round(pct_change, 2)]
            await asyncio.to_thread(TRADE_LOG_WS.append_row, row, value_input_option='USER_ENTERED')
        
        await update.message.reply_text(f"✅ Сделка по <b>{pos.get('pair')}</b> закрыта. P&L: <b>{pnl:+.2f} USDT ({pct_change:+.2f}%)</b>", parse_mode="HTML")
        
        state['current_position'] = None; state['mode'] = 'SEARCHING'; save_state()
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Неверный формат: <code>/exit &lt;итоговый_депозит&gt;</code>", parse_mode="HTML")

if __name__ == "__main__":
    load_state()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = CHAT_IDS
    app.add_handler(CommandHandler("start", cmd_start)); app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("entry", cmd_entry)); app.add_handler(CommandHandler("exit", cmd_exit))
    log.info("Sniper Assistant starting...")
    if state.get('bot_on', False):
        scanner_task = asyncio.create_task(main_loop(app))
    app.run_polling()
