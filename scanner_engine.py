# File: scanner_engine.py (ТЕСТОВАЯ ВЕРСИЯ "ПУСТЫШКА")

import asyncio

print("--- EXECUTING DUMMY SCANNER ENGINE (HANG TEST) ---")

async def scanner_main_loop(app, ask_llm_func, broadcast_func, trade_log_ws, state, save_state_func):
    """
    Это тестовая версия-пустышка. Она ничего не делает,
    кроме как пишет в лог каждые 30 секунд, чтобы показать, что она жива.
    """
    print("Dummy scanner main loop initiated successfully.")
    cycle_count = 0
    while True:
        cycle_count += 1
        print(f"Dummy scanner cycle {cycle_count} is running...")
        await asyncio.sleep(30)
