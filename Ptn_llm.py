import asyncio
import json
import os
import queue
import random
import re
import sys
import threading
import time
from collections import OrderedDict

import websockets
from playwright.sync_api import sync_playwright
import numpy as np
import cv2
import pytesseract
from PIL import Image
import io

# ================= CONFIG =================
WS_URL = "ws://127.0.0.1:5000/ws"
HEARTBEAT_SEC = 10
RECONNECT_SEC = 3
PING_SEC = 30
HEADLESS = os.environ.get("CLICKER_HEADLESS") == "1"
CHROME_PATH = os.environ.get("CLICKER_CHROME_PATH") or None
TESSERACT_CMD = os.environ.get("TESSERACT_CMD") or (r"C:\Program Files\Tesseract-OCR\tesseract.exe" if os.path.exists(r"C:\Program Files\Tesseract-OCR\tesseract.exe") else None)

PENDING_TIMEOUT_SEC = 10.0     
STALE_MAP_TTL = 30.0           
REAP_INTERVAL = 5.0            
SEEN_SECOND_MAX = 300          
COMPARE_THRESHOLD = 0.65  

VIEW_W = 1080
VIEW_H = 920
WIN_W = VIEW_W + 100
WIN_H = VIEW_H + 120

# ⭐ আপনার দেওয়া মাপ (Absolute Position)
# (side, line, x, y, w, h)
TARGET_LAYOUTS = [
    ("over", 0.5, 7,   46, 435, 40),
    ("under", 0.5, 446, 46, 440, 40),
    ("over", 1.5, 7,   89, 435, 40),
    ("under", 1.5, 446, 89, 440, 40),
    ("over", 3.5, 7,   131, 435, 40),
    ("under", 3.5, 446, 131, 440, 40),
]

# আপনার ছবি অনুযায়ী "17 - Over..." লেখাটি যে পজিশনে ছিল (রেফারেন্সের জন্য)
BASE_ANCHOR_X = 6
BASE_ANCHOR_Y = 37

# স্ক্রল করার জন্য মার্কেট প্লেসের এরিয়া (বাইরের পেজ স্ক্রল রোধ করতে)
MARKET_X = 400
MARKET_Y = 500
# ==========================================

event_q = queue.Queue()
ws_state = {"up": False}

targets = []
targets_lock = threading.Lock()

first_maps = {}
first_lock = threading.Lock()

pending = []
pending_lock = threading.Lock()

stop_evt = threading.Event()
used_points = {}
used_lock = threading.Lock()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ---------- WebSocket ----------
async def ws_loop():
    while not stop_evt.is_set():
        err = None
        try:
            async with websockets.connect(WS_URL, ping_interval=None, compression=None, max_size=None) as ws:
                ws_state["up"] = True
                log(f"🔌 [WS] connected: {WS_URL}")

                async def pinger():
                    while not stop_evt.is_set():
                        await asyncio.sleep(PING_SEC)
                        try:
                            await ws.send("ping")
                        except Exception:
                            break

                ptask = asyncio.create_task(pinger())
                try:
                    async for msg in ws:
                        if msg == "pong":
                            continue
                        event_q.put((time.time(), msg))
                finally:
                    ptask.cancel()
        except Exception as e:
            err = e
        if ws_state["up"]:
            log("🔌 [WS] disconnected")
        ws_state["up"] = False
        if stop_evt.is_set():
            break
        log(f"🔄 [WS] {RECONNECT_SEC}s pore reconnect... ({err or 'closed'})")
        await asyncio.sleep(RECONNECT_SEC)

def start_ws_thread():
    threading.Thread(target=lambda: asyncio.run(ws_loop()), daemon=True).start()

# ---------- Engines ----------
def setup_engines():
    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    log("🔎 [OCR & OpenCV] ready for Dynamic Targeting")
    return True

# ---------- Human-like click ----------
def do_human_click(page, ball_key, rect):
    x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
    with used_lock:
        used = used_points.setdefault(ball_key, set())

    point = None
    for _ in range(6):
        px = x + random.uniform(0.15 * w, 0.85 * w)
        py = y + random.uniform(0.15 * h, 0.85 * h)
        k = (round(px), round(py))
        if k not in used:
            used.add(k)
            point = (px, py)
            break
    if point is None:
        point = (x + w / 2, y + h / 2)

    px, py = point
    try:
        page.mouse.move(px, py)
        page.wait_for_timeout(random.randint(20, 50))
        page.mouse.down()
        page.wait_for_timeout(random.randint(15, 40))
        page.mouse.up()
        log(f"✅ [CLICK] ball={ball_key} at ({px:.0f},{py:.0f})")
    except Exception as e:
        log(f"⚠️ [CLICK] error: {e}")

# ---------- ১ম পুশ (ডাইনামিক অ্যাংকর স্ক্যান) ----------
def process_first_push(page, ball_key, over_num, delivery_num):
    log(f"💾 [PUSH-1] ball={ball_key} (Searching Anchor for {over_num} - {delivery_num})")
    
    # ১. পুরো স্ক্রিনের স্ক্রিনশট
    img_bytes = page.screenshot(type="png", scale="css")
    np_arr = np.frombuffer(img_bytes, np.uint8)
    full_img = cv2.imdecode(np_arr, cv2.IMREAD_GRAYSCALE)
    img_pil = Image.open(io.BytesIO(img_bytes))
    
    # ২. OCR দিয়ে ডাইনামিক অ্যাংকর (যেমন 17 - Over, 3 - Delivery) খোঁজা
    data = pytesseract.image_to_data(img_pil, config="--psm 11", output_type=pytesseract.Output.DICT)
    
    anchor_x, anchor_y = BASE_ANCHOR_X, BASE_ANCHOR_Y # ফলব্যাক
    
    # লাইন অনুযায়ী গ্রুপ করা
    rows = {}
    for i in range(len(data['text'])):
        text = str(data['text'][i]).strip()
        if not text: continue
        y_center = data['top'][i] + data['height'][i] / 2
        row_key = round(y_center / 10) * 10
        if row_key not in rows: rows[row_key] = []
        rows[row_key].append(i)

    for row_key, indices in rows.items():
        indices.sort(key=lambda i: data['left'][i])
        row_text = " ".join([str(data['text'][i]).strip() for i in indices]).lower()
        
        # এই লাইনে কি আমাদের ডাইনামিক Over এবং Delivery আছে? (যেমন: "17 - over" এবং "3 - delivery")
        if str(over_num) in row_text and "over" in row_text and str(delivery_num) in row_text and "delivery" in row_text:
            anchor_x = min(data['left'][i] for i in indices)
            anchor_y = min(data['top'][i] for i in indices)
            log(f"   🎯 Anchor Found: X={anchor_x}, Y={anchor_y}")
            break

    # ৩. আপনার দেওয়া মাপের সাথে ডাইনামিক অ্যাংকরের দূরত্ব বের করা
    dx = anchor_x - BASE_ANCHOR_X
    dy = anchor_y - BASE_ANCHOR_Y

    cells = []
    with targets_lock:
        targets.clear()
        
    for side, line, rx, ry, rw, rh in TARGET_LAYOUTS:
        tx = rx + dx
        ty = ry + dy
        
        # স্ক্রিনের বাইরে গেলে স্কিপ
        if ty + rh > full_img.shape[0] or tx + rw > full_img.shape[1] or tx < 0 or ty < 0:
            continue
            
        crop_img = full_img[ty:ty+rh, tx:tx+rw]
        cells.append({
            "side": side, "line": line,
            "img": crop_img
        })
        with targets_lock:
            targets.append({"side": side, "line": line})

    with first_lock:
        first_maps[ball_key] = {
            "cells": cells,
            "ts": time.time(),
        }
    log(f"   🗺️  {len(cells)} cells mapped dynamically")

def _drop_first_map(ball_key):
    with first_lock:
        first_maps.pop(ball_key, None)
    with used_lock:
        used_points.pop(ball_key, None)

# ---------- ২য় পুশ (স্মার্ট স্ক্রল ও ক্লিক) ----------
def process_second_push(page, ball_key, run, over):
    with first_lock:
        fm = first_maps.get(ball_key)
    if not fm:
        log(f"⏳ [PUSH-2] ball={ball_key} — ১ম map নেই")
        return False

    side_want = "under" if run == 0 else "over"
    line_want = 0.5 if run == 0 else max([ln for ln in [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5] if run > ln], default=0.5)

    log(f"🎯 [TARGET] ball={ball_key} run={run} → {side_want} {line_want}")

    match_cell = None
    for c in fm["cells"]:
        if c["side"] == side_want and abs(c["line"] - line_want) < 0.01:
            match_cell = c
            break

    if not match_cell:
        log(f"❌ [NO-CLICK] ball={ball_key} — map-এ {side_want} {line_want} নেই")
        return False

    tpl_img = match_cell["img"]
    
    def try_match():
        raw = page.screenshot(type="png", scale="css")
        np_arr = np.frombuffer(raw, np.uint8)
        src_img = cv2.imdecode(np_arr, cv2.IMREAD_GRAYSCALE)
        if src_img is None: return None
        
        res = cv2.matchTemplate(src_img, tpl_img, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        
        if max_val >= COMPARE_THRESHOLD:
            h, w = tpl_img.shape[:2]
            return {"x": max_loc[0], "y": max_loc[1], "w": w, "h": h, "score": max_val}
        return None

    # ১. বর্তমান স্ক্রিনে খোঁজা
    match = try_match()
    
    # ২. না পেলে মার্কেট প্লেসের ভেতরে স্ক্রল করা (বাইরের পেজ নয়)
    if not match:
        log("   🔍 স্ক্রিনে পাইনি, মার্কেট প্লেসের ভেতরে স্ক্রল করছি...")
        page.mouse.move(MARKET_X, MARKET_Y) # মাউস মার্কেটের মাঝখানে নিয়ে যাওয়া
        
        page.mouse.wheel(0, -400) # উপরে
        page.wait_for_timeout(200)
        match = try_match()
        
        if not match:
            page.mouse.wheel(0, 800) # নিচে
            page.wait_for_timeout(200)
            match = try_match()
            
        if not match:
            page.mouse.wheel(0, 600) # আরও নিচে
            page.wait_for_timeout(200)
            match = try_match()

    if match:
        log(f"   🎯 Found at ({match['x']},{match['y']}) score={match['score']:.3f}")
        do_human_click(page, ball_key, match)
        _drop_first_map(ball_key)
        return True
    else:
        log(f"❌ [NO-CLICK] ball={ball_key} — স্ক্রিনে বাটনটি পাওয়া যায়নি (score fail)")
        return False

# ---------- Push parse ----------
def parse_push(payload):
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None
    ball = data.get("ball")
    if isinstance(ball, dict):
        raw = ball.get("ball_key")
        bkey = str(raw) if raw is not None else ""
        if ball.get("is_delete") is True:
            return {"kind": "delete", "ball_key": bkey, "data": data}
        detail = ball.get("detail")
        if isinstance(detail, dict):
            return {"kind": "second", "ball_key": bkey, "data": data, "detail": detail}
        return {"kind": "first", "ball_key": bkey, "data": data}
    return {"kind": "match", "data": data}

def ball_info(d):
    bt = str(d.get("ball_type") or "normal").lower()
    ts = d.get("team_score") or {}
    bw = d.get("bowler") or {}
    if ts.get("is_wicket") or bw.get("is_wicket") or d.get("wicket"):
        return ("WICKET", "wicket", 0)
    if re.search(r"no_?ball", bt):
        br = int((d.get("batsman") or {}).get("runs") or 0)
        return (f"NO BALL +{br}" if br > 0 else "NO BALL", "noball", 1 + br)
    bs = d.get("batsman") or {}
    if bs.get("is_six"):
        return ("6", "six", 6)
    if bs.get("is_four"):
        return ("4", "four", 4)
    if re.search(r"wide", bt):
        wr = int(ts.get("runs") or 1) - 1
        return (f"WIDE +{wr}" if wr > 0 else "WIDE", "wide", 1 + wr)
    r = ts.get("runs")
    if r is None:
        r = bs.get("runs") or 0
    r = int(r)
    return (str(r), "dot" if r == 0 else "run", r)

def get_over_delivery(d):
    """over এবং delivery ডাইনামিকভাবে বের করা"""
    if not isinstance(d, dict):
        return None, None
    ov = d.get("overs")
    if isinstance(ov, list) and len(ov) >= 2:
        try:
            # d['overs'] সাধারণত [16, 2] এরকম আসে (মানে ১৭তম ওভার, ৩য় বল)
            # তাই ১ বেসড করতে +1 করা হচ্ছে
            return int(ov[0]) + 1, int(ov[1]) + 1
        except (TypeError, ValueError):
            pass
    return None, None

def make_seen_lru(maxlen):
    od = OrderedDict()
    def remember(k):
        if k in od:
            od.move_to_end(k)
        else:
            od[k] = True
        while len(od) > maxlen:
            od.popitem(last=False)
    return remember, lambda k: k in od, lambda k: od.pop(k, None)

# ---------- Main ----------
def monitor_and_click():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("\n❌ Roanuz match_key দিন!\n")
        return

    match_filter = sys.argv[1].strip()
    site_url = input("\nSite link din: ").strip() or "https://example.com"
    
    if not setup_engines():
        return

    with sync_playwright() as p:
        launch_kwargs = dict(
            headless=HEADLESS,
            args=[
                f"--window-size={WIN_W},{WIN_H}",
                "--window-position=0,0",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
            ],
        )
        if CHROME_PATH:
            launch_kwargs["executable_path"] = CHROME_PATH
            
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            viewport={"width": VIEW_W, "height": VIEW_H},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36",
        )

        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.goto(site_url)
        
        print("\n=== ম্যানুয়ালি লগইন করুন ===")
        match_url = input("Match URL: ").strip()
        if not match_url:
            browser.close()
            return
            
        page.goto(match_url)
        log(f"✅ [SETUP] Match page: {page.url}")

        print("\n" + "="*50)
        print("👉 ব্রাউজারে গিয়ে ম্যানুয়ালি 'Quick Events' সিলেক্ট করুন।")
        print("👉 স্ক্রিন রেডি হলে টার্মিনালে 'done' লিখে Enter দিন।")
        print("="*50 + "\n")
        
        while True:
            ans = input("টার্মিনালে 'done' লিখুন: ").strip().lower()
            if ans == "done": break

        log("▶️ স্ক্রিপ্ট চালু হচ্ছে (Dynamic OCR Mode)...")

        while not event_q.empty():
            try:
                event_q.get_nowait()
            except queue.Empty:
                break

        start_ws_thread()
        log(f"👂 [WS] wait korchi... (match_key={match_filter})\n")

        remember_seen, was_seen, forget_seen = make_seen_lru(SEEN_SECOND_MAX)

        last_event = time.time()
        last_reap = time.time()

        try:
            while True:
                now = time.time()

                # 1) pending processing
                with pending_lock:
                    items = list(pending)
                for it in items:
                    with first_lock:
                        fm = first_maps.get(it["ball_key"])
                    if fm:
                        try:
                            process_second_push(page, it["ball_key"], it["run"], it["over"])
                        except Exception as e:
                            log(f"⚠️ pending error: {e}")
                            if page.is_closed():
                                raise KeyboardInterrupt
                        with pending_lock:
                            try:
                                pending.remove(it)
                            except ValueError:
                                pass
                        remember_seen(it["ball_key"])
                    elif now - it["t"] > PENDING_TIMEOUT_SEC:
                        log(f"⌛ [SKIP] ball={it['ball_key']} — ১ম map নেই")
                        _drop_first_map(it["ball_key"])
                        with pending_lock:
                            try:
                                pending.remove(it)
                            except ValueError:
                                pass

                # 2) periodic reaper
                if now - last_reap >= REAP_INTERVAL:
                    stale_keys = []
                    with first_lock:
                        for k, v in list(first_maps.items()):
                            if now - v.get("ts", now) > STALE_MAP_TTL:
                                stale_keys.append(k)
                    for k in stale_keys:
                        _drop_first_map(k)
                    if stale_keys:
                        log(f"🧹 [REAP] {len(stale_keys)} stale first_map cleaned")
                    last_reap = now

                # 3) WebSocket event
                try:
                    recv_ts, raw = event_q.get(timeout=0.5)
                except queue.Empty:
                    if time.time() - last_event >= HEARTBEAT_SEC:
                        log(f"⏳ [HEARTBEAT] WS={'ON' if ws_state['up'] else 'OFF'} | maps={len(first_maps)}")
                        last_event = time.time()
                    continue

                last_event = time.time()
                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue

                push = parse_push(payload)
                if not push:
                    continue

                data = push["data"]
                mkey = data.get("matchkey") or data.get("match_key") or data.get("key")

                if mkey and mkey != match_filter:
                    continue

                # FIRST PUSH
                if push["kind"] == "first":
                    bkey = push["ball_key"]
                    if not bkey or was_seen(bkey):
                        continue
                    with first_lock:
                        if first_maps.get(bkey):
                            _drop_first_map(bkey)
                    
                    # ⭐ ডাইনামিক ওভার এবং ডেলিভারি বের করা
                    over_num, del_num = get_over_delivery(data)
                    if over_num is None:
                        over_num, del_num = 17, 3 # ফলব্যাক

                    log(f"▶️  [PUSH-1] ball={bkey} (Over: {over_num}, Del: {del_num})")
                    try:
                        process_first_push(page, bkey, over_num, del_num)
                    except Exception as e:
                        log(f"⚠️ [PUSH-1] error: {e}")
                        if page.is_closed():
                            raise KeyboardInterrupt
                    continue

                # DELETE
                if push["kind"] == "delete":
                    bkey = push["ball_key"]
                    _drop_first_map(bkey)
                    forget_seen(bkey)
                    with pending_lock:
                        pending[:] = [x for x in pending if x["ball_key"] != bkey]
                    log(f"🗑️  [DELETE] ball={bkey}")
                    continue

                # SECOND PUSH
                if push["kind"] == "second":
                    bkey = push["ball_key"]
                    d = push["detail"]
                    if was_seen(bkey):
                        continue
                    remember_seen(bkey)

                    over_num, del_num = get_over_delivery(d)
                    if not over_num:
                        continue
                    
                    text, kind, run = ball_info(d)
                    log(f"🏏 [PUSH-2] ball={bkey} over={over_num}.{del_num} {text} run={run}")

                    with first_lock:
                        fm = first_maps.get(bkey)
                        
                    if fm:
                        try:
                            process_second_push(page, bkey, run, over_num)
                        except Exception as e:
                            log(f"⚠️ [PUSH-2] error: {e}")
                            if page.is_closed():
                                raise KeyboardInterrupt
                    else:
                        with pending_lock:
                            pending.append({
                                "t": recv_ts, "ball_key": bkey,
                                "over": over_num, "run": run,
                            })
                        log(f"⏸️  [PENDING] queued ball={bkey}")

                    continue

        except KeyboardInterrupt:
            print("\nবন্ধ করা হচ্ছে...")
        finally:
            stop_evt.set()
            try:
                browser.close()
            except Exception:
                pass

if __name__ == "__main__":
    monitor_and_click()
