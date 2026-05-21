"""State management for claude-kitchen."""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


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


_FORGE_PREFIXES = {"github.com": "gh", "gitlab.com": "gl"}


def project_slug(project_path: Path) -> str:
    """Derive a project slug from `git config --get remote.origin.url`.

    Returns "<forge>-<owner>-<repo>" — forge prefix prevents
    cross-forge collisions (github.com/x/y vs gitlab.com/x/y). Unknown
    hosts are canonicalized by replacing "." with "-" on the full
    hostname, so self-hosted git.corp-a.example and git.corp-b.example
    don't collide. When no remote is configured but the path is a git
    repo, falls back to a slugification of the absolute repo toplevel
    path (collision-proof across unrelated repos on one machine; loses
    cross-machine identity). Fails loudly if the path is not a git repo.
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
        host_part, _, path_part = url.partition(":")
        host = host_part
    elif "://" in url:
        rest = url.split("://", 1)[1]
        host, _, path_part = rest.partition("/")
    else:
        sys.exit(f"Could not parse remote URL: {url}")

    if "@" in host:
        host = host.split("@", 1)[1]
    if ":" in host:
        host = host.split(":", 1)[0]

    parts = [p for p in path_part.split("/") if p]
    if len(parts) < 2:
        sys.exit(f"Could not derive owner/repo from remote URL: {url}")

    forge = _FORGE_PREFIXES.get(host) or (host.replace(".", "-") if host else "x")
    return f"{forge}-{parts[-2]}-{parts[-1]}"


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
