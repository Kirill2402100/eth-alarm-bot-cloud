# File: scanner_engine.py (v32 - Text-only LLM)
# Changelog 17-Jul-2025 (Europe/Belgrade):
# • Радикально изменен подход к работе с LLM для повышения надежности.
# • LLM теперь возвращает только текстовый анализ, а не JSON.
# • Python-код парсит текст для получения оценки. Вся логика входа теперь основана на ATR.
# • Удалена сложная логика парсинга JSON и гибридного входа.

import asyncio
import json
import re
import time
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from trade_executor import log_trade_to_sheet, update_trade_in_sheet

# === Конфигурация Сканера и Стратегии =====================================
PAIR_TO_SCAN = 'BTC/USDT'
TIMEFRAME = '15m'
LARGE_ORDER_USD = 500000
TOP_N_ORDERS_TO_ANALYZE = 15
MIN_TOTAL_LIQUIDITY_USD = 2000000
MIN_IMBALANCE_RATIO = 3.0
MAX_PORTFOLIO_SIZE = 1
MIN_CONFIDENCE_SCORE = 6
MIN_RR_RATIO = 1.5
SL_ATR_MULTIPLIER = 2.0
LLM_COOLDOWN_SECONDS = 180
COUNTER_ORDER_RATIO = 1.25

# === ПРОМПТ ДЛЯ LLM (v7 - Text-Only) =======================================
LLM_PROMPT_MICROSTRUCTURE = """
Ты — ведущий аналитик-квант в HFT-фонде с агрессивным стилем торговли. Твоя задача — проанализировать данные из стакана и дать краткое, четкое заключение.

**ТВОЯ ЗАДАЧА:**
Проанализируй предоставленные JSON-данные о топ-15 крупнейших лимитных заявках ("плитах").

**ПРАВИЛА ОТВЕТА:**
1.  **Формат:** Верни ответ в виде ОБЫЧНОГО ТЕКСТА, не JSON.
2.  **Содержание:** Твой ответ должен быть кратким аналитическим заключением.
3.  **Оценка:** В конце своего ответа ОБЯЗАТЕЛЬНО укажи свою уверенность в виде "Оценка: X/10", где X — число от 0 до 10.

**Пример ответа (торговый сетап):**
На стороне асков видна плотная стена ликвидности, которая оказывает давление на цену. Биды практически отсутствуют, что подтверждает слабость покупателей. Высокая вероятность движения вниз от этого уровня. Оценка: 8/10

**Пример ответа (НЕ торговый сетап):**
Хотя на стороне покупателей есть перевес, ордера размазаны по стакану и не формируют четкой поддержки. Похоже на искусственное давление или спуфинг. Оценка: 2/10
"""
async def monitor_active_trades(exchange, app, broadcast_func, trade_log_ws, state, save_state_func):
    active_signals = state.get('monitored_signals')
    if not active_signals: return
    signal = active_signals[0]
    try:
        ticker = await exchange.fetch_ticker(signal['pair'])
        current_price = ticker.get('last')
        if not current_price: return
        exit_status, exit_price = None, None
        trigger_order_usd = signal.get('trigger_order_usd', 0)
        if trigger_order_usd > 0:
            order_book = await exchange.fetch_order_book(signal['pair'], limit=25)
            if signal['side'] == 'LONG':
                counter_orders = [p * a for p, a in order_book.get('asks', []) if (p * a) > (trigger_order_usd * COUNTER_ORDER_RATIO)]
                if counter_orders:
                    exit_status, exit_price = "EMERGENCY_EXIT", current_price
            elif signal['side'] == 'SHORT':
                counter_orders = [p * a for p, a in order_book.get('bids', []) if (p * a) > (trigger_order_usd * COUNTER_ORDER_RATIO)]
                if counter_orders:
                    exit_status, exit_price = "EMERGENCY_EXIT", current_price
        if not exit_status:
            entry_price, sl_price, tp_price = signal['entry_price'], signal['sl_price'], signal['tp_price']
            if signal['side'] == 'LONG':
                if current_price <= sl_price: exit_status, exit_price = "SL_HIT", sl_price
                elif current_price >= tp_price: exit_status, exit_price = "TP_HIT", tp_price
            elif signal['side'] == 'SHORT':
                if current_price >= sl_price: exit_status, exit_price = "SL_HIT", sl_price
                elif current_price <= tp_price: exit_status, exit_price = "TP_HIT", tp_price
        if exit_status:
            entry_price = signal['entry_price']
            position_size_usd, leverage = 50, 100
            price_change_percent = ((exit_price - entry_price) / entry_price) if entry_price != 0 else 0
            if signal['side'] == 'SHORT': price_change_percent = -price_change_percent
            pnl_percent = price_change_percent * leverage * 100
            pnl_usd = position_size_usd * (pnl_percent / 100)
            await update_trade_in_sheet(trade_log_ws, signal, exit_status, exit_price, pnl_usd, pnl_percent)
            emoji = "⚠️" if exit_status == "EMERGENCY_EXIT" else ("✅" if pnl_usd > 0 else "❌")
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                   f"<b>Инструмент:</b> <code>{signal['pair']}</code>\n"
                   f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent:+.2f}%)</b>")
            await broadcast_func(app, msg)
            state['monitored_signals'] = []
            save_state_func()
    except Exception as e:
        print(f"Monitor Error: {e}", exc_info=True)

async def get_entry_atr(exchange, pair):
    try:
        ohlcv = await exchange.fetch_ohlcv(pair, TIMEFRAME, limit=20)
        if not ohlcv or len(ohlcv) < 15:
            print(f"ATR Error: Not enough OHLCV data for {pair}. Got: {len(ohlcv) if ohlcv else 0}")
            return 0
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col])
        df.ta.atr(length=14, append=True)
        atr_value = df.iloc[-1]['ATR_14']
        if pd.isna(atr_value):
            print(f"ATR Error: ATR calculation resulted in NaN for {pair}.")
            return 0
        return atr_value
    except Exception as e:
        print(f"ATR Error: {e}", exc_info=True)
        return 0

async def scan_for_new_opportunities(exchange, app, ask_llm_func, broadcast_func, trade_log_ws, state, save_state_func):
    current_time = time.time()
    last_call_time = state.get('llm_cooldown', {}).get(PAIR_TO_SCAN, 0)
    if (current_time - last_call_time) < LLM_COOLDOWN_SECONDS:
        return

    try:
        order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=50)
        large_bids = sorted([{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('bids', []) if p and a and (p*a > LARGE_ORDER_USD)], key=lambda x: x['value_usd'], reverse=True)
        large_asks = sorted([{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('asks', []) if p and a and (p*a > LARGE_ORDER_USD)], key=lambda x: x['value_usd'], reverse=True)
    except Exception as e:
        print(f"Could not fetch order book for {PAIR_TO_SCAN}: {e}")
        return

    top_bids = large_bids[:TOP_N_ORDERS_TO_ANALYZE]
    top_asks = large_asks[:TOP_N_ORDERS_TO_ANALYZE]
    
    total_bids_usd = sum(b['value_usd'] for b in top_bids)
    total_asks_usd = sum(a['value_usd'] for a in top_asks)

    if (total_bids_usd + total_asks_usd) < MIN_TOTAL_LIQUIDITY_USD: return
    
    imbalance_ratio = 0
    dominant_side_is_bids = total_bids_usd > total_asks_usd
    if total_bids_usd > 0 and total_asks_usd > 0:
        imbalance_ratio = max(total_bids_usd / total_asks_usd, total_asks_usd / total_bids_usd)
    elif total_bids_usd > 0 or total_asks_usd > 0:
        imbalance_ratio = float('inf')

    if imbalance_ratio < MIN_IMBALANCE_RATIO: return

    state.setdefault('llm_cooldown', {})[PAIR_TO_SCAN] = time.time()
    save_state_func()

    dominant_side = "ПОКУПАТЕЛЕЙ" if dominant_side_is_bids else "ПРОДАВЦОВ"
    largest_order = (top_bids[0] if top_bids else None) if dominant_side_is_bids else (top_asks[0] if top_asks else None)
    direction_text = "вероятно движение ВВЕРХ" if dominant_side_is_bids else "вероятно движение ВНИЗ"

    if largest_order:
        detailed_msg = (f"🧠 **Обнаружена аномалия!**\n\n"
                        f"Сильный перевес на стороне {dominant_side} (дисбаланс {imbalance_ratio:.1f}x).\n"
                        f"Ключевой ордер: <code>${largest_order['value_usd']/1e6:.2f} млн</code> на уровне <code>{largest_order['price']}</code>.\n"
                        f"Ожидание: {direction_text}.\n\n"
                        f"Отправляю на текстовый анализ в LLM...")
    else:
        detailed_msg = f"🧠 Сканер нашел **качественную аномалию** (дисбаланс {imbalance_ratio:.1f}x). Отправляю на анализ LLM..."
    
    await broadcast_func(app, detailed_msg)

    focused_data = {PAIR_TO_SCAN: {'bids': top_bids, 'asks': top_asks}}
    prompt_data = json.dumps(focused_data, indent=2)
    # ВАЖНО: Убираем "response_format" из вызова LLM, т.к. ждем текст
    full_prompt = LLM_PROMPT_MICROSTRUCTURE + "\n\nАНАЛИЗИРУЕМЫЕ ДАННЫЕ:\n" + prompt_data
    
    llm_response_text = await ask_llm(full_prompt) # ask_llm нужно адаптировать, чтобы он не требовал JSON

    if not llm_response_text: return

    try:
        # --- НОВАЯ ЛОГИКА: Парсинг текста вместо JSON ---
        llm_reason = llm_response_text
        confidence_match = re.search(r"Оценка:\s*(\d+)/10", llm_response_text)
        
        if not confidence_match:
            await broadcast_func(app, f"⚠️ LLM вернул ответ без оценки. Ответ: <i>{llm_reason}</i>")
            return
            
        confidence = int(confidence_match.group(1))

        if confidence < MIN_CONFIDENCE_SCORE:
            await broadcast_func(app, f"🧐 <b>СИГНАЛ ОТКЛОНЕН LLM (Оценка: {confidence}/10)</b>\n\n<b>Причина:</b> <i>\"{llm_reason}\"</i>")
            return

        # --- УПРОЩЕННАЯ ЛОГИКА: Вход всегда по ATR ---
        trade_plan = {}
        ticker = await exchange.fetch_ticker(PAIR_TO_SCAN)
        current_price = ticker.get('last')
        if not current_price: return
        
        trade_plan['strategy_idea'] = "Momentum Entry (ATR)"
        entry_atr = await get_entry_atr(exchange, PAIR_TO_SCAN)
        if entry_atr == 0:
            await broadcast_func(app, "⚠️ Не удалось рассчитать ATR для Momentum-входа. Сделка пропущена.")
            return
        
        trade_plan['entry_price'] = current_price
        if dominant_side_is_bids:
            trade_plan['side'] = "LONG"
            trade_plan['sl_price'] = current_price - (entry_atr * SL_ATR_MULTIPLIER)
            trade_plan['tp_price'] = current_price + (entry_atr * SL_ATR_MULTIPLIER * MIN_RR_RATIO)
        else:
            trade_plan['side'] = "SHORT"
            trade_plan['sl_price'] = current_price + (entry_atr * SL_ATR_MULTIPLIER)
            trade_plan['tp_price'] = current_price - (entry_atr * SL_ATR_MULTIPLIER * MIN_RR_RATIO)

        decision = {
            "confidence_score": confidence,
            "reason": llm_reason,
            "algorithm_type": "Pressure", # Упрощенный тип
        }
        decision.update(trade_plan)
        decision['pair'] = PAIR_TO_SCAN
        if largest_order:
            decision['trigger_order_usd'] = largest_order['value_usd']
        
        msg = (f"<b>🔥 НОВЫЙ СИГНАЛ (Оценка: {confidence}/10)</b>\n\n"
               f"<b>Тип входа:</b> {decision['strategy_idea']}\n"
               f"<b>Рассчитанный план (RR ~{MIN_RR_RATIO:.1f}:1):</b>\n"
               f"  - Вход: <code>{decision['entry_price']:.2f}</code>\n"
               f"  - SL: <code>{decision['sl_price']:.2f}</code>\n"
               f"  - TP: <code>{decision['tp_price']:.2f}</code>\n\n"
               f"<b>Обоснование LLM:</b> <i>\"{llm_reason}\"</i>")
        await broadcast_func(app, msg)

        success = await log_trade_to_sheet(trade_log_ws, decision, entry_atr, state, save_state_func)
        if success:
            await broadcast_func(app, "✅ Виртуальная сделка успешно залогирована и взята на мониторинг.")

    except Exception as e:
        print(f"Error processing new opportunity: {e}", exc_info=True)

async def scanner_main_loop(app, ask_llm_func, broadcast_func, trade_log_ws, state, save_state_func):
    print("Main Engine loop started (v32_text_only_llm).")
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}})
    while state.get("bot_on", True):
        try:
            await monitor_active_trades(exchange, app, broadcast_func, trade_log_ws, state, save_state_func)
            if len(state.get('monitored_signals', [])) < MAX_PORTFOLIO_SIZE:
                await scan_for_new_opportunities(exchange, app, ask_llm_func, broadcast_func, trade_log_ws, state, save_state_func)
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            print("Main Engine loop cancelled.")
            break
        except Exception as e:
            print(f"CRITICAL Error in Main Engine loop: {e}", exc_info=True)
            await asyncio.sleep(60)
    print("Main Engine loop stopped.")
    await exchange.close()
