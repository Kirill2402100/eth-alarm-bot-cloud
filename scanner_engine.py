# scanner_engine.py
# ============================================================================
# v31.2 - FINAL RISK MANAGEMENT
# - Добавлен финальный фильтр: проверка минимального расстояния до стоп-лосса.
# - Бот больше не будет входить в сделки со слишком близким стопом,
#   что повышает качество сигналов и устойчивость к шуму.
# ============================================================================
import asyncio
import time
import logging
from datetime import datetime, timezone

import ccxt.async_support as ccxt
from telegram.ext import Application

# Локальные импорты
from trade_executor import log_trade_to_sheet, update_trade_in_sheet
from state_utils import save_state

log = logging.getLogger("bot")

# === Конфигурация сканера ==================================================
PAIR_TO_SCAN = 'BTC/USDT'
LARGE_ORDER_USD = 200000
TOP_N_ORDERS_TO_ANALYZE = 20
AGGRESSION_TIMEFRAME_SEC = 45
AGGRESSION_RATIO = 2.0
SL_BUFFER_PERCENT = 0.0005
SCAN_INTERVAL = 5

# АДАПТИВНЫЕ НАСТРОЙКИ
LOW_VOLATILITY_THRESHOLD = 0.0025
FLAT_MARKET_MIN_IMBALANCE = 1.8
TREND_MARKET_MIN_IMBALANCE = 2.5

# НОВЫЙ ПАРАМЕТР РИСК-МЕНЕДЖМЕНТА
MIN_SL_DISTANCE_PCT = 0.0008  # 0.08% - минимальное расстояние от входа до стопа


# === Функции-помощники (без изменений) =====================================
def get_imbalance_and_walls(order_book):
    bids, asks = order_book.get('bids', []), order_book.get('asks', [])
    if not bids or not asks: return 1.0, None, None, 0, 0
    large_bids, large_asks = [], []
    for bid in bids:
        if len(bid) == 2:
            price, amount = bid
            if price * amount > LARGE_ORDER_USD:
                large_bids.append({'price': price, 'value_usd': round(price * amount)})
    for ask in asks:
        if len(ask) == 2:
            price, amount = ask
            if price * amount > LARGE_ORDER_USD:
                large_asks.append({'price': price, 'value_usd': round(price * amount)})
    if not large_bids or not large_asks: return 1.0, None, None, 0, 0
    top_bids_usd = sum(b['value_usd'] for b in large_bids[:TOP_N_ORDERS_TO_ANALYZE])
    top_asks_usd = sum(a['value_usd'] for a in large_asks[:TOP_N_ORDERS_TO_ANALYZE])
    imbalance_ratio = (max(top_bids_usd, top_asks_usd) / min(top_bids_usd, top_asks_usd)) if top_bids_usd > 0 and top_asks_usd > 0 else float('inf')
    return imbalance_ratio, large_bids, large_asks, top_bids_usd, top_asks_usd

# === Логика сканирования (ИЗМЕНЕНА) ========================================
async def scan_for_new_opportunities(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    status_code, status_message = None, None
    try:
        order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=100, params={'type': 'swap'})
        imbalance_ratio, large_bids, large_asks, top_bids_usd, top_asks_usd = get_imbalance_and_walls(order_book)
        if not large_bids or not large_asks:
            status_code, status_message = "WAIT_LIQUIDITY", "Ожидание крупных ордеров в стакане..."
        else:
            support_wall, resistance_wall = large_bids[0], large_asks[0]
            zone_width_pct = (resistance_wall['price'] - support_wall['price']) / support_wall['price']
            market_regime = "ФЛЭТ" if zone_width_pct < LOW_VOLATILITY_THRESHOLD else "ТРЕНД"
            min_imbalance_needed = FLAT_MARKET_MIN_IMBALANCE if market_regime == "ФЛЭТ" else TREND_MARKET_MIN_IMBALANCE
            if imbalance_ratio < min_imbalance_needed:
                status_code, status_message = "WAIT_IMBALANCE", f"Режим: {market_regime}. Дисбаланс ({imbalance_ratio:.1f}x) ниже порога ({min_imbalance_needed}x)."
            else:
                dominant_side_is_bids = top_bids_usd > top_asks_usd
                now, since = exchange.milliseconds(), exchange.milliseconds() - AGGRESSION_TIMEFRAME_SEC * 1000
                trades = await exchange.fetch_trades(PAIR_TO_SCAN, since=since, limit=100, params={'type': 'swap', 'until': now})
                if not trades:
                    status_code, status_message = "WAIT_AGGRESSION", f"Режим: {market_regime}. Дисбаланс ({imbalance_ratio:.1f}x) есть, жду агрессию..."
                else:
                    buy_volume, sell_volume = sum(t['cost'] for t in trades if t['side'] == 'buy'), sum(t['cost'] for t in trades if t['side'] == 'sell')
                    aggression_side = "LONG" if buy_volume > sell_volume * AGGRESSION_RATIO else "SHORT" if sell_volume > buy_volume * AGGRESSION_RATIO else None
                    if (aggression_side == "LONG" and dominant_side_is_bids) or (aggression_side == "SHORT" and not dominant_side_is_bids):
                        entry_price = trades[-1]['price']
                        side = aggression_side
                        sl_price = support_wall['price'] * (1 - SL_BUFFER_PERCENT) if side == "LONG" else resistance_wall['price'] * (1 + SL_BUFFER_PERCENT)
                        
                        # --- КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: Проверка расстояния до стопа ---
                        sl_distance_pct = abs(entry_price - sl_price) / entry_price
                        if sl_distance_pct < MIN_SL_DISTANCE_PCT:
                            status_code, status_message = "RISK_TOO_HIGH", f"Сигнал {side} отменен. Стоп-лосс ({sl_distance_pct:.2%}) слишком близко."
                            return
                        # --- КОНЕЦ ИЗМЕНЕНИЯ ---

                        idea = f"Режим {market_regime}. Агрессия {side} + Дисбаланс {imbalance_ratio:.1f}x"
                        decision = { "Signal_ID": f"signal_{int(time.time() * 1000)}", "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), "Pair": PAIR_TO_SCAN, "Algorithm_Type": "Adaptive Imbalance", "Strategy_Idea": idea, "Entry_Price": entry_price, "SL_Price": sl_price, "TP_Price": None, "side": side, "Deposit": bot_data.get('deposit', 50), "Leverage": bot_data.get('leverage', 100), "Trigger_Order_USD": support_wall['value_usd'] if side == "LONG" else resistance_wall['value_usd'], "dominance_lost_counter": 0 }
                        msg = f"🔥 <b>ВХОД В СДЕЛКУ ({side})</b>\n\n<b>Тип:</b> <code>{idea}</code>\n<b>Вход:</b> <code>{entry_price:.4f}</code> | <b>SL:</b> <code>{sl_price:.4f}</code>"
                        await broadcast_func(app, msg)
                        await log_trade_to_sheet(decision)
                        bot_data['monitored_signals'].append(decision)
                        save_state(app)
                        status_code, status_message = "TRADE_OPENED", f"Сделка {side} открыта."
                    else:
                        status_code, status_message = "WAIT_AGGRESSION_MATCH", f"Режим: {market_regime}. Дисбаланс ({imbalance_ratio:.1f}x) есть, но агрессия слабая или в другую сторону."
    finally:
        last_code = bot_data.get('last_debug_code', '')
        if status_code and status_code != last_code:
            bot_data['last_debug_code'] = status_code
            if bot_data.get('debug_mode_on', False):
                await broadcast_func(app, f"<code>{status_message}</code>")

# === Логика мониторинга (без изменений) ======================================
async def monitor_active_trades(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    if not bot_data.get('monitored_signals'): return
    signal = bot_data['monitored_signals'][0]
    pair, entry_price, sl_price, side = (signal['Pair'], signal['Entry_Price'], signal['SL_Price'], signal['side'])
    try:
        order_book = await exchange.fetch_order_book(pair, limit=100, params={'type': 'swap'})
        if not (order_book.get('bids') and order_book['bids'][0] and order_book.get('asks') and order_book['asks'][0]):
            log.warning(f"Order book for {pair} is incomplete, skipping monitor cycle.")
            return
        best_bid, best_ask = order_book['bids'][0][0], order_book['asks'][0][0]
        last_price = (best_bid + best_ask) / 2
        _, _, _, top_bids_usd, top_asks_usd = get_imbalance_and_walls(order_book)
        exit_status, exit_price, reason = None, None, None
        
        if (side == 'LONG' and last_price <= sl_price) or (side == 'SHORT' and last_price >= sl_price):
            exit_status, exit_price, reason = "SL_HIT", sl_price, "Аварийный стоп-лосс"
        
        if not exit_status:
            dominance_is_lost = (side == 'LONG' and top_bids_usd <= top_asks_usd) or (side == 'SHORT' and top_asks_usd <= top_bids_usd)
            if dominance_is_lost:
                signal['dominance_lost_counter'] = signal.get('dominance_lost_counter', 0) + 1
                if signal['dominance_lost_counter'] >= 2:
                    reason_text = "Потеря доминации покупателей" if side == 'LONG' else "Потеря доминации продавцов"
                    exit_status, exit_price, reason = "DOMINANCE_LOST", last_price, f"{reason_text} (подтверждено)"
            else:
                signal['dominance_lost_counter'] = 0
        
        if exit_status:
            pnl_percent_raw = ((exit_price - entry_price) / entry_price) * (-1 if side == 'SHORT' else 1)
            pnl_usd = signal['Deposit'] * signal['Leverage'] * pnl_percent_raw
            pnl_percent_display = pnl_percent_raw * 100 * signal['Leverage']
            await update_trade_in_sheet(signal, exit_status, exit_price, pnl_usd, pnl_percent_display, reason=reason)
            emoji = "✅" if pnl_usd > 0 else "❌"
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n<b>Причина:</b> {reason}\n<b>Результат: ${pnl_usd:+.2f} ({pnl_percent_display:+.2f}%)</b>")
            await broadcast_func(app, msg)
            bot_data['monitored_signals'] = []
            save_state(app)
    except Exception as e:
        log.error(f"CRITICAL MONITORING ERROR: {e}", exc_info=True)
        await broadcast_func(app, f"⚠️ <b>Критическая ошибка мониторинга!</b>\n<code>Ошибка: {e}</code>")

# === Главный цикл (без изменений) ============================================
async def scanner_main_loop(app: Application, broadcast_func):
    bot_version = getattr(app, 'bot_version', 'N/A')
    log.info(f"Main Engine loop starting (v{bot_version})...")
    exchange = None
    try:
        exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
        await exchange.load_markets()
        log.info("Exchange connection and markets loaded.")
        while app.bot_data.get("bot_on", False):
            try:
                if not app.bot_data.get('monitored_signals'):
                    await scan_for_new_opportunities(exchange, app, broadcast_func)
                else:
                    await monitor_active_trades(exchange, app, broadcast_func)
                await asyncio.sleep(SCAN_INTERVAL)
            except asyncio.CancelledError:
                log.info("Main Engine loop cancelled by command.")
                break
            except Exception as e:
                log.critical(f"CRITICAL Error in loop iteration: {e}", exc_info=True)
                await broadcast_func(app, f"Критическая ошибка в цикле: {e}")
                await asyncio.sleep(20)
    except Exception as e:
        log.critical(f"CRITICAL STARTUP ERROR in Main Engine: {e}", exc_info=True)
        await broadcast_func(app, f"<b>КРИТИЧЕСКАЯ ОШИБКА ЗАПУСКА!</b>\n<code>Ошибка: {e}</code>")
        app.bot_data['bot_on'] = False
        save_state(app)
    finally:
        if exchange:
            log.info("Closing exchange connection.")
            await exchange.close()
        log.info("Main Engine loop stopped.")
