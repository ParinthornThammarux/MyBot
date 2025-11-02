# ============================================================
#  Bitkub Mean Reversion Bot — THB_XRP (เวอร์ชันอ่านยอดจริง)
#  [Fixed: use v3 trades + robust vwap_tail]
# ============================================================

import os, time, hmac, hashlib, json, requests
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

SYMBOL = "THB_XRP"
WINDOW = 80
THRESH_Z = 1.6
REFRESH_SEC = 3
ORDER_NOTIONAL_THB = 100
SLIPPAGE_BPS = 8
DRY_RUN = True
PRICE_ROUND = 2
QTY_ROUND = 6
MAX_SERIES_LEN = 5000
TRADES_FETCH = max(200, WINDOW + 5)
TIME_SYNC_INTERVAL = 300

# Optional: เปิดดู sample เทรดทุกๆ 60 วินาที
DEBUG_SAMPLE_TRADE = True

# ------------------------------------------------------------
# [2] SERVER TIME SYNC
# ------------------------------------------------------------
_server_offset_ms = 0
_last_sync_ts = 0

def sync_server_time():
    global _server_offset_ms, _last_sync_ts
    try:
        url = f"{BASE_URL}/api/v3/servertime"
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        server_time = int(r.json())
        local_time = int(time.time() * 1000)
        _server_offset_ms = server_time - local_time
        _last_sync_ts = time.time()
        readable_time = datetime.datetime.fromtimestamp(server_time / 1000)
        print(f"[SYNC] offset={_server_offset_ms} ms, server={readable_time:%Y-%m-%d %H:%M:%S}")
    except Exception as e:
        print("[SYNC ERROR]", e)

def ts_ms_str() -> str:
    global _server_offset_ms, _last_sync_ts
    now = time.time()
    if now - _last_sync_ts > TIME_SYNC_INTERVAL:
        sync_server_time()
    local_ms = int(now * 1000)
    return str(local_ms + _server_offset_ms)

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
# [4] PUBLIC API  — FIXED to use v3 and unwrap "result"
# ------------------------------------------------------------
def get_trades(sym: str, limit: int = 10) -> List[Dict[str, Any]]:
    url = f"{BASE_URL}/api/v3/market/trades"  # ใช้ v3
    r = requests.get(url, params={"sym": sym, "lmt": limit}, timeout=10)
    r.raise_for_status()
    data = r.json()
    # v3 มักคืน {"error":0,"result":[ {...}, {...} ]}
    if isinstance(data, dict) and "result" in data:
        return data["result"] or []
    # กันเหนียว: ถ้าบางกรณีได้ลิสต์ดิบๆ มาก็ส่งกลับไปเลย
    return data if isinstance(data, list) else []

# ------------------------------------------------------------
# [5] PRIVATE TRADE API
# ------------------------------------------------------------
def place_bid(sym: str, thb_amount: float, rate: float, dry_run: bool = True) -> Dict[str, Any]:
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
    r = requests.post(BASE_URL + path, headers=build_headers(ts, sg), data=body, timeout=10)
    r.raise_for_status()
    return r.json()

def place_ask(sym: str, qty_coin: float, rate: float, dry_run: bool = True) -> Dict[str, Any]:
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
    r = requests.post(BASE_URL + path, headers=build_headers(ts, sg), data=body, timeout=10)
    r.raise_for_status()
    return r.json()

# ------------------------------------------------------------
# [5.1] ACCOUNT — Balance
# ------------------------------------------------------------
def market_wallet() -> Dict[str, Any]:
    method, path = "POST", "/api/v3/market/wallet"
    ts = ts_ms_str()
    body = "{}"
    sg = sign(ts, method, path, body)
    r = requests.post(BASE_URL + path, headers=build_headers(ts, sg), data=body, timeout=10)
    r.raise_for_status()
    return r.json()

def market_balances() -> Dict[str, Any]:
    method, path = "POST", "/api/v3/market/balances"
    ts = ts_ms_str()
    body = "{}"
    sg = sign(ts, method, path, body)
    r = requests.post(BASE_URL + path, headers=build_headers(ts, sg), data=body, timeout=10)
    r.raise_for_status()
    return r.json()

def get_available(asset: str) -> float:
    try:
        res = market_balances()
        if res.get("result") and res["result"].get(asset):
            node = res["result"][asset]
            if isinstance(node, dict) and "available" in node:
                return float(node["available"])
    except Exception:
        pass
    try:
        res = market_wallet()
        if res.get("result") and asset in res["result"]:
            return float(res["result"][asset])
    except Exception:
        pass
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
            # โครงสร้าง v3: มักใช้ "rate" และ "amount" (บางที่ใช้ "amt")
            rate = x.get("rate", x.get("rat"))
            amt  = x.get("amount", x.get("amt"))
        elif isinstance(x, (list, tuple)) and len(x) >= 3:
            # เผื่อรูปแบบเก่าเป็นลิสต์: เดา index [1]=rate, [2]=amount
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
        # fallback: ใช้ราคาสุดท้ายถ้ามี
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

    print(f"Bitkub Mean Reversion Bot — {SYMBOL}")
    print(f"WINDOW={WINDOW} THRESH_Z={THRESH_Z} DRY_RUN={DRY_RUN}")

    while True:
        try:
            trades = get_trades(SYMBOL, limit=TRADES_FETCH)

            # debug schema ตัวอย่างเทรดทุกๆ นาที
            if DEBUG_SAMPLE_TRADE and trades and int(time.time()) % 60 == 0:
                print("[DEBUG] trade sample:", trades[-1])

            px = vwap_tail(trades, tail=10)
            if px is None:
                time.sleep(REFRESH_SEC)
                continue

            price_series.append(px)
            z = compute_zscore(price_series, WINDOW)
            if z is None:
                print("[WARMUP] collecting data...")
                time.sleep(REFRESH_SEC)
                continue

            bid_px = round(px * (1 - SLIPPAGE_BPS / 10000), PRICE_ROUND)
            ask_px = round(px * (1 + SLIPPAGE_BPS / 10000), PRICE_ROUND)

            if z <= -THRESH_Z:
                thb_avail = get_available("THB")
                if thb_avail < ORDER_NOTIONAL_THB:
                    print(f"[SKIP BUY] THB={thb_avail:.2f} < {ORDER_NOTIONAL_THB}")
                else:
                    qty_est = ORDER_NOTIONAL_THB / bid_px
                    resp = place_bid(SYMBOL, ORDER_NOTIONAL_THB, bid_px, dry_run=DRY_RUN)
                    print(f"[BUY ] z={z:.2f} bid≈{bid_px} THB≈{ORDER_NOTIONAL_THB} (~{qty_est:.6f} XRP) -> {resp}")

            elif z >= THRESH_Z:
                xrp_avail = get_available("XRP")
                if xrp_avail <= 0:
                    print(f"[SKIP SELL] XRP={xrp_avail:.6f}")
                else:
                    sell_qty = round(xrp_avail * 0.5, QTY_ROUND)
                    if sell_qty > 0:
                        resp = place_ask(SYMBOL, sell_qty, ask_px, dry_run=DRY_RUN)
                        print(f"[SELL] z={z:.2f} ask≈{ask_px} qty≈{sell_qty:.6f} -> {resp}")
                    else:
                        print(f"[SKIP SELL] qty too small after rounding")

            else:
                print(f"[HOLD] px={px:.4f} z={z:.2f}")

        except requests.HTTPError as e:
            print("HTTP error:", getattr(e.response, "text", str(e)))
        except Exception as e:
            print("Error:", e)

        time.sleep(REFRESH_SEC)

# ------------------------------------------------------------
# [8] ENTRY POINT
# ------------------------------------------------------------
if __name__ == "__main__":
    run_loop()
