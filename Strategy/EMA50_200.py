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

REFRESH_SEC = 60            # วินาทีต่อการวนลูป 1 รอบ (จะเช็กทุก 1 นาทีด้วยแท่ง 1H)
ORDER_NOTIONAL_THB = 100    # ขนาดออเดอร์ต่อไม้ (THB)
SLIPPAGE_BPS = 0            # slippage (bps) สำหรับตั้ง bid/ask ให้ match ง่ายขึ้น
FEE_RATE = 0.0025           # 0.25% ต่อข้าง

DRY_RUN = False             # True = ทดสอบ, False = ยิง order จริง

PRICE_ROUND = 2
QTY_ROUND = 6

TIME_SYNC_INTERVAL = 300    # วินาทีในการ resync server time
COOLDOWN_SEC = 1800         # วินาที cooldown หลัง "เข้าไม้" (ใช้กับ BUY เท่านั้น)

POS_FILE = "Cost.json"      # ไฟล์เก็บสถานะ position

# --- EMA / ATR STRATEGY SETTINGS (จาก Pine Script) ---
EMA_FAST_LEN = 5
EMA_SLOW_LEN = 13
ATR_LEN = 14
ATR_MULT_SL = 0.5           # ระยะ SL ผูกกับ ATR
RR_TARGET = 3.0             # ต้องการ R:R = 3:1 (Reward : Risk)

CONFIRM_CANDLE = True       # เหมือน confirmCandle ใน Pine

# Debug/Networking
DEBUG_HTTP = False
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
    url = f"{BASE_URL}/api/servertime"
    try:
        r = http_get(url, timeout=8)
        data = r.json()
        # server time ของ Bitkub v1 จะคืนเป็น int ตรง ๆ หรืออยู่ใน "result"
        if isinstance(data, dict) and "result" in data:
            server_time = int(data["result"])
        else:
            server_time = int(data)
        local_time = int(time.time() * 1000)
        _server_offset_ms = server_time * 1000 - local_time
        _last_sync_ts = time.time()
        readable_time = datetime.datetime.fromtimestamp(server_time)
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
    method, path = "POST", "/api/market/place-bid"
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
    method, path = "POST", "/api/market/place-ask"
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
# [6] POSITION PERSISTENCE (Cost.json)
# ------------------------------------------------------------
def _default_pos() -> Dict[str, Any]:
    return {
        "side": "FLAT",        # "FLAT" หรือ "LONG"
        "entry_price": 0.0,
        "qty": 0.0,
        "last_trade_ts": 0,
        "stop_loss": 0.0,
        "take_profit": 0.0,
    }


def load_pos() -> Dict[str, Any]:
    if not os.path.exists(POS_FILE):
        return _default_pos()
    try:
        with open(POS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        defaults = _default_pos()
        for k, v in defaults.items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        log(f"[POS ERROR] load_pos failed: {e}")
        return _default_pos()


def save_pos(pos: Dict[str, Any]):
    try:
        with open(POS_FILE, "w", encoding="utf-8") as f:
            json.dump(pos, f, ensure_ascii=False, indent=2)
        log(f"[POS] saved: {pos}")
    except Exception as e:
        log(f"[POS ERROR] save_pos failed: {e}")


# ------------------------------------------------------------
# [7] OHLCV (1h candles) VIA Bitkub tradingview/history
# ------------------------------------------------------------
ONE_HR_SEC = 60 * 60


def fetch_1h_candles(sym: str, lookback_bars: int = 200) -> List[Dict[str, Any]]:
    """
    ดึงแท่งเทียน 1 ชั่วโมงย้อนหลัง lookback_bars แท่ง
    จาก Bitkub public endpoint: GET /tradingview/history
    """
    now_sec = now_server_ms() // 1000
    frm = now_sec - lookback_bars * ONE_HR_SEC - ONE_HR_SEC

    url = f"{BASE_URL}/tradingview/history"  # Bitkub API (Non-secure)
    params = {
        "symbol": sym,       # เช่น "XRP_THB"
        "resolution": "60",  # 60 นาที (1 ชั่วโมง)
        "from": frm,
        "to": now_sec
    }

    r = http_get(url, params=params, timeout=HTTP_TIMEOUT)
    data = r.json()

    # ปกติจะเป็น: { "s":"ok", "t":[...], "o":[...], "h":[...], "l":[...], "c":[...], "v":[...] }
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
# [8] COOLDOWN CHECK
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
# [9] STRATEGY: EMA + ATR + TP (R:R = 3:1, LONG ONLY)
# ------------------------------------------------------------
def decide_and_trade_ema_atr():
    """
    Logic จาก Pine EMA+ATR:
    - bullTrend = emaFast > emaSlow
    - trendChange = bullTrend != bullTrend[1]
    - buy/sell signal ตาม confirmCandle
    - SL ใช้ ATR, TP คิดจาก R:R = 3:1
    """
    pos = load_pos()
    side = pos.get("side", "FLAT")

    candles = fetch_1h_candles(SYMBOL, lookback_bars=200)
    if len(candles) < 50:
        log("[SKIP] Not enough candles for EMA/ATR")
        return

    df = pd.DataFrame(candles)
    df["dt"] = pd.to_datetime(df["ts"], unit="s")
    df.set_index("dt", inplace=True)

    # คำนวณ EMA และ ATR
    df["ema_fast"] = ta.ema(df["close"], length=EMA_FAST_LEN)
    df["ema_slow"] = ta.ema(df["close"], length=EMA_SLOW_LEN)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=ATR_LEN)

    df_valid = df.dropna(subset=["ema_fast", "ema_slow", "atr"])
    if len(df_valid) < 2:
        log("[SKIP] EMA/ATR not ready yet")
        return

    prev_row = df_valid.iloc[-2]
    last_row = df_valid.iloc[-1]

    last_close = float(last_row["close"])
    last_open  = float(last_row["open"])
    last_high  = float(last_row["high"])
    last_low   = float(last_row["low"])
    atr_now    = float(last_row["atr"])

    log(f"[PRICE] {SYMBOL} last close (1h) = {last_close:.4f}")

    # --------- 1) CHECK TP / SL EXIT (LONG ONLY) ----------
    if side == "LONG" and pos.get("qty", 0) > 0:
        sl = float(pos.get("stop_loss", 0.0) or 0.0)
        tp = float(pos.get("take_profit", 0.0) or 0.0)
        exit_reason = None

        if tp > 0 and last_high >= tp:
            exit_reason = "TP"
        elif sl > 0 and last_low <= sl:
            exit_reason = "SL"

        if exit_reason:
            qty = pos["qty"]
            price = round(last_close * (1 - SLIPPAGE_BPS / 10000), PRICE_ROUND)
            log(
                f"[SELL] {exit_reason} hit: last_close={last_close:.4f}, "
                f"entry={pos.get('entry_price', 0.0):.4f}, tp={tp:.4f}, sl={sl:.4f}, "
                f"qty={qty} @ {price} THB (dry_run={DRY_RUN})"
            )
            res = place_ask(SYMBOL, qty, price, DRY_RUN)
            log(f"[SELL] result: {res}")

            now_sec = now_server_ms() // 1000
            pos["side"] = "FLAT"
            pos["entry_price"] = 0.0
            pos["qty"] = 0.0
            pos["last_trade_ts"] = now_sec
            pos["stop_loss"] = 0.0
            pos["take_profit"] = 0.0
            save_pos(pos)
            return

    # --------- 2) BUILD SIGNALS จาก EMA/ATR LOGIC ----------
    bull_trend_now  = last_row["ema_fast"] > last_row["ema_slow"]
    bull_trend_prev = prev_row["ema_fast"] > prev_row["ema_slow"]
    bear_trend_now  = last_row["ema_fast"] < last_row["ema_slow"]
    bear_trend_prev = prev_row["ema_fast"] < prev_row["ema_slow"]

    trend_change = bull_trend_now != bull_trend_prev

    buyCondition1 = bull_trend_now and trend_change and (last_close > last_open)
    sellCondition1 = bear_trend_now and trend_change and (last_close < last_open)
    buyCondition2 = bull_trend_now and trend_change
    sellCondition2 = bear_trend_now and trend_change

    if CONFIRM_CANDLE:
        buySignal = buyCondition1
        sellSignal = sellCondition1
    else:
        buySignal = buyCondition2
        sellSignal = sellCondition2

    log(
        f"[EMA ATR DBG] ema_fast={last_row['ema_fast']:.4f}, "
        f"ema_slow={last_row['ema_slow']:.4f}, atr={atr_now:.4f}, "
        f"bull_now={bull_trend_now}, bull_prev={bull_trend_prev}, "
        f"trend_change={trend_change}, buySignal={buySignal}, sellSignal={sellSignal}"
    )

    # --------- 3) INVALIDATION: SELL เมื่อมี sellSignal ----------
    if sellSignal and side == "LONG" and pos.get("qty", 0) > 0:
        qty = pos["qty"]
        price = round(last_close * (1 - SLIPPAGE_BPS / 10000), PRICE_ROUND)
        log(
            f"[SELL] Invalidation by sellSignal: "
            f"qty={qty} @ {price} THB (dry_run={DRY_RUN})"
        )
        res = place_ask(SYMBOL, qty, price, DRY_RUN)
        log(f"[SELL] result: {res}")

        now_sec = now_server_ms() // 1000
        pos["side"] = "FLAT"
        pos["entry_price"] = 0.0
        pos["qty"] = 0.0
        pos["last_trade_ts"] = now_sec
        pos["stop_loss"] = 0.0
        pos["take_profit"] = 0.0
        save_pos(pos)
        return

    # --------- 4) NEW LONG ENTRY เมื่อมี buySignal ----------
    if buySignal:
        if side == "LONG" and pos.get("qty", 0) > 0:
            log("[SKIP] Already LONG, skip new BUY")
            return

        if not can_trade_after_cooldown(pos):
            return

        entry_price = last_close
        stop_loss = last_low - atr_now * ATR_MULT_SL
        risk = entry_price - stop_loss

        if risk <= 0:
            log(
                f"[SKIP] Invalid risk (entry={entry_price:.4f}, "
                f"stop_loss={stop_loss:.4f})"
            )
            return

        # TP จาก Risk:Reward = RR_TARGET (เช่น 3:1)
        take_profit = entry_price + risk * RR_TARGET

        thb_amount = ORDER_NOTIONAL_THB
        price_for_order = round(entry_price * (1 + SLIPPAGE_BPS / 10000), PRICE_ROUND)

        log(
            f"[BUY ] Signal=BUY (EMA/ATR) entry={entry_price:.4f}, "
            f"sl={stop_loss:.4f}, tp(RR={RR_TARGET:.1f})={take_profit:.4f}, "
            f"risk={risk:.4f}, "
            f"amt={thb_amount} THB @ {price_for_order} (dry_run={DRY_RUN})"
        )

        res = place_bid(SYMBOL, thb_amount, price_for_order, DRY_RUN)
        log(f"[BUY ] result: {res}")

        now_sec = now_server_ms() // 1000
        qty = (thb_amount / entry_price) * (1.0 - FEE_RATE)

        pos["side"] = "LONG"
        pos["entry_price"] = entry_price
        pos["qty"] = qty
        pos["last_trade_ts"] = now_sec
        pos["stop_loss"] = stop_loss
        pos["take_profit"] = take_profit
        save_pos(pos)
        return

    # --------- 5) ไม่มีสัญญาณ ----------
    log("[HOLD] No trading signal from EMA/ATR")
    return


# ------------------------------------------------------------
# [10] MAIN LOOP (EMA+ATR 1h BOT)
# ------------------------------------------------------------
def run_ema_atr_bot():
    log(
        f"[INIT] Starting EMA+ATR 1h bot on {SYMBOL}, "
        f"DRY_RUN={DRY_RUN}, RR={RR_TARGET:.1f}:1"
    )
    sync_server_time()

    while True:
        try:
            decide_and_trade_ema_atr()
        except Exception as e:
            log(f"[ERROR] Exception in main loop: {e}")
        time.sleep(REFRESH_SEC)


if __name__ == "__main__":
    run_ema_atr_bot()
