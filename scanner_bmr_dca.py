from __future__ import annotations
import asyncio, time, logging, json, os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
from telegram.ext import Application
import gspread

import trade_executor

log = logging.getLogger("bmr_dca_engine")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
class CONFIG:
    SYMBOL = "EURC/USDT:USDT"
    TF_ENTRY = "5m"
    TF_RANGE = "1h"
    RANGE_LOOKBACK_DAYS = 90
    FETCH_TIMEOUT = 8
    BASE_STEP_MARGIN = 10.0
    DCA_LEVELS = 5
    DCA_GROWTH = 2.0
    LEVERAGE = 20
    FEE_MAKER = 0.0005
    FEE_TAKER = 0.0005
    Q_LOWER = 0.025
    Q_UPPER = 0.975
    RANGE_MIN_ATR_MULT = 1.5
    RSI_LEN = 14
    ADX_LEN = 14
    VOL_WIN = 50
    WEIGHTS = {
        "border": 0.45, "rsi": 0.15, "ema_dev": 0.20,
        "supertrend": 0.10, "vol": 0.10
    }
    SCORE_THR = 0.55
    ATR_MULTS = [1, 2, 3, 4, 5]
    TP_PCT = 0.010
    TRAIL_ARM = 0.8
    TRAIL_LOCK = 0.6
    SCAN_INTERVAL_SEC = 3
    REBUILD_RANGE_EVERY_MIN = 15

# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------
def fmt(p: float) -> str:
    if p is None: return "N/A"
    if p < 0.01: return f"{p:.6f}"
    if p < 1.0:  return f"{p:.5f}"
    return f"{p:.4f}"

def approx_liq_price(entry: float, side: str, lev: int) -> float:
    k = 1.0/lev
    return entry * (1 - k) if side == "LONG" else entry * (1 + k)

def chandelier_stop(side: str, price: float, atr: float, mult: float = 3.0):
    if side == "LONG": return price - mult*atr
    else: return price + mult*atr

def next_dca_price(side: str, avg: float, atr5m: float, level_idx: int):
    step = CONFIG.ATR_MULTS[level_idx] * atr5m
    return (avg - step) if side == "LONG" else (avg + step)

def weighted_score(side: str, px: float, rng: dict, ind: dict) -> float:
    lower, upper = rng["lower"], rng["upper"]
    width = max(upper - lower, 1e-9)
    if side == "LONG":
        border = 1.0 - max(0.0, min(1.0, (px - lower) / (0.20*width)))
    else:
        border = max(0.0, min(1.0, (upper - px) / (0.20*width)))
    rsi_term = (50 - ind["rsi"]) / 50.0 if side=="LONG" else (ind["rsi"]-50)/50.0
    ema_dev = min(ind["ema_dev_atr"]/2.0, 1.0)
    st_term = 1.0 if ((side=="LONG" and ind["supertrend"]=="down_to_up_near") or
                      (side=="SHORT" and ind["supertrend"]=="up_to_down_near")) else 0.0
    vol_term = max(0.0, min((ind["vol_z"]-0.6)/1.0, 1.0))
    w = CONFIG.WEIGHTS
    score = (w["border"]*border + w["rsi"]*rsi_term + w["ema_dev"]*ema_dev +
             w["supertrend"]*st_term + w["vol"]*vol_term)
    return max(0.0, min(1.0, score))

# ---------------------------------------------------------------------------
# Core Logic Functions
# ---------------------------------------------------------------------------
async def build_range(exchange, symbol: str):
    limit_h = min(int(CONFIG.RANGE_LOOKBACK_DAYS * 24), 1500)
    ohlc = await asyncio.wait_for(exchange.fetch_ohlcv(symbol, CONFIG.TF_RANGE, limit=limit_h),
                                  timeout=CONFIG.FETCH_TIMEOUT)
    df = pd.DataFrame(ohlc, columns=["ts","open","high","low","close","volume"])
    ema = ta.ema(df["close"], length=50)
    atr = ta.atr(df["high"], df["low"], df["close"], length=14)
    lower = float(np.quantile(df["close"].dropna(), CONFIG.Q_LOWER))
    upper = float(np.quantile(df["close"].dropna(), CONFIG.Q_UPPER))
    if pd.notna(ema.iloc[-1]) and pd.notna(atr.iloc[-1]):
        mid = float(ema.iloc[-1])
        atr1h = float(atr.iloc[-1])
        lower = min(lower, mid - CONFIG.RANGE_MIN_ATR_MULT*atr1h)
        upper = max(upper, mid + CONFIG.RANGE_MIN_ATR_MULT*atr1h)
    else:
        atr1h = 0.0
        mid = float(df["close"].iloc[-1])
    return {"lower": lower, "upper": upper, "mid": mid, "atr1h": atr1h, "width": upper-lower}

def compute_indicators_5m(df: pd.DataFrame) -> dict:
    atr5m = ta.atr(df["high"], df["low"], df["close"], length=14).iloc[-1]
    rsi = ta.rsi(df["close"], length=CONFIG.RSI_LEN).iloc[-1]
    adx = ta.adx(df["high"], df["low"], df["close"], length=CONFIG.ADX_LEN)["ADX_14"].iloc[-1]
    ema20 = ta.ema(df["close"], length=20).iloc[-1]
    vol_z = (df["volume"].iloc[-1] - df["volume"].rolling(CONFIG.VOL_WIN).mean().iloc[-1]) / \
            max(df["volume"].rolling(CONFIG.VOL_WIN).std().iloc[-1], 1e-9)
    st = ta.supertrend(df["high"], df["low"], df["close"], length=10, multiplier=3.0)
    st_up = st["SUPERT_10_3.0"].iloc[-1]
    st_dir = "up" if df["close"].iloc[-1] > st_up else "down"
    st_prev = "up" if df["close"].iloc[-2] > st["SUPERT_10_3.0"].iloc[-2] else "down"
    st_state = "up_to_down_near" if st_prev=="up" and st_dir=="down" else \
               "down_to_up_near" if st_prev=="down" and st_dir=="up" else st_dir
    ema_dev_atr = abs(df["close"].iloc[-1] - ema20) / max(float(atr5m), 1e-9)
    return {
        "atr5m": float(atr5m), "rsi": float(rsi), "adx": float(adx),
        "ema20": float(ema20), "vol_z": float(vol_z),
        "ema_dev_atr": float(ema_dev_atr), "supertrend": st_state
    }

# ---------------------------------------------------------------------------
# Position State Manager
# ---------------------------------------------------------------------------
class Position:
    def __init__(self, side: str, signal_id: str):
        self.side = side
        self.signal_id = signal_id
        self.steps_filled = 0
        self.step_margins = []
        self.qty = 0.0
        self.avg = 0.0
        self.tp_pct = CONFIG.TP_PCT
        self.tp_price = 0.0
        self.sl_price = None
        self.open_ts = time.time()

    def plan_margins(self):
        m = CONFIG.BASE_STEP_MARGIN
        self.step_margins = [m*(CONFIG.DCA_GROWTH**i) for i in range(CONFIG.DCA_LEVELS)]

    def add_step(self, price: float):
        margin = self.step_margins[self.steps_filled]
        notional = margin * CONFIG.LEVERAGE
        new_qty = notional / max(price,1e-9)
        self.avg = (self.avg*self.qty + price*new_qty) / max(self.qty+new_qty,1e-9) if self.qty>0 else price
        self.qty += new_qty
        self.steps_filled += 1
        self.tp_price = self.avg*(1+self.tp_pct) if self.side=="LONG" else self.avg*(1-self.tp_pct)
        return margin, notional

    def next_dca_price(self, atr5m: float):
        if self.steps_filled >= CONFIG.DCA_LEVELS: return None
        return next_dca_price(self.side, self.avg, atr5m, self.steps_filled)

# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------
async def scanner_main_loop(app: Application, broadcast):
    log.info("BMR-DCA loop starting…")
    app.bot_data.setdefault("position", None)
    app.bot_data.setdefault("last_range_build", 0.0)
    app.bot_data['broadcast_func'] = broadcast

    try:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        sheet_key  = os.environ.get("SHEET_ID")
        if creds_json and sheet_key:
            gc = gspread.service_account_from_dict(json.loads(creds_json))
            sheet = gc.open_by_key(sheet_key)
            await trade_executor.ensure_bmr_log_sheet(sheet, title="BMR_DCA_Log")
    except Exception as e:
        log.error(f"Sheets init error: {e}", exc_info=True)

    exchange = ccxt.mexc({'options': {'defaultType': 'swap'}, 'enableRateLimit': True, 'rateLimit': 150})
    await exchange.load_markets(True)
    
    symbol = CONFIG.SYMBOL
    if symbol not in exchange.markets:
        log.critical(f"Символ {symbol} не найден у биржи. Проверьте, что EURC/USDT swap доступен на выбранной бирже.")
        await exchange.close()
        return

    rng = None
    last_flush = 0

    while app.bot_data.get("bot_on", False):
        try:
            now = time.time()
            manage_only = app.bot_data.get("scan_paused", False)

            if (not rng) or (now - app.bot_data.get("last_range_build", 0) > CONFIG.REBUILD_RANGE_EVERY_MIN*60):
                rng = await build_range(exchange, symbol)
                # ИСПРАВЛЕНО: Обновляем метку времени после перестроения диапазона
                app.bot_data["last_range_build"] = now
                log.info(f"[RANGE] lower={fmt(rng['lower'])} upper={fmt(rng['upper'])} width={fmt(rng['width'])}")

            ohlc5 = await asyncio.wait_for(exchange.fetch_ohlcv(symbol, CONFIG.TF_ENTRY, limit=max(60, CONFIG.VOL_WIN+CONFIG.ADX_LEN+20)),
                                           timeout=CONFIG.FETCH_TIMEOUT)
            df5 = pd.DataFrame(ohlc5, columns=["ts","open","high","low","close","volume"])
            ind = compute_indicators_5m(df5)
            px = float(df5["close"].iloc[-1])

            if not manage_only:
                long_zone  = px <= rng["lower"] * (1+0.0015)
                short_zone = px >= rng["upper"] * (1-0.0015)
                side_cand = "LONG" if long_zone else "SHORT" if short_zone else None
                
                if side_cand and app.bot_data.get("position") is None:
                    sc = weighted_score(side_cand, px, rng, ind)
                    if sc >= CONFIG.SCORE_THR:
                        pos = Position(side_cand, signal_id=f"{symbol.split('/')[0]}_{int(now)}")
                        pos.plan_margins()
                        margin, _ = pos.add_step(px)
                        app.bot_data["position"] = pos
                        
                        liq = approx_liq_price(pos.avg, pos.side, CONFIG.LEVERAGE)
                        nxt = pos.next_dca_price(ind["atr5m"])
                        nxt_txt = fmt(nxt)
                        cum_margin = sum(pos.step_margins[:pos.steps_filled])
                        fee_est = margin * CONFIG.LEVERAGE * CONFIG.FEE_TAKER
                        next_mult = CONFIG.ATR_MULTS[pos.steps_filled] if pos.steps_filled < CONFIG.DCA_LEVELS else ""

                        if bc := app.bot_data.get('broadcast_func'):
                            await bc(app,
                                f"⚡ <b>BMR-DCA {pos.side} ({symbol.split('/')[0]})</b>\n"
                                f"Вход: <code>{fmt(px)}</code>\n"
                                f"Шаговая маржа: <b>{margin:.2f} USDT</b> | Депозит: <b>{cum_margin:.2f} USDT</b> | Плечо: <b>{CONFIG.LEVERAGE}x</b>\n"
                                f"TP: <code>{fmt(pos.tp_price)}</code> (+{CONFIG.TP_PCT*100:.2f}%)\n"
                                f"Ликвидация≈ <code>{fmt(liq)}</code>\n"
                                f"След. усреднение: <code>{nxt_txt}</code>"
                            )
                        await trade_executor.bmr_log_event({
                            "Event_ID": f"OPEN_{pos.signal_id}", "Signal_ID": pos.signal_id,
                            "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            "Pair": symbol, "Side": pos.side, "Event": "OPEN",
                            "Step_No": pos.steps_filled, "Step_Margin_USDT": margin,
                            "Cum_Margin_USDT": cum_margin, "Leverage": CONFIG.LEVERAGE,
                            "Entry_Price": px, "Avg_Price": pos.avg,
                            "TP_Pct": CONFIG.TP_PCT, "TP_Price": pos.tp_price, "Liq_Est_Price": liq,
                            "Next_DCA_ATR_mult": next_mult, "Next_DCA_Price": nxt or "",
                            "Fee_Rate_Maker": CONFIG.FEE_MAKER, "Fee_Rate_Taker": CONFIG.FEE_TAKER,
                            "Fee_Est_USDT": fee_est,
                            "ATR_5m": ind["atr5m"], "ATR_1h": rng["atr1h"],
                            "RSI_5m": ind["rsi"], "ADX_5m": ind["adx"], "Supertrend": ind["supertrend"], "Vol_z": ind["vol_z"],
                            "Range_Lower": rng["lower"], "Range_Upper": rng["upper"], "Range_Width": rng["width"]
                        })

            pos: Position | None = app.bot_data.get("position")
            if pos:
                if not manage_only:
                    nxt = pos.next_dca_price(ind["atr5m"])
                    if nxt and ((pos.side=="LONG" and px <= nxt) or (pos.side=="SHORT" and px >= nxt)):
                        margin, _ = pos.add_step(px)
                        liq = approx_liq_price(pos.avg, pos.side, CONFIG.LEVERAGE)
                        nxt2 = pos.next_dca_price(ind["atr5m"])
                        nxt2_txt = fmt(nxt2)
                        cum_margin = sum(pos.step_margins[:pos.steps_filled])
                        fee_est = margin * CONFIG.LEVERAGE * CONFIG.FEE_TAKER
                        next_mult = CONFIG.ATR_MULTS[pos.steps_filled] if pos.steps_filled < CONFIG.DCA_LEVELS else ""
                        
                        if bc := app.bot_data.get('broadcast_func'):
                            await bc(app,
                                f"➕ Усреднение #{pos.steps_filled}: {margin:.2f} USDT\n"
                                f"Новый депозит: <b>{cum_margin:.2f} USDT</b>\n"
                                f"Новая средняя: <code>{fmt(pos.avg)}</code> | Новый TP: <code>{fmt(pos.tp_price)}</code>\n"
                                f"Новая ликвидация≈ <code>{fmt(liq)}</code>\n"
                                f"След. усреднение: <code>{nxt2_txt}</code>"
                            )
                        await trade_executor.bmr_log_event({
                            "Event_ID": f"ADD_{pos.signal_id}_{pos.steps_filled}", "Signal_ID": pos.signal_id,
                            "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            "Pair": symbol, "Side": pos.side, "Event": "ADD",
                            "Step_No": pos.steps_filled, "Step_Margin_USDT": margin,
                            "Cum_Margin_USDT": cum_margin,
                            "Entry_Price": px, "Avg_Price": pos.avg,
                            "TP_Price": pos.tp_price, "SL_Price": pos.sl_price or "",
                            "Liq_Est_Price": liq,
                            "Next_DCA_ATR_mult": next_mult, "Next_DCA_Price": nxt2 or "",
                            "Fee_Rate_Maker": CONFIG.FEE_MAKER, "Fee_Rate_Taker": CONFIG.FEE_TAKER,
                            "Fee_Est_USDT": fee_est,
                            "ATR_5m": ind["atr5m"], "ATR_1h": rng["atr1h"],
                            "RSI_5m": ind["rsi"], "ADX_5m": ind["adx"], "Supertrend": ind["supertrend"], "Vol_z": ind["vol_z"],
                            "Range_Lower": rng["lower"], "Range_Upper": rng["upper"], "Range_Width": rng["width"]
                        })

                if pos.side == "LONG":
                    gain_to_tp = max(0.0, (px / max(pos.avg,1e-9) - 1.0) / CONFIG.TP_PCT)
                else:
                    gain_to_tp = max(0.0, (pos.avg / max(px,1e-9) - 1.0) / CONFIG.TP_PCT)

                if gain_to_tp >= CONFIG.TRAIL_ARM:
                    lock_pct = CONFIG.TRAIL_LOCK * CONFIG.TP_PCT
                    locked = pos.avg*(1+lock_pct) if pos.side=="LONG" else pos.avg*(1-lock_pct)
                    chand = chandelier_stop(pos.side, px, ind["atr5m"])
                    new_sl = max(locked, chand) if pos.side=="LONG" else min(locked, chand)
                    if (pos.sl_price is None) or (pos.side=="LONG" and new_sl>pos.sl_price) or (pos.side=="SHORT" and new_sl<pos.sl_price):
                        pos.sl_price = new_sl
                        if bc := app.bot_data.get('broadcast_func'):
                            await bc(app, f"🔒 Трейлинг-SL установлен: <code>{fmt(pos.sl_price)}</code>")
                        await trade_executor.bmr_log_event({
                            "Event_ID": f"TRAIL_SET_{pos.signal_id}_{int(now)}", "Signal_ID": pos.signal_id,
                            "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            "Pair": symbol, "Side": pos.side, "Event": "TRAIL_SET",
                            "SL_Price": pos.sl_price, "Avg_Price": pos.avg
                        })

                tp_hit = (pos.side=="LONG" and px>=pos.tp_price) or (pos.side=="SHORT" and px<=pos.tp_price)
                sl_hit = pos.sl_price and ((pos.side=="LONG" and px<=pos.sl_price) or (pos.side=="SHORT" and px>=pos.sl_price))

                if tp_hit or sl_hit:
                    reason = "TP_HIT" if tp_hit else "SL_HIT"
                    exit_p = pos.tp_price if tp_hit else pos.sl_price
                    time_min = (time.time()-pos.open_ts)/60.0
                    pnl_pct = (abs(exit_p/pos.avg-1) * (1 if tp_hit else -1)) * 100 * CONFIG.LEVERAGE
                    pnl_usd = sum(pos.step_margins[:pos.steps_filled]) * (pnl_pct/100.0)
                    atr_now = ind["atr5m"]
                    if bc := app.bot_data.get('broadcast_func'):
                        await bc(app, f"{'✅' if tp_hit else '❌'} <b>{reason}</b>\n"
                                      f"Цена выхода: <code>{fmt(exit_p)}</code>\n"
                                      f"P&L≈ {pnl_usd:+.2f} USDT ({pnl_pct:+.2f}%)\n"
                                      f"ATR(5m): {atr_now:.6f}\n"
                                      f"Время в сделке: {time_min:.1f} мин")
                    await trade_executor.bmr_log_event({
                        "Event_ID": f"{reason}_{pos.signal_id}", "Signal_ID": pos.signal_id,
                        "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "Pair": symbol, "Side": pos.side, "Event": reason,
                        "PNL_Realized_USDT": pnl_usd, "PNL_Realized_Pct": pnl_pct,
                        "Time_In_Trade_min": time_min,
                        "ATR_5m": atr_now
                    })
                    app.bot_data["position"] = None

            if trade_executor.PENDING_TRADES and (time.time() - last_flush >= 10):
                await trade_executor.flush_log_buffers()
                last_flush = time.time()
            
            await asyncio.sleep(CONFIG.SCAN_INTERVAL_SEC)
        except Exception:
            log.exception("BMR-DCA loop error")
            await asyncio.sleep(5)

    await exchange.close()
    log.info("BMR-DCA loop gracefully stopped.")
