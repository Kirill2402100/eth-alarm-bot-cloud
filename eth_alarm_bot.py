#!/usr/bin/env python3
# ============================================================================
# eth_alarm_bot.py — v12.4  (25-Jun-2025)
#  • Исправлен сигнальный массив SSL  → Series  (ошибка replace/fillna)
#  • Почищен расчёт RR (None-safe)
#  • Qty = 100 % free USDT  (isolated)
#  • LLM ответ всегда на русском
# ============================================================================

import os, asyncio, json, logging, math, time
from datetime import datetime

import numpy as np
import pandas as pd
import ccxt.async_support as ccxt
import aiohttp
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, Defaults, ContextTypes

# ─────────────────────────── env • logging ────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
PAIR_RAW = os.getenv("PAIR", "BTC-USDT-SWAP")
INIT_LEV = int(os.getenv("LEVERAGE", 4))

LLM_API_KEY  = os.getenv("LLM_API_KEY")
LLM_API_URL  = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_CONF_TH  = float(os.getenv("LLM_CONFIDENCE_THRESHOLD", 7.0))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for noisy in ("httpx", "telegram.vendor.httpx", "aiohttp.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ───────────────────────── Google Sheets (опц.) ───────────────────────
SHEET_ID = os.getenv("SHEET_ID")
_GS_SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
if os.getenv("GOOGLE_CREDENTIALS"):
    creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(os.getenv("GOOGLE_CREDENTIALS")), _GS_SCOPE)
    _gs  = gspread.authorize(creds)
else:
    _gs = None; log.warning("GOOGLE_CREDENTIALS not set.")

def _ws(title):
    if not (_gs and SHEET_ID): return None
    ss = _gs.open_by_key(SHEET_ID)
    try: return ss.worksheet(title)
    except gspread.WorksheetNotFound: return ss.add_worksheet(title, rows=1000, cols=20)

HEADERS = ["DATE-UTC", "SIDE", "DEPOSIT", "ENTRY", "SL", "TP", "RR", "PNL", "APR%", "LLM DEC", "CONF"]
WS = _ws("AI-V12")
if WS and WS.row_values(1) != HEADERS:
    WS.clear(); WS.append_row(HEADERS)

# ────────────────────────────  OKX  ───────────────────────────────────
exchange = ccxt.okx({
    "apiKey": os.getenv("OKX_API_KEY"),
    "secret": os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "options": {"defaultType": "swap"},
    "enableRateLimit": True,
})
PAIR = PAIR_RAW.replace("/", "-").upper()
if "-SWAP" not in PAIR: PAIR += "-SWAP"

# ─── Стратегические константы ─────────────────────────────────────────
SSL_LEN, RSI_LEN = 13, 14
RSI_LONGT, RSI_SHORTT = 52, 48

# ─────────────────────────── indicators ───────────────────────────────
def _ta_rsi(series: pd.Series, length=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(length).mean()
    loss  = (-delta.clip(upper=0)).rolling(length).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi

def calc_atr(df: pd.DataFrame, length=14):
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low']  - df['close'].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(length).mean()

def calc_ind(df: pd.DataFrame):
    df['ema_fast'] = df['close'].ewm(span=20, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=50, adjust=False).mean()

    sma = df['close'].rolling(SSL_LEN).mean()
    hi  = df['high'].rolling(SSL_LEN).max()
    lo  = df['low'].rolling(SSL_LEN).min()
    df['ssl_up'] = np.where(df['close'] > sma, hi, lo)
    df['ssl_dn'] = np.where(df['close'] > sma, lo, hi)

    prev_up, prev_dn = df['ssl_up'].shift(1), df['ssl_dn'].shift(1)
    sig_arr = np.select(
        [(prev_up < prev_dn) & (df['ssl_up'] > df['ssl_dn']),
         (prev_up > prev_dn) & (df['ssl_up'] < df['ssl_dn'])],
        [1, -1],
        default=0
    )
    df['ssl_sig'] = (
        pd.Series(sig_arr, index=df.index)
          .replace(0, np.nan).ffill().fillna(0).astype(int)   # ← NEW
    )

    df['rsi'] = _ta_rsi(df['close'], RSI_LEN).ffill()
    df['atr'] = calc_atr(df)
    return df

# ─────────────────────────── state ────────────────────────────────────
state = {"monitor": False, "lev": INIT_LEV, "pos": None, "last_ts": 0}

# ───────────────────────── helpers ────────────────────────────────────
async def broadcast(ctx, text):
    for cid in ctx.application.chat_ids:
        try: await ctx.application.bot.send_message(cid, text)
        except: pass

async def get_free_usdt():
    try:
        bal = await exchange.fetch_balance()
        return bal['USDT']['free']
    except: return 0

# ─────────────────── LLM (русский ответ) ──────────────────────────────
LLM_PROMPT = (
 "Ты опытный крипто-трейдер. Проанализируй сет-ап и ответь СТРОГО JSON-объектом:\n"
 "1) decision  ('APPROVE'|'REJECT'); 2) confidence_score (0-10);\n"
 "3) reasoning (кратко, по-русски); 4) suggested_tp; 5) suggested_sl.\n"
 "Сет-ап:\n{trade}"
)

async def ask_llm(trade_data, ctx):
    if not LLM_API_KEY: return None
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": LLM_PROMPT.format(trade=json.dumps(trade_data, ensure_ascii=False, indent=2))}],
        "temperature": 0.2
    }
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, json=payload, headers=headers, timeout=60) as r:
                if r.status == 200:
                    cont = (await r.json())['choices'][0]['message']['content']
                    ans  = json.loads(cont)
                    await broadcast(ctx,
                        f"🧠 <b>LLM:</b>\n<b>Decision:</b> {ans['decision']}\n<b>Conf:</b> {ans['confidence_score']}/10\n"
                        f"<b>Note :</b> {ans['reasoning']}")
                    return ans
                else:
                    await broadcast(ctx, f"⚠️ LLM API error {r.status}")
    except Exception as e:
        log.error("LLM err: %s", e)
    return None

# ───────────────────── open / close pos  ───────────────────────────────
async def open_pos(side, price, llm, td, ctx):
    atr = td['volatility_atr']
    usdt = await get_free_usdt()
    if usdt < 1: await broadcast(ctx,"❗Баланс 0"); return

    m = exchange.market(PAIR); step = m['precision']['amount'] or 0.0001
    qty = math.floor((usdt * state['lev'] / price) / step) * step          # 100 % free USDT
    if qty < (m['limits']['amount']['min'] or step):
        await broadcast(ctx,"❗Слишком мало USDT"); return

    await exchange.set_leverage(state['lev'], PAIR)
    order = await exchange.create_market_order(PAIR, 'buy' if side=="LONG" else 'sell', qty, params={"tdMode":"isolated"})
    entry = order.get('average', price)

    sl = llm.get('suggested_sl') or (entry - atr*1.5 if side=="LONG" else entry + atr*1.5)
    tp = llm.get('suggested_tp') or (entry + atr*3   if side=="LONG" else entry - atr*3)

    state['pos'] = dict(side=side, qty=qty, entry=entry, sl=sl, tp=tp,
                        dep=usdt, atr=atr, opened=time.time(), llm=llm)

    await broadcast(ctx, f"✅ Открыта {side} qty={qty:.4f} entry={entry:.2f}\nSL={sl:.2f} | TP={tp:.2f}")

async def close_pos(reason, price, ctx):
    p = state.pop('pos', None)
    if not p: return
    await exchange.create_market_order(PAIR, 'sell' if p['side']=="LONG" else 'buy',
                                       p['qty'], params={"tdMode":"isolated","reduceOnly":True})
    pnl  = (price - p['entry']) * p['qty'] * (1 if p['side']=="LONG" else -1)
    days = max((time.time()-p['opened'])/86400,1e-6)
    apr  = pnl/p['dep']*365/days*100

    await broadcast(ctx,f"⛔ Закрыта ({reason}) PnL={pnl:.2f} APR={apr:.1f}%")

    if WS:
        rr = round(abs((p['tp']-p['entry']) / (p['entry']-p['sl'])),2) if p['entry']!=p['sl'] else 0
        WS.append_row([datetime.utcnow().isoformat(" ", "seconds"), p['side'], p['dep'],
                       p['entry'], p['sl'], p['tp'], rr, pnl, round(apr,2),
                       p['llm'].get('decision'), p['llm'].get('confidence_score')])

# ─────────────────────── telegram cmds ────────────────────────────────
async def cmd_start(u:Update, ctx):
    ctx.application.chat_ids.add(u.effective_chat.id)
    state['monitor']=True
    await u.message.reply_text("✅ Monitoring ON v12.4")
    if not ctx.chat_data.get("task"):
        ctx.chat_data["task"]=asyncio.create_task(monitor(ctx))

async def cmd_stop(u,ctx): state['monitor']=False; await u.message.reply_text("⛔ OFF")
async def cmd_lev(u,ctx):
    try:
        lev=int(u.message.text.split()[1]); assert 1<=lev<=100
        state['lev']=lev; await u.message.reply_text(f"Lev → {lev}×")
    except: await u.message.reply_text("/leverage 5")

# ─────────────────────── main monitor loop ────────────────────────────
async def monitor(ctx):
    log.info("monitor loop start")
    while True:
        await asyncio.sleep(30)
        if not state['monitor']: continue
        try:
            ohlcv = await exchange.fetch_ohlcv(PAIR,'15m',limit=100)
            df = pd.DataFrame(ohlcv, columns=['ts','open','high','low','close','volume'])
            if df.ts.iat[-1]==state['last_ts']: continue
            state['last_ts']=df.ts.iat[-1]
            df=calc_ind(df)
            ind=df.iloc[-1]

            # ─── exit logic ───
            p=state.get('pos')
            if p:
                price=ind.close
                if (price>=p['tp'] if p['side']=="LONG" else price<=p['tp']):
                    await close_pos("TP",price,ctx); continue
                if (price<=p['sl'] if p['side']=="LONG" else price>=p['sl']):
                    await close_pos("SL",price,ctx); continue
                continue

            # ─── entry logic ───
            sig=int(ind.ssl_sig)
            if sig==0: continue
            longCond  = sig==1  and ind.close>ind.ema_fast>ind.ema_slow and ind.rsi>RSI_LONGT
            shortCond = sig==-1 and ind.close<ind.ema_fast<ind.ema_slow and ind.rsi<RSI_SHORTT
            if not (longCond or shortCond): continue

            side="LONG" if longCond else "SHORT"
            trade_data={
                "asset":PAIR,"tf":"15m","signal":side,
                "price":round(ind.close,2),
                "ind":{"rsi":round(ind.rsi,2),"ema_fast":round(ind.ema_fast,2),"ema_slow":round(ind.ema_slow,2)},
                "volatility_atr":round(ind.atr,4)
            }
            await broadcast(ctx,f"🔍 Сигнал {side}. Анализ LLM…")
            llm=await ask_llm(trade_data,ctx) or {}
            if llm.get("decision")=="APPROVE" and llm.get("confidence_score",0)>=LLM_CONF_TH:
                await open_pos(side, ind.close, llm, trade_data, ctx)
        except Exception as e:
            log.exception("Loop err: %s",e)
            state['pos']=None

# ──────────────────── graceful shutdown ───────────────────────────────
async def shutdown(app): await exchange.close()

# ─────────────────── entry-point ───────────────────────────────────────
async def main():
    app = (ApplicationBuilder().token(BOT_TOKEN)
           .defaults(Defaults(parse_mode="HTML"))
           .post_shutdown(shutdown).build())
    app.chat_ids=set(CHAT_IDS)
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("leverage",cmd_lev))
    async with app:
        await exchange.load_markets()
        bal=await exchange.fetch_balance()
        log.info("USDT free=%s total=%s", bal['USDT']['free'], bal['USDT']['total'])
        await app.start(); await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__=="__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutdown…")
