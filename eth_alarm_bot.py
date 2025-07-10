#!/usr/bin/env python3
# ============================================================================
# v8.7 - Strategy v2.6  (Ultimate "On‑the‑Fly" Filtering – refactored)
# Changelog 10‑Jul‑2025 (Europe/Belgrade):
# • Bug‑fix ①  – candle_range now compared to ATR_14 (absolute), not ATRr_14.
# • Bug‑fix ②  – second ADX ≥ 25 check just before LLM call.
# • Risk       – SL = 1.5 × ATR,  TP = 2.2 × ATR  (RR ≈ 1 : 1.47).
# • New metrics logged: H1_ATR_percent, MFE_R, MAE_R, LLM_Confidence.
# • Google‑Sheets: logs now go to worksheet 'Autonomous_Trade_Log_v5'.
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
BOT_TOKEN              = os.getenv("BOT_TOKEN")
CHAT_IDS               = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
SHEET_ID               = os.getenv("SHEET_ID")
COIN_LIST_SIZE         = int(os.getenv("COIN_LIST_SIZE", "300"))
MAX_CONCURRENT_SIGNALS = int(os.getenv("MAX_CONCURRENT_SIGNALS", "10"))
ANOMALOUS_CANDLE_MULT  = 3.0
COOLDOWN_HOURS         = 4

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

HEADERS = [
    "Signal_ID","Pair","Side","Status",
    "Entry_Time_UTC","Exit_Time_UTC",
    "Entry_Price","Exit_Price","SL_Price","TP_Price",
    "MFE_Price","MAE_Price","MFE_R","MAE_R",
    "Entry_RSI","Entry_ADX","H1_Trend_at_Entry","H1_ATR_percent",
    "Entry_BB_Position","LLM_Reason","LLM_Confidence"
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
STATE_FILE = "bot_state_v2_6.json"
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
exchange  = ccxt.mexc({'options': {'defaultType':'swap'}})
TF_ENTRY  = os.getenv("TF_ENTRY", "15m")
ATR_LEN   = 14
SL_ATR_MULT  = 1.5
RR_RATIO     = 2.2
MIN_M15_ADX  = 25

# === LLM prompt ===========================================================
PROMPT = (
    "Ты — главный трейдер-аналитик. Перед тобой список candidates, уже прошедших "
    "тройной фильтр (M15 EMA‑кросс, H1 устойчивый тренд, ADX≥25). "
    "Выбери ровно ОДИН лучший сетап по комбинации (ADX, RSI). "
    "Верни JSON: либо весь объект кандидата + field 'reason', либо "
    "{'decision':'REJECT','reason':'...'}."
)

async def ask_llm(prompt: str):
    if not LLM_API_KEY: return None
    payload = {
        "model": LLM_MODEL_ID,
        "messages":[{"role":"user","content":prompt}],
        "temperature":0.4,
        "response_format":{"type":"json_object"},
        "logprobs":True
    }
    hdrs = {"Authorization":f"Bearer {LLM_API_KEY}","Content-Type":"application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, json=payload, headers=hdrs, timeout=180) as r:
                data = await r.json()
                content = data["choices"][0]["message"]["content"]
                obj = json.loads(content)
                obj["llm_confidence"] = data["choices"][0].get("logprobs",{}).get("token_logprobs","N/A")
                return obj
    except Exception as e:
        log.error("LLM error: %s", e)
        return None

# === Utils ===============================================================
async def broadcast(app, txt:str):
    for cid in getattr(app,"chat_ids", CHAT_IDS):
        try: await app.bot.send_message(chat_id=cid, text=txt, parse_mode="HTML")
        except Exception as e: log.error("Send fail %s: %s", cid, e)

# === Main loops ===========================================================
async def scanner(app):
    while state["bot_on"]:
        try:
            if len(state["monitored_signals"]) >= MAX_CONCURRENT_SIGNALS:
                await asyncio.sleep(300); continue
            # purge cooldown
            now = datetime.now(timezone.utc).timestamp()
            state["cooldown"] = {p:t for p,t in state["cooldown"].items() if now-t < COOLDOWN_HOURS*3600}
            # Stage 1 – initial scan
            await broadcast(app, f"🔍 Stage 1: scanning top‑{COIN_LIST_SIZE} for EMA‑cross + ADX≥{MIN_M15_ADX} ...")
            tickers = await exchange.fetch_tickers()
            pairs = sorted(
                ((s,t) for s,t in tickers.items() if s.endswith(':USDT') and t['quoteVolume']),
                key=lambda x:x[1]['quoteVolume'], reverse=True
            )[:COIN_LIST_SIZE]
            pre = []
            for sym,_ in pairs:
                if len(pre)>=10: break
                if sym in state["cooldown"]: continue
                try:
                    df15 = pd.DataFrame(await exchange.fetch_ohlcv(sym, TF_ENTRY, limit=50),
                                        columns=["ts","o","h","l","c","v"])
                    if len(df15)<30: continue
                    df15.ta.ema(length=9, append=True)
                    df15.ta.ema(length=21, append=True)
                    df15.ta.atr(length=ATR_LEN, append=True)   # ATR_14
                    df15.ta.adx(length=14, append=True)
                    last, prev = df15.iloc[-1], df15.iloc[-2]
                    adx = last["ADX_14"];  side = None
                    if adx < MIN_M15_ADX: continue
                    # EMA cross
                    if prev["EMA_9"]<=prev["EMA_21"] and last["EMA_9"]>last["EMA_21"]: side="LONG"
                    elif prev["EMA_9"]>=prev["EMA_21"] and last["EMA_9"]<last["EMA_21"]: side="SHORT"
                    if not side: continue
                    # anomalous candle
                    atr = last[f"ATR_{ATR_LEN}"]
                    if (last["h"]-last["l"]) > atr*ANOMALOUS_CANDLE_MULT: continue
                    # H1 trend
                    df1h = pd.DataFrame(await exchange.fetch_ohlcv(sym,'1h',limit=100),
                                        columns=["ts","o","h","l","c","v"])
                    df1h.ta.ema(length=9,append=True); df1h.ta.ema(length=21,append=True); df1h.ta.ema(length=50,append=True)
                    recent = df1h.iloc[-3:]
                    up   = all(r["EMA_9"]>r["EMA_21"]>r["EMA_50"] for _,r in recent.iterrows())
                    down = all(r["EMA_9"]<r["EMA_21"]<r["EMA_50"] for _,r in recent.iterrows())
                    if (side=="LONG" and not up) or (side=="SHORT" and not down): continue
                    pre.append({"pair":sym,"side":side,"h1_trend":"UP" if up else "DOWN"})
                except Exception as e:
                    log.warning("Scan %s: %s", sym, e)
            if not pre:
                await asyncio.sleep(900); continue
            # Stage 2 – enrich
            await broadcast(app, f"📊 Stage 2: {len(pre)} candidates → enrich for LLM.")
            setups=[]
            for c in pre:
                try:
                    df = pd.DataFrame(await exchange.fetch_ohlcv(c["pair"], TF_ENTRY, limit=100),
                                      columns=["ts","o","h","l","c","v"])
                    df.ta.bbands(length=20, std=2, append=True)
                    df.ta.atr(length=ATR_LEN, append=True)
                    df.ta.rsi(length=14, append=True)
                    df.ta.adx(length=14, append=True)
                    last = df.iloc[-1]
                    adx = round(float(last["ADX_14"]),2)
                    if adx < MIN_M15_ADX: continue   # second gate
                    entry = last["c"]; risk = last[f"ATR_{ATR_LEN}"]*SL_ATR_MULT
                    sl,tp = (entry-risk, entry+risk*RR_RATIO) if c["side"]=="LONG" else (entry+risk, entry-risk*RR_RATIO)
                    bb_pos="Inside"
                    if entry>last["BBU_20_2.0"]: bb_pos="Above_Upper"
                    elif entry<last["BBL_20_2.0"]: bb_pos="Below_Lower"
                    # H1 ATR%
                    df1h = pd.DataFrame(await exchange.fetch_ohlcv(c["pair"],'1h',limit=60),
                                        columns=["ts","o","h","l","c","v"])
                    df1h.ta.atr(length=ATR_LEN, append=True)
                    h1_atr_pct = round(float(df1h.iloc[-1][f"ATR_{ATR_LEN}"]/df1h.iloc[-1]["c"]*100),2)
                    setups.append({
                        "pair":c["pair"],"side":c["side"],"entry_price":entry,"sl":sl,"tp":tp,
                        "h1_trend":c["h1_trend"],"adx":adx,"rsi":round(float(last["RSI_14"]),2),
                        "bb_pos":bb_pos,"h1_atr_pct":h1_atr_pct
                    })
                except Exception as e: log.warning("Enrich %s: %s", c["pair"], e)
            if not setups:
                await asyncio.sleep(900); continue
            # Stage 3 – LLM
            await broadcast(app, "🤖 Stage 3: sending setups to LLM ...")
            llm_obj = await ask_llm(PROMPT + "\n\n" + json.dumps({"candidates":setups}))
            if llm_obj and llm_obj.get("pair"):
                sig_id = str(uuid.uuid4())[:8]
                llm_obj.update({
                    "signal_id":sig_id,
                    "entry_time_utc":datetime.now(timezone.utc).isoformat(),
                    "mfe_price":llm_obj["entry_price"],
                    "mae_price":llm_obj["entry_price"]
                })
                state["monitored_signals"].append(llm_obj)
                state["cooldown"][llm_obj["pair"]] = now
                save_state()
                await broadcast(app, f"🚀 NEW SETUP {llm_obj['pair']} ({llm_obj['side']}) – ID {sig_id}")
            else:
                await broadcast(app, f"ℹ️ LLM rejected: {llm_obj.get('reason','no response') if llm_obj else 'no response'}")
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
                if (s["side"]=="LONG" and price>=s["tp"]) or (s["side"]=="SHORT" and price<=s["tp"]):
                    hit="TP_HIT"
                elif (s["side"]=="LONG" and price<=s["sl"]) or (s["side"]=="SHORT" and price>=s["sl"]):
                    hit="SL_HIT"
                if hit:
                    risk = abs(s["entry_price"]-s["sl"])
                    mfe_r = round(abs(s["mfe_price"]-s["entry_price"])/risk,2)
                    mae_r = round(abs(s["mae_price"]-s["entry_price"])/risk,2)
                    if TRADE_LOG_WS:
                        row=[
                            s["signal_id"],s["pair"],s["side"],hit,
                            s["entry_time_utc"],datetime.now(timezone.utc).isoformat(),
                            s["entry_price"],price,s["sl"],s["tp"],
                            s["mfe_price"],s["mae_price"],mfe_r,mae_r,
                            s.get("rsi"),s.get("adx"),s.get("h1_trend"),s.get("h1_atr_pct"),
                            s.get("bb_pos"),s.get("reason"),s.get("llm_confidence","N/A")
                        ]
                        await asyncio.to_thread(TRADE_LOG_WS.append_row,row,value_input_option='USER_ENTERED')
                    await broadcast(app, f"✖️ Closed {s['pair']} ({hit}) @ {fmt(price)}")
                    state["monitored_signals"].remove(s); save_state()
            except Exception as e: log.error("Monitor %s: %s", s.get("signal_id"), e)
        await asyncio.sleep(60)

# === Telegram commands =====================================================
async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    cid=update.effective_chat.id
    ctx.application.chat_ids.add(cid)
    if not state["bot_on"]:
        state["bot_on"]=True; save_state()
        await update.message.reply_text("✅ Бот v2.6 запущен."); asyncio.create_task(scanner(ctx.application)); asyncio.create_task(monitor(ctx.application))
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
    log.info("Bot v2.6 started.")
    app.run_polling()
