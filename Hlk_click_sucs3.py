import asyncio
import json
import os
import queue
import random
import re
import sys
import threading
import time
import io
from collections import OrderedDict
from pathlib import Path

import websockets
from playwright.sync_api import sync_playwright
import numpy as np
import cv2
import pytesseract
from PIL import Image

# ================= CONFIG =================
WS_URL = "ws://127.0.0.1:5000/ws"
HEARTBEAT_SEC = 10
RECONNECT_SEC = 3
PING_SEC = 30
HEADLESS = os.environ.get("CLICKER_HEADLESS") == "1"
CHROME_PATH = os.environ.get("CLICKER_CHROME_PATH") or None
TESSERACT_CMD = os.environ.get("TESSERACT_CMD") or (r"C:\Program Files\Tesseract-OCR\tesseract.exe" if os.path.exists(r"C:\Program Files\Tesseract-OCR\tesseract.exe") else None)

COMPARE_THRESHOLD = 0.70 
VIEW_W = 1080
VIEW_H = 920
WIN_W = VIEW_W + 100
WIN_H = VIEW_H + 120

# ⭐ DONE দেওয়ার পর স্ক্রিপ্ট এখানে ডেটা সেভ করবে
ANCHOR_TEMPLATE = {}
MAPPED_TARGETS = [] 

# ⭐ পরিষ্কার UI (Blue Anchor + Red Boxes)
SHOW_TARGETS_JS = """
() => {
    let box = document.getElementById('__pw_target_guide');
    if (!box) {
        box = document.createElement('div');
        box.id = '__pw_target_guide';
        box.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:999999;background:rgba(0,0,0,0.2);';
        document.body.appendChild(box);
    }
    box.innerHTML = '';
    
    let rowCount = window.__row_count || 3;
    let baseY = 450; 
    let anchorW = 420;
    let anchorH = 25;
    
    // ১. ব্লু বক্স (Anchor/Headline)
    const blueBox = document.createElement('div');
    blueBox.style.cssText = `position:absolute;left:70px;top:${baseY}px;width:${anchorW}px;height:${anchorH}px;` +
      `border:2px solid #00aaff;background:rgba(0,170,255,0.3);box-sizing:border-box;` +
      `display:flex;align-items:center;justify-content:center;color:white;font-weight:bold;font-size:12px;`;
    blueBox.textContent = "HEADLINE (Anchor)";
    box.appendChild(blueBox);

    // ২. রেড বক্স (Targets)
    let firstRowY = baseY + anchorH; 
    let rowGap = 4;
    let rowHeight = 30;
    let finalTargets = [];

    for (let i = 0; i < rowCount; i++) {
        let y = firstRowY + i * (rowHeight + rowGap);

        // Left (Over)
        const leftBox = document.createElement('div');
        leftBox.style.cssText = `position:absolute;left:70px;top:${y}px;width:${anchorW}px;height:${rowHeight}px;border:2px solid red;background:transparent;box-sizing:border-box;`;
        box.appendChild(leftBox);

        // Right (Under)
        const rightBox = document.createElement('div');
        rightBox.style.cssText = `position:absolute;left:510px;top:${y}px;width:${anchorW}px;height:${rowHeight}px;border:2px solid red;background:transparent;box-sizing:border-box;`;
        box.appendChild(rightBox);

        finalTargets.push({side: 'over', x: 70, y: y, w: anchorW, h: rowHeight});
        finalTargets.push({side: 'under', x: 510, y: y, w: anchorW, h: rowHeight});
    }

    // ৩. বাটন সেকশন
    let btnTop = firstRowY + rowCount * (rowHeight + rowGap) + 10;
    const btnContainer = document.createElement('div');
    btnContainer.style.cssText = `position:absolute;left:70px;top:${btnTop}px;pointer-events:auto;display:flex;gap:10px;`;
    
    const doneBtn = document.createElement('button');
    doneBtn.textContent = 'DONE';
    doneBtn.style.cssText = 'padding:10px 30px;font-size:16px;font-weight:bold;background:#00c853;color:white;border:none;border-radius:6px;cursor:pointer;';
    doneBtn.onclick = () => { window.__pw_final_targets = finalTargets; window.__pw_action = 'done'; };

    const plusBtn = document.createElement('button');
    plusBtn.textContent = '+ 3 Rows';
    plusBtn.style.cssText = 'padding:10px 20px;font-size:16px;font-weight:bold;background:#2196f3;color:white;border:none;border-radius:6px;cursor:pointer;';
    plusBtn.onclick = () => { window.__row_count = Math.min(9, rowCount + 3); window.__pw_action = 'update'; };

    const minusBtn = document.createElement('button');
    minusBtn.textContent = '- 3 Rows';
    minusBtn.style.cssText = 'padding:10px 20px;font-size:16px;font-weight:bold;background:#ff9800;color:white;border:none;border-radius:6px;cursor:pointer;';
    minusBtn.onclick = () => { window.__row_count = Math.max(3, rowCount - 3); window.__pw_action = 'update'; };

    const resetBtn = document.createElement('button');
    resetBtn.textContent = 'RESET';
    resetBtn.style.cssText = 'padding:10px 20px;font-size:16px;font-weight:bold;background:#ff3d00;color:white;border:none;border-radius:6px;cursor:pointer;';
    resetBtn.onclick = () => { window.__pw_action = 'reset'; };

    btnContainer.appendChild(doneBtn);
    btnContainer.appendChild(plusBtn);
    btnContainer.appendChild(minusBtn);
    btnContainer.appendChild(resetBtn);
    box.appendChild(btnContainer);
}
"""

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

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ================= DONE ক্লিকের পর সেটআপ =================
def setup_from_done(page, final_targets):
    log("⚙️  স্ক্রিন থেকে নীল বক্স (Anchor) এবং লাল বক্সের লেখা সেভ করা হচ্ছে...")
    
    # UI মুছে ফেলা
    page.evaluate("() => { let b = document.getElementById('__pw_target_guide'); if(b) b.remove(); }")
    page.wait_for_timeout(300)

    # স্ক্রিনশট নেওয়া (UI ছাড়া)
    raw = page.screenshot(type="png", scale="css")
    np_arr = np.frombuffer(raw, np.uint8)
    src_img = cv2.imdecode(np_arr, cv2.IMREAD_GRAYSCALE)

    # ১. নীল বক্স (Anchor) সেভ করা (UI-তে Blue box ছিল y=450, x=70, w=420, h=25)
    anchor_img = src_img[450:475, 70:490]
    ANCHOR_TEMPLATE.update({"img": anchor_img, "w": 420, "h": 25})
    log("   ✅ Anchor (Blue Box) সফলভাবে সেভ হয়েছে।")

    # ২. লাল বক্সগুলোর লেখা পড়া
    MAPPED_TARGETS.clear()
    for t in final_targets:
        x, y, w, h = t['x'], t['y'], t['w'], t['h']
        
        # ওই নির্দিষ্ট লাল বক্সের জায়গার ছবি কাটা
        crop = src_img[y:y+h, x:x+w]
        crop_resized = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        
        # OCR চালানো
        text = pytesseract.image_to_string(crop_resized, config='--psm 7').lower()
        
        # লেখা থেকে লাইনটি আলাদা করা (যেমন: over 0.5 বা under 1.5)
        # যেহেতু লেখায় "24 - Over, 0 - Delivery, Over 0.5" থাকে, তাই রেজেক্স দিয়ে শেষেরটা নিচ্ছি
        matches = re.findall(r'(over|under)\s*(\d+\.?\d*)', text)
        if matches:
            # সাধারণত শেষের ম্যাচটাই আসল লাইন হয় (Over 0.5)
            side, line_str = matches[-1]
            line = float(line_str)
            MAPPED_TARGETS.append({
                "side": side,
                "line": line,
                "x": x, "y": y, "w": w, "h": h
            })
            log(f"      ✅ ম্যাপিং: {side.upper()} {line} সফলভাবে সেভ হয়েছে।")
        else:
            log(f"      ⚠️ OCR পড়তে পারেনি। লেখা ছিল: '{text.strip()[:30]}'")

# ================= লাইভ ভেরিফিকেশন =================
def verify_over_delivery(img_crop, ws_over, market_del):
    try:
        img_crop = cv2.resize(img_crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        text = pytesseract.image_to_string(img_crop, config='--psm 7').lower()
        clean_text = re.sub(r'[^a-z0-9]', '', text)
        
        # ওভার এবং ডেলিভারি নাম্বার ম্যাচিং
        if str(ws_over) in clean_text and str(market_del) in clean_text:
            return True
        return False
    except Exception:
        return False

def process_second_push(page, ball_key, run, kind, ws_over, market_del):
    side_want = "under" if run == 0 else "over"
    
    if kind == 'wide':
        if run == 1: line_want = 0.5
        elif run >= 3: line_want = 3.5
        else: line_want = 0.5
    elif kind == 'noball':
        if run == 1: line_want = 0.5
        elif run >= 5: line_want = 3.5
        elif run >= 3: line_want = 1.5
        else: line_want = 0.5
    else:
        if run == 0: line_want = 0.5
        else: line_want = max([ln for ln in [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5] if run > ln], default=0.5)

    log(f"🎯 [TARGET] ball={ball_key} run={run} ({kind}) → {side_want} {line_want} | WS: {ws_over}-{market_del}")

    # ১. নীল বক্স (Anchor) খুঁজে আনা
    page.evaluate(SCROLL_JS, -2000)
    page.wait_for_timeout(300)
    
    anchor_found = False
    ax, ay = 0, 0
    
    for _ in range(10):
        raw = page.screenshot(type="png", scale="css")
        np_arr = np.frombuffer(raw, np.uint8)
        src_img = cv2.imdecode(np_arr, cv2.IMREAD_GRAYSCALE)
        
        res = cv2.matchTemplate(src_img, ANCHOR_TEMPLATE["img"], cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        
        if max_val >= COMPARE_THRESHOLD:
            ax, ay = max_loc
            anchor_found = True
            break
            
        page.evaluate(SCROLL_JS, 300)
        page.wait_for_timeout(200)

    if not anchor_found:
        log(f"❌ [NO-CLICK] ball={ball_key} — নীল বক্স (Anchor) পাওয়া যায়নি!")
        return False

    # ২. সঠিক লাল বক্সটি খোঁজা
    target_box = None
    for t in MAPPED_TARGETS:
        if t["side"] == side_want and abs(t["line"] - line_want) < 0.01:
            target_box = t
            break

    if not target_box:
        log(f"❌ [NO-CLICK] ball={ball_key} — ম্যাপিং-এ {side_want} {line_want} নেই!")
        return False

    # বক্সের বর্তমান পজিশন বের করা (Anchor এর সাপেক্ষে)
    rel_y = target_box["y"] - 475 # 475 হলো UI তে প্রথম লাল বক্সের y
    actual_y = ay + rel_y
    actual_x = ax + (target_box["x"] - 70) # 70 হলো UI তে নীল বক্সের x

    # ৩. ভেরিফিকেশন (লেখা মিলছে কিনা)
    crop = src_img[actual_y:actual_y+target_box["h"], actual_x:actual_x+target_box["w"]]
    
    if verify_over_delivery(crop, ws_over, market_del):
        center_x = actual_x + (target_box["w"] / 2)
        center_y = actual_y + (target_box["h"] / 2)
        
        log(f"   🎯 Verified! Clicking at ({center_x},{center_y})")
        try:
            page.mouse.move(center_x, center_y)
            page.wait_for_timeout(random.randint(20, 50))
            page.mouse.down()
            page.wait_for_timeout(random.randint(15, 40))
            page.mouse.up()
            log(f"✅ [CLICK] ball={ball_key}")
        except Exception as e:
            log(f"⚠️ [CLICK] error: {e}")
        return True
    else:
        log(f"   ⚠️ [OCR Mismatch] বাটন পাওয়া গেছে কিন্তু নাম্বার মেলেনি।")
        return False

# ================= WebSocket & Parsers =================
async def ws_loop():
    while True:
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
        except Exception:
            pass
        ws_state["up"] = False
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

def get_over_delivery(d):
    if not isinstance(d, dict):
        return None, None
    ov = d.get("overs")
    if isinstance(ov, list) and len(ov) >= 2:
        try:
            return int(ov[0]) + 1, int(ov[1]) + 1
        except: pass
    return None, None

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
    
    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

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

        print("\n" + "="*65)
        print("👉 ব্রাউজারে গিয়ে ম্যানুয়ালি 'Quick Events' সিলেক্ট করুন।")
        print("👉 নীল বক্সের নিচে লাল বক্সগুলো এডজাস্ট করুন।")
        print("👉 সব ঠিক থাকলে 'DONE' বাটনে ক্লিক করুন।")
        print("="*65 + "\n")

        while True:
            page.evaluate("() => { window.__pw_action = null; }")
            page.evaluate(SHOW_TARGETS_JS)
            
            print("⏳ ব্রাউজারে আপনার টার্গেট সেট করুন (DONE / + / - / RESET)...")
            while True:
                action = page.evaluate("() => window.__pw_action")
                if action in ('done', 'update', 'reset'):
                    break
                page.wait_for_timeout(200)

            if action == 'done':
                final_targets = page.evaluate("() => window.__pw_final_targets")
                
                # ⭐ DONE দেওয়ার সাথে সাথে অটো-রিডিং হবে
                setup_from_done(page, final_targets)
                break
            elif action == 'reset':
                page.evaluate("() => { window.__row_count = 3; }")
                continue
            elif action == 'update':
                continue

        if not MAPPED_TARGETS:
            log("⚠️ কোনো টার্গেট ম্যাপিং করা যায়নি — স্ক্রিপ্ট বন্ধ।")
            browser.close(); return

        start_ws_thread()
        log(f"👂 [WS] wait korchi... (match_key={match_filter})\n")

        remember_seen, was_seen, forget_seen = make_seen_lru(300)
        
        last_ws_over = None
        market_del_counter = 0

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
                    
                    ws_over, ws_del_legal = get_over_delivery(d)
                    if not ws_over: continue
                    
                    if last_ws_over is None:
                        last_ws_over = ws_over
                        market_del_counter = ws_del_legal
                    elif ws_over != last_ws_over:
                        last_ws_over = ws_over
                        market_del_counter = 1
                    else:
                        market_del_counter += 1
                    
                    text, kind, run = ball_info(d)
                    log(f"🏏 [PUSH-2] ball={bkey} over={ws_over}.{market_del_counter} {text} run={run}")
                    
                    process_second_push(page, bkey, run, kind, ws_over, market_del_counter)
                    continue

        except KeyboardInterrupt:
            print("\nবন্ধ করা হচ্ছে...")
        finally:
            try: browser.close()
            except Exception: pass

if __name__ == "__main__":
    monitor_and_click()
