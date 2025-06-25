#!/usr/bin/env python3
# ============================================================================
# eth_alarm_bot.py — v12.3 "LLM-Русский + Hotfix"  (26-Jun-2025)
#
# – Исправлен KeyError при форматировании LLM-промпта
# – Ответы LLM приходят только на русском и строго в JSON
# – Надёжный разбор ответа (без падений, если JSON кривой)
# – Защита от None в сообщении «Открыта позиция»
# ============================================================================

import os, asyncio, json, logging, math, time
from datetime import datetime

import numpy as np, pandas as pd
import ccxt.async_support as ccxt
import gspread, aiohttp
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, Defaults, ContextTypes

# ─────────────────── конфиг из переменных окружения ──────────────────
BOT_TOKEN  = os.getenv("BOT_TOKEN")
PAIR_RAW   = os.getenv("PAIR", "BTC-USDT-SWAP")
SHEET_ID   = os.getenv("SHEET_ID")
INIT_LEV   = int(os.getenv("LEVERAGE", 4))
CHAT_IDS   = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}

LLM_API_KEY   = os.getenv("LLM_API_KEY")
LLM_API_URL   = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_CONF_THR  = float(os.getenv("LLM_CONFIDENCE_THRESHOLD", 7.0))  # min score

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)8s  %(message)s")
log = logging.getLogger("bot")
for _m in ("httpx","telegram.vendor.httpx","aiohttp.access"):
    logging.getLogger(_m).setLevel(logging.WARNING)

# ─────────────────────── Google-Sheets (если заданы) ──────────────────
_GS_SCOPE = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
if os.getenv("GOOGLE_CREDENTIALS"):
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.getenv("GOOGLE_CREDENTIALS")), _GS_SCOPE)
    _gs = gspread.authorize(creds)
else:
    _gs = None; log.warning("GOOGLE_CREDENTIALS not set → Sheets OFF.")

def _ws(title:str):
    if not (_gs and SHEET_ID): return None
    ss = _gs.open_by_key(SHEET_ID)
    try:  return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        return ss.add_worksheet(title, rows=1000, cols=20)

HEADERS = ["DATE-TIME","SIDE","DEP","ENTRY","SL","TP","RR","PNL","APR%",
           "LLM DEC","LLM CONF"]
WS = _ws("AI-V12")
if WS and WS.row_values(1) != HEADERS:
    WS.clear(); WS.append_row(HEADERS)

# ──────────────────────────── OKX ─────────────────────────────────────
exchange = ccxt.okx({
    "apiKey":    os.getenv("OKX_API_KEY"),
    "secret":    os.getenv("OKX_SECRET"),
    "password":  os.getenv("OKX_PASSWORD"),
    "options":   {"defaultType":"swap"},
    "enableRateLimit": True,
})
PAIR = PAIR_RAW.replace("/", "-").replace(":USDT","").upper()
if "-SWAP" not in PAIR: PAIR += "-SWAP"

# ───────────── параметры базовой фильтра-стратегии (15-мин) ───────────
SSL_LEN, RSI_LEN          = 13, 14
RSI_LONGT, RSI_SHORTT     = 52, 48

# ───────────── индикаторы (без ta-lib) ─────────────
def _rsi(series: pd.Series, length=14):
    delta = series.diff(); gain = delta.clip(lower=0).rolling(length).mean()
    loss  = (-delta.clip(upper=0)).rolling(length).mean()
    if loss.empty or loss.iloc[-1]==0: return 100
    rs = gain.iloc[-1] / loss.iloc[-1]
    return 100 - 100/(1+rs)

def _atr(df: pd.DataFrame, length=14):
    hl    = df.high - df.low
    h_c   = (df.high - df.close.shift()).abs()
    l_c   = (df.low  - df.close.shift()).abs()
    tr    = pd.concat([hl,h_c,l_c],axis=1).max(axis=1)
    return tr.rolling(length).mean()

def calc_ind(df: pd.DataFrame):
    df["ema_fast"] = df.close.ewm(span=20).mean()
    df["ema_slow"] = df.close.ewm(span=50).mean()

    sma = df.close.rolling(SSL_LEN).mean()
    hi  = df.high.rolling(SSL_LEN).max()
    lo  = df.low .rolling(SSL_LEN).min()
    df["ssl_up"] = np.where(df.close > sma, hi, lo)
    df["ssl_dn"] = np.where(df.close > sma, lo, hi)

    cross_up   = (df.ssl_up.shift(1)<df.ssl_dn.shift(1))&(df.ssl_up>df.ssl_dn)
    cross_down = (df.ssl_up.shift(1)>df.ssl_dn.shift(1))&(df.ssl_up<df.ssl_dn)
    sig = pd.Series(np.nan,index=df.index)
    sig.loc[cross_up]   =  1
    sig.loc[cross_down] = -1
    df["ssl_sig"] = sig.ffill().fillna(0).astype(int)

    df["rsi"] = _rsi(df.close, RSI_LEN)
    df["atr"] = _atr(df, 14)
    return df

# ────────────────────────────── STATE ────────────────────────────────
state = {"monitor":False, "leverage":INIT_LEV,
         "position":None, "last_ts":0}

# ──────────────────────── Telegram broadcast ─────────────────────────
async def broadcast(ctx, txt):
    for cid in ctx.application.chat_ids:
        try: await ctx.application.bot.send_message(cid, txt)
        except Exception as e: log.warning("Broadcast to %s failed: %s",cid,e)

async def usdt_free():
    try:
        bal = await exchange.fetch_balance()
        return bal["USDT"].get("free") or 0
    except Exception as e:
        log.error("fetch_balance: %s",e); return 0

# ────────────────────────── LLM PROMPT ───────────────────────────────
LLM_PROMPT_TEMPLATE = """
Ты — профессиональный трейдер-аналитик «Сигма».
Проанализируй сетап (JSON ниже) и ответь **строго JSON-объектом**:
{{
  "confidence_score": float,          // 0-10
  "decision": "APPROVE"|"REJECT",
  "reasoning": "кратко, по-русски",
  "suggested_tp": float,              // цена
  "suggested_sl": float               // цена
}}
Ни одного слова вне JSON.

Сетап ↓
{trade_data}
"""

async def ask_llm(trade_data: dict, ctx):
    if not LLM_API_KEY:
        await broadcast(ctx,"⚠️ LLM_API_KEY не задан — пропуск анализа.")
        return None

    prompt = LLM_PROMPT_TEMPLATE.format(
        trade_data=json.dumps(trade_data, ensure_ascii=False, indent=2))

    headers = {"Authorization": f"Bearer {LLM_API_KEY}",
               "Content-Type": "application/json"}
    payload = {
        "model": "gpt-4o-mini",
        "messages":[{"role":"user","content":prompt}],
        "response_format":{"type":"json_object"},
        "temperature":0.3,
    }

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, headers=headers,
                               json=payload, timeout=45) as r:
                if r.status!=200:
                    txt = await r.text()
                    log.error("LLM HTTP %s: %s",r.status,txt)
                    await broadcast(ctx,f"❌ Ошибка LLM: HTTP {r.status}")
                    return None
                raw = (await r.json())["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error("LLM request error: %s",e)
        await broadcast(ctx,"❌ Сбой запроса к LLM.")
        return None

    try:
        decision = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("LLM bad JSON: %s\n%s",e,raw)
        await broadcast(ctx,"⚠️ LLM прислал некорректный JSON, сигнал отвергнут.")
        return None

    # Сообщаем результат
    await broadcast(ctx,
        f"🧠 LLM:\n<b>Decision:</b> {decision.get('decision')}\n"
        f"<b>Conf:</b> {decision.get('confidence_score')}/10\n"
        f"<i>Note :</i> {decision.get('reasoning','—')}")
    return decision

# ───────────────────── OPEN / CLOSE POSITION ─────────────────────────
async def open_pos(side:str, price:float,
                   llm:dict, td:dict, ctx):

    atr = td["volatility_atr"]
    usdt = await usdt_free()
    if usdt<=1:
        await broadcast(ctx,"❗ Недостаточно средств."); state["position"]=None; return

    m   = exchange.market(PAIR)
    step= m["precision"]["amount"] or 1e-4
    qty = math.floor(usdt*state["leverage"]/price/step)*step
    if qty < (m["limits"]["amount"]["min"] or step):
        await broadcast(ctx,"❗ Слишком мало USDT для позиции."); state["position"]=None; return

    await exchange.set_leverage(state["leverage"],PAIR)
    params={"tdMode":"isolated"}

    try:
        order = await exchange.create_market_order(
            PAIR,'buy' if side=="LONG" else 'sell',qty,params=params)
    except Exception as e:
        await broadcast(ctx,f"❌ Ошибка открытия: {e}"); state["position"]=None; return

    entry = order.get("average", price)
    sl = llm.get("suggested_sl") or (entry-atr*1.5 if side=="LONG" else entry+atr*1.5)
    tp = llm.get("suggested_tp") or (entry+atr*3   if side=="LONG" else entry-atr*3)
    rr = round(abs((tp-entry)/(entry-sl)),2) if entry!=sl else 0

    state["position"] = dict(side=side,amount=qty,entry=entry,sl=sl,tp=tp,
                             deposit=usdt,opened=time.time(),
                             llm_decision=llm)

    await broadcast(ctx,
        f"✅ Открыта {side} qty={qty:.4f}\n"
        f"entry={entry:.2f} | SL={sl:.2f} | TP={tp:.2f} | RR={rr}")

async def close_pos(reason:str, price:float, ctx):
    pos = state.pop("position",None);  # атомарно
    if not pos: return
    params={"tdMode":"isolated","reduceOnly":True}
    try:
        order = await exchange.create_market_order(
            PAIR,'sell' if pos["side"]=="LONG" else 'buy',pos["amount"],params=params)
        close = order.get("average",price)
    except Exception as e:
        log.error("close order err: %s",e); close = price

    pnl = (close-pos["entry"])*pos["amount"]*(1 if pos["side"]=="LONG" else -1)
    days= max((time.time()-pos["opened"])/86400,1e-9)
    apr = pnl/pos["deposit"]*(365/days)*100

    await broadcast(ctx,f"⛔ Закрыта ({reason}) P&L={pnl:.2f} USDT | APR={apr:.1f}%")
    if WS:
        try:
            rr = round(abs((pos["tp"]-pos["entry"])/(pos["entry"]-pos["sl"])),2)
            WS.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                           pos["side"],pos["deposit"],pos["entry"],
                           pos["sl"],pos["tp"],rr,pnl,round(apr,2),
                           pos["llm_decision"].get("decision"),
                           pos["llm_decision"].get("confidence_score")])
        except Exception as e: log.error("GS append: %s",e)

# ─────────────────── Telegram commands ────────────────────────────────
async def cmd_start(u:Update,ctx): 
    ctx.application.chat_ids.add(u.effective_chat.id)
    state["monitor"]=True
    await u.message.reply_text("✅ Monitoring ON (v12.3)")
    if not ctx.chat_data.get("task"):
        ctx.chat_data["task"]=asyncio.create_task(monitor(ctx))

async def cmd_stop(u,ctx): state["monitor"]=False; await u.message.reply_text("⛔ Monitoring OFF")
async def cmd_lev(u,ctx):
    try:
        lev=int(u.message.text.split()[1]); assert 1<=lev<=100
        state["leverage"]=lev; await u.message.reply_text(f"Leverage → {lev}×")
    except: await u.message.reply_text("Пример: /leverage 5")

# ──────────────────────── MONITOR LOOP ───────────────────────────────
async def monitor(ctx):
    log.info("Monitor loop start.")
    while True:
        await asyncio.sleep(30)
        if not state["monitor"]: continue
        try:
            ohl = await exchange.fetch_ohlcv(PAIR,'15m',limit=60)
            df  = pd.DataFrame(ohl,columns=['ts','open','high','low','close','volume'])
            ts  = df.ts.iloc[-1]
            if ts == state["last_ts"]: continue
            state["last_ts"]=ts

            df  = calc_ind(df)
            ind = df.iloc[-1]

            # закрытие позиции
            pos = state.get("position")
            if pos:
                price=ind.close
                if (price>=pos["tp"] and pos["side"]=="LONG") or \
                   (price<=pos["tp"] and pos["side"]=="SHORT"):
                       await close_pos("TP",price,ctx); continue
                if (price<=pos["sl"] and pos["side"]=="LONG") or \
                   (price>=pos["sl"] and pos["side"]=="SHORT"):
                       await close_pos("SL",price,ctx); continue

            # новый сигнал
            if pos is None:
                sig = int(ind.ssl_sig)
                if sig==0: continue
                longCond  = sig==1  and ind.close>ind.ema_fast>ind.ema_slow and ind.rsi>RSI_LONGT
                shortCond = sig==-1 and ind.close<ind.ema_fast<ind.ema_slow and ind.rsi<RSI_SHORTT
                side = "LONG" if longCond else "SHORT" if shortCond else None
                if not side: continue

                await broadcast(ctx,f"🔍 Сигнал {side}. Анализ LLM…")
                td = {"asset":PAIR,"tf":"15m","signal":side,
                      "price":round(ind.close,2),
                      "ind":{"rsi":round(ind.rsi,1),
                             "ema_fast":round(ind.ema_fast,2),
                             "ema_slow":round(ind.ema_slow,2)},
                      "volatility_atr":round(ind.atr,4)}
                state["position"]={"opening":True}
                llm = await ask_llm(td,ctx)
                if llm and llm.get("decision")=="APPROVE" \
                       and llm.get("confidence_score",0)>=LLM_CONF_THR:
                    await open_pos(side,ind.close,llm,td,ctx)
                else:
                    await broadcast(ctx,"🧊 LLM отклонил сигнал."); state["position"]=None

        except ccxt.NetworkError as e:
            log.warning("Network err: %s",e)
        except Exception as e:
            log.exception("Loop err: %s",e); state["position"]=None

# ───────────────────────── ENTRY-POINT ────────────────────────────────
async def shutdown(app): await exchange.close()
async def main():
    app = (ApplicationBuilder().token(BOT_TOKEN)
           .defaults(Defaults(parse_mode="HTML"))
           .post_shutdown(shutdown).build())
    app.chat_ids=set(CHAT_IDS)
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("stop" ,cmd_stop ))
    app.add_handler(CommandHandler("leverage",cmd_lev))

    async with app:
        try:
            await exchange.load_markets()
            bal=await exchange.fetch_balance()
            log.info("USDT free=%s total=%s",bal['USDT']['free'],bal['USDT']['total'])
        except Exception as e:
            log.error("Init error: %s",e); return
        await app.start(); await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__=="__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt,SystemExit):
        log.info("Shutdown requested.")
