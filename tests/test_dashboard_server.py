"""Tests for the overview v2 dashboard server (Chunk 3: structured /state)."""
import json
import os
import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from claude_kitchen.dashboard_server import app

client = TestClient(app)


def _mk(home, name, kitchen=None, sous=None, synopsis=None, age_s=None,
        cooks=None):
    """Synthesize a kitchen state dir under <home>/.claude-kitchen/. `synopsis`
    is the dict written to synopsis.json (the structured contract). age_s
    back-dates the sous.json mtime (or the dir mtime when there's no sous.json).
    `cooks` is {name: (status, age_s)} → cooks/<name>.json with a back-dated
    mtime and a deliberately ancient in-file `ts` (the classifier must judge
    freshness by FILE mtime — the ts field lags the hook's rewrite)."""
    d = home / ".claude-kitchen" / name
    d.mkdir(parents=True)
    (d / "kitchen.json").write_text(json.dumps(kitchen or {"source": "/p/" + name}))
    target = d
    if sous is not None:
        (d / "sous.json").write_text(json.dumps(sous))
        target = d / "sous.json"
    if synopsis is not None:
        (d / "synopsis.json").write_text(json.dumps(synopsis))
    if age_s is not None:
        t = time.time() - age_s
        os.utime(target, (t, t))
    for cook, (status, cook_age_s) in (cooks or {}).items():
        cd = d / "cooks"
        cd.mkdir(exist_ok=True)
        f = cd / f"{cook}.json"
        f.write_text(json.dumps(
            {"status": status, "ts": "2020-01-01T00:00:00Z", "backend": "claude"}))
        t = time.time() - cook_age_s
        os.utime(f, (t, t))
    return d


def _syn(line="", block=None, actions=None, urgency="low", generated_at=None):
    return {"line": line, "block": block, "actions": actions or [],
            "urgency": urgency, "generated_at": generated_at}


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_index_serves_dashboard_html():
    r = client.get("/")
    assert r.status_code == 200
    # variant-A design markers (assert the actual design, not just the <title>)
    assert "Waiting on you" in r.text       # the status-spine group label
    assert "Fraunces" in r.text             # the serif display face
    assert "#f7f4ee" in r.text              # warm-paper background token
    assert 'id="dormToggle"' in r.text      # the dormant show/hide toggle
    # the old Tailwind dashboard and its hardcoded sample data are gone
    assert "cdn.tailwindcss.com" not in r.text
    assert "const K =" not in r.text


def test_events_ws_accepts_connection():
    with client.websocket_connect("/events"):
        pass


def test_loop_tick_broadcasts_to_ws_clients():
    # POST /internal/loop-tick → every connected dashboard receives a loop_tick.
    with client.websocket_connect("/events") as ws:
        r = client.post("/internal/loop-tick")
        assert r.status_code == 200
        msg = ws.receive_json()
        assert msg["type"] == "loop_tick"
        assert msg["ts"].endswith("Z")


def test_state_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude-kitchen").mkdir()
    body = client.get("/state").json()
    assert body["next_loop_tick_at"] is None          # Chunk 2 wires the loop
    assert body["server_started_at"].endswith("Z")    # ISO timestamp captured at load
    assert body["kitchens"] == []


# The §3 grouping contract — every Done-when case, asserted in one fixture set.
def test_state_grouping_all_cases(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    HOUR = 3600

    # (1) blocked + recent → waiting_on_you, dormant False
    _mk(tmp_path, "c1_blocked_recent", sous={"status": "idle"}, age_s=120,
        synopsis=_syn(block="approve the prod deploy",
                      actions=["review the diff", "say go"], urgency="high"))
    # (2) blocked + OLD (>24h) → MUST still be waiting_on_you AND dormant False
    _mk(tmp_path, "c2_blocked_old", sous={"status": "idle"}, age_s=30 * HOUR,
        synopsis=_syn(block="merge the long-running PR", actions=["click merge"],
                      urgency="med"))
    # (3) block!=null but sous working + recent → working (busy can't nag)
    _mk(tmp_path, "c3_working_blocked", sous={"status": "working"}, age_s=30,
        synopsis=_syn(line="running the build", block="a stale ask", urgency="high"))
    # (4) working status but stale mtime (>10min) + block!=null → finished → waiting
    _mk(tmp_path, "c4_stale_working", sous={"status": "working"}, age_s=20 * 60,
        synopsis=_syn(block="confirm the rename", actions=["ack"], urgency="low"))
    # (5) idle, block==null, recent → idle, dormant False
    _mk(tmp_path, "c5_idle_recent", sous={"status": "idle"}, age_s=120,
        synopsis=_syn(line="waiting for the next task"))
    # (6) idle, block==null, age >24h → idle, dormant True
    _mk(tmp_path, "c6_idle_old", sous={"status": "idle"}, age_s=30 * HOUR,
        synopsis=_syn(line="long done"))
    # (7a) no sous.json + recent → booting
    _mk(tmp_path, "c7a_nosous_recent")
    # (7b) no sous.json + OLD → idle, dormant True (NOT booting forever)
    _mk(tmp_path, "c7b_nosous_old", age_s=30 * HOUR)
    # (8) no synopsis.json → graceful-empty, no crash
    _mk(tmp_path, "c8_no_synopsis", sous={"status": "idle"}, age_s=120)
    # extra waiting kitchen to exercise oldest-first tiebreak among low urgency
    _mk(tmp_path, "c9_blocked_low_old", sous={"status": "idle"}, age_s=40 * HOUR,
        synopsis=_syn(block="old low-priority ask", actions=["glance"], urgency="low"))
    # exclusions
    _mk(tmp_path, "overview", sous={"status": "idle"}, age_s=10)
    _mk(tmp_path, "subby", kitchen={"source": "/p", "parent_kitchen": "x"},
        sous={"status": "working"}, age_s=10)

    body = client.get("/state").json()
    by = {k["name"]: k for k in body["kitchens"]}
    assert "overview" not in by and "subby" not in by   # excluded

    # (1) blocked + recent
    c1 = by["c1_blocked_recent"]
    assert c1["status"] == "waiting_on_you" and c1["dormant"] is False
    assert c1["block"] == "approve the prod deploy"
    assert c1["actions"] == ["review the diff", "say go"]
    assert c1["urgency"] == "high"

    # (2) THE time-independence regression: old block stays waiting AND not dormant
    c2 = by["c2_blocked_old"]
    assert c2["status"] == "waiting_on_you"
    assert c2["dormant"] is False

    # (3) working overrides a stale block when fresh
    assert by["c3_working_blocked"]["status"] == "working"
    assert by["c3_working_blocked"]["dormant"] is False

    # (4) stale working with a block → treated as finished → waiting
    assert by["c4_stale_working"]["status"] == "waiting_on_you"
    assert by["c4_stale_working"]["dormant"] is False

    # (5) idle recent
    assert by["c5_idle_recent"]["status"] == "idle"
    assert by["c5_idle_recent"]["dormant"] is False

    # (6) idle old → dormant
    assert by["c6_idle_old"]["status"] == "idle"
    assert by["c6_idle_old"]["dormant"] is True

    # (7a) booting
    assert by["c7a_nosous_recent"]["status"] == "booting"
    assert by["c7a_nosous_recent"]["dormant"] is False

    # (7b) old leaked dir → idle + dormant, never stuck booting
    assert by["c7b_nosous_old"]["status"] == "idle"
    assert by["c7b_nosous_old"]["dormant"] is True

    # (8) no synopsis.json → graceful-empty defaults, no crash
    c8 = by["c8_no_synopsis"]
    assert c8["status"] == "idle"               # block==null keeps it out of waiting
    assert c8["line"] == "" and c8["block"] is None
    assert c8["actions"] == [] and c8["urgency"] == "low"
    assert c8["synopsis_generated_at"] is None

    # the old prose `synopsis` field is gone everywhere
    assert all("synopsis" not in k for k in body["kitchens"])

    # sort: waiting_on_you by urgency high→med→low, then oldest first
    waiting_order = [k["name"] for k in body["kitchens"]
                     if k["status"] == "waiting_on_you"]
    assert waiting_order == [
        "c1_blocked_recent",    # high
        "c2_blocked_old",       # med
        "c9_blocked_low_old",   # low, age 40h  (oldest)
        "c4_stale_working",     # low, age 20m  (newest)
    ]


def test_working_cooks_count_as_working(tmp_path, monkeypatch):
    """The sous idles between turns while its brigade grinds — a kitchen with a
    fresh working cook is 'working', not 'idle'. block != null still wins
    (needs-you outranks cooks-working), and the displayed age tracks the
    freshest working cook, not the idle sous."""
    monkeypatch.setenv("HOME", str(tmp_path))

    # sous idle 5min ago, one cook actively working 30s ago → working,
    # last_status_mtime reflects the COOK (≈30s old, not ≈300s)
    _mk(tmp_path, "k1_cook_working", sous={"status": "idle"}, age_s=300,
        synopsis=_syn(line="cooks grinding"),
        cooks={"eng": ("working", 30), "qa": ("idle", 10)})
    # blocked + a fresh working cook → STILL waiting_on_you
    _mk(tmp_path, "k2_blocked_cook_working", sous={"status": "idle"}, age_s=300,
        synopsis=_syn(block="approve the deploy", actions=["say go"]),
        cooks={"eng": ("working", 30)})
    # working cook gone STALE (>10min file mtime; in-file ts is ancient for all
    # fixtures, so a ts-based classifier would misread k1 too) → idle
    _mk(tmp_path, "k3_stale_cook", sous={"status": "idle"}, age_s=300,
        synopsis=_syn(line="all quiet"),
        cooks={"eng": ("working", 20 * 60)})
    # only idle cooks → idle, age stays the sous's
    _mk(tmp_path, "k4_idle_cooks", sous={"status": "idle"}, age_s=300,
        synopsis=_syn(line="between tasks"), cooks={"eng": ("idle", 30)})

    body = client.get("/state").json()
    by = {k["name"]: k for k in body["kitchens"]}

    def age_of(k):
        dt = datetime.strptime(k["last_status_mtime"], "%Y-%m-%dT%H:%M:%SZ")
        return time.time() - dt.replace(tzinfo=timezone.utc).timestamp()

    k1 = by["k1_cook_working"]
    assert k1["status"] == "working" and k1["dormant"] is False
    assert age_of(k1) < 120      # cook's ~30s, not the sous's ~300s

    assert by["k2_blocked_cook_working"]["status"] == "waiting_on_you"
    assert by["k3_stale_cook"]["status"] == "idle"
    k4 = by["k4_idle_cooks"]
    assert k4["status"] == "idle"
    assert 240 < age_of(k4) < 420    # sous's ~300s untouched by the idle cook


def test_synopsis_generated_at_surfaced(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _mk(tmp_path, "k", sous={"status": "idle"}, age_s=60,
        synopsis=_syn(line="did a thing", generated_at="2026-06-03T18:45:00Z"))
    by = {k["name"]: k for k in client.get("/state").json()["kitchens"]}
    assert by["k"]["line"] == "did a thing"
    assert by["k"]["synopsis_generated_at"] == "2026-06-03T18:45:00Z"


def test_malformed_synopsis_json_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = _mk(tmp_path, "garbled", sous={"status": "idle"}, age_s=60)
    (d / "synopsis.json").write_text("{not valid json")
    body = client.get("/state").json()           # must not 500
    by = {k["name"]: k for k in body["kitchens"]}
    assert by["garbled"]["status"] == "idle"     # graceful-empty → block null → not waiting
    assert by["garbled"]["line"] == "" and by["garbled"]["block"] is None
    assert by["garbled"]["urgency"] == "low"


def test_nondict_synopsis_json_does_not_crash(tmp_path, monkeypatch):
    # Valid JSON that isn't an object ([], null, a bare string, a number) must
    # fall back to graceful-empty too — not crash /state with AttributeError on
    # data.get(...). Guards the parse-ok-but-wrong-shape case.
    monkeypatch.setenv("HOME", str(tmp_path))
    for i, content in enumerate(["[]", "null", '"oops"', "42"]):
        d = _mk(tmp_path, f"nondict{i}", sous={"status": "idle"}, age_s=60)
        (d / "synopsis.json").write_text(content)
    body = client.get("/state").json()           # must not 500
    by = {k["name"]: k for k in body["kitchens"]}
    for i in range(4):
        k = by[f"nondict{i}"]
        assert k["status"] == "idle"             # graceful-empty → block null → not waiting
        assert k["line"] == "" and k["block"] is None
        assert k["actions"] == [] and k["urgency"] == "low"
        assert k["synopsis_generated_at"] is None
