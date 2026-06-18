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
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError:
    print("ERROR: Run 'pip install fastapi uvicorn' to use the dashboard.")
    sys.exit(1)

from src.utils.bot_state import BotState

app = FastAPI(title="Kalshi Trading Bot Dashboard")

STATIC = Path(__file__).parent / "static"
ENV_PATH = PROJECT_ROOT / ".env"
KEY_PATH = PROJECT_ROOT / "secrets" / "kalshi_private_key.pem"

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


def _set_env_var(name: str, value: str) -> None:
    """Insert or replace a KEY=value line in the .env file (creating it if needed)."""
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


@app.get("/api/config/status")
def config_status():
    """Report what credentials are currently configured (no secrets returned)."""
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
    """Save Kalshi API Key ID (.env) and/or the private key PEM (secrets/)."""
    key_id = (payload.get("key_id") or "").strip()
    private_key = (payload.get("private_key") or "").strip()

    saved = []
    try:
        if key_id:
            _set_env_var("KALSHI_API_KEY_ID", key_id)
            saved.append("API Key ID")

        if private_key:
            # Basic sanity check that it looks like a PEM.
            if "BEGIN" not in private_key or "PRIVATE KEY" not in private_key:
                return {"ok": False, "error": "That does not look like a private key. It should start with -----BEGIN ... PRIVATE KEY-----"}
            KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Normalise line endings and ensure a trailing newline.
            pem = private_key.replace("\r\n", "\n").strip() + "\n"
            KEY_PATH.write_text(pem, encoding="utf-8")
            saved.append("private key")

        if not saved:
            return {"ok": False, "error": "Nothing to save - enter a key ID and/or private key."}

        return {
            "ok": True,
            "message": f"Saved: {', '.join(saved)}. Press Stop, then start the bot again to apply.",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/stop")
def stop_bot():
    """Signal the bot to stop gracefully (works because the dashboard runs
    in the same process as the engine and shares BotState)."""
    try:
        BotState.update({"stop_requested": True})
        return {"ok": True, "message": "Stop signal sent. The bot will shut down in a moment."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
    return HTMLResponse("<h1>Dashboard starting...</h1><p>Static files not found.</p>")


# ------------------------------------------------------------------ main
if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", 8080))
    print(f"\n  Dashboard -> http://localhost:{port}")
    print(f"  Open this address in your browser.\n")
    # Pass the app object directly (not an import string) so this works no
    # matter how the script is launched - no dependence on sys.path / package
    # name resolution.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
