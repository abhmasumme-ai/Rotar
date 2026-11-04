import asyncio
import gzip
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("uvicorn.error")
_tasks = set()

PROJECT_KEY = os.environ.get("ROANUZ_PROJECT_KEY")
API_KEY = os.environ.get("ROANUZ_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # optional

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5000"))
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]

BASE = "https://api.sports.roanuz.com/v5"

client = httpx.AsyncClient(timeout=15)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not PROJECT_KEY or not API_KEY:
        log.warning("ROANUZ_PROJECT_KEY / ROANUZ_API_KEY missing (.env check korun)")
    if not WEBHOOK_SECRET:
        log.warning("WEBHOOK_SECRET set kora nai: /webhook shobar jonno open")
    yield
    await client.aclose()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def _send(self, conn: WebSocket, message: str):
        try:
            await asyncio.wait_for(conn.send_text(message), timeout=2)
        except Exception:
            self.disconnect(conn)

    async def broadcast(self, message: str):
        conns = list(self.active_connections)
        if conns:
            await asyncio.gather(*(self._send(c, message) for c in conns))

manager = ConnectionManager()

_token = {"value": None, "ts": 0.0}
TOKEN_TTL = 60 * 60

async def get_token(force: bool = False):
    if not force and _token["value"] and time.time() - _token["ts"] < TOKEN_TTL:
        return _token["value"]
    r = await client.post(f"{BASE}/core/{PROJECT_KEY}/auth/", json={"api_key": API_KEY})
    data = r.json()
    token = (data.get("data") or {}).get("token")
    if not token:
        raise RuntimeError(f"Auth failed: {data}")
    _token["value"] = token
    _token["ts"] = time.time()
    return token

VIEWPORT_KEYS = {"key", "matchkey", "match_key", "status", "live", "ball", "match_push_kind", "ts"}

def slim_payload(payload):
    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    slim_data = {k: v for k, v in data.items() if k in VIEWPORT_KEYS}
    slimmed = {k: v for k, v in payload.items() if k != "data"}
    slimmed["data"] = slim_data
    return slimmed

def _spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)

@app.post("/webhook")
async def roanuz_webhook(request: Request):
    body = await request.body()

    if WEBHOOK_SECRET:
        supplied = request.query_params.get("token") or request.headers.get("x-webhook-secret")
        if not supplied or not hmac.compare_digest(supplied, WEBHOOK_SECRET):
            return JSONResponse({"status": "unauthorized"}, status_code=401)

    try:
        if body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")

        now = time.time()
        payload["_server_ts"] = now * 1000

        d = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        ts_v = d.get("ts")
        if isinstance(ts_v, (int, float)):
            payload["_lag_ms"] = round((now - ts_v) * 1000)  # Roanuz -> server delay (browser pie chart)

        _spawn(manager.broadcast(json.dumps(slim_payload(payload), separators=(",", ":"))))
    except Exception:
        return {"status": "ignored"}
    return {"status": "success"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
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
    # Telegram optional + background: click response Telegram er jonno wait korbe na
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        _spawn(send_telegram(f"Action: {action}\nMatch: {match}"))
    return {"status": "ok", "action": action}

@app.get("/api/matches")
async def get_matches():
    if not PROJECT_KEY or not API_KEY:
        return JSONResponse({"status": "error", "message": "API keys not configured"}, status_code=500)
    url = f"{BASE}/cricket/{PROJECT_KEY}/featured-matches-2/"
    try:
        token = await get_token()
        r = await client.get(url, headers={"rs-token": token})
        if r.status_code in (401, 403):
            token = await get_token(force=True)
            r = await client.get(url, headers={"rs-token": token})
        return r.json()
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=502)

@app.get("/api/match/{match_key}")
async def get_match(match_key: str):
    if not PROJECT_KEY or not API_KEY:
        return JSONResponse({"status": "error", "message": "API keys not configured"}, status_code=500)
    url = f"{BASE}/cricket/{PROJECT_KEY}/match/{match_key}/"
    try:
        token = await get_token()
        r = await client.get(url, headers={"rs-token": token})
        if r.status_code in (401, 403):
            token = await get_token(force=True)
            r = await client.get(url, headers={"rs-token": token})
        return r.json()
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=502)

@app.get("/api/ball-by-ball/{match_key}")
async def get_ball_by_ball(match_key: str):
    if not PROJECT_KEY or not API_KEY:
        return JSONResponse({"status": "error", "message": "API keys not configured"}, status_code=500)
    url = f"{BASE}/cricket/{PROJECT_KEY}/match/{match_key}/ball-by-ball/"
    try:
        token = await get_token()
        r = await client.get(url, headers={"rs-token": token})
        if r.status_code in (401, 403):
            token = await get_token(force=True)
            r = await client.get(url, headers={"rs-token": token})
        return r.json()
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
    return {"status": "ok", "ws_clients": len(manager.active_connections)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, workers=1, access_log=False, log_level="warning",
                ws_per_message_deflate=False)   # choto message e compress korle shudhu delay barhe