# scanner_engine.py
# ============================================================================
# v26.5 - Исправлена критическая ошибка мониторинга открытых сделок
# ============================================================================
import asyncio
import time
from datetime import datetime, timezone

import ccxt.async_support as ccxt
from trade_executor import log_trade_to_sheet, update_trade_in_sheet

# === Конфигурация ===
PAIR_TO_SCAN = 'BTC/USDT'
MIN_LIQUIDITY_USD = 2000000
MIN_IMBALANCE_RATIO = 2.5
MAX_IMBALANCE_RATIO = 15.0
LARGE_ORDER_USD = 250000
TOP_N_ORDERS_TO_ANALYZE = 20
ABSORPTION_TIMEFRAME_SEC = 10
ABSORPTION_VOLUME_RATIO = 0.3
SL_BUFFER_PERCENT = 0.0005
TP_BUFFER_PERCENT = 0.0005
MIN_RR_RATIO = 1.0
COUNTER_WALL_RATIO = 1.5
API_TIMEOUT = 10.0
SCAN_INTERVAL = 5

async def monitor_active_trades(exchange, app, broadcast_func, state, save_state_func):
    if not state.get('monitored_signals'):
        return
    signal = state['monitored_signals'][0]
    
    pair, entry_price, sl_price, tp_price, side, trigger_order_usd, support_wall_price = (
        signal.get('Pair'), signal.get('Entry_Price'), signal.get('SL_Price'),
        signal.get('TP_Price'), signal.get('side'), signal.get('Trigger_Order_USD'),
        signal.get('support_wall_price')
    )
    
    if not all([pair, entry_price, sl_price, tp_price, side, trigger_order_usd, support_wall_price]):
        state['monitored_signals'] = []
        save_state_func()
        await broadcast_func(app, "⚠️ Ошибка в данных активной сделки, мониторинг остановлен.")
        return

    try:
        # --- ИЗМЕНЕНИЕ: Добавлен params={'type': 'swap'} во все запросы мониторинга ---
        params = {'type': 'swap'}
        ticker = await exchange.fetch_ticker(pair, params=params)
        last_price = ticker.get('last')
        if not last_price: return

        exit_status, exit_price = None, None
        emergency_reason = None
        
        if side == 'LONG':
            if last_price <= sl_price: exit_status, exit_price = "SL_HIT", sl_price
            elif last_price >= tp_price: exit_status, exit_price = "TP_HIT", tp_price
        elif side == 'SHORT':
            if last_price >= sl_price: exit_status, exit_price = "SL_HIT", sl_price
            elif last_price <= tp_price: exit_status, exit_price = "TP_HIT", tp_price

        if not exit_status:
            order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=50, params=params)
            large_bids = [{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('bids', []) if p*a > LARGE_ORDER_USD]
            large_asks = [{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('asks', []) if p*a > LARGE_ORDER_USD]

            if side == 'LONG':
                if not any(b['price'] == support_wall_price for b in large_bids):
                    emergency_reason = "Стена поддержки исчезла"
                elif large_asks and large_asks[0]['value_usd'] > trigger_order_usd * COUNTER_WALL_RATIO:
                    emergency_reason = f"Появилась контр-стена ${large_asks[0]['value_usd']/1e6:.2f}M"
            elif side == 'SHORT':
                if not any(a['price'] == support_wall_price for a in large_asks):
                    emergency_reason = "Стена сопротивления исчезла"
                elif large_bids and large_bids[0]['value_usd'] > trigger_order_usd * COUNTER_WALL_RATIO:
                    emergency_reason = f"Появилась контр-стена ${large_bids[0]['value_usd']/1e6:.2f}M"

            if emergency_reason:
                exit_status, exit_price = "EMERGENCY_EXIT", last_price
                await broadcast_func(app, f"⚠️ <b>ЭКСТРЕННЫЙ ВЫХОД!</b>\nПричина: {emergency_reason}.")
        # --- КОНЕЦ ИЗМЕНЕНИЯ ---

        if exit_status:
            leverage = signal.get('Leverage', 100)
            deposit = signal.get('Deposit', 50)
            pnl_percent_raw = ((exit_price - entry_price) / entry_price) * (-1 if side == 'SHORT' else 1)
            pnl_usd = deposit * leverage * pnl_percent_raw
            pnl_percent_display = pnl_percent_raw * 100 * leverage
            await update_trade_in_sheet(signal, exit_status, exit_price, pnl_usd, pnl_percent_display, reason=emergency_reason)
            emoji = "⚠️" if exit_status == "EMERGENCY_EXIT" else ("✅" if pnl_usd > 0 else "❌")
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                   f"<b>Инструмент:</b> <code>{pair}</code>\n"
                   f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent_display:+.2f}%)</b>")
            await broadcast_func(app, msg)
            state['monitored_signals'] = []
            save_state_func()
    except Exception as e:
        print(f"CRITICAL MONITORING ERROR: {e}")
        await broadcast_func(app, f"⚠️ <b>Критическая ошибка мониторинга!</b>\n<code>Ошибка: {e}</code>")

async def check_absorption(exchange, pair, side_to_absorb, required_volume):
    try:
        since = exchange.milliseconds() - ABSORPTION_TIMEFRAME_SEC * 1000
        trades = await exchange.fetch_trades(pair, since=since, limit=100, params={'type': 'swap'})
        if not trades: return {'absorbed': False}
        
        absorbing_side = 'buy' if side_to_absorb == 'sell' else 'sell'
        absorbed_volume = sum(trade['cost'] for trade in trades if trade['side'] == absorbing_side)
        
        if absorbed_volume >= required_volume:
            return {'absorbed': True, 'volume': absorbed_volume, 'entry_price': trades[-1]['price']}
        return {'absorbed': False}
    except Exception as e:
        print(f"Absorption check error: {e}")
        return {'absorbed': False}

async def scan_for_new_opportunities(exchange, app, broadcast_func, state, save_state_func):
    try:
        order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=100, params={'type': 'swap'})
        
        bids, asks = order_book.get('bids', []), order_book.get('asks', [])
        if not bids or not asks: return

        large_bids = [{'price': p, 'value_usd': round(p*a)} for p, a in bids if p and a and (p*a > LARGE_ORDER_USD)]
        large_asks = [{'price': p, 'value_usd': round(p*a)} for p, a in asks if p and a and (p*a > LARGE_ORDER_USD)]
        if not large_bids or not large_asks: return

        top_bids_usd = sum(b['value_usd'] for b in large_bids[:TOP_N_ORDERS_TO_ANALYZE])
        top_asks_usd = sum(a['value_usd'] for a in large_asks[:TOP_N_ORDERS_TO_ANALYZE])

        if (top_bids_usd + top_asks_usd) < MIN_LIQUIDITY_USD: return

        imbalance_ratio = (max(top_bids_usd, top_asks_usd) / min(top_bids_usd, top_asks_usd)) if top_bids_usd > 0 and top_asks_usd > 0 else float('inf')

        if not (MIN_IMBALANCE_RATIO <= imbalance_ratio <= MAX_IMBALANCE_RATIO):
            state['last_status_info'] = f"Поиск | Дисбаланс {imbalance_ratio:.1f}x (вне коридора)"
            return

        dominant_side_is_bids = top_bids_usd > top_asks_usd
        side = "LONG" if dominant_side_is_bids else "SHORT"
        
        status_msg = f"Обнаружен дисбаланс {imbalance_ratio:.1f}x в пользу {side}. Ожидание поглощения..."
        state['last_status_info'] = status_msg
        
        if state.get('last_signal_broadcast') != status_msg:
            await broadcast(app, f"🗣️ {status_msg}")
            state['last_signal_broadcast'] = status_msg

        if dominant_side_is_bids:
            support_wall = large_bids[0]
            resistance_wall = large_asks[0]
            side_to_absorb = 'sell'
            target_order_to_absorb = asks[0]
        else:
            support_wall = large_asks[0]
            resistance_wall = large_bids[0]
            side_to_absorb = 'buy'
            target_order_to_absorb = bids[0]
        
        required_volume = (target_order_to_absorb[0] * target_order_to_absorb[1]) * ABSORPTION_VOLUME_RATIO
        absorption_result = await check_absorption(exchange, PAIR_TO_SCAN, side_to_absorb, required_volume)

        if not absorption_result.get('absorbed'):
            return

        state['last_signal_broadcast'] = None
        
        entry_price = absorption_result['entry_price']
        
        if side == "LONG":
            sl_price = support_wall['price'] * (1 - SL_BUFFER_PERCENT)
            tp_price = resistance_wall['price'] * (1 - TP_BUFFER_PERCENT)
        else: # SHORT
            sl_price = support_wall['price'] * (1 + SL_BUFFER_PERCENT)
            tp_price = resistance_wall['price'] * (1 + TP_BUFFER_PERCENT)
        
        if (side == "LONG" and entry_price >= tp_price) or \
           (side == "SHORT" and entry_price <= tp_price):
            await broadcast(app, f"⚠️ Сделка {side} отменена: цена входа слишком близко к TP.")
            return
        
        rr_ratio = abs(tp_price - entry_price) / abs(sl_price - entry_price) if abs(sl_price - entry_price) > 0 else 0
        if rr_ratio < MIN_RR_RATIO:
            await broadcast(app, f"⚠️ Сделка {side} отменена: низкий RR (~{rr_ratio:.1f}:1). Риск выше потенциальной прибыли.")
            return

        idea = f"Дисбаланс {imbalance_ratio:.1f}x, поглощение ${absorption_result.get('volume'):.0f} за {ABSORPTION_TIMEFRAME_SEC}с"
        
        decision = {
            "Signal_ID": f"signal_{int(time.time() * 1000)}",
            "Timestamp_UTC": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            "Pair": PAIR_TO_SCAN,
            "Algorithm_Type": "Liquidity Absorption",
            "Strategy_Idea": idea,
            "Entry_Price": entry_price,
            "SL_Price": sl_price,
            "TP_Price": tp_price,
            "side": side,
            "Deposit": state.get('deposit', 50),
            "Leverage": state.get('leverage', 100),
            "Trigger_Order_USD": support_wall['value_usd'],
            "support_wall_price": support_wall['price']
        }
        
        msg = (f"<b>ВХОД В СДЕЛКУ (Поглощение Ликвидности)</b>\n\n"
               f"<b>Идея:</b> <code>{idea}</code>\n"
               f"<b>Депозит:</b> ${decision['Deposit']} | <b>Плечо:</b> x{decision['Leverage']}\n"
               f"<b>Рассчитанный план (RR ~{rr_ratio:.1f}:1):</b>\n"
               f" - Вход (<b>{side}</b>): <code>{entry_price:.4f}</code>\n"
               f" - SL: <code>{sl_price:.4f}</code> (за стеной {support_wall['price']})\n"
               f" - TP: <code>{tp_price:.4f}</code> (перед стеной {resistance_wall['price']})")
        
        await broadcast(app, msg)
        state['monitored_signals'].append(decision)
        save_state_func()
        
        if await log_trade_to_sheet(decision):
            await broadcast_func(app, "✅ ...успешно залогирована в Google Sheets.")
        else:
            await broadcast_func(app, "⚠️ Не удалось сохранить сделку в Google Sheets.")

    except Exception as e:
        print(f"CRITICAL SCANNER ERROR: {e}", exc_info=True)
        state['last_status_info'] = f"Ошибка сканера: {e}"

async def scanner_main_loop(app, broadcast_func, state, save_state_func):
    bot_version = "26.5"
    app.bot_version = bot_version
    print(f"Main Engine loop started (v{bot_version}). Strategy: Liquidity Absorption.")
    
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    
    while state.get("bot_on", True):
        try:
            if not state.get('monitored_signals'):
                await scan_for_new_opportunities(exchange, app, broadcast_func, state, save_state_func)
            else:
                await monitor_active_trades(exchange, app, broadcast_func, state, save_state_func)
            
            await asyncio.sleep(SCAN_INTERVAL)
        except asyncio.CancelledError:
            print("Main Engine loop cancelled.")
            break
        except Exception as e:
            print(f"CRITICAL Error in Main Engine loop: {e}", exc_info=True)
            await broadcast_func(app, f"Критическая ошибка в главном цикле: {e}")
            await asyncio.sleep(60)
            
    print("Main Engine loop stopped.")
    await exchange.close()
