#!/usr/bin/env python3
# ============================================================================
# v3.1 - Enhanced UX & Reporting
# Changelog 10‑Jul‑2025 (Europe/Belgrade):
# • UX: All Telegram messages are now in Russian, well-formatted, and more informative.
# • New Module: Market Volatility Report. Bot now analyzes BTC ATR% and reports on market volatility.
# • New Module: Daily P&L Report. A new task now runs every 24h to summarize and report the P&L.
# ============================================================================

import os, asyncio, json, logging, uuid
from datetime import datetime, timezone, timedelta

import pandas as pd
import pandas_ta as ta
import aiohttp, gspread, ccxt.async_support as ccxt
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, constants
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# === ENV / Logging =========================================================
BOT_TOKEN               = os.getenv("BOT_TOKEN")
CHAT_IDS                = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
SHEET_ID                = os.getenv("SHEET_ID")
COIN_LIST_SIZE          = int(os.getenv("COIN_LIST_SIZE", "300"))
MAX_CONCURRENT_SIGNALS  = int(os.getenv("MAX_CONCURRENT_SIGNALS", "10"))
ANOMALOUS_CANDLE_MULT = 3.0
COOLDOWN_HOURS          = 4

LLM_API_KEY   = os.getenv("LLM_API_KEY")
LLM_API_URL   = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_MODEL_ID  = os.getenv("LLM_MODEL_ID", "gpt-4o-mini")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for n in ("httpx", "httpcore", "gspread"):
    logging.getLogger(n).setLevel(logging.WARNING)

# === Helper ================================================================
def fmt(price: float | None) -> str:
    if price is None:       return "N/A"
    if price > 10:          return f"{price:,.2f}"
    elif price > 0.1:       return f"{price:.4f}"
    elif price > 0.001:     return f"{price:.6f}"
    else:                   return f"{price:.8f}"

# === Google‑Sheets =========================================================
TRADE_LOG_WS = None
SHEET_NAME   = "Autonomous_Trade_Log_v5"

HEADERS = [
    "Signal_ID","Pair","Side","Status",
    "Entry_Time_UTC","Exit_Time_UTC",
    "Entry_Price","Exit_Price","SL_Price","TP_Price",
    "MFE_Price","MAE_Price","MFE_R","MAE_R",
    "Entry_RSI","Entry_ADX","H1_Trend_at_Entry",
    "Entry_BB_Position","LLM_Reason",
    "Confidence_Score", "Position_Size_USD", "Leverage", "PNL_USD", "PNL_Percent"
]

def setup_sheets():
    global TRADE_LOG_WS
    if not SHEET_ID: return
    try:
        scope = ["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
        gs = gspread.authorize(ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope))
        ss = gs.open_by_key(SHEET_ID)
        try:
            ws = ss.worksheet(SHEET_NAME)
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title=SHEET_NAME, rows="1000", cols=len(HEADERS))
        if ws.row_values(1) != HEADERS:
            ws.clear(); ws.update("A1",[HEADERS]); ws.format(f"A1:{chr(ord('A')+len(HEADERS)-1)}1",{"textFormat":{"bold":True}})
        TRADE_LOG_WS = ws
        log.info("Google‑Sheets ready – logging to '%s'.", SHEET_NAME)
    except Exception as e:
        log.error("Sheets init failed: %s", e)

# === State ================================================================
STATE_FILE = "bot_state_v3_1.json"
state = {}
def load_state():
    global state
    if os.path.exists(STATE_FILE):
        state = json.load(open(STATE_FILE))
    state.setdefault("bot_on", False)
    state.setdefault("monitored_signals", [])
    state.setdefault("cooldown", {})
    log.info("State loaded (%d signals).", len(state["monitored_signals"]))
def save_state():
    json.dump(state, open(STATE_FILE,"w"), indent=2)

# === Exchange & Strategy ==================================================
exchange  = ccxt.mexc({
    'options': {'defaultType':'swap'},
    'timeout': 30000,
})
TF_ENTRY  = os.getenv("TF_ENTRY", "15m")
ATR_LEN   = 14
SL_ATR_MULT  = 1.5
RR_RATIO     = 2.2
MIN_M15_ADX  = 20
MIN_CONFIDENCE_SCORE = 6

# === LLM prompt ===========================================================
PROMPT = (
    "Ты — главный трейдер-аналитик. Проанализируй КАЖДОГО кандидата из списка ниже. Для каждого кандидата верни:"
    "1. 'confidence_score': твою уверенность в успехе сделки по шкале от 1 до 10."
    "2. 'reason': краткое обоснование оценки (2-3 предложения), обращая внимание на комбинацию ADX, RSI и положение цены относительно BBands."
    "Верни ТОЛЬКО JSON-массив с объектами по каждому кандидату, без лишних слов."
)

async def ask_llm(prompt: str):
    if not LLM_API_KEY: return None
    payload = {
        "model": LLM_MODEL_ID,
        "messages":[{"role":"user","content":prompt}],
        "temperature":0.4,
        "response_format":{"type":"json_object"}
    }
    hdrs = {"Authorization":f"Bearer {LLM_API_KEY}","Content-Type":"application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, json=payload, headers=hdrs, timeout=180) as r:
                data = await r.json()
                content = data["choices"][0]["message"]["content"]
                response_data = json.loads(content)
                if isinstance(response_data, dict) and "candidates" in response_data:
                    return response_data["candidates"]
                elif isinstance(response_data, list):
                    return response_data
                else:
                    log.error(f"LLM returned unexpected format: {response_data}")
                    return []
    except Exception as e:
        log.error("LLM error: %s", e)
        return None

# === Utils ===============================================================
async def broadcast(app, txt:str):
    for cid in getattr(app,"chat_ids", CHAT_IDS):
        try: await app.bot.send_message(chat_id=cid, text=txt, parse_mode=constants.ParseMode.HTML)
        except Exception as e: log.error("Send fail %s: %s", cid, e)

# === Main loops ===========================================================
async def get_market_snapshot():
    try:
        ohlcv_btc = await exchange.fetch_ohlcv('BTC/USDT', '1d', limit=51)
        df_btc = pd.DataFrame(ohlcv_btc, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df_btc.ta.ema(length=50, append=True)
        df_btc.ta.atr(length=14, append=True)
        last_btc = df_btc.iloc[-1]
        
        regime = "BULLISH"
        if last_btc['close'] < last_btc['EMA_50']:
            regime = "BEARISH"
            
        atr_percent = last_btc['ATRr_14']
        if atr_percent < 2.5: volatility = "Низкая"
        elif atr_percent < 5: volatility = "Умеренная"
        else: volatility = "Высокая"
            
        return {'regime': regime, 'volatility': volatility, 'volatility_percent': f"{atr_percent:.2f}%"}
    except Exception as e:
        log.warning(f"Could not fetch BTC market snapshot: {e}")
        return {'regime': "BULLISH", 'volatility': "N/A", 'volatility_percent': "N/A"}

async def scanner(app):
    while state["bot_on"]:
        try:
            scan_start_time = datetime.now(timezone.utc)
            if len(state["monitored_signals"]) >= MAX_CONCURRENT_SIGNALS:
                await asyncio.sleep(300); continue
            
            snapshot = await get_market_snapshot()
            market_regime = snapshot['regime']
            
            msg = (f"🔍 <b>Начинаю сканирование рынка...</b>\n"
                   f"<i>Режим:</i> {market_regime} | <i>Волатильность:</i> {snapshot['volatility']} (ATR {snapshot['volatility_percent']})")
            await broadcast(app, msg)

            if market_regime == "BEARISH":
                await broadcast(app, "❗️ <b>Рынок в медвежьей фазе.</b> Длинные позиции (LONG) отключены.")

            now = datetime.now(timezone.utc).timestamp()
            state["cooldown"] = {p:t for p,t in state["cooldown"].items() if now-t < COOLDOWN_HOURS*3600}
            
            pairs = sorted(
                ((s,t) for s,t in (await exchange.fetch_tickers()).items() if s.endswith(':USDT') and t['quoteVolume']),
                key=lambda x:x[1]['quoteVolume'], reverse=True
            )[:COIN_LIST_SIZE]
            pre = []
            for sym,_ in pairs:
                if len(pre)>=10: break
                if sym in state["cooldown"]: continue
                try:
                    df15 = pd.DataFrame (await exchange.fetch_ohlcv(sym, TF_ENTRY, limit=50),
                        columns=["timestamp", "open", "high", "low", "close", "volume"])
                    if len(df15)<30: continue
                    df15.ta.ema(length=9, append=True)
                    df15.ta.ema(length=21, append=True)
                    df15.ta.atr(length=ATR_LEN, append=True)
                    df15.ta.adx(length=14, append=True)
                    last15 = df15.iloc[-1]

                    if f"ATR_{ATR_LEN}" not in df15.columns or pd.isna(last15[f"ATR_{ATR_LEN}"]): continue

                    adx = last15["ADX_14"]; side = None
                    if adx < MIN_M15_ADX: continue
                    
                    side = None
                    for i in range(1, 4):
                        cur, prev = df15.iloc[-i], df15.iloc[-i-1]
                        if prev["EMA_9"] <= prev["EMA_21"] and cur["EMA_9"] > cur["EMA_21"]: side = "LONG"; break
                        if prev["EMA_9"] >= prev["EMA_21"] and cur["EMA_9"] < cur["EMA_21"]: side = "SHORT"; break
                    if not side: continue
                    
                    if market_regime == "BEARISH" and side == "LONG": continue

                    if (last15["high"]-last15["low"]) > last15[f"ATR_{ATR_LEN}"]*ANOMALOUS_CANDLE_MULT: continue
                    
                    df1h = pd.DataFrame(await exchange.fetch_ohlcv(sym, "1h", limit=51),
                        columns=["timestamp", "open", "high", "low", "close", "volume"])
                    df1h.ta.ema(length=9,append=True); df1h.ta.ema(length=21,append=True); df1h.ta.ema(length=50,append=True)
                    last1h = df1h.iloc[-1]
                    h1_trend = "NEUTRAL"
                    if last1h["EMA_9"] > last1h["EMA_21"] > last1h["EMA_50"]: h1_trend = "UP"
                    if last1h["EMA_9"] < last1h["EMA_21"] < last1h["EMA_50"]: h1_trend = "DOWN"

                    if (side=="LONG" and h1_trend != "UP") or (side=="SHORT" and h1_trend != "DOWN"): continue

                    pre.append({"pair":sym, "side":side, "h1_trend":h1_trend})
                except Exception as e:
                    log.warning("Scan %s: %s", sym, e)
            
            if not pre:
                duration = (datetime.now(timezone.utc) - scan_start_time).total_seconds()
                await broadcast(app, f"✅ <b>Поиск завершен за {duration:.0f} сек.</b> Подходящих сетапов не найдено.")
                await asyncio.sleep(900); continue

            await broadcast(app, f"📊 <b>Найдено {len(pre)} кандидатов.</b> Отправляю на детальный анализ и оценку LLM...")
            setups_for_llm=[]
            for c in pre:
                try:
                    df = pd.DataFrame(await exchange.fetch_ohlcv(c["pair"], TF_ENTRY, limit=100),
                        columns=["timestamp", "open", "high", "low", "close", "volume"])
                    df.ta.bbands(length=20, std=2, append=True)
                    df.ta.atr(length=ATR_LEN, append=True)
                    df.ta.rsi(length=14, append=True)
                    df.ta.adx(length=14, append=True)
                    last = df.iloc[-1]
                    
                    if f"ATR_{ATR_LEN}" not in df.columns or pd.isna(last[f"ATR_{ATR_LEN}"]): continue

                    entry = last["close"]
                    bb_pos="Внутри канала"
                    if entry>last["BBU_20_2.0"]: bb_pos="Выше верхней границы"
                    elif entry<last["BBL_20_2.0"]: bb_pos="Ниже нижней границы"
                    
                    setups_for_llm.append({
                        "pair":c["pair"], "side":c["side"], "entry_price":entry,
                        "adx":round(float(last["ADX_14"]),2), "rsi":round(float(last["RSI_14"]),2),
                        "bb_pos":bb_pos, "sl_atr_mult": SL_ATR_MULT, "rr_ratio": RR_RATIO,
                        "atr_val": last[f"ATR_{ATR_LEN}"]
                    })
                except Exception as e: log.warning("Enrich %s: %s", c["pair"], e)
            
            if not setups_for_llm:
                duration = (datetime.now(timezone.utc) - scan_start_time).total_seconds()
                await broadcast(app, f"✅ <b>Анализ завершен за {duration:.0f} сек.</b> Ни один кандидат не прошел углубленную проверку.")
                await asyncio.sleep(900); continue

            scored_candidates = await ask_llm(PROMPT + "\n\n" + json.dumps({"candidates":setups_for_llm}))
            
            if not scored_candidates:
                await broadcast(app, "❗️ <b>LLM не вернул оценки.</b> Пропускаю цикл.")
                await asyncio.sleep(900); continue
            
            final_candidates = []
            for i, scored in enumerate(scored_candidates):
                if i < len(setups_for_llm):
                    original_setup = setups_for_llm[i]
                    if scored.get('confidence_score', 0) >= MIN_CONFIDENCE_SCORE:
                        original_setup.update(scored)
                        final_candidates.append(original_setup)
            
            if not final_candidates:
                duration = (datetime.now(timezone.utc) - scan_start_time).total_seconds()
                await broadcast(app, f"✅ <b>Анализ завершен за {duration:.0f} сек.</b> Ни один сетап не получил достаточной оценки (>{MIN_CONFIDENCE_SCORE}).")
                await asyncio.sleep(900); continue

            final_candidates.sort(key=lambda x: (x.get('confidence_score', 0), x.get('adx', 0)), reverse=True)
            best_setup = final_candidates[0]
            
            score = best_setup.get('confidence_score', 0)
            if score >= 9: position_size_usd = 50
            elif score >= 7: position_size_usd = 30
            else: position_size_usd = 20

            entry = best_setup["entry_price"]
            risk = best_setup["atr_val"] * SL_ATR_MULT
            sl,tp = (entry-risk, entry+risk*RR_RATIO) if best_setup["side"]=="LONG" else (entry+risk, entry-risk*RR_RATIO)
            
            sig_id = str(uuid.uuid4())[:8]
            signal_to_monitor = {
                "signal_id": sig_id, "entry_time_utc": datetime.now(timezone.utc).isoformat(),
                "pair": best_setup["pair"], "side": best_setup["side"],
                "entry_price": entry, "sl": sl, "tp": tp,
                "mfe_price": entry, "mae_price": entry,
                "confidence_score": score, "position_size_usd": position_size_usd, "leverage": 100,
                "reason": best_setup.get("reason"), "adx": best_setup.get("adx"), "rsi": best_setup.get("rsi"),
                "bb_pos": best_setup.get("bb_pos")
            }
            
            state["monitored_signals"].append(signal_to_monitor)
            state["cooldown"][best_setup["pair"]] = now
            save_state()
            
            msg = (f"🚀 <b>НОВЫЙ СИГНАЛ</b> 🚀\n\n"
                   f"<b>Инструмент:</b> <code>{best_setup['pair']}</code>\n"
                   f"<b>Направление:</b> {best_setup['side']}\n\n"
                   f"⭐ <b>Оценка LLM: {score}/10</b>\n"
                   f"💵 <b>Рекомендуемый размер: ${position_size_usd}</b> (плечо 100x)\n\n"
                   f"<i>Обоснование: {best_setup.get('reason', 'N/A')}</i>\n\n"
                   f"<b>Вход:</b> <code>{fmt(entry)}</code>\n"
                   f"<b>Stop Loss:</b> <code>{fmt(sl)}</code>\n"
                   f"<b>Take Profit:</b> <code>{fmt(tp)}</code>")
            await broadcast(app, msg)
            
            await asyncio.sleep(900)
        except Exception as e:
            log.error("Scanner critical: %s", e, exc_info=True)
            await asyncio.sleep(300)

async def monitor(app):
    while state["bot_on"]:
        if not state["monitored_signals"]:
            await asyncio.sleep(30); continue
        for s in list(state["monitored_signals"]):
            try:
                price = (await exchange.fetch_ticker(s["pair"]))["last"]
                if not price: continue
                
                if s["side"]=="LONG":
                    if price>s["mfe_price"]: s["mfe_price"]=price
                    if price<s["mae_price"]: s["mae_price"]=price
                else:
                    if price<s["mfe_price"]: s["mfe_price"]=price
                    if price>s["mae_price"]: s["mae_price"]=price
                
                hit=None
                exit_price = None
                if (s["side"]=="LONG" and price>=s["tp"]): hit, exit_price = "TP_HIT", s["tp"]
                elif (s["side"]=="SHORT" and price<=s["tp"]): hit, exit_price = "TP_HIT", s["tp"]
                elif (s["side"]=="LONG" and price<=s["sl"]): hit, exit_price = "SL_HIT", s["sl"]
                elif (s["side"]=="SHORT" and price>=s["sl"]): hit, exit_price = "SL_HIT", s["sl"]
                
                if hit:
                    risk = abs(s["entry_price"]-s["sl"])
                    mfe_r = round(abs(s["mfe_price"]-s["entry_price"])/risk, 2) if risk > 0 else 0
                    mae_r = round(abs(s["mae_price"]-s["entry_price"])/risk, 2) if risk > 0 else 0
                    
                    leverage = s.get("leverage", 100)
                    position_size = s.get("position_size_usd", 0)
                    
                    price_change = (exit_price - s['entry_price']) / s['entry_price']
                    if s["side"] == "SHORT": price_change = -price_change
                    
                    pnl_percent = price_change * leverage * 100
                    pnl_usd = position_size * (pnl_percent / 100)

                    if TRADE_LOG_WS:
                        row=[
                            s["signal_id"],s["pair"],s["side"],hit,
                            s["entry_time_utc"],datetime.now(timezone.utc).isoformat(),
                            s["entry_price"],exit_price,s["sl"],s["tp"],
                            s["mfe_price"],s["mae_price"],mfe_r,mae_r,
                            s.get("rsi"),s.get("adx"),"N/A", # h1_trend не сохраняем
                            s.get("bb_pos"),s.get("reason"),
                            s.get("confidence_score"), s.get("position_size_usd"), s.get("leverage"),
                            pnl_usd, pnl_percent
                        ]
                        await asyncio.to_thread(TRADE_LOG_WS.append_row,row,value_input_option='USER_ENTERED')
                    
                    status_emoji = "✅" if hit == "TP_HIT" else "❌"
                    msg = (f"{status_emoji} <b>СДЕЛКА ЗАКРЫТА</b> ({hit})\n\n"
                           f"<b>Инструмент:</b> <code>{s['pair']}</code>\n"
                           f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent:+.2f}%)</b>")
                    await broadcast(app, msg)
                    state["monitored_signals"].remove(s); save_state()
            except Exception as e: log.error("Monitor %s: %s", s.get("signal_id"), e)
        await asyncio.sleep(60)

async def daily_pnl_report(app):
    while True:
        # Ждем до следующего дня, до 00:05 UTC
        now = datetime.now(timezone.utc)
        tomorrow = now + timedelta(days=1)
        next_run = tomorrow.replace(hour=0, minute=5, second=0, microsecond=0)
        await asyncio.sleep((next_run - now).total_seconds())

        if not TRADE_LOG_WS: continue
        log.info("Running daily P&L report...")

        try:
            records = await asyncio.to_thread(TRADE_LOG_WS.get_all_records)
            now = datetime.now(timezone.utc)
            total_pnl = 0
            wins = 0
            losses = 0

            for rec in records:
                try:
                    exit_time_str = rec.get("Exit_Time_UTC")
                    if not exit_time_str: continue
                    
                    exit_time = datetime.fromisoformat(exit_time_str).replace(tzinfo=timezone.utc)
                    if now - exit_time < timedelta(days=1):
                        pnl = float(rec.get("PNL_USD", 0))
                        total_pnl += pnl
                        if pnl > 0: wins += 1
                        elif pnl < 0: losses += 1
                except (ValueError, TypeError):
                    continue

            if wins > 0 or losses > 0:
                msg = (f"📈 <b>Ежедневный отчет по P&L</b>\n\n"
                       f"<b>Результат за 24ч:</b> ${total_pnl:+.2f}\n"
                       f"<b>Прибыльных сделок:</b> {wins}\n"
                       f"<b>Убыточных сделок:</b> {losses}")
                await broadcast(app, msg)

        except Exception as e:
            log.error(f"Daily P&L report failed: {e}")


# === Telegram commands =====================================================
async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    cid=update.effective_chat.id
    ctx.application.chat_ids.add(cid)
    if not state["bot_on"]:
        state["bot_on"]=True; save_state()
        await update.message.reply_text("✅ <b>Бот v3.1 запущен.</b>"); 
        asyncio.create_task(scanner(ctx.application))
        asyncio.create_task(monitor(ctx.application))
        asyncio.create_task(daily_pnl_report(ctx.application))
    else:
        await update.message.reply_text("ℹ️ Бот уже запущен.")

async def cmd_stop(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    state["bot_on"]=False; save_state(); await update.message.reply_text("🛑 <b>Бот остановлен.</b>")

async def cmd_status(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    snapshot = await get_market_snapshot()
    msg = (f"<b>Состояние бота:</b> {'✅ ON' if state['bot_on'] else '🛑 OFF'}\n"
           f"<b>Активных сигналов:</b> {len(state['monitored_signals'])}/{MAX_CONCURRENT_SIGNALS}\n"
           f"<b>Режим рынка:</b> {snapshot['regime']}\n"
           f"<b>Волатильность:</b> {snapshot['volatility']} (ATR {snapshot['volatility_percent']})")
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)

# === Entrypoint ============================================================
if __name__=="__main__":
    load_state(); setup_sheets()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = set(CHAT_IDS)
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status",cmd_status))
    if state["bot_on"]:
        asyncio.create_task(scanner(app))
        asyncio.create_task(monitor(app))
        asyncio.create_task(daily_pnl_report(app))
    log.info("Bot v3.1 started.")
    app.run_polling()
