"""Model identifier → max input context lookup.

Three-layer resolution per spec
docs/superpowers/specs/2026-05-03-model-context-via-litellm-design.md:

  1. Disk cache at ~/.claude-kitchen/model_context.json (TTL 24h, populated
     from LiteLLM's public registry — keys are full Anthropic model IDs).
  2. Live fetch from LiteLLM if the cache is stale or missing the model.
  3. In-tree offline floor for plane-mode / first-run / network-down.

The longest-substring-match rule applies uniformly to whichever source
ends up being consulted: for each KEY in the dict, if `key in model`,
the longest matching key wins. Future date-suffixed identifiers
(e.g. claude-opus-4-7-20260601) match the bare base ID via substring.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_kitchen.state import atomic_write_json

# Last-resort floor — kitchen still works on a plane / behind a corp
# firewall / before the first cache write. A few lines, intentionally
# minimal — NOT the registry. fable earns one despite that: it is the
# sous's default tier, so it is the most common model in a brigade, and
# ctx drives cook rotation.
OFFLINE_FLOOR = {
    "claude-fable-5": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
}

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
CACHE_PATH = Path.home() / ".claude-kitchen" / "model_context.json"
CACHE_TTL = timedelta(hours=24)
FETCH_TIMEOUT_S = 5


def max_context_for(model):
    """Return max input context for a Claude model identifier, or None
    if no source has it. Public surface — single call site for the
    brigade / channel display layer."""
    if not model:
        return None
    cache = _read_cache()
    if cache:
        hit = _lookup(model, cache.get("models") or {})
        if hit is not None and _cache_is_fresh(cache):
            return hit

    # Cache stale, missing the model, corrupt, or absent → try a fetch.
    fetched = _fetch_from_litellm()
    if fetched is not None:
        _write_cache(fetched)
        hit = _lookup(model, fetched)
        if hit is not None:
            return hit

    # Network/parse failure → keep using stale cache values if present.
    if cache:
        hit = _lookup(model, cache.get("models") or {})
        if hit is not None:
            return hit

    return _lookup(model, OFFLINE_FLOOR)


def _lookup(model, models_dict):
    best, best_len = None, -1
    for key, value in models_dict.items():
        if key in model and len(key) > best_len:
            best, best_len = value, len(key)
    return best


def _cache_is_fresh(cache):
    fetched_at = cache.get("fetched_at")
    if not fetched_at:
        return False
    try:
        ts = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - ts < CACHE_TTL


def _read_cache():
    """Return parsed cache dict, or None if the file is missing or
    unreadable/corrupt. A corrupt file falls through to a fresh fetch
    rather than crashing."""
    if not CACHE_PATH.exists():
        return None
    try:
        return json.loads(CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(models_dict):
    """Write the cache atomically. Filesystem failures are swallowed —
    same posture as network failures: the fetched value still serves
    the current call from in-memory state; future calls re-fetch."""
    payload = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "models": models_dict,
    }
    try:
        atomic_write_json(CACHE_PATH, payload)
    except OSError:
        pass


def _fetch_from_litellm():
    """Single GET, 5s timeout, no retries. Filters to `claude-*` entries
    with a `max_input_tokens` int. Any HTTP/parse failure collapses to
    None — caller falls through to stale cache or offline floor."""
    try:
        with urllib.request.urlopen(LITELLM_URL, timeout=FETCH_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for key, entry in data.items():
        if not key.startswith("claude-") or not isinstance(entry, dict):
            continue
        ctx = entry.get("max_input_tokens")
        if isinstance(ctx, int):
            out[key] = ctx
    return out or None
