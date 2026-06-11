"""Overview v2 — local web dashboard server.

A small FastAPI app that renders a cross-kitchen status dashboard in a browser
tab. It reads kitchen state straight off disk (`~/.claude-kitchen/<name>/`) on
every `/state` request — one cheap filesystem scan, no daemon state to keep in
sync. The `kitchen overview-loop` daemon writes `synopsis.json` files and pushes
a `loop_tick` over the `/events` WebSocket; connected dashboards re-fetch `/state`.

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

# Classification thresholds. Grouping is *time-independent* for blocked
# kitchens (see _classify): _IDLE_AFTER only bounds "actively working" and
# "just booting" (a working/no-sous kitchen quiet >10min falls through to idle);
# _DORMANT_AFTER collapses long-idle kitchens into the dashboard's drawer. A
# kitchen blocked on the head chef stays waiting_on_you at any age and is never
# dormant.
_IDLE_AFTER = timedelta(minutes=10)
_DORMANT_AFTER = timedelta(hours=24)

# Sort order within "Waiting on you": high → med → low, then oldest first.
_URGENCY_RANK = {"high": 0, "med": 1, "low": 2}

app = FastAPI(title="claude-kitchen overview")


# --- state scan ------------------------------------------------------------

def _kitchen_root() -> Path:
    # Read at call time (not import) so tests can redirect HOME.
    return Path.home() / ".claude-kitchen"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_synopsis(path: Path) -> dict:
    """Parse synopsis.json → the structured copy fields exposed in /state
    (`line`/`block`/`actions`/`urgency`, plus `generated_at` surfaced as
    `synopsis_generated_at`). Absent or unparseable → graceful-empty defaults:
    the *common* state right after deploy (only a stale synopsis.md exists until
    the loop writes the .json). A missing/garbled file is a normal transient
    data state, not a misconfiguration, so it must never crash /state — the
    kitchen just renders with no copy until the next tick fills it in."""
    empty = {"line": "", "block": None, "actions": [], "urgency": "low",
             "synopsis_generated_at": None}
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return empty
    if not isinstance(data, dict):   # valid JSON but [], null, a bare string, …
        return empty
    return {
        "line": data.get("line", ""),
        "block": data.get("block"),
        "actions": data.get("actions", []),
        "urgency": data.get("urgency", "low"),
        "synopsis_generated_at": data.get("generated_at"),
    }


def _working_cook_mtime(base: Path, now: datetime) -> Optional[datetime]:
    """Freshest mtime among cooks whose status file says "working" and whose
    FILE mtime is ≤ _IDLE_AFTER. The judged-by-mtime rule matches the sous: the
    in-file `ts` field lags the hook's last rewrite, so it is never consulted.
    None when no cook qualifies (incl. no cooks/ dir)."""
    freshest = None
    for f in (base / "cooks").glob("*.json"):
        try:
            data = json.loads(f.read_text())
            # valid JSON but [], null, a bare string, … — same guard as synopsis
            if not isinstance(data, dict) or data.get("status") != "working":
                continue
            m = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        except (json.JSONDecodeError, OSError):
            continue
        if now - m <= _IDLE_AFTER and (freshest is None or m > freshest):
            freshest = m
    return freshest


def _classify(has_sous: bool, sous_status: Optional[str],
              block: Optional[str], age: timedelta, cook_working: bool) -> str:
    """Derive a dashboard status — time-independent for "waiting on you". A
    blocked kitchen (block != null) stays waiting_on_you at any age and is never
    swept to idle/dormant; recency only separates actively-working from idle
    among non-blocked kitchens. Order is significant:
      1. working sous, fresh (≤10min)            → working
      2. blocked on the head chef (any age)      → waiting_on_you  (also catches
         a stale working sous that left a block — busy can't nag forever; and
         needs-you outranks cooks-still-working)
      3. any cook working, fresh (≤10min)        → working (the sous idles
         between turns while its brigade grinds — that kitchen is not idle)
      4. no sous yet, fresh (≤10min)             → booting (just spawned)
      5. otherwise                               → idle"""
    if sous_status == "working" and age <= _IDLE_AFTER:
        return "working"
    if block is not None:
        return "waiting_on_you"
    if cook_working:
        return "working"
    if not has_sous and age <= _IDLE_AFTER:
        return "booting"
    return "idle"


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
    cook_mtime = _working_cook_mtime(base, now)
    syn = _read_synopsis(base / "synopsis.json")
    status = _classify(has_sous, sous.get("status"), syn["block"], age,
                       cook_mtime is not None)
    # The displayed age tracks the busiest signal: a kitchen whose cooks are
    # mid-task shows their freshness, not "working · 45m" off an idle sous.
    # (`age`/dormant stay sous-based: a fresh working cook forces status to
    # working or waiting_on_you above, never idle, so dormant is unaffected.)
    if cook_mtime is not None and cook_mtime > mtime:
        mtime = cook_mtime
    return {
        "name": base.name,
        "status": status,
        "line": syn["line"],
        "block": syn["block"],
        "actions": syn["actions"],
        "urgency": syn["urgency"],
        "last_status_mtime": _iso(mtime),
        "synopsis_generated_at": syn["synopsis_generated_at"],
        # dormant only collapses long-idle kitchens. A blocked kitchen is
        # waiting_on_you (never idle), so it is never dormant — this is the field
        # that would otherwise re-hide a blocked-old kitchen.
        "dormant": status == "idle" and age > _DORMANT_AFTER,
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
    return _sort_kitchens(out)


def _sort_kitchens(kitchens: list[dict]) -> list[dict]:
    """waiting_on_you sorts urgency high→med→low, then oldest mtime first; every
    other group keeps name order (the scan already yields name-sorted). The
    frontend buckets by status and renders each group in the order given — one
    source of truth, no client-side re-sort. last_status_mtime is zero-padded
    ISO, so an ascending string sort puts the oldest first."""
    waiting = [k for k in kitchens if k["status"] == "waiting_on_you"]
    others = [k for k in kitchens if k["status"] != "waiting_on_you"]
    waiting.sort(key=lambda k: (_URGENCY_RANK.get(k["urgency"], 2),
                                k["last_status_mtime"]))
    return waiting + others


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
    """Loopback-only hook the overview loop POSTs after a tick that (re)wrote at
    least one synopsis. Broadcasts a `loop_tick` so every connected dashboard
    re-fetches /state with the fresh synopses."""
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
