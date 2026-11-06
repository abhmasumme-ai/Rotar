import asyncio
import json
import os
import queue
import random
import re
import sys
import threading
import time
import uuid
import io
from collections import OrderedDict
from pathlib import Path

import websockets
from playwright.sync_api import sync_playwright
import numpy as np

# ================= CONFIG =================
WS_URL = "ws://127.0.0.1:5000/ws"
HEARTBEAT_SEC = 10
RECONNECT_SEC = 3
PING_SEC = 30
HEADLESS = os.environ.get("CLICKER_HEADLESS") == "1"
CHROME_PATH = os.environ.get("CLICKER_CHROME_PATH") or None
TESSERACT_CMD = os.environ.get("TESSERACT_CMD") or None

COMPARE_THRESHOLD = 0.65 

VIEW_W = 1080
VIEW_H = 920
WIN_W = VIEW_W + 100
WIN_H = VIEW_H + 120

# ================= PATTERNS =================
TARGET_PATTERNS = [
    {"side": "over", "line": 0.5},
    {"side": "under", "line": 0.5},
    {"side": "over", "line": 1.5},
    {"side": "under", "line": 1.5},
    {"side": "over", "line": 2.5},
    {"side": "under", "line": 2.5},
    {"side": "over", "line": 3.5},
    {"side": "under", "line": 3.5},
]
# ============================================

event_q = queue.Queue()
ws_state = {"up": False}

used_points = {}
used_lock = threading.Lock()

_ocr = {"ready": False}
_ssim = {"ready": False}

TEMPLATES = {} 

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ---------- Engines ----------
def setup_engines():
    try:
        import pytesseract
        from PIL import Image, ImageOps
        cmd = TESSERACT_CMD or (r"C:\Program Files\Tesseract-OCR\tesseract.exe" if os.path.exists(r"C:\Program Files\Tesseract-OCR\tesseract.exe") else None)
        if cmd: pytesseract.pytesseract.tesseract_cmd = cmd
        pytesseract.get_tesseract_version()
        _ocr.update(ready=True, pt=pytesseract, Image=Image, ImageOps=ImageOps)
        log("🔎 [OCR] ready")
    except Exception as e:
        log(f"⚠️ OCR setup fail: {e}")
        return False

    try:
        import cv2
        _ssim.update(ready=True, cv2=cv2)
        log("🔎 [OpenCV] ready")
    except ImportError:
        log("⚠️ pip install opencv-python numpy")
        return False
    return True

# ---------- Setup Mode (Exact Regex Row Grouping) ----------
def setup_templates(page):
    print("\n🔍 [SETUP] Exact Pattern স্ক্যান করা হচ্ছে...")
    page.mouse.move(VIEW_W // 2, VIEW_H // 2)
    page.wait_for_timeout(500)

    seen = set()
    cv2 = _ssim["cv2"]
    
    # কঠোর রেগুলার এক্সপ্রেশন (শুধু এই ফরম্যাটটাই ধরবে)
    # উদাহরণ: 17 - Over, 3 - Delivery, Over 0.5
    strict_regex = r"(\d+)\s*-\s*Over,\s*(\d+)\s*-\s*Delivery,\s*(Over|Under)\s*(\d+(?:\.\d+)?)"
    
    for attempt in range(5):
        img_bytes = page.screenshot(type="png", scale="css")
        img_pil = _ocr["Image"].open(io.BytesIO(img_bytes))
        
        # get data with coordinates
        data = _ocr["pt"].image_to_data(img_pil, config="--psm 11", output_type=_ocr["pt"].Output.DICT)
        
        # ১. লাইন অনুযায়ী টেক্সট গ্রুপ করা (Row Grouping)
        rows = {}
        for i in range(len(data['text'])):
            text = str(data['text'][i]).strip()
            if not text: continue
            y_center = data['top'][i] + data['height'][i] / 2
            row_key = round(y_center / 10) * 10  # ১০ পিক্সেল কাছাকাছি হলে একই লাইন ধরা হবে
            
            if row_key not in rows: rows[row_key] = []
            rows[row_key].append(i)
            
        for row_key, indices in rows.items():
            indices.sort(key=lambda i: data['left'][i])
            
            # লাইনটির সম্পূর্ণ টেক্সট বানানো
            full_text = ""
            word_start_indices = []
            for i in indices:
                word = str(data['text'][i]).strip()
                if not word: continue
                word_start_indices.append((len(full_text), i))
                full_text += word + " "
                
            # ২. Exact Regex Match
            match = re.search(strict_regex, full_text, re.IGNORECASE)
            if match:
                side = match.group(3).lower()
                line = float(match.group(4))
                
                # চেক করা এটি আমাদের টার্গেট লিস্টে আছে কি না
                is_target = False
                for t in TARGET_PATTERNS:
                    if t["side"] == side and abs(t["line"] - line) < 0.01:
                        is_target = True
                        break
                
                if is_target and (side, line) not in seen:
                    matched_str = match.group(0)
                    
                    # ৩. শুধু টেক্সট এরিয়ার বাউন্ডিং বক্স বের করা (ওডস বাদ দেওয়ার জন্য)
                    valid_indices = []
                    for start_idx, word_idx in word_start_indices:
                        if start_idx < len(matched_str):
                            valid_indices.append(word_idx)
                            
                    if not valid_indices: continue
                    
                    min_x = min(data['left'][i] for i in valid_indices)
                    max_x = max(data['left'][i] + data['width'][i] for i in valid_indices)
                    min_y = min(data['top'][i] for i in valid_indices)
                    max_y = max(data['top'][i] + data['height'][i] for i in valid_indices)
                    
                    pad_x, pad_y = 5, 3
                    rect = {"x": max(0, min_x - pad_x), "y": max(0, min_y - pad_y), "w": (max_x - min_x) + pad_x*2, "h": (max_y - min_y) + pad_y*2}
                    
                    crop_bytes = page.screenshot(type="png", scale="css", clip=rect)
                    crop_arr = np.frombuffer(crop_bytes, np.uint8)
                    tpl_img = cv2.imdecode(crop_arr, cv2.IMREAD_GRAYSCALE)
                    
                    TEMPLATES[(side, line)] = {"img": tpl_img}
                    seen.add((side, line))
                    log(f"   ✅ Exact Match: '{matched_str}' -> {side} {line}")
        
        if len(seen) >= 2: break
        page.mouse.wheel(0, 400)
        page.wait_for_timeout(300)

# ---------- Live Mode ----------
def fast_match_and_click(page, side, line, ball_key, current_over):
    tpl_data = TEMPLATES.get((side, line))
    if not tpl_data: return False
    
    cv2 = _ssim["cv2"]
    tpl_img = tpl_data["img"]
    
    def try_match():
        img_bytes = page.screenshot(type="png", scale="css")
        np_arr = np.frombuffer(img_bytes, np.uint8)
        src_img = cv2.imdecode(np_arr, cv2.IMREAD_GRAYSCALE)
        if src_img is None: return None
        
        res = cv2.matchTemplate(src_img, tpl_img, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        
        if max_val >= COMPARE_THRESHOLD:
            h, w = tpl_img.shape[:2]
            return {"x": max_loc[0], "y": max_loc[1], "w": w, "h": h, "score": max_val}
        return None

    match = try_match()
    
    # স্ক্রল করে খোঁজা (যদি স্ক্রিনের বাইরে থাকে)
    if not match:
        page.mouse.move(VIEW_W // 2, VIEW_H // 2)
        page.mouse.wheel(0, -300)
        page.wait_for_timeout(100)
        match = try_match()
    if not match:
        page.mouse.wheel(0, 600)
        page.wait_for_timeout(100)
        match = try_match()

    if match:
        log(f"   🎯 Found at ({match['x']},{match['y']}) score={match['score']:.3f}")
        do_human_click(page, ball_key, match)
        return True
        
    # ⭐ Self-Healing: যদি ওভার পরিবর্তন হয়ে যায় (যেমন ১৭ থেকে ১৮), তবে ছবি মিলবে না। তখন রিস্ক্যান করবে।
    if current_over:
        log(f"🔄 [RESCAN] Over {current_over} এর জন্য নতুন করে স্ক্যান করা হচ্ছে...")
        setup_templates(page)
        
    return False

# ---------- Human-like click ----------
def do_human_click(page, ball_key, rect):
    x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
    with used_lock:
        used = used_points.setdefault(ball_key, set())
    point = None
    for _ in range(6):
        px = x + random.uniform(0.2 * w, 0.8 * w)
        py = y + random.uniform(0.2 * h, 0.8 * h)
        k = (round(px), round(py))
        if k not in used:
            used.add(k); point = (px, py); break
    if point is None: point = (x + w / 2, y + h / 2)
    px, py = point
    try:
        page.mouse.move(px, py)
        page.wait_for_timeout(random.randint(10, 30))
        page.mouse.down()
        page.wait_for_timeout(random.randint(10, 25))
        page.mouse.up()
        log(f"✅ [CLICK] ball={ball_key} at ({px:.0f},{py:.0f})")
    except Exception as e:
        log(f"⚠️ [CLICK] error: {e}")

# ---------- Push parse ----------
def parse_push(payload):
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict): return None
    ball = data.get("ball")
    if isinstance(ball, dict):
        raw = ball.get("ball_key")
        bkey = str(raw) if raw is not None else ""
        if ball.get("is_delete") is True: return {"kind": "delete", "ball_key": bkey, "data": data}
        detail = ball.get("detail")
        if isinstance(detail, dict): return {"kind": "second", "ball_key": bkey, "data": data, "detail": detail}
        return {"kind": "first", "ball_key": bkey, "data": data}
    return {"kind": "match", "data": data}

def ball_info(d):
    bt = str(d.get("ball_type") or "normal").lower()
    ts, bw, bs = d.get("team_score") or {}, d.get("bowler") or {}, d.get("batsman") or {}
    if ts.get("is_wicket") or bw.get("is_wicket") or d.get("wicket"): return ("WICKET", "wicket", 0)
    if re.search(r"no_?ball", bt):
        br = int(bs.get("runs") or 0)
        return (f"NO BALL +{br}" if br > 0 else "NO BALL", "noball", 1 + br)
    if bs.get("is_six"): return ("6", "six", 6)
    if bs.get("is_four"): return ("4", "four", 4)
    if re.search(r"wide", bt):
        wr = int(ts.get("runs") or 1) - 1
        return (f"WIDE +{wr}" if wr > 0 else "WIDE", "wide", 1 + wr)
    r = ts.get("runs") if ts.get("runs") is not None else (bs.get("runs") or 0)
    return (str(int(r)), "dot" if int(r) == 0 else "run", int(r))

def compute_target(d):
    ov = d.get("overs")
    if isinstance(ov, list) and len(ov) >= 2:
        try: return int(ov[0]) + 1
        except: pass
    return None

def make_seen_lru(maxlen):
    od = OrderedDict()
    def remember(k):
        if k in od: od.move_to_end(k)
        else: od[k] = True
        while len(od) > maxlen: od.popitem(last=False)
    return remember, lambda k: k in od, lambda k: od.pop(k, None)

# ---------- WebSocket ----------
async def ws_loop():
    while not _stop_evt.is_set():
        err = None
        try:
            async with websockets.connect(WS_URL, ping_interval=None, compression=None, max_size=None) as ws:
                ws_state["up"] = True
                log(f"🔌 [WS] connected: {WS_URL}")
                async def pinger():
                    while not _stop_evt.is_set():
                        await asyncio.sleep(PING_SEC)
                        try: await ws.send("ping")
                        except Exception: break
                ptask = asyncio.create_task(pinger())
                try:
                    async for msg in ws:
                        if msg == "pong": continue
                        event_q.put((time.time(), msg))
                finally:
                    ptask.cancel()
        except Exception as e:
            err = e
        if ws_state["up"]: log("🔌 [WS] disconnected")
        ws_state["up"] = False
        if _stop_evt.is_set(): break
        log(f"🔄 [WS] {RECONNECT_SEC}s pore reconnect... ({err or 'closed'})")
        await asyncio.sleep(RECONNECT_SEC)

def start_ws_thread():
    threading.Thread(target=lambda: asyncio.run(ws_loop()), daemon=True).start()

# ---------- Main ----------
def monitor_and_click():
    global _stop_evt
    _stop_evt = threading.Event()

    if len(sys.argv) < 2: return
    match_filter = sys.argv[1].strip()
    
    site_url = input("\nSite link din: ").strip() or "https://example.com"
    if not setup_engines(): return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, args=[f"--window-size={WIN_W},{WIN_H}", "--window-position=0,0"])
        context = browser.new_context(viewport={"width": VIEW_W, "height": VIEW_H}, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36")
        page = context.new_page()
        page.goto(site_url)
        
        print("\n=== ম্যানুয়ালি লগইন করুন ===")
        match_url = input("Match URL: ").strip()
        if not match_url: browser.close(); return
        page.goto(match_url)
        log(f"✅ Match page: {page.url}")

        print("\n" + "="*50)
        print("👉 ব্রাউজারে গিয়ে ম্যানুয়ালি 'Quick Events' সিলেক্ট করুন।")
        print("👉 স্ক্রিন রেডি হলে টার্মিনালে 'done' লিখে Enter দিন।")
        print("="*50 + "\n")
        
        while True:
            ans = input("টার্মিনালে 'done' লিখুন: ").strip().lower()
            if ans == "done": break

        log("▶️ স্ক্যানিং শুরু হচ্ছে...")
        
        while not TEMPLATES:
            setup_templates(page)
            if not TEMPLATES:
                log("⚠️ এখনো কোনো Exact Pattern পাওয়া যায়নি (মার্কেট সাসপেন্ড হতে পারে)।")
                log("🔄 ৫ সেকেন্ড পর আবার স্ক্যান করা হবে... (বন্ধ করতে Ctrl+C চাপুন)")
                time.sleep(5)

        while not event_q.empty():
            try: event_q.get_nowait()
            except queue.Empty: break

        start_ws_thread()
        log(f"👂 [WS] wait korchi...\n")

        remember_seen, was_seen, forget_seen = make_seen_lru(300)
        last_event = time.time()

        try:
            while True:
                try: recv_ts, raw = event_q.get(timeout=0.5)
                except queue.Empty:
                    if time.time() - last_event >= HEARTBEAT_SEC:
                        log(f"⏳ WS={'ON' if ws_state['up'] else 'OFF'} | templates={len(TEMPLATES)}")
                        last_event = time.time()
                    continue

                last_event = time.time()
                try: payload = json.loads(raw)
                except ValueError: continue
                
                push = parse_push(payload)
                if not push: continue

                data = push["data"]
                mkey = data.get("matchkey") or data.get("match_key") or data.get("key")
                if mkey and mkey != match_filter: continue

                if push["kind"] == "second":
                    bkey = push["ball_key"]
                    d = push["detail"]
                    if was_seen(bkey): continue
                    remember_seen(bkey)
                    
                    text, kind, run = ball_info(d)
                    side, line = ("under", 0.5) if run == 0 else ("over", max([ln for ln in [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5] if run > ln], default=0.5))
                    current_over = compute_target(d)
                    
                    log(f"🏏 [PUSH-2] run={run} → {side} {line}")
                    
                    if not fast_match_and_click(page, side, line, bkey, current_over):
                        log(f"❌ [NO-CLICK] {side} {line} বাটনটি স্ক্রিনে পাওয়া যায়নি।")
                    continue

        except KeyboardInterrupt:
            print("\nবন্ধ করা হচ্ছে...")
        finally:
            _stop_evt.set()
            browser.close()

if __name__ == "__main__":
    monitor_and_click()
