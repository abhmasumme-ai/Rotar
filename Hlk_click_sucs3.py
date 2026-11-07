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

TARGET_LAYOUTS = []
TEMPLATES = {} 

SHOW_TARGETS_JS = """
() => {
    let box = document.getElementById('__pw_target_guide');
    if (!box) {
        box = document.createElement('div');
        box.id = '__pw_target_guide';
        box.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:999999;background:rgba(0,0,0,0.4);';
        document.body.appendChild(box);
    }
    box.innerHTML = '';
    
    let rowCount = window.__row_count || 3;
    
    const blueBox = document.createElement('div');
    blueBox.style.cssText = `position:absolute;left:168px;top:150px;width:221px;height:32px;` +
      `border:2px solid blue;background:rgba(0,0,255,0.3);box-sizing:border-box;` +
      `display:flex;align-items:center;justify-content:center;color:white;font-weight:bold;font-size:12px;`;
    blueBox.textContent = "HEADLINE (Anchor)";
    box.appendChild(blueBox);

    let baseY = 192;
    let finalTargets = [];

    for (let i = 0; i < rowCount; i++) {
        let y = baseY + i * 37;

        const leftBox = document.createElement('div');
        leftBox.style.cssText = `position:absolute;left:168px;top:${y}px;width:304px;height:32px;border:2px solid red;background:rgba(255,0,0,0.1);box-sizing:border-box;display:flex;`;
        
        const yBoxL = document.createElement('div');
        yBoxL.style.cssText = `width:152px;height:100%;background:rgba(255,255,0,0.3);display:flex;align-items:center;justify-content:center;font-size:11px;color:#fff;font-weight:bold;`;
        yBoxL.textContent = `Over`;
        
        const cBoxL = document.createElement('div');
        cBoxL.style.cssText = `width:152px;height:100%;background:rgba(0,255,255,0.3);display:flex;align-items:center;justify-content:center;font-size:11px;color:#fff;font-weight:bold;`;
        cBoxL.textContent = `Del`;
        
        leftBox.appendChild(yBoxL);
        leftBox.appendChild(cBoxL);
        box.appendChild(leftBox);

        const rightBox = document.createElement('div');
        rightBox.style.cssText = `position:absolute;left:482px;top:${y}px;width:304px;height:32px;border:2px solid red;background:rgba(255,0,0,0.1);box-sizing:border-box;display:flex;`;
        
        const yBoxR = document.createElement('div');
        yBoxR.style.cssText = `width:152px;height:100%;background:rgba(255,255,0,0.3);display:flex;align-items:center;justify-content:center;font-size:11px;color:#fff;font-weight:bold;`;
        yBoxR.textContent = `Over`;
        
        const cBoxR = document.createElement('div');
        cBoxR.style.cssText = `width:152px;height:100%;background:rgba(0,255,255,0.3);display:flex;align-items:center;justify-content:center;font-size:11px;color:#fff;font-weight:bold;`;
        cBoxR.textContent = `Del`;
        
        rightBox.appendChild(yBoxR);
        rightBox.appendChild(cBoxR);
        box.appendChild(rightBox);

        finalTargets.push({side: 'over', x: 168, y: y, w: 304, h: 32});
        finalTargets.push({side: 'under', x: 482, y: y, w: 304, h: 32});
    }

    let btnTop = baseY + rowCount * 37 + 15;
    const btnContainer = document.createElement('div');
    btnContainer.style.cssText = `position:absolute;left:168px;top:${btnTop}px;pointer-events:auto;display:flex;gap:10px;`;
    
    const doneBtn = document.createElement('button');
    doneBtn.textContent = 'DONE';
    doneBtn.style.cssText = 'padding:10px 30px;font-size:16px;font-weight:bold;background:#00c853;color:white;border:none;border-radius:6px;cursor:pointer;';
    doneBtn.onclick = () => {
        window.__pw_final_targets = finalTargets;
        window.__pw_action = 'done';
    };

    const plusBtn = document.createElement('button');
    plusBtn.textContent = '+ 3 Rows';
    plusBtn.style.cssText = 'padding:10px 20px;font-size:16px;font-weight:bold;background:#2196f3;color:white;border:none;border-radius:6px;cursor:pointer;';
    plusBtn.onclick = () => {
        window.__row_count = Math.min(9, rowCount + 3);
        window.__pw_action = 'update';
    };

    const minusBtn = document.createElement('button');
    minusBtn.textContent = '- 3 Rows';
    minusBtn.style.cssText = 'padding:10px 20px;font-size:16px;font-weight:bold;background:#ff9800;color:white;border:none;border-radius:6px;cursor:pointer;';
    minusBtn.onclick = () => {
        window.__row_count = Math.max(3, rowCount - 3);
        window.__pw_action = 'update';
    };

    const resetBtn = document.createElement('button');
    resetBtn.textContent = 'RESET';
    resetBtn.style.cssText = 'padding:10px 20px;font-size:16px;font-weight:bold;background:#ff3d00;color:white;border:none;border-radius:6px;cursor:pointer;';
    resetBtn.onclick = () => {
        window.__pw_action = 'reset';
    };

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

def auto_capture_templates(page):
    log("📸 আপনার সেট করা লাল বক্সগুলো থেকে বাটনের ছবি কেটে নেওয়া হচ্ছে...")
    img_bytes = page.screenshot(type="png", scale="css")
    np_arr = np.frombuffer(img_bytes, np.uint8)
    full_img = cv2.imdecode(np_arr, cv2.IMREAD_GRAYSCALE)
    
    TEMPLATES.clear()
    for side, line, rx, ry, rw, rh in TARGET_LAYOUTS:
        if ry + rh > full_img.shape[0] or rx + rw > full_img.shape[1] or rx < 0 or ry < 0:
            continue
        crop_img = full_img[ry:ry+rh, rx:rx+rw]
        TEMPLATES[(side, line)] = {"img": crop_img, "w": rw, "h": rh}
        log(f"   ✅ ক্যাপচারড: {side.upper()} {line} (Y: {ry})")

# ================= OCR Verification =================
def verify_over_delivery(img_crop, ws_over, market_del):
    try:
        img_crop = cv2.resize(img_crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        text = pytesseract.image_to_string(img_crop, config='--psm 7').lower()
        
        m = re.search(r'(\d+)\s*-?\s*over.*?(\d+)\s*-?\s*delivery', text, re.IGNORECASE)
        
        if m:
            screen_over = int(m.group(1))
            screen_del = int(m.group(2))
            
            if screen_over == int(ws_over) and screen_del == int(market_del):
                return True
            else:
                log(f"   ⚠️ [OCR Mismatch] Screen: {screen_over}-{screen_del}, Expected: {ws_over}-{market_del}")
        else:
            clean_text = re.sub(r'[^a-z0-9]', '', text)
            if str(ws_over) in clean_text and str(market_del) in clean_text:
                return True
            log(f"   ⚠️ [OCR Parse Error] Text: {text.strip()[:30]}...")
            
        return False
    except Exception as e:
        log(f"⚠️ [OCR] error: {e}")
        return False

# ================= Smart Line Logic =================
def decide_side_line(run, kind=None):
    # ⭐ Wide এবং No Ball এর জন্য আপনার দেওয়া কাস্টম লজিক
    if kind == 'wide':
        if run == 1: return ("over", 0.5)
        elif run >= 3: return ("over", 3.5)
    elif kind == 'noball':
        if run == 1: return ("over", 0.5)
        elif run >= 5: return ("over", 3.5)
        elif run >= 3: return ("over", 1.5)
        
    # অন্যান্য বলের জন্য স্ট্যান্ডার্ড লজিক
    if run == 0:
        return ("under", 0.5)
    known_lines = [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5]
    best = max([ln for ln in known_lines if run > ln], default=0.5)
    return ("over", best)

# ================= স্মার্ট স্ক্রল এবং ক্লিক =================
def process_second_push(page, ball_key, run, kind, ws_over, market_del):
    side_want, line_want = decide_side_line(run, kind)
    
    available_lines = [ln for (s, ln) in TEMPLATES.keys() if s == side_want]
    if not available_lines:
        log(f"❌ [NO-CLICK] {side_want} এর জন্য কোনো টার্গেট নেই!")
        return False

    if (side_want, line_want) not in TEMPLATES:
        line_want = min(available_lines, key=lambda x: abs(x - line_want))

    log(f"🎯 [TARGET] ball={ball_key} run={run} ({kind}) → {side_want} {line_want} | Expected: {ws_over}-{market_del}")

    tpl_data = TEMPLATES.get((side_want, line_want))
    
    def try_match_and_verify():
        raw = page.screenshot(type="png", scale="css")
        np_arr = np.frombuffer(raw, np.uint8)
        src_img = cv2.imdecode(np_arr, cv2.IMREAD_GRAYSCALE)
        if src_img is None: return None
        
        res = cv2.matchTemplate(src_img, tpl_data["img"], cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        
        if max_val >= COMPARE_THRESHOLD:
            px, py = max_loc
            w, h = tpl_data["w"], tpl_data["h"]
            crop_img = src_img[py:py+h, px:px+w]
            
            if verify_over_delivery(crop_img, ws_over, market_del):
                return {"x": px, "y": py, "w": w, "h": h, "score": max_val}
            else:
                return None
        return None

    match = try_match_and_verify()
    
    if not match:
        log("   🔍 স্ক্রিনে পাইনি বা নাম্বার মিলছে না, স্ক্রল করছি...")
        page.evaluate(SCROLL_JS, -300)
        page.wait_for_timeout(200)
        match = try_match_and_verify()
        
        if not match:
            page.evaluate(SCROLL_JS, 600)
            page.wait_for_timeout(200)
            match = try_match_and_verify()

    if match:
        log(f"   🎯 Verified & Found at ({match['x']},{match['y']}) score={match['score']:.3f}")
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
        log(f"❌ [NO-CLICK] ball={ball_key} — সঠিক বাটনটি পাওয়া যায়নি বা নাম্বার মেলেনি।")
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
        print("👉 ব্লু বক্সের নিচে থাকা লাল বক্সগুলোতে আসল বাটনগুলো এডজাস্ট করুন।")
        print("👉 প্রয়োজন হলে + বা - ক্লিক করে রো কমবেশি করুন।")
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
                rows = len(final_targets) // 2
                
                print(f"\n👉 আপনি {rows}টি রো সিলেক্ট করেছেন।")
                lines_input = input(f"👉 এই {rows}টি রো-এর লাইন নাম্বারগুলো কমা দিয়ে লিখুন (যেমন: 0.5, 1.5, 3.5): ").strip()
                try:
                    lines_list = [float(x.strip()) for x in lines_input.split(",")]
                    if len(lines_list) < rows:
                        lines_list += [lines_list[-1] + 1.0] * (rows - len(lines_list))
                except Exception:
                    lines_list = [i * 1.0 + 0.5 for i in range(rows)]
                
                TARGET_LAYOUTS.clear()
                for i, t in enumerate(final_targets):
                    row_idx = i // 2
                    line = lines_list[row_idx]
                    TARGET_LAYOUTS.append((t['side'], line, t['x'], t['y'], t['w'], t['h']))
                break
            elif action == 'reset':
                page.evaluate("() => { window.__row_count = 3; }")
                continue
            elif action == 'update':
                continue

        page.evaluate("() => { let b = document.getElementById('__pw_target_guide'); if(b) b.remove(); }")
        page.wait_for_timeout(300)

        auto_capture_templates(page)

        if not TEMPLATES:
            log("⚠️ কোনো টার্গেট ক্যাপচার হয়নি — স্ক্রিপ্ট বন্ধ।")
            browser.close()
            return

        start_ws_thread()
        log(f"👂 [WS] wait korchi... (match_key={match_filter})\n")

        remember_seen, was_seen, forget_seen = make_seen_lru(300)
        
        # ⭐ ডাইনামিক ডেলিভারি কাউন্টারের জন্য ভেরিয়েবল
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
                    
                    # ⭐ Smart Push Counter (Legal, Wide, No Ball সবই ১টি ডেলিভারি হিসেবে গোনা হবে)
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
