import os, time, random, requests, datetime
from typing import Dict, Any, List
from dotenv import load_dotenv

import pandas as pd
import pandas_ta as ta

load_dotenv()

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
BASE_URL = "https://api.bitkub.com"
SYMBOL   = "BTC_THB"

ORDER_NOTIONAL_THB = 100    # ขนาดต่อไม้
SLIPPAGE_BPS       = 0      # slippage (bps)
FEE_RATE           = 0.0025 # 0.25% ต่อข้าง

PRICE_ROUND = 2
QTY_ROUND   = 6

# ADX FILTER
ADX_LENGTH    = 14
ADX_THRESHOLD = 20.0

# COOLDOWN หลังเทรด (หน่วย: วินาที timestamp แท่ง)
COOLDOWN_SEC = 300

# EMA ใช้ดูเทรนด์
EMA_TREND_LENGTH = 200

# BACKTEST CONFIG
INITIAL_BALANCE = 10000.0
LOOKBACK_BARS   = 2000  # จำนวนแท่งย้อนหลัง (TF = 1 ชั่วโมง)

DEBUG_HTTP       = False
HTTP_TIMEOUT     = 12
RETRY_MAX        = 4
RETRY_BASE_DELAY = 0.6

COMMON_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json"
}

session = requests.Session()

# ------------------------------------------------------------
# ANSI COLOR
# ------------------------------------------------------------
RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"

FG_RED     = "\033[31m"
FG_GREEN   = "\033[32m"
FG_YELLOW  = "\033[33m"
FG_BLUE    = "\033[34m"
FG_MAGENTA = "\033[35m"
FG_CYAN    = "\033[36m"
FG_WHITE   = "\033[37m"


def color_for(msg: str) -> str:
    if "ERROR" in msg or "EXC" in msg:
        return FG_RED + BOLD
    if msg.startswith("[HTTP GET]"):
        return FG_CYAN + DIM
    if "[HTTP GET ERROR" in msg:
        return FG_RED
    if msg.startswith("[BACKTEST]"):
        return FG_MAGENTA + BOLD
    if msg.startswith("[BUY "):
        return FG_GREEN + BOLD
    if msg.startswith("[SELL]"):
        return FG_YELLOW + BOLD
    return FG_WHITE


def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    color = color_for(msg)
    out = f"{DIM}[{ts}]{RESET} {color}{msg}{RESET}"
    print(out)


# ------------------------------------------------------------
# HTTP + BACKOFF
# ------------------------------------------------------------
def _backoff_sleep(i: int):
    delay = RETRY_BASE_DELAY * (2 ** i) + random.uniform(0, 0.2)
    time.sleep(delay)


def http_get(url, params=None, timeout=HTTP_TIMEOUT):
    last_exc = None
    for i in range(RETRY_MAX):
        try:
            r = session.get(url, params=params, headers=COMMON_HEADERS, timeout=timeout)
            if DEBUG_HTTP:
                print(f"[HTTP GET] {r.request.method} {r.url} -> {r.status_code}")
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            if DEBUG_HTTP:
                print(f"[HTTP GET ERROR#{i+1}] {url} params={params} err={e}")
            _backoff_sleep(i)
    raise last_exc


# ------------------------------------------------------------
# OHLCV via tradingview/history (TF = 60 นาที)
# ------------------------------------------------------------
FIFTEEN_MIN_SEC = 15 * 60


def fetch_1h_candles(sym: str, lookback_bars: int = 1000) -> List[Dict[str, Any]]:
    """
    ดึงแท่งเทียน 1 ชั่วโมงย้อนหลัง lookback_bars แท่ง
    """
    now_sec = int(time.time())
    frm = now_sec - lookback_bars * 60 * 60 - 60 * 60

    url = f"{BASE_URL}/tradingview/history"
    params = {
        "symbol": sym,
        "resolution": "60",
        "from": frm,
        "to": now_sec,
    }

    r = http_get(url, params=params, timeout=HTTP_TIMEOUT)
    data = r.json()

    if not isinstance(data, dict) or data.get("s") != "ok":
        log(f"[ERROR] fetch_1h_candles unexpected payload: {data}")
        return []

    ts_list = data.get("t", [])
    o_list  = data.get("o", [])
    h_list  = data.get("h", [])
    l_list  = data.get("l", [])
    c_list  = data.get("c", [])
    v_list  = data.get("v", [])

    candles = []
    for ts, o, h, l, c, v in zip(ts_list, o_list, h_list, l_list, c_list, v_list):
        candles.append({
            "ts": int(ts),
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": float(v),
        })

    candles.sort(key=lambda x: x["ts"])
    return candles


# ------------------------------------------------------------
# Build indicators: MACD + ADX + EMA200 (trend filter)
# ------------------------------------------------------------
def build_indicators(candles: List[Dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df["dt"] = pd.to_datetime(df["ts"], unit="s")
    df.set_index("dt", inplace=True)

    macd_df = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if macd_df is not None:
        df = pd.concat([df, macd_df], axis=1)

    adx_df = ta.adx(df["high"], df["low"], df["close"], length=ADX_LENGTH)
    if adx_df is not None:
        df = pd.concat([df, adx_df], axis=1)

    ema_trend = ta.ema(df["close"], length=EMA_TREND_LENGTH)
    if ema_trend is not None:
        df[f"EMA_{EMA_TREND_LENGTH}"] = ema_trend

    return df


# ------------------------------------------------------------
# BACKTEST: MACD ENTRY + HISTOGRAM WEAK EXIT + UP-TREND FILTER
# ------------------------------------------------------------
def backtest_macd_histweak_uptrend(
    sym: str,
    lookback_bars: int = LOOKBACK_BARS,
    initial_balance: float = INITIAL_BALANCE,
):
    log(f"[BACKTEST] Fetching {lookback_bars} 1H bars of {sym} ...")
    candles = fetch_1h_candles(sym, lookback_bars=lookback_bars)
    if not candles:
        log("[ERROR] No candles fetched, abort.")
        return

    log(f"[BACKTEST] Got {len(candles)} candles")
    df = build_indicators(candles)

    macd_col   = "MACD_12_26_9"
    signal_col = "MACDs_12_26_9"
    hist_col   = "MACDh_12_26_9"
    adx_col    = f"ADX_{ADX_LENGTH}"
    ema_trend_col = f"EMA_{EMA_TREND_LENGTH}"

    for c in ["ts", "close", macd_col, signal_col, hist_col, adx_col, ema_trend_col]:
        if c not in df.columns:
            log(f"[ERROR] Missing indicator column: {c}")
            return

    df_ind = df[["ts", "close", macd_col, signal_col, hist_col, adx_col, ema_trend_col]].dropna()
    if df_ind.empty:
        log("[ERROR] No valid rows after dropna.")
        return

    balance = initial_balance
    equity_curve: List[Dict[str, Any]] = []
    trades: List[Dict[str, Any]] = []

    pos = {
        "side": "FLAT",
        "entry_price": 0.0,
        "qty": 0.0,
        "entry_ts": None,
        "notional": 0.0,
        "last_trade_ts": 0,
    }

    for i in range(1, len(df_ind)):
        row_prev = df_ind.iloc[i - 1]
        row_now  = df_ind.iloc[i]

        ts          = int(row_now["ts"])
        close_price = float(row_now["close"])

        macd_prev = float(row_prev[macd_col])
        macd_now  = float(row_now[macd_col])
        sig_prev  = float(row_prev[signal_col])
        sig_now   = float(row_now[signal_col])
        hist_prev = float(row_prev[hist_col])
        hist_now  = float(row_now[hist_col])
        adx_now   = float(row_now[adx_col])

        ema_trend_now  = float(row_now[ema_trend_col])
        ema_trend_prev = float(row_prev[ema_trend_col])

        bullish_cross = (macd_prev < sig_prev) and (macd_now > sig_now)
        # bearish_cross = (macd_prev > sig_prev) and (macd_now < sig_now)  # ไม่ได้ใช้แล้ว

        # ----------------------------------------------------
        # EXIT: Histogram Weakening (ขายเร็วขึ้น แต่ไม่เร็วเกิน)
        # ----------------------------------------------------
        if pos["side"] == "LONG" and pos["qty"] > 0:
            # เงื่อนไข: โมเมนตัมขึ้นเริ่มอ่อนตัว
            # hist_prev > 0 (กำลังเป็นฝั่งขาขึ้น)
            # hist_now > 0 (ยังไม่กลับฝั่งลง)
            # hist_now < hist_prev (เริ่มอ่อน)
            if hist_prev > 0 and hist_now > 0 and hist_now < hist_prev:
                qty = pos["qty"]
                exec_price = round(close_price * (1 - SLIPPAGE_BPS / 10000), PRICE_ROUND)
                gross_value = qty * exec_price
                net_value   = gross_value * (1.0 - FEE_RATE)

                notional_in = pos["notional"]
                pnl = net_value - notional_in
                pnl_pct = pnl / notional_in if notional_in > 0 else 0.0

                balance += net_value

                trades.append({
                    "entry_ts": pos["entry_ts"],
                    "exit_ts": ts,
                    "entry_price": pos["entry_price"],
                    "exit_price": exec_price,
                    "qty": qty,
                    "reason": "HIST_WEAK",
                    "notional": notional_in,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                })

                log(
                    f"[SELL] (HIST_WEAK) ts={datetime.datetime.fromtimestamp(ts)} "
                    f"price={exec_price:.2f} qty={qty:.6f} "
                    f"pnl={pnl:.2f} THB ({pnl_pct*100:.2f}%), "
                    f"balance={balance:.2f}"
                )

                pos["side"]         = "FLAT"
                pos["entry_price"]  = 0.0
                pos["qty"]          = 0.0
                pos["entry_ts"]     = None
                pos["notional"]     = 0.0
                pos["last_trade_ts"] = ts

                equity_curve.append({"ts": ts, "equity": balance})
                continue

        # ----------------------------------------------------
        # ENTRY: MACD cross ขึ้น + hist แข็ง + ADX + ขาขึ้น EMA200
        # ----------------------------------------------------

        # 1) ADX filter: ต้องมีเทรนด์พอสมควร
        if adx_now < ADX_THRESHOLD:
            equity_curve.append({"ts": ts, "equity": balance})
            continue

        # 2) Uptrend filter: ราคาอยู่เหนือ EMA200 และ EMA200 กำลังชันขึ้น
        if not (close_price > ema_trend_now and ema_trend_now > ema_trend_prev):
            equity_curve.append({"ts": ts, "equity": balance})
            continue

        # 3) MACD bullish cross
        if not bullish_cross:
            equity_curve.append({"ts": ts, "equity": balance})
            continue

        # 4) ถ้ามี position อยู่แล้ว ไม่เปิดซ้ำ
        if pos["side"] == "LONG":
            equity_curve.append({"ts": ts, "equity": balance})
            continue

        # 5) cooldown
        last_trade_ts = pos.get("last_trade_ts", 0)
        if last_trade_ts and (ts - last_trade_ts) < COOLDOWN_SEC:
            equity_curve.append({"ts": ts, "equity": balance})
            continue

        # 6) Histogram filter: hist_now > 0 และกำลังเพิ่มขึ้น
        if not (hist_now > 0 and hist_now > hist_prev):
            equity_curve.append({"ts": ts, "equity": balance})
            continue

        # 7) เงินพอรึเปล่า
        if balance < ORDER_NOTIONAL_THB:
            equity_curve.append({"ts": ts, "equity": balance})
            continue

        # -------- EXECUTE BUY --------
        exec_price = round(close_price * (1 + SLIPPAGE_BPS / 10000), PRICE_ROUND)
        qty = (ORDER_NOTIONAL_THB / exec_price) * (1.0 - FEE_RATE)
        qty = round(qty, QTY_ROUND)

        balance -= ORDER_NOTIONAL_THB

        pos["side"]        = "LONG"
        pos["entry_price"] = exec_price
        pos["qty"]         = qty
        pos["entry_ts"]    = ts
        pos["notional"]    = ORDER_NOTIONAL_THB
        pos["last_trade_ts"] = ts

        log(
            f"[BUY ] ts={datetime.datetime.fromtimestamp(ts)} "
            f"price={exec_price:.2f} qty={qty:.6f} "
            f"hist_now={hist_now:.6f}, hist_prev={hist_prev:.6f}, "
            f"adx={adx_now:.2f}, ema200={ema_trend_now:.2f}, "
            f"balance={balance:.2f}"
        )

        equity_curve.append({"ts": ts, "equity": balance})

    # --------------------------------------------------------
    # ปิดไม้ค้างด้วยราคาปิดแท่งสุดท้าย
    # --------------------------------------------------------
    final_equity = balance
    if pos["side"] == "LONG" and pos["qty"] > 0:
        last_row   = df_ind.iloc[-1]
        ts_last    = int(last_row["ts"])
        close_last = float(last_row["close"])

        qty = pos["qty"]
        exec_price = close_last
        gross_value = qty * exec_price
        net_value   = gross_value * (1.0 - FEE_RATE)

        notional_in = pos["notional"]
        pnl = net_value - notional_in
        pnl_pct = pnl / notional_in if notional_in > 0 else 0.0

        final_equity += net_value

        trades.append({
            "entry_ts": pos["entry_ts"],
            "exit_ts": ts_last,
            "entry_price": pos["entry_price"],
            "exit_price": exec_price,
            "qty": qty,
            "reason": "FORCE_EXIT",
            "notional": notional_in,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
        })

    total_pnl        = final_equity - initial_balance
    total_return_pct = (total_pnl / initial_balance * 100.0) if initial_balance > 0 else 0.0

    wins    = [t for t in trades if t["pnl"] > 0]
    losses  = [t for t in trades if t["pnl"] <= 0]
    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0

    log("====================================================")
    log(f"[BACKTEST] Symbol       : {sym}")
    log(f"[BACKTEST] Initial Bal. : {initial_balance:.2f} THB")
    log(f"[BACKTEST] Final   Bal. : {final_equity:.2f} THB")
    log(f"[BACKTEST] PnL          : {total_pnl:.2f} THB ({total_return_pct:.2f}%)")
    log(f"[BACKTEST] #Trades      : {len(trades)}")
    log(f"[BACKTEST] Win rate     : {win_rate:.2f}%")
    if trades:
        best_trade  = max(trades, key=lambda x: x["pnl"])
        worst_trade = min(trades, key=lambda x: x["pnl"])
        log(f"[BACKTEST] Best trade  : {best_trade['pnl']:.2f} THB "
            f"({best_trade['pnl_pct']*100:.2f}%)")
        log(f"[BACKTEST] Worst trade : {worst_trade['pnl']:.2f} THB "
            f"({worst_trade['pnl_pct']*100:.2f}%)")
    log("====================================================")

    return {
        "equity_final": final_equity,
        "equity_start": initial_balance,
        "trades": trades,
        "equity_curve": equity_curve,
    }


if __name__ == "__main__":
    backtest_macd_histweak_uptrend(SYMBOL, lookback_bars=LOOKBACK_BARS, initial_balance=INITIAL_BALANCE)
