"""Tests for the overview v2 dashboard server (Chunk 1)."""
import json
import os
import time

from fastapi.testclient import TestClient

from claude_kitchen.dashboard_server import app

client = TestClient(app)


def _mk(home, name, kitchen=None, sous=None, synopsis=None, age_s=None):
    """Synthesize a kitchen state dir under <home>/.claude-kitchen/. age_s
    back-dates the sous.json mtime (or the dir mtime when there's no sous.json)."""
    d = home / ".claude-kitchen" / name
    d.mkdir(parents=True)
    (d / "kitchen.json").write_text(json.dumps(kitchen or {"source": "/p/" + name}))
    target = d
    if sous is not None:
        (d / "sous.json").write_text(json.dumps(sous))
        target = d / "sous.json"
    if synopsis is not None:
        (d / "synopsis.md").write_text(synopsis)
    if age_s is not None:
        t = time.time() - age_s
        os.utime(target, (t, t))
    return d


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_index_serves_dashboard_html():
    r = client.get("/")
    assert r.status_code == 200
    assert "kitchen dashboard" in r.text
    assert "cdn.tailwindcss.com" in r.text  # Tailwind via CDN, no build step


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


def test_state_shape_and_classification(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _mk(tmp_path, "waiter", sous={"status": "idle", "summary": "need a call"}, age_s=120)   # recent idle → waiting
    _mk(tmp_path, "busy", sous={"status": "working"}, age_s=30)                              # working
    _mk(tmp_path, "fresh")                                                                   # no sous.json, recent → booting
    _mk(tmp_path, "sleepy", sous={"status": "idle"}, age_s=30 * 60)                          # >10min → idle, not dormant
    _mk(tmp_path, "stale", sous={"status": "idle"}, age_s=30 * 3600)                         # >24h → idle + dormant
    _mk(tmp_path, "withsyn", sous={"status": "idle"}, age_s=60,
        synopsis="---\ngenerated_at: 2026-06-03T18:45:00Z\nkitchen: withsyn\n---\nDid a thing.\nThen another.")
    _mk(tmp_path, "overview", sous={"status": "idle"}, age_s=10)                             # excluded by name
    _mk(tmp_path, "subby", kitchen={"source": "/p", "parent_kitchen": "waiter"},
        sous={"status": "working"}, age_s=10)                                               # excluded (sub-sous)

    body = client.get("/state").json()
    assert body["next_loop_tick_at"] is None          # Chunk 2 wires the loop
    assert body["server_started_at"].endswith("Z")    # ISO timestamp captured at load

    by = {k["name"]: k for k in body["kitchens"]}
    assert set(by) == {"waiter", "busy", "fresh", "sleepy", "stale", "withsyn"}  # overview + subby filtered out

    assert by["waiter"]["status"] == "waiting_on_you"
    assert by["busy"]["status"] == "working"
    assert by["fresh"]["status"] == "booting"
    assert by["sleepy"]["status"] == "idle" and by["sleepy"]["dormant"] is False
    assert by["stale"]["dormant"] is True
    assert by["busy"]["last_status_mtime"].endswith("Z")

    # synopsis.md is parsed into body + generated_at frontmatter
    assert by["withsyn"]["synopsis"] == "Did a thing.\nThen another."
    assert by["withsyn"]["synopsis_generated_at"] == "2026-06-03T18:45:00Z"
    # no synopsis.md → empty synopsis, null generated_at
    assert by["fresh"]["synopsis"] == ""
    assert by["fresh"]["synopsis_generated_at"] is None


def test_state_empty_when_no_kitchens(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude-kitchen").mkdir()
    body = client.get("/state").json()
    assert body["kitchens"] == []


def test_old_leaked_dir_is_idle_not_booting(tmp_path, monkeypatch):
    # Chunk 1 review fold-in: a leaked state dir (no sous.json, dir mtime >10min)
    # classifies as idle, NOT booting — age dominates. Guards the deviation from
    # a naive "no sous.json → booting" rule.
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".claude-kitchen" / "leaked"
    d.mkdir(parents=True)
    (d / "kitchen.json").write_text(json.dumps({"source": "/p"}))  # no sous.json
    old = time.time() - 30 * 60  # set AFTER writing kitchen.json (which bumps dir mtime)
    os.utime(d, (old, old))
    by = {k["name"]: k for k in client.get("/state").json()["kitchens"]}
    assert by["leaked"]["status"] == "idle"
