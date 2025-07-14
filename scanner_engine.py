# File: scanner_engine.py

import asyncio
import json
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from data_feeder import last_data
from trade_executor import log_trade_to_sheet

# --- КОНФИГУРАЦИЯ ---
LARGE_ORDER_USD = 50000 
TOP_N_ORDERS_TO_SEND = 5
MAX_PORTFOLIO_SIZE = 10
MIN_RR_RATIO = 1.5 # Минимально допустимое соотношение Risk/Reward

# --- ПРОМПТ ДЛЯ LLM ---
LLM_PROMPT_MICROSTRUCTURE = """
Ты — ведущий аналитик-квант в HFT-фонде, специализирующийся на анализе микроструктуры рынка (Order Flow, Market Making).

**ТВОЯ ЗАДАЧА:**
Проанализируй предоставленные JSON-данные о состоянии биржевого стакана для нескольких криптовалютных пар. Данные включают топ-5 крупнейших лимитных заявок ("плит") на покупку (bids) и продажу (asks).

1.  **Выбери ОДНУ САМУЮ лучшую пару** с наиболее явным и надежным сетапом для торговли. Ищи четкие "коридоры", сильные уровни поддержки/сопротивления, созданные плитами.
2.  **Определи тип алгоритма,** который, скорее всего, создает эти плиты (например, "Market-Maker", "Absorption Algorithm").
3.  **Предложи конкретный торговый план** (entry, sl, tp).

**ФОРМАТ ОТВЕТА:**
Верни ОДИН JSON-объект для лучшего кандидата.

{
  "pair": "BTC/USDT",
  "confidence_score": 9,
  "algorithm_type": "Classic Market-Maker",
  "strategy_idea": "Range Trading (Long)",
  "reason": "Очень плотный кластер бидов выступает сильной поддержкой. Аски разрежены. Высокая вероятность отскока.",
  "entry_price": 119200.0,
  "sl_price": 119050.0,
  "tp_price": 119425.0
}

Если уверенных сетапов нет, верни: {"confidence_score": 0}
"""

exchange = ccxt.mexc({'options': {'defaultType': 'swap'}})

async def get_entry_atr(pair):
    try:
        ohlcv = await exchange.fetch_ohlcv(pair, '15m', limit=20)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df.ta.atr(length=14, append=True)
        atr_value = df.iloc[-1]['ATR_14']
        return atr_value if pd.notna(atr_value) else 0
    except Exception as e:
        print(f"Could not fetch ATR for {pair}: {e}")
        return 0

async def scanner_main_loop(app, ask_llm_func, broadcast_func, trade_log_ws, state):
    print("Scanner Engine loop started (v_final_rr_check).")
    last_llm_call_time = 0

    while True:
        try:
            await asyncio.sleep(10)

            active_pairs = {s['pair'] for s in state.get('monitored_signals', [])}
            if len(active_pairs) >= MAX_PORTFOLIO_SIZE:
                print(f"Portfolio is full ({len(active_pairs)}). Scanner is sleeping.")
                await asyncio.sleep(30)
                continue

            market_anomalies = {}
            current_data_snapshot = dict(last_data)

            for symbol, data in current_data_snapshot.items():
                pair_name = symbol.split(':')[0]
                if not data or not data.get('bids') or not data.get('asks') or pair_name in active_pairs:
                    continue

                large_bids = [{'price': p, 'value_usd': round(p*a)} for p, a in data.get('bids', []) if p and a and (p*a > LARGE_ORDER_USD)]
                large_asks = [{'price': p, 'value_usd': round(p*a)} for p, a in data.get('asks', []) if p and a and (p*a > LARGE_ORDER_USD)]

                if large_bids or large_asks:
                    top_bids = sorted(large_bids, key=lambda x: x['value_usd'], reverse=True)[:TOP_N_ORDERS_TO_SEND]
                    top_asks = sorted(large_asks, key=lambda x: x['value_usd'], reverse=True)[:TOP_N_ORDERS_TO_SEND]
                    if top_bids or top_asks:
                      market_anomalies[pair_name] = {'bids': top_bids, 'asks': top_asks}

            current_time = asyncio.get_event_loop().time()
            if (current_time - last_llm_call_time) > 45 and market_anomalies:
                last_llm_call_time = current_time
                prompt_data = json.dumps(market_anomalies, indent=2)
                full_prompt = LLM_PROMPT_MICROSTRUCTURE + "\n\nАНАЛИЗИРУЕМЫЕ ДАННЫЕ:\n" + prompt_data
                await broadcast_func(app, f"🧠 Сканер нашел {len(market_anomalies)} аномалий. Отправляю на анализ LLM...")
                llm_response_content = await ask_llm_func(full_prompt)
                
                if llm_response_content:
                    try:
                        cleaned_response = llm_response_content.strip().strip('```json').strip('```').strip()
                        decision = json.loads(cleaned_response)

                        if decision and decision.get("confidence_score", 0) >= 7:
                            # --- ПРОВЕРКА RISK/REWARD В КОДЕ ---
                            entry = decision.get("entry_price")
                            sl = decision.get("sl_price")
                            tp = decision.get("tp_price")
                            side = "LONG" if "Long" in decision.get("strategy_idea", "") else "SHORT"
                            
                            if not all([entry, sl, tp]):
                                await broadcast_func(app, f"⚠️ LLM вернул неполные данные для {decision.get('pair')}. Пропускаю.")
                                continue

                            risk = abs(entry - sl)
                            reward = abs(tp - entry)

                            if risk == 0: continue # Избегаем деления на ноль

                            rr_ratio = reward / risk

                            if rr_ratio < MIN_RR_RATIO:
                                await broadcast_func(app, f"⚠️ LLM предложил сделку по {decision.get('pair')} с низким RR ({rr_ratio:.2f}:1). Отклонено.")
                                continue
                            # --- КОНЕЦ ПРОВЕРКИ ---

                            pair_to_trade = decision.get("pair")
                            if pair_to_trade in active_pairs:
                                await broadcast_func(app, f"⚠️ LLM предложил сделку по {pair_to_trade}, но он уже в портфеле. Пропускаю.")
                                continue

                            msg = (f"<b>🔥 LLM РЕКОМЕНДАЦИЯ (RR {rr_ratio:.2f}:1, Оценка: {decision['confidence_score']}/10)</b>\n\n"
                                   f"<b>Инструмент:</b> <code>{decision['pair']}</code>\n"
                                   f"<b>Стратегия:</b> {decision['strategy_idea']}\n"
                                   f"<b>Алгоритм:</b> <i>{decision['algorithm_type']}</i>\n"
                                   f"<b>План:</b>\n"
                                   f"  - Вход: <code>{entry}</code>\n"
                                   f"  - SL: <code>{sl}</code>\n"
                                   f"  - TP: <code>{tp}</code>\n\n"
                                   f"<b>Обоснование:</b> <i>\"{decision['reason']}\"</i>")
                            await broadcast_func(app, msg)
                            
                            entry_atr = await get_entry_atr(pair_to_trade)
                            success = await log_trade_to_sheet(trade_log_ws, decision, entry_atr)
                            if success:
                                await broadcast_func(app, "✅ Виртуальная сделка успешно залогирована.")
                            else:
                                await broadcast_func(app, "⚠️ Не удалось залогировать сделку.")
                        else:
                            await broadcast_func(app, "🧐 LLM не нашел уверенного сетапа.")
                    except Exception as e:
                        print(f"Error parsing LLM decision: {e} | Response: {llm_response_content}")
                        await broadcast_func(app, "⚠️ Ошибка обработки ответа LLM.")
                else:
                    await broadcast_func(app, "⚠️ LLM не ответил на запрос.")
        except asyncio.CancelledError:
            print("Scanner Engine loop cancelled.")
            break
        except Exception as e:
            print(f"Error in Scanner Engine loop: {e}")
            await asyncio.sleep(10)
