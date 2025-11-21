# ============================================================
# Bitkub Grid Bot — USDT_THB (real-balance + robust I/O)
#
# - Grid strategy (no Z-score)
# - v3 trades + server-time logs + HTTP debug + retries
# - normalized trades + stable VWAP (numpy)
# - position tracking (avg cost + PnL) + JSON persistence
# - colored logs
# - generic BASE/QUOTE (รองรับทุกคู่ที่เป็น BASE_QUOTE)
# - Grid ตามหลัก: เลเวลลด = BUY, เลเวลเพิ่ม = SELL (ทีละกริด)
# - เวอร์ชันนี้ตั้งราคาออเดอร์ตาม "เส้นกริด" จริง ๆ
# ============================================================

import os
import time
import hmac
import hashlib
import json
import requests
import random
import datetime
import math

from dotenv import load_dotenv
from typing import Dict, Any, List, Optional

import numpy as np

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
    """เลือกสีตามประเภท log จาก prefix / keyword ในข้อความ"""
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
    if "NO TRADES" in msg or "WARMUP" in msg:
        return FG_WHITE + DIM

    # DEFAULT
    return FG_WHITE


# ------------------------------------------------------------
# [1] CONFIGURATION
# ------------------------------------------------------------

BASE_URL = "https://api.bitkub.com"
API_KEY = os.getenv("BITKUB_API_KEY", "")
API_SECRET = (os.getenv("BITKUB_API_SECRET", "") or "").encode()

SYMBOL = "USDT_THB"        # คู่ที่ใช้เทรด

REFRESH_SEC = 60           # วินาทีต่อการวนลูป 1 รอบ
TRADES_FETCH = 200         # จำนวน trade ที่ดึงมาใช้คำนวณ VWAP

ORDER_NOTIONAL_THB = 100   # มูลค่า THB ต่อไม้ (ขนาดต่อกริด)
SLIPPAGE_BPS = 0           # สำหรับเวอร์ชันนี้ ถ้าจะใช้ให้ไป offset จากราคาเส้นกริดเอง
FEE_RATE = 0.0025          # 0.25% ต่อข้าง (ซื้อ 0.25% + ขาย 0.25%)

DRY_RUN = True             # True = ทดสอบ, False = ยิง order จริง

PRICE_ROUND = 2            # ทศนิยมราคาหน่วย THB
QTY_ROUND = 6              # ทศนิยมจำนวนเหรียญ

TIME_SYNC_INTERVAL = 300   # (ไม่ได้ใช้ offset แล้ว แต่เก็บไว้เผื่อ)
COOLDOWN_SEC = 90          # วินาที cooldown หลังเทรด

POS_FILE = "Cost_USDT.json"  # ไฟล์เก็บสถานะ position สำหรับ USDT

# ==== GRID STRATEGY CONFIG ====
GRID_CENTER_PRICE = 32     # จุดกึ่งกลางกริด (THB ต่อ 1 USDT)
GRID_STEP_PCT = 0.7        # ระยะห่างแต่ละชั้นกริดเป็น % เช่น 1% ต่อขั้น
GRID_LEVELS_DOWN = 10      # จำนวนขั้นกริดด้านล่าง center (เลเวลติดลบสุด)
GRID_LEVELS_UP = 10        # จำนวนขั้นกริดด้านบน center (เลเวลบวกสุด)

# Debug/Networking
DEBUG_SAMPLE_TRADE = True
DEBUG_HTTP = True
HTTP_TIMEOUT = 12

RETRY_MAX = 4
RETRY_BASE_DELAY = 0.6     # seconds

COMMON_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}

session = requests.Session()

# แยก base / quote จาก SYMBOL เช่น USDT_THB -> base=USDT, quote=THB
BASE_ASSET, QUOTE_ASSET = SYMBOL.split("_", 1)


# ------------------------------------------------------------
# [2] HTTP HELPERS
# ------------------------------------------------------------

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
                body_dbg = data if isinstance(data, str) and len(data) < 300 else str(data)[:300] + "...(+)"
                print(f"[HTTP POST] {r.request.method} {r.url} -> {r.status_code} body={body_dbg}")
                # log response text เผื่อดีบั๊ก error code จาก Bitkub
                try:
                    print(f"[HTTP POST RESP] {r.text}")
                except Exception:
                    pass
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            if DEBUG_HTTP:
                print(f"[HTTP POST ERROR#{i+1}] {url} err={e}")
            _backoff_sleep(i)
    raise last_exc


# ------------------------------------------------------------
# [3] TIME + LOGGING (ไม่ใช้ offset แล้ว)
# ------------------------------------------------------------

def now_server_ms() -> int:
    # ใช้ local time แบบตรง ๆ เป็น millisecond (เทียบเท่า JS Date.now())
    return int(time.time() * 1000)


def now_server_dt() -> datetime.datetime:
    return datetime.datetime.fromtimestamp(time.time())


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


def ts_ms_str() -> str:
    """
    Timestamp สำหรับ Bitkub v3 (ต้องการ ms เช่นเดียวกับ JS Date.now().toString())
    """
    return str(now_server_ms())


# ------------------------------------------------------------
# [4] AUTH UTILITIES
# ------------------------------------------------------------

def sign(timestamp_ms: str, method: str, request_path: str, body: str = "") -> str:
    """
    v3 sign = HMAC_SHA256( timestamp + method + requestPath + body )
    - method: ใช้ตัวใหญ่ 'POST' / 'GET'
    - request_path: เช่น '/api/market/wallet' หรือ '/api/v3/market/place-bid'
    - body: string ที่ส่งจริง ("" หรือ JSON)
    """
    payload = (timestamp_ms + method.upper() + request_path + (body or "")).encode()
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
# [5] PUBLIC API — TRADES (normalized)
# ------------------------------------------------------------

def get_trades(sym: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    ดึง trade จาก Bitkub แล้วแปลงให้อยู่รูปแบบเดียว:
    [{"ts": int, "rate": float, "amount": float}, ...] เรียงจากเก่า -> ใหม่
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
                        if all(k in x for k in ("ts", "rat", "amt")):
                            ts_raw, rate_raw, amt_raw = x["ts"], x["rat"], x["amt"]
                        elif all(k in x for k in ("ts", "rate", "amount")):
                            ts_raw, rate_raw, amt_raw = x["ts"], x["rate"], x["amount"]
                        else:
                            continue
                    else:
                        continue

                    ts = int(ts_raw)
                    rate = float(rate_raw)
                    amt = float(amt_raw)

                    if rate <= 0 or amt <= 0:
                        continue

                    trades.append({
                        "ts": ts,
                        "rate": rate,
                        "amount": amt,
                    })

                except Exception:
                    continue

            if not trades:
                log(f"[TRADES WARN] no valid trades (len(raw)={len(raw)})")
                return []

            trades.sort(key=lambda t: t["ts"])
            return trades

        except Exception as e:
            log(f"[TRADES EXC#{i+1}] {e}")
            _backoff_sleep(i)

    return []


# ------------------------------------------------------------
# [6] STRATEGY FUNCTIONS — VWAP (with numpy)
# ------------------------------------------------------------

def vwap_tail(trades: List[Dict[str, Any]], tail: int = 20) -> Optional[float]:
    """
    คำนวณ VWAP จาก trade ช่วงท้ายสุดของ list
    ใช้ numpy ช่วยคำนวณให้เสถียรและเร็วขึ้น
    trades: [{"ts","rate","amount"}, ...] เรียงเก่า -> ใหม่
    """
    if not trades:
        return None

    t = trades[-min(tail, len(trades)):]  # tail ช่วงท้าย

    try:
        rates = np.array([float(x["rate"]) for x in t], dtype=float)
        amts = np.array([float(x["amount"]) for x in t], dtype=float)
    except Exception:
        # fallback เป็น loop ธรรมดา
        total_notional = 0.0
        total_qty = 0.0
        for x in t:
            try:
                rate = float(x["rate"])
                amt = float(x["amount"])
            except (KeyError, TypeError, ValueError):
                continue
            if rate <= 0 or amt <= 0:
                continue
            total_notional += rate * amt
            total_qty += amt
        if total_qty > 0:
            return total_notional / total_qty
        last = t[-1]
        try:
            return float(last["rate"])
        except Exception:
            return None

    mask = (rates > 0) & (amts > 0)
    rates = rates[mask]
    amts = amts[mask]

    if amts.size == 0:
        # fallback เป็น trade สุดท้าย
        last = t[-1]
        try:
            return float(last["rate"])
        except Exception:
            return None

    vwap = np.sum(rates * amts) / np.sum(amts)
    return float(vwap)


# ------------------------------------------------------------
# [6.1] POSITION TRACKER + PERSISTENCE
# ------------------------------------------------------------

position_qty = 0.0         # ปริมาณ BASE_ASSET ที่ถืออยู่ตอนนี้ (เช่น USDT)
position_cost_thb = 0.0    # ต้นทุนรวม (THB)
realized_pnl_thb = 0.0     # กำไร/ขาดทุนที่ล็อกแล้ว (THB)

# จำนวนกริด BUY ที่ยังไม่ถูกปิดด้วย SELL
open_grid_buys = 0


def load_position():
    """โหลดสถานะ position จากไฟล์ JSON (ถ้ามี)"""
    global position_qty, position_cost_thb, realized_pnl_thb, open_grid_buys
    if not os.path.exists(POS_FILE):
        print("[POS] position file not found. starting fresh.")
        return
    try:
        with open(POS_FILE, "r") as f:
            data = json.load(f)
        position_qty = float(data.get("position_qty", 0.0))
        position_cost_thb = float(data.get("position_cost_thb", 0.0))
        realized_pnl_thb = float(data.get("realized_pnl_thb", 0.0))
        open_grid_buys = int(data.get("open_grid_buys", 0))
        print(
            f"[POS] loaded: qty={position_qty} cost_sum={position_cost_thb} "
            f"realized={realized_pnl_thb} open_grid_buys={open_grid_buys}"
        )
    except Exception as e:
        print(f"[POS ERROR] failed to load position: {e}")


def save_position():
    """บันทึกสถานะ position ลงไฟล์ JSON"""
    data = {
        "position_qty": position_qty,
        "position_cost_thb": position_cost_thb,
        "realized_pnl_thb": realized_pnl_thb,
        "open_grid_buys": open_grid_buys,
    }
    try:
        with open(POS_FILE, "w") as f:
            json.dump(data, f, indent=2)
        print("[POS] saved.")
    except Exception as e:
        print(f"[POS ERROR] failed to save: {e}")


def pos_avg_cost() -> float:
    """ต้นทุนเฉลี่ยต่อ 1 หน่วย BASE_ASSET"""
    if position_qty <= 0:
        return 0.0
    return position_cost_thb / position_qty


def on_fill_buy(qty: float, price: float, fee_rate: float = FEE_RATE):
    """อัพเดตต้นทุนเมื่อ 'ซื้อ' BASE_ASSET"""
    global position_qty, position_cost_thb

    if qty <= 0 or price <= 0:
        return

    gross = qty * price
    fee = gross * fee_rate
    cost = gross + fee

    position_qty += qty
    position_cost_thb += cost

    save_position()


def on_fill_sell(qty: float, price: float, fee_rate: float = FEE_RATE):
    """อัพเดตต้นทุน + realized PnL เมื่อ 'ขาย' BASE_ASSET"""
    global position_qty, position_cost_thb, realized_pnl_thb

    if qty <= 0 or price <= 0 or position_qty <= 0:
        return

    portion = min(qty / position_qty, 1.0)
    cost_part = position_cost_thb * portion

    gross = qty * price
    fee = gross * fee_rate
    proceed = gross - fee

    pnl = proceed - cost_part
    realized_pnl_thb += pnl

    position_qty -= qty
    position_cost_thb -= cost_part

    if position_qty <= 0:
        position_qty = 0.0
        position_cost_thb = 0.0

    save_position()


def log_position(px: Optional[float] = None):
    """log ต้นทุนเฉลี่ย, unrealized PnL, realized PnL"""
    global open_grid_buys

    if position_qty <= 0:
        log(f"[POS] flat | realized={realized_pnl_thb:.2f} THB | open_grid_buys={open_grid_buys}")
        return

    avg_cost = pos_avg_cost()
    unreal = (px - avg_cost) * position_qty if px is not None else 0.0

    log(
        "[POS] qty={qty:.6f} {asset} avg_cost={avg:.4f} THB | "
        "cost_sum={cost:.2f} THB | unreal={unreal:.2f} THB | "
        "realized={realized:.2f} THB | open_grid_buys={ogb}"
        .format(
            qty=position_qty,
            asset=BASE_ASSET,
            avg=avg_cost,
            cost=position_cost_thb,
            unreal=unreal,
            realized=realized_pnl_thb,
            ogb=open_grid_buys,
        )
    )


# ------------------------------------------------------------
# [6.2] GRID HELPERS
# ------------------------------------------------------------

last_grid_level = None  # state ของกริดในรอบก่อนหน้า


def grid_step_thb() -> float:
    """จำนวน THB ต่อ 1 ขั้นกริด จาก % ที่กำหนด (เทียบจาก center)"""
    return GRID_CENTER_PRICE * GRID_STEP_PCT / 100.0


def grid_price(level: int) -> float:
    """
    คืนราคา "เส้นกริด" สำหรับ level ที่กำหนด
    level = 0  : เส้นที่ center
    level = 1  : เส้นถัดไปด้านบน
    level = -1 : เส้นถัดไปด้านล่าง
    """
    return GRID_CENTER_PRICE + grid_step_thb() * level


def grid_level_from_price(price: float) -> float:
    """
    แปลงราคา -> index แบบ float ว่าอยู่ห่างจาก center กี่ step
    ยังไม่ floor/round (ใช้ raw index)
    """
    step = grid_step_thb()
    if step <= 0:
        return 0.0
    return (price - GRID_CENTER_PRICE) / step


# ------------------------------------------------------------
# [5.1] ACCOUNT — Wallet / Balances (PATH + BODY ตาม Docs)
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
    asset_key = asset.upper()
    # พยายามใช้ balances ก่อน (มี reserved ด้วย)
    try:
        res = market_balances()
        if res.get("result") and res["result"].get(asset_key):
            node = res["result"][asset_key]
            if isinstance(node, dict) and "available" in node:
                return float(node["available"])
    except Exception as e:
        log(f"[BAL ERR] balances {e}")

    # fallback เป็น wallet (available only)
    try:
        res = market_wallet()
        if res.get("result") and asset_key in res["result"]:
            return float(res["result"][asset_key])
    except Exception as e:
        log(f"[BAL ERR] wallet {e}")

    return 0.0


# ------------------------------------------------------------
# [5] PRIVATE TRADE API
# ------------------------------------------------------------

def place_bid(sym: str, thb_amount: float, rate: float, dry_run: bool) -> Dict[str, Any]:
    method, path = "POST", "/api/v3/market/place-bid"
    ts = ts_ms_str()

    payload = {
        "sym": sym,
        "amt": float(int(thb_amount)),           # quote ต้องเป็นจำนวนเต็ม
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
# [7] MAIN LOOP (GRID STRATEGY)
# ------------------------------------------------------------

def run_loop():
    global last_grid_level, open_grid_buys

    load_position()

    last_trade_ts = 0.0   # เวลาเทรดล่าสุด
    debug_counter = 0

    step_thb = grid_step_thb()
    log(f"Bitkub GRID Bot — {SYMBOL}")
    log(f"GRID_CENTER={GRID_CENTER_PRICE} THB | STEP={GRID_STEP_PCT}% (~{step_thb:.4f} THB/step)")
    log(f"LEVELS_DOWN={GRID_LEVELS_DOWN} LEVELS_UP={GRID_LEVELS_UP}")
    log(f"ORDER_NOTIONAL_THB={ORDER_NOTIONAL_THB} DRY_RUN={DRY_RUN}")
    log(f"COOLDOWN_SEC={COOLDOWN_SEC}")
    log(f"Est. profit per grid (before slippage) ≈ {GRID_STEP_PCT - 2*FEE_RATE*100:.3f}%")

    while True:
        try:
            trades = get_trades(SYMBOL, limit=TRADES_FETCH)
            if not trades:
                log(f"[NO TRADES] sym={SYMBOL} lmt={TRADES_FETCH}. retry in {REFRESH_SEC}s")
                time.sleep(REFRESH_SEC)
                continue

            debug_counter += 1
            if DEBUG_SAMPLE_TRADE and trades and debug_counter % 5 == 0:
                log(f"[DEBUG] trade sample (norm last): {trades[-1]}")

            px = vwap_tail(trades, tail=20)
            if px is None:
                log("[WARMUP] no price yet, waiting...")
                time.sleep(REFRESH_SEC)
                continue

            last_trade = trades[-1]
            try:
                log(f"[PRICE] px={px:.4f} | last_rate={last_trade['rate']:.4f} amt={last_trade['amount']}")
            except Exception:
                log(f"[PRICE] px={px:.4f} | last_trade={last_trade}")

            # ========= GRID LOGIC =========
            raw_idx = grid_level_from_price(px)   # index float
            lvl = math.floor(raw_idx)             # cell ล่างของราคาปัจจุบัน

            # กำหนดเส้นกริดล่าง/บนสำหรับ cell นี้
            buy_level = lvl
            sell_level = lvl + 1

            # ถ้าเลยกรอบกริดไปแล้ว -> ไม่เทรด
            if buy_level < -GRID_LEVELS_DOWN or sell_level > GRID_LEVELS_UP:
                log(
                    f"[HOLD] out-of-grid-range lvl={lvl} (buy_level={buy_level}, sell_level={sell_level}) "
                    f"px={px:.4f} (center={GRID_CENTER_PRICE}, step≈{step_thb:.4f})"
                )
                last_grid_level = lvl
                time.sleep(REFRESH_SEC)
                continue

            buy_px = round(grid_price(buy_level), PRICE_ROUND)   # ราคาเส้นล่าง
            sell_px = round(grid_price(sell_level), PRICE_ROUND) # ราคาเส้นบน

            # (ถ้าอยากให้มี slippage offset จากเส้นกริด เช่น buy ที่ต่ำกว่าเส้นเล็กน้อย/ขายที่สูงกว่าเล็กน้อย
            #  สามารถปรับที่นี่ได้เอง เช่น:
            #  if SLIPPAGE_BPS > 0:
            #      buy_px = round(buy_px * (1 - SLIPPAGE_BPS/10000), PRICE_ROUND)
            #      sell_px = round(sell_px * (1 + SLIPPAGE_BPS/10000), PRICE_ROUND)
            # )

            now_ts = time.time()
            in_cooldown = (now_ts - last_trade_ts) < COOLDOWN_SEC if last_trade_ts > 0 else False
            cooldown_left = COOLDOWN_SEC - (now_ts - last_trade_ts) if in_cooldown else 0

            # รอบแรก: ตั้งค่า last_grid_level แล้วรอดูการเคลื่อนที่ก่อน
            if last_grid_level is None:
                last_grid_level = lvl
                log(
                    f"[WARMUP] init grid level = {lvl} at px={px:.4f} "
                    f"(buy_level={buy_level} @{buy_px}, sell_level={sell_level} @{sell_px})"
                )
                time.sleep(REFRESH_SEC)
                continue

            moved_down = lvl < last_grid_level   # เลเวลลด -> BUY
            moved_up = lvl > last_grid_level     # เลเวลเพิ่ม -> SELL

            # ===== BUY SIDE: เลเวลลด -> BUY 1 กริด =====
            if moved_down:
                if in_cooldown:
                    log(
                        f"[COOLDOWN] skip BUY lvl={lvl} (prev={last_grid_level}) "
                        f"remaining={cooldown_left:.1f}s px={px:.4f} buy_px={buy_px}"
                    )
                else:
                    quote_avail = get_available(QUOTE_ASSET)
                    if quote_avail < ORDER_NOTIONAL_THB:
                        log(
                            f"[SKIP BUY] {QUOTE_ASSET}={quote_avail:.2f} < {ORDER_NOTIONAL_THB} | "
                            f"px={px:.4f} lvl={lvl} buy_lvl={buy_level} buy_px={buy_px}"
                        )
                    else:
                        qty_est = ORDER_NOTIONAL_THB / buy_px
                        resp = place_bid(SYMBOL, ORDER_NOTIONAL_THB, buy_px, dry_run=DRY_RUN)

                        if not DRY_RUN:
                            on_fill_buy(qty_est, buy_px)
                            open_grid_buys += 1
                            save_position()
                        else:
                            open_grid_buys += 1

                        log(
                            f"[BUY ] lvl={lvl} (prev={last_grid_level}) -> px={px:.4f} "
                            f"buy_lvl={buy_level} buy_px={buy_px} {QUOTE_ASSET}≈{ORDER_NOTIONAL_THB} "
                            f"(~{qty_est:.6f} {BASE_ASSET}) -> {resp} | open_grid_buys={open_grid_buys}"
                        )
                        log_position(px)
                        last_trade_ts = now_ts

            # ===== SELL SIDE: เลเวลเพิ่ม -> SELL 1 กริด =====
            elif moved_up:
                if in_cooldown:
                    log(
                        f"[COOLDOWN] skip SELL lvl={lvl} (prev={last_grid_level}) "
                        f"remaining={cooldown_left:.1f}s px={px:.4f} sell_px={sell_px}"
                    )
                else:
                    base_avail = get_available(BASE_ASSET)

                    if open_grid_buys <= 0:
                        log(
                            f"[SKIP SELL] no open grid positions | {BASE_ASSET}={base_avail:.6f} "
                            f"px={px:.4f} lvl={lvl} sell_lvl={sell_level}"
                        )
                    elif base_avail <= 0:
                        log(f"[SKIP SELL] {BASE_ASSET}={base_avail:.6f} | px={px:.4f} lvl={lvl}")
                    else:
                        target_qty = ORDER_NOTIONAL_THB / sell_px
                        target_qty = round(target_qty, QTY_ROUND)

                        sell_qty = min(target_qty, round(base_avail, QTY_ROUND))

                        if sell_qty > 0:
                            resp = place_ask(SYMBOL, sell_qty, sell_px, dry_run=DRY_RUN)

                            if not DRY_RUN:
                                on_fill_sell(sell_qty, sell_px)
                                open_grid_buys -= 1
                                if open_grid_buys < 0:
                                    open_grid_buys = 0
                                save_position()
                            else:
                                open_grid_buys -= 1
                                if open_grid_buys < 0:
                                    open_grid_buys = 0

                            log(
                                f"[SELL] lvl={lvl} (prev={last_grid_level}) -> px={px:.4f} "
                                f"sell_lvl={sell_level} sell_px={sell_px} "
                                f"qty≈{sell_qty:.6f} {BASE_ASSET} -> {resp} | open_grid_buys={open_grid_buys}"
                            )
                            log_position(px)
                            last_trade_ts = now_ts
                        else:
                            log("[SKIP SELL] qty too small after rounding")

            else:
                # ยังอยู่ใน level เดิม
                log(
                    f"[HOLD] px={px:.4f} lvl={lvl} prev_lvl={last_grid_level} "
                    f"buy_lvl={buy_level}@{buy_px} sell_lvl={sell_level}@{sell_px} "
                    f"| open_grid_buys={open_grid_buys}"
                )

            # อัปเดต level ล่าสุดทุกครั้ง
            last_grid_level = lvl

        except requests.HTTPError as e:
            # แสดง body เผื่อเห็น error code ของ Bitkub เช่น {"error":6}
            try:
                body = e.response.text
            except Exception:
                body = str(e)
            log(f"[HTTP ERROR] {body}")
        except Exception as e:
            log(f"[ERROR] {e}")

        time.sleep(REFRESH_SEC)


# ------------------------------------------------------------
# [8] ENTRY POINT
# ------------------------------------------------------------
if __name__ == "__main__":
    run_loop()
