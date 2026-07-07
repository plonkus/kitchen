"""State management for claude-kitchen."""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


# Per-kitchen MCP config filename. Deliberately NOT ".mcp.json": Claude Code
# auto-discovers any file named ".mcp.json" by walking up from a cook's cwd,
# which made cooks spawn their own channel-server. This name is handed to the
# sous explicitly via --mcp-config and is never auto-discovered.
MCP_CONFIG_NAME = "kitchen-mcp.json"

# Legacy filename: kitchens opened before the rename wrote the per-kitchen
# config as a discoverable ".mcp.json". We self-heal these on open/resume and
# remove them on close so existing kitchens stop leaking a cook-discoverable
# config.
LEGACY_MCP_CONFIG_NAME = ".mcp.json"


def state_dir(kitchen: str) -> Path:
    return Path.home() / ".claude-kitchen" / kitchen


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
    sans `.git`. `git@github.com:owner/my-project.git` -> `my-project`.
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

    Idempotent: a `requested` name that is already slug-scoped is returned
    untouched, so the formula never stacks prefixes. This collapses the
    common `kitchen open` no-name case (dir name == slug → `seed-domo`, not
    `seed-domo-seed-domo`) and stops re-opens / opening from a worktree dir
    already named for the kitchen from piling slug on slug (the on-disk
    `my-project-my-project-my-project-feature-x` triple). The `f"{slug}-"`
    boundary keeps a mere prefix match (`my-projecty`) from being mistaken for
    an already-scoped name.
    """
    slug = project_slug(project)
    if requested == slug or requested.startswith(f"{slug}-"):
        return requested
    return f"{slug}-{requested}"


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
