#!/usr/bin/env python3
# ============================================================================
# eth_alarm_bot.py — v10 "Hyper-Aggressive" (24-Jun-2025)
# Исправлен баг с состоянием, добавлена новая логика выхода
# ============================================================================

import os
import asyncio
import json
import logging
import math
import time
from datetime import datetime

import numpy as np
import pandas as pd
import ccxt.async_support as ccxt
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (ApplicationBuilder, CommandHandler,
                          Defaults, ContextTypes)

# ─────────────────────────── env / logging ────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
PAIR_RAW = os.getenv("PAIR", "BTC-USDT-SWAP")
SHEET_ID = os.getenv("SHEET_ID")
INIT_LEV = int(os.getenv("LEVERAGE", 4))

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
    _gs = None; log.warning("GOOGLE_CREDENTIALS not set.")

def _ws(title: str):
    if not (_gs and SHEET_ID): return None
    ss = _gs.open_by_key(SHEET_ID)
    try: return ss.worksheet(title)
    except gspread.WorksheetNotFound: return ss.add_worksheet(title, rows=1000, cols=20)

HEADERS = ["DATE-TIME", "POSITION", "DEPOSIT", "ENTRY", "STOP LOSS", "TAKE PROFIT", "RR", "P&L (USDT)", "APR (%)"]
WS = _ws("AI-V10")
if WS and WS.row_values(1) != HEADERS:
    WS.clear(); WS.append_row(HEADERS)

# ──────────────────────────── OKX ───────────────────────────────────
exchange = ccxt.okx({
    "apiKey": os.getenv("OKX_API_KEY"),
    "secret": os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "options": {"defaultType": "swap"},
    "enableRateLimit": True,
})
PAIR = PAIR_RAW.replace("/", "-").replace(":USDT", "").upper()
if "-SWAP" not in PAIR: PAIR += "-SWAP"

# ─── STRATEGY V10 "HYPER-AGGRESSIVE" PARAMETERS ─────────────────
SSL_LEN = 13
RSI_LEN = 14
RSI_LONGT = 52
RSI_SHORTT = 48
ATR_LEN = 14
ATR_CONFIRM_MUL = 0.4
ATR_MIN_PCT = 0.35 / 100
VOL_MULT = 1.0
VOL_LEN = 20
# --- Новые параметры выхода ---
TP_ATR_MUL = 2.0         # Тейк-профит на 2 ATR
INITIAL_SL_ATR_MUL = 1.0 # Начальный стоп на 1 ATR
TRAIL_STEP_ATR_MUL = 0.5 # Шаг трейлинга 0.5 ATR

# ─────────────────────────── indicators ───────────────────────────────
def _ta_rsi(series: pd.Series, length=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(length).mean()
    loss = (-delta.clip(upper=0)).rolling(length).mean()
    if loss.iloc[-1] == 0: return 100
    return 100 - 100 / (1 + gain / loss)

def calc_atr(df: pd.DataFrame, length=14):
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(length).mean()

def calc_ind(df: pd.DataFrame):
    sma = df['close'].rolling(SSL_LEN).mean()
    hi = df['high'].rolling(SSL_LEN).max()
    lo = df['low'].rolling(SSL_LEN).min()
    df['ssl_up'] = np.where(df['close'] > sma, hi, lo)
    df['ssl_dn'] = np.where(df['close'] > sma, lo, hi)
    ssl_cross_up = (df['ssl_up'].shift(1) < df['ssl_dn'].shift(1)) & (df['ssl_up'] > df['ssl_dn'])
    ssl_cross_down = (df['ssl_up'].shift(1) > df['ssl_dn'].shift(1)) & (df['ssl_up'] < df['ssl_dn'])
    signal = pd.Series(np.nan, index=df.index)
    signal.loc[ssl_cross_up] = 1
    signal.loc[ssl_cross_down] = -1
    signal = signal.ffill()
    df['ssl_sig'] = signal.fillna(0).astype(int)
    df['rsi'] = _ta_rsi(df['close'], RSI_LEN)
    df['atr'] = calc_atr(df, ATR_LEN)
    df['vol_ok'] = (df['volume'] > df['volume'].rolling(VOL_LEN).mean() * VOL_MULT)
    return df

# ─────────────────────────── state ──────────────────────────────────
state = { "monitor": False, "leverage": INIT_LEV, "position": None }

# ───────────────────────── helpers / telegram ─────────────────────────
async def broadcast(ctx, txt):
    for cid in ctx.application.chat_ids:
        try: await ctx.application.bot.send_message(cid, txt)
        except Exception as e: log.warning("Broadcast failed to %s: %s", cid, e)

async def get_free_usdt():
    try:
        bal = await exchange.fetch_balance()
        return bal['USDT'].get('available') or bal['USDT'].get('free') or 0
    except Exception as e:
        log.error("Could not fetch balance: %s", e)
        return 0

# ─────────────────────── open / close position ────────────────────────
async def open_pos(side: str, price: float, atr: float, ctx):
    usdt = await get_free_usdt()
    if usdt <= 1:
        await broadcast(ctx, "❗ Недостаточно средств для открытия позиции.")
        state['position'] = None # Сбрасываем блокировку, если не удалось открыть
        return
        
    m = exchange.market(PAIR)
    step = m['precision']['amount'] or 0.0001
    qty = math.floor((usdt * state['leverage'] / price) / step) * step
    qty = round(qty, 8)
    
    if qty < (m['limits']['amount']['min'] or step):
        await broadcast(ctx, f"❗ Недостаточно средств: qty={qty} (min={m['limits']['amount']['min']})")
        state['position'] = None
        return

    await exchange.set_leverage(state['leverage'], PAIR)
    params = {"tdMode": "isolated"}

    try:
        order = await exchange.create_market_order(PAIR, 'buy' if side == "LONG" else 'sell', qty, params=params)
    except Exception as e:
        log.error("Failed to create order: %s", e)
        await broadcast(ctx, f"❌ Ошибка открытия позиции: {e}")
        state['position'] = None # Сбрасываем блокировку при ошибке
        return

    entry = order.get('average', price)
    initial_sl = entry - (atr * INITIAL_SL_ATR_MUL) if side == "LONG" else entry + (atr * INITIAL_SL_ATR_MUL)
    
    # Обновляем состояние полной информацией о сделке
    state['position'] = dict(side=side, amount=qty, entry=entry, sl=initial_sl, atr_at_entry=atr, deposit=usdt, opened=time.time())
    
    await broadcast(ctx, f"✅ Открыта {side} | Qty: {qty:.5f} | Entry: {entry:.2f}")

async def close_pos(reason: str, price: float, ctx):
    p = state.pop('position', None) # Используем pop для атомарного получения и удаления
    if not p: return
    
    params = {"tdMode": "isolated", "reduceOnly": True}
    try:
        order = await exchange.create_market_order(PAIR, 'sell' if p['side'] == "LONG" else 'buy', p['amount'], params=params)
        close_price = order.get('average', price)
    except Exception as e:
        log.error("close_pos order error: %s", e)
        close_price = price

    pnl = (close_price - p['entry']) * p['amount'] * (1 if p['side'] == "LONG" else -1)
    days = max((time.time() - p['opened']) / 86400, 1e-9)
    apr = (pnl / p['deposit']) * (365 / days) * 100
    await broadcast(ctx, f"⛔ Закрыта ({reason}) | P&L: {pnl:.2f} USDT | APR: {apr:.1f}%")

    if WS:
        try:
            tp = p['entry'] + (p['atr_at_entry'] * TP_ATR_MUL) if p['side'] == "LONG" else p['entry'] - (p['atr_at_entry'] * TP_ATR_MUL)
            sl = p['entry'] - (p['atr_at_entry'] * INITIAL_SL_ATR_MUL) if p['side'] == "LONG" else p['entry'] + (p['atr_at_entry'] * INITIAL_SL_ATR_MUL)
            rr = round(abs((tp - p['entry']) / (p['entry'] - sl)), 2)
            WS.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), p['side'], p['deposit'], p['entry'], sl, tp, rr, pnl, round(apr, 2)])
        except Exception as e: log.error("Failed to write to Google Sheets: %s", e)

# ──────────────────────── telegram commands ───────────────────────────
async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.application.chat_ids.add(u.effective_chat.id); state["monitor"] = True
    await u.message.reply_text("✅ Monitoring ON (Strategy v10 Hyper-Aggressive)")
    if not ctx.chat_data.get("task"): ctx.chat_data["task"] = asyncio.create_task(monitor(ctx))
async def cmd_stop(u: Update, ctx): state["monitor"] = False; await u.message.reply_text("⛔ Monitoring OFF")
async def cmd_lev(u: Update, ctx):
    try: lev = int(u.message.text.split()[1]); assert 1 <= lev <= 100; state["leverage"] = lev; await u.message.reply_text(f"Leverage → {lev}×")
    except: await u.message.reply_text("Использование: /leverage 5")

# ───────────────────────── MAIN MONITOR LOOP ──────────────────────────
async def monitor(ctx):
    log.info("Monitor started with Strategy v10")
    while True:
        if not state["monitor"]: await asyncio.sleep(2); continue
        try:
            ohlcv_15m = await exchange.fetch_ohlcv(PAIR, '15m', limit=150)
            ohlcv_1h = await exchange.fetch_ohlcv(PAIR, '1h', limit=201)
            df_15m = pd.DataFrame(ohlcv_15m, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
            df_1h = pd.DataFrame(ohlcv_1h, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
            ind = calc_ind(df_15m).iloc[-1]
            h1_sma200 = df_1h['close'].rolling(200).mean().iloc[-1]
            price = ind['close']; atr = ind['atr']

            pos = state.get("position")
            # --- 3. ЛОГИКА ВЫХОДА ИЗ ПОЗИЦИИ ---
            if isinstance(pos, dict): # Проверяем, что позиция полностью определена
                # 3a. Обновление трейлинг-стопа
                new_sl = 0
                if pos['side'] == "LONG": new_sl = max(pos['sl'], price - (atr * TRAIL_STEP_ATR_MUL))
                else: new_sl = min(pos['sl'], price + (atr * TRAIL_STEP_ATR_MUL))
                if new_sl != pos['sl']: pos['sl'] = new_sl

                # 3b. Проверка TP и SL
                tp_price = pos['entry'] + (pos['atr_at_entry'] * TP_ATR_MUL) if pos['side'] == "LONG" else pos['entry'] - (pos['atr_at_entry'] * TP_ATR_MUL)
                hit_tp = price >= tp_price if pos['side'] == "LONG" else price <= tp_price
                hit_sl = price <= pos['sl'] if pos['side'] == "LONG" else price >= pos['sl']
                if hit_tp: await close_pos("TP", price, ctx)
                elif hit_sl: await close_pos("SL", price, ctx)
            
            # --- 4. ЛОГИКА ВХОДА В ПОЗИЦИЮ ---
            elif pos is None: # Входим только если нет открытой позиции или процесса открытия
                sig = int(ind['ssl_sig']); 
                if sig == 0: await asyncio.sleep(30); continue
                atr_ok = (atr / price) > ATR_MIN_PCT if atr > 0 else False
                vol_ok = ind['vol_ok']
                
                long_pc_ok = price >= df_15m['close'].iloc[-2] + (atr * ATR_CONFIRM_MUL)
                long_rsi_ok = ind['rsi'] > RSI_LONGT
                long_trend_ok = price > h1_sma200
                longCond = sig == 1 and long_rsi_ok and long_pc_ok and atr_ok and vol_ok and long_trend_ok

                short_pc_ok = price <= df_15m['close'].iloc[-2] - (atr * ATR_CONFIRM_MUL)
                short_rsi_ok = ind['rsi'] < RSI_SHORTT
                short_trend_ok = price < h1_sma200
                shortCond = sig == -1 and short_rsi_ok and short_pc_ok and atr_ok and vol_ok and short_trend_ok
                
                if longCond:
                    log.info("LONG condition met. Opening position...")
                    await broadcast(ctx, f"Сигнал LONG: цена={price:.2f}, RSI={ind['rsi']:.1f}")
                    state['position'] = {"opening": True} # Блокируем новые сигналы
                    await open_pos("LONG", price, atr, ctx)
                elif shortCond:
                    log.info("SHORT condition met. Opening position...")
                    await broadcast(ctx, f"Сигнал SHORT: цена={price:.2f}, RSI={ind['rsi']:.1f}")
                    state['position'] = {"opening": True} # Блокируем новые сигналы
                    await open_pos("SHORT", price, atr, ctx)

        except ccxt.NetworkError as e: log.warning("Network error: %s", e)
        except Exception as e: log.exception("Unhandled error in monitor loop: %s", e)
        
        await asyncio.sleep(30)

# ───────────────────────── graceful shutdown ──────────────────────────
async def shutdown_hook(app): log.info("Shutting down..."); await exchange.close()

# ───────────────────────── entry-point ────────────────────────────────
async def main():
    app = (ApplicationBuilder().token(BOT_TOKEN).defaults(Defaults(parse_mode="HTML")).post_shutdown(shutdown_hook).build())
    app.chat_ids = set(CHAT_IDS)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_lev))
    async with app:
        try:
            await exchange.load_markets(); log.info("Markets loaded.")
            bal = await exchange.fetch_balance(); log.info("Initial USDT balance: %s", bal.get("total", {}).get("USDT"))
        except Exception as e: log.error("Failed to initialize exchange: %s", e); return
        await app.start(); await app.updater.start_polling(); log.info("Bot polling started.")
        await asyncio.Event().wait()
if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): log.info("Bot shutdown requested by user.")
