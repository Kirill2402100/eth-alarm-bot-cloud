#!/usr/bin/env python3
# ============================================================================
#  eth_alarm_bot.py — Variant B  (23-Jun-2025)
#  SSL-13 + ATR-confirm + RSI  |  ATR/ADX/Volume filter
#  TP-1 + адаптивный trail     |  Реальная торговля OKX (isolated margin)
# ============================================================================

import os, asyncio, json, logging, math, time
from datetime import datetime

import numpy as np
import pandas as pd
import ccxt.async_support as ccxt          # ← async-API
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (ApplicationBuilder, CommandHandler,
                          Defaults, ContextTypes)

# ─────────────────────────── env / logging ────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_IDS    = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
PAIR_RAW    = os.getenv("PAIR", "BTC-USDT-SWAP")
SHEET_ID    = os.getenv("SHEET_ID")
INIT_LEV    = int(os.getenv("LEVERAGE", 4))          # default 4× для примера

logging.basicConfig(level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for noisy in ("httpx", "telegram.vendor.httpx"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ───────────────────────── Google Sheets (опц.) ───────────────────────
_GS_SCOPE = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
if os.getenv("GOOGLE_CREDENTIALS"):
    _creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.getenv("GOOGLE_CREDENTIALS")), _GS_SCOPE)
    _gs = gspread.authorize(_creds)
else:
    _gs = None; log.warning("GOOGLE_CREDENTIALS not set — Sheets disabled.")

def _ws(title: str):
    if not (_gs and SHEET_ID): return None
    ss = _gs.open_by_key(SHEET_ID)
    try:    return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        return ss.add_worksheet(title, rows=1000, cols=20)

HEADERS = ["DATE-TIME","POSITION","DEPOSIT","ENTRY",
           "STOP LOSS","TAKE PROFIT","RR","P&L (USDT)","APR (%)"]
WS = _ws("AI")
if WS and WS.row_values(1) != HEADERS:
    WS.clear(); WS.append_row(HEADERS)

# ────────────────────────────  OKX  ───────────────────────────────────
exchange = ccxt.okx({
    "apiKey":   os.getenv("OKX_API_KEY"),
    "secret":   os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "options":  {"defaultType": "swap"},
    "enableRateLimit": True,
})

PAIR = PAIR_RAW.replace("/", "-").replace(":USDT", "").upper()
if "-SWAP" not in PAIR: PAIR += "-SWAP"

# ──────────────── Strategy parameters (Variant B) ─────────────────────
SSL_LEN        = 13
USE_ATR_CONF   = True
PC_ATR_MUL     = 0.60
PC_LONG_PERC   = 0.40/100
PC_SHORT_PERC  = 0.40/100

RSI_LEN   = 14
RSI_LONGT  = 55
RSI_SHORTT = 45

ATR_LEN   = 14
ATR_MIN_PCT = 0.35/100
ADX_LEN   = 14
ADX_MIN   = 24

USE_VOL_FILTER = True
VOL_MULT = 1.40
VOL_LEN  = 20

TP1_SHARE    = 0.20
TP1_ATR_MUL  = 1.00
TRAIL_ATR_MUL= 0.65
TP1_PCT      = 1.0/100
TRAIL_PCT    = 0.60/100

WAIT_BARS = 1

# ─────────────────────────── indicators ───────────────────────────────
def _ta_rsi(series: pd.Series, length=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(length).mean()
    loss  = (-delta.clip(upper=0)).rolling(length).mean()
    return 100 - 100/(1+gain/loss)

def calc_ind(df: pd.DataFrame):
    sma = df['close'].rolling(SSL_LEN).mean()
    ssl_up, ssl_dn = [], []
    for i in range(len(df)):
        if i < SSL_LEN-1:
            ssl_up.append(np.nan); ssl_dn.append(np.nan); continue
        hi = df['high'].iloc[i-SSL_LEN+1:i+1].max()
        lo = df['low' ].iloc[i-SSL_LEN+1:i+1].min()
        if df['close'].iloc[i] > sma.iloc[i]:
            ssl_up.append(hi); ssl_dn.append(lo)
        else:
            ssl_up.append(lo); ssl_dn.append(hi)
    df['ssl_up'], df['ssl_dn'] = ssl_up, ssl_dn

    sig=[0]
    for i in range(1,len(df)):
        pu,pd = df.at[i-1,'ssl_up'],df.at[i-1,'ssl_dn']
        cu,cd = df.at[i  ,'ssl_up'],df.at[i  ,'ssl_dn']
        if not np.isnan([pu,pd,cu,cd]).any():
            sig.append( 1 if (pu<pd and cu>cd) else
                       -1 if (pu>pd and cu<cd) else sig[-1])
        else: sig.append(sig[-1])
    df['ssl_sig']=sig

    df['rsi']   = _ta_rsi(df['close'], RSI_LEN)
    df['atr']   = df['close'].rolling(ATR_LEN).apply(
        lambda x: pd.Series(x).max()-pd.Series(x).min(), raw=False)
    df['adx']   = (df['high']-df['low']).ewm(span=ADX_LEN).mean()
    df['vol_ok']= (~USE_VOL_FILTER) | (
        df['volume'] > df['volume'].rolling(VOL_LEN).mean()*VOL_MULT)
    return df

# ───────────────────────────  state  ──────────────────────────────────
state = {
    "monitor": False,
    "leverage": INIT_LEV,
    "position": None,       # дикт или None
    "pending":  None,       # {'dir','ssl_price','rsi'}
    "bars_since_close": 999,
}

# ───────────────────────── helpers / telegram ─────────────────────────
async def broadcast(ctx, txt):
    for cid in ctx.application.chat_ids:
        try: await ctx.application.bot.send_message(cid, txt)
        except: pass

async def get_free_usdt():
    bal = await exchange.fetch_balance()
    return bal['USDT'].get('available') or bal['USDT'].get('free') or 0

# ─────────────────────── open / close position ────────────────────────
async def open_pos(side:str, price:float, ctx):
    usdt = await get_free_usdt()
    m    = exchange.market(PAIR)
    step = m['precision']['amount'] or 0.0001
    qty  = math.floor((usdt*state['leverage']/price)/step)*step
    qty  = round(qty,8)
    if qty < (m['limits']['amount']['min'] or step):
        await broadcast(ctx,f"❗ Недостаточно средств: qty={qty}")
        return

    await exchange.set_leverage(state['leverage'], PAIR)
    params = {"tdMode":"isolated",
              "posSide": "long" if side=="LONG" else "short"}

    order = await exchange.create_market_order(
        PAIR, 'buy' if side=="LONG" else 'sell', qty, params=params)

    entry = order['average'] or price
    tp = entry*(1+TP1_ATR_MUL*TRAIL_PCT) if side=="LONG" else entry*(1-TP1_ATR_MUL*TRAIL_PCT)
    sl = entry*(1-TRAIL_PCT)             if side=="LONG" else entry*(1+TRAIL_PCT)
    state['position'] = dict(side=side, amount=qty, entry=entry,
                             tp=tp, sl=sl, deposit=usdt, opened=time.time())
    state['bars_since_close']=0
    await broadcast(ctx, f"🟢 Открыта {side}  qty={qty:.5f}  entry={entry:.2f}")

async def close_pos(reason:str, price:float, ctx):
    p = state['position'];  state['position']=None
    if not p: return
    params = {"tdMode":"isolated",
              "posSide": "long" if p['side']=="LONG" else "short",
              "reduceOnly": True}
    try:
        order = await exchange.create_market_order(
            PAIR, 'sell' if p['side']=="LONG" else 'buy', p['amount'], params=params)
        close_price = order['average'] or price
    except Exception as e:
        log.error("close_pos order error: %s", e)
        close_price = price

    pnl  = (close_price-p['entry'])*p['amount']*(1 if p['side']=="LONG" else -1)
    days = max((time.time()-p['opened'])/86400,1e-9)
    apr  = (pnl/p['deposit'])*(365/days)*100
    await broadcast(ctx,f"🔴 Закрыта ({reason}) pnl={pnl:.2f} APR={apr:.1f}%")

    if WS:
        rr = round(abs((p['tp']-p['entry'])/(p['entry']-p['sl'])),2)
        WS.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                       p['side'],p['deposit'],p['entry'],p['sl'],p['tp'],
                       rr,pnl,round(apr,2)])

# ──────────────────────── telegram commands ───────────────────────────
async def cmd_start(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    ctx.application.chat_ids.add(u.effective_chat.id)
    state["monitor"]=True
    await u.message.reply_text("✅ Monitoring ON")
    if not ctx.chat_data.get("task"):
        ctx.chat_data["task"]=asyncio.create_task(monitor(ctx))

async def cmd_stop(u:Update,ctx): state.update(monitor=False);await u.message.reply_text("⛔ Monitoring OFF")
async def cmd_lev(u:Update,ctx):
    try:
        lev=int(u.message.text.split()[1]); assert 1<=lev<=100
        state["leverage"]=lev; await u.message.reply_text(f"Leverage → {lev}×")
    except: await u.message.reply_text("/leverage 4")

# ───────────────────────── main monitor loop ──────────────────────────
def clear_pending(): state["pending"]=None

async def monitor(ctx):
    log.info("monitor started")
    while True:
        if not state["monitor"]: await asyncio.sleep(2); continue
        try:
            ohlcv = await exchange.fetch_ohlcv(PAIR,'15m',limit=150)
        except Exception as e:
            log.warning("fetch_ohlcv error %s",e); await asyncio.sleep(30); continue

        df  = pd.DataFrame(ohlcv,columns=['ts','open','high','low','close','volume'])
        ind = calc_ind(df).iloc[-1]
        price = ind['close']

        # --- стоп-логика -------------------------------------------------
        pos = state["position"]
        if pos:
            hit_tp = price>=pos['tp'] if pos['side']=="LONG" else price<=pos['tp']
            hit_sl = price<=pos['sl'] if pos['side']=="LONG" else price>=pos['sl']
            if hit_tp: await close_pos("TP",price,ctx)
            elif hit_sl: await close_pos("SL",price,ctx)
        else:
            state["bars_since_close"] += 1

        sig = int(ind['ssl_sig'])

        # ===================== 1) если позиция уже есть ==================
        if state["position"]:
            # если произошло обратное пересечение — закрываем
            if (sig== 1 and state['position']['side']=="SHORT") or \
               (sig==-1 and state['position']['side']=="LONG"):
                await close_pos("пересечение SSL", price, ctx)
            await asyncio.sleep(30); continue

        # ===================== 2) нет позиции, нет pending ===============
        if state["pending"] is None and state["bars_since_close"]>=WAIT_BARS:
            cond12 = (
                sig!=0 and ind['vol_ok'] and
                ind['atr']/price>ATR_MIN_PCT and ind['adx']>ADX_MIN and
                ((sig==1 and ind['rsi']>RSI_LONGT) or (sig==-1 and ind['rsi']<RSI_SHORTT))
            )
            if cond12:
                state["pending"] = dict(dir="LONG" if sig==1 else "SHORT",
                                        ssl_price=price, rsi=float(ind['rsi']))
                await broadcast(ctx, f"ℹ️ Cond 1+2 OK ({state['pending']['dir']}) | "
                                     f"SSL {price:.0f} | RSI {ind['rsi']:.1f} | Δ 0.00%")
            await asyncio.sleep(30); continue

        # ===================== 3) pending есть, ждём Δ ===================
        if state["pending"]:
            pend = state["pending"]
            delta = abs(price-pend['ssl_price'])
            need  = PC_ATR_MUL*ind['atr'] if USE_ATR_CONF else pend['ssl_price']*PC_LONG_PERC
            same_dir = (sig==1 and pend['dir']=="LONG") or (sig==-1 and pend['dir']=="SHORT")

            # A) cond 3 выполнено  →  open
            if same_dir and delta>=need:
                await open_pos(pend['dir'], price, ctx)
                clear_pending(); await asyncio.sleep(30); continue

            # B) пересечение поменялось ⇒ сбрасываем
            if not same_dir and sig!=0:
                clear_pending()

        await asyncio.sleep(30)

# ───────────────────────── graceful shutdown ──────────────────────────
async def shutdown(app): await exchange.close()

# ───────────────────────── entry-point ────────────────────────────────
async def main():
    app = (ApplicationBuilder().token(BOT_TOKEN)
           .defaults(Defaults(parse_mode="HTML"))
           .post_shutdown(shutdown).build())
    app.chat_ids=set(CHAT_IDS)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop",  cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_lev))

    async with app:
        await exchange.load_markets()
        bal=await exchange.fetch_balance(); log.info("USDT balance: %s", bal["total"].get("USDT"))
        await app.start(); await app.updater.start_polling()
        log.info("Bot polling started.")
        await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(main())
