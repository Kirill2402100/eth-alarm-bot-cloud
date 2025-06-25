#!/usr/bin/env python3
# ============================================================================
# eth_alarm_bot.py — v12.2.1 "Hot-Hot-fix" (25-Jun-2025)
# ▸ Защита от None/NaN в open_pos
# ▸ LLM отвечает по-русски
# ============================================================================

import os, asyncio, json, logging, math, time
from datetime import datetime
import numpy as np, pandas as pd
import ccxt.async_support as ccxt
import gspread, aiohttp
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (ApplicationBuilder, CommandHandler,
                          Defaults, ContextTypes)

# ──────────── env / logging ────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN")
CHAT_IDS       = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
PAIR_RAW       = os.getenv("PAIR", "BTC-USDT-SWAP")
SHEET_ID       = os.getenv("SHEET_ID")
INIT_LEV       = int(os.getenv("LEVERAGE", 4))
LLM_API_KEY    = os.getenv("LLM_API_KEY")
LLM_API_URL    = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_CONF_THR   = float(os.getenv("LLM_CONFIDENCE_THRESHOLD", 7.0))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for noisy in ("httpx", "telegram.vendor.httpx", "aiohttp.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ─────────── Google Sheets (опц.) ───────────
_GS_SCOPE = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
if os.getenv("GOOGLE_CREDENTIALS"):
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.getenv("GOOGLE_CREDENTIALS")), _GS_SCOPE)
    _gs = gspread.authorize(creds)
else:
    _gs = None; log.warning("GOOGLE_CREDENTIALS not set.")

def _ws(title):
    if not (_gs and SHEET_ID): return None
    ss = _gs.open_by_key(SHEET_ID)
    try: return ss.worksheet(title)
    except gspread.WorksheetNotFound: return ss.add_worksheet(title, 1000, 20)

HEADERS = ["DATE-TIME","POS","DEP","ENTRY","SL","TP","RR","P&L","APR%","LLM","CONF"]
WS = _ws("AI-V12")
if WS and WS.row_values(1) != HEADERS:
    WS.clear(); WS.append_row(HEADERS)

# ─────────── OKX ───────────
exchange = ccxt.okx({
    "apiKey":    os.getenv("OKX_API_KEY"),
    "secret":    os.getenv("OKX_SECRET"),
    "password":  os.getenv("OKX_PASSWORD"),
    "options":   {"defaultType": "swap"},
    "enableRateLimit": True,
})
PAIR = PAIR_RAW.replace("/", "-").replace(":USDT", "").upper()
if "-SWAP" not in PAIR: PAIR += "-SWAP"

# ─────────── базовые параметры стратегии ───────────
SSL_LEN, RSI_LEN = 13, 14
RSI_LONGT, RSI_SHORTT = 52, 48

# ─────────── тех-индикаторы ───────────
def _ta_rsi(series: pd.Series, length=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(length).mean()
    loss = (-delta.clip(upper=0)).rolling(length).mean()
    rs   = np.where(loss==0, np.inf, gain/loss)
    return 100 - 100/(1+rs)

def calc_atr(df, length=14):
    tr = pd.concat([
        df.high-df.low,
        (df.high-df.close.shift()).abs(),
        (df.low -df.close.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(length).mean()

def calc_ind(df):
    df['ema_fast'] = df.close.ewm(span=20, adjust=False).mean()
    df['ema_slow'] = df.close.ewm(span=50, adjust=False).mean()
    sma = df.close.rolling(SSL_LEN).mean()
    hi, lo = df.high.rolling(SSL_LEN).max(), df.low.rolling(SSL_LEN).min()
    df['ssl_up'] = np.where(df.close>sma, hi, lo)
    df['ssl_dn'] = np.where(df.close>sma, lo, hi)
    x_up  = (df.ssl_up.shift(1)<df.ssl_dn.shift(1)) & (df.ssl_up>df.ssl_dn)
    x_dn  = (df.ssl_up.shift(1)>df.ssl_dn.shift(1)) & (df.ssl_up<df.ssl_dn)
    sig   = pd.Series(np.nan,index=df.index)
    sig.loc[x_up], sig.loc[x_dn] = 1,-1
    df['ssl_sig'] = sig.ffill().fillna(0).astype(int)
    df['rsi'] = _ta_rsi(df.close, RSI_LEN)
    df['atr'] = calc_atr(df,14)
    return df

# ─────────── состояние ───────────
state = {"monitor":False, "leverage":INIT_LEV, "position":None, "last_ts":0}

# ─────────── Telegram utils ───────────
async def broadcast(ctx, txt):
    for cid in ctx.application.chat_ids:
        try: await ctx.application.bot.send_message(cid, txt)
        except Exception as e: log.warning("TG send fail %s: %s", cid, e)

async def get_free_usdt():
    try:
        bal = await exchange.fetch_balance()
        return bal['USDT'].get('free') or 0
    except Exception as e:
        log.error("balance err: %s", e); return 0

# ─────────── LLM анализ ───────────
LLM_PROMPT = """Ты — трейдер-аналитик «Сигма». Пиши ОДНИМ JSON по-русски:
{{
 "confidence_score": 0-10,       // float
 "decision": "APPROVE|REJECT",   // строка
 "reasoning": "кратко",
 "suggested_tp": число,          // цена или null
 "suggested_sl": число           // цена или null
}}
Сетап:
{trade_data}
"""

async def get_llm(trade_data, ctx):
    if not LLM_API_KEY: return None
    payload = {
        "model":"gpt-4o-mini",
        "messages":[{"role":"user","content":LLM_PROMPT.format(trade_data=json.dumps(trade_data,indent=2,ensure_ascii=False))}]
    }
    hdr = {"Authorization":f"Bearer {LLM_API_KEY}","Content-Type":"application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL,headers=hdr,json=payload,timeout=45) as r:
                if r.status!=200:
                    log.error("LLM %s %s", r.status, await r.text()); return None
                data = await r.json()
                resp = json.loads(data['choices'][0]['message']['content'])
                await broadcast(ctx, f"🧠 LLM:\n<b>Decision:</b> {resp['decision']}\n<b>Conf :</b> {resp['confidence_score']}/10\n<b>Note :</b> {resp['reasoning']}")
                return resp
    except Exception as e:
        log.error("LLM call err: %s", e); return None

# ─────────── trade helpers ───────────
async def open_pos(side, price, llm, trade_data, ctx):
    atr = float(trade_data.get("volatility_atr") or 0)
    atr = 0 if math.isnan(atr) else atr

    usdt = await get_free_usdt()
    if usdt<1: await broadcast(ctx,"❗Нет средств"); state['position']=None; return
    m = exchange.market(PAIR); step = m['precision']['amount'] or 0.0001
    qty = math.floor(usdt*state['leverage']/price/step)*step
    if qty < (m['limits']['amount']['min'] or step):
        await broadcast(ctx,"❗Мало баланса"); state['position']=None; return

    await exchange.set_leverage(state['leverage'], PAIR)
    try:
        order = await exchange.create_market_order(
            PAIR,'buy' if side=="LONG" else 'sell',qty,params={"tdMode":"isolated"})
    except Exception as e:
        log.error("open_pos order err: %s", e); state['position']=None; return

    entry = order.get('average', price)
    sl_llm = llm.get("suggested_sl") if llm else None
    tp_llm = llm.get("suggested_tp") if llm else None
    sl = sl_llm if isinstance(sl_llm,(int,float)) else (entry-atr*1.5 if side=="LONG" else entry+atr*1.5)
    tp = tp_llm if isinstance(tp_llm,(int,float)) else (entry+atr*3   if side=="LONG" else entry-atr*3)

    state['position'] = dict(side=side,amount=qty,entry=entry,sl=sl,tp=tp,
                             deposit=usdt,opened=time.time(),llm=llm)
    await broadcast(ctx,f"✅ Открыта {side} qty={qty:.4f} entry={entry:.2f}\nSL {sl:.2f} | TP {tp:.2f}")

async def close_pos(reason, price, ctx):
    p = state.pop("position",None)
    if not p: return
    params={"tdMode":"isolated","reduceOnly":True}
    await exchange.create_market_order(PAIR,'sell' if p['side']=="LONG" else 'buy',p['amount'],params=params)
    pnl = (price-p['entry'])*p['amount']*(1 if p['side']=="LONG" else -1)
    apr = pnl/p['deposit']*365/(max((time.time()-p['opened'])/86400,1e-6))*100
    await broadcast(ctx,f"⛔ Закрыта ({reason}) P&L {pnl:.2f} APR {apr:.1f}%")

    if WS:
        rr = abs((p['tp']-p['entry'])/(p['entry']-p['sl'])) if p['entry']!=p['sl'] else 0
        WS.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),p['side'],p['deposit'],
                       p['entry'],p['sl'],p['tp'],round(rr,2),pnl,round(apr,2),
                       p['llm'].get("decision") if p.get('llm') else "–",
                       p['llm'].get("confidence_score") if p.get('llm') else "–"])

# ─────────── telegram handlers ───────────
async def cmd_start(u:Update,ctx):
    ctx.application.chat_ids.add(u.effective_chat.id); state['monitor']=True
    await u.message.reply_text("✅ Monitoring ON (v12.2.1)")
    if not ctx.chat_data.get("task"): ctx.chat_data["task"]=asyncio.create_task(monitor(ctx))
async def cmd_stop(u:Update,ctx): state['monitor']=False; await u.message.reply_text("⛔ Monitoring OFF")
async def cmd_lev(u:Update,ctx):
    try:
        lev=int(u.message.text.split()[1]); assert 1<=lev<=100
        state['leverage']=lev; await u.message.reply_text(f"Leverage → {lev}×")
    except: await u.message.reply_text("usage: /leverage 5")

# ─────────── основной цикл ───────────
async def monitor(ctx):
    log.info("monitor loop start")
    while True:
        await asyncio.sleep(30)
        if not state["monitor"]: continue
        try:
            df = pd.DataFrame(await exchange.fetch_ohlcv(PAIR,'15m',limit=50),
                              columns=['ts','open','high','low','close','volume'])
            if df.ts.iloc[-1]==state.get("last_ts"): continue
            state["last_ts"]=df.ts.iloc[-1]
            df=calc_ind(df); ind=df.iloc[-1]

            # контроль открытой позиции
            p=state.get("position")
            if p:
                price=ind.close
                if (price>=p['tp'] if p['side']=="LONG" else price<=p['tp']):
                    await close_pos("TP",price,ctx)
                elif(price<=p['sl'] if p['side']=="LONG" else price>=p['sl']):
                    await close_pos("SL",price,ctx)
                continue

            # сигнал
            sig=int(ind.ssl_sig)
            longCond  = sig==1 and ind.close>ind.ema_fast>ind.ema_slow and ind.rsi>RSI_LONGT
            shortCond = sig==-1 and ind.close<ind.ema_fast<ind.ema_slow and ind.rsi<RSI_SHORTT
            side = "LONG" if longCond else "SHORT" if shortCond else None
            if not side: continue

            await broadcast(ctx,f"🔍 Базовый сигнал {side}. Анализ LLM…")
            td = {"asset":PAIR,"price":ind.close,"signal":side,
                  "ind":{"rsi":round(ind.rsi,2),"ema_fast":round(ind.ema_fast,2),"ema_slow":round(ind.ema_slow,2)},
                  "volatility_atr": round(ind.atr,4)}
            llm = await get_llm(td,ctx)
            if llm and llm.get("decision")=="APPROVE" and llm.get("confidence_score",0)>=LLM_CONF_THR:
                await open_pos(side, ind.close, llm, td, ctx)
            else:
                await broadcast(ctx,"🧊 LLM отклонил сигнал")

        except Exception as e:
            log.exception("loop err: %s", e); state['position']=None

# ─────────── entry-point ───────────
async def shutdown(app): await exchange.close()
async def main():
    app=(ApplicationBuilder().token(BOT_TOKEN)
         .defaults(Defaults(parse_mode="HTML"))
         .post_shutdown(shutdown).build())
    app.chat_ids=set(CHAT_IDS)
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("stop",cmd_stop))
    app.add_handler(CommandHandler("leverage",cmd_lev))
    async with app:
        await exchange.load_markets()
        await app.start(); await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__=="__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt,SystemExit):
        log.info("shutdown")
