# =============================================================================
#  eth_alarm_bot.py — Variant B (SSL-13 + RSI + ATR-confirm, ATR/ADX/Volume filter)
#  TP-1 + адаптивный трейлинг. Реальная торговля OKX.
#  Вариант от 2025-06-21 — «sig_price»: ATR-фильтр проверяется относительно
#  цены того бара, на котором появился сигнал SSL.
# =============================================================================

import os, asyncio, json, logging, math, time
from datetime import datetime

import numpy as np
import pandas as pd
import ccxt.async_support as ccxt           # async-версия ccxt
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, Defaults, ContextTypes
)

# ──────────────────────────── Переменные окружения ──────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN")
CHAT_IDS       = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
PAIR_RAW       = os.getenv("PAIR", "BTC-USDT-SWAP")
SHEET_ID       = os.getenv("SHEET_ID")
INIT_LEVERAGE  = int(os.getenv("LEVERAGE", 1))

# ─────────────────────────────── Логирование ────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for n in ("httpx", "telegram.vendor.httpx"):
    logging.getLogger(n).setLevel(logging.WARNING)

# ─────────────────────────────── Google Sheets ──────────────────────────────
_GS_SCOPE = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
if os.getenv("GOOGLE_CREDENTIALS"):
    _creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.getenv("GOOGLE_CREDENTIALS")), _GS_SCOPE)
    _gs = gspread.authorize(_creds)
else:
    _gs = None
    log.warning("GOOGLE_CREDENTIALS not set — Sheets logging disabled.")

def _open_ws(sheet_id: str, title: str):
    if not _gs:
        return None
    ss = _gs.open_by_key(sheet_id)
    try:
        return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        return ss.add_worksheet(title, rows=1000, cols=20)

HEADERS = ["DATE-TIME", "POSITION", "DEPOSIT", "ENTRY", "STOP LOSS",
           "TAKE PROFIT", "RR", "P&L (USDT)", "APR (%)"]
WS = _open_ws(SHEET_ID, "AI") if SHEET_ID else None
if WS and WS.row_values(1) != HEADERS:
    WS.clear(); WS.append_row(HEADERS)

# ─────────────────────────────────── OKX ────────────────────────────────────
exchange = ccxt.okx({
    "apiKey"   : os.getenv("OKX_API_KEY"),
    "secret"   : os.getenv("OKX_SECRET"   ),
    "password" : os.getenv("OKX_PASSWORD" ),
    "options"  : {"defaultType": "swap"},
    "enableRateLimit": True,
})

PAIR = PAIR_RAW.replace("/", "-").replace(":USDT", "").upper()
if "-SWAP" not in PAIR:
    PAIR += "-SWAP"
log.info("Using trading pair: %s", PAIR)

# ─────────────────────── Параметры стратегии (Variant B) ────────────────────
SSL_LEN         = 13
USE_ATR_CONF    = True
PC_ATR_MUL      = 0.60
PC_LONG_PERC    = 0.40 / 100
PC_SHORT_PERC   = 0.40 / 100

RSI_LEN         = 14
RSI_LONG_T      = 55
RSI_SHORT_T     = 45

ATR_LEN         = 14
ATR_MIN_PCT     = 0.35 / 100
ADX_LEN         = 14
ADX_MIN         = 24

USE_VOL_FILTER  = True
VOL_MULT        = 1.40
VOL_LEN         = 20

USE_ATR_STOPS   = True
TP1_SHARE       = 0.20
TP1_ATR_MUL     = 1.0
TRAIL_ATR_MUL   = 0.65
TP1_PCT         = 1.0  / 100
TRAIL_PCT       = 0.60 / 100

WAIT_BARS       = 1   # минимальная пауза после закрытия позиции

# ───────────────────────── Индикаторы & фильтры ────────────────────────────
def _ta_rsi(s: pd.Series, length=14):
    delta = s.diff()
    gain  = delta.clip(lower=0).rolling(length).mean()
    loss  = (-delta.clip(upper=0)).rolling(length).mean()
    return 100 - 100 / (1 + gain / loss)

def calc_ssl_and_filters(df: pd.DataFrame):
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

    sig = [0]
    for i in range(1, len(df)):
        pu, pd = df.at[i-1,'ssl_up'], df.at[i-1,'ssl_dn']
        cu, cd = df.at[i  ,'ssl_up'], df.at[i  ,'ssl_dn']
        if not np.isnan([pu,pd,cu,cd]).any():
            if pu < pd and cu > cd:  sig.append( 1)
            elif pu > pd and cu < cd: sig.append(-1)
            else:                     sig.append(sig[-1])
        else:
            sig.append(sig[-1])
    df['ssl_sig'] = sig

    df['rsi'] = _ta_rsi(df['close'], RSI_LEN)
    df['atr'] = df['close'].rolling(ATR_LEN).apply(
        lambda x: pd.Series(x).max() - pd.Series(x).min(), raw=False)
    df['adx'] = (df['high']-df['low']).ewm(span=ADX_LEN).mean()
    df['vol_ok'] = (~USE_VOL_FILTER) | (
        df['volume'] > df['volume'].rolling(VOL_LEN).mean()*VOL_MULT )
    return df

# ──────────────────────────── Состояние ────────────────────────────────────
state = {
    "monitor"         : False,
    "leverage"        : INIT_LEVERAGE,
    "position"        : None,
    "bars_since_close": 999,
    # для логики sig_price
    "sig_side" : 0,        # +1 / -1, 0 = нет сигнала
    "sig_price": None,
}

# ───────────────────────────── Telegram ─────────────────────────────────────
async def broadcast(ctx, txt):
    for cid in ctx.application.chat_ids:
        try: await ctx.application.bot.send_message(cid, txt)
        except: pass

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

# ───────────────────────── Торговые функции ────────────────────────────────
async def get_free_usdt():
    bal = await exchange.fetch_balance()
    return bal['USDT'].get('available') or bal['USDT'].get('free') or 0

async def open_pos(side:str, price:float, ctx):
    usdt = await get_free_usdt()
    m = exchange.market(PAIR)
    step = m['precision']['amount'] or 0.0001
    min_amt = m['limits']['amount']['min'] or step
    qty = math.floor((usdt*state['leverage']/price)/step)*step
    qty = round(qty, 8)
    if qty < min_amt:
        await broadcast(ctx, f"❗ Недостаточно средств ({qty:.4f} < {min_amt})")
        return
    await exchange.set_leverage(state['leverage'], PAIR)
    order = await exchange.create_market_order(PAIR,
               'buy' if side=='LONG' else 'sell', qty)
    entry = order['average'] or price
    tp = entry*(1+TRAIL_PCT*TP1_ATR_MUL) if side=='LONG' else entry*(1-TRAIL_PCT*TP1_ATR_MUL)
    sl = entry*(1-TRAIL_PCT)             if side=='LONG' else entry*(1+TRAIL_PCT)
    state['position'] = dict(side=side, amount=qty, entry=entry,
                             tp=tp, sl=sl, deposit=usdt, opened=time.time())
    state['bars_since_close'] = 0
    await broadcast(ctx, f"🟢 Открыта {side} qty={qty} entry={entry:.2f}")

async def close_pos(reason:str, price:float, ctx):
    p = state['position']
    if not p: return
    order = await exchange.create_market_order(PAIR,
               'sell' if p['side']=='LONG' else 'buy', p['amount'],
               params={"reduceOnly": True})
    close_p = order['average'] or price
    pnl = (close_p - p['entry'])*p['amount']*(1 if p['side']=='LONG' else -1)
    days = max((time.time()-p['opened'])/86400, 1e-9)
    apr  = (pnl/p['deposit'])*(365/days)*100
    await broadcast(ctx, f"🔴 Закрыта ({reason}) pnl={pnl:.2f} APR={apr:.1f}%")
    state['position']=None
    state['bars_since_close']=0

# ───────────────────────────── Main loop ───────────────────────────────────
async def monitor(ctx):
    log.info("monitor started")
    backoff, back_max = 5, 300
    while True:
        if not state['monitor']:
            await asyncio.sleep(2); continue
        # --- fetch ----------------------------------------------------------------
        try:
            ohlcv = await exchange.fetch_ohlcv(PAIR, '15m', limit=150)
            backoff = 5
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            log.warning("fetch_ohlcv fail: %s — retry in %is", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff*2, back_max)
            continue
        df = pd.DataFrame(ohlcv, columns=['ts','open','high','low','close','volume'])
        df = calc_ssl_and_filters(df)
        row   = df.iloc[-1]
        price = row['close']

        # --- trail / tp / sl -------------------------------------------------------
        pos = state['position']
        if pos:
            hit_tp = price >= pos['tp'] if pos['side']=='LONG' else price <= pos['tp']
            hit_sl = price <= pos['sl'] if pos['side']=='LONG' else price >= pos['sl']
            if hit_tp: await close_pos("TP", price, ctx)
            elif hit_sl: await close_pos("SL", price, ctx)
        else:
            state['bars_since_close'] += 1

        # --- сигнальная логика ----------------------------------------------------
        sig = int(row['ssl_sig'])
        if sig != state['sig_side']:
            state['sig_side']  = sig
            state['sig_price'] = price
        if sig == 0:
            state['sig_side']=0; state['sig_price']=None

        if (state['position'] is None and state['sig_side'] != 0
            and state['bars_since_close'] >= WAIT_BARS and state['sig_price'] is not None):
            # статические фильтры (текущий бар)
            atr_ok = row['atr']/price > ATR_MIN_PCT
            adx_ok = row['adx'] > ADX_MIN
            vol_ok = bool(row['vol_ok'])
            rsi = row['rsi']

            side = "LONG" if state['sig_side']==1 else "SHORT"
            # динамика цены
            if state['sig_side'] == 1:
                move_ok = (price - state['sig_price']) >= (PC_ATR_MUL*row['atr']) if USE_ATR_CONF \
                          else price >= state['sig_price']*(1+PC_LONG_PERC)
                rsi_ok = rsi > RSI_LONG_T
            else:
                move_ok = (state['sig_price'] - price) >= (PC_ATR_MUL*row['atr']) if USE_ATR_CONF \
                          else price <= state['sig_price']*(1-PC_SHORT_PERC)
                rsi_ok = rsi < RSI_SHORT_T

            if vol_ok and atr_ok and adx_ok and rsi_ok and move_ok:
                await open_pos(side, price, ctx)
                state['sig_side']=0; state['sig_price']=None

        await asyncio.sleep(30)

# ───────────────────────────── Shutdown & main ─────────────────────────────
async def shutdown(app):
    log.info("Closing OKX client…"); await exchange.close()

async def main():
    defaults = Defaults(parse_mode="HTML")
    app = (ApplicationBuilder().token(BOT_TOKEN).defaults(defaults)
           .post_shutdown(shutdown).build())
    app.chat_ids = set(CHAT_IDS)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_leverage))

    async with app:
        await app.initialize()
        await exchange.load_markets()
        bal = await exchange.fetch_balance()
        log.info("USDT balance: %s", bal["total"].get("USDT", "N/A"))
        await app.start(); await app.updater.start_polling()
        log.info("Bot polling started.")
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop(); await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
