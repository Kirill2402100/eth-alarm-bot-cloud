# scanner_engine.py
# ============================================================================
# v29.0 - SMART VERSION
# 1. Расчет потенциала заменен на анализ ширины канала (поддержка/сопротивление).
# 2. Параметры дисбаланса и мин. прибыли теперь адаптивно меняются в зависимости
#    от волатильности рынка (ширины канала).
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
# ОБЩИЕ ПАРАМЕТРЫ
PAIR_TO_SCAN = 'BTC/USDT'
LARGE_ORDER_USD = 150000             # Размер крупного ордера для анализа
TOP_N_ORDERS_TO_ANALYZE = 20
AGGRESSION_TIMEFRAME_SEC = 15
AGGRESSION_RATIO = 2.0
SL_BUFFER_PERCENT = 0.0005
SCAN_INTERVAL = 5

# АДАПТИВНЫЕ НАСТРОЙКИ
# Порог волатильности: если ширина канала меньше этого значения, включается режим "Флэт"
LOW_VOLATILITY_THRESHOLD = 0.0025  # 0.25%

# Настройки для РЕЖИМА "ФЛЭТ" (низкая волатильность)
FLAT_MARKET_MIN_IMBALANCE = 1.8
FLAT_MARKET_MIN_PROFIT_PCT = 0.0012 # 0.12%

# Настройки для РЕЖИМА "ТРЕНД" (нормальная/высокая волатильность)
TREND_MARKET_MIN_IMBALANCE = 2.5
TREND_MARKET_MIN_PROFIT_PCT = 0.0018 # 0.18%


# === Функции-помощники (без изменений) =====================================
def get_imbalance_and_walls(order_book):
    bids, asks = order_book.get('bids', []), order_book.get('asks', [])
    if not bids or not asks: return 1.0, None, None, 0, 0
    large_bids = [{'price': p, 'value_usd': round(p*a)} for p, a in bids if p*a > LARGE_ORDER_USD]
    large_asks = [{'price': p, 'value_usd': round(p*a)} for p, a in asks if p*a > LARGE_ORDER_USD]
    if not large_bids or not large_asks: return 1.0, None, None, 0, 0
    top_bids_usd = sum(b['value_usd'] for b in large_bids[:TOP_N_ORDERS_TO_ANALYZE])
    top_asks_usd = sum(a['value_usd'] for a in large_asks[:TOP_N_ORDERS_TO_ANALYZE])
    imbalance_ratio = (max(top_bids_usd, top_asks_usd) / min(top_bids_usd, top_asks_usd)) if top_bids_usd > 0 and top_asks_usd > 0 else float('inf')
    return imbalance_ratio, large_bids, large_asks, top_bids_usd, top_asks_usd


# === Логика сканирования (полностью переписана) ============================
async def scan_for_new_opportunities(exchange, app: Application, broadcast_func):
    bot_data = app.bot_data
    status_message = ""

    try:
        # 1. Получаем базовые данные
        order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=100, params={'type': 'swap'})
        imbalance_ratio, large_bids, large_asks, top_bids_usd, top_asks_usd = get_imbalance_and_walls(order_book)

        if not large_bids or not large_asks:
            status_message = "Ожидание крупных ордеров в стакане..."
            return

        # 2. ОПРЕДЕЛЯЕМ РЕЖИМ РЫНКА (КЛЮЧЕВАЯ ЛОГИКА)
        support_wall = large_bids[0]
        resistance_wall = large_asks[0]
        zone_width_pct = (resistance_wall['price'] - support_wall['price']) / support_wall['price']

        # Выбираем набор правил в зависимости от волатильности
        if zone_width_pct < LOW_VOLATILITY_THRESHOLD:
            market_regime = "ФЛЭТ"
            min_imbalance_needed = FLAT_MARKET_MIN_IMBALANCE
            min_profit_needed = FLAT_MARKET_MIN_PROFIT_PCT
        else:
            market_regime = "ТРЕНД"
            min_imbalance_needed = TREND_MARKET_MIN_IMBALANCE
            min_profit_needed = TREND_MARKET_MIN_PROFIT_PCT
        
        # 3. Проверяем потенциал канала
        if zone_width_pct < min_profit_needed:
            status_message = f"Режим: {market_regime}. Канал слишком узкий ({zone_width_pct:.2%}), нет потенциала."
            return

        # 4. Проверяем дисбаланс
        if imbalance_ratio < min_imbalance_needed:
            status_message = f"Режим: {market_regime}. Дисбаланс ({imbalance_ratio:.1f}x) ниже порога ({min_imbalance_needed}x)."
            return

        # 5. Ищем агрессию, подтверждающую дисбаланс
        dominant_side_is_bids = top_bids_usd > top_asks_usd
        
        now = exchange.milliseconds()
        since = now - AGGRESSION_TIMEFRAME_SEC * 1000
        trades = await exchange.fetch_trades(PAIR_TO_SCAN, since=since, limit=100, params={'type': 'swap', 'until': now})
        if not trades:
            status_message = f"Режим: {market_regime}. Дисбаланс ({imbalance_ratio:.1f}x) есть, жду агрессию..."
            return

        buy_volume = sum(trade['cost'] for trade in trades if trade['side'] == 'buy')
        sell_volume = sum(trade['cost'] for trade in trades if trade['side'] == 'sell')
        
        aggression_side = "LONG" if buy_volume > sell_volume * AGGRESSION_RATIO else "SHORT" if sell_volume > buy_volume * AGGRESSION_RATIO else None
        
        # Агрессия должна совпадать с дисбалансом
        if (aggression_side == "LONG" and dominant_side_is_bids) or \
           (aggression_side == "SHORT" and not dominant_side_is_bids):
            # 6. ВСЕ УСЛОВИЯ СОВПАЛИ - ВХОД
            entry_price = trades[-1]['price']
            side = aggression_side
            sl_price = support_wall['price'] * (1 - SL_BUFFER_PERCENT) if side == "LONG" else resistance_wall['price'] * (1 + SL_BUFFER_PERCENT)
            
            idea = f"Режим {market_regime}. Агрессия {side} + Дисбаланс {imbalance_ratio:.1f}x"
            decision = {
                "Signal_ID": f"signal_{int(time.time() * 1000)}", "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                "Pair": PAIR_TO_SCAN, "Algorithm_Type": f"Adaptive Imbalance", "Strategy_Idea": idea, 
                "Entry_Price": entry_price, "SL_Price": sl_price, "TP_Price": None, "side": side, 
                "Deposit": bot_data.get('deposit', 50), "Leverage": bot_data.get('leverage', 100),
                "Trigger_Order_USD": support_wall['value_usd'] if side == "LONG" else resistance_wall['value_usd']
            }
            msg = (f"🔥 <b>ВХОД В СДЕЛКУ ({side})</b>\n\n"
                   f"<b>Тип:</b> <code>{idea}</code>\n"
                   f"<b>Вход:</b> <code>{entry_price:.4f}</code> | <b>SL:</b> <code>{sl_price:.4f}</code>")
            await broadcast_func(app, msg)
            await log_trade_to_sheet(decision)
            bot_data['monitored_signals'].append(decision)
            save_state(app)
        else:
            status_message = f"Режим: {market_regime}. Дисбаланс ({imbalance_ratio:.1f}x) есть, но агрессия слабая или в другую сторону."

    except Exception as e:
        status_message = f"КРИТИЧЕСКАЯ ОШИБКА СКАНЕРА: {e}"
        log.error(status_message, exc_info=True)
    finally:
        last_message = bot_data.get('last_debug_message', '')
        if status_message and status_message != last_message:
            bot_data['last_debug_message'] = status_message
            if bot_data.get('debug_mode_on', False):
                await broadcast_func(app, f"<code>{status_message}</code>")


# === Мониторинг и главный цикл (без изменений) ==============================
async def monitor_active_trades(exchange, app: Application, broadcast_func):
    # ... (код этой функции остается без изменений) ...
    bot_data = app.bot_data
    if not bot_data.get('monitored_signals'): return
    signal = bot_data['monitored_signals'][0]
    pair, entry_price, sl_price, side = (signal['Pair'], signal['Entry_Price'], signal['SL_Price'], signal['side'])
    try:
        order_book = await exchange.fetch_order_book(pair, limit=100, params={'type': 'swap'})
        ticker = await exchange.fetch_ticker(pair, params={'type': 'swap'})
        last_price = ticker.get('last')
        if not last_price: return
        current_imbalance, _, _, top_bids_usd, top_asks_usd = get_imbalance_and_walls(order_book)
        bot_data['monitored_signals'][0]['current_imbalance_ratio'] = current_imbalance
        exit_status, exit_price, reason = None, None, None
        if (side == 'LONG' and last_price <= sl_price) or (side == 'SHORT' and last_price >= sl_price):
            exit_status, exit_price, reason = "SL_HIT", sl_price, "Аварийный стоп-лосс"
        if not exit_status:
            if current_imbalance < FLAT_MARKET_MIN_IMBALANCE or ((side == 'LONG') != (top_bids_usd > top_asks_usd)): # Используем минимальный порог для удержания
                exit_status, exit_price, reason = "IMBALANCE_LOST", last_price, f"Дисбаланс упал до {current_imbalance:.1f}x"
        if exit_status:
            pnl_percent_raw = ((exit_price - entry_price) / entry_price) * (-1 if side == 'SHORT' else 1)
            pnl_usd = signal['Deposit'] * signal['Leverage'] * pnl_percent_raw
            pnl_percent_display = pnl_percent_raw * 100 * signal['Leverage']
            await update_trade_in_sheet(signal, exit_status, exit_price, pnl_usd, pnl_percent_display, reason=reason)
            emoji = "✅" if pnl_usd > 0 else "❌"
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                   f"<b>Причина:</b> {reason}\n"
                   f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent_display:+.2f}%)</b>")
            await broadcast_func(app, msg)
            bot_data['monitored_signals'] = []
            save_state(app)
    except Exception as e:
        log.error(f"CRITICAL MONITORING ERROR: {e}")
        await broadcast_func(app, f"⚠️ <b>Критическая ошибка мониторинга!</b>\n<code>Ошибка: {e}</code>")

async def scanner_main_loop(app: Application, broadcast_func):
    bot_version = getattr(app, 'bot_version', 'N/A')
    log.info(f"Main Engine loop starting (v{bot_version})...")
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
