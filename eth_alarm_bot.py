# File: scanner_engine.py (v25 - Smart Pre-filter)
# Changelog 17-Jul-2025 (Europe/Belgrade):
# • Добавлен умный предварительный фильтр для отсеивания слабого рыночного шума.
# • Введены параметры MIN_TOTAL_LIQUIDITY_USD и MIN_IMBALANCE_RATIO.
# • LLM теперь вызывается только для анализа качественных, несбалансированных сетапов.

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
TOP_N_ORDERS_TO_ANALYZE = 15 # Сколько топ-заявок суммировать для анализа

# --- НОВЫЕ ПАРАМЕТРЫ ПРЕДВАРИТЕЛЬНОГО ФИЛЬТРА ---
MIN_TOTAL_LIQUIDITY_USD = 2000000 # Минимальный суммарный объем топ-N плит для анализа
MIN_IMBALANCE_RATIO = 3.0         # Во сколько раз одна сторона должна быть сильнее другой

# --- Параметры торговой стратегии ---
MAX_PORTFOLIO_SIZE = 1
MIN_CONFIDENCE_SCORE = 7
MIN_RR_RATIO = 1.5
ENTRY_OFFSET_PERCENT = 0.0005
SL_OFFSET_PERCENT = 0.0010
LLM_COOLDOWN_SECONDS = 180

# ... (Промпт и другие функции остаются без изменений) ...
LLM_PROMPT_MICROSTRUCTURE = """
Ты — ведущий аналитик-квант в HFT-фонде, специализирующийся на анализе микроструктуры рынка BTC/USDT.

**ТВОЯ ЗАДАЧА:**
Проанализируй предоставленные JSON-данные о топ-15 крупнейших лимитных заявках ("плитах") в биржевом стакане. Эти данные уже прошли предварительную фильтрацию и указывают на значительный дисбаланс ликвидности.

1.  **Оцени сетап:** Является ли этот дисбаланс надежным для торговли?
2.  **Определи тип алгоритма,** который создает этот дисбаланс (Market-Maker, Absorption, Spoofing).
3.  **Определи ключевые уровни:** Найди самый значимый уровень поддержки и сопротивления.
4.  **Обоснуй свое решение:** ВСЕГДА предоставляй краткое объяснение в поле "reason".

**ФОРМАТ ОТВЕТА:**
Верни ТОЛЬКО JSON-объект.

Пример уверенного сетапа:
{
  "confidence_score": 9,
  "algorithm_type": "Absorption",
  "reason": "Очень плотный кластер бидов на ~119200 выступает сильной поддержкой. Аски разрежены. Высокая вероятность отскока от этого уровня.",
  "key_support_level": 119200.0,
  "key_resistance_level": 119850.0
}

Пример неуверенного сетапа:
{
  "confidence_score": 2,
  "algorithm_type": "Spoofing",
  "reason": "Несмотря на сильный перевес бидов, их структура похожа на спуфинг. Отсутствие крупных асков делает сетап неустойчивым.",
  "key_support_level": 119000.0,
  "key_resistance_level": 121000.0
}
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

async def get_entry_atr(exchange, pair):
    try:
        ohlcv = await exchange.fetch_ohlcv(pair, TIMEFRAME, limit=20)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df.ta.atr(length=14, append=True)
        atr_value = df.iloc[-1]['ATR_14']
        return atr_value if pd.notna(atr_value) else 0
    except Exception: return 0

# === МОДУЛЬ СКАНИРОВАНИЯ (с умным пре-фильтром) =========================
async def scan_for_new_opportunities(exchange, app, ask_llm_func, broadcast_func, trade_log_ws, state, save_state_func):
    current_time = time.time()
    last_call_time = state.get('llm_cooldown', {}).get(PAIR_TO_SCAN, 0)
    if (current_time - last_call_time) < LLM_COOLDOWN_SECONDS:
        return

    try:
        order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=50) # Берем больше данных для анализа
        large_bids = sorted([{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('bids', []) if p and a and (p*a > LARGE_ORDER_USD)], key=lambda x: x['value_usd'], reverse=True)
        large_asks = sorted([{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('asks', []) if p and a and (p*a > LARGE_ORDER_USD)], key=lambda x: x['value_usd'], reverse=True)
    except Exception as e:
        print(f"Could not fetch order book for {PAIR_TO_SCAN}: {e}")
        return

    # --- НОВЫЙ БЛОК: ПРЕДВАРИТЕЛЬНАЯ ФИЛЬТРАЦИЯ ---
    top_bids = large_bids[:TOP_N_ORDERS_TO_ANALYZE]
    top_asks = large_asks[:TOP_N_ORDERS_TO_ANALYZE]
    
    total_bids_usd = sum(b['value_usd'] for b in top_bids)
    total_asks_usd = sum(a['value_usd'] for a in top_asks)

    if (total_bids_usd + total_asks_usd) < MIN_TOTAL_LIQUIDITY_USD:
        print(f"Pre-filter: Low liquidity (${total_bids_usd/1e6:.1f}M / ${total_asks_usd/1e6:.1f}M). Skipped.")
        return

    imbalance_ratio = 0
    if total_bids_usd > 0 and total_asks_usd > 0:
        imbalance_ratio = max(total_bids_usd / total_asks_usd, total_asks_usd / total_bids_usd)
    elif total_bids_usd > 0 or total_asks_usd > 0:
        imbalance_ratio = float('inf') # Если одна сторона пуста, дисбаланс максимальный

    if imbalance_ratio < MIN_IMBALANCE_RATIO:
        print(f"Pre-filter: Weak imbalance (ratio: {imbalance_ratio:.2f}, threshold: {MIN_IMBALANCE_RATIO:.2f}). Skipped.")
        return
    # --- КОНЕЦ БЛОКА ФИЛЬТРАЦИИ ---

    # Если мы дошли сюда, аномалия качественная. Запускаем полный анализ.
    state.setdefault('llm_cooldown', {})[PAIR_TO_SCAN] = time.time()
    save_state_func()

    focused_data = {PAIR_TO_SCAN: {'bids': top_bids, 'asks': top_asks}}
    prompt_data = json.dumps(focused_data, indent=2)
    full_prompt = LLM_PROMPT_MICROSTRUCTURE + "\n\nАНАЛИЗИРУЕМЫЕ ДАННЫЕ:\n" + prompt_data

    await broadcast_func(app, f"🧠 Сканер нашел **качественную аномалию** (дисбаланс {imbalance_ratio:.1f}x). Отправляю на анализ LLM...")
    llm_response_content = await ask_llm_func(full_prompt)

    if not llm_response_content: return

    try:
        # ... (дальнейшая логика обработки ответа LLM остается такой же) ...
        cleaned_response = llm_response_content.strip().strip('```json').strip('```').strip()
        decision = json.loads(cleaned_response)
        confidence = decision.get("confidence_score", 0)
        reason = decision.get("reason", "Причина не указана.")

        if confidence < MIN_CONFIDENCE_SCORE:
            msg = (f"🧐 <b>СИГНАЛ ОТКЛОНЕН LLM (Оценка: {confidence}/10)</b>\n\n"
                   f"<b>Причина:</b> <i>\"{reason}\"</i>")
            await broadcast_func(app, msg)
            return

        support = decision.get("key_support_level")
        resistance = decision.get("key_resistance_level")
        if not all(isinstance(v, (int, float)) for v in [support, resistance]):
            await broadcast_func(app, f"⚠️ LLM вернул уверенный сигнал, но без корректных уровней. Причина: {reason}")
            return

        ticker = await exchange.fetch_ticker(PAIR_TO_SCAN)
        current_price = ticker.get('last')
        if not current_price: return

        dist_to_support = abs(current_price - support)
        dist_to_resistance = abs(current_price - resistance)
        trade_plan = {}
        if dist_to_support < dist_to_resistance:
            trade_plan['side'] = "LONG"
            trade_plan['entry_price'] = support * (1 + ENTRY_OFFSET_PERCENT)
            trade_plan['sl_price'] = support * (1 - SL_OFFSET_PERCENT)
            risk = trade_plan['entry_price'] - trade_plan['sl_price']
            trade_plan['tp_price'] = trade_plan['entry_price'] + risk * MIN_RR_RATIO
            trade_plan['strategy_idea'] = "Long from Support"
        else:
            trade_plan['side'] = "SHORT"
            trade_plan['entry_price'] = resistance * (1 - ENTRY_OFFSET_PERCENT)
            trade_plan['sl_price'] = resistance * (1 + SL_OFFSET_PERCENT)
            risk = trade_plan['sl_price'] - trade_plan['entry_price']
            trade_plan['tp_price'] = trade_plan['entry_price'] - risk * MIN_RR_RATIO
            trade_plan['strategy_idea'] = "Short from Resistance"

        decision.update(trade_plan)
        decision['pair'] = PAIR_TO_SCAN
        msg = (f"<b>🔥 НОВЫЙ СИГНАЛ (Оценка: {confidence}/10)</b>\n\n"
               f"<b>Инструмент:</b> <code>{PAIR_TO_SCAN}</code>\n"
               f"<b>Стратегия:</b> {decision['strategy_idea']}\n"
               f"<b>Алгоритм в стакане:</b> <i>{decision.get('algorithm_type', 'N/A')}</i>\n"
               f"<b>Рассчитанный план (RR ~{MIN_RR_RATIO:.1f}:1):</b>\n"
               f"  - Вход: <code>{decision['entry_price']:.2f}</code>\n"
               f"  - SL: <code>{decision['sl_price']:.2f}</code>\n"
               f"  - TP: <code>{decision['tp_price']:.2f}</code>\n\n"
               f"<b>Обоснование LLM:</b> <i>\"{reason}\"</i>")
        await broadcast_func(app, msg)

        entry_atr = await get_entry_atr(exchange, PAIR_TO_SCAN)
        success = await log_trade_to_sheet(trade_log_ws, decision, entry_atr, state, save_state_func)
        if success:
            await broadcast_func(app, "✅ Виртуальная сделка успешно залогирована и взята на мониторинг.")

    except json.JSONDecodeError:
        print(f"Error parsing LLM JSON response. Raw response: {llm_response_content}")
        await broadcast_func(app, "⚠️ LLM вернул некорректный JSON. Не могу обработать.")
    except Exception as e:
        print(f"Error processing new opportunity: {e}", exc_info=True)

# === ГЛАВНЫЙ ЦИКЛ ========================================================
async def scanner_main_loop(app, ask_llm_func, broadcast_func, trade_log_ws, state, save_state_func):
    print("Main Engine loop started (v25_smart_pre-filter).")
    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}})
    while state.get("bot_on", True):
        try:
            print(f"\n--- Running Main Cycle | Active Trades: {len(state.get('monitored_signals',[]))} ---")
            await monitor_active_trades(exchange, app, broadcast_func, trade_log_ws, state, save_state_func)
            if len(state.get('monitored_signals', [])) < MAX_PORTFOLIO_SIZE:
                await scan_for_new_opportunities(exchange, app, ask_llm_func, broadcast_func, trade_log_ws, state, save_state_func)
            print(f"--- Cycle Finished. Sleeping for 30 seconds. ---")
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            print("Main Engine loop cancelled.")
            break
        except Exception as e:
            print(f"CRITICAL Error in Main Engine loop: {e}", exc_info=True)
            await asyncio.sleep(60)
    print("Main Engine loop stopped.")
    await exchange.close()
