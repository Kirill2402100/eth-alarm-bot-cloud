# scanner_engine.py
# ============================================================================
# v33.3 - FINAL
# - Восстановлена логика анти-спама для диагностических сообщений.
# - Бот готов к работе.
# ============================================================================
import asyncio
import time
import logging
from datetime import datetime, timezone

import ccxt.async_support as ccxt
from telegram.ext import Application

from trade_executor import log_trade_to_sheet, update_trade_in_sheet
from state_utils import save_state

log = logging.getLogger("bot")

# === Конфигурация сканера ==================================================
PAIR_TO_SCAN = 'BTC/USDT'
LARGE_ORDER_USD = 200000
TOP_N_ORDERS_TO_ANALYZE = 20
AGGRESSION_TIMEFRAME_SEC = 60
AGGRESSION_RATIO = 1.2
SL_BUFFER_PERCENT = 0.0005
SCAN_INTERVAL = 5
MIN_WALL_STABILITY_SEC = 60
MIN_SL_DISTANCE_PCT = 0.0008

# === Функции-помощники =====================================================
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

# === Логика сканирования ====================================================
async def scan_for_new_opportunities(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    status_code, status_message = None, None
    try:
        order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=100, params={'type': 'swap'})
        _, large_bids, large_asks, top_bids_usd, top_asks_usd = get_imbalance_and_walls(order_book)
        
        if not large_bids or not large_asks:
            status_code, status_message = "WAIT_LIQUIDITY", "Ожидание крупных ордеров в стакане..."
        else:
            now_ts = time.time()
            current_support, current_resistance = large_bids[0], large_asks[0]
            stable_walls = bot_data.get('stable_walls', {})
            if abs(stable_walls.get('support', {}).get('price', 0) - current_support['price']) > 10:
                stable_walls['support'] = {'price': current_support['price'], 'seen_since': now_ts}
            if abs(stable_walls.get('resistance', {}).get('price', 0) - current_resistance['price']) > 10:
                stable_walls['resistance'] = {'price': current_resistance['price'], 'seen_since': now_ts}
            bot_data['stable_walls'] = stable_walls

            dominant_side_is_bids = top_bids_usd > top_asks_usd
            side, stable_wall = (None, None)
            support_stability = now_ts - stable_walls.get('support', {}).get('seen_since', now_ts)
            resistance_stability = now_ts - stable_walls.get('resistance', {}).get('seen_since', now_ts)

            if dominant_side_is_bids and support_stability > MIN_WALL_STABILITY_SEC:
                side, stable_wall = "LONG", stable_walls['support']
            elif not dominant_side_is_bids and resistance_stability > MIN_WALL_STABILITY_SEC:
                side, stable_wall = "SHORT", stable_walls['resistance']
            
            if not side:
                status_code, status_message = "WAIT_STABILITY", f"Поиск стабильного уровня... S: {support_stability:.0f}с, R: {resistance_stability:.0f}с"
            else:
                now_ms, since = exchange.milliseconds(), exchange.milliseconds() - AGGRESSION_TIMEFRAME_SEC * 1000
                trades = await exchange.fetch_trades(PAIR_TO_SCAN, since=since, limit=100, params={'type': 'swap', 'until': now_ms})
                if not trades:
                    status_code, status_message = "WAIT_AGGRESSION", f"Стабильный уровень {side} ({stable_wall['price']:.2f}) найден, жду агрессию..."
                else:
                    buy_volume = sum(t['cost'] for t in trades if t['side'] == 'buy')
                    sell_volume = sum(t['cost'] for t in trades if t['side'] == 'sell')
                    aggression_side = "LONG" if buy_volume > sell_volume * AGGRESSION_RATIO else "SHORT" if sell_volume > buy_volume * AGGRESSION_RATIO else None
                    if aggression_side == side:
                        entry_price = trades[-1]['price']
                        sl_price = current_support['price'] * (1 - SL_BUFFER_PERCENT) if side == "LONG" else current_resistance['price'] * (1 + SL_BUFFER_PERCENT)
                        if abs(entry_price - sl_price) / entry_price < MIN_SL_DISTANCE_PCT:
                            status_code, status_message = "RISK_TOO_HIGH", f"Сигнал {side} отменен. Стоп-лосс слишком близко."
                        else:
                            idea = f"Торговля от стабильного уровня {side} ({stable_wall['price']:.2f})"
                            decision = {"Signal_ID": f"signal_{int(time.time() * 1000)}", "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), "Pair": PAIR_TO_SCAN, "Algorithm_Type": "Stable Level", "Strategy_Idea": idea, "Entry_Price": entry_price, "SL_Price": sl_price, "side": side, "Deposit": bot_data.get('deposit', 50), "Leverage": bot_data.get('leverage', 100), "dominance_lost_counter": 0}
                            msg = f"🔥 <b>ВХОД В СДЕЛКУ ({side})</b>\n\n<b>Тип:</b> <code>{idea}</code>\n<b>Вход:</b> <code>{entry_price:.2f}</code> | <b>SL:</b> <code>{sl_price:.2f}</code>"
                            await broadcast_func(app, msg)
                            await log_trade_to_sheet(decision)
                            bot_data['monitored_signals'].append(decision)
                            save_state(app)
                            status_code, status_message = "TRADE_OPENED", f"Сделка {side} открыта."
                    else:
                        status_code, status_message = "WAIT_AGGRESSION_MATCH", f"Стабильный уровень {side} ({stable_wall['price']:.2f}) есть, но агрессия слабая."
    except Exception as e:
        status_code = "SCANNER_ERROR"
        status_message = f"КРИТИЧЕСКАЯ ОШИБКА СКАНЕРА: {e}"
        log.error(status_message, exc_info=True)
    
    # --- ИЗМЕНЕНИЕ: Возвращаем анти-спам фильтр ---
    last_code = bot_data.get('last_debug_code', '')
    if status_code and status_code != last_code:
        bot_data['last_debug_code'] = status_code
        if bot_data.get('debug_mode_on', False):
            await broadcast_func(app, f"<code>{status_message}</code>")
    # --- КОНЕЦ ИЗМЕНЕНИЯ ---

# === Логика мониторинга =======================================================
async def monitor_active_trades(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    if not bot_data.get('monitored_signals'): return
    signal = bot_data['monitored_signals'][0]
    pair, entry_price, sl_price, side = (signal['Pair'], signal['Entry_Price'], signal['SL_Price'], signal['side'])
    try:
        order_book = await exchange.fetch_order_book(pair, limit=100, params={'type': 'swap'})
        if not (order_book.get('bids') and order_book['bids'][0] and order_book.get('asks') and order_book['asks'][0]): return
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

# === Главный цикл =============================================================
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
