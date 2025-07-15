# File: scanner_engine.py (v16 - REST-only Architecture)

import asyncio
import json
import time
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from trade_executor import log_trade_to_sheet, update_trade_in_sheet

# --- Конфигурация ---
SYMBOLS_TO_SCAN = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'DOGE/USDT', 'WLD/USDT', 'PEPE/USDT', 'BNB/USDT']
LARGE_ORDER_USD = 75000 
TOP_N_ORDERS_TO_SEND = 10
MAX_PORTFOLIO_SIZE = 10
MIN_RR_RATIO = 1.5
LLM_COOLDOWN_SECONDS = 300
LLM_PROMPT_MICROSTRUCTURE = """
Ты — ведущий аналитик-квант в HFT-фонде, специализирующийся на анализе микроструктуры рынка (Order Flow, Market Making).

**ТВОЯ ЗАДАЧА:**
Проанализируй предоставленные JSON-данные о состоянии биржевого стакана для ОДНОЙ криптовалютной пары. Данные включают топ-10 крупнейших лимитных заявок ("плит").

1.  **Оцени сетап.** Насколько он надежен для торговли?
2.  **Определи тип алгоритма,** который создает эти плиты (например, "Market-Maker", "Absorption Algorithm").
3.  **Предложи конкретный торговый план,** если сетап достаточно надежен.

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
    if not state.get('monitored_signals'): return
    
    signals_to_remove = []
    try:
        pairs_to_fetch = [s['pair'] for s in state['monitored_signals']]
        all_tickers = await exchange.fetch_tickers(pairs_to_fetch)
    except Exception as e:
        print(f"Monitor: Could not fetch tickers. Error: {e}")
        return

    for signal in state['monitored_signals']:
        ticker = all_tickers.get(signal['pair'])
        if not ticker or not ticker.get('last'): continue
        current_price = ticker['last']
        
        exit_status, exit_price = None, None
        
        if signal['side'] == 'LONG':
            if current_price <= signal['sl_price']: exit_status, exit_price = "SL_HIT", signal['sl_price']
            elif current_price >= signal['tp_price']: exit_status, exit_price = "TP_HIT", signal['tp_price']
        elif signal['side'] == 'SHORT':
            if current_price >= signal['sl_price']: exit_status, exit_price = "SL_HIT", signal['sl_price']
            elif current_price <= signal['tp_price']: exit_status, exit_price = "TP_HIT", signal['tp_price']

        if exit_status:
            position_size_usd, leverage = 50, 100
            price_change_percent = ((exit_price - signal['entry_price']) / signal['entry_price'])
            if signal['side'] == 'SHORT': price_change_percent = -price_change_percent
            pnl_percent = price_change_percent * leverage * 100
            pnl_usd = position_size_usd * (pnl_percent / 100)
            
            await update_trade_in_sheet(trade_log_ws, signal, exit_status, exit_price, pnl_usd, pnl_percent)
            
            emoji = "✅" if pnl_usd > 0 else "❌"
            msg = (f"{emoji} <b>СДЕЛКА ЗАКРЫТА ({exit_status})</b>\n\n"
                   f"<b>Инструмент:</b> <code>{signal['pair']}</code>\n"
                   f"<b>Результат: ${pnl_usd:+.2f} ({pnl_percent:+.2f}%)</b>")
            await broadcast_func(app, msg)
            signals_to_remove.append(signal)

    if signals_to_remove:
        state['monitored_signals'] = [s for s in state['monitored_signals'] if s not in signals_to_remove]
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
    state['llm_cooldown'] = {p: t for p, t in state['llm_cooldown'].items() if (current_time - t) < LLM_COOLDOWN_SECONDS}
    
    active_pairs = {s['pair'] for s in state.get('monitored_signals', [])}
    pairs_to_scan = [s for s in SYMBOLS_TO_SCAN if s not in active_pairs and s not in state['llm_cooldown']]
    if not pairs_to_scan: return

    print(f"Scanning for anomalies in {len(pairs_to_scan)} pairs...")
    market_anomalies = {}
    for pair in pairs_to_scan:
        try:
            order_book = await exchange.fetch_order_book(pair, limit=20)
            large_bids = [{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('bids', []) if p and a and (p*a > LARGE_ORDER_USD)]
            large_asks = [{'price': p, 'value_usd': round(p*a)} for p, a in order_book.get('asks', []) if p and a and (p*a > LARGE_ORDER_USD)]
            if large_bids or large_asks:
                market_anomalies[pair] = {'bids': large_bids, 'asks': large_asks}
        except Exception as e:
            print(f"Could not fetch order book for {pair}: {e}")
            continue

    if not market_anomalies: return

    best_candidate_pair, max_order_value = None, 0
    for pair, anomalies in market_anomalies.items():
        all_orders = anomalies['bids'] + anomalies['asks']
        if not all_orders: continue
        current_max = max(order['value_usd'] for order in all_orders)
        if current_max > max_order_value:
            max_order_value = current_max
            best_candidate_pair = pair
    
    if not best_candidate_pair: return

    state['llm_cooldown'][best_candidate_pair] = time.time()
    
    top_bids = sorted(market_anomalies[best_candidate_pair]['bids'], key=lambda x: x['value_usd'], reverse=True)[:TOP_N_ORDERS_TO_SEND]
    top_asks = sorted(market_anomalies[best_candidate_pair]['asks'], key=lambda x: x['value_usd'], reverse=True)[:TOP_N_ORDERS_TO_SEND]
    focused_data = {best_candidate_pair: {'bids': top_bids, 'asks': top_asks}}
    prompt_data = json.dumps(focused_data, indent=2)
    full_prompt = LLM_PROMPT_MICROSTRUCTURE + "\n\nАНАЛИЗИРУЕМЫЕ ДАННЫЕ:\n" + prompt_data
    
    await broadcast_func(app, f"🧠 Сканер выбрал лучшего кандидата ({best_candidate_pair}). Отправляю на анализ LLM...")
    llm_response_content = await ask_llm_func(full_prompt)
    
    if llm_response_content:
        try:
            # ... (логика обработки ответа LLM, RR-check и вызов log_trade_to_sheet)
        except Exception as e:
            print(f"Error parsing LLM decision: {e}")

# --- ГЛАВНЫЙ ЦИКЛ ---
async def scanner_main_loop(app, ask_llm_func, broadcast_func, trade_log_ws, state, save_state_func):
    print("Main Engine loop started (REST-only).")
    while state.get("bot_on", True):
        try:
            print(f"\n--- Running Main Cycle | Active Trades: {len(state.get('monitored_signals',[]))} ---")
            await monitor_active_trades(app, broadcast_func, trade_log_ws, state, save_state_func)
            
            if len(state.get('monitored_signals', [])) < MAX_PORTFOLIO_SIZE:
                await scan_for_new_opportunities(app, ask_llm_func, broadcast_func, trade_log_ws, state)
            
            print("--- Cycle Finished. Sleeping for 25 seconds. ---")
            await asyncio.sleep(25)
        except asyncio.CancelledError:
            print("Main Engine loop cancelled.")
            break
        except Exception as e:
            print(f"Error in Main Engine loop: {e}")
            await asyncio.sleep(60)
    print("Main Engine loop stopped.")
    await exchange.close()
