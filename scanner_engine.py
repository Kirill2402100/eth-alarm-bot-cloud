# File: scanner_engine.py (ФИНАЛЬНАЯ ВЕРСИЯ)

import asyncio
import json
from data_feeder import last_data
from trade_executor import log_trade_to_sheet

# --- КОНФИГУРАЦИЯ И ПРОМПТ ---
LARGE_ORDER_USD = 50000 
TOP_N_ORDERS_TO_SEND = 5
LLM_PROMPT_MICROSTRUCTURE = """
Ты — ведущий аналитик-квант в HFT-фонде, специализирующийся на анализе микроструктуры рынка (Order Flow, Market Making).

**ТВОЯ ЗАДАЧА:**
Проанализируй предоставленные JSON-данные о состоянии биржевого стакана для нескольких криптовалютных пар. Данные включают топ-5 крупнейших лимитных заявок ("плит") на покупку (bids) и продажу (asks).

1.  **Выбери ОДНУ САМУЮ лучшую пару** с наиболее явным и надежным сетапом для торговли. Ищи четкие "коридоры", сильные уровни поддержки/сопротивления, созданные плитами.
2.  **Определи тип алгоритма,** который, скорее всего, создает эти плиты (например, "Market-Maker", "Absorption Algorithm").
3.  **Предложи конкретный торговый план,** если сетап достаточно надежен.

**ФОРМАТ ОТВЕТА:**
Верни ОДИН JSON-объект (не массив) для лучшего кандидата.

{
  "pair": "BTC/USDT",
  "confidence_score": 9,
  "algorithm_type": "Classic Market-Maker",
  "strategy_idea": "Range Trading (Long)",
  "reason": "Очень плотный кластер бидов выступает сильной поддержкой. Аски разрежены. Высокая вероятность отскока.",
  "entry_price": 119200.0,
  "sl_price": 119050.0,
  "tp_price": 119800.0
}

Если уверенных сетапов нет, верни: {"confidence_score": 0}
"""

async def scanner_main_loop(app, ask_llm_func, broadcast_func, trade_log_ws):
    """
    Главный цикл сканера с надежной обработкой данных и интеграцией с LLM.
    """
    print("Scanner Engine loop started (v_final_unpack_fix).")
    last_llm_call_time = 0

    while True:
        try:
            await asyncio.sleep(5)
            market_anomalies = {}
            current_data_snapshot = dict(last_data)

            for symbol, data in current_data_snapshot.items():
                if not data or not data.get('bids') or not data.get('asks'):
                    continue

                large_bids = []
                large_asks = []

                # --- Надежная обработка данных стакана ---
                for order in data.get('bids', []):
                    if not (isinstance(order, (list, tuple)) and len(order) >= 2): continue
                    price, amount = order[0], order[1]
                    if price is None or amount is None: continue
                    order_value_usd = price * amount
                    if order_value_usd > LARGE_ORDER_USD:
                        large_bids.append({'price': price, 'value_usd': round(order_value_usd)})

                for order in data.get('asks', []):
                    if not (isinstance(order, (list, tuple)) and len(order) >= 2): continue
                    price, amount = order[0], order[1]
                    if price is None or amount is None: continue
                    order_value_usd = price * amount
                    if order_value_usd > LARGE_ORDER_USD:
                        large_asks.append({'price': price, 'value_usd': round(order_value_usd)})

                if large_bids or large_asks:
                    top_bids = sorted(large_bids, key=lambda x: x['value_usd'], reverse=True)[:TOP_N_ORDERS_TO_SEND]
                    top_asks = sorted(large_asks, key=lambda x: x['value_usd'], reverse=True)[:TOP_N_ORDERS_TO_SEND]
                    if top_bids or top_asks:
                      market_anomalies[symbol] = {'bids': top_bids, 'asks': top_asks}

            current_time = asyncio.get_event_loop().time()
            if (current_time - last_llm_call_time) > 45 and market_anomalies:
                last_llm_call_time = current_time
                
                prompt_data = json.dumps(market_anomalies, indent=2)
                full_prompt = LLM_PROMPT_MICROSTRUCTURE + "\n\nАНАЛИЗИРУЕМЫЕ ДАННЫЕ:\n" + prompt_data
                
                await broadcast_func(app, "🧠 Сканер агрегировал данные. Отправляю топ-5 аномалий на анализ LLM...")
                
                llm_response_content = await ask_llm_func(full_prompt)
                
                if llm_response_content:
                    try:
                        cleaned_response = llm_response_content.strip().strip('```json').strip('```').strip()
                        decision = json.loads(cleaned_response)

                        if decision and decision.get("confidence_score", 0) >= 7:
                            msg = (f"<b>🔥 LLM РЕКОМЕНДАЦИЯ (Оценка: {decision['confidence_score']}/10)</b>\n\n"
                                   f"<b>Инструмент:</b> <code>{decision['pair']}</code>\n"
                                   f"<b>Стратегия:</b> {decision['strategy_idea']}\n"
                                   f"<b>Алгоритм:</b> <i>{decision['algorithm_type']}</i>\n"
                                   f"<b>План:</b>\n"
                                   f"  - Вход: <code>{decision.get('entry_price', 'N/A')}</code>\n"
                                   f"  - SL: <code>{decision.get('sl_price', 'N/A')}</code>\n"
                                   f"  - TP: <code>{decision.get('tp_price', 'N/A')}</code>\n\n"
                                   f"<b>Обоснование:</b> <i>\"{decision['reason']}\"</i>")
                            await broadcast_func(app, msg)

                            success = await log_trade_to_sheet(trade_log_ws, decision)
                            if success:
                                await broadcast_func(app, "✅ Виртуальная сделка успешно залогирована в Google-таблицу.")
                            else:
                                await broadcast_func(app, "⚠️ Не удалось залогировать сделку.")
                        else:
                            await broadcast_func(app, "🧐 LLM проанализировал данные, но не нашел уверенного сетапа.")
                    except Exception as e:
                        print(f"Error parsing LLM decision: {e} | Response: {llm_response_content}")
                        await broadcast_func(app, "⚠️ Ошибка обработки ответа LLM.")
                else:
                    await broadcast_func(app, "⚠️ LLM не ответил на запрос. Проверьте логи.")

        except asyncio.CancelledError:
            print("Scanner Engine loop cancelled.")
            break
        except Exception as e:
            print(f"Error in Scanner Engine loop: {e}")
            await asyncio.sleep(10)
