import os, time, hmac, hashlib, json, requests, random
import datetime
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv

import pandas as pd
import pandas_ta as ta

load_dotenv()

# ------------------------------------------------------------
# COLOR CONSTANTS (ANSI)
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

# ------------------------------------------------------------
# [1] CONFIGURATION
# ------------------------------------------------------------
BASE_URL = "https://api.bitkub.com"
API_KEY  = os.getenv("BITKUB_API_KEY", "")
API_SECRET = (os.getenv("BITKUB_API_SECRET", "") or "").encode()

SYMBOL = "XRP_THB"          # ใช้คู่เทรดสำหรับส่งออเดอร์

REFRESH_SEC = 60            # วินาทีต่อการวนลูป 1 รอบ
ORDER_NOTIONAL_THB = 100    # ขนาดออเดอร์ต่อไม้ (THB)
SLIPPAGE_BPS = 6            # slippage (bps) สำหรับตั้ง bid/ask ให้ match ง่ายขึ้น
FEE_RATE = 0.0025           # 0.25% ต่อข้าง

DRY_RUN = True              # True = ทดสอบ, False = ยิง order จริง

PRICE_ROUND = 2
QTY_ROUND = 6

TIME_SYNC_INTERVAL = 300    # วินาทีในการ resync server time
COOLDOWN_SEC = 300          # วินาที cooldown หลังเทรด (เช่น 300 = 5 นาที)

POS_FILE = "Cost.json"      # ไฟล์เก็บสถานะ position

# Debug/Networking
DEBUG_HTTP = True
HTTP_TIMEOUT = 12
RETRY_MAX = 4
RETRY_BASE_DELAY = 0.6      # seconds

COMMON_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json"
}

session = requests.Session()

# ------------------------------------------------------------
# [2] HTTP + BACKOFF
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


def http_post(url, headers=None, data="{}", timeout=HTTP_TIMEOUT):
    h = COMMON_HEADERS.copy()
    if headers:
        h.update(headers)
    last_exc = None
    for i in range(RETRY_MAX):
        try:
            r = session.post(url, headers=h, data=data, timeout=timeout)
            if DEBUG_HTTP:
                body_dbg = data if len(data) < 300 else data[:300] + "...(+)"
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
# [3] SERVER TIME SYNC + LOGGING
# ------------------------------------------------------------
_server_offset_ms = 0
_last_sync_ts = 0


def now_server_ms() -> int:
    return int(time.time() * 1000) + _server_offset_ms


def now_server_dt() -> datetime.datetime:
    return datetime.datetime.fromtimestamp(now_server_ms() / 1000)


def ts_hms() -> str:
    return now_server_dt().strftime("%Y-%m-%d %H:%M:%S")


def color_for(msg: str) -> str:
    if "ERROR" in msg or "EXC" in msg:
        return FG_RED + BOLD

    if msg.startswith("[HTTP GET]") or msg.startswith("[HTTP POST]"):
        return FG_CYAN + DIM
    if "[HTTP GET ERROR" in msg or "[HTTP POST ERROR" in msg:
        return FG_RED

    if msg.startswith("[SYNC"):
        return FG_CYAN
    if msg.startswith("[POS]"):
        return FG_MAGENTA
    if msg.startswith("[PRICE]"):
        return FG_BLUE + BOLD
    if msg.startswith("[HOLD]"):
        return FG_CYAN
    if msg.startswith("[MACD]"):
        return FG_BLUE
    if msg.startswith("[BUY "):
        return FG_GREEN + BOLD
    if msg.startswith("[SELL]"):
        return FG_YELLOW + BOLD
    if msg.startswith("[COOLDOWN]"):
        return FG_YELLOW
    if msg.startswith("[SKIP"):
        return FG_YELLOW
    if "WARN" in msg:
        return FG_YELLOW + DIM
    return FG_WHITE


def log(msg: str):
    ts = ts_hms()
    color = color_for(msg)
    out = f"{DIM}[{ts}]{RESET} {color}{msg}{RESET}"
    print(out)


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
# [4] AUTH UTILITIES
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
# [5] PRIVATE TRADE API
# ------------------------------------------------------------
def place_bid(sym: str, thb_amount: float, rate: float, dry_run: bool) -> Dict[str, Any]:
    method, path = "POST", "/api/v3/market/place-bid"
    ts = ts_ms_str()
    payload = {
        "sym": sym,
        "amt": float(int(thb_amount)),               # ถ้า Bitkub รองรับทศนิยม ค่อยปรับตรงนี้
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
# [5.1] ACCOUNT — OPTIONAL HELPERS
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


# ------------------------------------------------------------
# [6] POSITION PERSISTENCE (Cost.json)
# ------------------------------------------------------------
def load_pos() -> Dict[str, Any]:
    if not os.path.exists(POS_FILE):
        return {
            "side": "FLAT",
            "entry_price": 0.0,
            "qty": 0.0,
            "last_trade_ts": 0
        }
    try:
        with open(POS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception as e:
        log(f"[POS ERROR] load_pos failed: {e}")
        return {
            "side": "FLAT",
            "entry_price": 0.0,
            "qty": 0.0,
            "last_trade_ts": 0
        }


def save_pos(pos: Dict[str, Any]):
    try:
        with open(POS_FILE, "w", encoding="utf-8") as f:
            json.dump(pos, f, ensure_ascii=False, indent=2)
        log(f"[POS] saved: {pos}")
    except Exception as e:
        log(f"[POS ERROR] save_pos failed: {e}")


# ------------------------------------------------------------
# [7] OHLCV (5m candles) VIA tradingview/history
# ------------------------------------------------------------
FIVE_MIN_SEC = 5 * 60


def fetch_5m_candles(sym: str, lookback_bars: int = 200) -> List[Dict[str, Any]]:
    now_sec = now_server_ms() // 1000
    frm = now_sec - lookback_bars * FIVE_MIN_SEC - FIVE_MIN_SEC

    url = f"{BASE_URL}/tradingview/history"
    params = {
        "symbol": sym,        # เช่น "XRP_THB"
        "resolution": "5",    # 5 นาที
        "from": frm,
        "to": now_sec
    }

    r = http_get(url, params=params, timeout=HTTP_TIMEOUT)
    data = r.json()

    # ปกติจะเป็น: { "s":"ok", "t":[...], "o":[...], "h":[...], "l":[...], "c":[...], "v":[...] }
    if not isinstance(data, dict) or data.get("s") != "ok":
        log(f"[ERROR] fetch_5m_candles unexpected payload: {data}")
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
# [8] MACD ด้วย pandas-ta
# ------------------------------------------------------------
def macd_signal_from_candles(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(candles) < 50:
        return {"signal": "HOLD"}

    df = pd.DataFrame(candles)
    df["dt"] = pd.to_datetime(df["ts"], unit="s")
    df.set_index("dt", inplace=True)

    macd_df = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if macd_df is None or macd_df.empty:
        return {"signal": "HOLD"}

    df = pd.concat([df, macd_df], axis=1)

    col_macd = macd_df.columns[0]      # เช่น MACD_12_26_9
    col_signal = macd_df.columns[1]    # เช่น MACDs_12_26_9
    col_hist = macd_df.columns[2]      # เช่น MACDh_12_26_9

    df_valid = df.dropna(subset=[col_macd, col_signal, col_hist])
    if len(df_valid) < 2:
        return {"signal": "HOLD"}

    prev_row = df_valid.iloc[-2]
    last_row = df_valid.iloc[-1]

    macd_prev, macd_now = prev_row[col_macd], last_row[col_macd]
    sig_prev, sig_now = prev_row[col_signal], last_row[col_signal]
    hist_now = last_row[col_hist]

    bullish_cross = macd_prev < sig_prev and macd_now > sig_now
    bearish_cross = macd_prev > sig_prev and macd_now < sig_now

    if bullish_cross and hist_now > 0:
        sig = "BUY"
    elif bearish_cross and hist_now < 0:
        sig = "SELL"
    else:
        sig = "HOLD"

    log(
        f"[MACD] macd_now={macd_now:.6f} signal_now={sig_now:.6f} "
        f"hist={hist_now:.6f} -> {sig}"
    )
    return {
        "signal": sig,
        "macd": float(macd_now),
        "signal_line": float(sig_now),
        "hist": float(hist_now),
    }


# ------------------------------------------------------------
# [9] COOLDOWN CHECK
# ------------------------------------------------------------
def can_trade_after_cooldown(pos: Dict[str, Any]) -> bool:
    last_ts = pos.get("last_trade_ts", 0)
    now_sec = now_server_ms() // 1000
    if now_sec - last_ts < COOLDOWN_SEC:
        remain = COOLDOWN_SEC - (now_sec - last_ts)
        log(f"[COOLDOWN] wait {remain:.0f}s more before next trade")
        return False
    return True


# ------------------------------------------------------------
# [10] EXECUTE STRATEGY (MACD 5m) - ใช้เฉพาะราคา close
# ------------------------------------------------------------
def decide_and_trade_macd():
    pos = load_pos()
    side = pos.get("side", "FLAT")

    candles = fetch_5m_candles(SYMBOL, lookback_bars=200)
    if not candles:
        log("[ERROR] No candles fetched, skip this round")
        return

    last_close = candles[-1]["close"]
    log(f"[PRICE] {SYMBOL} last close (5m) = {last_close:.4f}")

    macd_sig = macd_signal_from_candles(candles)
    sig = macd_sig.get("signal", "HOLD")

    if sig == "HOLD":
        log("[HOLD] No MACD cross signal")
        return

    if not can_trade_after_cooldown(pos):
        return

    # ใช้ราคา close แท่งล่าสุด + slippage เล็กน้อย
    if sig == "BUY":
        if side == "LONG":
            log("[SKIP] Already LONG, skip BUY")
            return

        price = round(last_close * (1 + SLIPPAGE_BPS / 10000), PRICE_ROUND)
        thb_amount = ORDER_NOTIONAL_THB

        log(f"[BUY ] Signal=BUY @ {price} THB amount={thb_amount} (dry_run={DRY_RUN})")
        res = place_bid(SYMBOL, thb_amount, price, DRY_RUN)
        log(f"[BUY ] result: {res}")

        now_sec = now_server_ms() // 1000
        pos["side"] = "LONG"
        pos["entry_price"] = price
        qty = (thb_amount / price) * (1.0 - FEE_RATE)
        pos["qty"] = qty
        pos["last_trade_ts"] = now_sec
        save_pos(pos)
        return

    if sig == "SELL":
        if side != "LONG" or pos.get("qty", 0) <= 0:
            log("[SKIP] No LONG position to close, skip SELL")
            return

        qty = pos["qty"]
        price = round(last_close * (1 - SLIPPAGE_BPS / 10000), PRICE_ROUND)

        log(f"[SELL] Signal=SELL qty={qty} @ {price} THB (dry_run={DRY_RUN})")
        res = place_ask(SYMBOL, qty, price, DRY_RUN)
        log(f"[SELL] result: {res}")

        now_sec = now_server_ms() // 1000
        pos["side"] = "FLAT"
        pos["entry_price"] = 0.0
        pos["qty"] = 0.0
        pos["last_trade_ts"] = now_sec
        save_pos(pos)
        return


# ------------------------------------------------------------
# [11] MAIN LOOP (MACD 5m BOT)
# ------------------------------------------------------------
def run_macd_bot():
    log(f"[INIT] Starting MACD 5m bot on {SYMBOL}, DRY_RUN={DRY_RUN}")
    sync_server_time()

    while True:
        try:
            decide_and_trade_macd()
        except Exception as e:
            log(f"[ERROR] Exception in main loop: {e}")
        time.sleep(REFRESH_SEC)


if __name__ == "__main__":
    run_macd_bot()
