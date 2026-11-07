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
import cv2
from PIL import Image

# ================= CONFIG =================
WS_URL = "ws://127.0.0.1:5000/ws"
HEARTBEAT_SEC = 10
RECONNECT_SEC = 3
PING_SEC = 30
HEADLESS = os.environ.get("CLICKER_HEADLESS") == "1"
CHROME_PATH = os.environ.get("CLICKER_CHROME_PATH") or None

PENDING_TIMEOUT_SEC = 10.0     
STALE_MAP_TTL = 30.0           
REAP_INTERVAL = 5.0            
SEEN_SECOND_MAX = 300          
COMPARE_THRESHOLD = 0.75  # ছবি ম্যাচিংয়ের জন্য থ্রেশহোল্ড

VIEW_W = 1080
VIEW_H = 920
WIN_W = VIEW_W + 100
WIN_H = VIEW_H + 120

# ================= INBUILD TARGET MAPPING (আপনার দেওয়া মাপ) =================
# (side, line, X, Y, Width, Height)
TARGET_LAYOUTS = [
    ("over", 0.5, 7,   46, 435, 40),
    ("under", 0.5, 446, 46, 440, 40),
    ("over", 1.5, 7,   89, 435, 40),
    ("under", 1.5, 446, 89, 440, 40),
    ("over", 3.5, 7,   131, 435, 40),
    ("under", 3.5, 446, 131, 440, 40),
]
# ========================================================================

TEMPLATES = {} # মেমোরিতে বাটনগুলোর ছবি জমা রাখার ডিকশনারি

# ⭐ স্ক্রিনে লাল বক্স (Visual Guide) দেখানোর জন্য
SHOW_TARGETS_JS = """
(targets) => {
    let box = document.getElementById('__pw_target_guide');
    if (!box) {
        box = document.createElement('div');
        box.id = '__pw_target_guide';
        box.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:999999;';
        document.body.appendChild(box);
    }
    box.innerHTML = '';
    targets.forEach((t) => {
        const d = document.createElement('div');
        d.style.cssText = `position:absolute;left:${t.x}px;top:${t.y}px;` +
          `width:${t.w}px;height:${t.h}px;border:3px solid red;` +
          `background:rgba(255,0,0,0.2);box-sizing:border-box;font:bold 16px sans-serif;color:white;` +
          `display:flex;align-items:center;justify-content:center;text-shadow: 1px 1px 2px black;`;
        d.textContent = `${t.side.toUpperCase()} ${t.line}`;
        box.appendChild(d);
    });
}
"""

# ⭐ মার্কেট প্লেসের ভেতরে স্ক্রল করার জন্য (বাইরের পেজ নয়)
SCROLL_JS = """
(dy) => {
    let x = 400, y = 500;
    let el = document.elementFromPoint(x, y);
    while (el) {
        let style = window.getComputedStyle(el);
        if ((style.overflowY === 'auto' || style.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) {
            el.scrollBy(0, dy);
            return true;
        }
        el = el.parentElement;
    }
    return false;
}
"""

event_q = queue.Queue()
ws_state = {"up": False}

used_points = {}
used_lock = threading.Lock()

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def safe_remove(path):
    try:
        if path and os.path.exists(path): os.remove(path)
    except Exception: pass

# ================= ১. স্বয়ংক্রিয়ভাবে ছবি কেটে নেওয়া =================
def auto_capture_templates(page):
    log("📸 আপনার দেওয়া ফর্ম (Layout) অনুযায়ী বাটনগুলোর ছবি কেটে নেওয়া হচ্ছে...")
    
    # স্ক্রিনশট নেওয়া
    img_bytes = page.screenshot(type="png", scale="css")
    np_arr = np.frombuffer(img_bytes, np.uint8)
    full_img = cv2.imdecode(np_arr, cv2.IMREAD_GRAYSCALE)
    
    TEMPLATES.clear()
    
    for side, line, rx, ry, rw, rh in TARGET_LAYOUTS:
        # স্ক্রিনের বাইরে গেলে স্কিপ
        if ry + rh > full_img.shape[0] or rx + rw > full_img.shape[1] or rx < 0 or ry < 0:
            continue
            
        crop_img = full_img[ry:ry+rh, rx:rx+rw]
        TEMPLATES[(side, line)] = {"img": crop_img, "w": rw, "h": rh}
        log(f"   ✅ ক্যাপচারড: {side.upper()} {line} (Size: {rw}x{rh})")

    log(f"✅ মোট {len(TEMPLATES)}টি টার্গেট মেমোরিতে সেভ হয়েছে।")

# ================= ২. স্মার্ট স্ক্রল এবং ক্লিক =================
def process_second_push(page, ball_key, run, over):
    side_want = "under" if run == 0 else "over"
    line_want = 0.5 if run == 0 else max([ln for ln in [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5] if run > ln], default=0.5)

    log(f"🎯 [TARGET] ball={ball_key} run={run} → {side_want} {line_want}")

    tpl_data = TEMPLATES.get((side_want, line_want))
    if not tpl_data:
        log(f"❌ [NO-CLICK] {side_want} {line_want} এর জন্য কোনো ছবি নেই!")
        return False

    tpl_img = tpl_data["img"]
    
    def try_match():
        raw = page.screenshot(type="png", scale="css")
        np_arr = np.frombuffer(raw, np.uint8)
        src_img = cv2.imdecode(np_arr, cv2.IMREAD_GRAYSCALE)
        if src_img is None: return None
        
        res = cv2.matchTemplate(src_img, tpl_img, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        
        if max_val >= COMPARE_THRESHOLD:
            return {"x": max_loc[0], "y": max_loc[1], "w": tpl_data["w"], "h": tpl_data["h"], "score": max_val}
        return None

    # ১. বর্তমান স্ক্রিনে খোঁজা
    match = try_match()
    
    # ২. না পেলে ভেতরের মার্কেট প্লেসে স্ক্রল করা
    if not match:
        log("   🔍 স্ক্রিনে পাইনি, মার্কেট প্লেসের ভেতরে স্ক্রল করছি...")
        
        page.evaluate(SCROLL_JS, -300) # উপরে
        page.wait_for_timeout(200)
        match = try_match()
        
        if not match:
            page.evaluate(SCROLL_JS, 600) # নিচে
            page.wait_for_timeout(200)
            match = try_match()

    if match:
        log(f"   🎯 Found at ({match['x']},{match['y']}) score={match['score']:.3f}")
        
        px = match['x'] + (match['w'] / 2)
        py = match['y'] + (match['h'] / 2)
        
        try:
            page.mouse.move(px, py)
            page.wait_for_timeout(random.randint(20, 50))
            page.mouse.down()
            page.wait_for_timeout(random.randint(15, 40))
            page.mouse.up()
            log(f"✅ [CLICK] ball={ball_key} at ({px:.0f},{py:.0f})")
        except Exception as e:
            log(f"⚠️ [CLICK] error: {e}")
            
        return True
    else:
        log(f"❌ [NO-CLICK] ball={ball_key} — স্ক্রিনে বাটনটি পাওয়া যায়নি।")
        return False

# ================= WebSocket & Parsers =================
async def ws_loop():
    while True:
        err = None
        try:
            async with websockets.connect(WS_URL, ping_interval=None, compression=None, max_size=None) as ws:
                ws_state["up"] = True
                log(f"🔌 [WS] connected: {WS_URL}")
                async def pinger():
                    while True:
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
        log(f"🔄 [WS] {RECONNECT_SEC}s pore reconnect...")
        await asyncio.sleep(RECONNECT_SEC)

def start_ws_thread():
    threading.Thread(target=lambda: asyncio.run(ws_loop()), daemon=True).start()

def parse_push(payload):
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict): return None
    ball = data.get("ball")
    if isinstance(ball, dict):
        raw = ball.get("ball_key")
        bkey = str(raw) if raw is not None else ""
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

# ================= Main =================
def monitor_and_click():
    if len(sys.argv) < 2: return
    match_filter = sys.argv[1].strip()
    
    site_url = input("\nSite link din: ").strip() or "https://example.com"

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

        # ⭐ লাল বক্স দেখানো (Visual Guide)
        overlay_data = [{"side": side, "line": line, "x": x, "y": y, "w": w, "h": h} for side, line, x, y, w, h in TARGET_LAYOUTS]
        page.evaluate(SHOW_TARGETS_JS, overlay_data)

        print("\n" + "="*65)
        print("👉 ব্রাউজারে গিয়ে ম্যানুয়ালি 'Quick Events' সিলেক্ট করুন।")
        print("👉 মাউস হুইল দিয়ে স্ক্রল করুন এবং আসল বাটনগুলোকে লাল বক্সের ভেতরে আনুন।")
        print("👉 স্ক্রিন রেডি হলে টার্মিনালে 'done' লিখে Enter দিন।")
        print("="*65 + "\n")
        
        while True:
            ans = input("টার্মিনালে 'done' লিখুন: ").strip().lower()
            if ans == "done": break

        # লাল বক্স মুছে ফেলা
        page.evaluate("() => { let b = document.getElementById('__pw_target_guide'); if(b) b.remove(); }")

        # ⭐ কোড নিজেই ছবি কেটে নেবে
        auto_capture_templates(page)

        if not TEMPLATES:
            log("⚠️ কোনো টার্গেট ক্যাপচার হয়নি — স্ক্রিপ্ট বন্ধ।")
            browser.close()
            return

        start_ws_thread()
        log(f"👂 [WS] wait korchi... (match_key={match_filter})\n")

        remember_seen, was_seen, forget_seen = make_seen_lru(SEEN_SECOND_MAX)

        try:
            while True:
                try:
                    recv_ts, raw = event_q.get(timeout=0.5)
                except queue.Empty:
                    continue

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
                    
                    over = compute_target(d)
                    if not over: continue
                    
                    text, kind, run = ball_info(d)
                    log(f"🏏 [PUSH-2] ball={bkey} over={over} {text} run={run}")
                    
                    process_second_push(page, bkey, run, over)
                    continue

        except KeyboardInterrupt:
            print("\nবন্ধ করা হচ্ছে...")
        finally:
            try: browser.close()
            except Exception: pass

if __name__ == "__main__":
    monitor_and_click()
