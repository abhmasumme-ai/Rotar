import asyncio
import io
import json
import os
import queue
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

OCR_SETTLE_MS = 400
OCR_SCALE = 1.5
PENDING_TIMEOUT_SEC = 3.0

SHOT_DIR = Path("ocr_shots")
SHOT_DIR.mkdir(exist_ok=True)

# --- FIXED VIEWPORT (ei duita change korben na) ---
VIEW_W = 1080
VIEW_H = 920
WIN_W = VIEW_W + 100      # browser window border/title bar soho
WIN_H = VIEW_H + 120
# ==========================================

event_q = queue.Queue()
ws_state = {"up": False}

shots = {}
shots_lock = threading.Lock()

pending = []
pending_lock = threading.Lock()

ocr_q = queue.Queue()
stop_evt = threading.Event()
clicked_balls = set()
clicked_lock = threading.Lock()

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
                        await ws.send("ping")

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


# ---------- OCR ----------
_ocr = {"ready": False}

CELL_PATTERN = re.compile(
    r"(\d{1,2})\s*[-–—~]?\s*Over\W*(\d{1,2})\s*[-–—~]?\s*Delivery"
    r"(?:\W*(Over|Under)\W*(\d+[.,]\d+))?",
    re.I,
)


def setup_ocr():
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
        log("⚠️  Tesseract paoa jayni. Install / TESSERACT_CMD set korun.")
        return False
    _ocr.update(ready=True, pt=pytesseract, Image=Image, ImageOps=ImageOps)
    log(f"🔎 [OCR] ready (Tesseract {ver})")
    return True


def ocr_measure_from_png(png_path):
    if not _ocr.get("ready"):
        return {}
    Image, ImageOps, pt = _ocr["Image"], _ocr["ImageOps"], _ocr["pt"]

    img = Image.open(png_path)
    g = ImageOps.autocontrast(img.convert("L"))
    if OCR_SCALE != 1:
        g = g.resize((int(g.width * OCR_SCALE), int(g.height * OCR_SCALE)), Image.LANCZOS)
    d = pt.image_to_data(g, config="--psm 6", output_type=pt.Output.DICT)

    lines = {}
    for i, t in enumerate(d["text"]):
        t = t.strip()
        if t and float(d["conf"][i]) >= 0:
            k = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            lines.setdefault(k, []).append(
                (t, d["left"][i], d["top"][i], d["width"][i], d["height"][i])
            )

    result = {}
    for words in lines.values():
        text, spans = "", []
        for w in words:
            if text:
                text += " "
            spans.append((len(text), len(text) + len(w[0])))
            text += w[0]

        for m in CELL_PATTERN.finditer(text):
            over = int(m.group(1))
            delivery = int(m.group(2))
            side = (m.group(3) or "").lower()
            line_raw = (m.group(4) or "").replace(",", ".")
            try:
                line = float(line_raw) if line_raw else None
            except ValueError:
                line = None

            hit = [w for w, (a, b) in zip(words, spans) if a < m.end() and b > m.start()]
            if not hit:
                continue
            l = min(w[1] for w in hit); t = min(w[2] for w in hit)
            r = max(w[1] + w[3] for w in hit); b = max(w[2] + w[4] for w in hit)
            cx = (l + r) / 2 / OCR_SCALE
            cy = (t + b) / 2 / OCR_SCALE
            result[(over, delivery, side, line)] = (cx, cy)
    return result


def ocr_worker(ocr_page):
    while not stop_evt.is_set():
        try:
            item = ocr_q.get(timeout=0.5)
        except queue.Empty:
            continue
        if item is None:
            break
        reason, ball_key = item
        try:
            if OCR_SETTLE_MS > 0:
                ocr_page.wait_for_timeout(OCR_SETTLE_MS)

            ts = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time()*1000)%1000:03d}"
            safe_key = re.sub(r"[^A-Za-z0-9_-]", "_", ball_key or "unknown")
            png_path = SHOT_DIR / f"ball_{safe_key}_{ts}.png"
            raw = ocr_page.screenshot(type="png", scale="css")
            with open(png_path, "wb") as f:
                f.write(raw)
            log(f"💾 [OCR] screenshot saved: {png_path}")

            t0 = time.time()
            m = ocr_measure_from_png(png_path)
            dt = (time.time() - t0) * 1000

            if ball_key is not None:
                with shots_lock:
                    shots[ball_key] = {"map": m, "png": png_path}
                log(f"📐 [OCR] ball={ball_key} map={len(m)} cells ({dt:.0f}ms)")
            else:
                log(f"📐 [OCR] map={len(m)} cells ({dt:.0f}ms)")
        except Exception as e:
            log(f"⚠️ [OCR] error: {e}")


# ---------- পুশ পার্সিং ----------
def parse_push(payload):
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None

    ball = data.get("ball")
    if isinstance(ball, dict):
        bkey = str(ball.get("ball_key") if ball.get("ball_key") is not None else "")
        if ball.get("is_delete"):
            return {"kind": "delete", "ball_key": bkey, "data": data}
        if isinstance(ball.get("detail"), dict):
            return {"kind": "second", "ball_key": bkey, "data": data, "detail": ball["detail"]}
        return {"kind": "first", "ball_key": bkey, "data": data}

    return {"kind": "match", "data": data}


def ball_info(d):
    bt = str(d.get("ball_type") or "normal").lower()

    if (d.get("team_score") or {}).get("is_wicket") or (d.get("bowler") or {}).get("is_wicket") or d.get("wicket"):
        return ("WICKET", "wicket", 0)

    if re.search(r"no_?ball", bt):
        br = int((d.get("batsman") or {}).get("runs") or 0)
        return (f"NO BALL +{br}" if br > 0 else "NO BALL", "noball", 1 + br)

    if (d.get("batsman") or {}).get("is_six"):
        return ("6", "six", 6)
    if (d.get("batsman") or {}).get("is_four"):
        return ("4", "four", 4)

    if re.search(r"wide", bt):
        wr = int((d.get("team_score") or {}).get("runs") or 1) - 1
        return (f"WIDE +{wr}" if wr > 0 else "WIDE", "wide", 1 + wr)

    r = (d.get("team_score") or {}).get("runs")
    if r is None:
        r = (d.get("batsman") or {}).get("runs") or 0
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


def under_is_true(line, run):
    if line is None:
        return False
    return run < line


def pick_click_cell(shot_map, over, delivery, run):
    if not shot_map:
        return None
    candidates = []
    for (o, d, side, line), xy in shot_map.items():
        if o != over or d != delivery:
            continue
        if side != "under":
            continue
        if under_is_true(line, run):
            candidates.append((line, xy))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


# ---------- মূল লুপ ----------
def monitor_and_click():
    match_filter = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    site_url = input("Site link din: ").strip() or "https://your website"

    if not setup_ocr():
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
        log(f"📁 [SETUP] Screenshot folder: {SHOT_DIR.resolve()}")
        log(f"🖥️  [SETUP] Fixed viewport: {VIEW_W}x{VIEW_H}")

        ocr_page = context.new_page()
        ocr_page.goto(match_url)
        ocr_page.wait_for_timeout(2000)
        log("🖼️  [SETUP] OCR page ready")

        worker = threading.Thread(target=ocr_worker, args=(ocr_page,), daemon=True)
        worker.start()

        start_ws_thread()
        log("👂 [WS] wait korchi...\n")

        last_event = time.time()
        other_match = 0
        seen_second = set()

        try:
            while True:
                now = time.time()
                with pending_lock:
                    items = list(pending)
                for it in items:
                    with shots_lock:
                        sh = shots.get(it["ball_key"])
                    sm = sh["map"] if sh else None
                    xy = pick_click_cell(sm, it["over"], it["delivery"], it["run"]) if sm else None
                    if xy:
                        with clicked_lock:
                            if it["ball_key"] in clicked_balls:
                                with pending_lock:
                                    pending.remove(it)
                                continue
                            clicked_balls.add(it["ball_key"])
                        try:
                            page.mouse.click(*xy)
                            log(f"✅ [CLICK] ball={it['ball_key']} "
                                f"({it['over']},{it['delivery']}) run={it['run']} "
                                f"at ({xy[0]:.0f},{xy[1]:.0f}) [pending]")
                        except Exception as e:
                            log(f"⚠️ [CLICK] error: {e}")
                            if page.is_closed():
                                raise KeyboardInterrupt
                        with pending_lock:
                            pending.remove(it)
                    elif now - it["t"] > PENDING_TIMEOUT_SEC:
                        log(f"⌛ [SKIP] ball={it['ball_key']} ({it['over']},{it['delivery']}) "
                            f"run={it['run']} — Under cell mile nai")
                        with pending_lock:
                            pending.remove(it)

                try:
                    recv_ts, raw = event_q.get(timeout=0.5)
                except queue.Empty:
                    if time.time() - last_event >= HEARTBEAT_SEC:
                        log(f"⏳ [HEARTBEAT] WS={'ON' if ws_state['up'] else 'OFF'} | "
                            f"shots={len(shots)} | pending={len(pending)} | "
                            f"clicked={len(clicked_balls)} | other={other_match}")
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
                    if bkey:
                        log(f"▶️  [PUSH-1] ball={bkey} — OCR trigger")
                        ocr_q.put(("first_push", bkey))
                    else:
                        log("⚠️  [PUSH-1] ball_key paoa jayni")
                    continue

                if push["kind"] == "delete":
                    bkey = push["ball_key"]
                    with shots_lock:
                        sh = shots.pop(bkey, None)
                    with clicked_lock:
                        clicked_balls.discard(bkey)
                    if sh and sh.get("png"):
                        try:
                            os.remove(sh["png"])
                            log(f"🗑️  [DELETE] ball={bkey} png removed: {sh['png']}")
                        except Exception:
                            pass
                    log(f"🗑️  [DELETE] ball={bkey} cleared")
                    continue

                if push["kind"] == "second":
                    bkey = push["ball_key"]
                    d = push["detail"]

                    if bkey in seen_second:
                        continue
                    seen_second.add(bkey)

                    tgt = compute_target(d)
                    if not tgt:
                        log(f"⚠️  [PUSH-2] ball={bkey} overs nei: {d.get('overs')}")
                        continue
                    over, delivery = tgt

                    text, kind, run = ball_info(d)
                    log(f"🏏 [PUSH-2] ball={bkey} ({over},{delivery}) "
                        f"result={text} run={run}")

                    with shots_lock:
                        sh = shots.get(bkey)
                    sm = sh["map"] if sh else None

                    if sm:
                        xy = pick_click_cell(sm, over, delivery, run)
                        if xy:
                            with clicked_lock:
                                if bkey in clicked_balls:
                                    continue
                                clicked_balls.add(bkey)
                            page.mouse.click(*xy)
                            ms = (time.time() - recv_ts) * 1000
                            log(f"✅ [CLICK] ball={bkey} ({over},{delivery}) "
                                f"run={run} at ({xy[0]:.0f},{xy[1]:.0f}) — {ms:.0f}ms")
                        else:
                            log(f"❌ [NO-CLICK] ball={bkey} ({over},{delivery}) run={run} — "
                                f"Under cell mile nai")
                    else:
                        log(f"⏳ [WAIT] ball={bkey} er 1st push screenshot nei — pending")
                        with pending_lock:
                            pending.append({
                                "t": recv_ts, "ball_key": bkey,
                                "over": over, "delivery": delivery, "run": run,
                            })
                    continue

                log(f"ℹ️  [MATCH] push kind={data.get('match_push_kind', '-')} (skip)")

        except KeyboardInterrupt:
            print("\nবন্ধ করা হচ্ছে...")
        finally:
            stop_evt.set()
            ocr_q.put(None)
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    monitor_and_click()
