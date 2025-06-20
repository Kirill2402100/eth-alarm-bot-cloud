# ============================================================================
#  eth_alarm_bot.py — Variant B (SSL-13 + ATR-confirm + RSI,   ATR/ADX/Volume filter)
#  Реальная торговля на OKX  (TP-1 + адаптивный трейлинг)
#  © 2025-06-20  
# ============================================================================

import os, asyncio, json, logging, math, time
from datetime import datetime

import numpy as np
import pandas as pd
import ccxt.async_support as ccxt
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    ContextTypes, Defaults
)

# ─────────────────────────── Переменные окружения ───────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN")
CHAT_IDS       = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
PAIR_RAW       = os.getenv("PAIR", "BTC-USDT-SWAP")
SHEET_ID       = os.getenv("SHEET_ID")
INIT_LEVERAGE  = int(os.getenv("LEVERAGE", 1))

# ─────────────────────────────── Логирование ────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for noisy in ("httpx", "telegram.vendor.httpx"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ───────────────────────────── Google-Sheets ────────────────────────────────
_GS_SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
if os.getenv("GOOGLE_CREDENTIALS"):
    creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    _creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, _GS_SCOPE)
    _gs = gspread.authorize(_creds)
else:
    _gs = None
    log.warning("GOOGLE_CREDENTIALS not set — Sheets logging disabled.")

def _open_worksheet(sheet_id: str, title: str):
    if not _gs: return None
    ss = _gs.open_by_key(sheet_id)
    try:    return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        return ss.add_worksheet(title, rows=1000, cols=20)

HEADERS = [
    "DATE-TIME", "POSITION", "DEPOSIT", "ENTRY", "STOP LOSS",
    "TAKE PROFIT", "RR", "P&L (USDT)", "APR (%)"
]
WS = None
if SHEET_ID:
    WS = _open_worksheet(SHEET_ID, "AI")
    if WS and WS.row_values(1) != HEADERS:
        WS.clear(); WS.append_row(HEADERS)

# ─────────────────────────────── OKX ─────────────────────────────────────────
exchange = ccxt.okx({
    "apiKey":   os.getenv("OKX_API_KEY"),
    "secret":   os.getenv("OKX_SECRET"   ),
    "password": os.getenv("OKX_PASSWORD" ),
    "options":  {"defaultType": "swap"},
    "enableRateLimit": True,
})

PAIR = PAIR_RAW.replace("/", "-").replace(":USDT", "").upper()
if "-SWAP" not in PAIR:
    PAIR += "-SWAP"
log.info("Using trading pair: %s", PAIR)

# ─────────────────────── Параметры Variant B стратегии ──────────────────────
SSL_LEN          = 13
USE_ATR_CONF     = True
PC_ATR_MUL       = 0.60
PC_LONG_PERC     = 0.40 / 100
PC_SHORT_PERC    = 0.40 / 100

RSI_LEN          = 14
RSI_LONG_T       = 55
RSI_SHORT_T      = 45

ATR_LEN          = 14
ATR_MIN_PCT      = 0.35 / 100
ADX_LEN          = 14
ADX_MIN          = 24

USE_VOL_FILTER   = True
VOL_MULT         = 1.40
VOL_LEN          = 20

USE_ATR_STOPS    = True
TP1_SHARE        = 0.20
TP1_ATR_MUL      = 1.0
TRAIL_ATR_MUL    = 0.65
TP1_PCT          = 1.0 / 100
TRAIL_PCT        = 0.60 / 100

WAIT_BARS        = 1   # пауза после закрытия позиции

# ─────────────────────────── Индикаторы ─────────────────────────────────────
def _ta_rsi(series: pd.Series, length=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(length).mean()
    loss  = (-delta.clip(upper=0)).rolling(length).mean()
    return 100 - 100 / (1 + gain / loss)

def calc_ssl_and_filters(df: pd.DataFrame):
    # SSL-канал 13×
    sma = df['close'].rolling(SSL_LEN).mean()
    ssl_up, ssl_dn = [], []
    for i in range(len(df)):
        if i < SSL_LEN-1:
            ssl_up.append(None); ssl_dn.append(None); continue
        hi = df['high'].iloc[i-SSL_LEN+1:i+1].max()
        lo = df['low' ].iloc[i-SSL_LEN+1:i+1].min()
        if df['close'].iloc[i] > sma.iloc[i]:
            ssl_up.append(hi); ssl_dn.append(lo)
        else:
            ssl_up.append(lo); ssl_dn.append(hi)
    df['ssl_up'], df['ssl_dn'] = ssl_up, ssl_dn
    sig = []
    for i in range(1, len(df)):
        prev_up, prev_dn = df['ssl_up'][i-1], df['ssl_dn'][i-1]
        cur_up , cur_dn  = df['ssl_up'][i  ], df['ssl_dn'][i  ]
        if pd.notna(prev_up) and pd.notna(prev_dn):
            if prev_up < prev_dn and cur_up > cur_dn:
                sig.append( 1)            # LONG
            elif prev_up > prev_dn and cur_up < cur_dn:
                sig.append(-1)            # SHORT
            else:
                sig.append(sig[-1] if sig else 0)
        else:
            sig.append(sig[-1] if sig else 0)
    sig.insert(0,0)
    df['ssl_sig'] = sig

    # Доп. индикаторы
    df['rsi']   = _ta_rsi(df['close'], RSI_LEN)
    df['atr']   = df['close'].rolling(ATR_LEN).apply(lambda x: pd.Series(x).max()-pd.Series(x).min(), raw=False)
    # DMI/ADX из ccxt нет — расчёт грубым approx: |high-low| EMA
    df['adx']   = (df['high']-df['low']).ewm(span=ADX_LEN).mean()  # surrogate
    df['vol_ok']= ~USE_VOL_FILTER | (df['volume'] > df['volume'].rolling(VOL_LEN).mean()*VOL_MULT)

    return df

# ────────────────────────── Глобальное состояние ───────────────────────────
state = {
    "monitor": False,
    "leverage": INIT_LEVERAGE,
    "position": None,
    "bars_since_close": 999,
}

# ──────────────────────── Telegram-команды ──────────────────────────────────
async def cmd_start(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.application.chat_ids.add(upd.effective_chat.id)
    state["monitor"] = True
    await upd.message.reply_text("✅ Monitoring ON")
    if not ctx.chat_data.get("task"):
        ctx.chat_data["task"] = asyncio.create_task(monitor(ctx))

async def cmd_stop(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["monitor"] = False
    await upd.message.reply_text("⛔ Monitoring OFF")

async def cmd_leverage(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = upd.message.text.split(maxsplit=1)
    if len(parts)!=2 or not parts[1].isdigit():
        await upd.message.reply_text("/leverage 3")
        return
    state["leverage"] = max(1, min(100, int(parts[1])))
    await upd.message.reply_text(f"↔ leverage set → {state['leverage']}×")

# ───────────────────────────── helpers (broadcast, balance) ─────────────────
async def broadcast(ctx, txt):
    for cid in ctx.application.chat_ids:
        try: await ctx.application.bot.send_message(cid, txt)
        except: pass

async def get_free_usdt():
    bal = await exchange.fetch_balance()
    return bal['USDT'].get('available') or bal['USDT'].get('free') or 0

# ───────────────────────── open / close позиции ─────────────────────────────
async def open_pos(side:str, price:float, ctx):
    usdt = await get_free_usdt()
    m   = exchange.market(PAIR)
    step= m['precision']['amount'] or 0.0001
    min_amt = m['limits']['amount']['min'] or step
    qty_raw = (usdt*state['leverage'])/price
    qty     = math.floor(qty_raw/step)*step
    qty     = round(qty, 8)
    if qty<min_amt:
        await broadcast(ctx, f"❗ Слишком мало средств ({qty:.4f} < {min_amt})")
        return
    await exchange.set_leverage(state['leverage'], PAIR)
    order = await exchange.create_market_order(PAIR, 'buy' if side=='LONG' else 'sell', qty)
    entry = order['average'] or price
    tp = entry*(1+TP1_ATR_MUL*TRAIL_PCT) if side=='LONG' else entry*(1-TP1_ATR_MUL*TRAIL_PCT)
    sl = entry*(1-TRAIL_PCT) if side=='LONG' else entry*(1+TRAIL_PCT)
    state['position']={"side":side,"amount":qty,"entry":entry,"tp":tp,"sl":sl,"deposit":usdt,"opened":time.time()}
    await broadcast(ctx, f"🟢 Открыта {side} qty={qty} entry={entry:.2f}")
    state['bars_since_close']=0

async def close_pos(reason:str, price:float, ctx):
    p=state['position'];
    if not p: return
    order=await exchange.create_market_order(PAIR,'sell' if p['side']=='LONG' else 'buy',p['amount'],params={"reduceOnly":True})
    close_price=order['average'] or price
    pnl=(close_price-p['entry'])*p['amount']*(1 if p['side']=='LONG' else -1)
    days=max((time.time()-p['opened'])/86400,1e-9)
    apr=(pnl/p['deposit'])*(365/days)*100
    await broadcast(ctx,f"🔴 Закрыта ({reason}) pnl={pnl:.2f} APR={apr:.1f}%")
    state['position']=None
    state['bars_since_close']=0

# ─────────────────────────── Main monitor loop ──────────────────────────────
async def monitor(ctx):
    log.info("monitor started")
    while True:
        if not state['monitor']:
            await asyncio.sleep(2); continue
        try:
            ohlcv=await exchange.fetch_ohlcv(PAIR,'15m',limit=150)
            df=pd.DataFrame(ohlcv,columns=['ts','open','high','low','close','volume'])
            df=calc_ssl_and_filters(df)
            price=df['close'].iloc[-1]
            row = df.iloc[-1]
            ssl_sig=r
