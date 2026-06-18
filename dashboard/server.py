"""
Dashboard FastAPI server — runs inside main.py as an asyncio task so it
shares the in-process BotState singleton with the trading engine.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    import httpx
    import uvicorn
except ImportError:
    print("ERROR: Run 'pip install fastapi uvicorn httpx' to use the dashboard.")
    sys.exit(1)

from src.utils.bot_state import BotState

app = FastAPI(title="Kalshi Trading Bot Dashboard")

STATIC = Path(__file__).parent / "static"
ENV_PATH = PROJECT_ROOT / ".env"
KEY_PATH = PROJECT_ROOT / "secrets" / "kalshi_private_key.pem"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
ALLOWED_SERIES = {"KXBTC15M", "KXETH15M"}

if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# ------------------------------------------------------------------ helpers
def _set_env_var(name: str, value: str) -> None:
    lines = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").split("\n")
    prefix = f"{name}="
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = f"{name}={value}"
            break
    else:
        lines.append(f"{name}={value}")
    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")


# ------------------------------------------------------------------ REST: state
@app.get("/api/state")
def get_state():
    return BotState.get()


@app.get("/api/logs")
def get_logs(n: int = 200):
    return {"lines": BotState.get_logs(n)}


# ------------------------------------------------------------------ REST: control
@app.post("/api/stop")
def stop_bot():
    state = BotState.get()
    if state.get("status") in ("stopped",):
        return {"ok": False, "error": "Bot is already stopped."}
    try:
        BotState.update({"stop_requested": True})
        return {"ok": True, "message": "Stop signal sent. The engine will shut down momentarily."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/start")
def start_bot():
    state = BotState.get()
    status = state.get("status", "stopped")
    if status == "running":
        return {"ok": False, "error": "Engine is already running."}
    if status == "starting":
        return {"ok": False, "error": "Engine is already starting up..."}
    try:
        BotState.update({"start_requested": True, "status": "starting"})
        return {"ok": True, "message": "Start signal sent. The engine is launching..."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ------------------------------------------------------------------ REST: config
@app.get("/api/config/status")
def config_status():
    key_id = ""
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").split("\n"):
            if line.strip().startswith("KALSHI_API_KEY_ID="):
                key_id = line.split("=", 1)[1].strip()
                break
    placeholder = (not key_id) or key_id.startswith("your-")
    pem_ok = KEY_PATH.exists() and KEY_PATH.stat().st_size > 100
    return {
        "key_id_set": bool(key_id) and not placeholder,
        "key_id_masked": (key_id[:8] + "..." + key_id[-4:]) if (key_id and not placeholder and len(key_id) > 12) else "",
        "private_key_present": pem_ok,
        "ready_for_live": (bool(key_id) and not placeholder and pem_ok),
    }


@app.post("/api/config/kalshi")
def set_kalshi_credentials(payload: dict = Body(...)):
    key_id = (payload.get("key_id") or "").strip()
    private_key = (payload.get("private_key") or "").strip()
    saved = []
    try:
        if key_id:
            _set_env_var("KALSHI_API_KEY_ID", key_id)
            saved.append("API Key ID")
        if private_key:
            if "BEGIN" not in private_key or "PRIVATE KEY" not in private_key:
                return {"ok": False, "error": "That does not look like a private key — it should start with -----BEGIN ... PRIVATE KEY-----"}
            KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
            pem = private_key.replace("\r\n", "\n").strip() + "\n"
            KEY_PATH.write_text(pem, encoding="utf-8")
            saved.append("private key")
        if not saved:
            return {"ok": False, "error": "Nothing to save — enter a Key ID and/or private key."}
        return {"ok": True, "message": f"Saved: {', '.join(saved)}. Press Stop then Start to apply."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ------------------------------------------------------------------ REST: Kalshi market proxy
@app.get("/api/kalshi/{series}")
async def kalshi_market(series: str):
    """Proxy public Kalshi API for live market data — no auth required."""
    if series not in ALLOWED_SERIES:
        return JSONResponse({"error": "Unknown series"}, status_code=400)

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            # Fetch the most recently expiring open market for this series
            r = await client.get(
                f"{KALSHI_API}/markets",
                params={"series_ticker": series, "status": "open", "limit": 5},
            )
            if r.status_code != 200:
                return {"error": f"Kalshi API {r.status_code}", "markets": []}

            markets = r.json().get("markets", [])
            if not markets:
                return {"series": series, "markets": [], "orderbook": None, "trades": []}

            # Pick the market expiring soonest (first in list from Kalshi)
            market = markets[0]
            ticker = market["ticker"]

            # Fetch orderbook and recent trades in parallel
            ob_task = client.get(f"{KALSHI_API}/markets/{ticker}/orderbook", params={"depth": 8})
            tr_task = client.get(f"{KALSHI_API}/markets/{ticker}/trades", params={"limit": 25})
            ob_r, tr_r = await asyncio.gather(ob_task, tr_task, return_exceptions=True)

            orderbook = ob_r.json() if not isinstance(ob_r, Exception) and ob_r.status_code == 200 else {}
            trades_raw = tr_r.json() if not isinstance(tr_r, Exception) and tr_r.status_code == 200 else {}

            return {
                "series": series,
                "market": {
                    "ticker": market.get("ticker"),
                    "title": market.get("title", ""),
                    "yes_bid": market.get("yes_bid"),
                    "yes_ask": market.get("yes_ask"),
                    "no_bid": market.get("no_bid"),
                    "no_ask": market.get("no_ask"),
                    "last_price": market.get("last_price"),
                    "volume": market.get("volume", 0),
                    "volume_24h": market.get("volume_24h", 0),
                    "open_interest": market.get("open_interest", 0),
                    "expiration_time": market.get("expiration_time"),
                    "liquidity": market.get("liquidity", 0),
                },
                "orderbook": orderbook.get("orderbook", {}),
                "trades": trades_raw.get("trades", []),
            }
    except Exception as exc:
        return {"error": str(exc), "series": series, "markets": []}


# ------------------------------------------------------------------ WebSocket
connected: set[WebSocket] = set()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected.add(ws)
    try:
        await ws.send_text(json.dumps({
            "type": "init",
            "state": BotState.get(),
            "logs": BotState.get_logs(100),
        }))
        while True:
            await asyncio.sleep(1)
            await ws.send_text(json.dumps({
                "type": "tick",
                "state": BotState.get(),
                "logs": BotState.get_logs(5),
            }))
    except (WebSocketDisconnect, Exception):
        connected.discard(ws)


# ------------------------------------------------------------------ HTML
@app.get("/", response_class=HTMLResponse)
def index():
    html_path = STATIC / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard starting...</h1><p>Static files not found.</p>")


# ------------------------------------------------------------------ standalone
if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", 8080))
    print(f"\n  Dashboard -> http://localhost:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
