import os, time, hmac, hashlib, json, requests, random, datetime
from typing import Dict, Any, Optional

from dotenv import load_dotenv
import pandas as pd
import pandas_ta as ta
from pathlib import Path

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

SYMBOL = "XRP_THB"

REFRESH_SEC = 305         # วินาทีต่อการวนลูป 1 รอบ

ORDER_NOTIONAL_THB = 100     # ขนาด order ต่อครั้ง (THB)
SLIPPAGE_BPS = 6             # slippage (bps) สำหรับตั้ง bid/ask ให้ match ง่ายขึ้น

FEE_RATE = 0.0025            # 0.25% ต่อข้าง (ซื้อ 0.25% + ขาย 0.25%)
DRY_RUN = True               # True = ทดสอบ, False = ยิง order จริง

PRICE_ROUND = 2
QTY_ROUND = 6

TIME_SYNC_INTERVAL = 300     # วินาทีในการ resync server time
COOLDOWN_SEC = 300           # วินาที cooldown หลังเทรด (เช่น 300 = 5 นาที)

POS_FILE = "Cost.json"       # ไฟล์เก็บสถานะ position

# Debug/Networking
DEBUG_HTTP = True
HTTP_TIMEOUT = 12
RETRY_MAX = 4
RETRY_BASE_DELAY = 0.6       # seconds

COMMON_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json"
}

session = requests.Session()

# ------------------------------------------------------------
# [2] LOGGING + TIME SYNC
# ------------------------------------------------------------
_server_offset_ms = 0
_last_sync_ts = 0


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


def now_server_ms() -> int:
    return int(time.time() * 1000) + _server_offset_ms


def now_server_dt() -> datetime.datetime:
    return datetime.datetime.fromtimestamp(now_server_ms() / 1000)


def ts_hms() -> str:
    return now_server_dt().strftime("%Y-%m-%d %H:%M:%S")


def color_for(msg: str) -> str:
    """
    เลือกสีตามประเภท log จาก prefix / keyword ในข้อความ
    """
    # ERROR / EXCEPTION
    if "ERROR" in msg or "EXC" in msg:
        return FG_RED + BOLD

    # HTTP DEBUG
    if msg.startswith("[HTTP GET]") or msg.startswith("[HTTP POST]"):
        return FG_CYAN + DIM
    if "[HTTP GET ERROR" in msg or "[HTTP POST ERROR" in msg:
        return FG_RED

    # SYNC / TIME
    if msg.startswith("[SYNC"):
        return FG_CYAN

    # POSITION
    if msg.startswith("[POS]"):
        return FG_MAGENTA

    # PRICE / HOLD
    if msg.startswith("[PRICE]"):
        return FG_BLUE + BOLD
    if msg.startswith("[HOLD]"):
        return FG_CYAN

    # BUY / SELL / COOLDOWN
    if msg.startswith("[BUY "):
        return FG_GREEN + BOLD
    if msg.startswith("[SELL]"):
        return FG_YELLOW + BOLD
    if msg.startswith("[COOLDOWN]"):
        return FG_YELLOW

    # SKIP / WARN
    if msg.startswith("[SKIP"):
        return FG_YELLOW
    if "WARN" in msg:
        return FG_YELLOW + DIM

    # DEFAULT
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
# [4] PRIVATE TRADE API
# ------------------------------------------------------------
def place_bid(sym: str, thb_amount: float, rate: float, dry_run: bool) -> Dict[str, Any]:
    method, path = "POST", "/api/v3/market/place-bid"
    ts = ts_ms_str()
    payload = {
        "sym": sym,
        "amt": float(int(thb_amount)),  # ถ้า Bitkub รองรับทศนิยม ค่อยเปลี่ยน logic ตรงนี้
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


def market_balances() -> Dict[str, Any]:
    method, path = "POST", "/api/v3/market/balances"
    ts = ts_ms_str()
    body = "{}"
    sg = sign(ts, method, path, body)
    r = http_post(BASE_URL + path, headers=build_headers(ts, sg), data=body, timeout=HTTP_TIMEOUT)
    return r.json()


# ------------------------------------------------------------
# [5] STRATEGY CONFIG - RSI + ADX ONLY
# ------------------------------------------------------------

RESOLUTION = "5"          # "240" = 4H, "60" = 1H, "15" = 15m
CANDLE_LIMIT = 300         # จำนวนแท่งเทียนย้อนหลังสำหรับคำนวณอินดิเคเตอร์

RSI_LENGTH = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

ADX_LENGTH = 14
ADX_TREND_THRESHOLD = 20   # ADX > 20 ถือว่ามีเทรนด์พอสมควร

ENABLE_SHORT = False       # Bitkub ไม่มี short margin ตรงๆ -> ให้ False ไว้ก่อน


# ------------------------------------------------------------
# [6] CANDLE FETCHING - TradingView API ของ Bitkub
# ------------------------------------------------------------
def fetch_candles(symbol: str, resolution: str, limit: int = 300) -> pd.DataFrame:
    """
    ดึงแท่งเทียนจาก Bitkub TradingView API
    return: DataFrame index เป็น datetime, column: [ts, open, high, low, close, volume]
    """
    now_sec = now_server_ms() // 1000
    tf_sec = int(resolution) * 60
    need_sec = (limit + 5) * tf_sec   # ขอเผื่อ 5 แท่ง
    params = {
        "symbol": symbol,
        "resolution": resolution,
        "from": now_sec - need_sec,
        "to": now_sec
    }
    url = f"{BASE_URL}/tradingview/history"

    try:
        r = http_get(url, params=params, timeout=HTTP_TIMEOUT)
        data = r.json()
    except Exception as e:
        log(f"[ERROR] fetch_candles http error: {e}")
        return pd.DataFrame()

    if not isinstance(data, dict) or data.get("s") != "ok":
        log(f"[ERROR] fetch_candles bad payload: {data}")
        return pd.DataFrame()

    t = data.get("t") or []
    o = data.get("o") or []
    h = data.get("h") or []
    l = data.get("l") or []
    c = data.get("c") or []
    v = data.get("v") or []

    if not t or not c:
        log("[ERROR] fetch_candles no candles returned")
        return pd.DataFrame()

    df = pd.DataFrame({
        "ts": t,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": v
    })

    df["dt"] = pd.to_datetime(df["ts"], unit="s")
    df.set_index("dt", inplace=True)
    df = df.sort_index()

    # ตัดให้เหลือ limit แท่งล่าสุด
    if len(df) > limit:
        df = df.iloc[-limit:]

    return df


# ------------------------------------------------------------
# [7] INDICATORS + SIGNAL LOGIC
# ------------------------------------------------------------
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    เติม RSI, ADX, +DI, -DI ลงใน DataFrame
    """
    if df.empty:
        return df

    close = df["close"].astype(float)
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)

    # RSI
    df["rsi"] = ta.rsi(close, length=RSI_LENGTH)

    # ADX + DI
    adx_df = ta.adx(high=high, low=low, close=close, length=ADX_LENGTH)
    df["adx"]      = adx_df[f"ADX_{ADX_LENGTH}"]
    df["plus_di"]  = adx_df[f"DMP_{ADX_LENGTH}"]   # +DI
    df["minus_di"] = adx_df[f"DMN_{ADX_LENGTH}"]   # -DI

    return df


def detect_signal(df: pd.DataFrame, in_long: bool, in_short: bool) -> Dict[str, Any]:
    """
    Strategy: RSI + ADX

    - ใช้ ADX + DI ดูทิศทางและความแรงของเทรนด์
    - ใช้ RSI หา timing เข้า/ออก

    เงื่อนไข:

    LONG_ENTRY:
      1) ยังไม่มี Long
      2) ADX_now > ADX_TREND_THRESHOLD (มีเทรนด์ชัดพอ)
      3) +DI_now > -DI_now (ฝั่งขึ้นได้เปรียบ)
      4) RSI ตัดขึ้นจาก oversold (prev < RSI_OVERSOLD, now >= RSI_OVERSOLD)

    LONG_EXIT:
      1) มี Long อยู่
      2) อย่างใดอย่างหนึ่งเป็นจริง:
         - RSI ตัดลงจาก overbought (prev > RSI_OVERBOUGHT, now <= RSI_OVERBOUGHT)
         - หรือ -DI_now > +DI_now (แรงขายกลับมาชนะ)
         - หรือ ADX_now ลดต่ำกว่า ADX_TREND_THRESHOLD * 0.8 (เทรนด์เริ่มอ่อน)
    """
    if df.empty or len(df) < ADX_LENGTH + 5:
        return {"signal": "NONE", "reason": "WARMUP"}

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # ดึงค่า indicator ล่าสุด
    price_now      = float(last["close"])
    rsi_now        = float(last["rsi"])
    rsi_prev       = float(prev["rsi"])
    adx_now        = float(last["adx"])
    plus_di_now    = float(last["plus_di"])
    minus_di_now   = float(last["minus_di"])

    # ถ้ายังมี NaN อยู่ แปลว่ายัง warmup indicator ไม่ครบ
    if any(pd.isna([price_now, rsi_now, rsi_prev, adx_now, plus_di_now, minus_di_now])):
        return {"signal": "NONE", "reason": "INDICATOR_NAN"}

    strong_trend = adx_now > ADX_TREND_THRESHOLD
    up_trend     = plus_di_now > minus_di_now
    down_trend   = minus_di_now > plus_di_now

    # ---------- LONG ENTRY ----------
    long_entry_cond = (
        (not in_long)
        and strong_trend
        and up_trend
        and (rsi_prev < RSI_OVERSOLD <= rsi_now)  # RSI cross up oversold
    )

    # ---------- LONG EXIT ----------
    long_exit_cond = in_long and (
        (rsi_prev > RSI_OVERBOUGHT >= rsi_now) or      # RSI ออกจากโซน overbought
        (down_trend) or                                # แรงขายกลับมาชนะ
        (adx_now < ADX_TREND_THRESHOLD * 0.8)         # เทรนด์เริ่มอ่อน
    )

    if long_entry_cond:
        return {
            "signal": "LONG_ENTRY",
            "reason": (
                "ADX strong uptrend (ADX>threshold, +DI>-DI) "
                "+ RSI cross up from oversold"
            ),
        }

    if long_exit_cond:
        return {
            "signal": "LONG_EXIT",
            "reason": (
                "RSI cooled down from overbought or sellers dominate "
                "or trend strength dropped"
            ),
        }

    # ยังไม่รองรับฝั่ง short จริงใน Bitkub อยู่แล้ว
    return {"signal": "NONE", "reason": "NO_CONDITION"}


# ------------------------------------------------------------
# [8] POSITION MANAGEMENT (ใช้ไฟล์ POS_FILE)
# ------------------------------------------------------------
def load_position() -> Dict[str, Any]:
    p = Path(POS_FILE)
    if not p.exists():
        return {
            "symbol": SYMBOL,
            "side": "NONE",     # "LONG" หรือ "NONE"
            "qty": 0.0,
            "avg_price": 0.0,
            "updated": None
        }
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception as e:
        log(f"[POS ERROR] load_position: {e}")
        return {
            "symbol": SYMBOL,
            "side": "NONE",
            "qty": 0.0,
            "avg_price": 0.0,
            "updated": None
        }


def save_position(pos: Dict[str, Any]):
    try:
        pos["updated"] = ts_hms()
        with open(POS_FILE, "w", encoding="utf-8") as f:
            json.dump(pos, f, ensure_ascii=False, indent=2)
        log(f"[POS] saved: {pos}")
    except Exception as e:
        log(f"[POS ERROR] save_position: {e}")


def get_balances_safe() -> Dict[str, Any]:
    """
    คืน dict ของ balances["result"] หรือ {} ถ้า error
    """
    try:
        data = market_balances()
        if isinstance(data, dict) and data.get("error") == 0:
            return data.get("result", {}) or {}
        log(f"[ERROR] market_balances response: {data}")
    except Exception as e:
        log(f"[ERROR] market_balances exception: {e}")
    return {}


# ------------------------------------------------------------
# [9] EXECUTION HELPERS (ซื้อ/ขาย)
# ------------------------------------------------------------
def open_long_market_like(last_price: float, dry_run: bool = DRY_RUN):
    """
    ซื้อด้วย THB ตาม ORDER_NOTIONAL_THB (หรือเท่าที่มี)
    ใช้ limit order ที่ราคาขึ้นไปเล็กน้อยเพื่อให้ match ง่ายขึ้น
    *NOTE: สมมติว่า order ถูก fill ทันที (สำหรับบอทจริงควรเช็คสถานะ order เพิ่มเติม)
    """
    balances = get_balances_safe()
    thb_info = balances.get("THB") or {}
    thb_available = float(thb_info.get("available", 0))

    if thb_available < ORDER_NOTIONAL_THB * 0.9:
        log(f"[SKIP] THB not enough: {thb_available:.2f} THB")
        return None

    amt_thb = min(thb_available, ORDER_NOTIONAL_THB)
    rate = round(last_price * (1 + SLIPPAGE_BPS / 10000.0), PRICE_ROUND)

    log(f"[BUY LONG] sym={SYMBOL}, amt_thb={amt_thb:.2f}, rate={rate}, dry_run={dry_run}")
    resp = place_bid(SYMBOL, amt_thb, rate, dry_run=dry_run)

    if dry_run:
        # คำนวณ position สมมติ
        qty_coin = (amt_thb * (1 - FEE_RATE)) / rate
        pos = load_position()
        new_qty = pos["qty"] + qty_coin
        if new_qty <= 0:
            avg_price = 0.0
        else:
            cost_old = pos["avg_price"] * pos["qty"]
            cost_new = amt_thb
            avg_price = (cost_old + cost_new) / new_qty

        pos["side"] = "LONG"
        pos["qty"] = new_qty
        pos["avg_price"] = avg_price
        save_position(pos)
    else:
        # production: ควรดึง order info / balance ใหม่ แล้วค่อยอัปเดต pos
        pass

    return resp


def close_long_market_like(last_price: float, dry_run: bool = DRY_RUN):
    """
    ปิด LONG ทั้งหมด
    ใช้ limit order ที่ราคาต่ำลงเล็กน้อยให้ match ง่ายขึ้น
    """
    pos = load_position()
    if pos.get("side") != "LONG" or pos.get("qty", 0) <= 0:
        log("[SKIP] no long position to close")
        return None

    qty = float(pos["qty"])
    rate = round(last_price * (1 - SLIPPAGE_BPS / 10000.0), PRICE_ROUND)

    log(f"[SELL] close LONG sym={SYMBOL}, qty={qty:.6f}, rate={rate}, dry_run={dry_run}")
    resp = place_ask(SYMBOL, qty, rate, dry_run=dry_run)

    if dry_run:
        # สมมติปิดหมด
        pos["side"] = "NONE"
        pos["qty"] = 0.0
        pos["avg_price"] = 0.0
        save_position(pos)
    else:
        # production: ควรเช็คสถานะ order / balance ก่อนเคลียร์ pos
        pass

    return resp


# ------------------------------------------------------------
# [10] MAIN LOOP - RSI + ADX BOT
# ------------------------------------------------------------
def main_loop():
    log(f"[INIT] Starting RSI+ADX bot on {SYMBOL}, TF={RESOLUTION}, DRY_RUN={DRY_RUN}")
    sync_server_time()

    last_candle_ts = None
    last_trade_time = 0.0

    while True:
        try:
            df = fetch_candles(SYMBOL, RESOLUTION, limit=CANDLE_LIMIT)
            if df.empty:
                log("[WARN] NO CANDLES, skip this round")
                time.sleep(REFRESH_SEC)
                continue

            latest_row = df.iloc[-1]
            candle_ts = int(latest_row["ts"])

            # เช็คว่ามีแท่งใหม่หรือยัง
            if last_candle_ts is not None and candle_ts == last_candle_ts:
                log("[SKIP] No new candle yet")
                time.sleep(REFRESH_SEC)
                continue

            last_candle_ts = candle_ts

            # เติม indicators
            df = add_indicators(df)
            last = df.iloc[-1]
            price     = float(last["close"])
            rsi_val   = float(last.get("rsi", float("nan")))
            adx_val   = float(last.get("adx", float("nan")))
            plus_di   = float(last.get("plus_di", float("nan")))
            minus_di  = float(last.get("minus_di", float("nan")))

            log(
                f"[PRICE] close={price:.4f}, rsi={rsi_val:.2f}, "
                f"adx={adx_val:.2f}, +di={plus_di:.2f}, -di={minus_di:.2f}"
            )

            pos = load_position()
            in_long = pos.get("side") == "LONG" and pos.get("qty", 0) > 0
            in_short = False  # ไม่มี short จริง

            sig = detect_signal(df, in_long=in_long, in_short=in_short)
            signal = sig["signal"]
            reason = sig["reason"]

            # cooldown หลังเทรด
            now_t = time.time()
            if now_t - last_trade_time < COOLDOWN_SEC and signal != "NONE":
                remain = int(COOLDOWN_SEC - (now_t - last_trade_time))
                log(f"[COOLDOWN] {remain} sec remaining, skip signal={signal} ({reason})")
                time.sleep(REFRESH_SEC)
                continue

            if signal == "LONG_ENTRY":
                log(f"[SIGNAL] LONG_ENTRY ({reason})")
                resp = open_long_market_like(price, dry_run=DRY_RUN)
                last_trade_time = time.time()
                log(f"[RESULT] LONG_ENTRY resp={resp}")

            elif signal == "LONG_EXIT":
                log(f"[SIGNAL] LONG_EXIT ({reason})")
                resp = close_long_market_like(price, dry_run=DRY_RUN)
                last_trade_time = time.time()
                log(f"[RESULT] LONG_EXIT resp={resp}")

            else:
                log(f"[HOLD] signal={signal}, reason={reason}")

        except KeyboardInterrupt:
            log("[STOP] KeyboardInterrupt received, exiting.")
            break
        except Exception as e:
            log(f"[ERROR] main_loop: {e}")

        time.sleep(REFRESH_SEC)


if __name__ == "__main__":
    main_loop()
