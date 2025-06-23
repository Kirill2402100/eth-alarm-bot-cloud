# ============================================================================
#  eth_alarm_bot.py — Variant B (SSL-13 + ATR confirm + RSI + ATR/ADX/Vol)
#  TP-1 + адаптивный trailing-stop        © 2025-06-23
# ============================================================================

import os, asyncio, json, logging, math, time
from datetime import datetime
import numpy as np, pandas as pd
import ccxt.async_support as ccxt
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, Defaults, ContextTypes

# ─────────── ENV ───────────
BOT_TOKEN      = os.getenv("BOT_TOKEN")
CHAT_IDS       = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
PAIR_RAW       = os.getenv("PAIR", "BTC-USDT-SWAP")
SHEET_ID       = os.getenv("SHEET_ID")
INIT_LEVERAGE  = int(os.getenv("LEVERAGE", 1))

# ─────────── LOG ───────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for n in ("httpx", "telegram.vendor.httpx"):
    logging.getLogger(n).setLevel(logging.WARNING)

# ─────────── Google Sheets ───────────
_GS_SCOPE = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
if os.getenv("GOOGLE_CREDENTIALS"):
    _creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.getenv("GOOGLE_CREDENTIALS")), _GS_SCOPE)
    _gs = gspread.authorize(_creds)
else:
    _gs = None
    log.warning("GOOGLE_CREDENTIALS not set — Sheets logging disabled.")

def _open_ws(sheet_id, title):
    if not _gs: return None
    ss = _gs.open_by_key(sheet_id)
    try:    return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        return ss.add_worksheet(title, 1000, 20)

HEADERS=["DATE-TIME","POSITION","DEPOSIT","ENTRY","STOP LOSS",
         "TAKE PROFIT","RR","P&L","APR"]
WS=_open_ws(SHEET_ID,"AI") if SHEET_ID else None
if WS and WS.row_values(1)!=HEADERS:
    WS.clear(); WS.append_row(HEADERS)

# ─────────── OKX ───────────
exchange=ccxt.okx({
    "apiKey":os.getenv("OKX_API_KEY"),
    "secret":os.getenv("OKX_SECRET"),
    "password":os.getenv("OKX_PASSWORD"),
    "options":{"defaultType":"swap"},
    "enableRateLimit":True,
})
PAIR=PAIR_RAW.replace("/","-").replace(":USDT","").upper()
if "-SWAP" not in PAIR: PAIR+="-SWAP"
log.info("Using trading pair: %s", PAIR)

# ─────────── Strategy params ───────────
SSL_LEN=13
USE_ATR_CONF,PC_ATR_MUL=True,0.60
PC_LONG_PERC=PC_SHORT_PERC=0.40/100
RSI_LEN,RSI_LONGT,RSI_SHORTT=14,55,45
ATR_LEN,ATR_MIN_PCT=14,0.35/100
ADX_LEN,ADX_MIN=14,24
USE_VOL_FILTER,VOL_MULT,VOL_LEN=True,1.4,20
TP1_SHARE,TP1_ATR_MUL,TRAIL_ATR_MUL=0.20,1.0,0.65
TP1_PCT,TRAIL_PCT=1.0/100,0.60/100
WAIT_BARS=1

# ─────────── Indicators ───────────
def _rsi(s,l=14):
    d=s.diff(); g=d.clip(lower=0).rolling(l).mean()
    loss=(-d.clip(upper=0)).rolling(l).mean()
    return 100-100/(1+g/loss)

def calc_ind(df):
    sma=df['close'].rolling(SSL_LEN).mean()
    up,dn=[],[]
    for i in range(len(df)):
        if i<SSL_LEN-1: up.append(np.nan); dn.append(np.nan); continue
        hi=df['high'].iloc[i-SSL_LEN+1:i+1].max()
        lo=df['low' ].iloc[i-SSL_LEN+1:i+1].min()
        if df['close'][i]>sma[i]: up.append(hi); dn.append(lo)
        else:                     up.append(lo); dn.append(hi)
    df['ssl_up'],df['ssl_dn']=up,dn
    sig=[0]
    for i in range(1,len(df)):
        pu,pd,cu,cd=df.at[i-1,'ssl_up'],df.at[i-1,'ssl_dn'],df.at[i,'ssl_up'],df.at[i,'ssl_dn']
        if not np.isnan([pu,pd,cu,cd]).any():
            if pu<pd and cu>cd: sig.append( 1)
            elif pu>pd and cu<cd: sig.append(-1)
            else: sig.append(sig[-1])
        else: sig.append(sig[-1])
    df['ssl_sig']=sig
    df['rsi']=_rsi(df['close'],RSI_LEN)
    df['atr'] = df['close'].rolling(ATR_LEN).max() - df['close'].rolling(ATR_LEN).min()    
    df['adx']=(df['high']-df['low']).ewm(span=ADX_LEN).mean()
    df['vol_ok']=(~USE_VOL_FILTER)|(
        df['volume']>df['volume'].rolling(VOL_LEN).mean()*VOL_MULT)
    return df

# ─────────── State ───────────
state={"monitor":False,"leverage":INIT_LEVERAGE,"position":None,
       "bars_since_close":999,"pending":None}

# ─────────── Telegram helpers ───────────
async def say(ctx,txt):
    for cid in ctx.application.chat_ids:
        try: await ctx.application.bot.send_message(cid,txt)
        except: pass

async def cmd_start(u:Update,c:ContextTypes.DEFAULT_TYPE):
    c.application.chat_ids.add(u.effective_chat.id)
    state["monitor"]=True
    await u.message.reply_text("✅ Monitoring ON")
    if not c.chat_data.get("task"):
        c.chat_data["task"]=asyncio.create_task(monitor(c))

async def cmd_stop(u:Update,c): state["monitor"]=False; await u.message.reply_text("⛔ Monitoring OFF")

async def cmd_leverage(u:Update,c):
    parts=u.message.text.split(maxsplit=1)
    if len(parts)!=2 or not parts[1].isdigit():
        await u.message.reply_text("/leverage 3"); return
    state['leverage']=max(1,min(100,int(parts[1])))
    await u.message.reply_text(f"↔ leverage → {state['leverage']}×")

# ─────────── Trading helpers ───────────
async def free_usdt():
    b=await exchange.fetch_balance()
    return b['USDT'].get('available') or b['USDT'].get('free') or 0

async def open_pos(side,price,ctx):
    usdt=await free_usdt(); m=exchange.market(PAIR)
    step=m['precision']['amount'] or 0.0001
    qty=math.floor((usdt*state['leverage']/price)/step)*step
    if qty < (m['limits']['amount']['min'] or step):
        await say(ctx,f"❗ Недостаточно средств ({qty})"); return
    await exchange.set_leverage(state['leverage'],PAIR)
    o=await exchange.create_market_order(PAIR,'buy' if side=='LONG' else 'sell',qty)
    entry=o['average'] or price
    tp=entry*(1+TP1_ATR_MUL*TRAIL_PCT) if side=='LONG' else entry*(1-TP1_ATR_MUL*TRAIL_PCT)
    sl=entry*(1-TRAIL_PCT)             if side=='LONG' else entry*(1+TRAIL_PCT)
    state['position']=dict(side=side,amount=qty,entry=entry,tp=tp,sl=sl,
                           deposit=usdt,opened=time.time())
    state['pending']=None; state['bars_since_close']=0
    await say(ctx,f"🟢 Открыта {side}  qty={qty}  entry={entry:.2f}")

async def close_pos(reason,price,ctx):
    p=state['position']; state['position']=None
    o=await exchange.create_market_order(PAIR,'sell' if p['side']=='LONG' else 'buy',
                                         p['amount'],params={"reduceOnly":True})
    close=o['average'] or price
    pnl=(close-p['entry'])*p['amount']*(1 if p['side']=='LONG' else -1)
    days=max((time.time()-p['opened'])/86400,1e-9)
    apr=pnl/p['deposit']*365/days*100
    await say(ctx,f"🔴 Закрыта ({reason}) pnl={pnl:.2f} APR={apr:.1f}%")
    if WS:
        rr=abs((p['tp']-p['entry'])/(p['entry']-p['sl'])); now=datetime.utcnow()
        WS.append_row([now.strftime("%Y-%m-%d %H:%M:%S"),p['side'],p['deposit'],
                       p['entry'],p['sl'],p['tp'],round(rr,2),pnl,round(apr,2)])
    state['bars_since_close']=0

# ─────────── Monitor ───────────
async def monitor(ctx):
    log.info("monitor started")
    while True:
        if not state['monitor']: await asyncio.sleep(2); continue
        try:
            ohlcv=await exchange.fetch_ohlcv(PAIR,'15m',limit=150)
        except Exception as e:
            log.warning("fetch err: %s",e); await asyncio.sleep(10); continue
        df=calc_ind(pd.DataFrame(ohlcv,columns=['ts','open','high','low','close','volume']))
        row=df.iloc[-1]; price=row['close']

        # --- стопы ---
        pos=state['position']
        if pos:
            if (price>=pos['tp'] if pos['side']=='LONG' else price<=pos['tp']):
                await close_pos("TP",price,ctx)
            elif (price<=pos['sl'] if pos['side']=='LONG' else price>=pos['sl']):
                await close_pos("SL",price,ctx)
        else:
            state['bars_since_close']+=1

        sig=int(row['ssl_sig'])
        pend=state['pending']
        if pend and sig!=pend['side']:     # обратный крест
            state['pending']=None; pend=None
        if sig!=0 and (not pend or sig!=pend['side']):
            state['pending']=dict(side=sig,cross_price=price,rsi_ok=False)
            pend=state['pending']

        # --- RSI ---
        if pend:
            rsi_ok=(row['rsi']>RSI_LONGT) if pend['side']==1 else (row['rsi']<RSI_SHORTT)
            if rsi_ok and not pend['rsi_ok']:
                pend['rsi_ok']=True
                msg=(f"ℹ️ Cond 1+2 OK "
                     f"({'LONG' if pend['side']==1 else 'SHORT'})"
                     f" | SSL {pend['cross_price']:.0f}"
                     f" | RSI {row['rsi']:.1f}"
                     f" | Δ 0.00%")
                await say(ctx,msg)

        # --- Δ-фильтр ---
        pend=state['pending']
        if pend and pend.get('rsi_ok'):
            delta=abs(price-pend['cross_price'])/pend['cross_price']
            atr_ratio=row['atr']/price
            if (USE_ATR_CONF and delta>=PC_ATR_MUL*atr_ratio) or \
               (not USE_ATR_CONF and delta>=(PC_LONG_PERC if pend['side']==1 else PC_SHORT_PERC)):
                await open_pos("LONG" if pend['side']==1 else "SHORT",price,ctx)

        await asyncio.sleep(30)

# ─────────── main ───────────
async def shutdown(app): await exchange.close()

async def main():
    app=(ApplicationBuilder().token(BOT_TOKEN)
         .defaults(Defaults(parse_mode="HTML"))
         .post_shutdown(shutdown).build())
    app.chat_ids=set(CHAT_IDS)
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_leverage))
    async with app:
        await app.initialize(); await exchange.load_markets()
        bal=await exchange.fetch_balance()
        log.info("USDT balance: %s",bal['total'].get('USDT','N/A'))
        await app.start(); await app.updater.start_polling()
        asyncio.create_task(monitor(ContextTypes.DEFAULT_TYPE(application=app)))
        await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(main())
