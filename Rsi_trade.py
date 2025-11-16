# ============================================================
#  Bitkub MR + RSI(5m) — THB_XRP (จาก trades ย้อนหลังเอง)
#  [v3 trades + 5m candle close signals + DRY_RUN + short logs]
# ============================================================

import os, time, hmac, hashlib, json, requests, math, random
import datetime
from statistics import mean, pstdev
from typing import Dict, Any, List, Optional, Tuple
from dotenv import load_dotenv
from collections import deque
import numpy as np
import talib as ta

load_dotenv()

# ------------------------------------------------------------
# [1] CONFIG
# ------------------------------------------------------------
BASE_URL = "https://api.bitkub.com"
API_KEY  = os.getenv("BITKUB_API_KEY", "")
API_SECRET = (os.getenv("BITKUB_API_SECRET", "") or "").encode()

SYMBOL = "XRP_THB"

# Mean-reversion (z-score)
WINDOW = 80
THRESH_Z = 1.6

# RSI on 5-minute candles (จาก trades ย้อนหลัง)
CANDLE_SEC = 300
RSI_PERIOD = 14
RSI_BUY_TH = 50.0
RSI_SELL_TH = 60.0
USE_RSI_CONFIRM = True     # ต้องมี RSI + z-score ยืนยันร่วม
RSI_ONLY_MODE   = False    # ใช้ RSI เดี่ยว ๆ

# Loop & Orders
REFRESH_SEC = 5
ORDER_NOTIONAL_THB = 100
SLIPPAGE_BPS = 8          # 8 bps = 0.08%
DRY_RUN = True
PRICE_ROUND = 2
QTY_ROUND = 6
MAX_SERIES_LEN = 5000
TRADES_FETCH = 300        # ดึง trade ล่าสุดสูงสุดกี่รายการต่อรอบ
TIME_SYNC_INTERVAL = 300

# Logging / Networking
SHORT_LOG = True           # log แบบสั้น
HEARTBEAT_SEC = 60         # พิมพ์สถานะย่อ ๆ ทุก N วินาที
HTTP_TIMEOUT = 12
RETRY_MAX = 4
RETRY_BASE_DELAY = 0.6

COMMON_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}

session = requests.Session()

# ---------- helper: safe formatter for logs ----------
def fmt(x: Optional[float], nd: int = 4) -> str:
    """Format number safely for logging; returns 'nan' if None/NaN/inf."""
    try:
        if x is None:
            return "nan"
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return "nan"
        return f"{x:.{nd}f}"
    except Exception:
        return "nan"

# ------------------------------------------------------------
# [2] TIME & LOG
# ------------------------------------------------------------
_server_offset_ms = 0
_last_sync_ts = 0
_last_heartbeat = 0

def now_server_ms() -> int:
    return int(time.time() * 1000) + _server_offset_ms

def now_server_dt() -> datetime.datetime:
    return datetime.datetime.fromtimestamp(now_server_ms() / 1000)

def ts_hms() -> str:
    return now_server_dt().strftime("%Y-%m-%d %H:%M:%S")

def log(msg: str):
    print(f"[{ts_hms()}] {msg}")

def slog(msg: str):
    if SHORT_LOG:
        print(f"[{ts_hms()}] {msg}")

def _backoff_sleep(i: int):
    time.sleep(RETRY_BASE_DELAY * (2 ** i) + random.uniform(0, 0.2))

def sync_server_time():
    global _server_offset_ms, _last_sync_ts
    url = f"{BASE_URL}/api/v3/servertime"
    try:
        r = session.get(url, headers=COMMON_HEADERS, timeout=8)
        r.raise_for_status()
        data = r.json()
        server_time = None
        if isinstance(data, (int, float, str)):
            server_time = int(data)
        elif isinstance(data, dict):
            server_time = int(data.get("result") or data.get("server_time"))
        if server_time is None:
            log(f"[SYNC ERROR] unexpected payload: {data}")
            return
        local_time = int(time.time() * 1000)
        _server_offset_ms = server_time - local_time
        _last_sync_ts = time.time()
    except Exception as e:
        log(f"[SYNC ERROR] {e}")

def ts_ms_str() -> str:
    global _last_sync_ts
    now = time.time()
    if now - _last_sync_ts > TIME_SYNC_INTERVAL:
        sync_server_time()
    return str(int(now * 1000) + _server_offset_ms)

# ------------------------------------------------------------
# [3] AUTH
# ------------------------------------------------------------
def sign(timestamp_ms: str, method: str, request_path: str, body: str = "") -> str:
    payload = (timestamp_ms + method.upper() + request_path + body).encode()
    return hmac.new(API_SECRET, payload, hashlib.sha256).hexdigest()

def build_headers(timestamp_ms: str, signature: Optional[str] = None) -> Dict[str, str]:
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-BTK-APIKEY": API_KEY,
        "X-BTK-TIMESTAMP": timestamp_ms,
    }
    if signature:
        h["X-BTK-SIGN"] = signature
    return h

# ------------------------------------------------------------
# [4] HTTP
# ------------------------------------------------------------
def http_get(url, params=None, timeout=HTTP_TIMEOUT):
    last_exc = None
    for i in range(RETRY_MAX):
        try:
            r = session.get(url, params=params, headers=COMMON_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            _backoff_sleep(i)
    raise last_exc

def http_post(url, headers=None, data="{}", timeout=HTTP_TIMEOUT):
    h = COMMON_HEADERS.copy()
    if headers:
        h.update(headers)
    last_exc = None
    for i in range(RETRY_MAX):
        try:
            r = session.post(url, headers=h, data=data, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            _backoff_sleep(i)
    raise last_exc

# ------------------------------------------------------------
# [5] PUBLIC/PRIVATE API
# ------------------------------------------------------------
def get_trades(sym: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    ใช้ /api/v3/market/trades (non-secure) ดึง trade ล่าสุด
    """
    url = f"{BASE_URL}/api/v3/market/trades"
    params = {"sym": sym, "lmt": limit}
    for i in range(RETRY_MAX):
        try:
            r = http_get(url, params=params, timeout=10)
            data = r.json()
            if isinstance(data, dict):
                if data.get("error") not in (0, None):
                    slog(f"[TRADES ERR] code={data.get('error')}")
                    return []
                res = data.get("result")
                return res if isinstance(res, list) else []
            elif isinstance(data, list):
                return data
            return []
        except Exception as e:
            slog(f"[TRADES EXC#{i+1}] {e}")
            _backoff_sleep(i)
    return []

def place_bid(sym: str, thb_amount: float, rate: float, dry_run: bool) -> Dict[str, Any]:
    method, path = "POST", "/api/v3/market/place-bid"
    ts = ts_ms_str()
    payload = {
        "sym": sym,
        "amt": float(int(thb_amount)),
        "rat": float(round(rate, PRICE_ROUND)),
        "typ": "limit"
    }
    body = json.dumps(payload, separators=(",", ":"))
    if dry_run:
        return {"dry_run": True, "endpoint": path, "payload": payload}
    sg = sign(ts, method, path, body)
    r = http_post(BASE_URL + path, headers=build_headers(ts, sg), data=body, timeout=HTTP_TIMEOUT)
    return r.json()

def place_ask(sym: str, qty_coin: float, rate: float, dry_run: bool) -> Dict[str, Any]:
    method, path = "POST", "/api/v3/market/place-ask"
    ts = ts_ms_str()
    payload = {
        "sym": sym,
        "amt": float(round(qty_coin, QTY_ROUND)),
        "rat": float(round(rate, PRICE_ROUND)),
        "typ": "limit"
    }
    body = json.dumps(payload, separators=(",", ":"))
    if dry_run:
        return {"dry_run": True, "endpoint": path, "payload": payload}
    sg = sign(ts, method, path, body)
    r = http_post(BASE_URL + path, headers=build_headers(ts, sg), data=body, timeout=HTTP_TIMEOUT)
    return r.json()

def market_wallet() -> Dict[str, Any]:
    method, path = "POST", "/api/v3/market/wallet"
    ts = ts_ms_str()
    body = "{}"
    sg = sign(ts, method, path, body)
    r = http_post(BASE_URL + path, headers=build_headers(ts, sg), data=body, timeout=HTTP_TIMEOUT)
    return r.json()

def market_balances() -> Dict[str, Any]:
    method, path = "POST", "/api/v3/market/balances"
    ts = ts_ms_str()
    body = "{}"
    sg = sign(ts, method, path, body)
    r = http_post(BASE_URL + path, headers=build_headers(ts, sg), data=body, timeout=HTTP_TIMEOUT)
    return r.json()

def get_available(asset: str) -> float:
    try:
        res = market_balances()
        if res.get("result") and res["result"].get(asset):
            node = res["result"][asset]
            if isinstance(node, dict) and "available" in node:
                return float(node["available"])
    except Exception as e:
        slog(f"[BAL ERR] balances {e}")
    try:
        res = market_wallet()
        if res.get("result") and asset in res["result"]:
            return float(res["result"][asset])
    except Exception as e:
        slog(f"[BAL ERR] wallet {e}")
    return 0.0

# ------------------------------------------------------------
# [6] STRATEGY UTILS — vwap_tail, zscore, RSI, candle from trades
# ------------------------------------------------------------
def vwap_tail(trades: List[Dict[str, Any]], tail: int = 10) -> Optional[float]:
    if not trades:
        return None
    t = trades[-min(tail, len(trades)):]
    total = 0.0
    qty = 0.0
    for x in t:
        rate = None
        amt  = None
        if isinstance(x, dict):
            rate = x.get("rate", x.get("rat"))
            amt  = x.get("amount", x.get("amt"))
        elif isinstance(x, (list, tuple)) and len(x) >= 3:
            try:
                rate = float(x[1]); amt = float(x[2])
            except Exception:
                try:
                    rate = float(x[2]); amt = float(x[1])
                except Exception:
                    pass
        if rate is None or amt is None:
            continue
        rate = float(rate); amt = float(amt)
        total += amt * rate
        qty   += amt
    if qty <= 0:
        last = t[-1]
        if isinstance(last, dict):
            last_rate = last.get("rate", last.get("rat"))
            return float(last_rate) if last_rate is not None else None
        return None
    return total / qty

def compute_zscore(series: List[float], window: int) -> Optional[float]:
    if len(series) < window or window < 2:
        return None
    sample = list(series)[-window:]
    mu = mean(sample)
    sig = pstdev(sample) or 1e-9
    return (series[-1] - mu) / sig

def compute_rsi(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    arr = np.asarray(closes, dtype=float)
    out = ta.RSI(arr, timeperiod=period)
    val = out[-1]
    return None if (val != val) else float(val)  # NaN check

def extract_trade_ts_ms(trade: Any) -> Optional[int]:
    if isinstance(trade, dict):
        for k in ("ts", "tsms", "timestamp", "t"):
            if k in trade:
                try:
                    v = int(trade[k])
                    return v if v > 10_000_000_000 else v * 1000
                except Exception:
                    pass
    elif isinstance(trade, (list, tuple)) and len(trade) >= 1:
        try:
            v = int(trade[0])
            return v if v > 10_000_000_000 else v * 1000
        except Exception:
            pass
    return None

def candle_bucket_start_ms(ts_ms: int, candle_sec: int = CANDLE_SEC) -> int:
    return (ts_ms // (candle_sec * 1000)) * (candle_sec * 1000)

def build_5m_candles_from_trades(
    trades: List[Dict[str, Any]],
    last_closed_bucket_ms: Optional[int],
    closes_5m: deque,
) -> Tuple[Optional[int], Optional[float], bool]:
    """
    สร้างแท่ง 5 นาทีจาก trade ทั้งก้อนในรอบนั้น
    - รวบ trade ตาม bucket 5 นาที (ใช้ timestamp ของ trade)
    - เอา trade สุดท้ายของแต่ละ bucket เป็น close
    - เติมเข้า closes_5m เฉพาะ bucket ที่ > last_closed_bucket_ms เท่านั้น
    """
    if not trades:
        return last_closed_bucket_ms, None, False

    items: List[Tuple[int, float]] = []
    for t in trades:
        ts = extract_trade_ts_ms(t)
        if ts is None:
            continue
        px = None
        if isinstance(t, dict):
            px = t.get("rate", t.get("rat"))
        elif isinstance(t, (list, tuple)) and len(t) >= 3:
            try:
                px = float(t[1])
            except Exception:
                try:
                    px = float(t[2])
                except Exception:
                    pass
        if px is None:
            continue
        items.append((ts, float(px)))

    if not items:
        return last_closed_bucket_ms, None, False

    # sort ตามเวลา
    items.sort(key=lambda x: x[0])

    # map: bucket_start_ms -> last price ใน bucket นั้น
    bucket_close: Dict[int, float] = {}
    for ts, px in items:
        b = candle_bucket_start_ms(ts)
        bucket_close[b] = px  # last trade in this bucket overwrites previous

    if not bucket_close:
        return last_closed_bucket_ms, None, False

    new_close = None
    closed_any = False

    for b in sorted(bucket_close.keys()):
        if (last_closed_bucket_ms is None) or (b > last_closed_bucket_ms):
            closes_5m.append(bucket_close[b])
            last_closed_bucket_ms = b
            new_close = bucket_close[b]
            closed_any = True

    return last_closed_bucket_ms, new_close, closed_any

# ------------------------------------------------------------
# [7] MAIN LOOP
# ------------------------------------------------------------
def run_loop():
    sync_server_time()

    price_series: deque = deque(maxlen=MAX_SERIES_LEN)
    closes_5m:    deque = deque(maxlen=3000)
    last_closed_bucket_ms: Optional[int] = None
    initialized_candles = False

    log(f"Bitkub MR+RSI — {SYMBOL} | DRY_RUN={DRY_RUN} | "
        f"RSI_PERIOD={RSI_PERIOD} TH({RSI_BUY_TH}/{RSI_SELL_TH}) | "
        f"CONFIRM={USE_RSI_CONFIRM} RSI_ONLY={RSI_ONLY_MODE} | SHORT_LOG={SHORT_LOG}")

    global _last_heartbeat
    _last_heartbeat = time.time()

    while True:
        try:
            trades = get_trades(SYMBOL, limit=TRADES_FETCH)
            if not trades:
                slog(f"[NO TRADES] retry {REFRESH_SEC}s")
                time.sleep(REFRESH_SEC)
                continue

            # vwap tail → price_series (ใช้กับ z-score)
            px = vwap_tail(trades, tail=10)
            if px is not None:
                price_series.append(px)
            z = compute_zscore(list(price_series), WINDOW) if px is not None else None

            # สร้าง / อัปเดตแท่ง 5m จาก trades ย้อนหลัง
            prev_bucket = last_closed_bucket_ms
            last_closed_bucket_ms, new_close, candle_closed = build_5m_candles_from_trades(
                trades, last_closed_bucket_ms, closes_5m
            )

            # heartbeat
            now = time.time()
            if now - _last_heartbeat >= HEARTBEAT_SEC:
                _last_heartbeat = now
                slog(f"[HB] px={fmt(px,4)} z={fmt(z,2)} n5m={len(closes_5m)}")

            # รอบแรก: seed แท่งย้อนหลังเฉย ๆ ก่อน ยังไม่ต้องเทรด
            if not initialized_candles:
                if last_closed_bucket_ms is not None:
                    initialized_candles = True
                    slog(f"[INIT 5M] n5m={len(closes_5m)}")
                time.sleep(REFRESH_SEC)
                continue

            # มีแท่งใหม่ปิด (bucket ใหม่ > bucket เดิม)
            if candle_closed and last_closed_bucket_ms != prev_bucket:
                rsi_val = compute_rsi(list(closes_5m), RSI_PERIOD)
                bid_px = round(px * (1 - SLIPPAGE_BPS / 10000), PRICE_ROUND) if px else None
                ask_px = round(px * (1 + SLIPPAGE_BPS / 10000), PRICE_ROUND) if px else None

                do_buy = do_sell = False
                if RSI_ONLY_MODE:
                    if rsi_val is not None and rsi_val <= RSI_BUY_TH:
                        do_buy = True
                    if rsi_val is not None and rsi_val >= RSI_SELL_TH:
                        do_sell = True
                elif USE_RSI_CONFIRM:
                    if (rsi_val is not None and rsi_val <= RSI_BUY_TH) and (z is not None and z <= -THRESH_Z):
                        do_buy = True
                    if (rsi_val is not None and rsi_val >= RSI_SELL_TH) and (z is not None and z >= THRESH_Z):
                        do_sell = True
                else:
                    if z is not None and z <= -THRESH_Z:
                        do_buy = True
                    if z is not None and z >= THRESH_Z:
                        do_sell = True

                if do_buy and bid_px is not None:
                    thb_avail = get_available("THB")
                    if thb_avail >= ORDER_NOTIONAL_THB:
                        qty_est = ORDER_NOTIONAL_THB / bid_px
                        resp = place_bid(SYMBOL, ORDER_NOTIONAL_THB, bid_px, dry_run=DRY_RUN)
                        slog(f"[BUY] px={fmt(px,4)} z={fmt(z,2)} RSI={fmt(rsi_val,2)} "
                             f"bid≈{fmt(bid_px,2)} THB≈{ORDER_NOTIONAL_THB} (~{fmt(qty_est,6)}) DRY={DRY_RUN}")
                    else:
                        slog(f"[SKIP BUY] THB {thb_avail:.2f}<{ORDER_NOTIONAL_THB}")

                elif do_sell and ask_px is not None:
                    xrp_avail = get_available("XRP")
                    if xrp_avail > 0:
                        sell_qty = round(xrp_avail * 0.5, QTY_ROUND)
                        if sell_qty > 0:
                            resp = place_ask(SYMBOL, sell_qty, ask_px, dry_run=DRY_RUN)
                            slog(f"[SELL] px={fmt(px,4)} z={fmt(z,2)} RSI={fmt(rsi_val,2)} "
                                 f"ask≈{fmt(ask_px,2)} qty≈{fmt(sell_qty,6)} DRY={DRY_RUN}")
                        else:
                            slog("[SKIP SELL] qty too small")
                    else:
                        slog(f"[SKIP SELL] XRP {xrp_avail:.6f}")
                else:
                    slog(f"[CLOSE] close={fmt(new_close,4)} RSI={fmt(rsi_val,2)} "
                         f"z={fmt(z,2)} n5m={len(closes_5m)}")

        except requests.HTTPError as e:
            log(f"[HTTP ERROR] {getattr(e.response, 'text', str(e))}")
        except Exception as e:
            log(f"[ERROR] {e}")

        time.sleep(REFRESH_SEC)

# ------------------------------------------------------------
# [8] ENTRY
# ------------------------------------------------------------
if __name__ == "__main__":
    run_loop()
