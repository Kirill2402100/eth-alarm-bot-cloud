#!/usr/bin/env python3
# ============================================================================
# eth_alarm_bot.py — v12.4 "Full-Capital + Stable RR" (25-Jun-2025)
#   • берём total USDT × 0.998 для сделки
#   • фикс падений от NoneType при TP/SL
#   • сообщение об открытии всегда отправляется
# ============================================================================

import os, asyncio, json, logging, math, time
from datetime import datetime

import numpy as np
import pandas as pd
import ccxt.async_support as ccxt
import gspread, aiohttp
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, Defaults, ContextTypes

# ─────────── ENV / LOGGING ───────────
BOT_TOKEN        = os.getenv("BOT_TOKEN")
CHAT_IDS         = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
PAIR_RAW         = os.getenv("PAIR", "BTC-USDT-SWAP")
SHEET_ID         = os.getenv("SHEET_ID")
INIT_LEV         = int(os.getenv("LEVERAGE", 4))
LLM_API_KEY      = os.getenv("LLM_API_KEY")
LLM_API_URL      = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_CONF_LIMIT   = float(os.getenv("LLM_CONFIDENCE_THRESHOLD", 7.0))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for noisy in ("httpx", "telegram.vendor.httpx", "aiohttp.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ─────────── GOOGLE SHEETS (опц.) ───────────
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
    except gspread.WorksheetNotFound: return ss.add_worksheet(title, rows=1000, cols=20)

HEADERS = ["DATE-TIME","POSITION","DEPOSIT","ENTRY","STOP","TARGET","RR",
           "P&L","APR","LLM DEC","CONF"]
WS = _ws("AI-V12")
if WS and WS.row_values(1) != HEADERS:
    WS.clear(); WS.append_row(HEADERS)

# ─────────── OKX ───────────
exchange = ccxt.okx({
    "apiKey": os.getenv("OKX_API_KEY"),
    "secret": os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "options": {"defaultType": "swap"},
    "enableRateLimit": True,
})
PAIR = PAIR_RAW.replace("/", "-").replace(":USDT", "").upper()
if "-SWAP" not in PAIR: PAIR += "-SWAP"

# ─────────── СТРАТЕГИЯ: баз. параметры ───────────
SSL_LEN    = 13
RSI_LEN    = 14
RSI_LONGT  = 52
RSI_SHORTT = 48

# ─────────── INDICATORS ───────────
def _ta_rsi(series: pd.Series, length=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(length, min_periods=length).mean()
    loss  = (-delta.clip(upper=0)).rolling(length, min_periods=length).mean()
    rs = np.where(loss == 0, np.inf, gain / loss)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calc_atr(df: pd.DataFrame, length=14):
    high_low   = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close  = (df['low']  - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(length, min_periods=length).mean()

def calc_ind(df: pd.DataFrame):
    df['ema_fast'] = df['close'].ewm(span=20, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=50, adjust=False).mean()

    sma = df['close'].rolling(SSL_LEN).mean()
    hi  = df['high'].rolling(SSL_LEN).max()
    lo  = df['low'].rolling(SSL_LEN).min()
    df['ssl_up'] = np.where(df['close'] > sma, hi, lo)
    df['ssl_dn'] = np.where(df['close'] > sma, lo, hi)

    ssl_up_prev, ssl_dn_prev = df['ssl_up'].shift(1), df['ssl_dn'].shift(1)
    df['ssl_sig'] = np.select(
        [ (ssl_up_prev < ssl_dn_prev) & (df['ssl_up'] > df['ssl_dn']),
          (ssl_up_prev > ssl_dn_prev) & (df['ssl_up'] < df['ssl_dn']) ],
        [1,-1], default=0).astype(int).replace(0, np.nan).ffill().fillna(0)

    df['rsi'] = _ta_rsi(df['close'], RSI_LEN).fillna(method='ffill')
    df['atr'] = calc_atr(df, 14)
    return df

# ─────────── STATE ───────────
state = {"monitor": False, "leverage": INIT_LEV, "position": None, "last_ts":0}

# ─────────── HELPERS ───────────
async def broadcast(ctx, txt):
    for cid in ctx.application.chat_ids:
        try: await ctx.application.bot.send_message(cid, txt)
        except Exception as e: log.warning("Broadcast failed to %s: %s", cid, e)

async def get_trade_usdt() -> float:
    try:
        bal = await exchange.fetch_balance()
        total = bal.get("USDT", {}).get("total", 0)
        return total * 0.998            # оставим ~0.2 % на комиссии
    except Exception as e:
        log.error("Balance fetch error: %s", e)
        return 0

# ─────────── LLM ANALYSER ───────────
LLM_PROMPT = """
Ты — профессиональный трейдер-аналитик. Проанализируй приведённый сетап (JSON)
и верни *только* JSON формата:
{{
  "decision": "APPROVE | REJECT",
  "confidence_score": 0-10,
  "reasoning": "…",
  "suggested_tp": <float>,
  "suggested_sl": <float>
}}
Сетап:
{trade_data}
"""

async def get_llm_decision(trade_data, ctx):
    if not LLM_API_KEY: return None
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role":"user",
                      "content": LLM_PROMPT.format(
                          trade_data=json.dumps(trade_data, ensure_ascii=False, indent=2))}],
        "temperature": 0.2
    }
    headers = {"Authorization": f"Bearer {LLM_API_KEY}",
               "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, json=payload, headers=headers, timeout=45) as r:
                if r.status != 200:
                    log.error("LLM status %s: %s", r.status, await r.text())
                    await broadcast(ctx, f"❌ Ошибка LLM: status {r.status}")
                    return None
                resp = await r.json()
                content = resp['choices'][0]['message']['content']
                result = json.loads(content)
                await broadcast(ctx,
                    f"🧠 LLM:\n<b>Decision:</b> {result['decision']}\n"
                    f"<b>Conf:</b> {result['confidence_score']}/10\n"
                    f"<b>Note :</b> {result['reasoning']}")
                return result
    except Exception as e:
        log.exception("LLM call err: %s", e); return None

# ─────────── OPEN / CLOSE ───────────
async def open_pos(side, price, llm, td, ctx):
    atr = td['volatility_atr']
    usdt = await get_trade_usdt()
    if usdt <= 1:
        await broadcast(ctx,"❗ Недостаточно средств."); state['position']=None; return

    m = exchange.market(PAIR)
    step = m['precision']['amount'] or 0.0001
    qty  = math.floor((usdt * state['leverage'] / price) / step) * step
    if qty < (m['limits']['amount']['min'] or step):
        await broadcast(ctx,"❗ Слишком мало средств."); state['position']=None; return

    await exchange.set_leverage(state['leverage'], PAIR)
    order = await exchange.create_market_order(
        PAIR, 'buy' if side=="LONG" else 'sell', qty, params={"tdMode":"isolated"})
    entry = order.get('average', price)

    sl = llm.get("suggested_sl") or (entry - atr*1.5 if side=="LONG" else entry + atr*1.5)
    tp = llm.get("suggested_tp") or (entry + atr*3.0 if side=="LONG" else entry - atr*3.0)
    rr = abs((tp-entry)/(entry-sl)) if sl else 0

    state['position'] = dict(side=side, amount=qty, entry=entry,
                             sl=sl, tp=tp, deposit=usdt,
                             opened=time.time(), llm=llm)

    await broadcast(ctx,
        (f"✅ Открыта {side}\n"
         f"• Qty: {qty:.4f}\n"
         f"• Entry: {entry:.2f}\n"
         f"• SL: {sl:.2f} | TP: {tp:.2f}\n"
         f"• R/R: {rr:.2f}"))

async def close_pos(reason, price, ctx):
    p = state.pop('position', None);  # None if already cleared
    if not p: return
    side = 'sell' if p['side']=="LONG" else 'buy'
    order = await exchange.create_market_order(
        PAIR, side, p['amount'], params={"tdMode":"isolated","reduceOnly":True})
    close_price = order.get('average', price)
    pnl = (close_price - p['entry']) * p['amount'] * (1 if p['side']=="LONG" else -1)
    days = max((time.time()-p['opened'])/86400, 1e-6)
    apr  = (pnl/p['deposit'])*365/days*100
    await broadcast(ctx, f"⛔ Закрыта ({reason}) | P&L {pnl:.2f} | APR {apr:.1f}%")

    if WS:
        rr = abs((p['tp']-p['entry'])/(p['entry']-p['sl'])) if p['sl'] else 0
        WS.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                       p['side'], p['deposit'], p['entry'],
                       p['sl'], p['tp'], rr, pnl, round(apr,2),
                       p['llm']['decision'], p['llm']['confidence_score']])

# ─────────── TELEGRAM CMDS ───────────
async def cmd_start(u:Update, ctx:ContextTypes.DEFAULT_TYPE):
    ctx.application.chat_ids.add(u.effective_chat.id)
    state["monitor"]=True
    await u.message.reply_text("✅ Monitoring ON (v12.4)")
    if not ctx.chat_data.get("task"):
        ctx.chat_data["task"] = asyncio.create_task(monitor(ctx))

async def cmd_stop(u:Update, ctx):
    state["monitor"]=False
    await u.message.reply_text("⛔ Monitoring OFF")

async def cmd_lev(u:Update, ctx):
    try:
        lev=int(u.message.text.split()[1]); assert 1<=lev<=100
        state["leverage"]=lev
        await u.message.reply_text(f"Leverage → {lev}×")
    except: await u.message.reply_text("Использование: /leverage 5")

# ─────────── MONITOR LOOP ───────────
async def monitor(ctx):
    log.info("Monitor loop start.")
    while True:
        await asyncio.sleep(30)
        if not state["monitor"]: continue
        try:
            ohlcv = await exchange.fetch_ohlcv(PAIR,'15m',limit=50)
            df = pd.DataFrame(ohlcv,columns=['ts','open','high','low','close','volume'])
            ts = int(df.iloc[-1]['ts'])
            if ts == state['last_ts']: continue
            state['last_ts']=ts

            df = calc_ind(df)
            ind = df.iloc[-1]

            # ─── выход ───
            pos = state.get('position')
            if pos:
                price = ind['close']
                if (price>=pos['tp'] if pos['side']=="LONG" else price<=pos['tp']):
                    await close_pos("TP", price, ctx)
                elif (price<=pos['sl'] if pos['side']=="LONG" else price>=pos['sl']):
                    await close_pos("SL", price, ctx)
                continue

            # ─── вход ───
            sig = int(ind['ssl_sig'])
            if sig==0: continue
            longCond  = sig==1  and ind['rsi']>RSI_LONGT  and ind['close']>ind['ema_fast']>ind['ema_slow']
            shortCond = sig==-1 and ind['rsi']<RSI_SHORTT and ind['close']<ind['ema_fast']<ind['ema_slow']
            side = "LONG" if longCond else "SHORT" if shortCond else None
            if not side: continue

            td = {"asset":PAIR,"timeframe":"15m","signal":side,"current_price":ind['close'],
                  "volatility_atr":float(ind['atr'] or 0)}
            await broadcast(ctx,f"🔍 Сигнал {side}. Анализ LLM…")
            llm = await get_llm_decision(td, ctx) or {}
            if llm.get("decision")!="APPROVE" or llm.get("confidence_score",0)<LLM_CONF_LIMIT:
                await broadcast(ctx,"📬 LLM отклонил сигнал."); continue
            state['position']={"opening":True}
            await open_pos(side, ind['close'], llm, td, ctx)

        except Exception as e:
            log.exception("Loop err: %s", e)
            state['position']=None

# ─────────── ENTRY POINT ───────────
async def shutdown(app): await exchange.close()

async def main():
    app=(ApplicationBuilder().token(BOT_TOKEN)
         .defaults(Defaults(parse_mode="HTML"))
         .post_shutdown(shutdown).build())
    app.chat_ids=set(CHAT_IDS)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_lev))

    async with app:
        await exchange.load_markets()
        bal = await exchange.fetch_balance()
        log.info("USDT free=%s total=%s",
                 bal.get("USDT",{}).get("free"),
                 bal.get("USDT",{}).get("total"))
        await app.start(); await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__=="__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt,SystemExit):
        log.info("Shutdown.")
