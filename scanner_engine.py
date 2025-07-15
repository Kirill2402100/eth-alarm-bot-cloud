# File: scanner_engine.py (v18 - Final REST-only Architecture)

import asyncio
import json
import time
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from trade_executor import log_trade_to_sheet, update_trade_in_sheet

# --- Конфигурация ---
PAIR_TO_SCAN = 'BTC/USDT'
LARGE_ORDER_USD = 500000 
TOP_N_ORDERS_TO_SEND = 15
MAX_PORTFOLIO_SIZE = 1
MIN_RR_RATIO = 1.5
LLM_COOLDOWN_SECONDS = 180

# --- ПРОМПТ ДЛЯ LLM ---
LLM_PROMPT_MICROSTRUCTURE = """
Ты — ведущий аналитик-квант в HFT-фонде, специализирующийся на анализе микроструктуры рынка BTC/USDT.

**ТВОЯ ЗАДАЧА:**
Проанализируй предоставленные JSON-данные о состоянии биржевого стакана для BTC/USDT. Данные включают топ-15 крупнейших лимитных заявок ("плит").

1.  **Оцени текущий сетап:** Является ли он надежным для входа в сделку прямо сейчас?
2.  **Определи тип алгоритма,** который создает эти плиты. Вот основные типы:
    * **Classic Market-Maker:** Две четкие "стены" на покупку и продажу, формирующие коридор. Стратегия: Range Trading.
    * **Absorption Algorithm:** Одна аномально крупная стена (на покупку или продажу), которая "впитывает" все рыночные ордера. Стратегия: Trade from Support/Resistance.
    * **Spoofing:** Крупная заявка, которая исчезает при подходе цены. Стратегия: No Trade или Fade (торговля в противоположную сторону).
3.  **Предложи конкретный торговый план** (entry, sl, tp) с учетом требуемой стратегии.

**ФОРМАТ ОТВЕТА:**
Верни JSON-объект.

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

Если сетап не подходит для торговли, верни: {"confidence_score": 0}
"""

exchange = ccxt.mexc({'options': {'defaultType': 'swap'}})

# --- МОДУЛЬ МОНИТОРИНГА ---
async def monitor_active_trades(app, broadcast_func, trade_log_ws, state, save_state_func):
    active_signals = state.get('monitored_signals')
    if not active_signals: return
    
    signal = active_signals[0] # Так как у нас всегда только одна сделка
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

# --- МОДУЛЬ СКАНИРОВАНИЯ ---
async def get_entry_atr(pair):
    try:
        ohlcv = await exchange.fetch_ohlcv(pair, '15m', limit=20)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df.ta.atr(length=14, append=True)
        atr_value = df.iloc[-1]['ATR_14']
        return atr_value if pd.notna(atr_value) else 0
    except Exception: return 0

async def scan_for_new_opportunities(app, ask_llm_func, broadcast_func, trade_log_ws, state):
    current_time = time.time()
    last_call_time = state.get('llm_cooldown', {}).get(PAIR_TO_SCAN, 0)
    if (current_time - last_call_time) < LLM_COOLDOWN_SECONDS:
        return

    print(f"Scanning for anomalies in {PAIR_TO_SCAN}...")
    try:
        order_book = await exchange.fetch_order_book(PAIR_TO_SCAN, limit=25)
        large_bids = [{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('bids', []) if p and a and (p*a > LARGE_ORDER_USD)]
        large_asks = [{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('asks', []) if p and a and (p*a > LARGE_ORDER_USD)]
        if not (large_bids or large_asks):
            print("No large orders found.")
            return
    except Exception as e:
        print(f"Could not fetch order book for {PAIR_TO_SCAN}: {e}")
        return

    state['llm_cooldown'][PAIR_TO_SCAN] = time.time()
    
    top_bids = sorted(large_bids, key=lambda x: x['value_usd'], reverse=True)[:TOP_N_ORDERS_TO_SEND]
    top_asks = sorted(large_asks, key=lambda x: x['value_usd'], reverse=True)[:TOP_N_ORDERS_TO_SEND]
    focused_data = {PAIR_TO_SCAN: {'bids': top_bids, 'asks': top_asks}}
    prompt_data = json.dumps(focused_data, indent=2)
    full_prompt = LLM_PROMPT_MICROSTRUCTURE + "\n\nАНАЛИЗИРУЕМЫЕ ДАННЫЕ:\n" + prompt_data
    
    await broadcast_func(app, f"🧠 Сканер нашел аномалии на {PAIR_TO_SCAN}. Отправляю на анализ LLM...")
    llm_response_content = await ask_llm_func(full_prompt)
    
    if llm_response_content:
        try:
            cleaned_response = llm_response_content.strip().strip('```json').strip('```').strip()
            decision = json.loads(cleaned_response)

            if decision and decision.get("confidence_score", 0) >= 7:
                entry, sl, tp = decision.get("entry_price"), decision.get("sl_price"), decision.get("tp_price")
                if not all(isinstance(v, (int, float)) for v in [entry, sl, tp]) or abs(entry - sl) == 0:
                    return
                
                rr_ratio = abs(tp - entry) / abs(entry - sl)
                if rr_ratio < MIN_RR_RATIO:
                    await broadcast_func(app, f"⚠️ LLM предложил сделку с низким RR ({rr_ratio:.2f}:1). Отклонено.")
                    return
                
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
                
                entry_atr = await get_entry_atr(decision.get("pair"))
                success = await log_trade_to_sheet(trade_log_ws, decision, entry_atr)
                if success:
                    await broadcast_func(app, "✅ Виртуальная сделка успешно залогирована.")
        except Exception as e:
            print(f"Error parsing LLM decision: {e}")

# --- ГЛАВНЫЙ ЦИКЛ ---
async def scanner_main_loop(app, ask_llm_func, broadcast_func, trade_log_ws, state, save_state_func):
    print("Main Engine loop started (v_rest_final).")
    while state.get("bot_on", True):
        try:
            print(f"\n--- Running Main Cycle | Active Trades: {len(state.get('monitored_signals',[]))} ---")
            await monitor_active_trades(app, broadcast_func, trade_log_ws, state, save_state_func)
            
            if len(state.get('monitored_signals', [])) < MAX_PORTFOLIO_SIZE:
                await scan_for_new_opportunities(app, ask_llm_func, broadcast_func, trade_log_ws, state)
            
            print("--- Cycle Finished. Sleeping for 30 seconds. ---")
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            print("Main Engine loop cancelled.")
            break
        except Exception as e:
            print(f"Error in Main Engine loop: {e}")
            await asyncio.sleep(60)
    print("Main Engine loop stopped.")
    await exchange.close()
