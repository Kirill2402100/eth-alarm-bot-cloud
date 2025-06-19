# ssl_rsi_okx_bot.py — законченная версия 19 Jun 2025
import os, asyncio, json, math
from datetime import datetime, timezone
import pandas as pd, ccxt, gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ──────────────────── ENV ────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_IDS    = [int(x) for x in os.getenv("CHAT_IDS", "").split(",") if x]
PAIR        = os.getenv("PAIR", "BTC-USDT").replace("-", "/")  # OKX даёт BTC-USDT, ccxt ждёт BTC/USDT:USDT
PAIR        = PAIR if ":" in PAIR else f"{PAIR}:USDT"
LEVERAGE    = int(os.getenv("LEVERAGE", 1))
RISK_PCT    = float(os.getenv("RISK_PCT", 0.10))      # 10 % баланса на сделку

SHEET_ID    = os.getenv("SHEET_ID")

# ─────────────── Google Sheets init ───────────────
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(os.getenv("GOOGLE_CREDENTIALS")), scope)
LOGS_WS = gspread.authorize(creds).open_by_key(SHEET_ID).worksheet("LP_Logs")
HEADERS = [
    "DATE - TIME","POSITION","DEPOSIT","ENTRY","STOP LOSS","TAKE PROFIT",
    "RR","P&L (USDT)","APR (%)"
]
if LOGS_WS.row_values(1) != HEADERS:
    LOGS_WS.resize(rows=1); LOGS_WS.update('A1', [HEADERS])

# ────────────── OKX connection ──────────────
exchange = ccxt.okx({
    "apiKey":    os.getenv("OKX_API_KEY"),
    "secret":    os.getenv("OKX_SECRET"),
    "password":  os.getenv("OKX_PASSWORD"),
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})

try:
    exchange.load_markets(params={"instType": "SWAP"})
except Exception as e:
    print("[warn] load_markets failed → fallback", e)
    swap = exchange.fetch_markets(params={"instType": "SWAP"})
    exchange.markets = {m["symbol"]: m for m in swap}

exchange.set_leverage(LEVERAGE, PAIR)

# ────────────── Strategy helpers ──────────────
def calculate_ssl(df: pd.DataFrame) -> pd.DataFrame:
    sma = df['close'].rolling(13).mean()
    hlv = (df['close'] > sma).astype(int)
    ssl_up, ssl_down = [], []
    for i in range(len(df)):
        if i < 12:
            ssl_up.append(None); ssl_down.append(None); continue
        window_high = df['high'].iloc[i-12:i+1]
        window_low  = df['low'].iloc[i-12:i+1]
        if hlv.iloc[i]:
            ssl_up.append(window_high.max());  ssl_down.append(window_low.min())
        else:
            ssl_up.append(window_low.min());   ssl_down.append(window_high.max())
    df['ssl_up'], df['ssl_down'], df['ssl_sig'] = ssl_up, ssl_down, None
    for i in range(1, len(df)):
        if pd.notna(df['ssl_up'][i]) and pd.notna(df['ssl_down'][i]):
            prev, curr = df.iloc[i-1], df.iloc[i]
            if prev['ssl_up'] < prev['ssl_down'] and curr['ssl_up'] > curr['ssl_down']:
                df.at[df.index[i], 'ssl_sig'] = 'LONG'
            elif prev['ssl_up'] > prev['ssl_down'] and curr['ssl_up'] < curr['ssl_down']:
                df.at[df.index[i], 'ssl_sig'] = 'SHORT'
    return df

async def fetch_signal():
    ohlcv = exchange.fetch_ohlcv(PAIR, timeframe='15m', limit=100)
    df = pd.DataFrame(ohlcv, columns=['ts','open','high','low','close','vol'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms'); df.set_index('ts', inplace=True)
    df = calculate_ssl(df)
    sigs = df['ssl_sig'].dropna()
    if len(sigs) < 2:  # нет нового пересечения
        return None, df['close'].iloc[-1], df
    prev, curr = sigs.iloc[-2], sigs.iloc[-1]
    price = df['close'].iloc[-1]
    if prev == curr:
        return None, price, df
    return curr, price, df

# ────────────── Trade execution ──────────────
async def open_trade(direction:str, price:float):
    bal = exchange.fetch_balance()['total'].get('USDT', 0)
    quote = bal * RISK_PCT            # сколько USDT выделяем под сделку
    mkt   = exchange.market(PAIR)
    min_amt = mkt['limits']['amount']['min'] or 0
    raw_amt = (quote * LEVERAGE) / price
    prec    = mkt['precision']['amount']
    amt     = max(min_amt, round(raw_amt, prec))
    side    = 'buy' if direction == 'LONG' else 'sell'
    order   = exchange.create_order(PAIR, 'market', side, amt)
    return order, bal

async def monitor(app):
    global curr_sig, position_open
    curr_sig=None; position_open=None
    while monitoring:
        try:
            sig, price, df = await fetch_signal()
            if sig and sig!=curr_sig:
                # допфильтры
                rsi = df['close'].diff().rolling(14).mean()  # грубый RSI-close
                rsi_val = rsi.iloc[-1]
                if (sig=='LONG' and rsi_val<55) or (sig=='SHORT' and rsi_val>45):
                    await asyncio.sleep(30); continue
                # подтверждение ценой ±0.2 %
                ref_price = df['close'].iloc[-2]  # цена на баре пересечения
                if sig=='LONG' and price < ref_price*1.002:  # ждём +0.2 %
                    await asyncio.sleep(30); continue
                if sig=='SHORT' and price > ref_price*0.998:
                    await asyncio.sleep(30); continue

                curr_sig=sig
                order,bal = await open_trade(sig, price)
                for cid in app.chat_ids:
                    await app.bot.send_message(cid,
                        f"🚀 OPEN {sig}\nDep: {bal:.2f} USDT\nEntry: {price}")
        except Exception as e:
            print('[error]',e)
        await asyncio.sleep(30)

# ────────────── Telegram cmds ──────────────
monitoring=False
async def cmd_start(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global monitoring
    c.application.chat_ids.add(u.effective_chat.id)
    if not monitoring:
        monitoring=True; asyncio.create_task(monitor(c.application))
        await u.message.reply_text('Monitoring ON ✅')
    else:
        await u.message.reply_text('Уже запущен.')

async def cmd_stop(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global monitoring; monitoring=False
    await u.message.reply_text('⏹️ Monitoring OFF')

async def cmd_leverage(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global LEVERAGE
    try:
        lev=int(c.args[0]); exchange.set_leverage(lev,PAIR); LEVERAGE=lev
        await u.message.reply_text(f'Leverage set to {lev}x')
    except Exception as e:
        await u.message.reply_text(f'Error: {e}')

# ────────────── main ──────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.chat_ids = set(CHAT_IDS)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop",  cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_leverage))
    print("✅ Bot up — waiting for /start …")
    app.run_polling()
