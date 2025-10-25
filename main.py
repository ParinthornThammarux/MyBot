# bitkub_mean_reversion_bot.py (TEST SAFE VERSION)
# -*- coding: utf-8 -*-
import os, time, hmac, hashlib, json, math, random, sys
import requests
from collections import deque
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

"""
การใช้งาน:
1) ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET (ใน .env หรือ export)
2) เปิดโหมดทดสอบ (SIMULATION_MODE = True) เพื่อดูการทำงานโดยไม่ส่งคำสั่งจริง
3) python bitkub_mean_reversion_bot.py
"""

# ---------------- CONFIG ----------------
load_dotenv()
BITKUB_BASE = "https://api.bitkub.com"
API_KEY    = os.getenv("BITKUB_API_KEY") or "YOUR_API_KEY"
API_SECRET = (os.getenv("BITKUB_API_SECRET") or "YOUR_API_SECRET").encode("utf-8")

SYMBOL         = "THB_BTC"
INTERVAL_S     = 5
WINDOW_MIN     = 15
WINDOW_TICKS   = int((WINDOW_MIN*60)//INTERVAL_S)

THB_PER_TRADE  = 100.0
SL_PCT         = 0.5/100
TP_PCT         = 1.0/100

BUY_SLIP_PCT   = 0.2/100
SELL_SLIP_PCT  = 0.2/100
REQ_TIMEOUT    = 10
JITTER_MAX_S   = 0.6

SIMULATION_MODE = True   # <<< เปิด True เพื่อทดสอบ ไม่ส่งคำสั่งจริง

prices   = deque(maxlen=WINDOW_TICKS)
position = None
session = requests.Session()
session.headers.update({"Accept":"application/json","Content-Type":"application/json"})

def log(*args):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(ts, "-", *args, flush=True)

# --------------- API HELPERS ---------------
def _sign(method: str, path: str, body_obj=None):
    ts = str(int(time.time() * 1000))
    body = "" if body_obj is None else json.dumps(body_obj, separators=(",",":"))
    payload = ts + method + path + body
    sig = hmac.new(API_SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "X-BTK-APIKEY": API_KEY,
        "X-BTK-TIMESTAMP": ts,
        "X-BTK-SIGN": sig,
        "Accept":"application/json","Content-Type":"application/json"
    }
    return headers, body

def server_time():
    r = session.get(f"{BITKUB_BASE}/api/servertime", timeout=REQ_TIMEOUT)
    return r.json()

def ticker(sym):
    r = session.get(f"{BITKUB_BASE}/api/market/ticker?sym={sym}", timeout=REQ_TIMEOUT)
    j = r.json()
    return float(j[sym]["last"])

def get_balances():
    path = "/api/market/wallet"
    headers, body = _sign("POST", path, {})
    r = session.post(f"{BITKUB_BASE}{path}", headers=headers, data=body, timeout=REQ_TIMEOUT)
    return r.json()

# ---- MOCK API (จำลองตอนทดสอบ) ----
def mock_response(action, **kwargs):
    return {"error": 0, "result": f"[SIMULATED {action}] {kwargs}"}

def place_bid(sym, amt_thb, rate, order_type="limit"):
    if SIMULATION_MODE:
        log(f"[TEST] place_bid(sym={sym}, amt_thb={amt_thb}, rate={rate:.2f})")
        return mock_response("BUY", sym=sym, amt_thb=amt_thb, rate=rate)
    path = "/api/market/place-bid"
    payload = {"sym": sym, "amt": round(float(amt_thb), 2), "rat": float(rate), "typ": order_type}
    headers, body = _sign("POST", path, payload)
    r = session.post(f"{BITKUB_BASE}{path}", headers=headers, data=body, timeout=REQ_TIMEOUT)
    return r.json()

def place_ask(sym, amt_coin, rate, order_type="limit"):
    if SIMULATION_MODE:
        log(f"[TEST] place_ask(sym={sym}, amt_coin={amt_coin}, rate={rate:.2f})")
        return mock_response("SELL", sym=sym, amt_coin=amt_coin, rate=rate)
    path = "/api/market/place-ask"
    payload = {"sym": sym, "amt": float(amt_coin), "rat": float(rate), "typ": order_type}
    headers, body = _sign("POST", path, payload)
    r = session.post(f"{BITKUB_BASE}{path}", headers=headers, data=body, timeout=REQ_TIMEOUT)
    return r.json()

# --------------- STRATEGY ---------------
def zscore(vals):
    n = len(vals)
    if n < 2: return 0.0, 0.0, 1.0
    m = sum(vals)/n
    var = sum((x-m)**2 for x in vals)/max(1,(n-1))
    s = math.sqrt(var) if var>0 else 1e-9
    return (vals[-1]-m)/s, m, s

ENTRY_Z = -1.5
EXIT_Z  = 0.0

def main():
    log("โหมดจำลอง:", SIMULATION_MODE)
    try:
        st = server_time()
        log("Server time:", st.get("result", st))
    except Exception as e:
        log("เชื่อมต่อ servertime ไม่สำเร็จ:", e)

    log(f"เริ่มบอท | คู่ {SYMBOL} | หน้าต่าง {WINDOW_TICKS} จุด (~{WINDOW_MIN} นาที) | THB/ไม้ {THB_PER_TRADE} | SL {SL_PCT*100:.2f}% | TP {TP_PCT*100:.2f}%")

    global position
    while True:
        try:
            px = float(ticker(SYMBOL))
            prices.append(px)
            z, mean, std = zscore(prices)
            log(f"[TICK] px={px:.2f} z={z:.2f} mean={mean:.2f} std={std:.2f}")

            if position:
                entry = position["entry"]
                qty   = position["qty_btc"]
                hit_sl = px <= entry*(1 - SL_PCT)
                hit_tp = px >= entry*(1 + TP_PCT)
                exit_z = z >= EXIT_Z and len(prices) == WINDOW_TICKS

                if hit_sl or hit_tp or exit_z:
                    sell_rate = px * (1 - SELL_SLIP_PCT)
                    resp = place_ask(SYMBOL, amt_coin=qty, rate=sell_rate)
                    log(">> EXIT",
                        f"px={px:.2f} entry={entry:.2f} PnL={(px-entry)/entry*100:.3f}%",
                        "| reason:", "SL" if hit_sl else ("TP" if hit_tp else "Z"),
                        "| resp:", resp)
                    position = None
            else:
                if len(prices) == WINDOW_TICKS and z <= ENTRY_Z:
                    thb_amt = THB_PER_TRADE
                    qty_btc = max(0.0, round(thb_amt/px, 6))
                    buy_rate = px * (1 + BUY_SLIP_PCT)
                    resp = place_bid(SYMBOL, amt_thb=thb_amt, rate=buy_rate)
                    log(">> ENTRY",
                        f"px={px:.2f} z={z:.2f} mean={mean:.2f} std={std:.2f}",
                        "| thb=", thb_amt, "qty_btc=", qty_btc, "| resp:", resp)
                    if resp.get("error") == 0:
                        position = {"side": "long", "entry": px, "qty_btc": qty_btc, "entry_time": datetime.now(timezone.utc)}

            time.sleep(INTERVAL_S + random.uniform(0, JITTER_MAX_S))
        except Exception as e:
            log("ERR:", repr(e))
            time.sleep(2.0)

if __name__ == "__main__":
    main()
