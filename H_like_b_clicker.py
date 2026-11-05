import asyncio
import json
import os
import queue
import random
import re
import sys
import threading
import time
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

OCR_SCALE = 1.3
PENDING_TIMEOUT_SEC = 2.5
COMPARE_THRESHOLD = 0.82

SHOT_DIR = Path("ocr_shots")
SHOT_DIR.mkdir(exist_ok=True)

VIEW_W = 1080
VIEW_H = 920
WIN_W = VIEW_W + 100
WIN_H = VIEW_H + 120
# ==========================================

event_q = queue.Queue()
ws_state = {"up": False}

# targets: list of {"rect": {x,y,w,h}, "label": ...}
targets = []
targets_lock = threading.Lock()

# 1st push maps: {ball_key: {"cells": [...], "png": str}}
first_maps = {}
first_lock = threading.Lock()

# OCR কাজ (worker thread → pytesseract)
ocr_job_q = queue.Queue()

pending = []
pending_lock = threading.Lock()

stop_evt = threading.Event()
clicked_balls = set()
clicked_lock = threading.Lock()
used_points = {}
used_lock = threading.Lock()

_ocr = {"ready": False}
_ssim = {"ready": False}

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- WebSocket ----------
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
        log(f"🔄 [WS] {RECONNECT_SEC}s pore reconnect... ({err or 'closed'})")
        await asyncio.sleep(RECONNECT_SEC)


def start_ws_thread():
    threading.Thread(target=lambda: asyncio.run(ws_loop()), daemon=True).start()


# ---------- Engines ----------
CELL_PATTERN = re.compile(
    r"(\d{1,2})\s*[-–—~]?\s*Ov\w*\W*(\d{1,2})\s*[-–—~]?\s*Deli\w*"
    r"(?:\W*(Over|Under)\W*(\d+[.,]\d+))?",
    re.I,
)


def setup_engines():
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError:
        log("⚠️  pip install pytesseract pillow")
        return False
    cmd = TESSERACT_CMD
    if not cmd:
        win = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        cmd = win if os.path.exists(win) else None
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        ver = pytesseract.get_tesseract_version()
    except Exception:
        log("⚠️  Tesseract paoa jayni.")
        return False
    _ocr.update(ready=True, pt=pytesseract, Image=Image, ImageOps=ImageOps)
    log(f"🔎 [OCR] ready (Tesseract {ver})")

    try:
        import cv2
        import numpy as np
        _ssim.update(ready=True, cv2=cv2, np=np)
        log("🔎 [SSIM] OpenCV ready")
    except ImportError:
        log("⚠️  pip install opencv-python numpy (compare er jonno)")
    return True


def ocr_text_from_png(png_path):
    if not _ocr.get("ready"):
        return ""
    Image, ImageOps, pt = _ocr["Image"], _ocr["ImageOps"], _ocr["pt"]
    img = Image.open(png_path)
    g = ImageOps.autocontrast(img.convert("L"))
    if OCR_SCALE != 1:
        g = g.resize((int(g.width * OCR_SCALE), int(g.height * OCR_SCALE)), Image.LANCZOS)
    return pt.image_to_string(g, config="--psm 6")


def parse_cell(text):
    m = CELL_PATTERN.search(text or "")
    if not m:
        return None
    try:
        over = int(m.group(1))
        delivery = int(m.group(2))
    except ValueError:
        return None
    side = (m.group(3) or "").lower()
    lr = (m.group(4) or "").replace(",", ".")
    try:
        line = float(lr) if lr else None
    except ValueError:
        line = None
    return (over, delivery, side, line)


def crop_from_png(png_path, rect, out_path):
    if not _ocr.get("ready"):
        return False
    Image = _ocr["Image"]
    img = Image.open(png_path)
    x, y, w, h = int(rect["x"]), int(rect["y"]), int(rect["w"]), int(rect["h"])
    x = max(0, x); y = max(0, y)
    w = max(1, w); h = max(1, h)
    img.crop((x, y, x + w, y + h)).save(out_path)
    return True


def ssim_score(png_a, png_b):
    """দুটি ক্রপ PNG-এর তুলনা (grayscale)।"""
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
        # TM_CCOEFF_NORMED — দ্রুত ও কার্যকর
        res = cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)
        return float(res[0][0])
    except Exception:
        return None


def ocr_worker():
    """শুধু pytesseract — Playwright কল নেই।"""
    while not stop_evt.is_set():
        try:
            item = ocr_job_q.get(timeout=0.5)
        except queue.Empty:
            continue
        if item is None:
            break
        job = item
        try:
            if job["kind"] == "first_crop":
                text = ocr_text_from_png(job["crop_png"]).strip().replace("\n", " ")
                job["result"].append({"idx": job["idx"], "text": text})
        except Exception as e:
            log(f"⚠️ [OCR] error: {e}")


# ---------- টার্গেট শেখানো ----------
CLICK_HOOK_JS = """
() => {
  if (window.__targetHook) return;
  window.__targetHook = true;
  document.addEventListener('click', (e) => {
    const c = document.querySelector('canvas');
    const r = c ? c.getBoundingClientRect() : {x:0, y:0};
    window.__lastClick = {x: e.clientX, y: e.clientY,
                          rel_x: e.clientX - r.x, rel_y: e.clientY - r.y};
  }, true);
}
"""

OVERLAY_JS = """
(targets) => {
  let box = document.getElementById('__pw_overlay');
  if (!box) {
    box = document.createElement('div');
    box.id = '__pw_overlay';
    box.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:999999';
    document.body.appendChild(box);
  }
  box.innerHTML = '';
  targets.forEach((t) => {
    const d = document.createElement('div');
    d.style.cssText = `position:absolute;left:${t.x}px;top:${t.y}px;` +
      `width:${t.w}px;height:${t.h}px;border:2px dashed #00e5ff;` +
      `background:rgba(0,229,255,0.15);box-sizing:border-box;`;
    box.appendChild(d);
  });
}
"""


def learn_targets(page):
    print("\n=== টার্গেট শেখানোর মোড ===")
    print("ব্রাউজারে অপশনে ম্যানুয়ালি ক্লিক করুন। প্রতিটির পর over/delivery/side/line লিখুন। শেষ: 'done'\n")
    page.evaluate(CLICK_HOOK_JS)
    while True:
        page.evaluate("() => { window.__lastClick = null; }")
        click = None
        t0 = time.time()
        while time.time() - t0 < 90:
            c = page.evaluate("() => window.__lastClick")
            if c:
                click = c
                break
            page.wait_for_timeout(100)
        if not click:
            print("⏳ ক্লিক পাওয়া যায়নি")
            continue

        ans = input("over delivery side line (যেমন 8 3 under 0.5) | skip | done: ").strip()
        if ans.lower() == "done":
            break
        if ans.lower() == "skip":
            continue

        rw = input("  rect width (ডিফল্ট 80): ").strip() or "80"
        rh = input("  rect height (ডিফল্ট 40): ").strip() or "40"
        try:
            rw, rh = int(rw), int(rh)
        except ValueError:
            rw, rh = 80, 40

        rect = {"x": click["x"] - rw / 2, "y": click["y"] - rh / 2, "w": rw, "h": rh}
        with targets_lock:
            targets.append({"rect": rect, "label": f"target-{len(targets)+1}"})
            overlay_list = [dict(t["rect"]) for t in targets]
        page.evaluate(OVERLAY_JS, overlay_list)
        print(f"📌 added rect=({rect['x']:.0f},{rect['y']:.0f},{rw},{rh}) | total={len(targets)}")


# ---------- ১ম পুশ: স্ক্রিনশট + প্রতিটি target rect ক্রপ + OCR ----------
def process_first_push(page, ball_key):
    ts = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time()*1000)%1000:03d}"
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", ball_key)
    png1 = SHOT_DIR / f"first_{safe}_{ts}.png"

    try:
        raw = page.screenshot(type="png", scale="css")
        with open(png1, "wb") as f:
            f.write(raw)
        log(f"💾 [PUSH-1] ball={ball_key} png: {png1}")
    except Exception as e:
        log(f"⚠️ [PUSH-1] screenshot error: {e}")
        return

    with targets_lock:
        tlist = [dict(t) for t in targets]

    crops = []  # [{idx, crop_png, rect, result_ref}]
    for i, t in enumerate(tlist):
        r = t["rect"]
        crop_path = SHOT_DIR / f"crop1_{safe}_t{i}_{ts}.png"
        if crop_from_png(png1, r, crop_path):
            result_ref = []
            crops.append({
                "idx": i, "rect": r, "crop_png": str(crop_path),
                "result": result_ref, "_ref": result_ref,
            })

    # worker thread-এ OCR পাঠাই
    refs = []
    for c in crops:
        ref = []
        refs.append(ref)
        ocr_job_q.put({"kind": "first_crop", "crop_png": c["crop_png"], "idx": c["idx"], "result": ref})

    # worker শেষ হওয়ার অপেক্ষা (সর্বোচ্চ ১.৫ সেকেন্ড)
    deadline = time.time() + 1.5
    while time.time() < deadline:
        if all(ref for ref in refs):
            break
        time.sleep(0.02)

    # ফল সংগ্রহ
    cells = []
    for c, ref in zip(crops, refs):
        text = ref[0]["text"] if ref else ""
        cell = parse_cell(text)
        if not cell:
            log(f"   ⚠️ target {c['idx']+1}: OCR mile nai {text!r}")
            continue
        over, delivery, side, line = cell
        cells.append({
            "rect": c["rect"], "over": over, "delivery": delivery,
            "side": side, "line": line, "text": text,
            "crop1_png": c["crop_png"],
        })
        log(f"   ✅ target {c['idx']+1}: ({over},{delivery}) {side} {line}")

    with first_lock:
        first_maps[ball_key] = {"cells": cells, "png": str(png1)}


# ---------- ২য় পুশ: শুধু rect ক্রপ স্ক্রিনশট + তুলনা + ক্লিক ----------
def process_second_push(page, ball_key, run, over, delivery):
    with first_lock:
        fm = first_maps.get(ball_key)
    if not fm:
        log(f"⏳ [PUSH-2] ball={ball_key} — ১ম map nei, pending")
        return False

    # যেসব cell মিলতে পারে আগে ফিল্টার
    candidates = [
        c for c in fm["cells"]
        if c["over"] == over and c["delivery"] == delivery
        and c["side"] == "under" and under_is_true(c["line"], run)
    ]
    if not candidates:
        log(f"❌ [NO-CLICK] ball={ball_key} ({over},{delivery}) run={run} — candidate nei")
        return False

    ts = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time()*1000)%1000:03d}"
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", ball_key)

    # প্রতিটি candidate rect-এর জন্য শুধু সেই rect-এর ক্রপ স্ক্রিনশট নাও
    for i, c in enumerate(candidates):
        r = c["rect"]
        clip = {"x": r["x"], "y": r["y"], "width": r["w"], "height": r["h"]}
        crop2 = SHOT_DIR / f"crop2_{safe}_t{i}_{ts}.png"
        try:
            raw = page.screenshot(type="png", scale="css", clip=clip)
            with open(crop2, "wb") as f:
                f.write(raw)
        except Exception as e:
            log(f"⚠️ compare crop error: {e}")
            continue

        score = ssim_score(c["crop1_png"], crop2)
        if score is None:
            log("   ⚠️ compare engine nei")
            continue
        log(f"   🔍 ({over},{delivery}) under {c['line']} score={score:.3f}")
        if score >= COMPARE_THRESHOLD:
            do_human_click(page, ball_key, c["rect"])
            safe_remove(c["crop1_png"])
            safe_remove(str(crop2))
            safe_remove(fm["png"])
            return True
        else:
            safe_remove(str(crop2))

    log(f"❌ [NO-CLICK] ball={ball_key} ({over},{delivery}) run={run} — compare fail")
    return False


def under_is_true(line, run):
    if line is None:
        return False
    return run < line


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
        log(f"✅ [CLICK] ball={ball_key} at ({px:.0f},{py:.0f}) rect=({x:.0f},{y:.0f},{w},{h})")
    except Exception as e:
        log(f"⚠️ [CLICK] error: {e}")


def safe_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


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
    ov = d.get("overs")
    if not isinstance(ov, list) or len(ov) < 2:
        return None
    try:
        return (int(ov[0]) + 1, int(ov[1]))
    except (TypeError, ValueError):
        return None


# ---------- Main ----------
def monitor_and_click():
    match_filter = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    site_url = input("Site link din: ").strip() or "https://example.com"

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
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.goto(site_url)
        print("\n=== ম্যানুয়ালি লগইন করুন ===\n")
        match_url = input("Match URL: ").strip()
        if not match_url:
            browser.close()
            return
        page.goto(match_url)
        log(f"✅ [SETUP] Match page: {page.url}")

        # টার্গেট শেখানো
        learn_targets(page)

        # OCR worker (শুধু pytesseract)
        worker = threading.Thread(target=ocr_worker, daemon=True)
        worker.start()

        start_ws_thread()
        log("👂 [WS] wait korchi...\n")

        last_event = time.time()
        other_match = 0
        seen_second = set()

        try:
            while True:
                # pending second push
                now = time.time()
                with pending_lock:
                    items = list(pending)
                for it in items:
                    with first_lock:
                        fm = first_maps.get(it["ball_key"])
                    if fm:
                        try:
                            process_second_push(
                                page, it["ball_key"], it["run"],
                                it["over"], it["delivery"]
                            )
                        except Exception as e:
                            log(f"⚠️ pending error: {e}")
                        with pending_lock:
                            pending.remove(it)
                    elif now - it["t"] > PENDING_TIMEOUT_SEC:
                        log(f"⌛ [SKIP] ball={it['ball_key']} — ১ম map nei")
                        with pending_lock:
                            pending.remove(it)

                # WS
                try:
                    recv_ts, raw = event_q.get(timeout=0.5)
                except queue.Empty:
                    if time.time() - last_event >= HEARTBEAT_SEC:
                        log(f"⏳ [HEARTBEAT] WS={'ON' if ws_state['up'] else 'OFF'} | "
                            f"targets={len(targets)} | first={len(first_maps)} | "
                            f"pending={len(pending)} | other={other_match}")
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
                if match_filter and mkey and mkey != match_filter:
                    other_match += 1
                    continue

                if push["kind"] == "first":
                    bkey = push["ball_key"]
                    if not bkey:
                        continue
                    log(f"▶️  [PUSH-1] ball={bkey}")
                    try:
                        process_first_push(page, bkey)
                    except Exception as e:
                        log(f"⚠️ [PUSH-1] error: {e}")
                    continue

                if push["kind"] == "delete":
                    bkey = push["ball_key"]
                    with first_lock:
                        fm = first_maps.pop(bkey, None)
                    if fm:
                        safe_remove(fm["png"])
                        for c in fm.get("cells", []):
                            safe_remove(c.get("crop1_png"))
                    with clicked_lock:
                        clicked_balls.discard(bkey)
                    seen_second.discard(bkey)
                    log(f"🗑️  [DELETE] ball={bkey}")
                    continue

                if push["kind"] == "second":
                    bkey = push["ball_key"]
                    d = push["detail"]
                    if bkey in seen_second:
                        continue
                    seen_second.add(bkey)

                    tgt = compute_target(d)
                    if not tgt:
                        continue
                    over, delivery = tgt
                    text, kind, run = ball_info(d)
                    log(f"🏏 [PUSH-2] ball={bkey} ({over},{delivery}) {text} run={run}")

                    with first_lock:
                        fm = first_maps.get(bkey)
                    if fm:
                        try:
                            process_second_push(page, bkey, run, over, delivery)
                        except Exception as e:
                            log(f"⚠️ [PUSH-2] error: {e}")
                            if page.is_closed():
                                raise KeyboardInterrupt
                    else:
                        with pending_lock:
                            pending.append({
                                "t": recv_ts, "ball_key": bkey,
                                "over": over, "delivery": delivery, "run": run,
                            })
                    continue

                log(f"ℹ️  [MATCH] push (skip)")

        except KeyboardInterrupt:
            print("\nবন্ধ করা হচ্ছে...")
        finally:
            stop_evt.set()
            ocr_job_q.put(None)
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    monitor_and_click()
