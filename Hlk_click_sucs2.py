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
from collections import OrderedDict
from pathlib import Path

import websockets
from playwright.sync_api import sync_playwright

# ================= CONFIG =================
WS_URL = "ws://127.0.0.1:5000/ws"
BALLS_PER_OVER = 6
HEARTBEAT_SEC = 10
RECONNECT_SEC = 3
PING_SEC = 30
HEADLESS = os.environ.get("CLICKER_HEADLESS") == "1"
CHROME_PATH = os.environ.get("CLICKER_CHROME_PATH") or None
TESSERACT_CMD = os.environ.get("TESSERACT_CMD") or None

OCR_SCALE = 1.0
PENDING_TIMEOUT_SEC = 10.0     
STALE_MAP_TTL = 30.0           
REAP_INTERVAL = 5.0            
SEEN_SECOND_MAX = 300          
COMPARE_THRESHOLD = 0.65  

SHOT_DIR = Path("ocr_shots")
SHOT_DIR.mkdir(parents=True, exist_ok=True)

VIEW_W = 1080
VIEW_H = 920
WIN_W = VIEW_W + 100
WIN_H = VIEW_H + 120

# ================= INBUILD TARGET MAPPING =================
TARGET_LAYOUTS = [
    ("over", 0.5, 7,   46, 435, 40),
    ("under", 0.5, 446, 46, 440, 40),
    ("over", 1.5, 7,   89, 435, 40),
    ("under", 1.5, 446, 89, 440, 40),
    ("over", 3.5, 7,   131, 435, 40),
    ("under", 3.5, 446, 131, 440, 40),
]
# ==========================================================

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

last_over = {"v": None}
last_over_lock = threading.Lock()

_ocr = {"ready": False}
_ssim = {"ready": False}

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ---------- ফাইল পরিষ্কার ----------
def collect_keep_pngs():
    """শুধু first_maps-এ থাকা (চলতি ওভারের) PNG-গুলো রাখবে।"""
    keep = set()
    with first_lock:
        for _, fm in first_maps.items():
            if fm.get("png"):
                keep.add(str(fm["png"]))
            for c in fm.get("cells", []):
                if c.get("crop1_png"):
                    keep.add(str(c["crop1_png"]))
    return keep


def cleanup_shots_dir(keep_pngs=None):
    """keep_pngs বাদে বাকি সব ছবি ডিলিট করবে।"""
    if keep_pngs is None:
        keep_pngs = set()
    keep_strs = {str(p) for p in keep_pngs}
    removed = 0
    try:
        for f in SHOT_DIR.glob("*.png"):
            if str(f) in keep_strs:
                continue
            try:
                f.unlink()
                removed += 1
            except Exception:
                pass
    except Exception as e:
        log(f"⚠️ [CLEANUP] error: {e}")
    if removed:
        log(f"🧹 [CLEANUP] {removed} files removed (over complete)")


def clear_all_shots():
    """প্রোগ্রাম বন্ধ হওয়ার সময় সব ছবি মুছে ফেলবে।"""
    try:
        for f in SHOT_DIR.glob("*.png"):
            try:
                f.unlink()
            except Exception:
                pass
        log("🧹 [CLEANUP] সব ছবি ডিলিট করা হয়েছে (Program Stopped).")
    except Exception:
        pass


def check_over_change(new_over):
    """ওভার নম্বর বদলালে পুরোনো ওভারের সব ছবি পরিষ্কার করে।"""
    with last_over_lock:
        old = last_over["v"]
        if old is None:
            last_over["v"] = new_over
            return
        if new_over != old:
            last_over["v"] = new_over
            keep = collect_keep_pngs() # শুধু pending/চলতি বলের ছবি বাঁচবে
            cleanup_shots_dir(keep)


# ---------- WebSocket ----------
async def ws_loop():
    while not stop_evt.is_set():
        err = None
        try:
            async with websockets.connect(
                WS_URL, ping_interval=None, compression=None, max_size=None
            ) as ws:
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
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError:
        log("⚠️  pip install pytesseract pillow")
        return False
    
    _ocr.update(ready=True, Image=Image)
    try:
        import cv2
        import numpy as np
        _ssim.update(ready=True, cv2=cv2, np=np)
        log("🔎 [OpenCV] ready for SSIM Compare")
    except ImportError:
        log("⚠️  pip install opencv-python numpy")
    return True


def crop_from_png(png_path, rect, out_path):
    if not _ocr.get("ready"):
        return False
    Image = _ocr["Image"]
    img = Image.open(png_path)

    x = int(rect.get("x", 0))
    y = int(rect.get("y", 0))
    w = int(rect.get("w", 1))
    h = int(rect.get("h", 1))

    x = max(0, x)
    y = max(0, y)

    img.crop((x, y, x + w, y + h)).save(out_path)
    return True


def ssim_score(png_a, png_b):
    if not _ssim.get("ready"):
        return None
    cv2 = _ssim["cv2"]
    a = cv2.imread(str(png_a), cv2.IMREAD_GRAYSCALE)
    b = cv2.imread(str(png_b), cv2.IMREAD_GRAYSCALE)
    if a is None or b is None:
        return None
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]))
    try:
        res = cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)
        return float(res[0][0])
    except Exception:
        return None


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


def safe_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ---------- রান অনুযায়ী side/line ----------
def decide_side_line(run):
    if run == 0:
        return ("under", 0.5)
    known_lines = [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5]
    best = max([ln for ln in known_lines if run > ln], default=0.5)
    return ("over", best)


def pick_click_target(run, over):
    side, line = decide_side_line(run)
    with targets_lock:
        tlist = [
            {
                "rect": dict(t["rect"]),
                "over": t["over"],
                "side": t["side"],
                "line": t["line"],
                "label": t.get("label"),
            }
            for t in targets
        ]

    exact = [
        t for t in tlist
        if t["side"] == side and t["line"] is not None
        and abs(t["line"] - line) < 0.01
        and (t["over"] is None or t["over"] == over)
    ]
    if exact:
        return exact[0]

    fallback = [
        t for t in tlist
        if t["side"] == side and t["line"] is not None
        and abs(t["line"] - line) < 0.01
    ]
    if fallback:
        return fallback[0]
    return None


# ---------- ১ম পুশ ----------
def process_first_push(page, ball_key):
    uid = uuid.uuid4().hex[:8]
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", ball_key)
    png1 = SHOT_DIR / f"first_{safe}_{uid}.png"

    try:
        raw = page.screenshot(type="png", scale="css")
        with open(png1, "wb") as f:
            f.write(raw)
        log(f"💾 [PUSH-1] ball={ball_key}")
    except Exception as e:
        log(f"⚠️ [PUSH-1] error: {e}")
        return

    scroll = page.evaluate("() => ({x: window.scrollX || 0, y: window.scrollY || 0})")
    sx, sy = scroll["x"], scroll["y"]

    with targets_lock:
        tlist = [
            {
                "rect": dict(t["rect"]),
                "over": t["over"],
                "side": t["side"],
                "line": t["line"],
            }
            for t in targets
        ]

    cells = []
    for i, t in enumerate(tlist):
        r = t["rect"]
        clip = {"x": r["x"] - sx, "y": r["y"] - sy,
                "width": r["w"], "height": r["h"]}
        crop_path = SHOT_DIR / f"crop1_{safe}_t{i}_{uid}.png"
        
        if crop_from_png(png1, clip, crop_path):
            cells.append({
                "rect": r,
                "over": t["over"],
                "side": t["side"], "line": t["line"],
                "crop1_png": str(crop_path),
            })

    with first_lock:
        first_maps[ball_key] = {
            "cells": cells,
            "png": str(png1),
            "ts": time.time(),
        }
    log(f"   🗺️  {len(cells)} cells mapped")


def _drop_first_map(ball_key):
    with first_lock:
        fm = first_maps.pop(ball_key, None)
    if fm:
        safe_remove(fm.get("png"))
        for c in fm.get("cells", []):
            safe_remove(c.get("crop1_png"))
    with used_lock:
        used_points.pop(ball_key, None)


# ---------- ২য় পুশ ----------
def process_second_push(page, ball_key, run, over):
    with first_lock:
        fm = first_maps.get(ball_key)
    if not fm:
        log(f"⏳ [PUSH-2] ball={ball_key} — ১ম map nei")
        return False

    target = pick_click_target(run, over)
    if not target:
        side, line = decide_side_line(run)
        log(f"❌ [NO-CLICK] ball={ball_key} over={over} run={run} — {side} {line} target nei")
        return False

    side_want, line_want = decide_side_line(run)
    log(f"🎯 [TARGET] ball={ball_key} run={run} → {side_want} {target['line']}")

    rect = target["rect"]
    match_cell = None
    for c in fm["cells"]:
        if (abs(c["rect"]["x"] - rect["x"]) < 1 and
            abs(c["rect"]["y"] - rect["y"]) < 1 and
            c["side"] == target["side"] and
            abs((c["line"] or -1) - (target["line"] or -1)) < 0.01):
            match_cell = c
            break

    if not match_cell:
        log(f"❌ [NO-CLICK] ball={ball_key} — ১ম map e cell nei")
        return False

    scroll = page.evaluate("() => ({x: window.scrollX || 0, y: window.scrollY || 0})")
    sx, sy = scroll["x"], scroll["y"]
    uid = uuid.uuid4().hex[:8]

    r = match_cell["rect"]
    clip = {"x": r["x"] - sx, "y": r["y"] - sy,
            "width": r["w"], "height": r["h"]}
    crop2 = SHOT_DIR / f"crop2_{re.sub(r'[^A-Za-z0-9_-]', '_', ball_key)}_{uid}.png"
    try:
        raw = page.screenshot(type="png", scale="css", clip=clip)
        with open(crop2, "wb") as f:
            f.write(raw)
    except Exception as e:
        log(f"⚠️ compare crop error: {e}")
        return False

    score = ssim_score(match_cell["crop1_png"], crop2)
    if score is None:
        log("   ⚠️ compare engine nei")
        safe_remove(str(crop2))
        return False
    log(f"   🔍 score={score:.3f}")
    if score >= COMPARE_THRESHOLD:
        vx = r["x"] - sx
        vy = r["y"] - sy
        do_human_click(page, ball_key, {"x": vx, "y": vy, "w": r["w"], "h": r["h"]})
        safe_remove(str(crop2))
        _drop_first_map(ball_key)
        return True
    else:
        log(f"❌ [NO-CLICK] ball={ball_key} compare fail score={score:.3f}")
        safe_remove(str(crop2))
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


def compute_target(d):
    if not isinstance(d, dict):
        return None
    ov = d.get("overs")
    if isinstance(ov, list) and len(ov) >= 2:
        try:
            return int(ov[0]) + 1
        except (TypeError, ValueError):
            pass
    return None


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

        # ⭐ Inbuild Mapping লোড
        log("📌 Inbuild ম্যাপিং লোড করা হচ্ছে...")
        with targets_lock:
            targets.clear()
            for side, line, x, y, w, h in TARGET_LAYOUTS:
                targets.append({
                    "rect": {"x": x, "y": y, "w": w, "h": h},
                    "over": None,
                    "side": side, "line": line,
                    "label": f"{side} {line}"
                })
        log(f"✅ {len(targets)}টি টার্গেট লোড হয়েছে।")

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
                    log(f"▶️  [PUSH-1] ball={bkey}")
                    try:
                        process_first_push(page, bkey)
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

                    over = compute_target(d)
                    if not over:
                        continue
                    
                    text, kind, run = ball_info(d)
                    log(f"🏏 [PUSH-2] ball={bkey} over={over} {text} run={run}")

                    with first_lock:
                        fm = first_maps.get(bkey)
                        
                    if fm:
                        try:
                            process_second_push(page, bkey, run, over)
                        except Exception as e:
                            log(f"⚠️ [PUSH-2] error: {e}")
                            if page.is_closed():
                                raise KeyboardInterrupt
                    else:
                        with pending_lock:
                            pending.append({
                                "t": recv_ts, "ball_key": bkey,
                                "over": over, "run": run,
                            })
                        log(f"⏸️  [PENDING] queued ball={bkey}")

                    check_over_change(over)
                    continue

        except KeyboardInterrupt:
            print("\nবন্ধ করা হচ্ছে...")
        finally:
            # ⭐ প্রোগ্রাম বন্ধ হলে সব ছবি ডিলিট করে দেবে
            stop_evt.set()
            try:
                browser.close()
            except Exception:
                pass
            clear_all_shots()

if __name__ == "__main__":
    monitor_and_click()
