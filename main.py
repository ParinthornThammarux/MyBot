# bitkub_mean_reversion_bot.py
# -*- coding: utf-8 -*-
import os, time, hmac, hashlib, json, math, random, sys
import requests
from collections import deque
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
"""
การใช้งาน:
1) ตั้งค่าตัวแปรแวดล้อม
   - BITKUB_API_KEY
   - BITKUB_API_SECRET
   (หรือแก้ค่าด้านล่างเป็นสตริงโดยตรง — ไม่แนะนำ)
2) pip install requests python-dotenv (ถ้าจะใช้ .env)
3) python bitkub_mean_reversion_bot.py
"""

# ---------------- CONFIG ----------------
load_dotenv()
BITKUB_BASE = "https://api.bitkub.com"
API_KEY    = os.getenv("BITKUB_API_KEY") or "YOUR_API_KEY"
API_SECRET = (os.getenv("BITKUB_API_SECRET") or "YOUR_API_SECRET").encode("utf-8")

SYMBOL         = "THB_BTC"       # คู่ที่เทรด
INTERVAL_S     = 5               # ดึงราคาทุก 5 วินาที
WINDOW_MIN     = 15              # หน้าต่าง ~15 นาที
WINDOW_TICKS   = int((WINDOW_MIN*60)//INTERVAL_S)  # 15 นาทีที่ความถี่ 5s => 180 จุด

THB_PER_TRADE  = 100.0           # ทุนต่อไม้ (บาท)
SL_PCT         = 0.5/100         # 0.5%
TP_PCT         = 1.0/100         # 1.0%

# ปรับลิมิตให้ "น่าจะจับคู่ได้ทันที"
BUY_SLIP_PCT   = 0.2/100         # ซื้อ: +0.2% จากราคา last
SELL_SLIP_PCT  = 0.2/100         # ขาย: -0.2% จากราคา last

# ป้องกัน rate limit เบื้องต้น
REQ_TIMEOUT    = 10
JITTER_MAX_S   = 0.6

# ---------------- STATE ----------------
prices   = deque(maxlen=WINDOW_TICKS)
position = None  # {"side":"long","entry":float,"qty_btc":float,"entry_time":datetime}

session = requests.Session()
session.headers.update({"Accept":"application/json","Content-Type":"application/json"})

def log(*args):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(ts, "-", *args, flush=True)

# --------------- API HELPERS ---------------
def _sign(method: str, path: str, body_obj=None):
    """
    Bitkub v3: signature = HMAC_SHA256(secret, timestamp + method + path + body_json)
    """
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
    return float(j[sym]["last"])  # THB last price

def get_balances():
    path = "/api/market/wallet"
    headers, body = _sign("POST", path, {})
    r = session.post(f"{BITKUB_BASE}{path}", headers=headers, data=body, timeout=REQ_TIMEOUT)
    return r.json()

def place_bid(sym, amt_thb, rate, order_type="limit"):
    path = "/api/market/place-bid"
    payload = {"sym": sym, "amt": round(float(amt_thb), 2), "rat": float(rate), "typ": order_type}
    headers, body = _sign("POST", path, payload)
    r = session.post(f"{BITKUB_BASE}{path}", headers=headers, data=body, timeout=REQ_TIMEOUT)
    return r.json()

def place_ask(sym, amt_coin, rate, order_type="limit"):
    path = "/api/market/place-ask"
    payload = {"sym": sym, "amt": float(amt_coin), "rat": float(rate), "typ": order_type}
    headers, body = _sign("POST", path, payload)
    r = session.post(f"{BITKUB_BASE}{path}", headers=headers, data=body, timeout=REQ_TIMEOUT)
    return r.json()

# (ออปชัน) ยกเลิกออเดอร์ ถ้าคุณอยากต่อยอด
def cancel_order(sym, order_id=None, order_hash=None, side=0):
    path = "/api/market/cancel-order"
    payload = {"sym": sym, "sd": side}
    if order_id is not None: payload["id"] = order_id
    if order_hash is not None: payload["hash"] = order_hash
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

# กฎ Mean Reversion:
# - เข้าเมื่อ z-score ต่ำกว่า ENTRY_Z
# - ออกเมื่อ z-score >= 0 หรือถึง TP/SL
ENTRY_Z = -1.5
EXIT_Z  = 0.0

def main():
    if "YOUR_API_KEY" in API_KEY or "YOUR_API_SECRET" in API_SECRET.decode():
        log("กรุณาใส่ BITKUB_API_KEY/BITKUB_API_SECRET ก่อนรัน")
        sys.exit(1)

    try:
        st = server_time()
        log("Servertime(ms):", st.get("result", st))
    except Exception as e:
        log("เชื่อมต่อ servertime ไม่สำเร็จ:", e)

    log(f"เริ่มบอท | คู่ {SYMBOL} | หน้าต่าง {WINDOW_TICKS} จุด (~{WINDOW_MIN} นาที) | THB/ไม้ {THB_PER_TRADE} | SL {SL_PCT*100:.2f}% | TP {TP_PCT*100:.2f}%")

    global position
    while True:
        try:
            px = float(ticker(SYMBOL))
            prices.append(px)

            z, mean, std = zscore(prices)

            # มีสถานะอยู่ → ตรวจ SL/TP/Exit
            if position:
                entry = position["entry"]
                qty   = position["qty_btc"]

                # เงื่อนไข SL/TP
                hit_sl = px <= entry*(1 - SL_PCT)
                hit_tp = px >= entry*(1 + TP_PCT)
                exit_z = z >= EXIT_Z and len(prices) == WINDOW_TICKS

                if hit_sl or hit_tp or exit_z:
                    # ตั้งขายลิมิตต่ำกว่าตลาดเล็กน้อย เพื่อโอกาสจับคู่ไว
                    sell_rate = px * (1 - SELL_SLIP_PCT)
                    resp = place_ask(SYMBOL, amt_coin=qty, rate=sell_rate, order_type="limit")
                    log(">> EXIT",
                        f"px={px:.2f} entry={entry:.2f} PnL={(px-entry)/entry*100:.3f}%",
                        "| reason:", "SL" if hit_sl else ("TP" if hit_tp else "Z"),
                        "| resp:", resp)
                    position = None

            # ไม่มีสถานะ → หาโอกาสเข้า
            else:
                if len(prices) == WINDOW_TICKS and z <= ENTRY_Z:
                    # ใช้ THB ต่อไม้คงที่
                    thb_amt = THB_PER_TRADE
                    qty_btc = max(0.0, round(thb_amt/px, 6))  # ปัดทศนิยมให้เหมาะสมกับคู่เทรด

                    if thb_amt >= 10 and qty_btc > 0:
                        buy_rate = px * (1 + BUY_SLIP_PCT)
                        resp = place_bid(SYMBOL, amt_thb=thb_amt, rate=buy_rate, order_type="limit")
                        log(">> ENTRY",
                            f"px={px:.2f} z={z:.2f} mean={mean:.2f} std={std:.2f}",
                            "| thb=", thb_amt, "qty_btc=", qty_btc, "| resp:", resp)

                        # สมมติเติมเต็ม (ในงานจริงควรเช็คสถานะออเดอร์/ยอดคงเหลือ)
                        if resp.get("error") == 0:
                            position = {
                                "side": "long",
                                "entry": px,          # ใช้ราคาตลาดล่าสุดเป็นราคาเข้าโดยประมาณ
                                "qty_btc": qty_btc,
                                "entry_time": datetime.now(timezone.utc)
                            }

            # พัก + ใส่จิตเตอร์เล็กน้อย เพื่อช่วยเรื่องเรตลิมิต
            time.sleep(INTERVAL_S + random.uniform(0, JITTER_MAX_S))

        except requests.HTTPError as he:
            log("HTTPError:", he)
            time.sleep(2.0)
        except Exception as e:
            log("ERR:", repr(e))
            time.sleep(2.0)

if __name__ == "__main__":
    main()
