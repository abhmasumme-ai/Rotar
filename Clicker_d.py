import asyncio
import json
import os
import queue
import sys
import threading
import time

import websockets
from playwright.sync_api import sync_playwright

# ================= CONFIG =================
WS_URL = "ws://127.0.0.1:5000/ws"   # tomar main.py er WebSocket
OUTCOME = "Under 0.5"     # site er option: "Over 0.5", "Under 0.5", ...
NEXT_BALL = False         # False = JSON er ball er market, True = porer ball
BALLS_PER_OVER = 6
HEARTBEAT_SEC = 10
RECONNECT_SEC = 3
PING_SEC = 30
SHOW_FULL_PAYLOAD = False
HEADLESS = os.environ.get("CLICKER_HEADLESS") == "1"
CHROME_PATH = os.environ.get("CLICKER_CHROME_PATH") or None

# --- Canvas coordinate mapping (tomar site onujayi eita customize korte hobe) ---
# Canvas er upor-bam kona theke relative position (CSS pixel)
# Example: over header row height, delivery column width
CANVAS_BASE_X = 120       # canvas er bam theke prothom delivery column er distance
CANVAS_BASE_Y = 80        # canvas er upor theke prothom over row er distance
CANVAS_COL_WIDTH = 90     # proti delivery column er width
CANVAS_ROW_HEIGHT = 40    # proti over row er height
# Over number er sathe row mapping: over 1 -> base, over 8 -> base + 7*row_height
# Delivery number: delivery 1 -> base, delivery 3 -> base + 2*col_width
# Eita tomar site er layout dekhe adjust korte hobe.

# Canvas selector (site onujayi bodlate hobe; iframe thakle frame e dhukte hobe)
CANVAS_SELECTOR = "canvas"

# Click er age chhoto delay (UI render howar jonno). 0 dile sathe sathe click.
CLICK_DELAY_MS = 50
# ==========================================

event_q = queue.Queue()
ws_state = {"up": False}


try:
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


# ---------- Canvas coordinate + click ----------
def get_canvas_rect(page):
    """Canvas er CSS rect ferot dey. Na paile None."""
    try:
        return page.evaluate(
            """(sel) => {
                const c = document.querySelector(sel);
                if (!c) return null;
                const r = c.getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height};
            }""",
            CANVAS_SELECTOR,
        )
    except Exception:
        return None


def get_canvas_coords(over_num, delivery_num, rect):
    """
    Over/delivery -> canvas er CSS pixel coordinate (absolute page coordinate).
    tomar site er layout onujayi eita customize korte hobe.
    """
    rel_x = CANVAS_BASE_X + (delivery_num - 1) * CANVAS_COL_WIDTH
    rel_y = CANVAS_BASE_Y + (over_num - 1) * CANVAS_ROW_HEIGHT
    # canvas er CSS size ar attribute size alada hole scale korte hobe
    return rect["x"] + rel_x, rect["y"] + rel_y


def click_option(page, over_num, delivery_num):
    """Canvas e over/delivery er option e click kore. Success hole True."""
    rect = get_canvas_rect(page)
    if not rect:
        log("❌ Canvas paoa jayni (selector/iframe check korun)")
        return False
    x, y = get_canvas_coords(over_num, delivery_num, rect)
    if CLICK_DELAY_MS > 0:
        page.wait_for_timeout(CLICK_DELAY_MS)
    page.mouse.click(x, y)
    log(f"🖱️  Canvas click at ({x:.0f}, {y:.0f}) for over={over_num}, delivery={delivery_num}")
    return True


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

        # Canvas layout ekbar log kori
        rect = get_canvas_rect(page)
        if rect:
            log(f"🖼️  Canvas found: x={rect['x']:.0f}, y={rect['y']:.0f}, "
                f"w={rect['w']:.0f}, h={rect['h']:.0f}")
        else:
            log("⚠️  Canvas paoa jayni — selector/iframe check korun. Click fail hote pare.")

        start_ws_thread()
        log("👂 main.py WebSocket er jonno wait korchi (bondho korte Ctrl+C)\n")

        seen = set()
        latest = {}
        clicked = set()
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
                    continue

                bkey = ball.get("ball_key")
                if ball.get("is_delete"):
                    log(f"⏭️  Skip: ball_key={bkey} delete kora ball")
                    continue

                d = ball.get("detail")
                if not isinstance(d, dict):
                    log(f"🔔 Ball push 1 (detail ashe ni) ball_key={bkey}")
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
                    log(f"⏭️  Skip: delivery={delivery_num} er market hoy na")
                    continue
                tgt = (inn, over_num, delivery_num)
                if tgt in clicked:
                    log(f"⏭️  Skip: '{over_num} - Over, {delivery_num} - Delivery' age click kora")
                    continue

                log(f"🎯 Target: over={over_num}, delivery={delivery_num}, outcome={OUTCOME}")

                try:
                    ok = click_option(page, over_num, delivery_num)
                    if ok:
                        clicked.add(tgt)
                        ms = (time.time() - recv_ts) * 1000
                        log(f"✅ Clicked (WS receive -> click: {ms:.0f}ms)")
                    else:
                        log("❌ Click fail: canvas paoa jayni")
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
                pass


if __name__ == "__main__":
    monitor_and_click()
