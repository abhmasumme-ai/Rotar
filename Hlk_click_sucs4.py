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
VIEW_H = 797  
WIN_W = VIEW_W + 100
WIN_H = VIEW_H + 120

ANCHOR_TEMPLATE = {}
MAPPED_TARGETS = [] 
market_del_counter = 0

# ⭐ মেমোরি পজিশন (Fast-Path)
CALIBRATED_ANCHOR_POS = None 
last_anchor_pos = None 

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
    let baseY = 400; 
    let anchorW = 100;
    let anchorH = 34;
    let leftX = 70;
    let rightX = 425; 
    let boxW = 220;
    let boxH = 30;
    let rowGap = 3.34;
    
    const blueBox = document.createElement('div');
    blueBox.style.cssText = `position:absolute;left:${leftX}px;top:${baseY}px;width:${anchorW}px;height:${anchorH}px;` +
      `border:2px solid #00aaff;background:rgba(0,170,255,0.4);box-sizing:border-box;display:flex;align-items:center;justify-content:center;color:white;font-weight:bold;font-size:10px;`;
    blueBox.textContent = "Anchor";
    box.appendChild(blueBox);

    let firstRowY = baseY + anchorH;
    let finalTargets = [];

    for (let i = 0; i < rowCount; i++) {
        let y = firstRowY + i * (boxH + rowGap);
        const leftBox = document.createElement('div');
        leftBox.style.cssText = `position:absolute;left:${leftX}px;top:${y}px;width:${boxW}px;height:${boxH}px;border:2px solid red;background:transparent;box-sizing:border-box;`;
        box.appendChild(leftBox);
        const rightBox = document.createElement('div');
        rightBox.style.cssText = `position:absolute;left:${rightX}px;top:${y}px;width:${boxW}px;height:${boxH}px;border:2px solid red;background:transparent;box-sizing:border-box;`;
        box.appendChild(rightBox);
        finalTargets.push({side: 'over', x: leftX, y: y, w: boxW, h: boxH});
        finalTargets.push({side: 'under', x: rightX, y: y, w: boxW, h: boxH});
    }

    let btnTop = firstRowY + rowCount * (boxH + rowGap) + 15;
    const btnContainer = document.createElement('div');
    btnContainer.style.cssText = `position:absolute;left:${leftX}px;top:${btnTop}px;pointer-events:auto;display:flex;gap:10px;`;
    
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

def setup_from_done(page, final_targets):
    global market_del_counter, CALIBRATED_ANCHOR_POS, last_anchor_pos
    log("⚙️  স্ক্রিন থেকে নীল বক্স (Anchor) এবং লাল বক্সের লেখা সেভ করা হচ্ছে...")
    
    page.evaluate("() => { let b = document.getElementById('__pw_target_guide'); if(b) b.remove(); }")
    page.wait_for_timeout(300)

    raw = page.screenshot(type="png", scale="css")
    np_arr = np.frombuffer(raw, np.uint8)
    src_img = cv2.imdecode(np_arr, cv2.IMREAD_GRAYSCALE)

    anchor_img = src_img[400:434, 70:170]
    ANCHOR_TEMPLATE.update({"img": anchor_img, "w": 100, "h": 34})
    
    # ⭐ DONE দেওয়ার সময় অ্যাংকরের পজিশন সেভ করা হলো (Fast-Path)
    res = cv2.matchTemplate(src_img, anchor_img, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    if max_val >= COMPARE_THRESHOLD:
        CALIBRATED_ANCHOR_POS = max_loc
        last_anchor_pos = max_loc
    
    log("   ✅ Anchor (Blue Box) সফলভাবে সেভ হয়েছে।")

    MAPPED_TARGETS.clear()
    max_del = 0
    
    for t in final_targets:
        x, y, w, h = int(t['x']), int(t['y']), int(t['w']), int(t['h'])
        
        crop = src_img[y:y+h, x:x+w]
        crop_resized = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        
        config = '--psm 7 -c tessedit_char_whitelist=0123456789.ovrdeli'
        text = pytesseract.image_to_string(crop_resized, config=config).lower()
        
        m_del = re.search(r'(\d+)\s*-?\s*delivery', text, re.IGNORECASE)
        if m_del:
            d_num = int(m_del.group(1))
            if d_num > max_del: max_del = d_num
        
        matches = re.findall(r'(over|under)\s*(\d+\.?\d*)', text)
        if matches:
            side, line_str = matches[-1]
            line = float(line_str)
            MAPPED_TARGETS.append({"side": side, "line": line, "x": x, "y": y, "w": w, "h": h})
            log(f"      ✅ ম্যাপিং: {side.upper()} {line} সেভ হয়েছে।")

    market_del_counter = max_del
    log(f"   📊 স্ক্রিন থেকে বর্তমান ডেলিভারি সেট করা হলো: {market_del_counter}")

def decide_side_line(run, kind):
    if kind == 'wide':
        if run == 1: return ("over", 0.5)
        elif 2 <= run <= 4: return ("over", 1.5)
        elif run >= 5: return ("over", 3.5)
    elif kind == 'noball':
        if run == 1: return ("over", 0.5)
        elif 2 <= run <= 4: return ("over", 1.5)
        elif run >= 5: return ("over", 3.5)
    elif kind == 'wicket':
        if run == 0: return ("under", 0.5)
        elif 1 <= run <= 3: return ("over", 1.5)
        elif run >= 4: return ("over", 3.5)
    else:
        if run == 0: return ("under", 0.5)
        known_lines = [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5]
        best = max([ln for ln in known_lines if run > ln], default=0.5)
        return ("over", best)
    return ("over", 0.5)

def ocr_crop(src_img, x, y, w, h):
    crop = src_img[y:y+h, x:x+w]
    if crop.size == 0: return ""
    crop_resized = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    config = '--psm 7 -c tessedit_char_whitelist=0123456789.ovrdeli'
    return pytesseract.image_to_string(crop_resized, config=config).lower()

# ⭐ নতুন সুপার ফাস্ট find_anchor লজিক
def find_anchor(page):
    global CALIBRATED_ANCHOR_POS, last_anchor_pos
    
    raw = page.screenshot(type="png", scale="css")
    np_arr = np.frombuffer(raw, np.uint8)
    src_img = cv2.imdecode(np_arr, cv2.IMREAD_GRAYSCALE)
    
    # ১. Fast-Path: DONE দেওয়ার সময় যে জায়গায় ছিল, ঠিক সেখানে চেক করা
    if CALIBRATED_ANCHOR_POS:
        cx, cy = CALIBRATED_ANCHOR_POS
        if cx + ANCHOR_TEMPLATE["w"] < VIEW_W and cy + ANCHOR_TEMPLATE["h"] < VIEW_H:
            crop = src_img[cy:cy+ANCHOR_TEMPLATE["h"], cx:cx+ANCHOR_TEMPLATE["w"]]
            if crop.size > 0:
                res = cv2.matchTemplate(crop, ANCHOR_TEMPLATE["img"], cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(res)
                if max_val >= 0.90: 
                    return src_img, cx, cy

    # ২. Slow-Path: শেষবার যেখানে পেয়েছিলাম সেখানে চেক করা
    if last_anchor_pos:
        ax, ay = last_anchor_pos
        if ax + ANCHOR_TEMPLATE["w"] < VIEW_W and ay + ANCHOR_TEMPLATE["h"] < VIEW_H:
            crop = src_img[ay:ay+ANCHOR_TEMPLATE["h"], ax:ax+ANCHOR_TEMPLATE["w"]]
            if crop.size > 0:
                res = cv2.matchTemplate(crop, ANCHOR_TEMPLATE["img"], cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(res)
                if max_val >= 0.90:
                    CALIBRATED_ANCHOR_POS = (ax, ay) # নতুন পজিশন আপডেট
                    return src_img, ax, ay

    # ৩. পুরো স্ক্রিনে খোঁজা
    res = cv2.matchTemplate(src_img, ANCHOR_TEMPLATE["img"], cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    if max_val >= COMPARE_THRESHOLD:
        last_anchor_pos = max_loc
        CALIBRATED_ANCHOR_POS = max_loc
        return src_img, max_loc[0], max_loc[1]

    # ৪. না পেলে স্ক্রল করে খোঁজা
    page.evaluate(SCROLL_JS, -2000)
    page.wait_for_timeout(200)
    for _ in range(10):
        raw = page.screenshot(type="png", scale="css")
        np_arr = np.frombuffer(raw, np.uint8)
        src_img = cv2.imdecode(np_arr, cv2.IMREAD_GRAYSCALE)
        res = cv2.matchTemplate(src_img, ANCHOR_TEMPLATE["img"], cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val >= COMPARE_THRESHOLD:
            last_anchor_pos = max_loc
            CALIBRATED_ANCHOR_POS = max_loc
            return src_img, max_loc[0], max_loc[1]
        page.evaluate(SCROLL_JS, 300)
        page.wait_for_timeout(200)

    return None, None, None

def process_second_push(page, ball_key, run, kind, ws_over, market_del):
    side_want, line_want = decide_side_line(run, kind)
    log(f"🎯 [TARGET] ball={ball_key} run={run} ({kind}) → {side_want} {line_want} | WS: {ws_over}-{market_del}")

    for attempt in range(3):
        src_img, ax, ay = find_anchor(page)
        
        if src_img is None:
            log(f"❌ [NO-CLICK] ball={ball_key} — নীল বক্স (Anchor) পাওয়া যায়নি!")
            return False

        target_box = None
        for t in MAPPED_TARGETS:
            if t["side"] == side_want and abs(t["line"] - line_want) < 0.01:
                target_box = t
                break

        if not target_box:
            log(f"❌ [NO-CLICK] ball={ball_key} — ম্যাপিং-এ {side_want} {line_want} নেই!")
            return False

        rel_y = target_box["y"] - 434 
        actual_y = ay + rel_y
        actual_x = ax + (target_box["x"] - 70) 

        text = ocr_crop(src_img, actual_x, actual_y, target_box["w"], target_box["h"])
        clean_text = re.sub(r'[^a-z0-9]', '', text)
        
        if str(ws_over) in clean_text and str(market_del) in clean_text:
            center_x = actual_x + (target_box["w"] / 2)
            center_y = actual_y + (target_box["h"] / 2)
            
            log(f"   🎯 Verified! Clicking at ({center_x},{center_y})")
            try:
                page.mouse.move(center_x, center_y)
                page.mouse.down() 
                page.wait_for_timeout(random.randint(5, 10)) # ⭐ Human-like delay (5-10ms)
                page.mouse.up()
                log(f"✅ [CLICK] ball={ball_key}")
                return True
            except Exception as e:
                log(f"⚠️ [CLICK] error: {e}")
                return False
        else:
            if attempt < 2:
                log(f"   ⏳ [RETRY] WS: {ws_over}-{market_del}, Screen: {clean_text[:10]}... অপেক্ষা করছি")
                page.wait_for_timeout(100)
            else:
                log(f"   ⚠️ [OCR Mismatch] ৩ বার চেষ্টার পরেও মেলেনি। ক্লিক বাতিল।")
                return False

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
    
    if ts.get("is_wicket") or bw.get("is_wicket") or d.get("wicket"): 
        w_runs = int(ts.get("runs") or bs.get("runs") or 0)
        return ("WICKET", "wicket", w_runs)
        
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

def monitor_and_click():
    global market_del_counter, last_anchor_pos, CALIBRATED_ANCHOR_POS
    
    if len(sys.argv) < 2: return
    match_filter = sys.argv[1].strip()
    
    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

    site_url = input("\nSite link din: ").strip() or "https://example.com"

    proxy_config = None
    print("\n=== Proxy Settings (IPRoyal) ===")
    proxy_url = input("Proxy URL (e.g., http://user:pass@geo.iproyal.com:12321) [Leave blank to skip]: ").strip()

    if proxy_url:
        try:
            if "@" in proxy_url:
                proto, rest = proxy_url.split("://") if "://" in proxy_url else ("http://", proxy_url)
                auth, host_port = rest.split("@")
                user, pwd = auth.split(":")
                proxy_config = {"server": f"{proto}://{host_port}", "username": user, "password": pwd}
            else:
                proxy_config = {"server": proxy_url}
            log(f"✅ Proxy configured: {proxy_config['server']}")
        except Exception as e:
            log(f"❌ Proxy format error: {e}. Skipping proxy.")
            proxy_config = None
    else:
        log("⏭️ Proxy skipped. Running on local IP.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, args=[f"--window-size={WIN_W},{WIN_H}", "--window-position=0,0"])
        
        context = browser.new_context(
            viewport={"width": VIEW_W, "height": VIEW_H}, 
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            proxy=proxy_config 
        )
        
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
                    elif ws_over != last_ws_over:
                        last_ws_over = ws_over
                        market_del_counter = 1
                        last_anchor_pos = None 
                        CALIBRATED_ANCHOR_POS = None # ⭐ নতুন ওভার হলে মেমোরি রিসেট
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
