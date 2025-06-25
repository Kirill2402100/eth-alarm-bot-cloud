#!/usr/bin/env python3
# ============================================================================
# eth_alarm_bot.py — v11 "Sigma-AI" (25-Jun-2025)
# Интегрирована система принятия решений на основе LLM.
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
import aiohttp # <-- НОВАЯ ЗАВИСИМОСТЬ
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
# --- НОВЫЕ ПЕРЕМЕННЫЕ ДЛЯ LLM ---
LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_API_URL = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
for noisy in ("httpx", "telegram.vendor.httpx", "aiohttp.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ───────────────────────── Google Sheets (без изменений) ────────────────
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

HEADERS = ["DATE-TIME", "POSITION", "DEPOSIT", "ENTRY", "STOP LOSS", "TAKE PROFIT", "RR", "P&L (USDT)", "APR (%)", "LLM DECISION", "LLM CONFIDENCE"]
WS = _ws("AI-V11")
if WS and WS.row_values(1) != HEADERS:
    WS.clear(); WS.append_row(HEADERS)

# ──────────────────────────── OKX (без изменений) ──────────────────────
exchange = ccxt.okx({
    "apiKey": os.getenv("OKX_API_KEY"),
    "secret": os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "options": {"defaultType": "swap"},
    "enableRateLimit": True,
})
PAIR = PAIR_RAW.replace("/", "-").replace(":USDT", "").upper()
if "-SWAP" not in PAIR: PAIR += "-SWAP"

# ─── БАЗОВЫЕ ПАРАМЕТРЫ СТРАТЕГИИ (теперь используются для генерации сигнала) ───
SSL_LEN = 13
RSI_LEN = 14
RSI_LONGT = 52
RSI_SHORTT = 48

# ─────────────────────────── indicators (без изменений) ──────────────────
def _ta_rsi(series: pd.Series, length=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(length).mean()
    loss = (-delta.clip(upper=0)).rolling(length).mean()
    if loss.empty or loss.iloc[-1] == 0: return 100
    return 100 - (100 / (1 + gain / loss))

def calc_atr(df: pd.DataFrame, length=14):
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(length).mean()

def calc_ind(df: pd.DataFrame):
    df['ema_fast'] = df['close'].ewm(span=20, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=50, adjust=False).mean()
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
    df['atr'] = calc_atr(df, 14)
    return df

# ─────────────────────────── state (без изменений) ─────────────────────
state = { "monitor": False, "leverage": INIT_LEV, "position": None }

# ───────────────────────── helpers / telegram (без изменений) ──────────
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

# ============================================================================
# |                     НОВЫЙ МОДУЛЬ: LLM АНАЛИТИК                         |
# ============================================================================
LLM_PROMPT_TEMPLATE = """
Ты — профессиональный трейдер-аналитик по имени 'Сигма'. Твоя задача — проанализировать предоставленный торговый сетап в формате JSON и вернуть свой вердикт СТРОГО в формате JSON.

Не добавляй никаких лишних слов или объяснений вне JSON.

Вот твои правила анализа:
1. Оцени общую уверенность в сетапе по шкале от 0.0 до 10.0 и запиши в поле 'confidence_score'.
2. Прими финальное решение: 'APPROVE' (одобрить) или 'REJECT' (отклонить). Запиши его в поле 'decision'.
3. В поле 'reasoning' кратко опиши логику твоего решения.
4. Основываясь на текущей цене и ATR, предложи разумные уровни для 'suggested_tp' (тейк-профит) и 'suggested_sl' (стоп-лосс).

Проанализируй следующий сетап:
{trade_data}
"""

async def get_llm_decision(trade_data: dict, ctx):
    if not LLM_API_KEY:
        log.warning("LLM_API_KEY не установлен. Пропускаем анализ ИИ.")
        return None

    prompt = LLM_PROMPT_TEMPLATE.format(trade_data=json.dumps(trade_data, indent=2))
    
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "gpt-4.1" # Или другая модель, например gpt-3.5-turbo
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"} # Важно для получения JSON
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(LLM_API_URL, headers=headers, json=payload, timeout=30) as response:
                if response.status == 200:
                    result = await response.json()
                    llm_response_str = result['choices'][0]['message']['content']
                    llm_decision = json.loads(llm_response_str)
                    log.info("LLM Decision: %s", llm_decision)
                    await broadcast(ctx, f"🧠 LLM Анализ:\n<b>Решение:</b> {llm_decision.get('decision')}\n<b>Уверенность:</b> {llm_decision.get('confidence_score')}/10\n<b>Логика:</b> {llm_decision.get('reasoning')}")
                    return llm_decision
                else:
                    error_text = await response.text()
                    log.error("LLM API Error (status %s): %s", response.status, error_text)
                    await broadcast(ctx, f"❌ Ошибка API LLM: статус {response.status}")
                    return None
    except Exception as e:
        log.exception("Error during LLM API call: %s", e)
        await broadcast(ctx, f"❌ Критическая ошибка при запросе к LLM: {e}")
        return None

# ─────────────────── open / close position (ДОРАБОТАНЫ) ───────────────────
async def open_pos(side: str, price: float, llm_decision: dict, ctx):
    usdt = await get_free_usdt()
    if usdt <= 1:
        await broadcast(ctx, "❗ Недостаточно средств для открытия позиции.")
        return

    m = exchange.market(PAIR)
    step = m['precision']['amount'] or 0.0001
    qty = math.floor((usdt * state['leverage'] / price) / step) * step
    qty = round(qty, 8)
    
    if qty < (m['limits']['amount']['min'] or step):
        await broadcast(ctx, f"❗ Недостаточно средств: qty={qty} (min={m['limits']['amount']['min']})")
        return

    await exchange.set_leverage(state['leverage'], PAIR)
    params = {"tdMode": "isolated"}

    try:
        order = await exchange.create_market_order(PAIR, 'buy' if side == "LONG" else 'sell', qty, params=params)
    except Exception as e:
        log.error("Failed to create order: %s", e)
        await broadcast(ctx, f"❌ Ошибка открытия позиции: {e}")
        return

    entry = order.get('average', price)
    
    # ИСПОЛЬЗУЕМ TP/SL, предложенные LLM
    sl = llm_decision.get('suggested_sl', entry - (trade_data['volatility_atr'] * 1.5) if side == "LONG" else entry + (trade_data['volatility_atr'] * 1.5))
    tp = llm_decision.get('suggested_tp', entry + (trade_data['volatility_atr'] * 3.0) if side == "LONG" else entry - (trade_data['volatility_atr'] * 3.0))

    state['position'] = dict(side=side, amount=qty, entry=entry, sl=sl, tp=tp, deposit=usdt, opened=time.time(), llm_decision=llm_decision)
    
    await broadcast(ctx, f"✅ Открыта {side} | Qty: {qty:.5f} | Entry: {entry:.2f}\nSL: {sl:.2f} | TP: {tp:.2f}")

async def close_pos(reason: str, price: float, ctx):
    p = state.pop('position', None)
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
            rr = round(abs((p['tp'] - p['entry']) / (p['entry'] - p['sl'])), 2) if p['entry'] != p['sl'] else 0
            llm_decision_text = p.get('llm_decision', {}).get('decision', 'N/A')
            llm_confidence = p.get('llm_decision', {}).get('confidence_score', 'N/A')
            WS.append_row([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), p['side'], p['deposit'], p['entry'], p['sl'], p['tp'], rr, pnl, round(apr, 2), llm_decision_text, llm_confidence])
        except Exception as e: log.error("Failed to write to Google Sheets: %s", e)

# ─────────────────── telegram commands (без изменений) ───────────────────
async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.application.chat_ids.add(u.effective_chat.id); state["monitor"] = True
    await u.message.reply_text("✅ Monitoring ON (Strategy v11 Sigma-AI)")
    if not ctx.chat_data.get("task"): ctx.chat_data["task"] = asyncio.create_task(monitor(ctx))
async def cmd_stop(u: Update, ctx): state["monitor"] = False; await u.message.reply_text("⛔ Monitoring OFF")
async def cmd_lev(u: Update, ctx):
    try: lev = int(u.message.text.split()[1]); assert 1 <= lev <= 100; state["leverage"] = lev; await u.message.reply_text(f"Leverage → {lev}×")
    except: await u.message.reply_text("Использование: /leverage 5")

# ============================================================================
# |                       ГЛАВНЫЙ ЦИКЛ МОНИТОРИНГА (ПЕРЕРАБОТАН)             |
# ============================================================================
async def monitor(ctx):
    log.info("Monitor started with Strategy v11 Sigma-AI")
    while True:
        if not state["monitor"]: await asyncio.sleep(2); continue
        try:
            ohlcv_15m = await exchange.fetch_ohlcv(PAIR, '15m', limit=50)
            df_15m = pd.DataFrame(ohlcv_15m, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
            ind = calc_ind(df_15m).iloc[-1]
            price = ind['close']; atr = ind['atr']

            pos = state.get("position")
            # --- 1. ЛОГИКА ВЫХОДА ИЗ ПОЗИЦИИ ---
            if isinstance(pos, dict):
                hit_tp = price >= pos['tp'] if pos['side'] == "LONG" else price <= pos['tp']
                hit_sl = price <= pos['sl'] if pos['side'] == "LONG" else price >= pos['sl']
                if hit_tp: await close_pos("TP", price, ctx)
                elif hit_sl: await close_pos("SL", price, ctx)
            
            # --- 2. ЛОГИКА ПОИСКА СИГНАЛА И ВХОДА ---
            elif pos is None: # Входим только если нет открытой позиции
                sig = int(ind['ssl_sig'])
                if sig == 0: await asyncio.sleep(30); continue
                
                base_long_cond = sig == 1 and (ind['close'] > ind['ema_fast'] > ind['ema_slow']) and ind['rsi'] > RSI_LONGT
                base_short_cond = sig == -1 and (ind['close'] < ind['ema_fast'] < ind['ema_slow']) and ind['rsi'] < RSI_SHORTT

                side_to_check = None
                if base_long_cond: side_to_check = "LONG"
                elif base_short_cond: side_to_check = "SHORT"
                
                if side_to_check:
                    log.info("Base %s signal detected. Querying LLM...", side_to_check)
                    await broadcast(ctx, f"🔍 Найден базовый сигнал {side_to_check}. Отправляю на анализ в LLM...")
                    
                    # Формируем данные для LLM
                    global trade_data
                    trade_data = {
                        "asset": PAIR,
                        "timeframe": "15m",
                        "signal_type": side_to_check,
                        "current_price": price,
                        "indicators": {
                            "rsi_value": round(ind['rsi'], 2),
                            "ema_fast_value": round(ind['ema_fast'], 2),
                            "ema_slow_value": round(ind['ema_slow'], 2),
                            "ssl_signal": "Crossover Up" if side_to_check == "LONG" else "Crossover Down"
                        },
                        "volatility_atr": round(atr, 4)
                    }
                    
                    llm_decision = await get_llm_decision(trade_data, ctx)

                    # Принимаем финальное решение
                    if llm_decision and llm_decision.get('decision') == 'APPROVE' and llm_decision.get('confidence_score', 0) >= 6.0:
                        log.info("LLM approved trade. Opening %s position.", side_to_check)
                        state['position'] = {"opening": True} # Блокируем новые сигналы
                        await open_pos(side_to_check, price, llm_decision, ctx)
                    else:
                        log.info("LLM rejected trade or confidence too low.")
                        await broadcast(ctx, "🤖 LLM отклонил сигнал.")

        except ccxt.NetworkError as e: log.warning("Network error: %s", e)
        except Exception as e: log.exception("Unhandled error in monitor loop: %s", e)
        
        await asyncio.sleep(30)

# ─────────────────── entry-point (без изменений) ──────────────────────
async def shutdown_hook(app): log.info("Shutting down..."); await exchange.close()

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
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot shutdown requested by user.")
