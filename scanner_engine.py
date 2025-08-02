# -*- coding: utf-8 -*-
"""
Swing-Trading Bot (MEXC Perpetuals, 1-hour)
Version: 2025-08-02 — Production Ready (v2.0)
"""

import asyncio
import time
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from telegram.ext import Application

from trade_executor import log_open_trade, update_closed_trade, log_diagnostic_entry

log = logging.getLogger("swing_bot_engine")

# ===========================================================================
# CONFIGURATION
# ===========================================================================
class CONFIG:
    TIMEFRAME = "60m"
    POSITION_SIZE_USDT = 10.0
    LEVERAGE = 20
    MAX_CONCURRENT_POSITIONS = 10
    MIN_DAILY_VOLATILITY_PCT = 3.0
    EMA_FAST_PERIOD = 9
    EMA_SLOW_PERIOD = 21
    EMA_TREND_PERIOD = 200
    STOCH_RSI_PERIOD = 14
    STOCH_RSI_K = 3
    STOCH_RSI_D = 3
    # ИСПРАВЛЕНО: Пороги для шкалы 0-100
    STOCH_RSI_OVERBOUGHT = 80
    STOCH_RSI_OVERSOLD = 20
    STOP_LOSS_PCT = 1.0
    TAKE_PROFIT_PCT = 3.0
    SCANNER_INTERVAL_SECONDS = 600
    TICK_MONITOR_INTERVAL_SECONDS = 5
    # ИСПРАВЛЕНО: Увеличен лимит для OHLCV
    OHLCV_LIMIT = 250

# ===========================================================================
# RISK MANAGEMENT
# ===========================================================================
def calculate_sl_tp(entry_price: float, side: str) -> tuple[float, float]:
    """Рассчитывает уровни Stop Loss и Take Profit."""
    if side == "LONG":
        sl_price = entry_price * (1 - CONFIG.STOP_LOSS_PCT / 100)
        tp_price = entry_price * (1 + CONFIG.TAKE_PROFIT_PCT / 100)
    else:  # SHORT
        sl_price = entry_price * (1 + CONFIG.STOP_LOSS_PCT / 100)
        tp_price = entry_price * (1 - CONFIG.TAKE_PROFIT_PCT / 100)
    return sl_price, tp_price

# ===========================================================================
# MARKET SCANNER
# ===========================================================================
async def filter_volatile_pairs(exchange: ccxt.Exchange) -> List[str]:
    """Отфильтровывает пары, используя ОДИН эффективный вызов fetch_tickers."""
    log.info("Filtering pairs by daily volatility using fetch_tickers()...")
    try:
        await exchange.load_markets()
        tickers = await exchange.fetch_tickers()
        
        volatile_pairs = []
        for symbol, data in tickers.items():
            if symbol not in exchange.markets:
                continue

            market = exchange.market(symbol)
            if market.get('type') == 'swap' and market.get('quote') == 'USDT':
                # ИСПРАВЛЕНО: Резервный расчёт волатильности
                vol = data.get('percentage')
                if vol is None and data.get('open') and data.get('last') and data['open'] > 0:
                    vol = abs(data['last'] - data['open']) / data['open'] * 100
                
                if vol is not None and vol >= CONFIG.MIN_DAILY_VOLATILITY_PCT:
                    volatile_pairs.append(symbol)

        log.info(f"Found {len(volatile_pairs)} volatile pairs.")
        return volatile_pairs
    except Exception as e:
        log.error(f"Error filtering volatile pairs: {e}", exc_info=True)
        return []

def check_entry_conditions(df: pd.DataFrame) -> Tuple[Optional[str], Dict]:
    """Проверяет условия 'тройного триггера' и возвращает результат и диагностику."""
    if df.empty: return None, {}

    ema_fast = f"EMA_{CONFIG.EMA_FAST_PERIOD}"
    ema_slow = f"EMA_{CONFIG.EMA_SLOW_PERIOD}"
    ema_trend = f"EMA_{CONFIG.EMA_TREND_PERIOD}"
    stoch_k = f"STOCHRSIk_{CONFIG.STOCH_RSI_PERIOD}_{CONFIG.STOCH_RSI_K}_{CONFIG.STOCH_RSI_D}"

    if stoch_k not in df.columns:
        return None, {"Reason_For_Fail": "No StochRSI data"}
    
    last = df.iloc[-1]
    
    # --- Оцениваем LONG-сценарий ---
    long_ema_ok = last[ema_fast] > last[ema_slow]
    long_trend_ok = last['close'] > last[ema_trend]
    long_stoch_ok = last[stoch_k] < CONFIG.STOCH_RSI_OVERSOLD
    long_passed_count = sum([long_ema_ok, long_trend_ok, long_stoch_ok])

    if long_passed_count == 3:
        diagnosis = {"Side": "LONG", "EMA_State_OK": True, "Trend_OK": True, "Stoch_OK": True, "Reason_For_Fail": "ALL_OK"}
        return "LONG", diagnosis

    # --- Оцениваем SHORT-сценарий ---
    short_ema_ok = last[ema_fast] < last[ema_slow]
    short_trend_ok = last['close'] < last[ema_trend]
    short_stoch_ok = last[stoch_k] > CONFIG.STOCH_RSI_OVERBOUGHT
    short_passed_count = sum([short_ema_ok, short_trend_ok, short_stoch_ok])

    if short_passed_count == 3:
        diagnosis = {"Side": "SHORT", "EMA_State_OK": True, "Trend_OK": True, "Stoch_OK": True, "Reason_For_Fail": "ALL_OK"}
        return "SHORT", diagnosis

    # --- Проверяем, нужно ли логировать для диагностики (пройден хотя бы 1 фильтр) ---
    if long_passed_count >= 1 or short_passed_count >= 1:
        if long_passed_count >= short_passed_count:
            failed = [name for name, ok in [("Trend", long_trend_ok), ("StochRSI", long_stoch_ok), ("EMA_State", long_ema_ok)] if not ok]
            diagnosis = {"Side": "LONG", "EMA_State_OK": long_ema_ok, "Trend_OK": long_trend_ok, "Stoch_OK": long_stoch_ok, "Reason_For_Fail": ", ".join(failed)}
            return None, diagnosis
        else:
            failed = [name for name, ok in [("Trend", short_trend_ok), ("StochRSI", short_stoch_ok), ("EMA_State", short_ema_ok)] if not ok]
            diagnosis = {"Side": "SHORT", "EMA_State_OK": short_ema_ok, "Trend_OK": short_trend_ok, "Stoch_OK": short_stoch_ok, "Reason_For_Fail": ", ".join(failed)}
            return None, diagnosis

    return None, {}

async def find_trade_signals(exchange: ccxt.Exchange, app: Application) -> None:
    """Основная функция сканера: находит, диагностирует и обрабатывает сигналы."""
    bot_data = app.bot_data
    if len(bot_data.get("active_trades", [])) >= CONFIG.MAX_CONCURRENT_POSITIONS:
        log.info("Position limit reached. Skipping scan.")
        return

    volatile_pairs = await filter_volatile_pairs(exchange)
    if not volatile_pairs: return

    # ИСПРАВЛЕНО: Добавлен семафор для контроля параллельных запросов
    sem = asyncio.Semaphore(8)
    async def safe_fetch_ohlcv(symbol):
        async with sem:
            return await exchange.fetch_ohlcv(symbol, CONFIG.TIMEFRAME, limit=CONFIG.OHLCV_LIMIT)

    tasks = [safe_fetch_ohlcv(symbol) for symbol in volatile_pairs]
    log.info(f"Fetching OHLCV data for {len(volatile_pairs)} pairs concurrently...")
    ohlcv_results = await asyncio.gather(*tasks, return_exceptions=True)
    log.info("Finished fetching OHLCV data. Processing results...")

    for i, ohlcv in enumerate(ohlcv_results):
        symbol = volatile_pairs[i]
        try:
            if isinstance(ohlcv, Exception) or not ohlcv or len(ohlcv) < CONFIG.EMA_TREND_PERIOD:
                if isinstance(ohlcv, Exception):
                    log.warning(f"Could not fetch OHLCV for {symbol}: {ohlcv}")
                continue
            
            if len(bot_data.get("active_trades", [])) >= CONFIG.MAX_CONCURRENT_POSITIONS:
                log.info("Position limit reached during signal processing. Halting.")
                break

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df.ta.ema(length=CONFIG.EMA_FAST_PERIOD, append=True)
            df.ta.ema(length=CONFIG.EMA_SLOW_PERIOD, append=True)
            df.ta.ema(length=CONFIG.EMA_TREND_PERIOD, append=True)
            df.ta.stochrsi(length=CONFIG.STOCH_RSI_PERIOD, k=CONFIG.STOCH_RSI_K, d=CONFIG.STOCH_RSI_D, append=True)
            df.dropna(inplace=True)

            side, diagnosis = check_entry_conditions(df)
            
            if side:
                await open_new_trade(symbol, side, df.iloc[-1]['close'], app)
            elif diagnosis:
                diagnosis.update({ "Pair": symbol, "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')})
                await log_diagnostic_entry(diagnosis)
        except Exception as e:
            log.error(f"Error processing symbol {symbol}: {e}")

# ===========================================================================
# TRADE MANAGER
# ===========================================================================
async def open_new_trade(symbol: str, side: str, entry_price: float, app: Application):
    bot_data = app.bot_data
    sl_price, tp_price = calculate_sl_tp(entry_price, side)
    
    trade = {
        "Signal_ID": f"{symbol}_{int(time.time())}", "Pair": symbol, "Side": side,
        "Entry_Price": entry_price, "SL_Price": sl_price, "TP_Price": tp_price,
        "Status": "ACTIVE", "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    
    bot_data.setdefault("active_trades", []).append(trade)
    log.info(f"New trade signal: {trade}")
    
    broadcast = app.bot_data.get('broadcast_func')
    if broadcast:
        msg = (f"🔥 <b>НОВЫЙ СИГНАЛ ({side})</b>\n\n"
               f"<b>Пара:</b> {symbol}\n<b>Вход:</b> <code>{entry_price:.4f}</code>\n"
               f"<b>SL:</b> <code>{sl_price:.4f}</code> (1%)\n<b>TP:</b> <code>{tp_price:.4f}</code> (3%)")
        await broadcast(app, msg)
    await log_open_trade(trade)

async def monitor_active_trades(exchange: ccxt.Exchange, app: Application):
    bot_data = app.bot_data
    active_trades = bot_data.get("active_trades", [])
    if not active_trades: return

    symbols = [t['Pair'] for t in active_trades]
    try:
        tickers = await exchange.fetch_tickers(symbols)
    except Exception as e:
        log.error(f"Failed to fetch tickers for monitoring: {e}")
        return

    trades_to_close = []
    for trade in active_trades:
        if trade['Pair'] not in tickers: continue
        last_price = tickers[trade['Pair']]['last']
        exit_reason = None

        if trade['Side'] == 'LONG':
            if last_price <= trade['SL_Price']: exit_reason = "STOP_LOSS"
            elif last_price >= trade['TP_Price']: exit_reason = "TAKE_PROFIT"
        else: # SHORT
            if last_price >= trade['SL_Price']: exit_reason = "STOP_LOSS"
            elif last_price <= trade['TP_Price']: exit_reason = "TAKE_PROFIT"
        
        if exit_reason:
            trades_to_close.append((trade, exit_reason, last_price))

    if trades_to_close:
        broadcast = app.bot_data.get('broadcast_func')
        for trade, reason, exit_price in trades_to_close:
            pnl_pct = ((exit_price - trade['Entry_Price']) / trade['Entry_Price']) * (1 if trade['Side'] == "LONG" else -1)
            pnl_usd = CONFIG.POSITION_SIZE_USDT * CONFIG.LEVERAGE * pnl_pct
            pnl_display = pnl_pct * 100 * CONFIG.LEVERAGE
            
            if broadcast:
                emoji = "✅" if pnl_usd > 0 else "❌"
                msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({reason})</b>\n\n"
                       f"<b>Пара:</b> {trade['Pair']}\n<b>Результат: ${pnl_usd:+.2f} ({pnl_display:+.2f}%)</b>")
                await broadcast(app, msg)
            
            await update_closed_trade(trade['Signal_ID'], "CLOSED", exit_price, pnl_usd, pnl_display, reason)
            
        closed_ids = {t['Signal_ID'] for t, _, _ in trades_to_close}
        bot_data["active_trades"] = [t for t in active_trades if t['Signal_ID'] not in closed_ids]

# ===========================================================================
# MAIN LOOP
# ===========================================================================
async def scanner_main_loop(app: Application, broadcast):
    log.info("Swing Strategy Engine loop starting…")
    app.bot_data.setdefault("active_trades", [])
    app.bot_data['broadcast_func'] = broadcast

    exchange = ccxt.mexc({
        'options': {'defaultType': 'swap'}, 'enableRateLimit': True, 'rateLimit': 200,
    })
    
    last_scan_time = 0
    while app.bot_data.get("bot_on", False):
        try:
            current_time = time.time()
            
            if current_time - last_scan_time >= CONFIG.SCANNER_INTERVAL_SECONDS:
                log.info(f"--- Running Market Scan (every {CONFIG.SCANNER_INTERVAL_SECONDS // 60} mins) ---")
                await find_trade_signals(exchange, app)
                last_scan_time = current_time
                log.info("--- Scan Finished ---")

            await monitor_active_trades(exchange, app)
            await asyncio.sleep(CONFIG.TICK_MONITOR_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            log.info("Main loop cancelled.")
            break
        except Exception as e:
            log.error(f"Error in main loop: {e}", exc_info=True)
            await asyncio.sleep(30)
    await exchange.close()
    log.info("Swing Strategy Engine loop stopped.")
