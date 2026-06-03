"""State management for claude-kitchen."""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


def state_dir(kitchen: str) -> Path:
    return Path.home() / ".claude-kitchen" / kitchen


def overview_state_dir() -> Path:
    """Fixed global state dir for the overview kitchen, independent of cwd."""
    return Path.home() / ".claude-kitchen" / "overview"


def wiki_dir(slug: str) -> Path:
    return Path.home() / ".claude-kitchen" / "projects" / slug / "wiki"


def notes_dir(kitchen: str) -> Path:
    return state_dir(kitchen) / "notes"


def atomic_write_json(path: Path, data: dict):
    """Atomic JSON write via temp-then-os.replace. Concurrent writers
    never share a temp filename and never replace each other's in-flight
    file. Readers see either the previous or the new full file — never
    empty/partial.

    Creates the parent directory if missing. On any failure (serialization,
    write, replace) the orphaned tempfile is unlinked before the exception
    re-raises, so repeated failures don't accumulate stale .tmp files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_status(base: Path, name: str, data: dict):
    # Eliminates the JSONDecodeError race for brigade / statusline-segment;
    # cmd_ticket + UserPromptSubmit hook for Claude cooks both fire writes.
    atomic_write_json(base / "cooks" / f"{name}.json", data)


def read_status(base: Path, name: str) -> Optional[dict]:
    path = base / "cooks" / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def update_status(base: Path, name: str, **fields):
    """Read existing status, merge in `fields`, write atomically.

    Use this for status transitions that must not silently clear durable
    fields like `tokens` and `backend`. Initial-write callers (cmd_hire's
    booting/failed writes) should call write_status directly — there's
    nothing to preserve before the first write.
    """
    current = read_status(base, name) or {}
    current.update(fields)
    write_status(base, name, current)


def project_slug(project_path: Path) -> str:
    """Derive a project slug from `git config --get remote.origin.url`.

    Returns the bare repo name — the last path component of the remote,
    sans `.git`. `git@github.com:plonkus/racksmith.git` -> `racksmith`.
    Two unrelated repos that share a repo name collide; that's the user's
    problem to disambiguate with an explicit `--kitchen` / kitchen name.
    When no remote is configured but the path is a git repo, falls back to
    a slugification of the absolute repo toplevel path (collision-proof
    across unrelated repos on one machine; loses cross-machine identity).
    Fails loudly if the path is not a git repo.
    """
    result = subprocess.run(
        ["git", "-C", str(project_path), "config", "--get", "remote.origin.url"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return _slug_from_toplevel(project_path)
    url = result.stdout.strip()
    if url.endswith(".git"):
        url = url[:-4]

    if ":" in url and "://" not in url:
        _, _, path_part = url.partition(":")
    elif "://" in url:
        _, _, path_part = url.split("://", 1)[1].partition("/")
    else:
        sys.exit(f"Could not parse remote URL: {url}")

    parts = [p for p in path_part.split("/") if p]
    if len(parts) < 2:
        sys.exit(f"Could not derive owner/repo from remote URL: {url}")

    return parts[-1]


def namespaced(project: Path, requested: str) -> str:
    """Kitchen name scoped by project slug: `<slug>-<requested>`.

    The single source of truth for the namespaced-name formula, so the
    open path and the lookup probe (resolve_kitchen) always agree.
    """
    return f"{project_slug(project)}-{requested}"


def _slug_from_toplevel(project_path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(project_path), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        sys.exit(
            f"{project_path} is not a git repository. "
            "Kitchen requires a git repo to derive a project slug."
        )
    toplevel = result.stdout.strip()
    slug = re.sub(r"[^a-z0-9-]+", "-", toplevel.lower()).strip("-")
    if not slug:
        sys.exit(f"Could not derive a slug from toplevel {toplevel!r}.")
    return slug


# --- Overview: sous status + cross-kitchen footer ----------------------------
#
# A regular kitchen's sous runs in the head chef's terminal, not a tmux window,
# so its status can't live in cooks/ (sweep deletes non-window cooks, and
# brigade/statusline count cooks/*.json as agents). Instead the sous persists
# its own status at <state-dir>/sous.json — read only by the overview footer
# and (Chunk 3) the snapshot helper.

SOUS_STATUS_FILE = "sous.json"

# A kitchen whose last sous activity was a Stop within this window is still
# "waiting on you"; older than this it's just idle.
_WAITING_WINDOW = timedelta(minutes=10)


def read_sous_status(base: Path) -> Optional[dict]:
    path = base / SOUS_STATUS_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def update_sous_status(base: Path, **fields):
    """Merge `fields` into <state-dir>/sous.json (atomic). Mirrors update_status
    but writes the sous's own file at the state-dir root, not under cooks/."""
    current = read_sous_status(base) or {}
    current.update(fields)
    atomic_write_json(base / SOUS_STATUS_FILE, current)


def _kitchen_dirs() -> list[Path]:
    """All kitchen state dirs under ~/.claude-kitchen that carry a kitchen.json."""
    root = Path.home() / ".claude-kitchen"
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir()
                  if d.is_dir() and (d / "kitchen.json").exists())


def classify_kitchen(base: Path, now: datetime) -> Optional[dict]:
    """Read one kitchen's state and classify it for the overview footer/snapshot.

    Returns {name, state, summary, mtime} where state is one of
    waiting_on_you / working / idle / booting, or None if the kitchen is
    excluded from overview (the overview kitchen itself, or a sub-sous whose
    kitchen.json has parent_kitchen set).
    """
    try:
        kj = json.loads((base / "kitchen.json").read_text())
    except (json.JSONDecodeError, OSError):
        return None
    name = base.name
    if name == "overview" or kj.get("parent_kitchen"):
        return None

    sous = read_sous_status(base) or {}
    status = sous.get("status")
    sous_path = base / SOUS_STATUS_FILE
    src = sous_path if sous_path.exists() else base
    mtime = datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc)
    recent = (now - mtime) <= _WAITING_WINDOW
    has_session_id = bool(kj.get("sous_session_id"))

    if status == "working":
        state = "working"
    elif status == "idle" and recent:
        state = "waiting_on_you"
    elif not has_session_id:
        state = "booting"
    else:
        state = "idle"

    return {"name": name, "state": state, "summary": sous.get("summary"), "mtime": mtime}


_STATE_GLYPH = {"waiting_on_you": "⏳", "working": "🔄", "booting": "🐣", "idle": "💤"}
_STATE_LABEL = {
    "waiting_on_you": "waiting on you",
    "working": "working",
    "booting": "booting",
    "idle": "idle",
}
# Sort: who needs the head chef first, who's busy next, just-started, then idle.
_STATE_ORDER = {"waiting_on_you": 0, "working": 1, "booting": 2, "idle": 3}


def _humanize_elapsed(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _render_kitchen_status_footer(now: Optional[datetime] = None) -> str:
    """Deterministic cross-kitchen status block included in every overview
    notification. Pure function of on-disk state — no LLM involved."""
    now = now or datetime.now(timezone.utc)
    kitchens = [k for k in (classify_kitchen(d, now) for d in _kitchen_dirs()) if k]
    kitchens.sort(key=lambda k: (_STATE_ORDER.get(k["state"], 9), k["name"]))

    header = "─── KITCHEN STATUS " + "─" * 24
    lines = [header]
    if not kitchens:
        lines.append("   (no other kitchens open)")
    for k in kitchens:
        glyph = _STATE_GLYPH.get(k["state"], "•")
        label = _STATE_LABEL.get(k["state"], k["state"])
        elapsed = "" if k["state"] == "booting" else f"  ({_humanize_elapsed(now - k['mtime'])})"
        lines.append(f"{glyph} {k['name']}    {label}{elapsed}")
        first = (k["summary"] or "").strip().splitlines()
        if first:
            ctx = first[0]
            if len(ctx) > 70:
                ctx = ctx[:69] + "…"
            lines.append(f"   └─ {ctx}")
    lines.append("─" * len(header))
    return "\n".join(lines)
