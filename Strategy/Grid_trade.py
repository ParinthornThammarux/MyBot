# ============================================================
# Bitkub Grid Bot — GENERIC BASE_QUOTE (real-balance + robust I/O)
#
# - Classical Grid:
#   * ราคาเคลื่อน "ลง" ผ่านเส้น -> BUY 1 grid ต่อ 1 เส้น
#   * ราคาเคลื่อน "ขึ้น" ผ่านเส้น -> SELL 1 grid ต่อ 1 เส้น (ปิดไม้ของ level ด้านล่าง)
# - Per-level grid slots (รู้ว่า level ไหนถืออยู่เท่าไร)
# - Hysteresis: ต้องเคลื่อนห่างจาก last_trade_price >= MIN_MOVE_PCT_FROM_LAST_TRADE
#   เพื่อลดการโดนค่าฟีแถวขอบกริด
# - v3 trades + server-time logs + HTTP debug + retries
# - normalized trades + VWAP (numpy)
# - position tracking (avg cost + PnL) + JSON persistence (รวม grid_slots)
# - colored logs
# - generic BASE/QUOTE (รองรับทุกคู่ที่เป็น BASE_QUOTE)
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

    # BUY / SELL / COOLDOWN / HYST
    if msg.startswith("[BUY "):
        return FG_GREEN + BOLD
    if msg.startswith("[SELL]"):
        return FG_YELLOW + BOLD
    if msg.startswith("[COOLDOWN]") or msg.startswith("[HYST]"):
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

SYMBOL = "USDT_THB"        # คู่ที่ใช้เทรด เช่น USDT_THB / BTC_THB ฯลฯ

REFRESH_SEC = 60           # วินาทีต่อการวนลูป 1 รอบ
TRADES_FETCH = 200         # จำนวน trade ที่ดึงมาใช้คำนวณ VWAP

ORDER_NOTIONAL_THB = 100   # มูลค่า THB ต่อ 1 grid (ขนาดต่อกริด)
FEE_RATE = 0.0025          # 0.25% ต่อข้าง (ซื้อ 0.25% + ขาย 0.25%)

DRY_RUN = True             # True = ทดสอบ, False = ยิง order จริง

PRICE_ROUND = 2            # ทศนิยมราคาหน่วย THB
QTY_ROUND = 6              # ทศนิยมจำนวนเหรียญ

COOLDOWN_SEC = 90          # วินาที cooldown หลังเทรด (กันเทรดถี่เกิน)

POS_FILE = "Cost_USDT.json"  # ไฟล์เก็บสถานะ position + grid_slots

# ==== GRID STRATEGY CONFIG ====
GRID_CENTER_PRICE = 32     # จุดกึ่งกลางกริด (THB ต่อ 1 BASE_ASSET)
GRID_STEP_PCT = 0.7        # ระยะห่างแต่ละชั้นกริดเป็น % เช่น 1% ต่อขั้น
GRID_LEVELS_DOWN = 10      # จำนวนขั้นกริดด้านล่าง center (เลเวลติดลบสุด)
GRID_LEVELS_UP = 10        # จำนวนขั้นกริดด้านบน center (เลเวลบวกสุด)

# Hysteresis: ต้องขยับห่างจาก last_trade_price อย่างน้อยเท่า grid step
# เพื่อลดการโดนค่าฟีจากการแกว่งแถวขอบกริด
MIN_MOVE_PCT_FROM_LAST_TRADE = GRID_STEP_PCT

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
# [3] TIME + LOGGING
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
                    if isinstance(x, (list, tuple)) and len(x) >= 3:
                        ts_raw, rate_raw, amt_raw = x[0], x[1], x[2]
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
# [6] STRATEGY FUNCTIONS — VWAP
# ------------------------------------------------------------

def vwap_tail(trades: List[Dict[str, Any]], tail: int = 20) -> Optional[float]:
    """
    คำนวณ VWAP จาก trade ช่วงท้ายสุดของ list
    trades: [{"ts","rate","amount"}, ...] เรียงเก่า -> ใหม่
    """
    if not trades:
        return None

    t = trades[-min(tail, len(trades)):]  # tail ช่วงท้าย

    try:
        rates = np.array([float(x["rate"]) for x in t], dtype=float)
        amts = np.array([float(x["amount"]) for x in t], dtype=float)
    except Exception:
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
        last = t[-1]
        try:
            return float(last["rate"])
        except Exception:
            return None

    vwap = np.sum(rates * amts) / np.sum(amts)
    return float(vwap)


# ------------------------------------------------------------
# [6.1] POSITION TRACKER + PERSISTENCE (+ GRID SLOTS)
# ------------------------------------------------------------

position_qty = 0.0         # ปริมาณ BASE_ASSET ทั้งหมดที่ถืออยู่
position_cost_thb = 0.0    # ต้นทุนรวม (THB)
realized_pnl_thb = 0.0     # กำไร/ขาดทุนที่ล็อกแล้ว (THB)

# per-level grid slots: level -> {"qty": float}
grid_slots: Dict[int, Dict[str, float]] = {}

# จำนวนกริด BUY (slots) ที่ยังไม่ถูกปิดด้วย SELL (level ที่ qty > 0)
open_grid_buys = 0


def load_position():
    """โหลดสถานะ position + grid slots จากไฟล์ JSON (ถ้ามี)"""
    global position_qty, position_cost_thb, realized_pnl_thb, open_grid_buys, grid_slots
    if not os.path.exists(POS_FILE):
        print("[POS] position file not found. starting fresh.")
        return
    try:
        with open(POS_FILE, "r") as f:
            data = json.load(f)
        position_qty = float(data.get("position_qty", 0.0))
        position_cost_thb = float(data.get("position_cost_thb", 0.0))
        realized_pnl_thb = float(data.get("realized_pnl_thb", 0.0))

        raw_slots = data.get("grid_slots", {})
        grid_slots = {}
        if isinstance(raw_slots, dict):
            for k, v in raw_slots.items():
                try:
                    lvl = int(k)
                except Exception:
                    continue
                if isinstance(v, dict):
                    qty = float(v.get("qty", 0.0))
                else:
                    qty = float(v)
                if qty > 0:
                    grid_slots[lvl] = {"qty": qty}

        open_grid_buys = sum(1 for s in grid_slots.values() if s.get("qty", 0.0) > 0)

        print(
            f"[POS] loaded: qty={position_qty} cost_sum={position_cost_thb} "
            f"realized={realized_pnl_thb} open_slots={open_grid_buys}"
        )
    except Exception as e:
        print(f"[POS ERROR] failed to load position: {e}")


def save_position():
    """บันทึกสถานะ position + grid slots ลงไฟล์ JSON"""
    global grid_slots, open_grid_buys
    grid_slots_for_json = {
        str(lvl): {"qty": float(slot.get("qty", 0.0))}
        for lvl, slot in grid_slots.items()
        if slot.get("qty", 0.0) > 0
    }

    open_grid_buys = sum(1 for s in grid_slots.values() if s.get("qty", 0.0) > 0)

    data = {
        "position_qty": position_qty,
        "position_cost_thb": position_cost_thb,
        "realized_pnl_thb": realized_pnl_thb,
        "open_grid_buys": open_grid_buys,
        "grid_slots": grid_slots_for_json,
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


def grid_open_buy(level: int, qty: float):
    """บันทึกว่า level นี้มีการ BUY grid เพิ่ม qty"""
    global grid_slots, open_grid_buys
    if qty <= 0:
        return
    prev_open = False
    slot = grid_slots.get(level)
    if slot is None:
        slot = {"qty": 0.0}
        grid_slots[level] = slot
    else:
        prev_open = slot.get("qty", 0.0) > 0

    slot["qty"] = slot.get("qty", 0.0) + qty
    if not prev_open and slot["qty"] > 0:
        open_grid_buys += 1

    save_position()


def grid_close_sell(level: int, max_qty: float) -> float:
    """
    ลด qty ของ slot ที่ level นี้ลง (SELL) สูงสุดไม่เกิน max_qty
    คืนค่า qty ที่ปิดได้จริง
    """
    global grid_slots, open_grid_buys
    if max_qty <= 0:
        return 0.0

    slot = grid_slots.get(level)
    if slot is None:
        return 0.0

    available = slot.get("qty", 0.0)
    if available <= 0:
        return 0.0

    qty = min(max_qty, available)
    if qty <= 0:
        return 0.0

    slot["qty"] = available - qty
    if slot["qty"] <= 1e-12:
        del grid_slots[level]
        open_grid_buys = max(open_grid_buys - 1, 0)

    save_position()
    return qty


def log_position(px: Optional[float] = None):
    """log ต้นทุนเฉลี่ย, unrealized PnL, realized PnL และจำนวน slots"""
    if position_qty <= 0:
        log(f"[POS] flat | realized={realized_pnl_thb:.2f} THB | open_slots={open_grid_buys}")
        return

    avg_cost = pos_avg_cost()
    unreal = (px - avg_cost) * position_qty if px is not None else 0.0

    log(
        "[POS] qty={qty:.6f} {asset} avg_cost={avg:.4f} THB | "
        "cost_sum={cost:.2f} THB | unreal={unreal:.2f} THB | "
        "realized={realized:.2f} THB | open_slots={ogb}"
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

last_grid_level: Optional[int] = None  # level ล่าสุดในรอบก่อนหน้า
last_trade_price: Optional[float] = None  # px ตอน trade ล่าสุด (ใช้สำหรับ hysteresis)


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
    แปลงราคา -> index แบบ float ว่าอยู่ห่างจาก center กี่ step (ยังไม่ floor)
    """
    step = grid_step_thb()
    if step <= 0:
        return 0.0
    return (price - GRID_CENTER_PRICE) / step


# ------------------------------------------------------------
# [5.1] ACCOUNT — Wallet / Balances
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
    global last_grid_level, last_trade_price

    load_position()

    last_trade_ts = 0.0   # เวลาเทรดล่าสุด (ใช้กับ COOLDOWN)
    debug_counter = 0

    step_thb = grid_step_thb()
    log(f"Bitkub GRID Bot — {SYMBOL}")
    log(f"GRID_CENTER={GRID_CENTER_PRICE} THB | STEP={GRID_STEP_PCT}% (~{step_thb:.4f} THB/step)")
    log(f"LEVELS_DOWN={GRID_LEVELS_DOWN} LEVELS_UP={GRID_LEVELS_UP}")
    log(f"ORDER_NOTIONAL_THB={ORDER_NOTIONAL_THB} DRY_RUN={DRY_RUN}")
    log(f"COOLDOWN_SEC={COOLDOWN_SEC}")
    est_profit = GRID_STEP_PCT - 2 * FEE_RATE * 100.0
    log(f"Est. profit per grid (before slippage) ≈ {est_profit:.3f}%")
    if est_profit <= 0:
        log("[WARN] GRID_STEP_PCT <= 2*fee -> กำไรต่อ grid ก่อน slippage ไม่บวก ลองเพิ่ม GRID_STEP_PCT")

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

            # เส้นกริดล่าง/บนของ cell ปัจจุบัน (แนวคิด)
            buy_level_cell = lvl
            sell_level_cell = lvl + 1

            # ถ้า cell นี้หลุดกรอบกริด -> ไม่เทรด
            if buy_level_cell < -GRID_LEVELS_DOWN or sell_level_cell > GRID_LEVELS_UP:
                log(
                    f"[HOLD] out-of-grid-range lvl={lvl} (buy_level={buy_level_cell}, sell_level={sell_level_cell}) "
                    f"px={px:.4f} (center={GRID_CENTER_PRICE}, step≈{step_thb:.4f}) | open_slots={open_grid_buys}"
                )
                last_grid_level = lvl
                time.sleep(REFRESH_SEC)
                continue

            now_ts = time.time()
            in_cooldown = (now_ts - last_trade_ts) < COOLDOWN_SEC if last_trade_ts > 0 else False
            cooldown_left = COOLDOWN_SEC - (now_ts - last_trade_ts) if in_cooldown else 0.0

            # รอบแรก: ตั้งค่า last_grid_level แล้วรอดูการเคลื่อนที่ก่อน
            if last_grid_level is None:
                last_grid_level = lvl
                log(
                    f"[WARMUP] init grid level = {lvl} at px={px:.4f} "
                    f"(buy_level={buy_level_cell} , sell_level={sell_level_cell})"
                )
                time.sleep(REFRESH_SEC)
                continue

            moved_down = lvl < last_grid_level   # เลเวลลด -> BUY grids
            moved_up = lvl > last_grid_level     # เลเวลเพิ่ม -> SELL grids

            # ถ้าไม่มีการเปลี่ยนเลเวล -> HOLD
            if not moved_down and not moved_up:
                log(
                    f"[HOLD] px={px:.4f} lvl={lvl} prev_lvl={last_grid_level} "
                    f"buy_lvl={buy_level_cell} sell_lvl={sell_level_cell} | open_slots={open_grid_buys}"
                )
                last_grid_level = lvl
                time.sleep(REFRESH_SEC)
                continue

            # COOL DOWN กันเทรดถี่เกิน
            if in_cooldown:
                side = "BUY" if moved_down else "SELL"
                log(
                    f"[COOLDOWN] skip {side} move: lvl={lvl} (prev={last_grid_level}) "
                    f"remaining={cooldown_left:.1f}s px={px:.4f}"
                )
                last_grid_level = lvl
                time.sleep(REFRESH_SEC)
                continue

            # HYSTERESIS: ต้องขยับไกลจาก last_trade_price >= MIN_MOVE_PCT_FROM_LAST_TRADE
            if last_trade_price is not None and last_trade_price > 0:
                move_pct = abs(px - last_trade_price) / last_trade_price * 100.0
                if move_pct < MIN_MOVE_PCT_FROM_LAST_TRADE:
                    log(
                        f"[HYST] move={move_pct:.3f}% < MIN_MOVE_PCT_FROM_LAST_TRADE="
                        f"{MIN_MOVE_PCT_FROM_LAST_TRADE:.3f}% from last_trade_px={last_trade_price:.4f} "
                        f"-> skip trade this round"
                    )
                    last_grid_level = lvl
                    time.sleep(REFRESH_SEC)
                    continue

            traded_this_round = False

            # ===== BUY SIDE: ราคาเคลื่อนลงผ่านเส้น (อาจข้ามหลายเส้น) =====
            if moved_down:
                # ขั้นที่เปลี่ยน: last_grid_level-1, last_grid_level-2, ..., lvl
                quote_avail = get_available(QUOTE_ASSET)
                if quote_avail < ORDER_NOTIONAL_THB:
                    log(
                        f"[SKIP BUY] {QUOTE_ASSET}={quote_avail:.2f} < {ORDER_NOTIONAL_THB} | "
                        f"px={px:.4f} lvl_from={last_grid_level} -> lvl_to={lvl}"
                    )
                else:
                    for grid_lvl in range(last_grid_level - 1, lvl - 1, -1):
                        # เส้น BUY = grid_lvl (ด้านล่างของ cell ที่เพิ่งข้ามลงมา)
                        if grid_lvl < -GRID_LEVELS_DOWN:
                            break

                        if quote_avail < ORDER_NOTIONAL_THB:
                            log(
                                f"[SKIP BUY] out of {QUOTE_ASSET} during multi-step move | "
                                f"remaining={quote_avail:.2f} THB"
                            )
                            break

                        buy_px = round(grid_price(grid_lvl), PRICE_ROUND)
                        qty_est = ORDER_NOTIONAL_THB / buy_px

                        resp = place_bid(SYMBOL, ORDER_NOTIONAL_THB, buy_px, dry_run=DRY_RUN)

                        if not DRY_RUN:
                            on_fill_buy(qty_est, buy_px)
                        grid_open_buy(grid_lvl, qty_est)

                        quote_avail -= ORDER_NOTIONAL_THB
                        traded_this_round = True

                        log(
                            f"[BUY ] step_lvl={grid_lvl} (from prev_lvl={last_grid_level} -> new_lvl={lvl}) "
                            f"px={px:.4f} buy_px={buy_px} "
                            f"{QUOTE_ASSET}≈{ORDER_NOTIONAL_THB} (~{qty_est:.6f} {BASE_ASSET}) "
                            f"-> {resp} | open_slots={open_grid_buys}"
                        )

            # ===== SELL SIDE: ราคาเคลื่อนขึ้นผ่านเส้น (อาจข้ามหลายเส้น) =====
            elif moved_up:
                if open_grid_buys <= 0:
                    log(
                        f"[SKIP SELL] no open grid slots | px={px:.4f} "
                        f"lvl_from={last_grid_level} -> lvl_to={lvl}"
                    )
                else:
                    base_avail = get_available(BASE_ASSET)
                    if base_avail <= 0:
                        log(
                            f"[SKIP SELL] {BASE_ASSET}={base_avail:.6f} | "
                            f"px={px:.4f} lvl_from={last_grid_level} -> lvl_to={lvl}"
                        )
                    else:
                        # ขั้นที่เปลี่ยน: last_grid_level+1, last_grid_level+2, ..., lvl
                        for grid_lvl in range(last_grid_level + 1, lvl + 1):
                            # เส้น SELL = grid_lvl, จะปิด slot ของ level ด้านล่าง (grid_lvl - 1)
                            sell_level = grid_lvl
                            if sell_level > GRID_LEVELS_UP:
                                break

                            close_level = sell_level - 1  # ตามหลัก grid: ขึ้น 1 step -> ปิดไม้ของ level ที่ต่ำกว่า 1 step
                            if base_avail <= 0:
                                log(
                                    f"[SKIP SELL] out of {BASE_ASSET} during multi-step move | "
                                    f"remaining={base_avail:.6f}"
                                )
                                break

                            sell_px = round(grid_price(sell_level), PRICE_ROUND)
                            target_qty = ORDER_NOTIONAL_THB / sell_px
                            max_qty_for_sell = min(target_qty, base_avail)

                            qty_to_sell = grid_close_sell(close_level, max_qty_for_sell)
                            if qty_to_sell <= 0:
                                log(
                                    f"[SKIP SELL] no open slot at level={close_level} "
                                    f"| px={px:.4f} sell_lvl={sell_level} sell_px={sell_px}"
                                )
                                continue

                            resp = place_ask(SYMBOL, qty_to_sell, sell_px, dry_run=DRY_RUN)

                            if not DRY_RUN:
                                on_fill_sell(qty_to_sell, sell_px)

                            base_avail -= qty_to_sell
                            traded_this_round = True

                            log(
                                f"[SELL] step_lvl={sell_level} close_level={close_level} "
                                f"(from prev_lvl={last_grid_level} -> new_lvl={lvl}) "
                                f"px={px:.4f} sell_px={sell_px} "
                                f"qty≈{qty_to_sell:.6f} {BASE_ASSET} -> {resp} | open_slots={open_grid_buys}"
                            )

            # ถ้ามีเทรดในรอบนี้ -> อัปเดต last_trade_ts / last_trade_price / position log
            if traded_this_round:
                last_trade_ts = now_ts
                last_trade_price = px
                log_position(px)
            else:
                log(
                    f"[HOLD] after-check px={px:.4f} lvl={lvl} prev_lvl={last_grid_level} "
                    f"| open_slots={open_grid_buys}"
                )

            # อัปเดต level ล่าสุดทุกครั้ง
            last_grid_level = lvl

        except requests.HTTPError as e:
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
