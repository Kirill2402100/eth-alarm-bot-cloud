#!/usr/bin/env python3
# ============================================================================
# v5.0 - Range Trading Scanner
# • Реализована логика поиска "пилы" (низкий ADX + широкий канал Боллинджера).
# • Сигнал генерируется при касании нижней границы BB и перепроданности RSI.
# ============================================================================

import os
import asyncio
import json
import logging
from datetime import datetime
import pandas as pd
import ccxt.async_support as ccxt
import pandas_ta as ta # Используем pandas_ta для индикаторов
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# === ENV / Logging ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
# ---> НОВЫЙ ПАРАМЕТР: Список монет для сканирования
COIN_LIST_STR = os.getenv("COIN_LIST", "BTC/USDT,ETH/USDT,SOL/USDT")
COIN_LIST = [coin.strip().upper() for coin in COIN_LIST_STR.split(",")]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# === STATE MANAGEMENT ===
# Запоминаем, когда для монеты был последний сигнал, чтобы не спамить
last_alert_times = {}

# === EXCHANGE (только для данных) ===
exchange = ccxt.mexc()

# === STRATEGY PARAMS ===
TIMEFRAME = '5m'
SCAN_INTERVAL_SECONDS = 60 * 5 # Как часто сканировать монеты (раз в 5 минут)

# Параметры для фильтров
ADX_LEN = 14
ADX_THRESHOLD = 20 # Ищем ADX НИЖЕ этого значения
BBANDS_LEN = 20
BBANDS_STD = 2.0
MIN_BB_WIDTH_PCT = 3.0 # Минимальная ширина канала Боллинджера в %

# Параметры для сигнала
RSI_LEN = 14
RSI_OVERSOLD = 30 # Уровень перепроданности RSI
ATR_LEN_FOR_SL = 14
SL_ATR_MUL = 0.5 # Множитель ATR для стоп-лосса

# === MAIN SCANNER LOOP ===
async def scanner_loop(app):
    while True:
        log.info(f"Starting new scan for {len(COIN_LIST)} coins...")
        for pair in COIN_LIST:
            try:
                # 1. Получаем данные
                ohlcv = await exchange.fetch_ohlcv(pair, timeframe=TIMEFRAME, limit=100)
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                if df.empty: continue

                # 2. Рассчитываем индикаторы с помощью pandas_ta
                df.ta.adx(length=ADX_LEN, append=True)
                df.ta.bbands(length=BBANDS_LEN, std=BBANDS_STD, append=True)
                df.ta.rsi(length=RSI_LEN, append=True)
                df.ta.atr(length=ATR_LEN_FOR_SL, append=True)
                df.dropna(inplace=True) # Удаляем строки, где индикаторы еще не рассчитались

                last = df.iloc[-1]
                
                # --- Логика Сканера ---
                # Фильтр 1: Рынок во флэте? (ADX < порога)
                adx_value = last[f'ADX_{ADX_LEN}']
                is_ranging = adx_value < ADX_THRESHOLD

                # Фильтр 2: Диапазон достаточно широкий?
                bb_upper = last[f'BBU_{BBANDS_LEN}_{BBANDS_STD}']
                bb_lower = last[f'BBL_{BBANDS_LEN}_{BBANDS_STD}']
                bb_width_pct = ((bb_upper - bb_lower) / bb_lower) * 100
                is_wide_enough = bb_width_pct > MIN_BB_WIDTH_PCT

                # Если монета не прошла фильтры, переходим к следующей
                if not (is_ranging and is_wide_enough):
                    continue

                # --- Логика Алертера (если монета прошла фильтры) ---
                rsi_value = last[f'RSI_{RSI_LEN}']
                
                # Условие входа: цена касается нижней границы + RSI перепродан
                if last['close'] <= bb_lower and rsi_value < RSI_OVERSOLD:
                    
                    # Проверка, чтобы не спамить по одной и той же монете
                    now = datetime.now().timestamp()
                    if (now - last_alert_times.get(pair, 0)) < 3600: # Пауза 1 час для одной монеты
                        continue

                    # Расчет параметров сделки
                    entry_price = last['close']
                    take_profit = last[f'BBM_{BBANDS_LEN}_{BBANDS_STD}'] # Средняя линия Боллинджера
                    stop_loss = bb_lower - (last[f'ATRr_{ATR_LEN_FOR_SL}'] * SL_ATR_MUL)

                    # Формирование и отправка сообщения
                    message = (
                        f"🔔 **СИГНАЛ: LONG (Range Trade)**\n\n"
                        f"**Монета:** `{pair}`\n"
                        f"**Текущая цена:** `{entry_price:.4f}`\n\n"
                        f"--- **Параметры сделки** ---\n"
                        f"*Вход:* `~{entry_price:.4f}`\n"
                        f"*Take Profit:* `{take_profit:.4f}` (Средняя BB)\n"
                        f"*Stop Loss:* `{stop_loss:.4f}`\n\n"
                        f"--- **Обоснование** ---\n"
                        f"* Рынок во флэте (ADX: {adx_value:.1f} < {ADX_THRESHOLD})\n"
                        f"* Ширина диапазона: {bb_width_pct:.1f}% (> {MIN_BB_WIDTH_PCT}%)\n"
                        f"* Цена у нижней границы BB + RSI перепродан ({rsi_value:.1f})"
                    )
                    await broadcast_message(app, message)
                    last_alert_times[pair] = now # Обновляем время последнего алерта для этой монеты
            
            except Exception as e:
                log.error(f"Error processing pair {pair}: {e}")

        await asyncio.sleep(SCAN_INTERVAL_SECONDS)

# === COMMANDS and RUN ===
async def broadcast_message(app, text):
    for chat_id in app.chat_ids:
        # MarkdownV2 требует экранирования спецсимволов
        safe_text = text.replace('.', r'\.').replace('-', r'\-').replace('(', r'\(').replace(')', r'\)').replace('>', r'\>').replace('+', r'\+').replace('=', r'\=').replace('*', r'\*').replace('_', r'\_').replace('`', r'\`')
        await app.bot.send_message(chat_id=chat_id, text=safe_text, parse_mode="MarkdownV2")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # Убедимся, что chat_ids существует в контексте приложения
    if not hasattr(ctx.application, 'chat_ids'):
        ctx.application.chat_ids = set()
    ctx.application.chat_ids.add(chat_id)
    
    # Запускаем сканер, если он еще не запущен
    if "scanner_task" not in ctx.application.chat_data or ctx.application.chat_data["scanner_task"].done():
        await update.message.reply_text("✅ Сканер для торговли в 'пиле' запущен.")
        # Сохраняем задачу в контекст, чтобы избежать повторного запуска
        ctx.application.chat_data["scanner_task"] = asyncio.create_task(scanner_loop(ctx.application))
    else:
        await update.message.reply_text("ℹ️ Сканер уже запущен.")


if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    
    log.info("Bot started...")
    app.run_polling()
