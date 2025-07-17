# File: scanner_engine.py (v29 - Robust ATR)
# Changelog 17-Jul-2025 (Europe/Belgrade):
# • Функция get_entry_atr полностью переработана для повышения надежности.
# • Добавлено детальное логирование ошибок и валидация данных при расчете ATR.
# • Устранена причина сбоя при расчете ATR для Momentum-входа.

import asyncio
import json
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
ENTRY_OFFSET_PERCENT = 0.0005
SL_OFFSET_PERCENT = 0.0010
LLM_COOLDOWN_SECONDS = 180

# === ПРОМПТ ДЛЯ LLM (v5 - Aggressive) =====================================
LLM_PROMPT_MICROSTRUCTURE = """
Ты — ведущий аналитик-квант в HFT-фонде с агрессивным стилем торговли. Твоя задача — находить возможности, а не избегать их.

**ТВОЯ ЗАДАЧА:**
Проанализируй предоставленные JSON-данные о топ-15 крупнейших лимитных заявках ("плитах"). Данные уже отфильтрованы и показывают значительный дисбаланс ликвидности.

**КЛЮЧЕВЫЕ ПРИНЦИПЫ АНАЛИЗА:**
1.  **Доверяй дисбалансу:** Сильный односторонний перевес ликвидности — это сам по себе мощный сигнал. Не считай его автоматически спуфингом только потому, что на другой стороне стакана пусто. Отсутствие сопротивления — это тоже признак слабости противоположной стороны.
2.  **Оценивай реалистичность:** Задавай себе вопрос: "Похоже ли это на реальную попытку удержать уровень или это просто одинокая, подозрительная заявка далеко от цены?".
3.  **Будь решительным:** Если видишь явную стену ликвидности, которая может служить поддержкой или сопротивлением, присваивай высокий `confidence_score`. Если четких уровней нет, но давление очевидно, все равно давай высокую оценку и объясняй это в причине.

**ФОРМАТ ОТВЕТА:**
Верни ТОЛЬКО JSON-объект.
"""
async def monitor_active_trades(exchange, app, broadcast_func, trade_log_ws, state, save_state_func):
    active_signals = state.get('monitored_signals')
    if not active_signals: return
    signal = active_signals[0]
    try:
        ticker = await exchange.fetch_ticker(signal['pair'])
        current_price = ticker.get('last')
        if not current_price: return
    except Exception as e:
        print(f"Monitor: Could not fetch ticker for {signal['pair']}. Error: {e}")
        return
    exit_status, exit_price = None, None
    entry_price, sl_price, tp_price = signal['entry_price'], signal['sl_price'], signal['tp_price']
    if signal['side'] == 'LONG':
        if current_price <= sl_price: exit_status, exit_price = "SL_HIT", sl_price
        elif current_price >= tp_price: exit_status, exit_price = "TP_HIT", tp_price
    elif signal['side'] == 'SHORT':
        if current_price >= sl_price: exit_status, exit_price = "SL_HIT", sl_price
        elif current_price <= tp_price: exit_status, exit_price = "TP_HIT", tp_price
    if exit_status:
        position_size_usd, leverage = 50, 100
        price_change_percent = ((exit_price - entry_price) / entry_price) if entry_price != 0 else 0
        if signal['side'] == 'SHORT': price_change_percent = -price_change_percent
        pnl_percent = price_change_percent * leverage * 100
        pnl_usd = position_size_usd * (pnl_percent / 100)
        await update_trade_in_sheet(trade_log_ws, signal, exit_status, exit_price, pnl_usd, pnl_percent)
        emoji = "✅" if pnl_usd > 0 else "❌"
        msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
               f"<b>Инструмент:</b> <code>{signal['pair']}</code>\n"
               f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent:+.2f}%)</b>")
        await broadcast_func(app, msg)
        state['monitored_signals'] = []
        save_state_func()
        print(f"Trade {signal['signal_id']} closed. Portfolio is now empty.")

# === НОВАЯ НАДЕЖНАЯ ФУНКЦИЯ РАСЧЕТА ATR =================================
async def get_entry_atr(exchange, pair):
    """
    Надежно рассчитывает ATR, обрабатывая ошибки API и данных.
    """
    try:
        # 1. Запрашиваем исторические данные
        ohlcv = await exchange.fetch_ohlcv(pair, TIMEFRAME, limit=20)

        # 2. Проверяем, что данные получены и их достаточно
        if not ohlcv or len(ohlcv) < 15:
            print(f"ATR Error: Not enough OHLCV data received for {pair}. Got: {len(ohlcv) if ohlcv else 0} candles.")
            return 0

        # 3. Создаем DataFrame
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # 4. Убеждаемся, что числовые колонки имеют правильный тип
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col])

        # 5. Рассчитываем ATR
        df.ta.atr(length=14, append=True)
        
        # 6. Извлекаем последнее значение ATR и проверяем его
        atr_value = df.iloc[-1]['ATR_14']
        
        if pd.isna(atr_value):
            print(f"ATR Error: ATR calculation resulted in NaN for {pair}.")
            return 0
            
        return atr_value

    except ccxt.NetworkError as e:
        print(f"ATR Error: Network issue while fetching OHLCV for {pair}. Details: {e}")
        return 0
    except ccxt.ExchangeError as e:
        print(f"ATR Error: Exchange returned an error for {pair}. Details: {e}")
        return 0
    except Exception as e:
        # Ловим все остальные неожиданные ошибки
        print(f"ATR Error: An unexpected error occurred in get_entry_atr for {pair}. Details: {e}", exc_info=True)
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

    if (total_bids_usd + total_asks_usd) < MIN_TOTAL_LIQUIDITY_USD:
        # This print is for our internal logs, not for Telegram
        # print(f"Pre-filter: Low liquidity (${total_bids_usd/1e6:.1f}M / ${total_asks_usd/1e6:.1f}M). Skipped.")
        return

    imbalance_ratio = 0
    dominant_side_is_bids = total_bids_usd > total_asks_usd
    if total_bids_usd > 0 and total_asks_usd > 0:
        imbalance_ratio = max(total_bids_usd / total_asks_usd, total_asks_usd / total_bids_usd)
    elif total_bids_usd > 0 or total_asks_usd > 0:
        imbalance_ratio = float('inf')

    if imbalance_ratio < MIN_IMBALANCE_RATIO:
        # This print is for our internal logs, not for Telegram
        # print(f"Pre-filter: Weak imbalance (ratio: {imbalance_ratio:.2f}, threshold: {MIN_IMBALANCE_RATIO:.2f}). Skipped.")
        return

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
                        f"Отправляю на глубокий анализ в LLM...")
    else:
        detailed_msg = f"🧠 Сканер нашел **качественную аномалию** (дисбаланс {imbalance_ratio:.1f}x). Отправляю на анализ LLM..."
    
    await broadcast_func(app, detailed_msg)

    focused_data = {PAIR_TO_SCAN: {'bids': top_bids, 'asks': top_asks}}
    prompt_data = json.dumps(focused_data, indent=2)
    full_prompt = LLM_PROMPT_MICROSTRUCTURE + "\n\nАНАЛИЗИРУЕМЫЕ ДАННЫЕ:\n" + prompt_data
    
    llm_response_content = await ask_llm_func(full_prompt)

    if not llm_response_content: return

    try:
        cleaned_response = llm_response_content.strip().strip('```json').strip('```').strip()
        decision = json.loads(cleaned_response)
        confidence = decision.get("confidence_score", 0)
        reason = decision.get("reason", "Причина не указана.")

        if confidence < MIN_CONFIDENCE_SCORE:
            await broadcast_func(app, f"🧐 <b>СИГНАЛ ОТКЛОНЕН LLM (Оценка: {confidence}/10)</b>\n\n<b>Причина:</b> <i>\"{reason}\"</i>")
            return

        trade_plan = {}
        ticker = await exchange.fetch_ticker(PAIR_TO_SCAN)
        current_price = ticker.get('last')
        if not current_price: return
        
        support = decision.get("key_support_level")
        resistance = decision.get("key_resistance_level")
        levels_are_valid = support and resistance and isinstance(support, (int, float)) and isinstance(resistance, (int, float))

        if levels_are_valid:
            trade_plan['strategy_idea'] = "Level-based Entry"
            if abs(current_price - support) < abs(current_price - resistance):
                trade_plan['side'] = "LONG"
                trade_plan['entry_price'] = support * (1 + ENTRY_OFFSET_PERCENT)
                trade_plan['sl_price'] = support * (1 - SL_OFFSET_PERCENT)
            else:
                trade_plan['side'] = "SHORT"
                trade_plan['entry_price'] = resistance * (1 - ENTRY_OFFSET_PERCENT)
                trade_plan['sl_price'] = resistance * (1 + SL_OFFSET_PERCENT)
            risk = abs(trade_plan['entry_price'] - trade_plan['sl_price'])
            trade_plan['tp_price'] = trade_plan['entry_price'] + risk * MIN_RR_RATIO if trade_plan['side'] == 'LONG' else trade_plan['entry_price'] - risk * MIN_RR_RATIO
        else:
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

        decision.update(trade_plan)
        decision['pair'] = PAIR_TO_SCAN
        msg = (f"<b>🔥 НОВЫЙ СИГНАЛ (Оценка: {confidence}/10)</b>\n\n"
               f"<b>Тип входа:</b> {decision['strategy_idea']}\n"
               f"<b>Инструмент:</b> <code>{PAIR_TO_SCAN}</code>\n"
               f"<b>Алгоритм в стакане:</b> <i>{decision.get('algorithm_type', 'N/A')}</i>\n"
               f"<b>Рассчитанный план (RR ~{MIN_RR_RATIO:.1f}:1):</b>\n"
               f"  - Вход: <code>{decision['entry_price']:.2f}</code>\n"
               f"  - SL: <code>{decision['sl_price']:.2f}</code>\n"
               f"  - TP: <code>{decision['tp_price']:.2f}</code>\n\n"
               f"<b>Обоснование LLM:</b> <i>\"{reason}\"</i>")
        await broadcast_func(app, msg)

        final_entry_atr = await get_entry_atr(exchange, PAIR_TO_SCAN)
        success = await log_trade_to_sheet(trade_log_ws, decision, final_entry_atr, state, save_state_func)
        if success:
            await broadcast_func(app, "✅ Виртуальная сделка успешно залогирована и взята на мониторинг.")

    except json.JSONDecodeError:
        print(f"Error parsing LLM JSON response. Raw response: {llm_response_content}")
        await broadcast_func(app, "⚠️ LLM вернул некорректный JSON. Не могу обработать.")
    except Exception as e:
        print(f"Error processing new opportunity: {e}", exc_info=True)

async def scanner_main_loop(app, ask_llm_func, broadcast_func, trade_log_ws, state, save_state_func):
    print("Main Engine loop started (v29_robust_atr).")
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}})
    while state.get("bot_on", True):
        try:
            print(f"\n--- Running Main Cycle | Active Trades: {len(state.get('monitored_signals',[]))} ---")
            await monitor_active_trades(exchange, app, broadcast_func, trade_log_ws, state, save_state_func)
            if len(state.get('monitored_signals', [])) < MAX_PORTFOLIO_SIZE:
                await scan_for_new_opportunities(exchange, app, ask_llm_func, broadcast_func, trade_log_ws, state, save_state_func)
            # Убрал лишние принты в консоль для чистоты логов
            # print(f"--- Cycle Finished. Sleeping for 30 seconds. ---")
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            print("Main Engine loop cancelled.")
            break
        except Exception as e:
            print(f"CRITICAL Error in Main Engine loop: {e}", exc_info=True)
            await asyncio.sleep(60)
    print("Main Engine loop stopped.")
    await exchange.close()
