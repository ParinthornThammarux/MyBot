# ============================================================
#  Bitkub Mean Reversion Bot — THB_XRP (real-balance + robust I/O)
#  [v3 trades + server-time logs + HTTP debug + retries + single DRY_RUN source]
# ============================================================

import os, time, hmac, hashlib, json, requests, math, random
import datetime
from statistics import mean, pstdev
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv
from collections import deque

load_dotenv()

# ------------------------------------------------------------
# [1] CONFIGURATION
# ------------------------------------------------------------
BASE_URL = "https://api.bitkub.com"
API_KEY  = os.getenv("BITKUB_API_KEY", "")
API_SECRET = (os.getenv("BITKUB_API_SECRET", "") or "").encode()

SYMBOL = "XRP_THB"

WINDOW = 30
REFRESH_SEC = 60
TRADES_FETCH = max(200, WINDOW + 20)

THRESH_Z = 2.0
ORDER_NOTIONAL_THB = 100
SLIPPAGE_BPS = 6 #set for order match

DRY_RUN = True #True for test

PRICE_ROUND = 2
QTY_ROUND = 6
MAX_SERIES_LEN = 5000

TIME_SYNC_INTERVAL = 300

# Debug/Networking
DEBUG_SAMPLE_TRADE = True
DEBUG_HTTP = True
HTTP_TIMEOUT = 12
RETRY_MAX = 4
RETRY_BASE_DELAY = 0.6  # seconds

COMMON_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json"
}

session = requests.Session()

def _backoff_sleep(i: int):
    # jittered exponential backoff
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

def http_post(url, headers=None, data="{}", timeout=HTTP_TIMEOUT):
    h = COMMON_HEADERS.copy()
    if headers:
        h.update(headers)
    last_exc = None
    for i in range(RETRY_MAX):
        try:
            r = session.post(url, headers=h, data=data, timeout=timeout)
            if DEBUG_HTTP:
                body_dbg = data if len(data) < 300 else data[:300]+"...(+)"
                print(f"[HTTP POST] {r.request.method} {r.url} -> {r.status_code} body={body_dbg}")
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            if DEBUG_HTTP:
                print(f"[HTTP POST ERROR#{i+1}] {url} err={e}")
            _backoff_sleep(i)
    raise last_exc

# ------------------------------------------------------------
# [2] SERVER TIME SYNC + LOGGING
# ------------------------------------------------------------
_server_offset_ms = 0
_last_sync_ts = 0

def now_server_ms() -> int:
    return int(time.time() * 1000) + _server_offset_ms

def now_server_dt() -> datetime.datetime:
    return datetime.datetime.fromtimestamp(now_server_ms() / 1000)

def ts_hms() -> str:
    return now_server_dt().strftime("%Y-%m-%d %H:%M:%S")

def log(msg: str):
    print(f"[{ts_hms()}] {msg}")

def sync_server_time():
    global _server_offset_ms, _last_sync_ts
    url = f"{BASE_URL}/api/v3/servertime"
    try:
        r = http_get(url, timeout=8)
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
        readable_time = datetime.datetime.fromtimestamp(server_time / 1000)
        log(f"[SYNC] offset={_server_offset_ms} ms, server={readable_time:%Y-%m-%d %H:%M:%S}")
    except Exception as e:
        log(f"[SYNC ERROR] {e}")

def ts_ms_str() -> str:
    global _last_sync_ts
    now = time.time()
    if now - _last_sync_ts > TIME_SYNC_INTERVAL:
        sync_server_time()
    return str(int(now * 1000) + _server_offset_ms)

# ------------------------------------------------------------
# [3] AUTH UTILITIES
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
# [4] PUBLIC API — robust v3 market/trades
# ------------------------------------------------------------
def get_trades(sym: str, limit: int = 10) -> List[Dict[str, Any]]:
    url = f"{BASE_URL}/api/v3/market/trades"
    params = {"sym": sym, "lmt": limit}
    for i in range(RETRY_MAX):
        try:
            r = http_get(url, params=params, timeout=10)
            data = r.json()
            if isinstance(data, dict):
                err = data.get("error")
                if err not in (0, None):
                    log(f"[TRADES ERROR] error_code={err} payload={data}")
                    return []
                res = data.get("result")
                if isinstance(res, list):
                    if DEBUG_HTTP and res:
                        print("[TRADES SAMPLE]", res[-1])
                    return res
            elif isinstance(data, list):
                if DEBUG_HTTP and data:
                    print("[TRADES SAMPLE(list)]", data[-1])
                return data
            log(f"[TRADES WARN] unexpected payload: {data}")
            return []
        except Exception as e:
            log(f"[TRADES EXC#{i+1}] {e}")
            _backoff_sleep(i)
    return []

# ------------------------------------------------------------
# [5] PRIVATE TRADE API
# ------------------------------------------------------------
def place_bid(sym: str, thb_amount: float, rate: float, dry_run: bool) -> Dict[str, Any]:
    method, path = "POST", "/api/v3/market/place-bid"
    ts = ts_ms_str()
    payload = {
        "sym": sym,
        "amt": float(int(thb_amount)),
        "rat": float(round(rate, PRICE_ROUND)),
        "typ": "limit",
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
        "typ": "limit",
    }
    body = json.dumps(payload, separators=(",", ":"))
    if dry_run:
        return {"dry_run": True, "endpoint": path, "payload": payload}
    sg = sign(ts, method, path, body)
    r = http_post(BASE_URL + path, headers=build_headers(ts, sg), data=body, timeout=HTTP_TIMEOUT)
    return r.json()

# ------------------------------------------------------------
# [5.1] ACCOUNT — Balance
# ------------------------------------------------------------
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
    # balances → wallet (fallback)
    try:
        res = market_balances()
        if res.get("result") and res["result"].get(asset):
            node = res["result"][asset]
            if isinstance(node, dict) and "available" in node:
                return float(node["available"])
    except Exception as e:
        log(f"[BAL ERR] balances {e}")
    try:
        res = market_wallet()
        if res.get("result") and asset in res["result"]:
            return float(res["result"][asset])
    except Exception as e:
        log(f"[BAL ERR] wallet {e}")
    return 0.0

# ------------------------------------------------------------
# [6] STRATEGY FUNCTIONS — robust vwap_tail
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

# ------------------------------------------------------------
# [7] MAIN LOOP
# ------------------------------------------------------------
def run_loop():
    sync_server_time()
    price_series: deque = deque(maxlen=MAX_SERIES_LEN)

    log(f"Bitkub Mean Reversion Bot — {SYMBOL}")
    log(f"WINDOW={WINDOW} THRESH_Z={THRESH_Z} DRY_RUN={DRY_RUN}")

    while True:
        try:
            trades = get_trades(SYMBOL, limit=TRADES_FETCH)
            if not trades:
                log(f"[NO TRADES] sym={SYMBOL} lmt={TRADES_FETCH}. retry in {REFRESH_SEC}s")
                time.sleep(REFRESH_SEC)
                continue

            if DEBUG_SAMPLE_TRADE and trades and int(time.time()) % 60 == 0:
                log(f"[DEBUG] trade sample: {trades[-1]}")

            px = vwap_tail(trades, tail=10)
            if px is None:
                log("[WARMUP] no price yet, waiting...")
                time.sleep(REFRESH_SEC)
                continue

            price_series.append(px)
            z = compute_zscore(price_series, WINDOW)
            if z is None:
                log(f"[WARMUP] collecting data... px={px:.4f} len={len(price_series)}/{WINDOW}")
                time.sleep(REFRESH_SEC)
                continue

            bid_px = round(px * (1 - SLIPPAGE_BPS / 10000), PRICE_ROUND)
            ask_px = round(px * (1 + SLIPPAGE_BPS / 10000), PRICE_ROUND)

            if z <= -THRESH_Z:
                thb_avail = get_available("THB")
                if thb_avail < ORDER_NOTIONAL_THB:
                    log(f"[SKIP BUY] THB={thb_avail:.2f} < {ORDER_NOTIONAL_THB} | px={px:.4f} z={z:.2f}")
                else:
                    qty_est = ORDER_NOTIONAL_THB / bid_px
                    resp = place_bid(SYMBOL, ORDER_NOTIONAL_THB, bid_px, dry_run=DRY_RUN)
                    log(f"[BUY ] z={z:.2f} px={px:.4f} bid≈{bid_px} THB≈{ORDER_NOTIONAL_THB} (~{qty_est:.6f} XRP) -> {resp}")

            elif z >= THRESH_Z:
                xrp_avail = get_available("XRP")
                if xrp_avail <= 0:
                    log(f"[SKIP SELL] XRP={xrp_avail:.6f} | px={px:.4f} z={z:.2f}")
                else:
                    sell_qty = round(xrp_avail * 0.5, QTY_ROUND)
                    if sell_qty > 0:
                        resp = place_ask(SYMBOL, sell_qty, ask_px, dry_run=DRY_RUN)
                        log(f"[SELL] z={z:.2f} px={px:.4f} ask≈{ask_px} qty≈{sell_qty:.6f} -> {resp}")
                    else:
                        log("[SKIP SELL] qty too small after rounding")
            else:
                log(f"[HOLD] px={px:.4f} z={z:.2f} bid≈{bid_px} ask≈{ask_px}")

        except requests.HTTPError as e:
            log(f"[HTTP ERROR] {getattr(e.response, 'text', str(e))}")
        except Exception as e:
            log(f"[ERROR] {e}")

        time.sleep(REFRESH_SEC)

# ------------------------------------------------------------
# [8] ENTRY POINT
# ------------------------------------------------------------
if __name__ == "__main__":
    run_loop()
