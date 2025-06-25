#!/usr/bin/env python3
# ============================================================================
# eth_alarm_bot.py — v12.4  (25-Jun-2025)
# • FIX: _ta_rsi теперь возвращает Series → .fillna() больше не падает
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

# ─────────────── env / logging ───────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS  = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
PAIR_RAW  = os.getenv("PAIR", "BTC-USDT-SWAP")
SHEET_ID  = os.getenv("SHEET_ID")
INIT_LEV  = int(os.getenv("LEVERAGE", 4))
LLM_API_KEY   = os.getenv("LLM_API_KEY")
LLM_API_URL   = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_CONFIDENCE_THRESHOLD = float(os.getenv("LLM_CONFIDENCE_THRESHOLD", 7.0))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for noisy in ("httpx", "telegram.vendor.httpx", "aiohttp.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ─────────────── Google Sheets ───────────────
_GS_SCOPE = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
if os.getenv("GOOGLE_CREDENTIALS"):
    _creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.getenv("GOOGLE_CREDENTIALS")), _GS_SCOPE)
    _gs = gspread.authorize(_creds)
else:
    _gs = None; log.warning("GOOGLE_CREDENTIALS not set.")

def _ws(title:str):
    if not (_gs and SHEET_ID): return None
    ss = _gs.open_by_key(SHEET_ID)
    try: return ss.worksheet(title)
    except gspread.WorksheetNotFound: return ss.add_worksheet(title, rows=1000, cols=20)

HEADERS = ["DATE-TIME","SIDE","DEPOSIT","ENTRY","SL","TP","RR","PNL","APR","LLM","CONF"]
WS = _ws("AI-V12")
if WS and WS.row_values(1) != HEADERS:
    WS.clear(); WS.append_row(HEADERS)

# ─────────────── OKX ───────────────
exchange = ccxt.okx({
    "apiKey":    os.getenv("OKX_API_KEY"),
    "secret":    os.getenv("OKX_SECRET"),
    "password":  os.getenv("OKX_PASSWORD"),
    "options":   {"defaultType": "swap"},
    "enableRateLimit": True,
})
PAIR = PAIR_RAW.replace("/", "-").replace(":USDT", "").upper()
if "-SWAP" not in PAIR: PAIR += "-SWAP"

# ─────────────── strategy params ─────────────
SSL_LEN, RSI_LEN = 13, 14
RSI_LONGT, RSI_SHORTT = 52, 48

# ─────────────── indicators ─────────────
def _ta_rsi(series: pd.Series, length:int=14) -> pd.Series:
    """classic Wilder RSI, SAFE version returning pd.Series"""
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(length).mean()
    loss  = (-delta.clip(upper=0)).rolling(length).mean()
    rs    = np.where(loss==0, np.inf, gain / loss)
    rsi   = 100 - 100/(1+rs)                # ← NumPy ndarray
    return pd.Series(rsi, index=series.index).fillna(50)   # ← Series!

def calc_atr(df:pd.DataFrame,length:int=14)->pd.Series:
    hl  = df['high'] - df['low']
    hc  = (df['high'] - df['close'].shift()).abs()
    lc  = (df['low']  - df['close'].shift()).abs()
    tr  = pd.concat([hl,hc,lc],axis=1).max(axis=1)
    return tr.rolling(length).mean()

def calc_ind(df:pd.DataFrame)->pd.DataFrame:
    df['ema_fast'] = df['close'].ewm(span=20,adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=50,adjust=False).mean()

    sma = df['close'].rolling(SSL_LEN).mean()
    hi  = df['high'].rolling(SSL_LEN).max()
    lo  = df['low'].rolling(SSL_LEN).min()
    df['ssl_up'] = np.where(df['close']>sma, hi, lo)
    df['ssl_dn'] = np.where(df['close']>sma, lo, hi)
    cross_up   = (df['ssl_up'].shift(1)<df['ssl_dn'].shift(1))&(df['ssl_up']>df['ssl_dn'])
    cross_down = (df['ssl_up'].shift(1)>df['ssl_dn'].shift(1))&(df['ssl_up']<df['ssl_dn'])
    sig = pd.Series(0,index=df.index); sig[cross_up]=1; sig[cross_down]=-1
    df['ssl_sig'] = sig.replace(0,np.nan).ffill().fillna(0).astype(int)

    df['rsi'] = _ta_rsi(df['close'], RSI_LEN)
    df['atr'] = calc_atr(df,14)
    return df
# ─────────────────────────── состояние ────────────────────────────────
state = {"monitor": False, "lev": INIT_LEV, "pos": None, "last_ts": 0}

# ────────────────────────── helpers ────────────────────────────────────
async def broadcast(ctx, txt):
    for cid in ctx.application.chat_ids:
        try: await ctx.application.bot.send_message(cid, txt)
        except Exception as e: log.warning("Broadcast failed to %s: %s", cid, e)

async def get_free_usdt():
    try:
        bal = await exchange.fetch_balance()
        return bal.get('USDT', {}).get('free', 0) or 0
    except Exception as e:
        log.error("Balance error: %s", e); return 0

# ──────────────────────── LLM SECTION ─────────────────────────────────
LLM_PROMPT_TEMPLATE = """
Ты — профессиональный трейдер-аналитик «Сигма».
Проанализируй торговый сетап (ниже JSON) и верни СТРОГО JSON:
{{
 "confidence_score": float,            // 0-10
 "decision": "APPROVE"|"REJECT",
 "reasoning": "краткий вывод о сетапе",
 "suggested_tp": float|null,
 "suggested_sl": float|null
}}
Ни одного слова вне JSON.

SETUP:
{trade_data}
"""

async def ask_llm(td:dict, ctx):
    if not LLM_API_KEY: return None
    prompt = LLM_PROMPT_TEMPLATE.format(trade_data=json.dumps(td, ensure_ascii=False, indent=2))
    headers = {"Authorization": f"Bearer {LLM_API_KEY}",
               "Content-Type": "application/json"}
    payload = {"model":"gpt-4o-mini",
               "messages":[{"role":"user","content":prompt}],
               "temperature":0.4}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, headers=headers, json=payload, timeout=45) as r:
                data = await r.json()
        raw = data['choices'][0]['message']['content']
        dec = json.loads(raw)
        dec.setdefault("suggested_sl", None)
        dec.setdefault("suggested_tp", None)
        await broadcast(ctx,
            (f"🧠 <b>LLM</b>:\n<b>Decision:</b> {dec['decision']}\n"
             f"<b>Conf :</b> {dec['confidence_score']:.1f}/10\n"
             f"<b>Note :</b> {dec['reasoning'][:240]}"))
        return dec
    except Exception as e:
        log.error("LLM call err: %s", e)
        await broadcast(ctx, f"⚠️ LLM error: {e}")
        return None

# ─────────────────────────── trades ───────────────────────────────────
async def open_pos(side:str, price:float, llm:dict, td:dict, ctx):
    atr = td.get("volatility_atr") or price*0.005
    if np.isnan(atr): atr = price*0.005

    usdt = await get_free_usdt()
    if usdt < 1: await broadcast(ctx,"❗ Недостаточно USDT"); return

    m = exchange.market(PAIR)
    step = m['precision']['amount'] or 0.0001
    qty  = math.floor((usdt*state['lev']/price)/step)*step
    if qty < (m['limits']['amount']['min'] or step):
        await broadcast(ctx,"❗ Слишком маленький лот"); return

    await exchange.set_leverage(state['lev'], PAIR)
    order = await exchange.create_market_order(
        PAIR, 'buy' if side=="LONG" else 'sell', qty,
        params={"tdMode":"isolated"})
    entry = order.get('average', price)

    sl = llm.get("suggested_sl")
    tp = llm.get("suggested_tp")
    if sl is None:
        sl = entry - atr*1.5 if side=="LONG" else entry + atr*1.5
    if tp is None:
        tp = entry + atr*3.0 if side=="LONG" else entry - atr*3.0

    rr = 0
    if sl and tp and sl!=entry:
        rr = round(abs((tp-entry)/(entry-sl)),2)

    state['pos'] = dict(side=side, qty=qty, entry=entry,
                        sl=sl, tp=tp, rr=rr,
                        deposit=usdt, opened=time.time(),
                        llm=llm)
    await broadcast(ctx,
        (f"✅ Открыта {side}\nQty={qty:.4f}\nEntry={entry:.2f}\n"
         f"SL={sl:.2f} | TP={tp:.2f}\nRR={rr if rr else '—'}"))

async def close_pos(reason:str, price:float, ctx):
    p = state.pop('pos', None)
    if not p: return
    order = await exchange.create_market_order(
        PAIR,'sell' if p['side']=="LONG" else 'buy',p['qty'],
        params={"tdMode":"isolated","reduceOnly":True})
    exit_price = order.get('average', price)
    pnl = (exit_price - p['entry'])*p['qty']*(1 if p['side']=="LONG" else -1)
    apr = (pnl/p['deposit'])*(365/ max((time.time()-p['opened'])/86400,1e-9))*100
    await broadcast(ctx,f"⛔ Закрыта ({reason}) | P&L {pnl:.2f} USDT | APR {apr:.1f}%")

    if WS:
        WS.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                       p['side'], p['deposit'], p['entry'], p['sl'], p['tp'],
                       p['rr'], pnl, round(apr,2),
                       p['llm'].get('decision','N/A'),
                       p['llm'].get('confidence_score','N/A')])

# ─────────────────────────── telegram ────────────────────────────────
async def cmd_start(u:Update, ctx): 
    ctx.application.chat_ids.add(u.effective_chat.id)
    state['monitor']=True
    await u.message.reply_text("✅ Monitoring ON (v12.3)")
    if not ctx.chat_data.get("task"):
        ctx.chat_data["task"]=asyncio.create_task(monitor(ctx))

async def cmd_stop(u:Update, ctx):
    state['monitor']=False
    await u.message.reply_text("⛔ Monitoring OFF")

async def cmd_lev(u:Update, ctx):
    try:
        lev=int(u.message.text.split()[1]); assert 1<=lev<=100
        state['lev']=lev; await u.message.reply_text(f"Leverage → {lev}×")
    except: await u.message.reply_text("Использование: /leverage 5")

# ─────────────────────── main monitor loop ───────────────────────────
async def monitor(ctx):
    log.info("Monitor loop start")
    while True:
        await asyncio.sleep(30)
        if not state['monitor']: continue
        try:
            ohlcv = await exchange.fetch_ohlcv(PAIR,'15m',limit=60)
            df = pd.DataFrame(ohlcv, columns=['ts','open','high','low','close','volume'])
            ts = df.iloc[-1]['ts']
            if ts==state['last_ts']: continue
            state['last_ts']=ts

            df = calc_ind(df)
            ind = df.iloc[-1]

            # ── exit logic ──
            if state.get('pos'):
                price = ind['close']
                p = state['pos']
                if (p['side']=="LONG" and price>=p['tp']) or (p['side']=="SHORT" and price<=p['tp']):
                    await close_pos("TP", price, ctx)
                elif (p['side']=="LONG" and price<=p['sl']) or (p['side']=="SHORT" and price>=p['sl']):
                    await close_pos("SL", price, ctx)
                continue

            # ── entry logic ──
            sig = int(ind['ssl_sig'])
            longCond  = sig==1  and ind['close']>ind['ema_fast']>ind['ema_slow'] and ind['rsi']>RSI_LONGT
            shortCond = sig==-1 and ind['close']<ind['ema_fast']<ind['ema_slow'] and ind['rsi']<RSI_SHORTT
            side = "LONG" if longCond else "SHORT" if shortCond else None
            if not side: continue

            await broadcast(ctx,f"🔍 Сигнал {side}. Анализ LLM…")
            td = {"asset":PAIR,"tf":"15m","signal":side,"price":ind['close'],
                  "ind":{"rsi":round(ind['rsi'],1),"ema_fast":round(ind['ema_fast'],2),
                         "ema_slow":round(ind['ema_slow'],2),"ssl_sig":sig},
                  "volatility_atr":round(ind['atr'],5)}

            llm = await ask_llm(td, ctx)
            if not llm or llm.get('decision')!="APPROVE" or llm.get('confidence_score',0)<LLM_CONFIDENCE_THRESHOLD:
                await broadcast(ctx,"🧊 LLM отклонил сигнал."); continue
            await open_pos(side, ind['close'], llm, td, ctx)

        except ccxt.NetworkError as e: log.warning("Network err: %s",e)
        except Exception as e:
            log.exception("Loop err: %s", e)
            state['pos']=None   # сбрасываем блокировку

# ───────────────────── entry-point ────────────────────────────────────
async def shutdown(app): log.info("Shutdown"); await exchange.close()

async def main():
    app = (ApplicationBuilder().token(BOT_TOKEN)
           .defaults(Defaults(parse_mode="HTML"))
           .post_shutdown(shutdown).build())
    app.chat_ids=set(CHAT_IDS)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_lev))

    async with app:
        try:
            await exchange.load_markets()
            bal=await exchange.fetch_balance()
            log.info("USDT free=%s total=%s",
                     bal.get('USDT',{}).get('free'),
                     bal.get('USDT',{}).get('total'))
        except Exception as e:
            log.error("Init error: %s", e); return
        await app.start(); await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__=="__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt,SystemExit):
        log.info("Manual shutdown")
