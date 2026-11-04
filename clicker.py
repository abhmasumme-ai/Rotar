"""cricket_clicker.py

main.py er /ws WebSocket e client hoye connect hoy (thik jemon browser er script.js kore),
tai frontend ar clicker ekoi data ekoi shomoye pay. Nijer kono webhook server nei.

Run:
    pip install websockets playwright
    python cricket_clicker.py [match_key]
"""
import asyncio
import io
import json
import os
import queue
import re
import sys
import threading
import time

import websockets
from playwright.sync_api import sync_playwright

# ================= CONFIG =================
WS_URL = "ws://127.0.0.1:5000/ws"   # main.py er WebSocket (script.js er WS_URL er moto)
OUTCOME = "Over 0.5"      # site er option: "Over 0.5", "Under 0.5", "Over 1.5", "Under 1.5", "Over 3.5", "Under 3.5"
                          # None dile market er prothom option click hobe
NEXT_BALL = False         # False = JSON er ball er market (jemon 7.3 -> "8 - Over, 3 - Delivery")
                          # True  = porer ball er market (jemon 7.3 -> "8 - Over, 4 - Delivery")
BALLS_PER_OVER = 6
HEARTBEAT_SEC = 10        # eto second kono event na ashle "waiting" message dekhabe
RECONNECT_SEC = 3         # WebSocket katle eto second pore abar connect (script.js er RECONNECT_MS)
PING_SEC = 30             # main.py ke "ping" pathay (script.js er moto)
SHOW_FULL_PAYLOAD = False # True dile WebSocket er puro JSON terminal e dekhabe
HEADLESS = os.environ.get("CLICKER_HEADLESS") == "1"
CHROME_PATH = os.environ.get("CLICKER_CHROME_PATH") or None

# --- Canvas site er jonno (text DOM e nei, screenshot theke OCR diye khuje) ---
MODE = "auto"             # "auto" = age DOM e khuje, na pele OCR | "dom" = shudhu DOM | "ocr" = shudhu OCR
OCR_REGION = None         # OCR er area (CSS pixel). None = puro screen. Chhoto korle onek druto hoy, jemon:
                          # {"x": 600, "y": 120, "width": 700, "height": 600}
OCR_SCALE = 1.5           # screenshot boro kore OCR kore. 1 = druto, 2 = chhoto font e beshi nirbhulo
OCR_DEBUG = False         # True dile OCR e na pele ocr_last_screenshot.png save hoy, pele ocr_last_hit.png
TESSERACT_CMD = os.environ.get("TESSERACT_CMD") or None   # Windows e tesseract.exe er path (auto-detect-o kore)
# ==========================================

event_q = queue.Queue()   # (receive_time, raw_json_text)
ws_state = {"up": False}


try:  # Windows console e emoji/Bangla print korte gele UnicodeEncodeError ashe, ota ghotate dey
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- WebSocket client (background thread) ----------
async def ws_loop():
    while True:
        err = None
        try:
            async with websockets.connect(
                WS_URL, ping_interval=None, compression=None, max_size=None
            ) as ws:
                ws_state["up"] = True
                log(f"🔌 main.py WebSocket connected: {WS_URL}")

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
            log("🔌 WebSocket disconnected")
        ws_state["up"] = False
        log(f"🔄 {RECONNECT_SEC}s pore abar connect korbo... ({err or 'connection closed'})")
        await asyncio.sleep(RECONNECT_SEC)


def start_ws_thread():
    threading.Thread(target=lambda: asyncio.run(ws_loop()), daemon=True).start()


# ---------- Site e market khuje click ----------
# Leaf element khuje ber kore, jar text: "<over> - Over, <delivery> - Delivery, <outcome>"
FIND_JS = r"""
([over, delivery, outcome]) => {
    const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const base = `\\b${over}\\s*-\\s*Over,\\s*${delivery}\\s*-\\s*Delivery`;
    const re = new RegExp(outcome ? base + `,\\s*${esc(outcome)}\\s*$` : base, 'i');
    const els = document.body.getElementsByTagName('*');
    for (let i = 0; i < els.length; i++) {
        const el = els[i];
        if (el.childElementCount !== 0) continue;
        const txt = (el.textContent || '').trim();
        if (!txt || txt.length > 120 || !re.test(txt)) continue;
        if (el.getClientRects().length === 0) continue;      // hidden element
        el.scrollIntoView({block: 'center'});
        const r = el.getBoundingClientRect();
        return {found: true, x: r.left + r.width / 2, y: r.top + r.height / 2,
                tag: el.tagName, text: txt};
    }
    return {found: false};
}
"""


# ---------- Canvas: screenshot -> OCR -> pixel position ----------
_ocr = {"ready": False}


def setup_ocr():
    """pytesseract + Tesseract ready kore. Na thakle False ferot dey."""
    try:
        import pytesseract
        from PIL import Image, ImageDraw, ImageOps
    except ImportError:
        log("⚠️  OCR er jonno lagbe: pip install pytesseract pillow")
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
        log("⚠️  Tesseract program paoa jayni. Install korun (Windows: UB-Mannheim installer) "
            "ba TESSERACT_CMD set korun.")
        return False
    _ocr.update(ready=True, pt=pytesseract, Image=Image, ImageDraw=ImageDraw, ImageOps=ImageOps)
    log(f"🔎 OCR ready (Tesseract {ver})")
    return True


def _outcome_pattern(outcome):
    if not outcome:
        return ""
    parts = [re.escape(t).replace(r"\.", "[.,]") for t in outcome.split()]
    return r"\W*" + r"\s*".join(parts)


def ocr_find(page, over, delivery):
    """Screenshot nie OCR kore '<over> - Over, <delivery> - Delivery, <OUTCOME>' er pixel (CSS) position ber kore."""
    if not _ocr.get("ready"):
        return {"found": False}
    Image, ImageOps, pt = _ocr["Image"], _ocr["ImageOps"], _ocr["pt"]
    shot = {"type": "png", "scale": "css"}
    ox = oy = 0
    if OCR_REGION:
        shot["clip"] = OCR_REGION
        ox, oy = OCR_REGION["x"], OCR_REGION["y"]
    img = Image.open(io.BytesIO(page.screenshot(**shot)))
    g = ImageOps.autocontrast(img.convert("L"))
    if OCR_SCALE != 1:
        g = g.resize((int(g.width * OCR_SCALE), int(g.height * OCR_SCALE)), Image.LANCZOS)
    d = pt.image_to_data(g, config="--psm 6", output_type=pt.Output.DICT)

    lines = {}
    for i, t in enumerate(d["text"]):
        t = t.strip()
        if t and float(d["conf"][i]) >= 0:
            k = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            lines.setdefault(k, []).append((t, d["left"][i], d["top"][i], d["width"][i], d["height"][i]))

    # (?<!\d) : "19 - Over" ke "9 - Over" bhule na dhora. OCR er space/hyphen/comma er chhoto bhul-o chole.
    pat = re.compile(rf"(?<!\d){over}\s*[-–—~]?\s*Over\W*{delivery}\s*[-–—~]?\s*Delivery"
                     rf"{_outcome_pattern(OUTCOME)}", re.I)
    for words in lines.values():
        text, spans = "", []
        for w in words:
            if text:
                text += " "
            spans.append((len(text), len(text) + len(w[0])))
            text += w[0]
        m = pat.search(text)
        if not m:
            continue
        hit = [w for w, (a, b) in zip(words, spans) if a < m.end() and b > m.start()]
        l = min(w[1] for w in hit); t = min(w[2] for w in hit)
        r = max(w[1] + w[3] for w in hit); b = max(w[2] + w[4] for w in hit)
        box = (ox + l / OCR_SCALE, oy + t / OCR_SCALE, ox + r / OCR_SCALE, oy + b / OCR_SCALE)
        if OCR_DEBUG:
            dbg = img.convert("RGB")
            dr = _ocr["ImageDraw"].Draw(dbg)
            dr.rectangle([(l / OCR_SCALE, t / OCR_SCALE), (r / OCR_SCALE, b / OCR_SCALE)], outline="red", width=2)
            dbg.save("ocr_last_hit.png")
        return {"found": True, "x": (box[0] + box[2]) / 2, "y": (box[1] + box[3]) / 2,
                "text": text[m.start():m.end()], "tag": "OCR"}
    if OCR_DEBUG:
        img.save("ocr_last_screenshot.png")
    return {"found": False}


def find_target(page, over, delivery):
    """MODE onujayi DOM ba OCR diye market ber kore. Return: {found, x, y, text, via}"""
    if MODE in ("auto", "dom"):
        res = page.evaluate(FIND_JS, [over, delivery, OUTCOME])
        if res.get("found"):
            res["via"] = "DOM"
            return res
        if MODE == "dom":
            return res
    res = ocr_find(page, over, delivery)
    res["via"] = "OCR"
    return res


def compute_target(overs_list):
    """JSON overs [7,3] (7.3 over) -> site er 1-based (over, delivery)."""
    over = int(overs_list[0]) + 1
    delivery = int(overs_list[1])
    if NEXT_BALL:
        delivery += 1
        if delivery > BALLS_PER_OVER:
            over += 1
            delivery = 1
    return over, delivery


def get_match_key(data):
    # script.js er updateViewport er moto
    m = data.get("match")
    return (data.get("matchkey")
            or (m.get("key") if isinstance(m, dict) else None)
            or data.get("match_key")
            or data.get("key"))


def monitor_and_click():
    match_filter = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    print("\n=== Site Automation Setup ===")
    if not match_filter:
        match_filter = input(
            "Match key din (script.js er Fixture e jeta dekhay). "
            "Faka dile shob match er ball e click hobe: "
        ).strip()
    site_url = input("প্রথমে সাইটের লিংক দিন : ").strip() or "https://your website"

    with sync_playwright() as p:
        launch_kwargs = dict(
            headless=HEADLESS,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
            ],
        )
        if CHROME_PATH:
            launch_kwargs["executable_path"] = CHROME_PATH
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            no_viewport=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        print(f"\nসাইটে প্রবেশ করছে: {site_url}")
        page.goto(site_url)

        print("\n=======================================================")
        print("ধাপ ১: ব্রাউজারে ম্যানুয়ালি লগইন সম্পূর্ণ করুন।")
        print("ধাপ ২: target ম্যাচ ওপেন করুন এবং URL কপি করুন।")
        print("=======================================================\n")

        match_url = input("ব্রাউজার অ্যাড্রেস বার থেকে কপি করা URL-টি এখানে পেস্ট করুন: ").strip()
        if not match_url:
            print("কোনো URL দেওয়া হয়নি, প্রোগ্রাম বন্ধ হচ্ছে।")
            browser.close()
            return

        page.goto(match_url)
        log(f"✅ Match page load hoyeche: {page.url}")
        log(f"⚙️  Config: OUTCOME={OUTCOME!r}, NEXT_BALL={NEXT_BALL}, "
            f"match_key={match_filter or 'SHOB MATCH'}")
        if not match_filter:
            log("⚠️  Match key deya hoy ni: main.py te jei match er ball ashbe tar-i market e click hobe!")

        if MODE in ("auto", "ocr"):
            if not setup_ocr():
                if MODE == "ocr":
                    log("MODE='ocr' kintu OCR ready na, bondho hocche.")
                    browser.close()
                    return
                log("OCR chhara cholbe: shudhu DOM site e kaj korbe, canvas e na.")
        log(f"🧭 Search mode: {MODE}")

        start_ws_thread()                       # login/page load er por shuru, jate purono event na jome
        log("👂 main.py WebSocket er jonno wait korchi (bondho korte Ctrl+C)\n")

        seen = set()            # process kora ball_key
        latest = {}             # innings -> shobcheye notun ball position (purono ball skip korar jonno)
        clicked = set()         # (innings, over, delivery): ekoi market duibar click hobe na
        other_match = 0
        last_event = time.time()
        try:
            while True:
                try:
                    recv_ts, raw = event_q.get(timeout=1)
                except queue.Empty:
                    if time.time() - last_event >= HEARTBEAT_SEC:
                        log(f"⏳ Kaj cholche | WS={'ON' if ws_state['up'] else 'OFF'} | "
                            f"processed balls: {len(seen)} | onno match er push: {other_match}")
                        last_event = time.time()
                    continue

                last_event = time.time()
                try:
                    payload = json.loads(raw)
                except ValueError:
                    log("⏭️  Skip: JSON parse hoyni")
                    continue
                if not isinstance(payload, dict):
                    continue
                if SHOW_FULL_PAYLOAD:
                    print(raw, flush=True)

                data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                mkey = get_match_key(data)
                if match_filter and mkey and mkey != match_filter:
                    other_match += 1
                    continue

                ball = data.get("ball")
                if not isinstance(ball, dict):
                    continue                                    # toss/status/score push: clicker er kaj na

                bkey = ball.get("ball_key")
                if ball.get("is_delete"):
                    log(f"⏭️  Skip: ball_key={bkey} delete kora ball")
                    continue

                d = ball.get("detail")
                if not isinstance(d, dict):
                    log(f"🔔 Ball push 1 (detail ashe ni) ball_key={bkey}")   # frontend e "Ball...." dekhay
                    continue

                bkey = str(ball.get("ball_key") if ball.get("ball_key") is not None else d.get("key"))
                if d.get("ball_play_status") != "played":
                    log(f"⏭️  Skip: ball_key={bkey} status={d.get('ball_play_status')!r} (played na)")
                    continue

                overs_list = d.get("overs") or []
                if bkey == "None" or len(overs_list) < 2:
                    log(f"⏭️  Skip: ball_key={bkey} overs={overs_list} (data adhura)")
                    continue
                if bkey in seen:
                    log(f"⏭️  Skip: ball_key={bkey} age e process kora hoyeche")
                    continue

                try:
                    pos = int(overs_list[0]) * BALLS_PER_OVER + int(overs_list[1])
                except (TypeError, ValueError):
                    log(f"[⚠️] overs format bujha jayni: {overs_list}")
                    continue
                inn = d.get("innings") or ball.get("batting_order")
                if pos < latest.get(inn, -1):
                    seen.add(bkey)
                    log(f"⏭️  Skip: ball_key={bkey} {overs_list} purono ball (late/re-send)")
                    continue
                latest[inn] = pos
                seen.add(bkey)

                lag = payload.get("_lag_ms")
                log(f"🏏 Notun ball: {d.get('display_score')} | {str(d.get('comment', '')).strip()[:70]}"
                    f"{f' | Roanuz->server {lag}ms' if isinstance(lag, (int, float)) else ''}")

                over_num, delivery_num = compute_target(overs_list)
                if not 1 <= delivery_num <= BALLS_PER_OVER:
                    log(f"⏭️  Skip: delivery={delivery_num} er market hoy na "
                        f"(wide/no-ball prothom ball e NEXT_BALL=False hole eta normal)")
                    continue
                tgt = (inn, over_num, delivery_num)
                if tgt in clicked:
                    log(f"⏭️  Skip: '{over_num} - Over, {delivery_num} - Delivery' age click kora")
                    continue

                log(f"🎯 Search: '{over_num} - Over, {delivery_num} - Delivery, {OUTCOME or '*'}'")

                try:
                    res = find_target(page, over_num, delivery_num)
                    if res.get("found"):
                        page.mouse.click(res["x"], res["y"])
                        clicked.add(tgt)
                        ms = (time.time() - recv_ts) * 1000
                        log(f"✅ Clicked [{res['via']}] {res['text']!r}  X={res['x']:.0f}, Y={res['y']:.0f}  "
                            f"(WS receive -> click: {ms:.0f}ms)")
                    else:
                        log("❌ Not Found: এই মুহূর্তে স্ক্রিনে market পাওয়া যায়নি।")
                except Exception as e:
                    log(f"⚠️ Error: {e}")
                    if page.is_closed():
                        log("Browser page bondho hoye gese, clicker bondho hocche.")
                        break
        except KeyboardInterrupt:
            print("\nবন্ধ করা হচ্ছে...")
        finally:
            try:
                browser.close()
            except Exception:
                pass            # browser age-i bondho hoye thakle error dekhabe na


if __name__ == "__main__":
    monitor_and_click()