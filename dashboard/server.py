"""
Dashboard WebSocket server.

Run alongside main.py in a separate terminal:
    python dashboard/server.py

Opens a browser at http://localhost:8080 showing the live trading dashboard.
Streams state + logs over WebSocket every second.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Allow imports from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError:
    print("ERROR: Run 'pip install fastapi uvicorn' to use the dashboard.")
    sys.exit(1)

from src.utils.bot_state import BotState

app = FastAPI(title="Kalshi Trading Bot Dashboard")

STATIC = Path(__file__).parent / "static"

# Serve static assets (JS/CSS if extracted).
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# ------------------------------------------------------------------ REST
@app.get("/api/state")
def get_state():
    return BotState.get()


@app.get("/api/logs")
def get_logs(n: int = 200):
    return {"lines": BotState.get_logs(n)}


# ------------------------------------------------------------------ WebSocket
connected: set[WebSocket] = set()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected.add(ws)
    # Send last 100 log lines immediately on connect.
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
    return HTMLResponse("<h1>Dashboard starting…</h1><p>Static files not found.</p>")


# ------------------------------------------------------------------ main
if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", 8080))
    print(f"\n  Dashboard → http://localhost:{port}\n")
    uvicorn.run(
        "dashboard.server:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="warning",
    )
