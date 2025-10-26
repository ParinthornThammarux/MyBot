# ============================================================
#  Bitkub Mean Reversion Bot — THB_XRP (พร้อมคำอธิบายละเอียด)
# ============================================================

import os, time, hmac, hashlib, json, requests
import datetime
from statistics import mean, pstdev
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv
load_dotenv()

# ------------------------------------------------------------
# [1] CONFIGURATION — ตั้งค่าพื้นฐานของบอตและพารามิเตอร์กลยุทธ์
# ------------------------------------------------------------
BASE_URL = "https://api.bitkub.com"  # URL หลักของ Bitkub API
API_KEY  = os.getenv("BITKUB_API_KEY", "")          # คีย์สำหรับเข้าถึง Private API
API_SECRET = (os.getenv("BITKUB_API_SECRET", "") or "").encode()  # secret key (ใช้เซ็น HMAC)

# === พารามิเตอร์ของกลยุทธ์ mean reversion ===
SYMBOL = "THB_XRP"          # คู่เทรด XRP/THB
WINDOW = 80                 # จำนวนจุดราคาล่าสุดที่ใช้คำนวณค่าเฉลี่ยและส่วนเบี่ยงเบนมาตรฐาน
THRESH_Z = 1.6              # เกณฑ์ z-score สำหรับเข้าเทรด (เช่น |z| > 1.6)
REFRESH_SEC = 3             # ดึงข้อมูลใหม่ทุกกี่วินาที
ORDER_NOTIONAL_THB = 100    # ขนาดคำสั่งต่อไม้ (หน่วยเป็น THB)
SLIPPAGE_BPS = 8            # ตั้งราคาเผื่อหลุด 0.08% จากราคาปัจจุบัน
DRY_RUN = True              # ถ้า True = ทดสอบ ไม่ส่งคำสั่งจริง
PRICE_ROUND = 2             # จำนวนทศนิยมของราคาเวลาส่งคำสั่ง
QTY_ROUND = 6               # จำนวนทศนิยมของจำนวนเหรียญเวลาส่งคำสั่ง
MAX_SERIES_LEN = 5000       # ความยาวสูงสุดของลิสต์ราคาที่เก็บไว้
TRADES_FETCH = max(200, WINDOW + 5)  # จำนวนเทรดที่ดึงมาแต่ละครั้ง
TIME_SYNC_INTERVAL = 300    # รีเฟรชเวลาจาก server ทุก 5 นาที

# ------------------------------------------------------------
# [2] SERVER TIME SYNC — ใช้เวลาเซิร์ฟเวอร์ Bitkub เป็นหลัก
# ------------------------------------------------------------

# ตัวแปรเก็บค่าความต่างระหว่างเวลา server กับ local (มิลลิวินาที)
_server_offset_ms = 0
_last_sync_ts = 0  # เวลา (หน่วยวินาที) ที่ sync ล่าสุด

def sync_server_time():
    """
    ดึงเวลาปัจจุบันจาก Bitkub server (GET /api/servertime)
    แล้วคำนวณส่วนต่างระหว่าง server_time กับ local_time
    เพื่อใช้ชดเชยเวลาในทุก request ภายหลัง
    """
    global _server_offset_ms, _last_sync_ts
    try:
        url = f"{BASE_URL}/api/v3/servertime"
        r = requests.get(url, timeout=5)
        server_time = int(r.json())                # เวลาเซิร์ฟเวอร์ (หน่วยมิลลิวินาที)
        local_time = int(time.time() * 1000)       # เวลาเครื่องเรา
        _server_offset_ms = server_time - local_time  # ส่วนต่าง (offset)
        _last_sync_ts = time.time()
        readable_time = datetime.datetime.fromtimestamp(server_time / 1000)
        print(f"[SYNC] Server time synced, offset={_server_offset_ms} ms")
        print(f"[SERVER TIME] {readable_time:%Y-%m-%d %H:%M:%S}")
    except Exception as e:
        print("[SYNC ERROR]", e)

def ts_ms_str() -> str:
    """
    คืน timestamp ปัจจุบัน (หน่วยมิลลิวินาที) โดยอิงเวลาเซิร์ฟเวอร์เป็นหลัก  
    ถ้าผ่านไปนานกว่า TIME_SYNC_INTERVAL จะเรียก sync_server_time() ใหม่
    """
    global _server_offset_ms
    now = time.time()
    if now - _last_sync_ts > TIME_SYNC_INTERVAL:
        sync_server_time()
    local_ms = int(now * 1000)
    return str(local_ms + _server_offset_ms)

# ------------------------------------------------------------
# [3] AUTH UTILITIES — ฟังก์ชันเกี่ยวกับการเซ็นลายเซ็นและสร้าง headers
# ------------------------------------------------------------
def sign(timestamp_ms: str, method: str, request_path: str, body: str = "") -> str:
    """
    สร้างลายเซ็น (signature) ตามสเปก Bitkub:
    signature = HMAC_SHA256( timestamp + METHOD + requestPath + body, apiSecret )
    ใช้สำหรับยืนยันตัวตนกับ Private API
    """
    payload = (timestamp_ms + method.upper() + request_path + body).encode()
    return hmac.new(API_SECRET, payload, hashlib.sha256).hexdigest()

def build_headers(timestamp_ms: str, signature: Optional[str] = None) -> Dict[str, str]:
    """
    สร้าง headers ที่ต้องใช้ในทุก request (โดยเฉพาะ Private API)
    ประกอบด้วย:
      - X-BTK-APIKEY     : รหัส API key ของเรา
      - X-BTK-TIMESTAMP  : เวลา (ms) ปัจจุบัน
      - X-BTK-SIGN       : ลายเซ็น (ถ้ามี)
    """
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
# [4] PUBLIC API — ดึงข้อมูลตลาดแบบไม่ต้องยืนยันตัวตน
# ------------------------------------------------------------
def get_trades(sym: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    เรียกดูรายการเทรดล่าสุดของคู่เหรียญ (Public)
    Endpoint: GET /api/market/trades?sym=THB_XRP&lmt=...
    คืนลิสต์ของ dict ที่มี rate, amt, ts, side ฯลฯ
    """
    url = f"{BASE_URL}/api/market/trades"
    r = requests.get(url, params={"sym": sym, "lmt": limit}, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("result", [])

# ------------------------------------------------------------
# [5] PRIVATE TRADE API — ฟังก์ชันที่ต้องใช้ API key/secret
# ------------------------------------------------------------
def place_bid(sym: str, thb_amount: float, rate: float, dry_run: bool = True) -> Dict[str, Any]:
    """
    ส่งคำสั่งซื้อ (limit buy)
    Endpoint: POST /api/v3/market/place-bid

    พารามิเตอร์สำคัญ:
      - sym: คู่เทรด เช่น 'THB_XRP'
      - amt: จำนวนเงิน THB ที่ใช้ซื้อ
      - rat: ราคาที่ต้องการซื้อ (limit)
      - typ: ประเภทคำสั่ง ('limit' หรือ 'market')
    """
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
        # ถ้า DRY_RUN=True จะไม่ส่งคำสั่งจริง แต่คืน payload กลับมาดูเฉย ๆ
        return {"dry_run": True, "endpoint": path, "payload": payload}

    sg = sign(ts, method, path, body)
    r = requests.post(BASE_URL + path, headers=build_headers(ts, sg), data=body, timeout=10)
    r.raise_for_status()
    return r.json()

def place_ask(sym: str, qty_coin: float, rate: float, dry_run: bool = True) -> Dict[str, Any]:
    """
    ส่งคำสั่งขาย (limit sell)
    Endpoint: POST /api/v3/market/place-ask

    พารามิเตอร์สำคัญ:
      - sym: คู่เทรด
      - amt: จำนวนเหรียญที่จะขาย
      - rat: ราคาขาย
    """
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
# [6] STRATEGY FUNCTIONS — ฟังก์ชันช่วยคำนวณทางสถิติ (mean reversion)
# ------------------------------------------------------------
def vwap_tail(trades: List[Dict[str, Any]], tail: int = 10) -> Optional[float]:
    """
    คำนวณราคาเฉลี่ยถ่วงน้ำหนักตามปริมาณ (VWAP)  
    จากรายการเทรดล่าสุด n รายการ (tail)
    ใช้เพื่อลด noise ของราคาจุดเดียว
    """
    if not trades:
        return None
    t = trades[-min(tail, len(trades)):]
    total = sum(float(x["amt"]) * float(x["rate"]) for x in t)
    qty = sum(float(x["amt"]) for x in t)
    return (total / qty) if qty > 0 else float(t[-1]["rate"])

def compute_zscore(series: List[float], window: int) -> Optional[float]:
    """
    คำนวณ Z-score = (ราคาล่าสุด - ค่าเฉลี่ย) / ส่วนเบี่ยงเบนมาตรฐาน
    ใช้เป็นตัวชี้วัดว่า 'ราคาเบี่ยงจากค่าเฉลี่ยมากแค่ไหน'
    ถ้า Z < -THRESH_Z → ถือว่าต่ำเกิน → สัญญาณซื้อ
    ถ้า Z > +THRESH_Z → ถือว่าสูงเกิน → สัญญาณขาย
    """
    if len(series) < window or window < 2:
        return None
    sample = series[-window:]
    mu = mean(sample)
    sig = pstdev(sample) or 1e-9
    return (series[-1] - mu) / sig

# ------------------------------------------------------------
# [7] MAIN LOOP — วนลูปหลักของบอต
# ------------------------------------------------------------
def run_loop():
    """
    ลูปหลักของบอต:
      1. sync เวลาเซิร์ฟเวอร์ก่อนเริ่ม
      2. ดึงราคาล่าสุดจากตลาด
      3. คำนวณค่า z-score
      4. ถ้า z-score เกินเกณฑ์ → ส่งคำสั่งซื้อ/ขาย (หรือ DRY_RUN)
      5. ทำซ้ำทุก REFRESH_SEC วินาที
    """
    sync_server_time()  # ซิงค์เวลาเซิร์ฟเวอร์ก่อนเริ่ม
    price_series: List[float] = []  # เก็บราคาล่าสุดย้อนหลัง
    position_coin = 0.0             # จำลองสถานะถือเหรียญ (ของจริงควรอ่านจาก balances)

    print(f"Bitkub Mean Reversion Bot — {SYMBOL}")
    print(f"WINDOW={WINDOW} THRESH_Z={THRESH_Z} DRY_RUN={DRY_RUN}")

    while True:
        try:
            # 1️⃣ ดึงรายการเทรดล่าสุด
            trades = get_trades(SYMBOL, limit=TRADES_FETCH)
            px = vwap_tail(trades, tail=10)
            if px is None:
                time.sleep(REFRESH_SEC)
                continue

            # 2️⃣ เก็บราคาไว้ในซีรีส์
            price_series.append(px)
            if len(price_series) > MAX_SERIES_LEN:
                price_series = price_series[-MAX_SERIES_LEN//2:]

            # 3️⃣ คำนวณ z-score
            z = compute_zscore(price_series, WINDOW)
            if z is None:
                print("[WARMUP] collecting data...")
                time.sleep(REFRESH_SEC)
                continue

            # 4️⃣ คำนวณราคาซื้อ/ขายเผื่อสลิปเพจ
            bid_px = round(px * (1 - SLIPPAGE_BPS / 10000), PRICE_ROUND)
            ask_px = round(px * (1 + SLIPPAGE_BPS / 10000), PRICE_ROUND)

            # 5️⃣ ตัดสินใจเทรดตาม z-score
            if z <= -THRESH_Z:
                qty_est = ORDER_NOTIONAL_THB / bid_px
                resp = place_bid(SYMBOL, ORDER_NOTIONAL_THB, bid_px, dry_run=DRY_RUN)
                print(f"[BUY ] z={z:.2f} bid≈{bid_px} THB size≈{ORDER_NOTIONAL_THB} (~{qty_est:.6f} XRP) -> {resp}")
                if not DRY_RUN:
                    position_coin += qty_est

            elif z >= THRESH_Z and position_coin > 0:
                sell_qty = round(position_coin * 0.5, QTY_ROUND)
                resp = place_ask(SYMBOL, sell_qty, ask_px, dry_run=DRY_RUN)
                print(f"[SELL] z={z:.2f} ask≈{ask_px} THB qty≈{sell_qty:.6f} -> {resp}")
                if not DRY_RUN:
                    position_coin -= sell_qty

            else:
                print(f"[HOLD] px={px:.4f} z={z:.2f}")

        except requests.HTTPError as e:
            print("HTTP error:", getattr(e.response, "text", str(e)))
        except Exception as e:
            print("Error:", e)

        time.sleep(REFRESH_SEC)

# ------------------------------------------------------------
# [8] ENTRY POINT — จุดเริ่มรันโปรแกรม
# ------------------------------------------------------------
if __name__ == "__main__":
    #run_loop()
    sync_server_time()
    trades = get_trades(SYMBOL)
    print(f"Fetched trades: {len(trades)}", flush=True)
    if trades:
        print("Sample trade:", trades, flush=True)