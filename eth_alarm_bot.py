# eth_alarm_bot.py
import os, asyncio, json, logging, math, time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import ccxt.async_support as ccxt                      # ← асинхронный
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes,
    Defaults
)

###############################################################################
# Константы / переменные окружения
###############################################################################
BOT_TOKEN      = os.getenv("BOT_TOKEN")
CHAT_IDS       = {int(cid) for cid in os.getenv("CHAT_IDS", "0").split(",") if cid}
PAIR_RAW       = os.getenv("PAIR", "BTC-USDT-SWAP")      # «сырой» вид от пользователя
SHEET_ID       = os.getenv("SHEET_ID")
INIT_LEVERAGE  = int(os.getenv("LEVERAGE", 1))

###############################################################################
# Логирование
###############################################################################
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("bot")

###############################################################################
# Google Sheets helper
###############################################################################
_GS_SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
# Проверяем, есть ли учетные данные, чтобы избежать ошибок при локальном запуске
if os.getenv("GOOGLE_CREDENTIALS"):
    creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    _creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, _GS_SCOPE)
    _gs = gspread.authorize(_creds)
else:
    _gs = None
    log.warning("GOOGLE_CREDENTIALS not set. Google Sheets logging is disabled.")


def _open_worksheet(sheet_id: str, title: str):
    if not _gs: return None
    ss = _gs.open_by_key(sheet_id)
    try:
        ws = ss.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title, rows=1000, cols=20)
    return ws

HEADERS = ["DATE-TIME", "POSITION", "DEPOSIT", "ENTRY", "STOP LOSS",
           "TAKE PROFIT", "RR", "P&L (USDT)", "APR (%)"]
if SHEET_ID:
    WS = _open_worksheet(SHEET_ID, "AI")
    if WS and WS.row_values(1) != HEADERS:
        WS.clear()
        WS.append_row(HEADERS)
else:
    WS = None
    log.warning("SHEET_ID not set. Google Sheets logging is disabled.")


###############################################################################
# Биржа OKX
###############################################################################
exchange = ccxt.okx({
    "apiKey":   os.getenv("OKX_API_KEY"),
    "secret":   os.getenv("OKX_SECRET"),
    "password": os.getenv("OKX_PASSWORD"),
    "options":  {"defaultType": "swap"},          # гарантируем бессрочные фьючи
    "enableRateLimit": True,
})

# Нормализуем PAIR из переменной окружения
if PAIR_RAW:
    PAIR = PAIR_RAW.replace("/", "-").replace(":USDT", "").upper()
    if "-SWAP" not in PAIR:
        PAIR += "-SWAP"
else:
    # Устанавливаем значение по умолчанию, если переменная окружения пуста
    PAIR = "BTC-USDT-SWAP"

log.info(f"Using trading pair: {PAIR}")


###############################################################################
# Стратегия: SSL-канал 13 + ценовое подтверждение + RSI
###############################################################################
def _calc_rsi(series: pd.Series, length=14):
    delta = series.diff()
    gain = (delta.clip(lower=0)).rolling(window=length).mean()
    loss = (-delta.clip(upper=0)).rolling(window=length).mean()
    rs   = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_ssl(df: pd.DataFrame):
    sma = df['close'].rolling(13).mean()
    ssl_up, ssl_dn = [], []
    for i in range(len(df)):
        if i < 12:
            ssl_up.append(None); ssl_dn.append(None); continue
        high_max = df['high'].iloc[i-12:i+1].max()
        low_min  = df['low'].iloc[i-12:i+1].min()
        if df['close'].iloc[i] > sma.iloc[i]:
            ssl_up.append(high_max); ssl_dn.append(low_min)
        else:
            ssl_up.append(low_min);  ssl_dn.append(high_max)
    df['ssl_up'], df['ssl_dn'] = ssl_up, ssl_dn
    df['ssl_sig'] = None
    for i in range(1, len(df)):
        if pd.notna(df['ssl_up'].iloc[i]) and pd.notna(df['ssl_dn'].iloc[i-1]):
            if df['ssl_up'].iloc[i-1] < df['ssl_dn'].iloc[i-1] and df['ssl_up'].iloc[i] > df['ssl_dn'].iloc[i]:
                df.at[df.index[i], 'ssl_sig'] = "LONG"
            elif df['ssl_up'].iloc[i-1] > df['ssl_dn'].iloc[i-1] and df['ssl_up'].iloc[i] < df['ssl_dn'].iloc[i]:
                df.at[df.index[i], 'ssl_sig'] = "SHORT"
    df['rsi'] = _calc_rsi(df['close'])
    return df

###############################################################################
# Текущее состояние стратегии
###############################################################################
state = {
    "monitoring": False,
    "current_sig": None,      # LONG | SHORT | None
    "last_cross":  None,      # datetime
    "leverage":    INIT_LEVERAGE,
    "position":    None,      # dict | None
}

###############################################################################
# Telegram-bot: Команды и функции
###############################################################################

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Включает мониторинг и добавляет пользователя в список рассылки."""
    ctx.application.chat_ids.add(update.effective_chat.id)
    state["monitoring"] = True
    await update.message.reply_text("✅ Monitoring ON")
    if not ctx.chat_data.get("task"):
        ctx.chat_data["task"] = asyncio.create_task(monitor(ctx))

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Выключает мониторинг."""
    state["monitoring"] = False
    await update.message.reply_text("⛔ Monitoring OFF")

async def cmd_leverage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Устанавливает кредитное плечо."""
    arg = update.message.text.split(maxsplit=1)
    if len(arg) != 2 or not arg[1].isdigit():
        await update.message.reply_text("Использование: <code>/leverage 3</code>")
        return
    lev = int(arg[1])
    state["leverage"] = max(1, min(100, lev))
    await update.message.reply_text(f"🛠 Leverage set ↦ {state['leverage']}x")

async def monitor(ctx: ContextTypes.DEFAULT_TYPE):
    """Основной цикл, который получает данные с биржи и ищет сигналы."""
    log.info("monitor() loop started")
    while True:
        if not state["monitoring"]:
            await asyncio.sleep(2); continue
        try:
            ohlcv = await exchange.fetch_ohlcv(PAIR, timeframe="15m", limit=150)
            df = pd.DataFrame(ohlcv, columns=['ts','open','high','low','close','vol'])
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df = calculate_ssl(df)
            sigs = df.dropna(subset=['ssl_sig'])
            if len(sigs) >= 2 and sigs.iloc[-1]['ssl_sig'] != sigs.iloc[-2]['ssl_sig']:
                sig = sigs.iloc[-1]['ssl_sig']
                price = df['close'].iloc[-1]
                rsi   = df['rsi'].iloc[-1]
                cond_price = (price >= (1.002 * df['close'].iloc[-2])) if sig=="LONG" else (price <= 0.998 * df['close'].iloc[-2])
                cond_rsi   = (rsi > 55) if sig=="LONG" else (rsi < 45)
                if cond_price and cond_rsi:
                    await send_signal(ctx, sig, price, rsi)
        except ccxt.NetworkError as e:
            log.error("Network error during fetch_ohlcv: %s", e)
        except ccxt.ExchangeError as e:
            log.error("Exchange error: %s. Is the pair '%s' correct?", e, PAIR)
        except Exception as e:
            log.exception("monitor-loop error: %s", e)
        await asyncio.sleep(30)

async def send_signal(ctx: ContextTypes.DEFAULT_TYPE, sig: str, price: float, rsi: float):
    """Отправляет сообщение с сигналом всем активным пользователям."""
    txt = (f"📡 <b>Signal → {sig}</b>\n"
           f"💰 Price: <code>{price:.2f}</code>\n"
           f"📈 RSI: {rsi:.1f}\n"
           f"⏰ {datetime.utcnow().strftime('%H:%M:%S UTC')}")
    for cid in ctx.application.chat_ids:
        try:
            await ctx.application.bot.send_message(cid, txt)
        except Exception as e:
            log.warning("send_signal: %s", e)

async def post_shutdown_hook(application: Application):
    """Эта функция будет вызвана при остановке бота для освобождения ресурсов."""
    log.info("Closing exchange connection...")
    await exchange.close()
    log.info("Exchange connection closed.")

###############################################################################
# Точка входа
###############################################################################
async def main():
    """Основная функция для настройки и запуска бота."""
    try:
        await exchange.load_markets()
        log.info("Markets loaded successfully.")
    except Exception as e:
        log.warning(f"Could not load all markets from OKX, but proceeding anyway. Error: {e}")

    try:
        bal = await exchange.fetch_balance()
        usdt_balance = bal['total'].get('USDT', 'N/A')
        log.info(f"USDT balance: {usdt_balance}")
    except Exception as e:
        log.error(f"Could not fetch balance. The bot will continue to run. Error: {e}")
    
    defaults = Defaults(parse_mode="HTML")
    
    # Регистрируем хук завершения работы прямо при сборке приложения
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .defaults(defaults)
        .post_shutdown(post_shutdown_hook)
        .build()
    )

    # Инициализируем пользовательский атрибут и добавляем ID из переменных окружения
    app.chat_ids = set() 
    app.chat_ids.update(CHAT_IDS)

    # Регистрируем хэндлеры
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    app.add_handler(CommandHandler("leverage", cmd_leverage))

    # Запускаем бота
    log.info("Bot is starting polling...")
    await app.run_polling()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot shutdown requested by user.")
    except Exception as e:
        log.exception("Bot crashed with a fatal error: %s", e)
    finally:
        log.info("Bot process has terminated.")
