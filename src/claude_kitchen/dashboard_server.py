"""Overview v2 — local web dashboard server.

A small FastAPI app that renders a cross-kitchen status dashboard in a browser
tab. It reads kitchen state straight off disk (`~/.claude-kitchen/<name>/`) on
every `/state` request — one cheap filesystem scan, no daemon state to keep in
sync. The `/loop` summarizer (Chunk 2) writes `synopsis.md` files and pushes a
`loop_tick` over the `/events` WebSocket; connected dashboards re-fetch `/state`.

localhost only — binds 127.0.0.1, no auth, no TLS. Run standalone with:

    uvicorn claude_kitchen.dashboard_server:app --host 127.0.0.1 --port 5757
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

_PKG_DIR = Path(__file__).parent
_DASHBOARD_HTML = _PKG_DIR / "dashboard.html"

# Captured once at import so the dashboard can show "server up since …".
SERVER_STARTED_AT = datetime.now(timezone.utc)

# Classification thresholds (precedence retained from v1): age dominates —
# anything quiet for >10min is idle regardless of its stored status; quiet for
# >24h is dormant (collapsed in the dashboard).
_IDLE_AFTER = timedelta(minutes=10)
_DORMANT_AFTER = timedelta(hours=24)

app = FastAPI(title="claude-kitchen overview")


# --- state scan ------------------------------------------------------------

def _kitchen_root() -> Path:
    # Read at call time (not import) so tests can redirect HOME.
    return Path.home() / ".claude-kitchen"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_synopsis(path: Path) -> tuple[str, Optional[str]]:
    """Return (body, generated_at) from a synopsis.md. ('', None) when the file
    is missing/unreadable. Parses the `generated_at:` frontmatter key if present."""
    if not path.exists():
        return "", None
    try:
        text = path.read_text()
    except OSError:
        return "", None
    generated_at = None
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            frontmatter, body = parts[1], parts[2]
            for line in frontmatter.splitlines():
                if line.strip().startswith("generated_at:"):
                    generated_at = line.split(":", 1)[1].strip()
    return body.strip(), generated_at


def _classify(has_sous: bool, sous_status: Optional[str], age: timedelta) -> str:
    """Derive a dashboard status. Age dominates (quiet >10min → idle); a kitchen
    that hasn't written sous.json yet is booting (it just opened) unless it has
    already gone quiet, in which case it's an idle/leaked dir."""
    if age > _IDLE_AFTER:
        return "idle"
    if not has_sous:
        return "booting"
    if sous_status == "working":
        return "working"
    if sous_status == "idle":
        return "waiting_on_you"
    return "booting"


def _scan_kitchen(base: Path, now: datetime) -> Optional[dict]:
    """One kitchen → a /state record, or None if excluded (overview itself, a
    parent_kitchen sub-sous) or unreadable (e.g. it closed mid-scan)."""
    try:
        kj = json.loads((base / "kitchen.json").read_text())
        if base.name == "overview" or kj.get("parent_kitchen"):
            return None
        sous_path = base / "sous.json"
        if sous_path.exists():
            try:
                sous = json.loads(sous_path.read_text())
            except (json.JSONDecodeError, OSError):
                sous = {}
            mtime = datetime.fromtimestamp(sous_path.stat().st_mtime, tz=timezone.utc)
            has_sous = True
        else:
            sous = {}
            mtime = datetime.fromtimestamp(base.stat().st_mtime, tz=timezone.utc)
            has_sous = False
    except (json.JSONDecodeError, OSError):
        return None

    age = now - mtime
    synopsis, generated_at = _read_synopsis(base / "synopsis.md")
    return {
        "name": base.name,
        "status": _classify(has_sous, sous.get("status"), age),
        "last_status_mtime": _iso(mtime),
        "synopsis": synopsis,
        "synopsis_generated_at": generated_at,
        "dormant": age > _DORMANT_AFTER,
    }


def _scan_kitchens(now: datetime) -> list[dict]:
    root = _kitchen_root()
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / "kitchen.json").exists():
            record = _scan_kitchen(d, now)
            if record:
                out.append(record)
    return out


def build_state() -> dict:
    """The full /state payload. `next_loop_tick_at` is null until Chunk 2 wires
    the real summarizer loop."""
    return {
        "kitchens": _scan_kitchens(datetime.now(timezone.utc)),
        "next_loop_tick_at": None,
        "server_started_at": _iso(SERVER_STARTED_AT),
    }


# --- routes ----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _DASHBOARD_HTML.read_text()


@app.get("/state")
def state() -> dict:
    return build_state()


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/internal/loop-tick")
async def loop_tick() -> dict:
    """Loopback-only hook the overview sous hits at the end of each synopsis
    tick (`kitchen overview-broadcast-tick`). Broadcasts a `loop_tick` so every
    connected dashboard re-fetches /state with the fresh synopses."""
    await broadcast({"type": "loop_tick", "ts": _iso(datetime.now(timezone.utc))})
    return {"ok": True}


# --- websocket broadcast ---------------------------------------------------
#
# Read-only stream: clients connect and listen; the server pushes a `loop_tick`
# after each summarizer tick (Chunk 2 calls broadcast()). Chunk 1 just holds
# connections open — no triggers yet.

_clients: set[WebSocket] = set()


@app.websocket("/events")
async def events(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    try:
        # The dashboard never sends; this await just parks until disconnect.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


async def broadcast(message: dict):
    """Push a JSON message to every connected dashboard. Drops clients that
    error on send. Called by the Chunk 2 loop-tick broadcaster."""
    for ws in list(_clients):
        try:
            await ws.send_json(message)
        except Exception:
            _clients.discard(ws)
