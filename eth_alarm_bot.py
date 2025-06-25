#!/usr/bin/env python3
# ============================================================================
# eth_alarm_bot.py — v12.2  (25-Jun-2025)
#  • hot-fix: safe_avg() для order['average'] == None
#  • atr==0 -> пропуск входа
#  • доп-логи, debug-режим
# ============================================================================

import os, asyncio, json, logging, math, time
from datetime import datetime
import numpy as np, pandas as pd, ccxt.async_support as ccxt, gspread, aiohttp
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, Defaults, ContextTypes

# ─────────────── ENV / LOGGING ───────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_IDS    = {int(cid) for cid in os.getenv("CHAT_IDS" , "0").split(",") if cid}
PAIR_RAW    = os.getenv("PAIR", "BTC-USDT-SWAP")
SHEET_ID    = os.getenv("SHEET_ID")
INIT_LEV    = int(os.getenv("LEVERAGE", 4))
DEBUG       = bool(int(os.getenv("BOT_DEBUG", "0")))

LLM_API_KEY   = os.getenv("LLM_API_KEY")
LLM_API_URL   = os.getenv("LLM_API_URL" , "https://api.openai.com/v1/chat/completions")
LLM_CONF_MIN  = float(os.getenv("LLM_CONFIDENCE_THRESHOLD", 6.0))

logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for n in ("httpx", "telegram.vendor.httpx", "aiohttp.access"):
    logging.getLogger(n).setLevel(logging.WARNING)

# ─────────────── GOOGLE SHEETS (опц.) ───────────────
_GS_SCOPE = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
if os.getenv("GOOGLE_CREDENTIALS"):
    _creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.getenv("GOOGLE_CREDENTIALS")), _GS_SCOPE)
    _gs = gspread.authorize(_creds)
else:
    _gs = None; log.warning("GOOGLE_CREDENTIALS not set.")

def _ws(title: str):
    if not (_gs and SHEET_ID): return None
    ss = _gs.open_by_key(SHEET_ID)
    try: return ss.worksheet(title)
    except gspread.WorksheetNotFound: return ss.add_worksheet(title, rows=1_000, cols=20)

HEADERS = ["DATE-TIME","POSITION","DEPOSIT","ENTRY","STOP LOSS","TAKE PROFIT","RR","P&L","APR (%)",
           "LLM DEC","LLM CONF"]
WS = _ws("AI-V12.2")
if WS and WS.row_values(1) != HEADERS:
    WS.clear(); WS.append_row(HEADERS)

# ─────────────── EXCHANGE ───────────────
exchange = ccxt.okx({
    "apiKey":    os.getenv("OKX_API_KEY"),
    "secret":    os.getenv("OKX_SECRET"),
    "password":  os.getenv("OKX_PASSWORD"),
    "options":   {"defaultType": "swap"},
    "enableRateLimit": True,
})
PAIR = PAIR_RAW.replace("/", "-").replace(":USDT", "").upper()
if "-SWAP" not in PAIR: PAIR += "-SWAP"

# ─────────────── СТРАТЕГИЯ (коротко) ───────────────
SSL_LEN, RSI_LEN = 13, 14
RSI_LONGT, RSI_SHORTT = 52, 48

# ─────────────── TECH-UTILS ───────────────
def safe_avg(order: dict, fallback: float) -> float:
    "Защищённое получение средней цены"
    return order.get("average") or fallback

def _ta_rsi(series: pd.Series, length=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(length).mean()
    loss = (-delta.clip(upper=0)).rolling(length).mean()
    if not loss.any() or loss.iloc[-1] == 0: return 100
    rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] else float("inf")
    return 100 - (100 / (1 + rs))

def calc_atr(df, l=14):
    rng = pd.concat([df.high-df.low,
                     (df.high-df.close.shift()).abs(),
                     (df.low -df.close.shift()).abs()], axis=1)
    return rng.max(axis=1).rolling(l).mean()

def calc_ind(df: pd.DataFrame):
    sma = df.close.rolling(SSL_LEN).mean()
    hi, lo = df.high.rolling(SSL_LEN).max(), df.low.rolling(SSL_LEN).min()
    df["ssl_up"] = np.where(df.close > sma, hi, lo)
    df["ssl_dn"] = np.where(df.close > sma, lo, hi)

    cross_up   = (df.ssl_up.shift(1) < df.ssl_dn.shift(1)) & (df.ssl_up > df.ssl_dn)
    cross_down = (df.ssl_up.shift(1) > df.ssl_dn.shift(1)) & (df.ssl_up < df.ssl_dn)
    sig = pd.Series(0, index=df.index)
    sig[cross_up]   = 1
    sig[cross_down] = -1
    df["ssl_sig"] = sig.replace(0, np.nan).ffill().fillna(0).astype(int)

    df["ema_fast"] = df.close.ewm(span=20).mean()
    df["ema_slow"] = df.close.ewm(span=50).mean()
    df["rsi"]      = _ta_rsi(df.close, RSI_LEN)
    df["atr"]      = calc_atr(df, 14)
    return df

# ─────────────── GLOBAL STATE ───────────────
state = {"monitor": False, "leverage": INIT_LEV,
         "position": None, "last_ts": 0}

async def broadcast(ctx, txt):
    for cid in ctx.application.chat_ids:
        try: await ctx.application.bot.send_message(cid, txt)
        except Exception as e: log.warning("Broadcast to %s failed: %s", cid, e)

async def get_free_usdt():
    try:
        bal = await exchange.fetch_balance()
        return bal.get("USDT", {}).get("free", 0) or 0
    except Exception as e:
        log.error("fetch_balance error: %s", e); return 0

# ─────────────── LLM (оставляем как было) ───────────────
LLM_PROMPT = """Ты — проф-трейдер ... (сокр.)"""  # тот же шаблон

async def ask_llm(trade_json, ctx):
    if not LLM_API_KEY: return None
    prompt = LLM_PROMPT.format(trade_data=json.dumps(trade_json, indent=2))
    headers = {"Authorization": f"Bearer {LLM_API_KEY}",
               "Content-Type": "application/json"}
    body = {"model": "gpt-4.1",
            "messages": [{"role":"user", "content": prompt}]}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, headers=headers, json=body, timeout=45) as r:
                if r.status != 200:
                    if DEBUG: log.error("LLM %s: %s", r.status, await r.text())
                    return None
                content = (await r.json())["choices"][0]["message"]["content"]
                return json.loads(content)
    except Exception as e:
        log.error("LLM error: %s", e); return None

# ─────────────── OPEN / CLOSE POSITIONS ───────────────
async def open_pos(side, price, atr, llm_dec, ctx):
    usdt = await get_free_usdt()
    if usdt < 1: await broadcast(ctx, "❗ Нет средств."); return

    m = exchange.market(PAIR); step = m['precision']['amount'] or 0.0001
    qty = math.floor((usdt * state['leverage'] / price) / step) * step
    if qty < (m['limits']['amount']['min'] or step):
        await broadcast(ctx, "❗ Слишком маленькая позиция."); return

    await exchange.set_leverage(state['leverage'], PAIR)
    try:
        order = await exchange.create_market_order(
            PAIR, 'buy' if side=="LONG" else 'sell', qty,
            params={"tdMode": "isolated"})
    except Exception as e:
        await broadcast(ctx, f"❌ Ошибка открытия: {e}"); return

    entry = safe_avg(order, price)
    sl = llm_dec.get('suggested_sl',
                     entry - atr*1.5 if side=="LONG" else entry + atr*1.5)
    tp = llm_dec.get('suggested_tp',
                     entry + atr*3.0 if side=="LONG" else entry - atr*3.0)

    state['position'] = dict(side=side, amount=qty, entry=entry,
                             sl=sl, tp=tp, deposit=usdt,
                             opened=time.time(), llm_dec=llm_dec)
    await broadcast(ctx, f"✅ {side} открыт @ {entry:.2f}\nSL {sl:.2f} | TP {tp:.2f}")

async def close_pos(reason, price, ctx):
    p = state.pop("position", None)
    if not p: return
    try:
        order = await exchange.create_market_order(
            PAIR, 'sell' if p['side']=="LONG" else 'buy', p['amount'],
            params={"tdMode": "isolated", "reduceOnly": True})
        close_price = safe_avg(order, price)
    except Exception as e:
        log.error("close error: %s", e); close_price = price

    pnl = (close_price - p['entry']) * p['amount'] * (1 if p['side']=="LONG" else -1)
    apr = (pnl/p['deposit']) * 365 / max((time.time()-p['opened'])/86400, 1e-9) * 100
    await broadcast(ctx, f"⛔ Закрыта ({reason}) P&L {pnl:.2f} APR {apr:.1f}%")

    if WS:
        rr = abs((p['tp']-p['entry']) / (p['entry']-p['sl'])) if p['entry']!=p['sl'] else 0
        WS.append_row([datetime.utcnow().strftime("%F %T"), p['side'], p['deposit'],
                       p['entry'], p['sl'], p['tp'], round(rr,2),
                       pnl, round(apr,2),
                       p['llm_dec'].get('decision','N/A'),
                       p['llm_dec'].get('confidence_score','N/A')])

# ─────────────── TELEGRAM CMDS ───────────────
async def cmd_start(u, ctx):
    ctx.application.chat_ids.add(u.effective_chat.id)
    state["monitor"] = True
    await u.message.reply_text("✅ Monitoring ON v12.2")
    if not ctx.chat_data.get("task"):
        ctx.chat_data["task"] = asyncio.create_task(monitor(ctx))

async def cmd_stop(u, ctx): state["monitor"]=False; await u.message.reply_text("⛔ OFF")
async def cmd_lev (u, ctx):
    try: lev=int(u.message.text.split()[1]); assert 1<=lev<=100
    except: return await u.message.reply_text("Исп: /leverage 5")
    state["leverage"]=lev; await u.message.reply_text(f"Leverage {lev}×")

# ─────────────── MAIN MONITOR LOOP ───────────────
async def monitor(ctx):
    log.info("Monitor loop started.")
    while True:
        await asyncio.sleep(30)
        if not state["monitor"]: continue
        try:
            df = pd.DataFrame(await exchange.fetch_ohlcv(PAIR,'15m',limit=50),
                              columns=['ts','open','high','low','close','volume'])
            if df.ts.iloc[-1] == state["last_ts"]: continue
            state["last_ts"] = df.ts.iloc[-1]

            df = calc_ind(df); ind = df.iloc[-1]
            atr = ind['atr']
            if atr == 0 or np.isnan(atr):  # защита
                if DEBUG: log.debug("ATR=0, candle skipped.")
                continue

            # -------- EXIT --------
            pos = state.get("position")
            if pos:
                price = ind.close
                if (pos['side']=="LONG"  and price>=pos['tp']) or \
                   (pos['side']=="SHORT" and price<=pos['tp']):
                    await close_pos("TP", price, ctx)
                elif (pos['side']=="LONG"  and price<=pos['sl']) or \
                     (pos['side']=="SHORT" and price>=pos['sl']):
                    await close_pos("SL", price, ctx)
                continue

            # -------- ENTRY --------
            sig = int(ind.ssl_sig)
            if sig == 0: continue

            long_ok  = sig==1  and ind.close>ind.ema_fast>ind.ema_slow and ind.rsi>RSI_LONGT
            short_ok = sig==-1 and ind.close<ind.ema_fast<ind.ema_slow and ind.rsi<RSI_SHORTT
            side = "LONG" if long_ok else "SHORT" if short_ok else None
            if not side: continue

            trade_json = {"asset": PAIR, "timeframe":"15m", "signal_type":side,
                          "current_price": ind.close,
                          "indicators":{"rsi": round(ind.rsi,2),
                                        "ema_fast": round(ind.ema_fast,2),
                                        "ema_slow": round(ind.ema_slow,2),
                                        "ssl": "Up" if side=="LONG" else "Down"},
                          "volatility_atr": round(atr,4)}

            await broadcast(ctx,f"🔍 Базовый сигнал {side}. Анализ LLM…")
            llm_dec = await ask_llm(trade_json, ctx) or {}
            if llm_dec.get("decision")!="APPROVE" or llm_dec.get("confidence_score",0)<LLM_CONF_MIN:
                await broadcast(ctx,"🤖 LLM отклонил сигнал."); continue

            await open_pos(side, ind.close, atr, llm_dec, ctx)

        except ccxt.NetworkError as e:
            log.warning("Network err: %s", e)
        except Exception as e:
            log.exception("Monitor loop err: %s", e)
            state["position"] = None  # разблокировка

# ─────────────── APP BOOTSTRAP ───────────────
async def shutdown(app): await exchange.close()

async def main():
    app = (ApplicationBuilder().token(BOT_TOKEN)
           .defaults(Defaults(parse_mode="HTML"))
           .post_shutdown(shutdown).build())
    app.chat_ids = set(CHAT_IDS)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop",  cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_lev))
    async with app:
        await exchange.load_markets()
        log.info("Markets loaded.")
        await app.start(); await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutdown requested.")
