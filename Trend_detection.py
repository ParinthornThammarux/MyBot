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

currency = ["XRP_THB", "BTC_THB", "ETH_THB", "USDT_THB", "KUB_THB", "ADA_THB", "BNB_THB"]

# ใช้ string resolution แบบ TradingView / Bitkub
timeframes = {
    "1m": "1",
    "5m": "5",     # 5 นาที
    "15m": "15",   # 15 นาที
    "30m": "30",
    "1h": "60",    # 60 นาที
    "4h": "240",   # 240 นาที
    "1d": "1D",    # 1 วัน
}

# โหลดสีจากไฟล์ config
with open("config/color.json", "r", encoding="utf-8") as f:
    COLORS = json.load(f)


def color_trend(val: str) -> str:
    """
    ใช้ใส่สีให้ค่า trend ตอนแสดงตาราง
    """
    if val is None:
        return "-"

    text = str(val)

    if text.startswith("ERROR"):
        return f"{COLORS['DOWN']}{text}{COLORS['RESET']}"

    if text in COLORS:
        return f"{COLORS[text]}{text}{COLORS['RESET']}"

    return text


def print_pretty_table(df: pd.DataFrame):
    """
    พิมพ์ DataFrame เป็นตารางสวย ๆ ใน console
    """
    df_fmt = df.copy()
    if "trend" in df_fmt.columns:
        df_fmt["trend"] = df_fmt["trend"].apply(color_trend)
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
            # 👇 แปลงจาก UTC -> เวลาไทยชัด ๆ
            "time": pd.to_datetime(pd.Series(data["t"]), unit="s", utc=True).dt.tz_convert("Asia/Bangkok"),
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
    ระบุเทรนด์จาก EMA(เร็ว/ช้า) + ADX + Supertrend
    """
    df = df.copy()

    # ถ้าแท่งไม่พอคำนวณ indicator เลย ให้บอก UNKNOWN ไปก่อน
    if len(df) < max(slow, adx_len + 1, super_len + 1):
        last = df.iloc[-1]
        last["ema_fast"] = None
        last["ema_slow"] = None
        last["adx"] = None
        last["supertrend"] = None
        last["supertrend_dir"] = None
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
    # pandas_ta.supertrend จะคืน DataFrame หลายคอลัมน์ เช่น:
    # ['SUPERT_10_3.0', 'SUPERTd_10_3.0', 'SUPERTl_10_3.0', 'SUPERTs_10_3.0']
    st_price_col = [c for c in st.columns if c.startswith("SUPERT_")][0]
    st_dir_col = [c for c in st.columns if c.startswith("SUPERTd_")][0]

    df["supertrend"] = st[st_price_col]
    df["supertrend_dir"] = st[st_dir_col]

    last = df.iloc[-1]

    # ถ้ายัง NaN อยู่ แสดงว่ายังคำนวณไม่ครบแท่ง
    if (
        pd.isna(last["ema_fast"])
        or pd.isna(last["ema_slow"])
        or pd.isna(last["adx"])
        or pd.isna(last["supertrend"])
        or pd.isna(last["supertrend_dir"])
    ):
        return "UNKNOWN", last

    # กติกาเทรนด์แบบใช้ทั้ง EMA + ADX + Supertrend
    if last["adx"] < adx_threshold:
        # ADX ต่ำ = เทรนด์อ่อน / ไซด์เวย์
        trend = "SIDEWAYS"
    else:
        # ใช้ทั้ง EMA และทิศทาง Supertrend ช่วยกัน confirm
        if last["ema_fast"] > last["ema_slow"] and last["supertrend_dir"] > 0:
            trend = "UP"
        elif last["ema_fast"] < last["ema_slow"] and last["supertrend_dir"] < 0:
            trend = "DOWN"
        else:
            # ไม่คอนเฟิร์มกัน ชี้ไปทางไซด์เวย์หรือไม่ชัด
            trend = "SIDEWAYS"

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
                        "ema_fast": last["ema_fast"],
                        "ema_slow": last["ema_slow"],
                        "adx": last["adx"],
                        "supertrend": last["supertrend"],
                        "supertrend_dir": last["supertrend_dir"],
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
        super_len=10,      # ปรับ length ของ Supertrend ได้
        super_mult=3.0,    # ปรับ multiplier ของ Supertrend ได้
    )

    for sym, group in trend_df.groupby("symbol"):
        print(f"\n===== {sym} =====\n")
        print_pretty_table(group)

    process = psutil.Process(os.getpid())
    memory_used = process.memory_info().rss / (1024 ** 2)  # MB
    print(f"Memory Used: {memory_used:.2f} MB")
