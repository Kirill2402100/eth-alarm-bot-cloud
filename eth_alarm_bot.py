#!/usr/bin/env python3
# ============================================================================
# eth_alarm_bot.py — v12.7 "Resilient" (25-Jun-2025)
# • Добавлена обработка ошибок при создании ордера для предотвращения падений.
# ============================================================================

import os, asyncio, json, logging, math, time
from datetime import datetime

import aiohttp, numpy as np, pandas as pd
import ccxt.async_support as ccxt
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, Defaults, ContextTypes

# ────────── ENV ──────────
BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_IDS    = {int(c) for c in os.getenv("CHAT_IDS", "0").split(",") if c}
PAIR_RAW    = os.getenv("PAIR", "BTC-USDT-SWAP")
INIT_LEV    = int(os.getenv("LEVERAGE", 4))

LLM_API_KEY  = os.getenv("LLM_API_KEY")
LLM_API_URL  = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_MODEL_ID = os.getenv("LLM_MODEL_ID", "gpt-4.1")
LLM_THRESHOLD= float(os.getenv("LLM_CONFIDENCE_THRESHOLD", 7.0))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for n in ("httpx", "telegram.vendor.httpx", "aiohttp.access"): logging.getLogger(n).setLevel(logging.WARNING)

# ────────── Google Sheets (опц.) ──────────
SHEET_ID = os.getenv("SHEET_ID")
_gscope  = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
if os.getenv("GOOGLE_CREDENTIALS"):
    _creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(os.getenv("GOOGLE_CREDENTIALS")), _gscope)
    _gs    = gspread.authorize(_creds)
else:
    _gs=None; log.warning("GOOGLE_CREDENTIALS not set.")

def _ws(title:str):
    if not (_gs and SHEET_ID): return None
    ss=_gs.open_by_key(SHEET_ID)
    try: return ss.worksheet(title)
    except gspread.WorksheetNotFound: return ss.add_worksheet(title, rows=1000, cols=20)

HEADERS = ["DATE-UTC","SIDE","DEPOSIT","ENTRY","SL","TP","RR","P&L","APR%","LLM","CONF"]
WS = _ws("AI-V12")
if WS and WS.row_values(1)!=HEADERS: WS.clear(); WS.append_row(HEADERS)

# ────────── биржа OKX (swap, isolated) ──────────
exchange = ccxt.okx({
    "apiKey": os.getenv("OKX_API_KEY"), "secret": os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"), "options": {"defaultType":"swap"},
    "enableRateLimit": True,
})
PAIR = PAIR_RAW.replace("/", "-").replace(":USDT","").upper()
if "-SWAP" not in PAIR: PAIR += "-SWAP"

# ────────── индикаторы ──────────
SSL_LEN, RSI_LEN = 13, 14
RSI_LONGT, RSI_SHORTT = 52, 48

def _ta_rsi(series:pd.Series, length=14):
    delta=series.diff(); gain=delta.clip(lower=0).rolling(length).mean()
    loss=(-delta.clip(upper=0)).rolling(length).mean().replace(0,np.nan)
    rs=gain/ loss ; rsi=100-100/(1+rs); return rsi.fillna(50)

def calc_ind(df:pd.DataFrame):
    df['ema_fast']=df['close'].ewm(span=20, adjust=False).mean()
    df['ema_slow']=df['close'].ewm(span=50, adjust=False).mean()
    sma=df['close'].rolling(SSL_LEN).mean()
    hi=df['high'].rolling(SSL_LEN).max(); lo=df['low'].rolling(SSL_LEN).min()
    df['ssl_up']=np.where(df['close']>sma, hi, lo)
    df['ssl_dn']=np.where(df['close']>sma, lo, hi)
    cross_up  =(df['ssl_up'].shift(1)<df['ssl_dn'].shift(1))&(df['ssl_up']>df['ssl_dn'])
    cross_dn  =(df['ssl_up'].shift(1)>df['ssl_dn'].shift(1))&(df['ssl_up']<df['ssl_dn'])
    sig=pd.Series(np.nan,index=df.index)
    sig.loc[cross_up]=1 ; sig.loc[cross_dn]=-1
    df['ssl_sig']=sig.ffill().fillna(0).astype(int)
    df['rsi']=_ta_rsi(df['close'], RSI_LEN)
    
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = true_range.rolling(14).mean()
    
    return df

# ────────── глобальное состояние ──────────
state={"monitor":False,"leverage":INIT_LEV,"position":None,"last_ts":0}

# ────────── helpers ──────────
async def broadcast(ctx, txt): 
    for cid in ctx.application.chat_ids:
        try: await ctx.application.bot.send_message(cid, txt, parse_mode="HTML")
        except Exception as e: log.warning("tg send %s: %s", cid, e)

async def free_usdt():
    try: bal=await exchange.fetch_balance(); return bal['USDT']['free']
    except: return 0

# ────────── LLM ──────────
LLM_PROMPT = (
"Ты — трейдер-аналитик. Дай ответ ТОЛЬКО JSON c полями "
"decision (APPROVE / REJECT), confidence_score (0–10), reasoning (RU), "
"suggested_tp, suggested_sl. Trade:\n{trade}")

async def ask_llm(trade_data, ctx):
    if not LLM_API_KEY: return None
    payload={"model":LLM_MODEL_ID,"messages":[{"role":"user","content":LLM_PROMPT.format(trade=json.dumps(trade_data,ensure_ascii=False))}],"temperature":0.2}
    headers={"Authorization":f"Bearer {LLM_API_KEY}","Content-Type":"application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL,json=payload,headers=headers,timeout=60) as r:
                txt=await r.text()
                if r.status!=200:
                    await broadcast(ctx,f"⚠️ LLM HTTP{r.status}: {txt[:120]}")
                    return None
                try:
                    cont=json.loads(txt); msg=cont["choices"][0]["message"]["content"]
                    clean_msg = msg
                    if "```json" in msg:
                        clean_msg = msg.split("```json")[1].split("```")[0]
                    elif "```" in msg:
                        clean_msg = msg.split("```")[1].split("```")[0]
                    ans=json.loads(clean_msg.strip())
                except Exception as e:
                    log.error("LLM raw: %s", txt[:300])
                    await broadcast(ctx,"⚠️ LLM ответ непонятен, сигнал пропущен.")
                    return None
                await broadcast(ctx, (f"🧠 <b>LLM:</b> {ans.get('decision', 'N/A')} "
                                      f"(<i>{ans.get('confidence_score', 'N/A')}/10</i>)\n"
                                      f"<i>{ans.get('reasoning', '')}</i>"))
                return ans
    except Exception as e:
        log.error("LLM req err: %s", e); await broadcast(ctx,f"❌ LLM error: {e}")
    return None

# ────────── торговые действия ──────────
async def open_pos(side, price, llm, td, ctx):
    atr=td.get('atr', 0); usdt=await free_usdt()
    m=exchange.market(PAIR); step=m['precision']['amount'] or 0.0001
    # Рассчитываем желаемое количество, используя 99% баланса для безопасности
    qty=math.floor(((usdt * 0.99) * state['leverage']/price)/step)*step

    if qty < (m['limits']['amount']['min'] or step):
        await broadcast(ctx,f"❗ Недостаточно средств для мин. лота ({m['limits']['amount']['min']}). Нужно {price * m['limits']['amount']['min'] / state['leverage']:.2f} USDT."); state['position']=None; return
    
    await exchange.set_leverage(state['leverage'], PAIR)
    
    # ---> ИСПРАВЛЕНИЕ: Оборачиваем создание ордера в try...except <---
    try:
        order=await exchange.create_market_order(PAIR,'buy' if side=="LONG" else 'sell',qty,params={"tdMode":"isolated"})
        if not isinstance(order, dict) or 'average' not in order:
            log.error("Exchange returned invalid order object: %s", order)
            await broadcast(ctx, "⚠️ Биржа вернула некорректный ответ на ордер. Позиция могла не открыться.")
            state['position']=None; return
    except Exception as e:
        log.error("Failed to create order: %s", e)
        await broadcast(ctx, f"❌ Биржа отклонила ордер: {e}")
        state['position']=None; return
    # ---> КОНЕЦ ИСПРАВЛЕНИЯ <---

    entry=order.get('average',price)
    sl=llm.get('suggested_sl', entry-atr*1.5 if side=="LONG" else entry+atr*1.5)
    tp=llm.get('suggested_tp', entry+atr*3.0 if side=="LONG" else entry-atr*3.0)
    state['position']=dict(side=side,amount=qty,entry=entry,sl=sl,tp=tp,opened=time.time(),llm=llm,dep=usdt)
    await broadcast(ctx, (f"✅ Открыта {side} qty={qty:.4f}\n🔹Entry={entry:.2f}\n"
                          f"🔻SL={sl:.2f}  🔺TP={tp:.2f}"))

async def close_pos(reason, price, ctx):
    p=state.pop('position',None); 
    if not p: return
    try:
        order=await exchange.create_market_order(PAIR,'sell' if p['side']=="LONG" else 'buy',p['amount'],params={"tdMode":"isolated","reduceOnly":True})
        close_price=order.get('average',price)
    except Exception as e:
        log.error("Failed to close position: %s", e)
        await broadcast(ctx, f"❌ Ошибка закрытия позиции: {e}. Пожалуйста, проверьте биржу.")
        close_price = price # Используем последнюю известную цену для расчета P&L
        
    pnl=(close_price-p['entry'])*p['amount']*(1 if p['side']=="LONG" else -1)
    days=max((time.time()-p['opened'])/86400,1e-9); apr=pnl/p['dep']*(365/days)*100
    await broadcast(ctx,f"⛔ Закрыта ({reason}) P&L={pnl:.2f}$ APR={apr:.1f}%")
    if WS:
        rr=round(abs((p['tp']-p['entry'])/(p['entry']-p['sl'])),2) if p['entry']!=p['sl'] else 0
        WS.append_row([datetime.utcnow().strftime("%F %T"),p['side'],p['dep'],p['entry'],p['sl'],p['tp'],rr,pnl,round(apr,2),p['llm']['decision'],p['llm']['confidence_score']])

# ────────── Telegram cmd ──────────
async def cmd_start(u,ctx):
    ctx.application.chat_ids.add(u.effective_chat.id); state["monitor"]=True
    await u.message.reply_text("✅ Monitoring ON (v12.7 Resilient)")
    if not ctx.chat_data.get("task"): ctx.chat_data["task"]=asyncio.create_task(monitor(ctx))
async def cmd_stop(u,ctx): state["monitor"]=False; await u.message.reply_text("⛔ Monitoring OFF")
async def cmd_lev(u,ctx):
    try: lev=int(u.message.text.split()[1]); assert 1<=lev<=100
    except: return await u.message.reply_text("usage /leverage 5")
    state['leverage']=lev; await u.message.reply_text(f"Leverage → {lev}x")

# ────────── MONITOR LOOP ──────────
async def monitor(ctx):
    while True:
        await asyncio.sleep(30)
        if not state['monitor']: continue
        try:
            ohl=await exchange.fetch_ohlcv(PAIR,'15m',limit=50)
            df=pd.DataFrame(ohl,columns=['ts','open','high','low','close','volume'])
            if df.iloc[-1]['ts']==state.get('last_ts', 0): continue
            state['last_ts']=int(df.iloc[-1]['ts'])
            df=calc_ind(df); ind=df.iloc[-1]

            # выход
            p=state.get('position')
            if p and p.get('side'):
                price=ind['close']
                if (p['side']=="LONG" and price>=p['tp']) or (p['side']=="SHORT" and price<=p['tp']):
                    await close_pos("TP",price,ctx)
                elif (p['side']=="LONG" and price<=p['sl']) or (p['side']=="SHORT" and price>=p['sl']):
                    await close_pos("SL",price,ctx)
                continue

            # вход
            if not state.get('position'):
                sig=int(ind['ssl_sig'])
                longCond = sig==1  and ind['close']>ind['ema_fast']>ind['ema_slow'] and ind['rsi']>RSI_LONGT
                shortCond= sig==-1 and ind['close']<ind['ema_fast']<ind['ema_slow'] and ind['rsi']<RSI_SHORTT
                side="LONG" if longCond else "SHORT" if shortCond else None
                if not side: continue
                await broadcast(ctx,f"🔍 Базовый сигнал {side}. Анализ LLM…")
                td={"asset":PAIR,"tf":"15m","signal":side,"price":ind['close'],
                    "atr":ind['atr'],"rsi":round(ind['rsi'],1),"ema_fast":round(ind['ema_fast'],2),"ema_slow":round(ind['ema_slow'],2)}
                state['position']={"opening":True}
                llm=await ask_llm(td,ctx)
                if llm and llm.get("decision")=="APPROVE" and llm.get("confidence_score",0)>=LLM_THRESHOLD:
                    await open_pos(side, ind['close'], llm, td, ctx)
                else:
                    await broadcast(ctx,"🟦 LLM отклонил сигнал."); state['position']=None
        except Exception as e:
            log.exception("loop err: %s", e); state['position']=None

# ────────── main ──────────
async def shutdown(app): await exchange.close()
async def main():
    app=(ApplicationBuilder().token(BOT_TOKEN).defaults(Defaults(parse_mode="HTML")).post_shutdown(shutdown).build())
    app.chat_ids=set(CHAT_IDS)
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("leverage",cmd_lev))
    async with app:
        await exchange.load_markets()
        bal=await exchange.fetch_balance(); log.info("USDT free=%s total=%s", bal['USDT']['free'], bal['USDT']['total'])
        await app.start(); await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__=="__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt,SystemExit): log.info("bye")
