"""
Cricket control panel backend  (Entity Sports edition)

Ball event = FAST PATH:
    Entity ball_event  →  slim-less direct payload  →  _spawn broadcast  →  browser
    (no cache, no slimming, no blocking)

Match push_obj = SLOW PATH (for score/team/status):
    Entity match_obj  →  slim_match()  →  cache + broadcast

Env:
    ES_PUSH_TOKEN   (must)
    ES_REST_TOKEN   (optional) fixture list er jonno
    ES_REST_BASE    (optional) default: https://restapi.entitysport.com/v2
"""
import asyncio
import gzip
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---- optional perf boosters ----
try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

try:
    import orjson as _orjson
except ImportError:
    _orjson = None


def _dumps(obj) -> str:
    """Fast JSON → text (browser WS text frame)."""
    if _orjson is not None:
        try:
            return _orjson.dumps(obj).decode("utf-8")
        except Exception:
            pass
    return json.dumps(obj, separators=(",", ":"), default=str)


log = logging.getLogger("uvicorn.error")
_tasks = set()

PUSH_TOKEN = os.environ.get("ES_PUSH_TOKEN")
REST_TOKEN = os.environ.get("ES_REST_TOKEN")
PUSH_URL = os.environ.get("ES_PUSH_URL", "ws://webhook.entitysport.com:8087/connect")

_REST_RAW = os.environ.get("ES_REST_BASE", "https://restapi.entitysport.com/v2").strip()
REST_BASE = _REST_RAW.split("?")[0].rstrip("/")
REST_BASE = re.sub(r"/matches/?$", "", REST_BASE).rstrip("/")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5000"))
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]

client = httpx.AsyncClient(timeout=15)

UP = {"connected": False, "msgs": 0, "last": 0.0}
LATEST_OBJ = {}      # match_id -> last slim match object
FIX = {}             # match_id -> fixture row
_rest_cache = {"ts": 0.0, "rows": []}
REST_TTL = 30
MAX_CACHE = 300

STATUS_MAP = {1: "not_started", 2: "completed", 3: "started", 4: "cancelled"}


def _spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _i(v, d=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def _decode(raw):
    if isinstance(raw, bytes):
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        raw = raw.decode("utf-8", errors="replace")
    return json.loads(raw)


_W_RE = re.compile(r"^\d*w$", re.I)


def fixture_row(mi):
    ta, tb = mi.get("teama") or {}, mi.get("teamb") or {}
    return {
        "key": str(mi.get("match_id")),
        "title": mi.get("title") or mi.get("short_title") or "",
        "format": mi.get("format_str") or "",
        "status": STATUS_MAP.get(_i(mi.get("status")), "not_started"),
        "start_at": mi.get("timestamp_start"),
        "teams": {"a": {"name": ta.get("name")}, "b": {"name": tb.get("name")}},
    }


def slim_comm(live):
    out = []
    for e in (live.get("commentaries") or []):
        if not isinstance(e, dict):
            continue
        if e.get("event") not in ("ball", "wicket"):
            continue
        eid = str(e.get("event_id") or "")
        if not eid:
            continue
        out.append({
            "id": eid,
            "ov": _i(e.get("over")),
            "b": _i(e.get("ball")),
            "x": _i(e.get("xball")),
            "run": _i(e.get("run")),
            "bat": _i(e.get("bat_run")),
            "wd": _i(e.get("wide_run")),
            "nb": _i(e.get("noball_run")),
            "by": _i(e.get("bye_run")),
            "lb": _i(e.get("legbye_run")),
            "four": bool(e.get("four")),
            "six": bool(e.get("six")),
            "w": e.get("event") == "wicket" or bool(_W_RE.match(str(e.get("score", "")).strip())),
            "dis": str(e.get("dismissal") or ""),
            "fh": bool(e.get("freehit")),
        })
    return out[-30:]


def slim_match(msg, now):
    r = msg.get("response") or {}
    mi = r.get("match_info") or {}
    live = r.get("live") if isinstance(r.get("live"), dict) else {}
    ta, tb = mi.get("teama") or {}, mi.get("teamb") or {}
    li = live.get("live_inning") if isinstance(live.get("live_inning"), dict) else {}
    ls = live.get("live_score") if isinstance(live.get("live_score"), dict) else {}
    inn_no = _i(live.get("live_inning_number"))

    bat_id = str(li.get("batting_team_id", ""))
    bat = "a" if bat_id and bat_id == str(ta.get("team_id")) else (
        "b" if bat_id and bat_id == str(tb.get("team_id")) else None)

    mx = _i(li.get("max_over"))
    if not mx:
        for inn in ((r.get("scorecard") or {}).get("innings") or []):
            if _i(inn.get("number")) == inn_no or not mx:
                mx = _i(inn.get("max_over")) or mx

    score = None
    if ls and ls.get("runs") is not None:
        score = {
            "runs": _i(ls.get("runs")),
            "wk": _i(ls.get("wickets")),
            "ov": str(ls.get("overs", 0)),
            "target": _i(ls.get("target")),
            "rrr": ls.get("required_runrate"),
        }

    lw = li.get("last_wicket") if isinstance(li.get("last_wicket"), dict) else None
    status = STATUS_MAP.get(_i(mi.get("status")), "started")
    result = mi.get("result") or (mi.get("status_note") if status == "completed" else "") or ""

    def team(t):
        return {"id": t.get("team_id"), "name": t.get("name"),
                "short": t.get("short_name"), "sc": t.get("scores_full") or ""}

    ts = msg.get("timestamp")
    out = {
        "t": "match",
        "id": str(r.get("match_id") or mi.get("match_id") or live.get("mid") or ""),
        "ts": ts if isinstance(ts, (int, float)) else None,
        "_server_ts": now * 1000,
        "status": status,
        "game": str(mi.get("game_state_str") or live.get("game_state_str") or ""),
        "fmt": mi.get("format_str") or "",
        "title": mi.get("title") or "",
        "result": result,
        "toss": (mi.get("toss") or {}).get("text") or "",
        "day": mi.get("day"),
        "session": mi.get("session"),
        "start": mi.get("timestamp_start"),
        "a": team(ta),
        "b": team(tb),
        "inn": inn_no,
        "bat": bat,
        "max": mx,
        "score": score,
        "lw": ({"ov": str(lw.get("overs_at_dismissal", "")),
                "dis": str(lw.get("dismissal") or ""),
                "sc": _i(lw.get("score_at_dismissal"))} if lw else None),
        "comm": slim_comm(live),
    }
    if isinstance(ts, (int, float)):
        lag = round(now * 1000 - ts)
        if 0 <= lag < 60000:
            out["_lag_ms"] = lag
    return out


def _fresh(obj):
    d = dict(obj)
    d["_server_ts"] = time.time() * 1000
    d.pop("_lag_ms", None)
    return d


class ConnectionManager:
    def __init__(self):
        self.conns = {}

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.conns[ws] = None

    def disconnect(self, ws: WebSocket):
        self.conns.pop(ws, None)

    def watch(self, ws: WebSocket, mid):
        if ws in self.conns:
            self.conns[ws] = mid or None

    async def _send(self, conn: WebSocket, message: str):
        try:
            await asyncio.wait_for(conn.send_text(message), timeout=2)
        except Exception:
            self.disconnect(conn)

    async def broadcast(self, message: str, match_id=None):
        targets = [c for c, w in list(self.conns.items())
                   if w is None or match_id is None or w == match_id]
        if targets:
            await asyncio.gather(*(self._send(c, message) for c in targets))


manager = ConnectionManager()


# =====================================================================
# UPSTREAM — Entity push handler
# =====================================================================
def handle_upstream(raw):
    now = time.time()
    msg = _decode(raw)
    if not isinstance(msg, dict):
        return
    r = msg.get("response")
    if not isinstance(r, dict):
        return

    # ---------- FAST PATH: ball event ----------
    # কোনো slimming, cache, বা লম্বা কাজ নেই — শুধু ছোট dict বানিয়ে সাথে সাথে broadcast
    if "ball_event" in r:
        d = r.get("data") if isinstance(r.get("data"), dict) else {}
        mid = str(r.get("match_id", ""))
        out = {
            "t": "ev", "id": mid,
            "ev": str(r.get("ball_event", "")),
            "code": str(r.get("code", "")),
            "over": d.get("over"), "ball": d.get("ball"),
            "inn": _i(d.get("inn_num")), "iid": d.get("iid"),
            "striker": d.get("striker_name"), "bowler": d.get("bowler_name"),
            "_server_ts": now * 1000,
        }
        payload = _dumps(out)               # একবার serialize
        _spawn(manager.broadcast(payload, mid))   # non-blocking
        return

    # ---------- SLOW PATH: full match object (score/team/commentary) ----------
    if "match_info" in r or "live" in r:
        out = slim_match(msg, now)
        mid = out["id"]
        if not mid:
            return
        LATEST_OBJ[mid] = out
        mi = r.get("match_info")
        if isinstance(mi, dict) and mi.get("match_id"):
            FIX[mid] = fixture_row(mi)
        if len(LATEST_OBJ) > MAX_CACHE:
            LATEST_OBJ.pop(next(iter(LATEST_OBJ)), None)
        if len(FIX) > MAX_CACHE:
            FIX.pop(next(iter(FIX)), None)
        payload = _dumps(out)
        _spawn(manager.broadcast(payload, mid))


async def upstream_loop():
    url = f"{PUSH_URL}?token={PUSH_TOKEN}"
    delay = 3
    while True:
        t0 = time.time()
        msgs0 = UP["msgs"]
        try:
            async with websockets.connect(url, ping_interval=None, max_size=None) as ws:
                UP["connected"] = True
                log.warning("Entity push connected")
                async for raw in ws:
                    UP["msgs"] += 1
                    UP["last"] = time.time()
                    try:
                        handle_upstream(raw)
                    except Exception as e:
                        log.warning("push message skip: %r", e)
                log.warning("Entity push: server close (code=%s reason=%r)",
                            ws.close_code, ws.close_reason)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Entity push disconnected: %r", e)
        UP["connected"] = False
        if time.time() - t0 > 30 or UP["msgs"] > msgs0:
            delay = 3
        else:
            log.warning("Push token reject/limit — invalid token? 2+ connections?")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if not PUSH_TOKEN:
        log.warning("ES_PUSH_TOKEN missing: live push cholbe na")
    else:
        task = asyncio.create_task(upstream_loop())
    if not REST_TOKEN:
        log.warning("ES_REST_TOKEN missing: fixture e shudhu push match dekhabe")
    log.warning("REST base: %s | orjson: %s", REST_BASE, _orjson is not None)
    yield
    if task:
        task.cancel()
        try:
            await task
        except BaseException:
            pass
    await client.aclose()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
            elif data.startswith("watch:"):
                mid = data[6:].strip()
                manager.watch(websocket, mid)
                obj = LATEST_OBJ.get(mid) if mid else None
                if obj:
                    await manager._send(websocket, _dumps(_fresh(obj)))
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


async def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        r = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        )
        return r.is_success
    except Exception:
        return False


@app.post("/api/click")
async def handle_click(data: dict):
    action = data.get("action")
    match = data.get("match", "")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        _spawn(send_telegram(f"Action: {action}\nMatch: {match}"))
    return {"status": "ok", "action": action}


async def rest_fixtures():
    if not REST_TOKEN:
        return []
    if time.time() - _rest_cache["ts"] < REST_TTL:
        return _rest_cache["rows"]

    async def one(status, per_page):
        try:
            r = await client.get(f"{REST_BASE}/matches/", params={
                "status": status, "per_page": per_page, "paged": 1, "token": REST_TOKEN})
            if not r.is_success:
                log.warning("REST (status=%s) HTTP %s: %s",
                            status, r.status_code, r.text[:200])
                return []
            resp = r.json().get("response")
            if not isinstance(resp, dict):
                log.warning("REST (status=%s) unexpected response: %r", status,
                            resp if not isinstance(resp, (list, dict)) else type(resp).__name__)
                return []
            items = resp.get("items") or []
            return [fixture_row(i) for i in items if isinstance(i, dict) and i.get("match_id")]
        except Exception as e:
            log.warning("REST (status=%s) failed: %r", status, e)
            return []

    parts = await asyncio.gather(one(3, 30), one(1, 20), one(2, 10))
    rows = [x for p in parts for x in p]
    _rest_cache.update(ts=time.time(), rows=rows)
    return rows


@app.get("/api/matches")
async def get_matches():
    merged = {}
    for row in await rest_fixtures():
        merged[row["key"]] = row
    for k, row in FIX.items():
        merged[k] = {**merged.get(k, {}), **{kk: vv for kk, vv in row.items() if vv not in (None, "")}}
    return {"status": "ok", "data": {"matches": list(merged.values())}}


@app.get("/api/match/{match_key}")
async def get_match(match_key: str):
    obj = LATEST_OBJ.get(match_key)
    if obj:
        return {"status": "ok", "data": _fresh(obj)}
    if not REST_TOKEN:
        return JSONResponse({"status": "error",
                             "message": "match cache e nai (ES_REST_TOKEN dile REST theke ante pari)"},
                            status_code=404)
    try:
        r = await client.get(f"{REST_BASE}/matches/{match_key}/info",
                             params={"token": REST_TOKEN})
        resp = r.json().get("response") or {}
        mi = resp.get("match_info") if isinstance(resp.get("match_info"), dict) else resp
        if not isinstance(mi, dict) or not mi:
            return JSONResponse({"status": "error", "message": "empty match info"}, status_code=502)
        obj = slim_match({"response": {"match_id": match_key, "match_info": mi}}, time.time())
        obj["id"] = str(match_key)
        return {"status": "ok", "data": _fresh(obj)}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=502)


BASE_DIR = Path(__file__).resolve().parent


def _file(name: str):
    f = BASE_DIR / name
    if not f.exists():
        return JSONResponse({"detail": f"{name} not found"}, status_code=404)
    return FileResponse(f)


@app.get("/")
async def index():
    return _file("index.html")


@app.get("/index.css")
async def css():
    return _file("index.css")


@app.get("/script.js")
async def js():
    return _file("script.js")


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "ws_clients": len(manager.conns),
        "push_connected": UP["connected"],
        "push_msgs": UP["msgs"],
        "push_last_age_s": round(time.time() - UP["last"], 1) if UP["last"] else None,
        "matches_cached": len(LATEST_OBJ),
        "rest_base": REST_BASE,
        "rest_token_set": bool(REST_TOKEN),
        "orjson": _orjson is not None,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, workers=1,
                access_log=False, log_level="warning",
                ws_per_message_deflate=False)
