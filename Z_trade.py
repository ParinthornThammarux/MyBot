# ============================================================
#  Bitkub Mean Reversion Bot — THB_XRP (real-balance + robust I/O)
#  v3 trades + server-time logs + HTTP debug + retries + single DRY_RUN source
#  + normalized trades + stable VWAP + better debug
#  + position tracking (avg cost + PnL) + JSON persistence
#  + colored logs
#  + edge filter vs fee (compute_zscore_with_stats + edge_pct)
# ============================================================

import os, time, hmac, hashlib, json, requests, math, random
import datetime
from statistics import mean, pstdev
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv
from collections import deque

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


def color_for(msg: str) -> str:
    """
    เลือกสีตามประเภท log จาก prefix / keyword ในข้อความ
    ปรับ mapping ตรงนี้ได้ตามชอบ
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

    # PRICE / Z / HOLD
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
    if "NO TRADES" in msg or "WARMUP" in msg:
        return FG_WHITE + DIM

    # DEFAULT
    return FG_WHITE


# ------------------------------------------------------------
# [1] CONFIGURATION
# ------------------------------------------------------------
BASE_URL = "https://api.bitkub.com"
API_KEY  = os.getenv("BITKUB_API_KEY", "")
API_SECRET = (os.getenv("BITKUB_API_SECRET", "") or "").encode()

SYMBOL = "XRP_THB"

WINDOW = 30                # จำนวนจุดข้อมูลที่ใช้คำนวณ Z-score
REFRESH_SEC = 60           # วินาทีต่อการวนลูป 1 รอบ
TRADES_FETCH = max(200, WINDOW + 20)

THRESH_Z = 2.8
ORDER_NOTIONAL_THB = 100
SLIPPAGE_BPS = 6           # slippage (bps) สำหรับตั้ง bid/ask ให้ match ง่ายขึ้น

FEE_RATE = 0.0025          # 0.25% ต่อข้าง (ซื้อ 0.25% + ขาย 0.25%)
FEE_ROUNDTRIP = 2 * FEE_RATE   # ~0.5% ไป-กลับ
EDGE_BUFFER   = 0.003          # 0.3% เผื่อ slippage/noise (ปรับได้)

DRY_RUN = True             # True = ทดสอบ, False = ยิง order จริง

PRICE_ROUND = 2
QTY_ROUND = 6
MAX_SERIES_LEN = 5000

TIME_SYNC_INTERVAL = 300   # วินาทีในการ resync server time

COOLDOWN_SEC = 300         # วินาที cooldown หลังเทรด (เช่น 300 = 5 นาที)

POS_FILE = "Cost.json"     # ไฟล์เก็บสถานะ position

# Debug/Networking
DEBUG_SAMPLE_TRADE = True
DEBUG_HTTP = True
HTTP_TIMEOUT = 12
RETRY_MAX = 4
RETRY_BASE_DELAY = 0.6     # seconds

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
    """
    log พร้อมสี: timestamp เป็นสีจาง, ตัวข้อความใช้สีตามประเภท
    """
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
# [4] PUBLIC API — robust v3 market/trades (normalized)
# ------------------------------------------------------------
def get_trades(sym: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    ดึง trade จาก Bitkub แล้วแปลงให้อยู่รูปแบบเดียว:
    [{"ts": int, "rate": float, "amount": float}, ...] เรียงจากเก่า -> ใหม่

    รองรับ 2 รูปแบบหลัก ๆ:
      1) list/tuple: [ts, rate, amount, ...]
      2) dict:
         - {"ts","rat","amt", ...}   # รูปแบบ v3 ที่คาดว่าใช้จริง
         - หรือ {"ts","rate","amount"} เผื่อบางตลาดใช้ชื่อเต็ม
    """
    url = f"{BASE_URL}/api/v3/market/trades"
    params = {"sym": sym, "lmt": limit}

    for i in range(RETRY_MAX):
        try:
            r = http_get(url, params=params, timeout=10)
            data = r.json()

            # ปกติ v3: {"error":0,"result":[...]}
            if isinstance(data, dict):
                err = data.get("error")
                if err not in (0, None):
                    log(f"[TRADES ERROR] error_code={err}")
                    return []
                raw = data.get("result", [])
            elif isinstance(data, list):
                raw = data
            else:
                log(f"[TRADES WARN] unexpected payload type: {type(data)}")
                return []

            if not isinstance(raw, list):
                log(f"[TRADES WARN] trades result is not a list: {type(raw)}")
                return []

            trades: List[Dict[str, Any]] = []

            for x in raw:
                try:
                    # รูปแบบ list/tuple: [ts, rate, amount, ...]
                    if isinstance(x, (list, tuple)) and len(x) >= 3:
                        ts_raw, rate_raw, amt_raw = x[0], x[1], x[2]

                    # รูปแบบ dict
                    elif isinstance(x, dict):
                        # เคส v3 ปกติ: ts + rat + amt
                        if all(k in x for k in ("ts", "rat", "amt")):
                            ts_raw, rate_raw, amt_raw = x["ts"], x["rat"], x["amt"]

                        # เผื่อบางตลาดใช้ชื่อเต็ม: rate + amount
                        elif all(k in x for k in ("ts", "rate", "amount")):
                            ts_raw, rate_raw, amt_raw = x["ts"], x["rate"], x["amount"]

                        else:
                            # รูปแบบไม่รู้จัก ข้าม
                            continue
                    else:
                        # ไม่ใช่ list/dict ข้าม
                        continue

                    ts   = int(ts_raw)
                    rate = float(rate_raw)
                    amt  = float(amt_raw)

                    if rate <= 0 or amt <= 0:
                        continue

                    trades.append({
                        "ts": ts,
                        "rate": rate,
                        "amount": amt,
                    })

                except Exception:
                    # ข้าม trade ที่ parse ไม่ได้
                    continue

            if not trades:
                log(f"[TRADES WARN] no valid trades (len(raw)={len(raw)})")
                return []

            # ensure เรียงตามเวลา เก่า -> ใหม่
            trades.sort(key=lambda t: t["ts"])

            return trades

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
        "amt": float(int(thb_amount)),               # ถ้า Bitkub รองรับทศนิยม ค่อยเปลี่ยน logic ตรงนี้
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
    asset_key = asset.upper()
    try:
        res = market_balances()
        if res.get("result") and res["result"].get(asset_key):
            node = res["result"][asset_key]
            if isinstance(node, dict) and "available" in node:
                return float(node["available"])
    except Exception as e:
        log(f"[BAL ERR] balances {e}")
    try:
        res = market_wallet()
        if res.get("result") and asset_key in res["result"]:
            return float(res["result"][asset_key])
    except Exception as e:
        log(f"[BAL ERR] wallet {e}")
    return 0.0


# ------------------------------------------------------------
# [6] STRATEGY FUNCTIONS — VWAP + Z-score
# ------------------------------------------------------------
def vwap_tail(trades: List[Dict[str, Any]], tail: int = 20) -> Optional[float]:
    """
    คำนวณ VWAP จาก trade ช่วงท้ายสุดของ list (ที่ normalize แล้ว)
    trades ต้องอยู่ในรูป [{"ts","rate","amount"}, ...] เรียงเวลาเก่า->ใหม่
    """
    if not trades:
        return None

    t = trades[-min(tail, len(trades)):]
    total_notional = 0.0
    total_qty = 0.0

    for x in t:
        try:
            rate = float(x["rate"])
            amt  = float(x["amount"])
        except (KeyError, TypeError, ValueError):
            continue

        if rate <= 0 or amt <= 0:
            continue

        total_notional += rate * amt
        total_qty      += amt

    if total_qty > 0:
        return total_notional / total_qty

    # ถ้าไม่มี trade ที่ใช้ได้เลย ให้ fallback เป็นราคาของ trade ล่าสุดจริง ๆ
    last = t[-1]
    try:
        return float(last["rate"])
    except Exception:
        return None


def compute_zscore(series: List[float], window: int) -> Optional[float]:
    """ฟังก์ชันเดิม (ยังเก็บไว้เผื่อใช้ที่อื่น)"""
    if len(series) < window or window < 2:
        return None
    sample = list(series)[-window:]
    mu = mean(sample)
    sig = pstdev(sample) or 1e-9
    return (series[-1] - mu) / sig


def compute_zscore_with_stats(series: List[float], window: int):
    """
    คืนค่า (z, mean, std) สำหรับ window ล่าสุด
    ใช้ให้เราคำนวณทั้ง Z-score และ %edge จาก mean ได้ในทีเดียว
    """
    if len(series) < window or window < 2:
        return None, None, None
    sample = list(series)[-window:]
    mu = mean(sample)
    sig = pstdev(sample) or 1e-9
    z = (series[-1] - mu) / sig
    return z, mu, sig


# ------------------------------------------------------------
# [6.1] POSITION TRACKER + PERSISTENCE (avg cost + PnL)
# ------------------------------------------------------------
position_xrp = 0.0        # ปริมาณ XRP ที่ถืออยู่ตอนนี้
position_cost_thb = 0.0   # ต้นทุนรวม (THB) ของ XRP ที่ถืออยู่
realized_pnl_thb = 0.0    # กำไร/ขาดทุนที่ "ล็อก" แล้ว (THB)


def load_position():
    """โหลดสถานะ position จากไฟล์ JSON (ถ้ามี)"""
    global position_xrp, position_cost_thb, realized_pnl_thb
    if not os.path.exists(POS_FILE):
        print("[POS] position file not found. starting fresh.")
        return
    try:
        with open(POS_FILE, "r") as f:
            data = json.load(f)
        position_xrp      = float(data.get("position_xrp", 0.0))
        position_cost_thb = float(data.get("position_cost_thb", 0.0))
        realized_pnl_thb  = float(data.get("realized_pnl_thb", 0.0))
        print(f"[POS] loaded: qty={position_xrp} cost_sum={position_cost_thb} realized={realized_pnl_thb}")
    except Exception as e:
        print(f"[POS ERROR] failed to load position: {e}")


def save_position():
    """บันทึกสถานะ position ลงไฟล์ JSON"""
    data = {
        "position_xrp": position_xrp,
        "position_cost_thb": position_cost_thb,
        "realized_pnl_thb": realized_pnl_thb,
    }
    try:
        with open(POS_FILE, "w") as f:
            json.dump(data, f, indent=2)
        print("[POS] saved.")
    except Exception as e:
        print(f"[POS ERROR] failed to save: {e}")


def pos_avg_cost() -> float:
    """ต้นทุนเฉลี่ยต่อ 1 XRP (THB)"""
    if position_xrp <= 0:
        return 0.0
    return position_cost_thb / position_xrp


def on_fill_buy(qty: float, price: float, fee_rate: float = FEE_RATE):
    """
    อัพเดตต้นทุนเมื่อ 'ซื้อ' XRP
    qty   = ปริมาณ XRP ที่ได้ (ประมาณจากคำสั่ง)
    price = ราคาซื้อ (THB/XRP)
    """
    global position_xrp, position_cost_thb

    if qty <= 0 or price <= 0:
        return

    gross = qty * price           # มูลค่าที่ซื้อก่อน fee
    fee   = gross * fee_rate      # ค่าธรรมเนียมฝั่งซื้อ
    cost  = gross + fee           # ต้นทุนจริงรวม fee

    position_xrp      += qty
    position_cost_thb += cost

    save_position()


def on_fill_sell(qty: float, price: float, fee_rate: float = FEE_RATE):
    """
    อัพเดตต้นทุน + realized PnL เมื่อ 'ขาย' XRP บางส่วนหรือทั้งหมด
    ใช้ average cost method
    """
    global position_xrp, position_cost_thb, realized_pnl_thb

    if qty <= 0 or price <= 0 or position_xrp <= 0:
        return

    # สัดส่วนของ position ที่ถูกขาย
    portion = min(qty / position_xrp, 1.0)
    cost_part = position_cost_thb * portion  # ต้นทุนส่วนที่ถูกขาย

    gross   = qty * price
    fee     = gross * fee_rate
    proceed = gross - fee                     # เงินสุทธิหลังหักค่าธรรมเนียมขาย

    pnl = proceed - cost_part                 # กำไร/ขาดทุนของล็อตที่ขาย
    realized_pnl_thb += pnl

    # อัพเดตคงเหลือ
    position_xrp      -= qty
    position_cost_thb -= cost_part

    # ถ้าขายหมดให้รีเซ็ตต้นทุน
    if position_xrp <= 0:
        position_xrp = 0.0
        position_cost_thb = 0.0

    save_position()


def log_position(px: Optional[float] = None):
    """
    log ต้นทุนเฉลี่ย, unrealized PnL, realized PnL
    px คือราคาตลาดปัจจุบัน (เช่น vwap_tail)
    """
    if position_xrp <= 0:
        log(f"[POS] flat | realized={realized_pnl_thb:.2f} THB")
        return

    avg_cost = pos_avg_cost()
    if px is not None:
        unreal = (px - avg_cost) * position_xrp
    else:
        unreal = 0.0

    log(
        "[POS] qty={qty:.6f} avg_cost={avg:.4f} THB | "
        "cost_sum={cost:.2f} THB | unreal={unreal:.2f} THB | realized={realized:.2f} THB"
        .format(
            qty=position_xrp,
            avg=avg_cost,
            cost=position_cost_thb,
            unreal=unreal,
            realized=realized_pnl_thb,
        )
    )


# ------------------------------------------------------------
# [7] MAIN LOOP (with COOLDOWN + POSITION + EDGE FILTER)
# ------------------------------------------------------------
def run_loop():
    # โหลดสถานะ position จากไฟล์ (ถ้ามี)
    load_position()

    sync_server_time()
    price_series: deque = deque(maxlen=MAX_SERIES_LEN)

    last_trade_ts = 0.0   # เวลาเทรดล่าสุด (epoch seconds)
    debug_counter = 0

    log(f"Bitkub Mean Reversion Bot — {SYMBOL}")
    log(f"WINDOW={WINDOW} THRESH_Z={THRESH_Z} DRY_RUN={DRY_RUN}")
    log(f"COOLDOWN_SEC={COOLDOWN_SEC}")
    log(f"FEE_ROUNDTRIP={FEE_ROUNDTRIP*100:.3f}% EDGE_BUFFER={EDGE_BUFFER*100:.3f}%")

    while True:
        try:
            trades = get_trades(SYMBOL, limit=TRADES_FETCH)
            if not trades:
                log(f"[NO TRADES] sym={SYMBOL} lmt={TRADES_FETCH}. retry in {REFRESH_SEC}s")
                time.sleep(REFRESH_SEC)
                continue

            debug_counter += 1
            if DEBUG_SAMPLE_TRADE and trades and debug_counter % 5 == 0:
                # ทุก ๆ 5 รอบ แสดง trade ล่าสุดที่ normalize แล้ว
                log(f"[DEBUG] trade sample (norm last): {trades[-1]}")

            px = vwap_tail(trades, tail=20)
            if px is None:
                log("[WARMUP] no price yet, waiting...")
                time.sleep(REFRESH_SEC)
                continue

            price_series.append(px)

            # ใช้ zscore + mean + std พร้อมกัน
            z, mu, sig = compute_zscore_with_stats(list(price_series), WINDOW)
            if z is None or mu is None:
                log(f"[WARMUP] collecting data... px={px:.4f} len={len(price_series)}/{WINDOW}")
                time.sleep(REFRESH_SEC)
                continue

            # --- EDGE FILTER: เช็คว่าเบี่ยงจาก mean กี่ % ---
            edge_pct = abs(px - mu) / mu
            min_edge = FEE_ROUNDTRIP + EDGE_BUFFER  # ต้องใหญ่กว่าค่า fee ทั้งรอบ + buffer

            if edge_pct < min_edge:
                log(f"[SKIP EDGE] px={px:.4f} mu={mu:.4f} edge={edge_pct*100:.2f}% < {min_edge*100:.2f}% | z={z:.2f}")
                time.sleep(REFRESH_SEC)
                continue
            # ------------------------------------------------

            bid_px = round(px * (1 - SLIPPAGE_BPS / 10000), PRICE_ROUND)
            ask_px = round(px * (1 + SLIPPAGE_BPS / 10000), PRICE_ROUND)

            # แสดงราคาที่ใช้ กับเทรดล่าสุดเพื่อเช็คความแม่น
            last_trade = trades[-1]
            try:
                log(f"[PRICE] vwap_tail={px:.4f} mu={mu:.4f} | last_trade_rate={last_trade['rate']:.4f} amt={last_trade['amount']} | z={z:.2f}")
            except Exception:
                log(f"[PRICE] vwap_tail={px:.4f} mu={mu:.4f} | last_trade={last_trade} | z={z:.2f}")

            # --------- COOLDOWN CHECK ----------
            now_ts = time.time()
            in_cooldown = (now_ts - last_trade_ts) < COOLDOWN_SEC if last_trade_ts > 0 else False
            cooldown_left = COOLDOWN_SEC - (now_ts - last_trade_ts) if in_cooldown else 0
            # -----------------------------------

            if z <= -THRESH_Z:
                if in_cooldown:
                    log(f"[COOLDOWN] skip BUY, remaining={cooldown_left:.1f}s | px={px:.4f} z={z:.2f}")
                else:
                    thb_avail = get_available("THB")
                    if thb_avail < ORDER_NOTIONAL_THB:
                        log(f"[SKIP BUY] THB={thb_avail:.2f} < {ORDER_NOTIONAL_THB} | px={px:.4f} z={z:.2f}")
                    else:
                        qty_est = ORDER_NOTIONAL_THB / bid_px
                        resp = place_bid(SYMBOL, ORDER_NOTIONAL_THB, bid_px, dry_run=DRY_RUN)

                        # อัพเดต position จริงเฉพาะตอน DRY_RUN = False
                        if not DRY_RUN:
                            on_fill_buy(qty_est, bid_px)

                        log(f"[BUY ] z={z:.2f} px={px:.4f} bid≈{bid_px} THB≈{ORDER_NOTIONAL_THB} (~{qty_est:.6f} XRP) -> {resp}")
                        log_position(px)

                        last_trade_ts = now_ts  # ตั้งคูลดาวน์หลังจากยิงออเดอร์

            elif z >= THRESH_Z:
                if in_cooldown:
                    log(f"[COOLDOWN] skip SELL, remaining={cooldown_left:.1f}s | px={px:.4f} z={z:.2f}")
                else:
                    xrp_avail = get_available("XRP")
                    if xrp_avail <= 0:
                        log(f"[SKIP SELL] XRP={xrp_avail:.6f} | px={px:.4f} z={z:.2f}")
                    else:
                        sell_qty = round(xrp_avail * 0.5, QTY_ROUND)
                        if sell_qty > 0:
                            resp = place_ask(SYMBOL, sell_qty, ask_px, dry_run=DRY_RUN)

                            # อัพเดต position จริงเฉพาะตอน DRY_RUN = False
                            if not DRY_RUN:
                                on_fill_sell(sell_qty, ask_px)

                            log(f"[SELL] z={z:.2f} px={px:.4f} ask≈{ask_px} qty≈{sell_qty:.6f} -> {resp}")
                            log_position(px)

                            last_trade_ts = now_ts  # ตั้งคูลดาวน์หลังจากยิงออเดอร์
                        else:
                            log("[SKIP SELL] qty too small after rounding")
            else:
                log(f"[HOLD] px={px:.4f} z={z:.2f} bid≈{bid_px} ask≈{ask_px} edge={edge_pct*100:.2f}%")
                # ถ้าอยากเห็นสถานะบ่อยขึ้น เปิดบรรทัดนี้ได้
                # log_position(px)

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
