import time
import requests
import pandas as pd
import pandas_ta as ta
import psutil
import os
import numpy as np
import json
from tabulate import tabulate

pd.set_option('display.max_rows', None)

BITKUB_TV_URL = "https://api.bitkub.com/tradingview/history"

currency = ["XRP_THB", "BTC_THB", "ETH_THB", "USDT_THB", "SOL_THB", "ADA_THB", "BNB_THB"]

timeframes = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "4h": "240",
    "1d": "1D",
}

with open("config/color.json", "r", encoding="utf-8") as f:
    COLORS = json.load(f)


def color_trend(val: str) -> str:
    if val is None:
        return "-"
    text = str(val)

    # ถ้าเป็น error ให้ใช้สี DOWN
    if text.startswith("ERROR"):
        return f"{COLORS.get('DOWN', '')}{text}{COLORS.get('RESET', '')}"

    # ถ้า text ตรงกับ key สีในไฟล์ config
    if text in COLORS:
        return f"{COLORS[text]}{text}{COLORS.get('RESET', '')}"

    return text


def print_pretty_table(df: pd.DataFrame):
    """
    แสดงทุกคอลัมน์ + ใส่สี trend + format atr/tp ไม่ให้เห็น NaN ตรง ๆ
    """
    df_fmt = df.copy()

    # ใส่สี trend ถ้ามี
    if "trend" in df_fmt.columns:
        df_fmt["trend"] = df_fmt["trend"].apply(color_trend)

    # จัดการ NaN ในคอลัมน์ atr / tp1 / tp2 แปลงให้เป็น "-" ตอนแสดงผล
    for col in ["atr", "tp1", "tp2"]:
        if col in df_fmt.columns:
            def _fmt(x):
                if pd.isna(x):
                    return "-"
                try:
                    return round(float(x), 4)
                except Exception:
                    return x
            df_fmt[col] = df_fmt[col].apply(_fmt)

    print(tabulate(df_fmt, headers="keys", tablefmt="grid", showindex=False))


def fetch_ohlcv(symbol: str, resolution: str, bars: int = 300) -> pd.DataFrame:
    """
    ดึง OHLCV จาก Bitkub TradingView API
    resolution เช่น "5","15","60","240","1D"
    """
    # แปลง resolution เป็นจำนวนวินาทีต่อแท่ง
    if resolution.upper().endswith("D"):
        num_days = int(resolution[:-1]) if len(resolution) > 1 else 1
        step_sec = num_days * 24 * 60 * 60
    else:
        step_min = int(resolution)
        step_sec = step_min * 60

    now = int(time.time())
    to_ts = now
    from_ts = now - bars * step_sec

    params = {
        "symbol": symbol,
        "resolution": resolution,
        "from": from_ts,
        "to": to_ts,
    }

    r = requests.get(BITKUB_TV_URL, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()

    if data.get("s") != "ok":
        raise ValueError(f"Bitkub returned non-ok status for {symbol} {resolution}: {data}")

    df = pd.DataFrame(
        {
            # แปลงจาก UTC -> เวลาไทย
            "time": pd.to_datetime(pd.Series(data["t"]), unit="s", utc=True).dt.tz_convert("Asia/Bangkok").dt.tz_localize(None),
            "open": data["o"],
            "high": data["h"],
            "low": data["l"],
            "close": data["c"],
            "volume": data["v"],
        }
    )

    # กันเหนียว เผื่อ API ส่งลำดับผิด
    df = df.sort_values("time").reset_index(drop=True)

    return df


def detect_trend(
    df: pd.DataFrame,
    fast: int = 50,
    slow: int = 200,
    adx_len: int = 14,
    adx_threshold: float = 20.0,
    super_len: int = 10,
    super_mult: float = 3.0,
):
    """
    ระบุเทรนด์จาก EMA(เร็ว/ช้า) + ADX + Supertrend + ATR และคำนวณ TP1/TP2
    """
    df = df.copy()

    # ถ้าแท่งไม่พอคำนวณ indicator ให้ UNKNOWN ไปก่อน
    if len(df) < max(slow, adx_len + 1, super_len + 1):
        last = df.iloc[-1].copy()
        last["ema_fast"] = np.nan
        last["ema_slow"] = np.nan
        last["adx"] = np.nan
        last["supertrend"] = np.nan
        last["supertrend_dir"] = np.nan
        last["atr"] = np.nan
        last["tp1"] = np.nan
        last["tp2"] = np.nan
        return "UNKNOWN", last

    # EMA
    df["ema_fast"] = ta.ema(df["close"], length=fast)
    df["ema_slow"] = ta.ema(df["close"], length=slow)

    # ADX
    adx = ta.adx(df["high"], df["low"], df["close"], length=adx_len)
    adx_col = f"ADX_{adx_len}"
    df["adx"] = adx[adx_col]

    # Supertrend
    st = ta.supertrend(
        df["high"],
        df["low"],
        df["close"],
        length=super_len,
        multiplier=super_mult,
    )
    # pandas_ta.supertrend คืนคอลัมน์ประมาณ: SUPERT_10_3.0, SUPERTd_10_3.0, ...
    st_price_col = [c for c in st.columns if c.startswith("SUPERT_")][0]
    st_dir_col = [c for c in st.columns if c.startswith("SUPERTd_")][0]

    df["supertrend"] = st[st_price_col]
    df["supertrend_dir"] = st[st_dir_col]

    # ATR
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    last = df.iloc[-1].copy()

    # ถ้า indicator ตัวใดตัวหนึ่งในชุดนี้ยังเป็น NaN ให้ถือว่าไม่พร้อมใช้
    required_cols = ["ema_fast", "ema_slow", "adx", "supertrend", "supertrend_dir", "atr"]
    if df[required_cols].iloc[-1].isna().any():
        last["tp1"] = np.nan
        last["tp2"] = np.nan
        return "UNKNOWN", last

    # คำนวณเทรนด์
    if last["adx"] < adx_threshold:
        trend = "SIDEWAYS"
    else:
        if last["ema_fast"] > last["ema_slow"] and last["supertrend_dir"] > 0:
            trend = "UP"
        elif last["ema_fast"] < last["ema_slow"] and last["supertrend_dir"] < 0:
            trend = "DOWN"
        else:
            trend = "SIDEWAYS"

    # คำนวณ TP จาก ATR เฉพาะเมื่อ trend ชัดเจน
    close = float(last["close"])
    atr = float(last["atr"])

    if trend == "UP":
        last["tp1"] = close + atr
        last["tp2"] = close + 2 * atr
    elif trend == "DOWN":
        last["tp1"] = close - atr
        last["tp2"] = close - 2 * atr
    else:
        last["tp1"] = np.nan
        last["tp2"] = np.nan

    return trend, last


def build_trend_table(
    symbols,
    timeframes_dict,
    bars: int = 300,
    fast: int = 20,
    slow: int = 50,
    adx_len: int = 14,
    adx_threshold: float = 20.0,
    super_len: int = 10,
    super_mult: float = 3.0,
) -> pd.DataFrame:
    rows = []

    for sym in symbols:
        for tf_label, res in timeframes_dict.items():
            try:
                df = fetch_ohlcv(sym, res, bars=bars)
                trend, last = detect_trend(
                    df,
                    fast=fast,
                    slow=slow,
                    adx_len=adx_len,
                    adx_threshold=adx_threshold,
                    super_len=super_len,
                    super_mult=super_mult,
                )

                rows.append(
                    {
                        "symbol": sym,
                        "timeframe": tf_label,
                        "last_time": last["time"],
                        "close": float(last["close"]),
                        "ema_fast": last.get("ema_fast", np.nan),
                        "ema_slow": last.get("ema_slow", np.nan),
                        "adx": last.get("adx", np.nan),
                        "supertrend": last.get("supertrend", np.nan),
                        "supertrend_dir": last.get("supertrend_dir", np.nan),
                        "atr": last.get("atr", np.nan),
                        "tp1": last.get("tp1", np.nan),
                        "tp2": last.get("tp2", np.nan),
                        "trend": trend,
                        "bars_count": len(df),
                    }
                )
            except Exception as e:
                rows.append(
                    {
                        "symbol": sym,
                        "timeframe": tf_label,
                        "last_time": None,
                        "close": None,
                        "ema_fast": None,
                        "ema_slow": None,
                        "adx": None,
                        "supertrend": None,
                        "supertrend_dir": None,
                        "atr": None,
                        "tp1": None,
                        "tp2": None,
                        "trend": f"ERROR: {e}",
                        "bars_count": 0,
                    }
                )

    return pd.DataFrame(rows)


if __name__ == "__main__":
    trend_df = build_trend_table(
        currency,
        timeframes,
        bars=200,
        fast=20,
        slow=50,
        adx_len=14,
        adx_threshold=20.0,
        super_len=10,
        super_mult=3.0,
    )


    for sym, group in trend_df.groupby("symbol"):
        print(f"\n===== {sym} =====\n")
        print_pretty_table(group)

    process = psutil.Process(os.getpid())
    memory_used = process.memory_info().rss / (1024 ** 2)  # MB
    print(f"Memory Used: {memory_used:.2f} MB")
