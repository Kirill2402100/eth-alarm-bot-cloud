# ============================================================================
#  eth_alarm_bot.py  ·  Variant B
#  SSL-13 + ATR-confirm + RSI  (ATR/ADX/Volume)  + TP-1 & адаптивный трейл
#  OKX  ·  Python 3.11  ·  2025-06-22
# ============================================================================

import os, asyncio, json, logging, math, time
from datetime import datetime

import numpy as np
import pandas as pd
import ccxt.async_support as ccxt
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, Defaults, ContextTypes

# ──────────── ENV ────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN")
CHAT_IDS      = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
PAIR_RAW      = os.getenv("PAIR", "BTC-USDT-SWAP")
SHEET_ID      = os.getenv("SHEET_ID")
INIT_LEVERAGE = int(os.getenv("LEVERAGE", 1))

# ──────────── LOGGING ────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for m in ("httpx", "telegram.vendor.httpx"):
    logging.getLogger(m).setLevel(logging.WARNING)

# ──────────── Google-Sheets (опционально) ────────────────────────────────────
_GS_SCOPE = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
if os.getenv("GOOGLE_CREDENTIALS"):
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.getenv("GOOGLE_CREDENTIALS")), _GS_SCOPE)
    _gs = gspread.authorize(creds)
else:
    _gs = None
    log.warning("GOOGLE_CREDENTIALS not set — Sheets logging disabled.")

def _open_ws(sheet_id: str, title: str):
    if not _gs: return None
    ss = _gs.open_by_key(sheet_id)
    try:    return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        return ss.add_worksheet(title, rows=1000, cols=20)

HEADERS = ["DATE-TIME","POSITION","DEPOSIT","ENTRY",
           "STOP LOSS","TAKE PROFIT","RR","P&L","APR"]
WS = _open_ws(SHEET_ID, "AI") if SHEET_ID else None
if WS and WS.row_values(1) != HEADERS:
    WS.clear(); WS.append_row(HEADERS)

# ──────────── OKX ────────────────────────────────────────────────────────────
exchange = ccxt.okx({
    "apiKey":   os.getenv("OKX_API_KEY"),
    "secret":   os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "options":  {"defaultType": "swap"},
    "enableRateLimit": True,
})
PAIR = PAIR_RAW.replace("/", "-").replace(":USDT","").upper()
if "-SWAP" not in PAIR: PAIR += "-SWAP"
log.info("Using trading pair: %s", PAIR)

# ──────────── Strategy params ────────────────────────────────────────────────
SSL_LEN = 13
USE_ATR_CONF=True; PC_ATR_MUL=0.60
PC_LONG_PERC=PC_SHORT_PERC=0.40/100

RSI_LEN=14; RSI_LONGT=55; RSI_SHORTT=45
ATR_LEN=14; ATR_MIN_PCT=0.35/100
ADX_LEN=14; ADX_MIN=24

USE_VOL_FILTER=True; VOL_MULT=1.40; VOL_LEN=20

TP1_SHARE=0.20; TP1_ATR_MUL=1.0
TRAIL_ATR_MUL=0.65; TP1_PCT=1.0/100; TRAIL_PCT=0.60/100
WAIT_BARS=1                      # пауза после закрытия

# ──────────── Indicators ─────────────────────────────────────────────────────
def _ta_rsi(s: pd.Series, length=14):
    d = s.diff(); g = d.clip(lower=0).rolling(length).mean()
    l = (-d.clip(upper=0)).rolling(length).mean()
    return 100-100/(1+g/l)

def calc_ssl(df: pd.DataFrame):
    sma = df['close'].rolling(SSL_LEN).mean()
    up, dn = [], []
    for i in range(len(df)):
        if i < SSL_LEN-1:
            up.append(np.nan); dn.append(np.nan); continue
        hi = df['high'].iloc[i-SSL_LEN+1:i+1].max()
        lo = df['low' ].iloc[i-SSL_LEN+1:i+1].min()
        if df['close'].iloc[i] > sma.iloc[i]:
            up.append(hi); dn.append(lo)
        else:
            up.append(lo); dn.append(hi)
    df['ssl_up'], df['ssl_dn'] = up, dn
    sig=[0]
    for i in range(1,len(df)):
        pu,pd,cu,cd = df.at[i-1,'ssl_up'],df.at[i-1,'ssl_dn'],df.at[i,'ssl_up'],df.at[i,'ssl_dn']
        if not np.isnan([pu,pd,cu,cd]).any():
            sig.append( 1 if pu<pd and cu>cd else -1 if pu>pd and cu<cd else sig[-1])
        else: sig.append(sig[-1])
    df['ssl_sig']=sig
    df['rsi']=_ta_rsi(df['close'],RSI_LEN)
    df['atr']=df['close'].rolling(ATR_LEN).apply(lambda x:x.max()-x.min(),raw=False)
    df['adx']=(df['high']-df['low']).ewm(span=ADX_LEN).mean()
    df['vol_ok']=~USE_VOL_FILTER | (df['volume']>df['volume'].rolling(VOL_LEN).mean()*VOL_MULT)
    return df

# ──────────── Global state ───────────────────────────────────────────────────
state=dict(
    monitor=False,
    leverage=INIT_LEVERAGE,
    position=None,          # dict | None
    bars_since_close=999,
    dir_sig=0,              # +1 / -1 / 0
    base_price=np.nan,      # цена на кроссе
    alert12=0               # отправлено ли “Cond1+2 OK” (+1 / -1 / 0)
)

# ──────────── Telegram helpers ───────────────────────────────────────────────
async def broadcast(ctx, txt):
    for cid in ctx.application.chat_ids:
        try: await ctx.application.bot.send_message(cid, txt)
        except: pass

async def cmd_start(u:Update,c:ContextTypes.DEFAULT_TYPE):
    c.application.chat_ids.add(u.effective_chat.id)
    state["monitor"]=True
    await u.message.reply_text("✅ Monitoring ON")
    if not c.chat_data.get("task"):
        c.chat_data["task"]=asyncio.create_task(monitor(c))

async def cmd_stop(u:Update,c:ContextTypes.DEFAULT_TYPE):
    state["monitor"]=False; await u.message.reply_text("⛔ Monitoring OFF")

async def cmd_leverage(u:Update,c:ContextTypes.DEFAULT_TYPE):
    try: lev=int(u.message.text.split()[1]); lev=max(1,min(100,lev))
    except: return await u.message.reply_text("/leverage 4")
    state['leverage']=lev; await u.message.reply_text(f"↔ leverage set → {lev}×")

# ──────────── Trading helpers ────────────────────────────────────────────────
async def get_usdt(): bal=await exchange.fetch_balance(); return bal['USDT'].get('available',0)

async def open_pos(side,price,ctx):
    usdt=await get_usdt(); m=exchange.market(PAIR)
    step=m['precision']['amount'] or 1e-4; min_amt=m['limits']['amount']['min'] or step
    qty=math.floor((usdt*state['leverage']/price)/step)*step; qty=round(qty,8)
    if qty<min_amt: return await broadcast(ctx,f"❗ Мало средств ({qty}< {min_amt})")
    await exchange.set_leverage(state['leverage'],PAIR)
    order=await exchange.create_market_order(PAIR,'buy' if side=='LONG' else 'sell',qty)
    entry=order['average'] or price
    tp=entry*(1+TP1_ATR_MUL*TRAIL_PCT) if side=='LONG' else entry*(1-TP1_ATR_MUL*TRAIL_PCT)
    sl=entry*(1-TRAIL_PCT)             if side=='LONG' else entry*(1+TRAIL_PCT)
    state['position']=dict(side=side,amount=qty,entry=entry,tp=tp,sl=sl,deposit=usdt,opened=time.time())
    state['bars_since_close']=0; state['alert12']=0
    await broadcast(ctx,f"🟢 Открыта {side} qty={qty} entry={entry:.2f}")

async def close_pos(reason,price,ctx):
    p=state['position'];   if not p: return
    order=await exchange.create_market_order(PAIR,'sell' if p['side']=='LONG' else 'buy',
                                             p['amount'],params={"reduceOnly":True})
    close_p=order['average'] or price
    pnl=(close_p-p['entry'])*p['amount']*(1 if p['side']=='LONG' else -1)
    apr=(pnl/p['deposit'])*(365/max((time.time()-p['opened'])/86400,1e-9))*100
    await broadcast(ctx,f"🔴 Закрыта ({reason}) pnl={pnl:.2f} APR={apr:.1f}%")
    state.update(position=None,bars_since_close=0)

# ──────────── Main monitor ───────────────────────────────────────────────────
async def monitor(ctx):
    log.info("monitor loop started")
    while True:
        if not state['monitor']: await asyncio.sleep(2); continue
        try:
            ohlcv=await exchange.fetch_ohlcv(PAIR,'15m',limit=150)
        except Exception as e:
            log.warning("fetch_ohlcv error %s",e); await asyncio.sleep(15); continue

        df=calc_ssl(pd.DataFrame(ohlcv,columns=['ts','open','high','low','close','volume']))
        row=df.iloc[-1]; price=row['close']; state['bars_since_close']+=1

        # ① новый кросс SSL → запоминаем направление и базовую цену
        if int(row['ssl_sig'])!=state['dir_sig']:
            state.update(dir_sig=int(row['ssl_sig']),
                         base_price=price,
                         alert12=0)          # сбросить предыдущий сигнал

        # ② стоп-контроль
        pos=state['position']
        if pos:
            hit_tp= price>=pos['tp'] if pos['side']=='LONG' else price<=pos['tp']
            hit_sl= price<=pos['sl'] if pos['side']=='LONG' else price>=pos['sl']
            if hit_tp: await close_pos("TP",price,ctx)
            elif hit_sl: await close_pos("SL",price,ctx)

        # ③ если нет позиции — отслеживаем набор условий
        if not state['position'] and state['dir_sig'] and state['bars_since_close']>=WAIT_BARS:
            sig=state['dir_sig']; rsi=row['rsi']; atr=row['atr']; adx=row['adx']; v_ok=row['vol_ok']
            cond2 = (sig==1 and rsi>RSI_LONGT) or (sig==-1 and rsi<RSI_SHORTT)
            # отправляем инфо-сообщение один раз
            if cond2 and state['alert12']!=sig:
                delta_pct=abs(price-state['base_price'])/state['base_price']*100
                await broadcast(ctx,
                    f"ℹ️ Cond 1+2 OK ({ 'LONG' if sig==1 else 'SHORT' }) | RSI {rsi:.1f} | Δ {delta_pct:.2f}%")
                state['alert12']=sig

            # Фильтр 3: цена ушла ≥ 0.6 ATR (или 0.4 %)
            if cond2 and v_ok and atr/price>ATR_MIN_PCT and adx>ADX_MIN:
                if USE_ATR_CONF:
                    passed = (sig==1 and price>=state['base_price']+PC_ATR_MUL*atr) or \
                             (sig==-1 and price<=state['base_price']-PC_ATR_MUL*atr)
                else:
                    passed = (sig==1 and price>=state['base_price']*(1+PC_LONG_PERC)) or \
                             (sig==-1 and price<=state['base_price']*(1-PC_SHORT_PERC))
                if passed:
                    await open_pos("LONG" if sig==1 else "SHORT",price,ctx)

        await asyncio.sleep(30)

# ──────────── Boilerplate ────────────────────────────────────────────────────
async def shutdown(app): await exchange.close()

async def main():
    app=(ApplicationBuilder().token(BOT_TOKEN)
         .defaults(Defaults(parse_mode="HTML"))
         .post_shutdown(shutdown).build())
    app.chat_ids=set(CHAT_IDS)
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("leverage",cmd_leverage))
    async with app:
        await exchange.load_markets()
        await app.initialize(); await app.start(); await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(main())
