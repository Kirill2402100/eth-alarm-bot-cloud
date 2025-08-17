from __future__ import annotations
import asyncio, time, logging, json, os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
import ccxt as ccxt_sync
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
    RANGE_LOOKBACK_DAYS = 60
    FETCH_TIMEOUT = 15
    BASE_STEP_MARGIN = 10.0
    DCA_LEVELS = 6
    DCA_GROWTH = 2.0
    # ИЗМЕНЕНО: Плечо зафиксировано на 50x
    LEVERAGE = 50
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
    ATR_MULTS = [0, 1, 2, 3, 4, 5]
    TP_PCT = 0.010
    # ИЗМЕНЕНО: Новые пороги для трейлинга
    TRAIL_ARM = 0.6
    TRAIL_LOCK = 0.5
    SCAN_INTERVAL_SEC = 3
    REBUILD_RANGE_EVERY_MIN = 15
    SAFETY_BANK_USDT = 1000.0
    # ИЗМЕНЕНО: Увеличена доля банка
    CUM_DEPOSIT_FRAC_AT_FULL = 0.64
    AUTO_LEVERAGE = False
    MIN_LEVERAGE = 2
    MAX_LEVERAGE = 50
    BREAK_EPS = 0.0025
    REENTRY_BAND = 0.003
    MAINT_MMR = 0.004
    LIQ_FEE_BUFFER = 1.0
    SL_NOTIFY_MIN_TICK_STEP = 1

assert len(CONFIG.ATR_MULTS) >= CONFIG.DCA_LEVELS, "ATR_MULTS must cover all DCA levels"

# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------
def fmt(p: float) -> str:
    if p is None or pd.isna(p): return "N/A"
    if p < 0.01: return f"{p:.6f}"
    if p < 1.0:  return f"{p:.5f}"
    return f"{p:.4f}"

def plan_margins_bank_first(bank: float, levels: int, growth: float) -> list[float]:
    if levels <= 0 or bank <= 0: return []
    if abs(growth - 1.0) < 1e-9:
        per = bank / levels
        return [per] * levels
    base = bank * (growth - 1.0) / (growth**levels - 1.0)
    return [base * (growth**i) for i in range(levels)]

def approx_liq_price_cross(avg: float, side: str, qty: float, equity: float, mmr: float, fees_paid: float = 0.0) -> float:
    if qty <= 0 or equity <= 0:
        return float('nan')
    eq = max(0.0, equity - fees_paid)
    if side == "LONG":
        denom = max(qty * (1.0 - mmr), 1e-12)
        return (avg * qty - eq) / denom
    else:
        denom = max(qty * (1.0 + mmr), 1e-12)
        return (avg * qty + eq) / denom

def liq_distance_pct(side: str, px: float, liq: float) -> float:
    if px is None or liq is None or px <= 0 or np.isnan(liq):
        return float('nan')
    return (liq / px - 1.0) * 100 if side == "SHORT" else (1.0 - liq / px) * 100

def trend_reversal_confirmed(side: str, ind: dict) -> bool:
    return (side=="SHORT" and ind["supertrend"] in ("up_to_down_near","down")) or \
           (side=="LONG"  and ind["supertrend"] in ("down_to_up_near","up"))

async def fetch_ohlcv_safe(exchange, symbol, timeframe, limit, retries=3, timeout=None):
    for attempt in range(retries):
        try:
            return await asyncio.wait_for(
                exchange.fetch_ohlcv(symbol, timeframe, limit=limit),
                timeout or CONFIG.FETCH_TIMEOUT
            )
        except (asyncio.TimeoutError, ccxt_sync.RequestTimeout, ccxt_sync.NetworkError,
                ccxt_sync.DDoSProtection, ccxt_sync.ExchangeNotAvailable) as e:
            wait = 1.5 ** attempt
            log.warning(f"OHLCV timeout {symbol} {timeframe} lim={limit} "
                        f"try {attempt+1}/{retries}: {e}; retry in {wait:.1f}s")
            await asyncio.sleep(wait)
    small = max(240, min(500, (limit or 500)//2))
    try:
        log.warning(f"Retries failed for limit={limit}. Falling back to limit={small}.")
        return await asyncio.wait_for(
            exchange.fetch_ohlcv(symbol, timeframe, limit=small),
            (timeout or CONFIG.FETCH_TIMEOUT) * 2
        )
    except Exception as e:
        log.error(f"OHLCV final fail {symbol} {timeframe}: {e}")
        return None

def chandelier_stop(side: str, price: float, atr: float, mult: float = 3.0):
    if side == "LONG": return price - mult*atr
    else: return price + mult*atr

def next_dca_price(side: str, avg: float, atr5m: float, level_idx: int):
    step = CONFIG.ATR_MULTS[level_idx] * atr5m
    return (avg - step) if side == "LONG" else (avg + step)

def break_levels(rng: dict) -> tuple[float, float]:
    up = rng["upper"] * (1.0 + CONFIG.BREAK_EPS)
    dn = rng["lower"] * (1.0 - CONFIG.BREAK_EPS)
    return up, dn

def break_distance_pcts(px: float, up: float, dn: float) -> tuple[float, float]:
    if px is None or px <= 0 or any(v is None or np.isnan(v) for v in (up, dn)):
        return float('nan'), float('nan')
    up_pct = max(0.0, (up / px - 1.0) * 100.0)
    dn_pct = max(0.0, (1.0 - dn / px) * 100.0)
    return up_pct, dn_pct

def cap_next_price(side: str, avg: float, target_raw: float | None, atr5m: float, rng: dict) -> tuple[float | None, bool, bool]:
    if target_raw is None or np.isnan(target_raw):
        return None, False, False
    brk_up, brk_dn = break_levels(rng)
    buf = max(1e-9, 0.05 * max(atr5m, 1e-9))
    if side == "SHORT":
        cap_price = brk_up - buf
        if cap_price <= avg * (1.0 + 1e-9):
            return None, False, True
        clipped = target_raw > cap_price
        return (min(target_raw, cap_price), clipped, False)
    else:
        cap_price = brk_dn + buf
        if cap_price >= avg * (1.0 - 1e-9):
            return None, False, True
        clipped = target_raw < cap_price
        return (max(target_raw, cap_price), clipped, False)

def sl_moved_enough(prev: float|None, new: float, side: str, tick: float, min_steps: int) -> bool:
    if prev is None:
        return True
    step = max(tick * max(1, min_steps), 1e-12)
    return (new > prev + step) if side == "LONG" else (new < prev - step)

def quantize_to_tick(x: float | None, tick: float) -> float | None:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return x
    return round(round(x / tick) * tick, 10)

# ---------------------------------------------------------------------------
# Core Logic Functions
# ---------------------------------------------------------------------------
async def build_range(exchange, symbol: str):
    limit_h = min(int(CONFIG.RANGE_LOOKBACK_DAYS * 24), 1500)
    ohlc = await fetch_ohlcv_safe(exchange, symbol, CONFIG.TF_RANGE, limit_h)
    if not ohlc: return None
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
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=CONFIG.ADX_LEN)
    adx_cols = adx_df.filter(like=f"ADX_{CONFIG.ADX_LEN}").columns
    if len(adx_cols) > 0:
        adx = adx_df[adx_cols[0]].iloc[-1]
    else:
        raise ValueError(f"Could not find ADX column for length {CONFIG.ADX_LEN}")

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
    for v in (atr5m, rsi, adx, ema20, vol_z, ema_dev_atr):
        if pd.isna(v) or np.isinf(v):
            raise ValueError("Indicators contain NaN/Inf")
    return {
        "atr5m": float(atr5m), "rsi": float(rsi), "adx": float(adx),
        "ema20": float(ema20), "vol_z": float(vol_z),
        "ema_dev_atr": float(ema_dev_atr), "supertrend": st_state
    }

# ---------------------------------------------------------------------------
# Position State Manager
# ---------------------------------------------------------------------------
class Position:
    def __init__(self, side: str, signal_id: str, leverage: int | None=None):
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
        self.leverage = leverage or CONFIG.LEVERAGE
        self.max_steps = CONFIG.DCA_LEVELS
        self.reserved_one = False
        self.last_sl_notified_price = None

    def plan_margins(self, bank: float):
        total_target = bank * CONFIG.CUM_DEPOSIT_FRAC_AT_FULL
        self.step_margins = plan_margins_bank_first(total_target, CONFIG.DCA_LEVELS, CONFIG.DCA_GROWTH)

    def add_step(self, price: float):
        margin = self.step_margins[self.steps_filled]
        notional = margin * self.leverage
        new_qty = notional / max(price,1e-9)
        self.avg = (self.avg*self.qty + price*new_qty) / max(self.qty+new_qty,1e-9) if self.qty>0 else price
        self.qty += new_qty
        self.steps_filled += 1
        self.tp_price = self.avg*(1+self.tp_pct) if self.side=="LONG" else self.avg*(1-self.tp_pct)
        return margin, notional

    def next_dca_price(self, atr5m: float):
        if self.steps_filled >= min(self.max_steps, len(self.step_margins)):
            return None
        step = CONFIG.ATR_MULTS[self.steps_filled] * atr5m
        return (self.avg - step) if self.side == "LONG" else (self.avg + step)

# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------
async def scanner_main_loop(app: Application, broadcast):
    log.info("BMR-DCA loop starting…")
    app.bot_data.setdefault("position", None)
    app.bot_data.setdefault("last_range_build", 0.0)

    try:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        sheet_key  = os.environ.get("SHEET_ID")
        if creds_json and sheet_key:
            gc = gspread.service_account_from_dict(json.loads(creds_json))
            sheet = gc.open_by_key(sheet_key)
            await trade_executor.ensure_bmr_log_sheet(sheet, title="BMR_DCA_Log")
    except Exception as e:
        log.error(f"Sheets init error: {e}", exc_info=True)

    exchange = ccxt.mexc({
        'options': {'defaultType': 'swap'},
        'enableRateLimit': True,
        'rateLimit': 150,
        'timeout': 20000,
    })
    await exchange.load_markets(True)
    
    candidates = [CONFIG.SYMBOL, "EUR/USDT:USDT", "EUR/USDT", "EURC/USDT", "EURUSDT"]
    symbol = None
    for s in candidates:
        if s in exchange.markets:
            symbol = s
            if s != CONFIG.SYMBOL:
                log.warning(f"Requested {CONFIG.SYMBOL}, but using available symbol {s} on the exchange.")
            else:
                log.info(f"Successfully found primary symbol {s} on the exchange.")
            break

    if not symbol:
        log.critical(f"None of the candidate symbols were found on the exchange: {candidates}")
        await exchange.close()
        return

    market = exchange.markets[symbol]
    tick = None
    p_prec = market.get("precision", {}).get("price")
    if isinstance(p_prec, (int, float)):
        if 0 < float(p_prec) < 1:
            tick = float(p_prec)
        elif int(p_prec) >= 0:
            tick = 10 ** (-int(p_prec))
    if not tick:
        tick = market.get("limits", {}).get("price", {}).get("min")
    if not tick or tick <= 0:
        tick = 1e-4
    app.bot_data["price_tick"] = float(tick)

    rng = None
    last_flush = 0

    while app.bot_data.get("bot_on", False):
        try:
            bank = float(app.bot_data.get("safety_bank_usdt", CONFIG.SAFETY_BANK_USDT))
            
            now = time.time()
            manage_only = app.bot_data.get("scan_paused", False)
            pos: Position | None = app.bot_data.get("position")

            need_build = (rng is None) or ((now - app.bot_data.get("last_range_build", 0) > CONFIG.REBUILD_RANGE_EVERY_MIN*60) and (pos is None))
            if need_build:
                tmp_rng = await build_range(exchange, symbol)
                if tmp_rng:
                    rng = tmp_rng
                    app.bot_data["last_range_build"] = now
                    log.info(f"[RANGE] New range built: lower={fmt(rng['lower'])} upper={fmt(rng['upper'])} width={fmt(rng['width'])}")
                    app.bot_data["intro_done"] = False
                else:
                    log.warning("[RANGE] Failed to build new range; using previous one if available.")
            
            if not rng:
                log.error("Range is not available. Cannot proceed.")
                await asyncio.sleep(10)
                continue

            ohlc5 = await fetch_ohlcv_safe(exchange, symbol, CONFIG.TF_ENTRY,
                                           limit=max(60, CONFIG.VOL_WIN+CONFIG.ADX_LEN+20))
            if not ohlc5:
                log.warning("Could not fetch 5m OHLCV data. Skipping this cycle.")
                await asyncio.sleep(2)
                continue
            
            df5 = pd.DataFrame(ohlc5, columns=["ts","open","high","low","close","volume"])
            try:
                ind = compute_indicators_5m(df5)
            except ValueError as e:
                log.warning(f"Indicator calculation failed: {e}. Skipping cycle.")
                await asyncio.sleep(2)
                continue

            px = float(df5["close"].iloc[-1])

            if (not app.bot_data.get("intro_done")) and (pos is None):
                p30 = rng["lower"] + 0.30 * rng["width"]
                p70 = rng["lower"] + 0.70 * rng["width"]
                d_to_long  = max(0.0, px - p30)
                d_to_short = max(0.0, p70 - px)
                pct_to_long  = (d_to_long / max(px, 1e-9)) * 100
                pct_to_short = (d_to_short / max(px, 1e-9)) * 100
                if broadcast:
                    await broadcast(app, (f"🎯 Пороги входа: LONG ≤ <code>{fmt(p30)}</code> (30%), "
                                   f"SHORT ≥ <code>{fmt(p70)}</code> (70%).\n"
                                   f"Текущая: <code>{fmt(px)}</code>. "
                                   f"До LONG: {fmt(d_to_long)} ({pct_to_long:.2f}%), до SHORT: {fmt(d_to_short)} ({pct_to_short:.2f}%)"))
                app.bot_data["intro_done"] = True

            pos = app.bot_data.get("position")
            
            # ДОБАВЛЕНО: Обработка ручного закрытия
            if pos and app.bot_data.get("force_close"):
                exit_p = px
                time_min = (time.time()-pos.open_ts)/60.0
                raw_pnl = (exit_p / pos.avg - 1.0) * (1 if pos.side == "LONG" else -1)
                pnl_pct = raw_pnl * 100 * pos.leverage
                pnl_usd = sum(pos.step_margins[:pos.steps_filled]) * (pnl_pct/100.0)

                if broadcast:
                    await broadcast(app, f"🧰 <b>MANUAL_CLOSE</b>\n"
                                          f"Цена выхода: <code>{fmt(exit_p)}</code>\n"
                                          f"P&L≈ {pnl_usd:+.2f} USDT ({pnl_pct:+.2f}%)\n"
                                          f"Время в сделке: {time_min:.1f} мин")
                await trade_executor.bmr_log_event({
                    "Event_ID": f"MANUAL_CLOSE_{pos.signal_id}", "Signal_ID": pos.signal_id,
                    "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "Pair": symbol, "Side": pos.side, "Event": "MANUAL_CLOSE",
                    "PNL_Realized_USDT": pnl_usd, "PNL_Realized_Pct": pnl_pct,
                    "Time_In_Trade_min": time_min
                })
                app.bot_data["force_close"] = False
                pos.last_sl_notified_price = None
                app.bot_data["position"] = None
                continue

            if pos:
                brk_up, brk_dn = break_levels(rng)
                if px >= brk_up or px <= brk_dn:
                    if not pos.reserved_one:
                        pos.max_steps = min(pos.steps_filled + 1, CONFIG.DCA_LEVELS)
                        pos.reserved_one = True
                        if broadcast:
                            await broadcast(app, "📌 Пробой коридора — обычные усреднения заморожены. Оставлен 1 резерв на ретест.")

            if not manage_only and not pos:
                pos_in = max(0.0, min(1.0, (px - rng["lower"]) / max(rng["width"], 1e-9)))
                side_cand = "LONG" if pos_in <= 0.30 else ("SHORT" if pos_in >= 0.70 else None)
                
                if side_cand:
                    pos = Position(side_cand, signal_id=f"{symbol.split('/')[0]}_{int(now)}")
                    pos.plan_margins(bank)
                    pos.max_steps = min(5, CONFIG.DCA_LEVELS)
                    pos.reserved_one = False
                    pos.leverage = CONFIG.LEVERAGE

                    margin, _ = pos.add_step(px)
                    app.bot_data["position"] = pos
                    
                    cum_margin = sum(pos.step_margins[:pos.steps_filled])
                    cum_notional = cum_margin * pos.leverage
                    fees_paid_est = cum_notional * CONFIG.FEE_TAKER * CONFIG.LIQ_FEE_BUFFER
                    liq = approx_liq_price_cross(
                        avg=pos.avg, side=pos.side, qty=pos.qty,
                        equity=bank, mmr=CONFIG.MAINT_MMR, fees_paid=fees_paid_est
                    )
                    if liq <= 0: liq = float('nan')
                    dist_to_liq_pct = liq_distance_pct(pos.side, px, liq)
                    dist_txt = "N/A" if np.isnan(dist_to_liq_pct) else f"{dist_to_liq_pct:.2f}%"
                    liq_arrow = "↓" if pos.side == "LONG" else "↑"

                    nxt_raw = pos.next_dca_price(ind["atr5m"])
                    nxt_cap, clipped, no_room = cap_next_price(pos.side, pos.avg, nxt_raw, ind["atr5m"], rng)
                    nxt_txt = "N/A" if nxt_cap is None else fmt(nxt_cap)
                    clip_note = " ✂️" if clipped else ""
                    no_room_note = " (пространство до пробоя исчерпано — дальше только резерв)" if no_room else ""
                    remaining = pos.max_steps - pos.steps_filled
                    
                    brk_up, brk_dn = break_levels(rng)
                    brk_up_pct, brk_dn_pct = break_distance_pcts(px, brk_up, brk_dn)
                    brk_line = (f"Пробой: ↑<code>{fmt(brk_up)}</code> ({brk_up_pct:.2f}%) | "
                                f"↓<code>{fmt(brk_dn)}</code> ({brk_dn_pct:.2f}%)")

                    if broadcast:
                        await broadcast(app,
                            f"⚡ <b>BMR-DCA {pos.side} ({symbol.split('/')[0]})</b>\n"
                            f"Вход: <code>{fmt(px)}</code>\n"
                            f"Депозит (старт): <b>{cum_margin:.2f} USDT</b> | Плечо: <b>{pos.leverage}x</b>\n"
                            f"TP: <code>{fmt(pos.tp_price)}</code> (+{CONFIG.TP_PCT*100:.2f}%)\n"
                            f"Ликвидация: {liq_arrow}<code>{fmt(liq)}</code> (до лик.: {dist_txt})\n"
                            f"{brk_line}\n"
                            f"След. усреднение по цене: <code>{nxt_txt}</code>{clip_note}{no_room_note} (осталось усреднений: {remaining} из 5)"
                        )
                    await trade_executor.bmr_log_event({
                        "Event_ID": f"OPEN_{pos.signal_id}", "Signal_ID": pos.signal_id, "Leverage": pos.leverage,
                        "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "Pair": symbol, "Side": pos.side, "Event": "OPEN",
                        "Step_No": pos.steps_filled, "Step_Margin_USDT": margin,
                        "Cum_Margin_USDT": cum_margin, "Entry_Price": px, "Avg_Price": pos.avg,
                        "TP_Pct": CONFIG.TP_PCT, "TP_Price": pos.tp_price, "Liq_Est_Price": liq,
                        "Next_DCA_Price": nxt_cap or "",
                        "Fee_Rate_Maker": CONFIG.FEE_MAKER, "Fee_Rate_Taker": CONFIG.FEE_TAKER,
                        "Fee_Est_USDT": fees_paid_est / CONFIG.LIQ_FEE_BUFFER, "ATR_5m": ind["atr5m"], "ATR_1h": rng["atr1h"],
                        "RSI_5m": ind["rsi"], "ADX_5m": ind["adx"], "Supertrend": ind["supertrend"], "Vol_z": ind["vol_z"],
                        "Range_Lower": rng["lower"], "Range_Upper": rng["upper"], "Range_Width": rng["width"]
                    })

            pos = app.bot_data.get("position")
            if pos:
                if not manage_only:
                    if pos.reserved_one:
                        need_retest = (pos.side=="SHORT" and px <= rng["upper"] * (1 - CONFIG.REENTRY_BAND)) or \
                                      (pos.side=="LONG"  and px >= rng["lower"] * (1 + CONFIG.REENTRY_BAND))
                        can_add = pos.steps_filled < pos.max_steps
                        if need_retest and can_add and trend_reversal_confirmed(pos.side, ind):
                            margin, _ = pos.add_step(px)
                            pos.max_steps = pos.steps_filled
                            cum_margin = sum(pos.step_margins[:pos.steps_filled])
                            cum_notional = cum_margin * pos.leverage
                            fees_paid_est = cum_notional * CONFIG.FEE_TAKER * CONFIG.LIQ_FEE_BUFFER
                            liq = approx_liq_price_cross(
                                avg=pos.avg, side=pos.side, qty=pos.qty,
                                equity=bank, mmr=CONFIG.MAINT_MMR, fees_paid=fees_paid_est
                            )
                            if liq <= 0: liq = float('nan')
                            dist_to_liq_pct = liq_distance_pct(pos.side, px, liq)
                            dist_txt = "N/A" if np.isnan(dist_to_liq_pct) else f"{dist_to_liq_pct:.2f}%"
                            liq_arrow = "↓" if pos.side == "LONG" else "↑"
                            
                            brk_up, brk_dn = break_levels(rng)
                            brk_up_pct, brk_dn_pct = break_distance_pcts(px, brk_up, brk_dn)
                            brk_line = (f"Пробой: ↑<code>{fmt(brk_up)}</code> ({brk_up_pct:.2f}%) | "
                                        f"↓<code>{fmt(brk_dn)}</code> ({brk_dn_pct:.2f}%)")

                            if broadcast:
                                await broadcast(app,
                                    f"↩️ Ретест — резервный добор\n"
                                    f"Цена: <code>{fmt(px)}</code>\n"
                                    f"Добор (резерв): <b>{margin:.2f} USDT</b> | Депозит (текущий): <b>{cum_margin:.2f} USDT</b>\n"
                                    f"Средняя: <code>{fmt(pos.avg)}</code> | TP: <code>{fmt(pos.tp_price)}</code>\n"
                                    f"Ликвидация: {liq_arrow}<code>{fmt(liq)}</code> (до лик.: {dist_txt})\n"
                                    f"{brk_line}")
                            await trade_executor.bmr_log_event({
                                "Event_ID": f"RETEST_ADD_{pos.signal_id}_{pos.steps_filled}", "Signal_ID": pos.signal_id,
                                "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                                "Pair": symbol, "Side": pos.side, "Event": "RETEST_ADD",
                                "Step_No": pos.steps_filled, "Step_Margin_USDT": margin,
                                "Entry_Price": px, "Avg_Price": pos.avg
                            })
                    else:
                        nxt_raw = pos.next_dca_price(ind["atr5m"])
                        nxt_cap, _, _ = cap_next_price(pos.side, pos.avg, nxt_raw, ind["atr5m"], rng)
                        
                        if (nxt_cap is not None) and ((pos.side=="LONG" and px <= nxt_cap) or (pos.side=="SHORT" and px >= nxt_cap)):
                            margin, _ = pos.add_step(px)
                            cum_margin = sum(pos.step_margins[:pos.steps_filled])
                            cum_notional = cum_margin * pos.leverage
                            fees_paid_est = cum_notional * CONFIG.FEE_TAKER * CONFIG.LIQ_FEE_BUFFER
                            liq = approx_liq_price_cross(
                                avg=pos.avg, side=pos.side, qty=pos.qty,
                                equity=bank, mmr=CONFIG.MAINT_MMR, fees_paid=fees_paid_est
                            )
                            if liq <= 0: liq = float('nan')
                            dist_to_liq_pct = liq_distance_pct(pos.side, px, liq)
                            dist_txt = "N/A" if np.isnan(dist_to_liq_pct) else f"{dist_to_liq_pct:.2f}%"
                            liq_arrow = "↓" if pos.side == "LONG" else "↑"
                            
                            nxt2_raw = pos.next_dca_price(ind["atr5m"])
                            nxt2_cap, clipped2, no_room2 = cap_next_price(pos.side, pos.avg, nxt2_raw, ind["atr5m"], rng)
                            nxt2_txt = "N/A" if nxt2_cap is None else fmt(nxt2_cap)
                            clip_note2 = " ✂️" if clipped2 else ""
                            no_room_note = " (пространство до пробоя исчерпано — дальше только резерв)" if no_room2 else ""
                            remaining = pos.max_steps - pos.steps_filled

                            brk_up, brk_dn = break_levels(rng)
                            brk_up_pct, brk_dn_pct = break_distance_pcts(px, brk_up, brk_dn)
                            brk_line = (f"Пробой: ↑<code>{fmt(brk_up)}</code> ({brk_up_pct:.2f}%) | "
                                        f"↓<code>{fmt(brk_dn)}</code> ({brk_dn_pct:.2f}%)")

                            if broadcast:
                                await broadcast(app,
                                    f"➕ Усреднение #{pos.steps_filled-1}\n"
                                    f"Цена: <code>{fmt(px)}</code>\n"
                                    f"Добор: <b>{margin:.2f} USDT</b> | Депозит (текущий): <b>{cum_margin:.2f} USDT</b>\n"
                                    f"Средняя: <code>{fmt(pos.avg)}</code> | TP: <code>{fmt(pos.tp_price)}</code>\n"
                                    f"Ликвидация: {liq_arrow}<code>{fmt(liq)}</code> (до лик.: {dist_txt})\n"
                                    f"{brk_line}\n"
                                    f"След. усреднение по цене: <code>{nxt2_txt}</code>{clip_note2}{no_room_note} (осталось: {remaining} из 5)")
                            await trade_executor.bmr_log_event({
                                "Event_ID": f"ADD_{pos.signal_id}_{pos.steps_filled}", "Signal_ID": pos.signal_id,
                                "Timestamp_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                                "Pair": symbol, "Side": pos.side, "Event": "ADD",
                                "Step_No": pos.steps_filled, "Step_Margin_USDT": margin,
                                "Cum_Margin_USDT": cum_margin, "Entry_Price": px, "Avg_Price": pos.avg,
                                "TP_Price": pos.tp_price, "SL_Price": pos.sl_price or "",
                                "Liq_Est_Price": liq, "Next_DCA_Price": nxt2_cap or "",
                                "Fee_Rate_Maker": CONFIG.FEE_MAKER, "Fee_Rate_Taker": CONFIG.FEE_TAKER,
                                "Fee_Est_USDT": fees_paid_est / CONFIG.LIQ_FEE_BUFFER, "ATR_5m": ind["atr5m"], "ATR_1h": rng["atr1h"],
                                "RSI_5m": ind["rsi"], "ADX_5m": ind["adx"], "Supertrend": ind["supertrend"], "Vol_z": ind["vol_z"],
                                "Range_Lower": rng["lower"], "Range_Upper": rng["upper"], "Range_Width": rng["width"]
                            })

                if pos.side == "LONG": gain_to_tp = max(0.0, (px / max(pos.avg,1e-9) - 1.0) / CONFIG.TP_PCT)
                else: gain_to_tp = max(0.0, (pos.avg / max(px,1e-9) - 1.0) / CONFIG.TP_PCT)

                if gain_to_tp >= CONFIG.TRAIL_ARM:
                    lock_pct = CONFIG.TRAIL_LOCK * CONFIG.TP_PCT
                    locked = pos.avg*(1+lock_pct) if pos.side=="LONG" else pos.avg*(1-lock_pct)
                    chand = chandelier_stop(pos.side, px, ind["atr5m"])
                    new_sl = max(locked, chand) if pos.side=="LONG" else min(locked, chand)
                    
                    tick = app.bot_data.get("price_tick", 1e-4)
                    new_sl_q = quantize_to_tick(new_sl, tick)
                    curr_sl_q = quantize_to_tick(pos.sl_price, tick)
                    last_notif_q = quantize_to_tick(pos.last_sl_notified_price, tick)

                    improves = (curr_sl_q is None) or \
                               (pos.side == "LONG" and new_sl_q > curr_sl_q) or \
                               (pos.side == "SHORT" and new_sl_q < curr_sl_q)

                    if improves:
                        pos.sl_price = new_sl_q
                        if sl_moved_enough(last_notif_q, pos.sl_price,
                                           pos.side, tick, CONFIG.SL_NOTIFY_MIN_TICK_STEP):
                            if broadcast:
                                await broadcast(app, f"🔒 Трейлинг-SL установлен: <code>{fmt(pos.sl_price)}</code>")
                            pos.last_sl_notified_price = pos.sl_price
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
                    raw_pnl = (exit_p / pos.avg - 1.0) * (1 if pos.side == "LONG" else -1)
                    pnl_pct = raw_pnl * 100 * pos.leverage
                    pnl_usd = sum(pos.step_margins[:pos.steps_filled]) * (pnl_pct/100.0)
                    atr_now = ind["atr5m"]
                    if broadcast:
                        await broadcast(app, f"{'✅' if pnl_usd > 0 else '❌'} <b>{reason}</b>\n"
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
                    pos.last_sl_notified_price = None
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
