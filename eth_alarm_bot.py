#!/usr/bin/env python3
# ============================================================================
# v3.4.5 - Robust Market Sync Fix
# Changelog 11‑Jul‑2025 (Europe/Belgrade):
# • Moved market loading inside the main loop with forced reload
#   to prevent stale cache issues and ensure accurate spot/futures sync.
# ============================================================================

import os, asyncio, json, logging, uuid
from datetime import datetime, timezone, timedelta

import pandas as pd
import pandas_ta as ta
import aiohttp, gspread, ccxt.async_support as ccxt
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, constants
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

# === ENV / Logging =========================================================
BOT_TOKEN               = os.getenv("BOT_TOKEN")
CHAT_IDS                = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
SHEET_ID                = os.getenv("SHEET_ID")
COIN_LIST_SIZE          = int(os.getenv("COIN_LIST_SIZE", "300"))
MAX_CONCURRENT_SIGNALS  = int(os.getenv("MAX_CONCURRENT_SIGNALS", "10"))
ANOMALOUS_CANDLE_MULT   = 3.0
COOLDOWN_HOURS          = 4
# --- Параметры для динамического RR ---
TREND_ADX_THRESHOLD     = 25
TREND_RR_RATIO          = 1.5
FLAT_RR_RATIO           = 1.0

# --- Черный список стейблкоинов ---
STABLECOIN_BLACKLIST = {'FDUSD', 'USDC', 'DAI', 'USDE', 'TUSD', 'BUSD', 'USDP', 'GUSD', 'USD1'}

LLM_API_KEY  = os.getenv("LLM_API_KEY")
LLM_API_URL  = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_MODEL_ID = os.getenv("LLM_MODEL_ID", "gpt-4o-mini")

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
HEADERS = ["Signal_ID","Pair","Side","Status", "Entry_Time_UTC","Exit_Time_UTC", "Entry_Price","Exit_Price","SL_Price","TP_Price", "MFE_Price","MAE_Price","MFE_R","MAE_R", "Entry_RSI","Entry_ADX","H1_Trend_at_Entry", "Entry_BB_Position","LLM_Reason", "Confidence_Score", "Position_Size_USD", "Leverage", "PNL_USD", "PNL_Percent"]

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
            ws.clear()
            ws.update("A1",[HEADERS])
            ws.format(f"A1:{chr(ord('A')+len(HEADERS)-1)}1",{"textFormat":{"bold":True}})
        TRADE_LOG_WS = ws
        log.info("Google‑Sheets ready – logging to '%s'.", SHEET_NAME)
    except Exception as e:
        log.error("Sheets init failed: %s", e)

# === State ================================================================
STATE_FILE = "bot_state_v6_2.json"
state = {}
def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
        except json.JSONDecodeError:
            state = {} # Start with a fresh state if file is corrupted
    state.setdefault("bot_on", False)
    state.setdefault("monitored_signals", [])
    state.setdefault("cooldown", {})
    log.info("State loaded (%d signals).", len(state["monitored_signals"]))

def save_state():
    with open(STATE_FILE,"w") as f:
        json.dump(state, f, indent=2)

# === Exchange & Strategy ==================================================
exchange_spot = ccxt.mexc({'options': {'defaultType': 'spot'}})
exchange_futures = ccxt.mexc({'options': {'defaultType': 'swap'}})

TF_ENTRY  = os.getenv("TF_ENTRY", "15m")
ATR_LEN, SL_ATR_MULT, MIN_CONFIDENCE_SCORE = 14, 1.5, 6

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df.ta.ema(length=9, append=True, col_names="EMA_9")
    df.ta.ema(length=21, append=True, col_names="EMA_21")
    df.ta.ema(length=50, append=True, col_names="EMA_50")
    df.ta.rsi(length=14, append=True, col_names="RSI_14")
    df.ta.atr(length=ATR_LEN, append=True, col_names=f"ATR_{ATR_LEN}")
    df.ta.adx(length=14, append=True, col_names=(f"ADX_14", f"DMP_14", f"DMN_14"))
    df.ta.bbands(length=20, std=2, append=True, col_names=("BBL_20_2.0", "BBM_20_2.0", "BBU_20_2.0", "BBB_20_2.0", "BBP_20_2.0"))
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# === LLM prompt ===========================================================
PROMPT = """Ты — главный трейдер-аналитик в инвестиционном фонде.

**КОНТЕКСТ:** Моя система уже провела первичный отбор. Все кандидаты ниже имеют подтвержденный тренд на старшем таймфрейме и являются лучшими по силе тренда (ADX) из доступных на рынке. Твоя задача — провести финальный, самый важный анализ.

**ЗАДАЧА:** Проанализируй КАЖДОГО кандидата. Для каждого верни JSON-объект с двумя полями:
1.  `confidence_score`: Твоя уверенность в сделке от 1 до 10. Учитывай комбинацию RSI, ADX и положение цены относительно Полос Боллинджера.
2.  `reason`: Краткое (1-2 предложения) обоснование.
    - Пример для ЛОНГА: "ADX сильный, RSI не в перекупленности, цена оттолкнулась от средней линии BB. Высокая вероятность движения вверх."
    - Пример для ШОРТА: "ADX сильный, RSI не в перепроданности, цена отбилась от верхней линии BB. Высокая вероятность падения."

**ФОРМАТ ОТВЕТА:** JSON-массив с объектами для каждого кандидата. Без лишних слов.
"""

async def ask_llm(prompt: str):
    if not LLM_API_KEY: return None
    payload = { "model": LLM_MODEL_ID, "messages":[{"role":"user","content":prompt}], "temperature":0.4, "response_format":{"type":"json_object"} }
    hdrs = {"Authorization":f"Bearer {LLM_API_KEY}","Content-Type":"application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, json=payload, headers=hdrs, timeout=180) as r:
                r.raise_for_status()
                data = await r.json()
                content = data["choices"][0]["message"]["content"]
                response_data = json.loads(content)
                if isinstance(response_data, dict) and next(iter(response_data.values())) and isinstance(next(iter(response_data.values())), list):
                    return next(iter(response_data.values()))
                elif isinstance(response_data, list):
                    return response_data
                else:
                    log.error(f"LLM returned unexpected format: {response_data}")
                    return []
    except Exception as e:
        log.error("LLM error: %s", e)
        return None

async def broadcast(app, txt:str):
    for cid in getattr(app,"chat_ids", CHAT_IDS):
        try:
            await app.bot.send_message(chat_id=cid, text=txt, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.error("Send fail %s: %s", cid, e)

# === Main loops ===========================================================
async def get_market_snapshot():
    try:
        ohlcv_btc = await exchange_spot.fetch_ohlcv('BTC/USDT', '1d', limit=100)
        if len(ohlcv_btc) < 51: raise ValueError("Received less than 51 candles for BTC")
        df_btc = pd.DataFrame(ohlcv_btc, columns=["timestamp", "open", "high", "low", "close", "volume"])
        for col in ["open", "high", "low", "close", "volume"]: df_btc[col] = pd.to_numeric(df_btc[col])
        df_btc = add_indicators(df_btc)
        if df_btc.empty: raise ValueError("BTC DataFrame is empty")
        last_btc = df_btc.iloc[-1]
        regime = "BULLISH" if last_btc['close'] > last_btc['EMA_50'] else "BEARISH"
        atr_percent = (last_btc[f'ATR_{ATR_LEN}'] / last_btc['close']) * 100 if last_btc['close'] > 0 else 0
        volatility = "Низкая" if atr_percent < 2.5 else "Умеренная" if atr_percent < 5 else "Высокая"
        return {'regime': regime, 'volatility': volatility, 'volatility_percent': f"{atr_percent:.2f}%"}
    except Exception as e:
        log.warning(f"Could not fetch BTC market snapshot: {e}")
        return {'regime': "BULLISH", 'volatility': "N/A", 'volatility_percent': "N/A"}

async def scanner(app):
    while state.get("bot_on", False):
        try:
            # Загрузка рынков перенесена внутрь цикла с принудительным обновлением
            log.info("Force-reloading markets for spot/futures sync...")
            await exchange_spot.load_markets(True)
            await exchange_futures.load_markets(True)
            futures_symbols = set(exchange_futures.markets.keys())
            log.info(f"Loaded {len(futures_symbols)} futures symbols for validation.")

            scan_start_time = datetime.now(timezone.utc)
            if len(state["monitored_signals"]) >= MAX_CONCURRENT_SIGNALS:
                await asyncio.sleep(300); continue
            
            snapshot = await get_market_snapshot()
            market_regime = snapshot['regime']
            msg = (f"🔍 <b>Начинаю сканирование рынка (Источник: {exchange_spot.name})...</b>\n"
                   f"<i>Режим:</i> {market_regime} | <i>Волатильность:</i> {snapshot['volatility']} (ATR {snapshot['volatility_percent']})")
            await broadcast(app, msg)
            if market_regime == "BEARISH":
                await broadcast(app, "❗️ <b>Рынок в медвежьей фазе.</b> Длинные позиции (LONG) отключены.")
            
            now = datetime.now(timezone.utc).timestamp()
            state["cooldown"] = {p:t for p,t in state["cooldown"].items() if now-t < COOLDOWN_HOURS*3600}
            
            spot_tickers = await exchange_spot.fetch_tickers()
            valid_spot_tickers = {s: t for s, t in spot_tickers.items() if s in futures_symbols}
            pairs = sorted(
                ((s,t) for s,t in valid_spot_tickers.items() if s.endswith('/USDT') and t.get('quoteVolume')), 
                key=lambda x:x[1]['quoteVolume'], 
                reverse=True
            )[:COIN_LIST_SIZE]
            
            log.info(f"Found {len(pairs)} USDT pairs present on both Spot and Futures markets.")
            rejection_stats = { "ERRORS": 0, "INSUFFICIENT_DATA": 0, "NO_CROSS": 0, "H1_TAILWIND": 0, "ANOMALOUS_CANDLE": 0, "MARKET_REGIME": 0, "BLACKLISTED": 0 }
            pre_candidates = []

            for i, (sym, _) in enumerate(pairs):
                base_currency = sym.split('/')[0]
                if base_currency in STABLECOIN_BLACKLIST:
                    rejection_stats["BLACKLISTED"] += 1
                    continue

                if sym in state["cooldown"]: continue
                try:
                    ohlcv_data = await exchange_spot.fetch_ohlcv(sym, TF_ENTRY, limit=100)
                    if len(ohlcv_data) < 50: rejection_stats["INSUFFICIENT_DATA"] += 1; continue
                    
                    df15 = pd.DataFrame(ohlcv_data, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    for col in ["open", "high", "low", "close", "volume"]: df15[col] = pd.to_numeric(df15[col])
                    df15 = add_indicators(df15)
                    if len(df15) < 2: rejection_stats["INSUFFICIENT_DATA"] += 1; continue
                    
                    last15, prev15 = df15.iloc[-1], df15.iloc[-2]
                    
                    side = None
                    if prev15["EMA_9"] <= prev15["EMA_21"] and last15["EMA_9"] > last15["EMA_21"]: side = "LONG"
                    elif prev15["EMA_9"] >= prev15["EMA_21"] and last15["EMA_9"] < last15["EMA_21"]: side = "SHORT"
                    
                    if not side: rejection_stats["NO_CROSS"] += 1; continue
                    if market_regime == "BEARISH" and side == "LONG": rejection_stats["MARKET_REGIME"] += 1; continue
                    if (last15["high"]-last15["low"]) > last15[f"ATR_{ATR_LEN}"] * ANOMALOUS_CANDLE_MULT: rejection_stats["ANOMALOUS_CANDLE"] += 1; continue
                    
                    ohlcv_h1 = await exchange_spot.fetch_ohlcv(sym, "1h", limit=100)
                    if len(ohlcv_h1) < 51: rejection_stats["INSUFFICIENT_DATA"] += 1; continue

                    df1h = pd.DataFrame(ohlcv_h1, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    for col in ["open", "high", "low", "close", "volume"]: df1h[col] = pd.to_numeric(df1h[col])
                    df1h = add_indicators(df1h)
                    if df1h.empty: rejection_stats["INSUFFICIENT_DATA"] += 1; continue

                    last1h = df1h.iloc[-1]
                    if (side == "LONG" and last1h['close'] < last1h['EMA_50']) or (side == "SHORT" and last1h['close'] > last1h['EMA_50']):
                        rejection_stats["H1_TAILWIND"] += 1; continue
                        
                    pre_candidates.append({
                        "pair": sym, "side": side, "adx": last15["ADX_14"], "rsi": last15["RSI_14"],
                        "bb_pos": last15["BBP_20_2.0"], "atr": last15[f"ATR_{ATR_LEN}"], "entry_price": last15["close"]
                    })
                except Exception as e:
                    rejection_stats["ERRORS"] += 1; log.warning(f"Scan ERROR on {sym}: {e}")
                await asyncio.sleep(0.5)

            if not pre_candidates:
                duration = (datetime.now(timezone.utc) - scan_start_time).total_seconds()
                report_msg = (f"✅ <b>Поиск завершен за {duration:.0f} сек.</b> Кандидатов нет.\n\n"
                              f"📊 <b>Диагностика фильтров (из {len(pairs)} монет):</b>\n"
                              f"<code>- {rejection_stats['BLACKLISTED']:<4}</code> отсеяно по черному списку\n"
                              f"<code>- {rejection_stats['ERRORS']:<4}</code> отсеяно из-за ошибок\n"
                              f"<code>- {rejection_stats['INSUFFICIENT_DATA']:<4}</code> отсеяно по недостатку данных\n"
                              f"<code>- {rejection_stats['H1_TAILWIND']:<4}</code> отсеяно по H1 фильтру\n"
                              f"<code>- {rejection_stats['NO_CROSS']:<4}</code> не найдено пересечения EMA\n"
                              f"<code>- {rejection_stats['ANOMALOUS_CANDLE']:<4}</code> отсеяно по аномальной свече\n"
                              f"<code>- {rejection_stats['MARKET_REGIME']:<4}</code> отсеяно из-за режима рынка")
                await broadcast(app, report_msg)
            
            else:
                sorted_pre = sorted(pre_candidates, key=lambda x: x['adx'], reverse=True)
                top_candidates = sorted_pre[:10]
                
                llm_prompt_data = [f"- Pair: {c['pair']}, Side: {c['side']}, ADX: {c['adx']:.2f}, RSI: {c['rsi']:.2f}, BB_Pos: {c['bb_pos']:.2f}" for c in top_candidates]
                full_prompt = PROMPT + "\n\n" + "КАНДИДАТЫ ДЛЯ АНАЛИЗА:\n" + "\n".join(llm_prompt_data)
                await broadcast(app, f"📊 <b>Найдено {len(pre_candidates)} кандидатов.</b> Отправляю топ-{len(top_candidates)} с лучшим ADX на анализ LLM...")

                llm_results = await ask_llm(full_prompt)

                if llm_results and len(llm_results) == len(top_candidates):
                    for i, cand in enumerate(top_candidates): cand.update(llm_results[i])
                    
                    valid_candidates = [c for c in top_candidates if c.get('confidence_score', 0) >= MIN_CONFIDENCE_SCORE]
                    if not valid_candidates:
                        await broadcast(app, "🧐 LLM не нашел достаточно уверенных сетапов. Пропускаю цикл.")
                    else:
                        best_candidate = max(valid_candidates, key=lambda x: (x['confidence_score'], x['adx']))

                        rr_ratio = TREND_RR_RATIO if best_candidate['adx'] >= TREND_ADX_THRESHOLD else FLAT_RR_RATIO
                        score = best_candidate['confidence_score']
                        pos_size = 50 if score >= 9 else 30 if score >= 7 else 20
                        
                        entry_price, atr_val = best_candidate['entry_price'], best_candidate['atr']
                        sl_distance = atr_val * SL_ATR_MULT
                        tp_distance = sl_distance * rr_ratio

                        if best_candidate['side'] == 'LONG':
                            sl_price, tp_price = entry_price - sl_distance, entry_price + tp_distance
                        else:
                            sl_price, tp_price = entry_price + sl_distance, entry_price - tp_distance

                        signal_id = str(uuid.uuid4())[:8]
                        signal = {
                            "signal_id": signal_id, "pair": best_candidate['pair'], "side": best_candidate['side'],
                            "entry_price": entry_price, "sl": sl_price, "tp": tp_price,
                            "entry_time_utc": datetime.now(timezone.utc).isoformat(), "status": "ACTIVE", 
                            "mfe_price": entry_price, "mae_price": entry_price,
                            "reason": best_candidate.get('reason', 'N/A'), "confidence_score": score, 
                            "position_size_usd": pos_size, "leverage": 100, 
                            "adx": best_candidate['adx'], "rsi": best_candidate['rsi'], "bb_pos": best_candidate['bb_pos']
                        }
                        state['monitored_signals'].append(signal)
                        state['cooldown'][signal['pair']] = datetime.now(timezone.utc).timestamp()
                        save_state()

                        emoji = "⬆️ LONG" if signal['side'] == 'LONG' else "⬇️ SHORT"
                        msg = (f"<b>🔥 НОВЫЙ СИГНАЛ</b> ({signal_id})\n\n"
                               f"<b>{emoji} {signal['pair']}</b>\n"
                               f"<b>Вход:</b> <code>{fmt(signal['entry_price'])}</code>\n"
                               f"<b>TP:</b> <code>{fmt(signal['tp'])}</code> (RR {rr_ratio:.1f}:1)\n"
                               f"<b>SL:</b> <code>{fmt(signal['sl'])}</code>\n\n"
                               f"<b>LLM Оценка: {signal['confidence_score']}/10</b> | <b>Размер: ${signal['position_size_usd']}</b>\n"
                               f"<i>LLM: \"{signal['reason']}\"</i>")
                        await broadcast(app, msg)
                else:
                    await broadcast(app, "⚠️ Не удалось получить корректный ответ от LLM.")
            
            await asyncio.sleep(900)

        except Exception as e:
            log.error("Scanner critical: %s", e, exc_info=True)
            await asyncio.sleep(300)

async def monitor(app: Application):
    while state.get("bot_on", False):
        if not state["monitored_signals"]:
            await asyncio.sleep(30); continue
        
        for s in list(state["monitored_signals"]):
            try:
                price = (await exchange_spot.fetch_ticker(s["pair"]))["last"]
                if not price: continue

                if s["side"] == "LONG":
                    if price > s["mfe_price"]: s["mfe_price"] = price
                    if price < s["mae_price"]: s["mae_price"] = price
                else: # SHORT
                    if price < s["mfe_price"]: s["mfe_price"] = price
                    if price > s["mae_price"]: s["mae_price"] = price

                hit, exit_price = None, None
                if (s["side"] == "LONG" and price >= s["tp"]): hit, exit_price = "TP_HIT", s["tp"]
                elif (s["side"] == "SHORT" and price <= s["tp"]): hit, exit_price = "TP_HIT", s["tp"]
                elif (s["side"] == "LONG" and price <= s["sl"]): hit, exit_price = "SL_HIT", s["sl"]
                elif (s["side"] == "SHORT" and price >= s["sl"]): hit, exit_price = "SL_HIT", s["sl"]

                if hit:
                    risk = abs(s["entry_price"] - s["sl"])
                    mfe_r = round(abs(s["mfe_price"] - s["entry_price"]) / risk, 2) if risk > 0 else 0
                    mae_r = round(abs(s["mae_price"] - s["entry_price"]) / risk, 2) if risk > 0 else 0
                    
                    leverage = s.get("leverage", 100)
                    position_size = s.get("position_size_usd", 0)
                    
                    if s['entry_price'] == 0: continue
                    price_change_percent = ((exit_price - s['entry_price']) / s['entry_price'])
                    if s["side"] == "SHORT": price_change_percent = -price_change_percent
                    
                    pnl_percent = price_change_percent * leverage * 100
                    pnl_usd = position_size * (pnl_percent / 100)

                    if TRADE_LOG_WS:
                        row = [
                            s["signal_id"], s["pair"], s["side"], hit, s["entry_time_utc"], datetime.now(timezone.utc).isoformat(),
                            s["entry_price"], exit_price, s["sl"], s["tp"], s["mfe_price"], s["mae_price"], mfe_r, mae_r,
                            s.get("rsi"), s.get("adx"), "N/A", s.get("bb_pos"), s.get("reason"),
                            s.get("confidence_score"), s.get("position_size_usd"), s.get("leverage"), pnl_usd, pnl_percent
                        ]
                        await asyncio.to_thread(TRADE_LOG_WS.append_row, row, value_input_option='USER_ENTERED')
                    
                    status_emoji = "✅" if hit == "TP_HIT" else "❌"
                    msg = (f"{status_emoji} <b>СДЕЛКА ЗАКРЫТА</b> ({hit})\n\n"
                           f"<b>Инструмент:</b> <code>{s['pair']}</code>\n"
                           f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent:+.2f}%)</b>")
                    await broadcast(app, msg)
                    
                    state["monitored_signals"].remove(s)
                    save_state()
            except Exception as e:
                log.error("Monitor %s: %s", s.get("signal_id"), e)
        await asyncio.sleep(60)

async def daily_pnl_report(app: Application):
    while True:
        await asyncio.sleep(3600) 
        now = datetime.now(timezone.utc)
        if now.hour != 0 or now.minute > 5: continue

        if not TRADE_LOG_WS: continue
        log.info("Running daily P&L report...")
        try:
            records = await asyncio.to_thread(TRADE_LOG_WS.get_all_records)
            yesterday = now - timedelta(days=1)
            total_pnl, wins, losses = 0, 0, 0

            for rec in records:
                try:
                    exit_time_str = rec.get("Exit_Time_UTC")
                    if not exit_time_str: continue
                    exit_time = datetime.fromisoformat(exit_time_str).replace(tzinfo=timezone.utc)
                    if exit_time.date() == yesterday.date():
                        pnl = float(rec.get("PNL_USD", 0))
                        total_pnl += pnl
                        if pnl > 0: wins += 1
                        elif pnl < 0: losses += 1
                except (ValueError, TypeError): continue
            
            if wins > 0 or losses > 0:
                msg = (f"📈 <b>Ежедневный отчет по P&L за {yesterday.strftime('%d-%m-%Y')}</b>\n\n"
                       f"<b>Результат:</b> ${total_pnl:+.2f}\n"
                       f"<b>Прибыльных сделок:</b> {wins}\n<b>Убыточных сделок:</b> {losses}")
                await broadcast(app, msg)
            else:
                log.info("No trades closed yesterday to report.")
        except Exception as e:
            log.error(f"Daily P&L report failed: {e}")

async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid not in ctx.application.chat_ids:
        ctx.application.chat_ids.add(cid)
    
    if not state.get("bot_on"):
        state["bot_on"] = True
        save_state()
        await update.message.reply_text("✅ <b>Бот v3.4.5 (Robust Sync) запущен.</b>")
        if not hasattr(ctx.application, '_scanner_task') or ctx.application._scanner_task.done():
             log.info("Starting scanner task from /start command...")
             ctx.application._scanner_task = asyncio.create_task(scanner(ctx.application))
        if not hasattr(ctx.application, '_monitor_task') or ctx.application._monitor_task.done():
             log.info("Starting monitor task from /start command...")
             ctx.application._monitor_task = asyncio.create_task(monitor(ctx.application))
    else:
        await update.message.reply_text("ℹ️ Бот уже запущен.")

async def cmd_stop(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    state["bot_on"] = False
    save_state()
    await update.message.reply_text("🛑 <b>Бот остановлен.</b>")

async def cmd_status(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    snapshot = await get_market_snapshot()
    msg = (f"<b>Состояние бота:</b> {'✅ ON' if state.get('bot_on') else '🛑 OFF'}\n"
           f"<b>Активных сигналов:</b> {len(state['monitored_signals'])}/{MAX_CONCURRENT_SIGNALS}\n"
           f"<b>Режим рынка:</b> {snapshot['regime']}\n"
           f"<b>Волатильность:</b> {snapshot['volatility']} (ATR {snapshot['volatility_percent']})")
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML)

async def post_init(app: Application):
    log.info("Bot application initialized. Checking prior state...")
    # Загрузка рынков один раз при старте приложения для начальной готовности
    try:
        await exchange_spot.load_markets()
        await exchange_futures.load_markets()
    except Exception as e:
        log.error(f"Initial market load failed: {e}")

    if state.get("bot_on"):
        log.info("Bot was ON before restart. Auto-starting tasks...")
        app._scanner_task = asyncio.create_task(scanner(app))
        app._monitor_task = asyncio.create_task(monitor(app))
    
    app._pnl_task = asyncio.create_task(daily_pnl_report(app))
    log.info("Background task scheduler is configured.")

if __name__ == "__main__":
    load_state()
    setup_sheets()
    
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.chat_ids = set(CHAT_IDS)
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    
    log.info("Bot v3.4.5 (Robust Sync) started polling.")
    app.run_polling()
