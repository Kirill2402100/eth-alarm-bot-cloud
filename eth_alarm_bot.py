#!/usr/bin/env python3
# ============================================================================
# v3.0 - Weighted Decision System
# Changelog 10‑Jul‑2025 (Europe/Belgrade):
# • New Module: LLM now scores candidates 1-10 instead of choosing one.
# • New Module: Dynamic Position Sizing ($50/$30/$20) based on LLM score.
# • New Module: Market Regime Filter based on BTC Daily EMA(50). Blocks LONGs in bearish market.
# • New Module: P&L is now calculated and logged to Google Sheets.
# • Strategy Tuning: Initial filters (H1 Trend, ADX) are relaxed to feed more candidates to the LLM.
# ============================================================================

import os, asyncio, json, logging, uuid
from datetime import datetime, timezone, timedelta

import pandas as pd
import pandas_ta as ta
import aiohttp, gspread, ccxt.async_support as ccxt
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
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
for n in ("httpx", "httpcore"):
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

# ИЗМЕНЕНИЕ: Добавлены новые колонки для расширенной аналитики
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
STATE_FILE = "bot_state_v3_0.json" # Новое имя файла для новой версии
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
# ИЗМЕНЕНИЕ: Ослабление фильтра ADX
MIN_M15_ADX  = 20
MIN_CONFIDENCE_SCORE = 6 # Минимальный балл от LLM для входа в сделку

# === LLM prompt ===========================================================
# ИЗМЕНЕНИЕ: Новый промпт для скоринга
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
                # Ожидаем, что LLM вернет объект с ключом "candidates" или просто массив
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
        try: await app.bot.send_message(chat_id=cid, text=txt, parse_mode="HTML")
        except Exception as e: log.error("Send fail %s: %s", cid, e)

# === Main loops ===========================================================
async def get_market_regime():
    try:
        ohlcv_btc = await exchange.fetch_ohlcv('BTC/USDT', '1d', limit=51)
        df_btc = pd.DataFrame(ohlcv_btc, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df_btc.ta.ema(length=50, append=True)
        last_btc = df_btc.iloc[-1]
        if last_btc['close'] < last_btc['EMA_50']:
            return "BEARISH"
        return "BULLISH"
    except Exception as e:
        log.warning(f"Could not fetch BTC market regime: {e}")
        return "BULLISH" # По умолчанию разрешаем все сделки

async def scanner(app):
    while state["bot_on"]:
        try:
            scan_start_time = datetime.now(timezone.utc)
            if len(state["monitored_signals"]) >= MAX_CONCURRENT_SIGNALS:
                await asyncio.sleep(300); continue
            
            # ИЗМЕНЕНИЕ: Фильтр рыночного режима
            market_regime = await get_market_regime()
            log.info(f"Market Regime: {market_regime}")
            if market_regime == "BEARISH":
                await broadcast(app, "📉 Market regime is BEARISH. LONG trades are disabled.")

            now = datetime.now(timezone.utc).timestamp()
            state["cooldown"] = {p:t for p,t in state["cooldown"].items() if now-t < COOLDOWN_HOURS*3600}
            
            await broadcast(app, f"🔍 Stage 1: scanning top‑{COIN_LIST_SIZE} for setups (ADX≥{MIN_M15_ADX})...")
            
            log.info("Fetching tickers from exchange...")
            tickers = await exchange.fetch_tickers()
            log.info(f"Successfully fetched {len(tickers)} tickers.")
            
            pairs = sorted(
                ((s,t) for s,t in tickers.items() if s.endswith(':USDT') and t['quoteVolume']),
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

                    if f"ATR_{ATR_LEN}" not in df15.columns or pd.isna(last15[f"ATR_{ATR_LEN}"]):
                        continue

                    adx = last15["ADX_14"]; side = None
                    if adx < MIN_M15_ADX: continue
                    
                    side = None
                    for i in range(1, 4):
                        cur, prev = df15.iloc[-i], df15.iloc[-i-1]
                        if prev["EMA_9"] <= prev["EMA_21"] and cur["EMA_9"] > cur["EMA_21"]: side = "LONG"; break
                        if prev["EMA_9"] >= prev["EMA_21"] and cur["EMA_9"] < cur["EMA_21"]: side = "SHORT"; break
                    if not side: continue
                    
                    # Применяем фильтр режима рынка
                    if market_regime == "BEARISH" and side == "LONG": continue

                    if (last15["high"]-last15["low"]) > last15[f"ATR_{ATR_LEN}"]*ANOMALOUS_CANDLE_MULT: continue
                    
                    # ИЗМЕНЕНИЕ: Ослабление H1 фильтра
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
                await broadcast(app, f"✅ Scan finished. No setups found. Took {duration:.0f}s.")
                await asyncio.sleep(900); continue

            await broadcast(app, f"📊 Stage 2: {len(pre)} candidates → enriching for LLM.")
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
                    bb_pos="Inside"
                    if entry>last["BBU_20_2.0"]: bb_pos="Above_Upper"
                    elif entry<last["BBL_20_2.0"]: bb_pos="Below_Lower"
                    
                    setups_for_llm.append({
                        "pair":c["pair"], "side":c["side"], "entry_price":entry,
                        "adx":round(float(last["ADX_14"]),2), "rsi":round(float(last["RSI_14"]),2),
                        "bb_pos":bb_pos, "sl_atr_mult": SL_ATR_MULT, "rr_ratio": RR_RATIO,
                        "atr_val": last[f"ATR_{ATR_LEN}"]
                    })
                except Exception as e: log.warning("Enrich %s: %s", c["pair"], e)
            
            if not setups_for_llm:
                duration = (datetime.now(timezone.utc) - scan_start_time).total_seconds()
                await broadcast(app, f"✅ Scan finished. No setups passed enrichment. Took {duration:.0f}s.")
                await asyncio.sleep(900); continue

            await broadcast(app, f"🤖 Stage 3: Sending {len(setups_for_llm)} setups to LLM for scoring...")
            scored_candidates = await ask_llm(PROMPT + "\n\n" + json.dumps({"candidates":setups_for_llm}))
            
            if not scored_candidates:
                await broadcast(app, "ℹ️ LLM did not return any scored candidates.")
                await asyncio.sleep(900); continue
            
            # ИЗМЕНЕНИЕ: Новая логика выбора и динамического сайзинга
            final_candidates = []
            for i, scored in enumerate(scored_candidates):
                original_setup = setups_for_llm[i]
                if scored.get('confidence_score', 0) >= MIN_CONFIDENCE_SCORE:
                    original_setup.update(scored)
                    final_candidates.append(original_setup)
            
            if not final_candidates:
                await broadcast(app, f"✅ Scan finished. No setups met min score of {MIN_CONFIDENCE_SCORE}. Took {(datetime.now(timezone.utc) - scan_start_time).total_seconds():.0f}s.")
                await asyncio.sleep(900); continue

            # Сортировка: сначала по убыванию score, потом по убыванию adx
            final_candidates.sort(key=lambda x: (x.get('confidence_score', 0), x.get('adx', 0)), reverse=True)
            best_setup = final_candidates[0]
            
            # Определение размера позиции
            score = best_setup.get('confidence_score', 0)
            if score >= 9: position_size_usd = 50
            elif score >= 7: position_size_usd = 30
            else: position_size_usd = 20

            # Расчет SL/TP
            entry = best_setup["entry_price"]
            risk = best_setup["atr_val"] * SL_ATR_MULT
            sl,tp = (entry-risk, entry+risk*RR_RATIO) if best_setup["side"]=="LONG" else (entry+risk, entry-risk*RR_RATIO)
            
            sig_id = str(uuid.uuid4())[:8]
            signal_to_monitor = {
                "signal_id": sig_id,
                "entry_time_utc": datetime.now(timezone.utc).isoformat(),
                "pair": best_setup["pair"], "side": best_setup["side"],
                "entry_price": entry, "sl": sl, "tp": tp,
                "mfe_price": entry, "mae_price": entry,
                "confidence_score": score,
                "position_size_usd": position_size_usd,
                "leverage": 100,
                "reason": best_setup.get("reason"),
                "adx": best_setup.get("adx"), "rsi": best_setup.get("rsi"),
                "bb_pos": best_setup.get("bb_pos")
            }
            
            state["monitored_signals"].append(signal_to_monitor)
            state["cooldown"][best_setup["pair"]] = now
            save_state()
            
            msg = (f"🚀 **NEW SETUP**\n\n"
                   f"**Pair:** {best_setup['pair']} ({best_setup['side']})\n"
                   f"**Score:** {score}/10\n"
                   f"**Recommended Size:** ${position_size_usd} (Leverage 100x)\n"
                   f"**Reason:** {best_setup.get('reason', 'N/A')}")
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
                
                # MFE/MAE
                if s["side"]=="LONG":
                    if price>s["mfe_price"]: s["mfe_price"]=price
                    if price<s["mae_price"]: s["mae_price"]=price
                else:
                    if price<s["mfe_price"]: s["mfe_price"]=price
                    if price>s["mae_price"]: s["mae_price"]=price
                
                hit=None
                if (s["side"]=="LONG" and price>=s["tp"]) or (s["side"]=="SHORT" and price<=s["tp"]): hit="TP_HIT"
                elif (s["side"]=="LONG" and price<=s["sl"]) or (s["side"]=="SHORT" and price>=s["sl"]): hit="SL_HIT"
                
                if hit:
                    risk = abs(s["entry_price"]-s["sl"])
                    mfe_r = round(abs(s["mfe_price"]-s["entry_price"])/risk, 2) if risk > 0 else 0
                    mae_r = round(abs(s["mae_price"]-s["entry_price"])/risk, 2) if risk > 0 else 0
                    
                    # ИЗМЕНЕНИЕ: Расчет PNL
                    leverage = s.get("leverage", 100)
                    position_size = s.get("position_size_usd", 0)
                    exit_price = s['tp'] if hit == 'TP_HIT' else s['sl']
                    
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
                            s.get("rsi"),s.get("adx"),s.get("h1_trend"),
                            s.get("bb_pos"),s.get("reason"),
                            s.get("confidence_score"), s.get("position_size_usd"), s.get("leverage"),
                            pnl_usd, pnl_percent
                        ]
                        await asyncio.to_thread(TRADE_LOG_WS.append_row,row,value_input_option='USER_ENTERED')
                    
                    await broadcast(app, f"✖️ Closed {s['pair']} ({hit}) @ {fmt(exit_price)}\nPNL: ${pnl_usd:.2f} ({pnl_percent:.2f}%)")
                    state["monitored_signals"].remove(s); save_state()
            except Exception as e: log.error("Monitor %s: %s", s.get("signal_id"), e)
        await asyncio.sleep(60)

# === Telegram commands =====================================================
async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    cid=update.effective_chat.id
    ctx.application.chat_ids.add(cid)
    if not state["bot_on"]:
        state["bot_on"]=True; save_state()
        await update.message.reply_text("✅ Бот v3.0 запущен."); asyncio.create_task(scanner(ctx.application)); asyncio.create_task(monitor(ctx.application))
    else:
        await update.message.reply_text("ℹ️ Уже запущен.")

async def cmd_stop(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    state["bot_on"]=False; save_state(); await update.message.reply_text("🛑 Бот остановлен.")

async def cmd_status(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    msg=f"<b>Status:</b> {'ON' if state['bot_on'] else 'OFF'}\nSignals: {len(state['monitored_signals'])}/{MAX_CONCURRENT_SIGNALS}"
    await update.message.reply_text(msg, parse_mode="HTML")

# === Entrypoint ============================================================
if __name__=="__main__":
    load_state(); setup_sheets()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = set(CHAT_IDS)
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status",cmd_status))
    if state["bot_on"]:
        asyncio.create_task(scanner(app)); asyncio.create_task(monitor(app))
    log.info("Bot v3.0 started.")
    app.run_polling()
