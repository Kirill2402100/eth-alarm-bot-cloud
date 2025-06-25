#!/usr/bin/env python3
# ============================================================================
# eth_alarm_bot.py — v12.2  (25-Jun-2025)
# ----------------------------------------------------------------------------
#  • fixed: JSONDecodeError on empty LLM reply
#  • added: response_format="json_object", env LLM_MODEL
#  • added: verbose balance log
#  • hardened open_pos (atr always defined)
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

# ─────────────────────────── env / logging ────────────────────────────
BOT_TOKEN  = os.getenv("BOT_TOKEN")
CHAT_IDS   = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
PAIR_RAW   = os.getenv("PAIR", "BTC-USDT-SWAP")
SHEET_ID   = os.getenv("SHEET_ID")
INIT_LEV   = int(os.getenv("LEVERAGE", 4))

# LLM
LLM_API_KEY              = os.getenv("LLM_API_KEY")
LLM_API_URL              = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_MODEL                = os.getenv("LLM_MODEL",  "gpt-4o-mini")
LLM_CONFIDENCE_THRESHOLD = float(os.getenv("LLM_CONFIDENCE_THRESHOLD", 6.0))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for noisy in ("httpx", "telegram.vendor.httpx", "aiohttp.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ───────────────────────── Google Sheets (опц.) ───────────────────────
_GS_SCOPE = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
_creds_json = os.getenv("GOOGLE_CREDENTIALS")
_gs = None
if _creds_json:
    _creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(_creds_json), _GS_SCOPE)
    _gs = gspread.authorize(_creds)
else:
    log.warning("GOOGLE_CREDENTIALS not set – Sheets disabled")

def _ws(title: str):
    if not (_gs and SHEET_ID): return None
    ss = _gs.open_by_key(SHEET_ID)
    try:       return ss.worksheet(title)
    except gspread.WorksheetNotFound:
               return ss.add_worksheet(title, rows=1000, cols=20)

HEADERS = ["DATE-TIME","POSITION","DEPOSIT","ENTRY","STOP LOSS","TAKE PROFIT",
           "RR","P&L (USDT)","APR (%)","LLM DECISION","LLM CONFIDENCE"]
WS = _ws("AI-V12")
if WS and WS.row_values(1) != HEADERS:
    WS.clear(); WS.append_row(HEADERS)

# ─────────────────────────── OKX ───────────────────────────────────────
exchange = ccxt.okx({
    "apiKey"        : os.getenv("OKX_API_KEY"),
    "secret"        : os.getenv("OKX_SECRET"),
    "password"      : os.getenv("OKX_PASSWORD"),
    "options"       : {"defaultType": "swap"},
    "enableRateLimit": True,
})
PAIR = PAIR_RAW.replace("/", "-").replace(":USDT", "").upper()
if "-SWAP" not in PAIR: PAIR += "-SWAP"

# ─── базовые thresholds ───────────────────────────────────────────────
SSL_LEN   = 13
RSI_LEN   = 14
RSI_LONGT = 52
RSI_SHORTT= 48

# ───────────────────────── technical utils ────────────────────────────
def _ta_rsi(series: pd.Series, length=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(length, min_periods=length).mean()
    loss  = (-delta.clip(upper=0)).rolling(length, min_periods=length).mean()
    if loss.empty or loss.iloc[-1] == 0: return 100
    rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] else float("inf")
    return 100 - 100 / (1 + rs)

def calc_atr(df: pd.DataFrame, length=14):
    tr = pd.concat([df['high']-df['low'],
                    (df['high']-df['close'].shift()).abs(),
                    (df['low'] -df['close'].shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(length, min_periods=length).mean()

def calc_ind(df: pd.DataFrame):
    df['ema_fast'] = df['close'].ewm(span=20).mean()
    df['ema_slow'] = df['close'].ewm(span=50).mean()
    sma = df['close'].rolling(SSL_LEN).mean()
    hi  = df['high'].rolling(SSL_LEN).max()
    lo  = df['low'].rolling(SSL_LEN).min()
    df['ssl_up'] = np.where(df['close']>sma, hi, lo)
    df['ssl_dn'] = np.where(df['close']>sma, lo, hi)
    cross_up   = (df['ssl_up'].shift(1)<df['ssl_dn'].shift(1))&(df['ssl_up']>df['ssl_dn'])
    cross_down = (df['ssl_up'].shift(1)>df['ssl_dn'].shift(1))&(df['ssl_up']<df['ssl_dn'])
    signal = pd.Series(np.nan,index=df.index)
    signal.loc[cross_up]   = 1
    signal.loc[cross_down] = -1
    df['ssl_sig'] = signal.ffill().fillna(0).astype(int)
    df['rsi'] = _ta_rsi(df['close'], RSI_LEN)
    df['atr'] = calc_atr(df, 14)
    return df

# ───────────────────────── глобальное состояние ───────────────────────
state = {"monitor":False, "leverage":INIT_LEV, "position":None, "last_ts":0}

# ───────────────────────── helpers / telegram ─────────────────────────
async def broadcast(ctx, txt):
    for cid in ctx.application.chat_ids:
        try: await ctx.application.bot.send_message(cid, txt)
        except Exception as e: log.warning("broadcast to %s failed: %s", cid, e)

async def get_free_usdt():
    try:
        bal = await exchange.fetch_balance()
        return bal['USDT'].get('free', 0) or 0
    except Exception as e:
        log.error("Balance fetch error: %s", e); return 0

# ───────────────────────── LLM analyser (robust) ───────────────────────
async def get_llm_decision(trade_data: dict, ctx):
    if not LLM_API_KEY: return None                           # AI фильтр выключен
    headers = {"Authorization": f"Bearer {LLM_API_KEY}",
               "Content-Type" : "application/json"}
    prompt  = {
        "role":"user",
        "content":(
            "Верни СТРОГО JSON-объект вида "
            '{"decision":"APPROVE/REJECT","confidence_score":0-10,'
            '"reasoning":"…","suggested_tp":float,"suggested_sl":float}. '
            f"Сетап: {json.dumps(trade_data,ensure_ascii=False)}")
    }
    payload = {
        "model"          : LLM_MODEL,
        "messages"       : [prompt],
        "response_format": {"type":"json_object"},
        "temperature"    : 0.3,
        "max_tokens"     : 300,
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, headers=headers, json=payload, timeout=45) as r:
                if r.status!=200:
                    txt = await r.text()
                    raise RuntimeError(f"HTTP {r.status}: {txt[:200]}")
                data     = await r.json()
                content  = data["choices"][0]["message"]["content"].strip()
                log.debug("Raw LLM content: %s", content)
                llm_decision = json.loads(content)
    except Exception as e:
        log.error("LLM error → signal skipped: %s", e)
        await broadcast(ctx, f"❌ LLM-ошибка: {e}")
        return None

    await broadcast(
        ctx, (f"🧠 LLM:\n<b>Decision:</b> {llm_decision['decision']}\n"
              f"<b>Conf :</b> {llm_decision['confidence_score']}/10\n"
              f"<b>Note :</b> {llm_decision['reasoning']}")
    )
    return llm_decision

# ───────────────────────── trade helpers ───────────────────────────────
async def open_pos(side:str, price:float, llm_decision:dict,
                   trade_data:dict, ctx):
    atr = float(trade_data.get("volatility_atr", 0))
    usdt= await get_free_usdt()
    if usdt<=1: await broadcast(ctx,"❗ Нет средств"); state['position']=None; return
    m=exchange.market(PAIR)
    step = m['precision']['amount'] or 0.0001
    qty  = math.floor((usdt*state['leverage']/price)/step)*step
    if qty < (m['limits']['amount']['min'] or step):
        await broadcast(ctx,"❗ Мало USDT"); state['position']=None; return
    await exchange.set_leverage(state['leverage'], PAIR)
    params={"tdMode":"isolated"}
    try:
        order = await exchange.create_market_order(
            PAIR, 'buy' if side=="LONG" else 'sell', qty, params=params)
    except Exception as e:
        await broadcast(ctx,f"❌ Ошибка ордера: {e}"); state['position']=None; return
    entry = order.get("average", price)
    sl = llm_decision.get("suggested_sl",
          entry - atr*1.5 if side=="LONG" else entry + atr*1.5)
    tp = llm_decision.get("suggested_tp",
          entry + atr*3   if side=="LONG" else entry - atr*3)
    state['position'] = dict(side=side, amount=qty, entry=entry,
                             sl=sl, tp=tp, deposit=usdt, opened=time.time(),
                             llm_decision=llm_decision)
    await broadcast(ctx,
        f"✅ Открыта {side} qty={qty:.5f} entry={entry:.2f}\nSL={sl:.2f}  TP={tp:.2f}")

async def close_pos(reason:str, price:float, ctx):
    p = state.pop("position",None)
    if not p: return
    params={"tdMode":"isolated", "reduceOnly":True}
    try:
        order = await exchange.create_market_order(
            PAIR,'sell' if p['side']=="LONG" else 'buy', p['amount'], params=params)
        close_price = order.get("average",price)
    except Exception as e:
        log.error("close_pos error: %s",e); close_price = price
    pnl  = (close_price-p['entry'])*p['amount']*(1 if p['side']=="LONG" else -1)
    days = max((time.time()-p['opened'])/86400,1e-9)
    apr  = pnl/p['deposit']*(365/days)*100
    await broadcast(ctx,f"⛔ Закрыта ({reason})  P&L {pnl:.2f}  APR {apr:.1f}%")
    if WS:
        rr = abs((p['tp']-p['entry'])/(p['entry']-p['sl'])) if p['entry']!=p['sl'] else 0
        WS.append_row([
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            p['side'], p['deposit'], p['entry'], p['sl'], p['tp'], round(rr,2),
            pnl, round(apr,2),
            p['llm_decision'].get("decision","N/A"),
            p['llm_decision'].get("confidence_score","N/A"),
        ])

# ───────────────────────── Telegram commands ──────────────────────────
async def cmd_start(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    ctx.application.chat_ids.add(u.effective_chat.id); state["monitor"]=True
    await u.message.reply_text("✅ Monitoring ON (v12.2)")
    if not ctx.chat_data.get("task"):
        ctx.chat_data["task"] = asyncio.create_task(monitor(ctx))
async def cmd_stop(u:Update,ctx): state["monitor"]=False; await u.message.reply_text("⛔ OFF")
async def cmd_lev(u:Update,ctx):
    try:
        lev=int(u.message.text.split()[1]); assert 1<=lev<=100
        state["leverage"]=lev; await u.message.reply_text(f"Leverage → {lev}×")
    except: await u.message.reply_text("Usage: /leverage 5")

# ───────────────────────── MONITOR LOOP ───────────────────────────────
async def monitor(ctx):
    log.info("Monitor loop started")
    while True:
        await asyncio.sleep(30)
        if not state["monitor"]: continue
        try:
            df = pd.DataFrame(
                await exchange.fetch_ohlcv(PAIR,'15m',limit=50),
                columns=['ts','open','high','low','close','volume'])
            if df.empty: continue
            ts = df.iloc[-1]['ts']
            if ts == state.get("last_ts"): continue
            state["last_ts"] = ts
            df = calc_ind(df)
            ind = df.iloc[-1]

            # --- контроль активной позиции ---
            pos = state.get("position")
            if pos:
                price = ind['close']
                if (price>=pos['tp'] if pos['side']=="LONG" else price<=pos['tp']):
                    await close_pos("TP",price,ctx); continue
                if (price<=pos['sl'] if pos['side']=="LONG" else price>=pos['sl']):
                    await close_pos("SL",price,ctx); continue

            # --- поиск нового сигнала ---
            sig = int(ind['ssl_sig'])
            if sig==0 or state.get("position") is not None: continue
            longCond  = sig==1  and ind['close']>ind['ema_fast']>ind['ema_slow'] and ind['rsi']>RSI_LONGT
            shortCond = sig==-1 and ind['close']<ind['ema_fast']<ind['ema_slow'] and ind['rsi']<RSI_SHORTT
            side = "LONG" if longCond else "SHORT" if shortCond else None
            if not side: continue

            await broadcast(ctx,f"🔍 Сигнал {side}. Анализ LLM…")
            trade_data = {
                "asset":PAIR,"timeframe":"15m","signal_type":side,
                "current_price":ind['close'],
                "indicators":{"rsi":round(ind['rsi'],2),
                              "ema_fast":round(ind['ema_fast'],2),
                              "ema_slow":round(ind['ema_slow'],2),
                              "ssl_signal":"Up" if side=="LONG" else "Down"},
                "volatility_atr":round(ind['atr'],4),
            }
            state['position']={"opening":True}
            llm = await get_llm_decision(trade_data,ctx)
            if llm and llm.get("decision")=="APPROVE" and llm.get("confidence_score",0)>=LLM_CONFIDENCE_THRESHOLD:
                await open_pos(side,ind['close'],llm,trade_data,ctx)
            else:
                await broadcast(ctx,"🤖 LLM отклонил сигнал")
                state['position']=None
        except ccxt.NetworkError as e:
            log.warning("Network error: %s",e)
        except Exception as e:
            log.exception("Loop error: %s",e); state['position']=None

# ───────────────────────── entry-point ────────────────────────────────
async def shutdown_hook(app): await exchange.close()
async def main():
    app = (ApplicationBuilder().token(BOT_TOKEN)
           .defaults(Defaults(parse_mode="HTML"))
           .post_shutdown(shutdown_hook).build())
    app.chat_ids=set(CHAT_IDS)
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("stop" ,cmd_stop ))
    app.add_handler(CommandHandler("leverage",cmd_lev))
    async with app:
        try:
            await exchange.load_markets()
            bal = await exchange.fetch_balance()
            log.info("USDT free=%s total=%s", bal['USDT'].get('free'),
                     bal['USDT'].get('total'))
        except Exception as e:
            log.error("Init error: %s",e); return
        await app.start(); await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__=="__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt,SystemExit):
        log.info("Manual shutdown")
