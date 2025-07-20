# scanner_engine.py
# ============================================================================
# v28.0 - СТАБИЛЬНАЯ ВЕРСИЯ
# Логика сканера и мониторинга теперь работает с `app.bot_data`.
# ============================================================================
import asyncio
import time
import logging
from datetime import datetime, timezone

import ccxt.async_support as ccxt
from telegram.ext import Application

# Локальные импорты
from trade_executor import log_trade_to_sheet, update_trade_in_sheet
from main_bot import save_state # Импортируем функцию сохранения

log = logging.getLogger("bot")

# === Конфигурация сканера ==================================================
PAIR_TO_SCAN = 'BTC/USDT'
MIN_LIQUIDITY_USD = 2000000
MIN_IMBALANCE_RATIO = 2.5
LARGE_ORDER_USD = 250000
TOP_N_ORDERS_TO_ANALYZE = 20
AGGRESSION_TIMEFRAME_SEC = 15
AGGRESSION_RATIO = 2.0
SL_BUFFER_PERCENT = 0.0005
MIN_PROFIT_TARGET_PERCENT = 0.0015
SCAN_INTERVAL = 5 # Секунды

# === Функции-помощники (без изменений) =====================================
def get_imbalance_and_walls(order_book):
    bids, asks = order_book.get('bids', []), order_book.get('asks', [])
    if not bids or not asks: return 1.0, None, None, 0, 0

    large_bids = [{'price': p, 'value_usd': round(p*a)} for p, a in bids if p*a > LARGE_ORDER_USD]
    large_asks = [{'price': p, 'value_usd': round(p*a)} for p, a in asks if p*a > LARGE_ORDER_USD]

    top_bids_usd = sum(b['value_usd'] for b in large_bids[:TOP_N_ORDERS_TO_ANALYZE])
    top_asks_usd = sum(a['value_usd'] for a in large_asks[:TOP_N_ORDERS_TO_ANALYZE])

    if (top_bids_usd + top_asks_usd) < MIN_LIQUIDITY_USD:
        return 1.0, None, None, top_bids_usd, top_asks_usd

    imbalance_ratio = (max(top_bids_usd, top_asks_usd) / min(top_bids_usd, top_asks_usd)) if top_bids_usd > 0 and top_asks_usd > 0 else float('inf')

    return imbalance_ratio, large_bids, large_asks, top_bids_usd, top_asks_usd

# === Логика мониторинга и сканирования (Новая архитектура) ================
async def monitor_active_trades(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    if not bot_data.get('monitored_signals'): return
    signal = bot_data['monitored_signals'][0]

    pair, entry_price, sl_price, side = (
        signal.get('Pair'), signal.get('Entry_Price'),
        signal.get('SL_Price'), signal.get('side')
    )

    try:
        params = {'type': 'swap'}
        order_book = await exchange.fetch_order_book(pair, limit=100, params=params)
        ticker = await exchange.fetch_ticker(pair, params=params)
        last_price = ticker.get('last')
        if not last_price: return

        current_imbalance, _, _, top_bids_usd, top_asks_usd = get_imbalance_and_walls(order_book)
        bot_data['monitored_signals'][0]['current_imbalance_ratio'] = current_imbalance

        exit_status, exit_price, reason = None, None, None

        if (side == 'LONG' and last_price <= sl_price) or \
           (side == 'SHORT' and last_price >= sl_price):
            exit_status, exit_price, reason = "SL_HIT", sl_price, "Аварийный стоп-лосс"

        if not exit_status:
            side_is_long = side == 'LONG'
            dominant_side_is_bids = top_bids_usd > top_asks_usd
            if current_imbalance < MIN_IMBALANCE_RATIO or (side_is_long != dominant_side_is_bids):
                exit_status, exit_price = "IMBALANCE_LOST", last_price
                reason = f"Дисбаланс упал до {current_imbalance:.1f}x"

        if exit_status:
            leverage = signal.get('Leverage', 100)
            deposit = signal.get('Deposit', 50)
            pnl_percent_raw = ((exit_price - entry_price) / entry_price) * (-1 if side == 'SHORT' else 1)
            pnl_usd = deposit * leverage * pnl_percent_raw
            pnl_percent_display = pnl_percent_raw * 100 * leverage

            await update_trade_in_sheet(signal, exit_status, exit_price, pnl_usd, pnl_percent_display, reason=reason)
            emoji = "✅" if pnl_usd > 0 else "❌"
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                   f"<b>Причина:</b> {reason}\n"
                   f"<b>Инструмент:</b> <code>{pair}</code>\n"
                   f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent_display:+.2f}%)</b>")
            await broadcast_func(app, msg)
            
            # Обновляем и сохраняем состояние
            bot_data['monitored_signals'] = []
            save_state(app)

    except Exception as e:
        log.error(f"CRITICAL MONITORING ERROR: {e}")
        await broadcast_func(app, f"⚠️ <b>Критическая ошибка мониторинга!</b>\n<code>Ошибка: {e}</code>")

async def scan_for_new_opportunities(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    try:
        since = exchange.milliseconds() - AGGRESSION_TIMEFRAME_SEC * 1000
        trades = await exchange.fetch_trades(PAIR_TO_SCAN, since=since, limit=100, params={'type': 'swap'})
        if not trades: return

        buy_volume = sum(trade['cost'] for trade in trades if trade['side'] == 'buy')
        sell_volume = sum(trade['cost'] for trade in trades if trade['side'] == 'sell')

        side = "LONG" if buy_volume > sell_volume * AGGRESSION_RATIO else "SHORT" if sell_volume > buy_volume * AGGRESSION_RATIO else None
        if not side: return

        order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=100, params={'type': 'swap'})
        current_imbalance, large_bids, large_asks, top_bids_usd, top_asks_usd = get_imbalance_and_walls(order_book)
        if not large_bids or not large_asks: return

        dominant_side_is_bids = top_bids_usd > top_asks_usd
        if (side == "LONG" and not dominant_side_is_bids) or \
           (side == "SHORT" and dominant_side_is_bids) or \
           (current_imbalance < MIN_IMBALANCE_RATIO):
            return

        entry_price = trades[-1]['price']
        support_wall = large_bids[0] if side == "LONG" else large_asks[0]
        resistance_wall = large_asks[0] if side == "LONG" else large_bids[0]
        sl_price = support_wall['price'] * (1 - SL_BUFFER_PERCENT) if side == "LONG" else support_wall['price'] * (1 + SL_BUFFER_PERCENT)

        if abs(resistance_wall['price'] - entry_price) / entry_price < MIN_PROFIT_TARGET_PERCENT:
            return

        idea = f"Агрессия ${buy_volume:.0f} vs ${sell_volume:.0f}, поддержка дисбаланса {current_imbalance:.1f}x"
        decision = {
            "Signal_ID": f"signal_{int(time.time() * 1000)}", "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            "Pair": PAIR_TO_SCAN, "Algorithm_Type": "Aggression + Imbalance", "Strategy_Idea": idea,
            "Entry_Price": entry_price, "SL_Price": sl_price, "TP_Price": None,
            "side": side, "Deposit": bot_data.get('deposit', 50), "Leverage": bot_data.get('leverage', 100),
            "Trigger_Order_USD": support_wall['value_usd']
        }

        msg = (f"🔥 <b>ВХОД В СДЕЛКУ (Агрессия + Дисбаланс)</b>\n\n"
               f"<b>Идея:</b> <code>{idea}</code>\n"
               f"<b>Депозит:</b> ${decision['Deposit']} | <b>Плечо:</b> x{decision['Leverage']}\n"
               f"<b>План:</b>\n"
               f" - Вход (<b>{side}</b>): <code>{entry_price:.4f}</code>\n"
               f" - Аварийный SL: <code>{sl_price:.4f}</code> (за стеной {support_wall['price']})\n"
               f" - <b>Выход:</b> при ослаблении дисбаланса (< {MIN_IMBALANCE_RATIO}x)")

        await broadcast_func(app, msg)
        await log_trade_to_sheet(decision)
        
        # Обновляем и сохраняем состояние
        bot_data['monitored_signals'].append(decision)
        save_state(app)

    except Exception as e:
        log.error(f"CRITICAL SCANNER ERROR: {e}", exc_info=True)


# === Главный цикл (Новая архитектура) =====================================
async def scanner_main_loop(app: Application, broadcast_func):
    log.info(f"Main Engine loop started (v{BOT_VERSION}). Strategy: Aggression + Imbalance.")
    exchange = None
    try:
        exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
        log.info("Exchange connection established.")

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
                log.critical(f"CRITICAL Error in Main Engine loop iteration: {e}", exc_info=True)
                await broadcast_func(app, f"Критическая ошибка в главном цикле: {e}")
                await asyncio.sleep(60) # Пауза перед следующей попыткой
    
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
