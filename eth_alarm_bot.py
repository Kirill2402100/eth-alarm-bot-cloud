#!/usr/bin/env python3
# =============================================================================
# eth_alarm_bot.py — v12.2 «Strict-TP/SL» (25-Jun-2025)
#  • Prompt заставляет LLM ВСЕГДА возвращать suggested_tp/sl
#  • _​safe_levels() страхует, если модель вдруг не прислала уровни
#  • Фикс падения NoneType/float и формат-строк
# =============================================================================

import os, asyncio, json, logging, math, time
from datetime import datetime

import numpy as np
import pandas as pd
import ccxt.async_support as ccxt
import gspread, aiohttp
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (ApplicationBuilder, CommandHandler,
                          Defaults, ContextTypes)

# ─────────────────────────── env / logging ────────────────────────────
BOT_TOKEN  = os.getenv("BOT_TOKEN")
CHAT_IDS   = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
PAIR_RAW   = os.getenv("PAIR", "BTC-USDT-SWAP")
SHEET_ID   = os.getenv("SHEET_ID")
INIT_LEV   = int(os.getenv("LEVERAGE", 4))

LLM_API_KEY              = os.getenv("LLM_API_KEY")
LLM_API_URL              = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_CONFIDENCE_THRESHOLD = float(os.getenv("LLM_CONFIDENCE_THRESHOLD", 7.0))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for noisy in ("httpx", "telegram.vendor.httpx", "aiohttp.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ─────────────────────── Google-Sheets (optional) ─────────────────────
_GS_SCOPE = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
if os.getenv("GOOGLE_CREDENTIALS"):
    _creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.getenv("GOOGLE_CREDENTIALS")), _GS_SCOPE)
    _gs = gspread.authorize(_creds)
else:
    _gs = None; log.warning("GOOGLE_CREDENTIALS not set.")

def _ws(title: str):
    if not (_gs and SHEET_ID):
        return None
    ss = _gs.open_by_key(SHEET_ID)
    try:
        return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        return ss.add_worksheet(title, rows=1000, cols=20)

HEADERS = ["DATE-TIME", "POSITION", "DEPOSIT", "ENTRY", "STOP LOSS", "TAKE PROFIT", "RR", "P&L (USDT)", "APR (%)", "LLM DEC", "LLM CONF"]
WS = _ws("AI-V12.2")
if WS and WS.row_values(1) != HEADERS:
    WS.clear(); WS.append_row(HEADERS)

# ──────────────────────────── OKX ────────────────────────────────────
exchange = ccxt.okx({
    "apiKey":    os.getenv("OKX_API_KEY"),
    "secret":    os.getenv("OKX_SECRET"),
    "password":  os.getenv("OKX_PASSWORD"),
    "options":  {"defaultType": "swap"},
    "enableRateLimit": True,
})
PAIR = PAIR_RAW.replace("/", "-").replace(":USDT", "").upper()
if "-SWAP" not in PAIR:
    PAIR += "-SWAP"

# ─── базовые параметры сигнала ───
SSL_LEN, RSI_LEN = 13, 14
RSI_LONGT, RSI_SHORTT = 52, 48

# ─────────────────────── indicator helpers ────────────────────────────

def _ta_rsi(series: pd.Series, length=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(length).mean()
    loss  = (-delta.clip(upper=0)).rolling(length).mean()
    if loss.iloc[-1] == 0:
        return 100
    rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] else float('inf')
    return 100 - 100 / (1 + rs)

def calc_atr(df: pd.DataFrame, length=14):
    high_low   = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close  = (df['low']  - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(length).mean()

def calc_ind(df: pd.DataFrame):
    df['ema_fast'] = df['close'].ewm(span=20).mean()
    df['ema_slow'] = df['close'].ewm(span=50).mean()

    sma = df['close'].rolling(SSL_LEN).mean()
    hi  = df['high'].rolling(SSL_LEN).max()
    lo  = df['low'] .rolling(SSL_LEN).min()
    df['ssl_up'] = np.where(df['close'] > sma, hi, lo)
    df['ssl_dn'] = np.where(df['close'] > sma, lo, hi)

    cross_up   = (df['ssl_up'].shift(1) < df['ssl_dn'].shift(1)) & (df['ssl_up'] > df['ssl_dn'])
    cross_down = (df['ssl_up'].shift(1) > df['ssl_dn'].shift(1)) & (df['ssl_up'] < df['ssl_dn'])
    sig = pd.Series(np.nan, index=df.index)
    sig.loc[cross_up]   = 1
    sig.loc[cross_down] = -1
    df['ssl_sig'] = sig.ffill().fillna(0).astype(int)

    df['rsi'] = _ta_rsi(df['close'], RSI_LEN)
    df['atr'] = calc_atr(df, 14)
    return df

# ─────────────────────────── state ──────────────────────────────────
state = {"monitor": False, "leverage": INIT_LEV, "position": None, "last_ts": 0}

# ────────────────── telegram & helpers ───────────────────────────────
async def broadcast(ctx, text: str):
    for cid in ctx.application.chat_ids:
        try:
            await ctx.application.bot.send_message(cid, text)
        except Exception as e:
            log.warning("Broadcast fail to %s: %s", cid, e)

async def get_free_usdt():
    try:
        bal = await exchange.fetch_balance()
        return bal.get('USDT', {}).get('free', 0) or 0
    except Exception as e:
        log.error("Balance error: %s", e)
        return 0

# ──────────────────────── LLM block ─────────────────────────────────
LLM_PROMPT_TEMPLATE = """
Ты — профессиональный трейдер-аналитик по имени «Сигма». Отвечай
**строго одним JSON-объектом на русском языке**, без пояснений снаружи.
Объект обязательно содержит ВСЕ поля:
  "confidence_score" — число (0-10)
  "decision"         — "APPROVE" или "REJECT"
  "suggested_tp"     — рекомендуемый тейк-профит
  "suggested_sl"     — рекомендуемый стоп-лосс
Если не можешь рассчитать TP/SL — поставь текущую цену ± 2 ATR.

Пример:
{
  "confidence_score": 8.7,
  "decision": "APPROVE",
  "suggested_tp": 103200,
  "suggested_sl": 100800
}

Проанализируй сет-ап ↓
{trade_data}
"""

async def get_llm_decision(trade_data: dict, ctx):
    if not LLM_API_KEY:
        return None
    prompt = LLM_PROMPT_TEMPLATE.format(trade_data=json.dumps(trade_data, ensure_ascii=False, indent=2))
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "gpt-4o", "messages": [{"role": "user", "content": prompt}]}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(LLM_API_URL, headers=headers, json=payload, timeout=45) as r:
                if r.status != 200:
                    await broadcast(ctx, f"❌ Ошибка LLM: status {r.status}")
                    return None
                data = await r.json()
                txt  = data['choices'][0]['message']['content']
                llm  = json.loads(txt)
                await broadcast(ctx,
                    (f"🧠 LLM: Decision={llm['decision']} Conf={llm['confidence_score']}/10\n"
                     f"TP={llm.get('suggested_tp')} | SL={llm.get('suggested_sl')}"))
                return llm
    except Exception as e:
        log.error("LLM call err: %s", e)
        return None

# ───────────────────── safe TP/SL helper ─────────────────────────────

def _safe_levels(llm: dict, entry: float, atr: float, side: str):
    tp = llm.get('suggested_tp') if llm else None
    sl = llm.get('suggested_sl') if llm else None

    if tp is None or sl is None:
        sl = entry - atr*1.5 if side == "LONG" else entry + atr*1.5
        tp = entry + atr*3.0 if side == "LONG" else entry - atr*3.0
        if llm is not None:
            llm['suggested_tp'] = tp; llm['suggested_sl'] = sl
    return tp, sl

# ───────────────────── open / close position ─────────────────────────
async def open_pos(side: str, price: float, llm_decision: dict, trade_data: dict, ctx):
    atr = trade_data.get('volatility_atr', 0) or 0
    usdt = await get_free_usdt()
    if usdt <= 1:
        await broadcast(ctx, "❗ Недостаточно средств."); state['position'] = None; return

    m = exchange.market(PAIR)
    step = m['precision']['amount'] or 0.0001
    qty  = math.floor((usdt * state['leverage'] / price) / step) * step
    if qty < (m['limits']['amount']['min'] or step):
        await broadcast(ctx, f"❗ Qty={qty} меньше минимального."); state['position'] = None; return

    await exchange.set_leverage(state['leverage'], PAIR)
    order = await exchange.create_market_order(PAIR, 'buy' if side=="LONG" else 'sell', qty, params={"tdMode":"isolated"})
    entry = order.get('average', price)

    tp, sl = _safe_levels(llm_decision, entry, atr, side)

    state['position'] = dict(side=side, amount=qty, entry=entry, sl=sl, tp=tp,
                             deposit=usdt, opened=time.time(), llm_decision=llm_decision)
    await broadcast(ctx, f"✅ Открыта {side} qty={qty:.4f} entry={entry:.2f}\nSL={sl:.2f} | TP={tp:.2f}")

async def close_pos(reason: str, price: float, ctx):
    p = state.pop('position', None)
    if not p: return
    order = await exchange.create_market_order(PAIR, 'sell' if p['side']=="LONG" else 'buy', p['amount'], params={"tdMode":"isolated", "reduceOnly":True})
    close_price = order.get('average', price)
    pnl = (close_price - p['entry']) * p['amount'] * (1 if p['side']=="LONG" else -1)
    apr = (pnl / p['deposit']) * (365 / max((time.time()-p['opened'])/86400,1e-9)) * 100
    await broadcast(ctx, f"⛔ Закрыта ({reason}) P&L={pnl:.2f} APR={apr:.1f}%")
    if WS:
        rr = round(abs((p['tp']-p['entry'])/(p['entry']-p['sl'])),2) if p['entry']!=p['sl'] else 0
        WS.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), p['side'], p['deposit'], p['entry'], p['sl'], p['tp'], rr, pnl, round(apr,2), p.get('llm_decision',{}).get('decision','N/A'), p.get('llm_decision',{}).get('confidence_score','N/A')])

# ───────────────────── telegram commands ─────────────────────────────
async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.application.chat_ids.add(u.effective_chat.id)
    state['monitor'] = True
    if not ctx.chat_data.get('task'):
        ctx.chat_data['task'] = asyncio.create_task(monitor(ctx))
    await u.message.reply_text("✅ Monitoring ON (v12.2)")

a_sync=lambda f:CommandHandler(f.__name__[4:],f)
async def cmd_stop(u,ctx): state['monitor']=False; await u.message.reply_text("⛔ Monitoring OFF")
async def cmd_leverage(u,ctx):
    try:
        lev=int(u.message.text.split()[1]); assert 1<=lev<=100
        state['leverage']=lev; await u.message.reply_text(f"Leverage → {lev}×")
    except: await u.message.reply_text("/leverage 5")

# ─────────────────────── main monitor loop ───────────────────────────
async def monitor(ctx):
    log.info("Monitor loop start")
    while True:
        await asyncio.sleep(30)
        if not state['monitor']:
            continue
        try:
            df = pd.DataFrame(await exchange.fetch_ohlcv(PAIR,'15m',limit=50), columns=['ts','open','high','low','close','volume'])
            if df.iloc[-1]['ts']==state['last_ts']:
                continue
            state['last_ts']=df.iloc[-1]['ts']
            df=calc_ind(df)
            ind=df.iloc[-1]
            price=ind['close']

            # check open position
            p=state.get('position')
            if p:
                hit_tp = price>=p['tp'] if p['side']=="LONG" else price<=p['tp']
                hit_sl = price<=p['sl'] if p['side']=="LONG" else price>=p['sl']
                if hit_tp: await close_pos("TP",price,ctx)
                elif hit_sl: await close_pos("SL",price,ctx)
                continue

            # new signal
            sig=int(ind['ssl_sig'])
            if sig==0: continue
            longCond  = sig==1  and (price>ind['ema_fast']>ind['ema_slow']) and ind['rsi']>RSI_LONGT
            shortCond = sig==-1 and (price<ind['ema_fast']<ind['ema_slow']) and ind['rsi']<RSI_SHORTT
            side = "LONG" if longCond else "SHORT" if shortCond else None
            if not side:
                continue

            trade_data={"asset":PAIR,"timeframe":"15m","signal_type":side,
                         "current_price":price,
                         "indicators":{"rsi":round(ind['rsi'],2),"ema_fast":round(ind['ema_fast'],2),"ema_slow":round(ind['ema_slow'],2)},
                         "volatility_atr":round(ind['atr'],4)}
            await broadcast(ctx,f"🔍 Сигнал {side}. Анализ LLM…")
            llm=await get_llm_decision(trade_data,ctx)
            if llm and llm.get('decision')=='APPROVE' and llm.get('confidence_score',0)>=LLM_CONFIDENCE_THRESHOLD:
                await open_pos(side,price,llm,trade_data,ctx)
            else:
                await broadcast(ctx,"🧊 LLM отклонил сигнал.")

        except ccxt.NetworkError as e:
            log.warning("Network: %s", e)
        except Exception as e:
            log.exception("Loop err: %s", e)
            state['position']=None

# ───────────────────────── entry-point ───────────────────────────────
async def shutdown(app): await exchange.close()
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).defaults(Defaults(parse_mode="HTML")).post_shutdown(shutdown).build()
    app.chat_ids=set(CHAT_IDS)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop",  cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_leverage))
    async with app:
        try:
            await exchange.load_markets()
            bal=await exchange.fetch_balance(); log.info("USDT free=%s total=%s", bal['USDT']['free'], bal['USDT']['total'])
        except Exception as e:
            log.error("Exchange init error: %s", e); return
        await app.start(); await app.updater.start_polling(); await asyncio.Event().wait()

if __name__=="__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Graceful shutdown")
