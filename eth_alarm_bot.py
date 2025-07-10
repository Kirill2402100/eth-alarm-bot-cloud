#!/usr/bin/env python3
# ============================================================================
# v4.6 - Final Data Fix
# Changelog 10‑Jul‑2025 (Europe/Belgrade):
# • Final Fix: The fetch_tickers call is now filtered to request ONLY spot
#   tickers, ensuring data requests match the available market data.
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
STATE_FILE = "bot_state_v4_6.json"
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
exchange_data = ccxt.bybit({'options': {'defaultType':'spot'}})
exchange_exec = ccxt.mexc({'options': {'defaultType':'swap'}})

TF_ENTRY  = os.getenv("TF_ENTRY", "15m")
ATR_LEN   = 14
SL_ATR_MULT  = 1.5
RR_RATIO     = 1.5
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
    payload = { "model": LLM_MODEL_ID, "messages":[{"role":"user","content":prompt}], "temperature":0.4, "response_format":{"type":"json_object"} }
    hdrs = {"Authorization":f"Bearer {LLM_API_KEY}","Content-Type":"application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, json=payload, headers=hdrs, timeout=180) as r:
                data = await r.json()
                content = data["choices"][0]["message"]["content"]
                response_data = json.loads(content)
                if isinstance(response_data, dict) and "candidates" in response_data: return response_data["candidates"]
                elif isinstance(response_data, list): return response_data
                else: log.error(f"LLM returned unexpected format: {response_data}"); return []
    except Exception as e:
        log.error("LLM error: %s", e); return None

# === Utils ===============================================================
async def broadcast(app, txt:str):
    for cid in getattr(app,"chat_ids", CHAT_IDS):
        try: await app.bot.send_message(chat_id=cid, text=txt, parse_mode=constants.ParseMode.HTML)
        except Exception as e: log.error("Send fail %s: %s", cid, e)

# === Main loops ===========================================================
async def get_market_snapshot():
    try:
        ohlcv_btc = await exchange_data.fetch_ohlcv('BTC/USDT', '1d', limit=51, params={'category':'spot'})
        if not ohlcv_btc: raise ValueError("Received empty OHLCV data for BTC")
        
        df_btc = pd.DataFrame(ohlcv_btc, columns=["timestamp", "open", "high", "low", "close", "volume"])
        for col in ["open", "high", "low", "close", "volume"]: df_btc[col] = pd.to_numeric(df_btc[col])
        
        df_btc.ta.ema(length=50, append=True)
        df_btc.ta.atr(length=14, append=True)
        last_btc = df_btc.iloc[-1]
        
        regime = "BULLISH"
        if last_btc['close'] < last_btc['EMA_50']: regime = "BEARISH"
        
        absolute_atr = last_btc['ATR_14']
        close_price = last_btc['close']
        atr_percent = (absolute_atr / close_price) * 100 if close_price > 0 else 0

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
            if len(state["monitored_signals"]) >= MAX_CONCURRENT_SIGNALS: await asyncio.sleep(300); continue
            
            snapshot = await get_market_snapshot()
            market_regime = snapshot['regime']
            
            msg = (f"🔍 <b>Начинаю сканирование рынка (Источник: Bybit Spot)...</b>\n"
                   f"<i>Режим:</i> {market_regime} | <i>Волатильность:</i> {snapshot['volatility']} (ATR {snapshot['volatility_percent']})")
            await broadcast(app, msg)

            if market_regime == "BEARISH": await broadcast(app, "❗️ <b>Рынок в медвежьей фазе.</b> Длинные позиции (LONG) отключены.")

            now = datetime.now(timezone.utc).timestamp()
            state["cooldown"] = {p:t for p,t in state["cooldown"].items() if now-t < COOLDOWN_HOURS*3600}
            
            # ИЗМЕНЕНИЕ: Запрашиваем тикеры только со спотового рынка
            tickers = await exchange_data.fetch_tickers(params={'category': 'spot'})
            pairs = sorted(
                ((s,t) for s,t in tickers.items() if s.endswith('/USDT') and t.get('quoteVolume')),
                key=lambda x:x[1]['quoteVolume'], reverse=True
            )[:COIN_LIST_SIZE]
            
            rejection_stats = { "ERRORS": 0, "INSUFFICIENT_DATA": 0, "LOW_ADX": 0, "NO_CROSS": 0, "H1_TAILWIND": 0, "ANOMALOUS_CANDLE": 0, "MARKET_REGIME": 0 }
            
            pre = []
            for i, (sym, _) in enumerate(pairs):
                if len(pre)>=10: break
                if sym in state["cooldown"]: continue
                try:
                    log.info(f"Scanning ({i+1}/{len(pairs)}): {sym}")
                    df15 = pd.DataFrame (await exchange_data.fetch_ohlcv(sym, TF_ENTRY, limit=40),
                        columns=["timestamp", "open", "high", "low", "close", "volume"])
                    if len(df15) < 35:
                        rejection_stats["INSUFFICIENT_DATA"] += 1; continue
                    
                    numeric_cols = ["open", "high", "low", "close", "volume"]
                    for col in numeric_cols: df15[col] = pd.to_numeric(df15[col])

                    df15.ta.ema(length=9, append=True); df15.ta.ema(length=21, append=True)
                    df15.ta.atr(length=ATR_LEN, append=True); df15.ta.adx(length=14, append=True)
                    last15 = df15.iloc[-1]

                    if f"ATR_{ATR_LEN}" not in df15.columns or pd.isna(last15[f"ATR_{ATR_LEN}"]):
                        rejection_stats["INSUFFICIENT_DATA"] += 1; continue

                    adx = last15["ADX_14"]; side = None
                    if adx < MIN_M15_ADX:
                        rejection_stats["LOW_ADX"] += 1; continue
                    
                    side = None
                    for j in range(1, 4):
                        cur, prev = df15.iloc[-j], df15.iloc[-j-1]
                        if prev["EMA_9"] <= prev["EMA_21"] and cur["EMA_9"] > cur["EMA_21"]: side = "LONG"; break
                        if prev["EMA_9"] >= prev["EMA_21"] and cur["EMA_9"] < cur["EMA_21"]: side = "SHORT"; break
                    if not side:
                        rejection_stats["NO_CROSS"] += 1; continue
                    
                    if market_regime == "BEARISH" and side == "LONG":
                        rejection_stats["MARKET_REGIME"] += 1; continue

                    if (last15["high"]-last15["low"]) > last15[f"ATR_{ATR_LEN}"]*ANOMALOUS_CANDLE_MULT:
                        rejection_stats["ANOMALOUS_CANDLE"] += 1; continue
                    
                    df1h = pd.DataFrame(await exchange_data.fetch_ohlcv(sym, "1h", limit=51),
                        columns=["timestamp", "open", "high", "low", "close", "volume"])
                    for col in numeric_cols: df1h[col] = pd.to_numeric(df1h[col])

                    df1h.ta.ema(length=50,append=True)
                    last1h = df1h.iloc[-1]
                    
                    if pd.isna(last1h['EMA_50']):
                        rejection_stats["INSUFFICIENT_DATA"] += 1; continue

                    if (side == "LONG" and last1h['close'] < last1h['EMA_50']) or \
                       (side == "SHORT" and last1h['close'] > last1h['EMA_50']):
                        rejection_stats["H1_TAILWIND"] += 1; continue

                    pre.append({"pair":sym, "side":side})
                except Exception as e:
                    rejection_stats["ERRORS"] += 1
                    log.warning(f"Scan ERROR on {sym}: {e}")
                
                await asyncio.sleep(0.5)
            
            if not pre:
                duration = (datetime.now(timezone.utc) - scan_start_time).total_seconds()
                report_msg = (f"✅ <b>Поиск завершен за {duration:.0f} сек.</b> Кандидатов нет.\n\n"
                              f"📊 <b>Диагностика фильтров (из {len(pairs)} монет):</b>\n"
                              f"<code>- {rejection_stats['ERRORS']:<4}</code> отсеяно из-за ошибок\n"
                              f"<code>- {rejection_stats['INSUFFICIENT_DATA']:<4}</code> отсеяно по недостатку данных\n"
                              f"<code>- {rejection_stats['LOW_ADX']:<4}</code> отсеяно по низкому ADX\n"
                              f"<code>- {rejection_stats['H1_TAILWIND']:<4}</code> отсеяно по H1 фильтру\n"
                              f"<code>- {rejection_stats['NO_CROSS']:<4}</code> не найдено пересечения EMA\n"
                              f"<code>- {rejection_stats['ANOMALOUS_CANDLE']:<4}</code> отсеяно по аномальной свече\n"
                              f"<code>- {rejection_stats['MARKET_REGIME']:<4}</code> отсеяно из-за режима рынка")
                await broadcast(app, report_msg); await asyncio.sleep(900); continue

            # ... (Остальная логика без изменений)
            
        except Exception as e:
            log.error("Scanner critical: %s", e, exc_info=True)
            await asyncio.sleep(300)

async def monitor(app):
    while state["bot_on"]:
        if not state["monitored_signals"]:
            await asyncio.sleep(30)
            continue
        for s in list(state["monitored_signals"]):
            try:
                price = (await exchange_data.fetch_ticker(s["pair"]))["last"]
                if not price: continue
                
                if s["side"]=="LONG":
                    if price>s["mfe_price"]: s["mfe_price"]=price
                    if price<s["mae_price"]: s["mae_price"]=price
                else:
                    if price<s["mfe_price"]: s["mfe_price"]=price
                    if price>s["mae_price"]: s["mae_price"]=price
                
                hit=None; exit_price = None
                if (s["side"]=="LONG" and price>=s["tp"]): hit, exit_price = "TP_HIT", s["tp"]
                elif (s["side"]=="SHORT" and price<=s["tp"]): hit, exit_price = "TP_HIT", s["tp"]
                elif (s["side"]=="LONG" and price<=s["sl"]): hit, exit_price = "SL_HIT", s["sl"]
                elif (s["side"]=="SHORT" and price>=s["sl"]): hit, exit_price = "SL_HIT", s["sl"]
                
                if hit:
                    risk = abs(s["entry_price"]-s["sl"])
                    mfe_r = round(abs(s["mfe_price"]-s["entry_price"])/risk, 2) if risk > 0 else 0
                    mae_r = round(abs(s["mae_price"]-s["entry_price"])/risk, 2) if risk > 0 else 0
                    
                    leverage = s.get("leverage", 100); position_size = s.get("position_size_usd", 0)
                    price_change = (exit_price - s['entry_price']) / s['entry_price']
                    if s["side"] == "SHORT": price_change = -price_change
                    
                    pnl_percent = price_change * leverage * 100
                    pnl_usd = position_size * (pnl_percent / 100)

                    if TRADE_LOG_WS:
                        row=[
                            s["signal_id"],s["pair"],s["side"],hit, s["entry_time_utc"],datetime.now(timezone.utc).isoformat(),
                            s["entry_price"],exit_price,s["sl"],s["tp"], s["mfe_price"],s["mae_price"],mfe_r,mae_r,
                            s.get("rsi"),s.get("adx"),"N/A", s.get("bb_pos"),s.get("reason"), s.get("confidence_score"),
                            s.get("position_size_usd"), s.get("leverage"), pnl_usd, pnl_percent
                        ]
                        await asyncio.to_thread(TRADE_LOG_WS.append_row,row,value_input_option='USER_ENTERED')
                    
                    status_emoji = "✅" if hit == "TP_HIT" else "❌"
                    msg = (f"{status_emoji} <b>СДЕЛКА ЗАКРЫТА</b> ({hit})\n\n"
                           f"<b>Инструмент:</b> <code>{s['pair']}</code>\n<b>Результат: ${pnl_usd:+.2f} ({pnl_percent:+.2f}%)</b>")
                    await broadcast(app, msg)
                    state["monitored_signals"].remove(s); save_state()
            except Exception as e: log.error("Monitor %s: %s", s.get("signal_id"), e)
        await asyncio.sleep(60)

async def daily_pnl_report(app):
    while True:
        now = datetime.now(timezone.utc)
        tomorrow = now + timedelta(days=1)
        next_run = tomorrow.replace(hour=0, minute=5, second=0, microsecond=0)
        await asyncio.sleep((next_run - now).total_seconds())

        if not TRADE_LOG_WS: continue
        log.info("Running daily P&L report...")

        try:
            records = await asyncio.to_thread(TRADE_LOG_WS.get_all_records)
            now = datetime.now(timezone.utc)
            total_pnl = 0; wins = 0; losses = 0

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
                except (ValueError, TypeError): continue

            if wins > 0 or losses > 0:
                msg = (f"📈 <b>Ежедневный отчет по P&L</b>\n\n"
                       f"<b>Результат за 24ч:</b> ${total_pnl:+.2f}\n"
                       f"<b>Прибыльных сделок:</b> {wins}\n<b>Убыточных сделок:</b> {losses}")
                await broadcast(app, msg)
            else: log.info("No trades closed in the last 24 hours to report.")
        except Exception as e:
            log.error(f"Daily P&L report failed: {e}")

# === Telegram commands =====================================================
async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    cid=update.effective_chat.id
    ctx.application.chat_ids.add(cid)
    if not state["bot_on"]:
        state["bot_on"]=True; save_state()
        await update.message.reply_text("✅ <b>Бот v4.6 (Final Data Fix) запущен.</b>"); 
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
    log.info("Bot v4.6 (Final Data Fix) started.")
    app.run_polling()
