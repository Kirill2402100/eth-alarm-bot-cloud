# File: scanner_engine.py (v8 - With Trade Executor)

import asyncio
import json
from data_feeder import last_data
from trade_executor import log_trade_to_sheet # Импортируем новую функцию

# ... (Конфигурация и LLM_PROMPT_MICROSTRUCTURE остаются без изменений)
LARGE_ORDER_USD = 50000 
TOP_N_ORDERS_TO_SEND = 5
LLM_PROMPT_MICROSTRUCTURE = """...""" # Ваш длинный промпт здесь

async def scanner_main_loop(app, ask_llm_func, broadcast_func, trade_log_ws):
    """
    Главный цикл сканера. Теперь он вызывает trade_executor для записи сделок.
    """
    print("Scanner Engine loop started.")
    last_llm_call_time = 0

    while True:
        try:
            # ... (логика сбора market_anomalies остается без изменений)
            await asyncio.sleep(5)
            # ...

            current_time = asyncio.get_event_loop().time()
            if (current_time - last_llm_call_time) > 45 and any(d['bids'] or d['asks'] for d in market_anomalies.values()):
                last_llm_call_time = current_time
                
                # ... (логика подготовки и отправки промпта остается без изменений)
                
                llm_response_content = await ask_llm_func(full_prompt)
                
                if llm_response_content:
                    try:
                        cleaned_response = llm_response_content.strip().strip('```json').strip('```').strip()
                        decision = json.loads(cleaned_response)

                        if decision and decision.get("confidence_score", 0) >= 7:
                            # ... (отправка сообщения в Telegram остается без изменений)
                            await broadcast_func(app, msg)

                            # >>> НОВАЯ ЛОГИКА: ВЫЗОВ ИСПОЛНИТЕЛЯ <<<
                            success = await log_trade_to_sheet(trade_log_ws, decision)
                            if success:
                                await broadcast_func(app, "✅ Виртуальная сделка успешно залогирована в Google-таблицу.")
                            else:
                                await broadcast_func(app, "⚠️ Не удалось залогировать сделку в Google-таблицу.")
                        else:
                            await broadcast_func(app, "🧐 LLM проанализировал данные, но не нашел уверенного сетапа.")
                    except Exception as e:
                        # ... (обработка ошибок без изменений)
                else:
                    await broadcast_func(app, "⚠️ LLM не ответил на запрос.")

        except asyncio.CancelledError:
            print("Scanner Engine loop cancelled.")
            break
        except Exception as e:
            print(f"Error in Scanner Engine loop: {e}")
            await asyncio.sleep(10)
