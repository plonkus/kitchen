"""Tests for the LiteLLM-backed model→max-context lookup."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_kitchen import models


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Each test gets its own cache path so they can't bleed into the
    real ~/.claude-kitchen/model_context.json."""
    cache = tmp_path / "model_context.json"
    monkeypatch.setattr(models, "CACHE_PATH", cache)
    return cache


def _write_cache(cache_path, models_dict, *, fetched_at=None):
    """Write a cache directly bypassing the atomic helper — tests don't
    care about atomicity here, only content."""
    if fetched_at is None:
        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"fetched_at": fetched_at, "models": models_dict}))


def _stale_iso(hours=25):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_cache_hit_fresh(isolated_cache):
    """Fresh cache containing the model → no fetch, return cached value."""
    _write_cache(isolated_cache, {"claude-opus-4-7": 1_000_000})
    with patch.object(models, "_fetch_from_litellm") as mock_fetch:
        result = models.max_context_for("claude-opus-4-7")
    assert result == 1_000_000
    mock_fetch.assert_not_called()


def test_b_cache_miss_fetch_success_writes_and_looks_up(isolated_cache):
    """No cache → fetch → cache file is written + the new value is
    looked up. Verifies the cache schema in passing."""
    fetched = {
        "claude-opus-4-7": 1_000_000,
        "claude-sonnet-4-6": 200_000,
    }
    with patch.object(models, "_fetch_from_litellm", return_value=fetched):
        result = models.max_context_for("claude-opus-4-7")

    assert result == 1_000_000
    written = json.loads(isolated_cache.read_text())
    assert written["models"] == fetched
    assert "fetched_at" in written


def test_c_fetch_failure_falls_back_to_stale_cache(isolated_cache):
    """Stale cache present, fetch fails → stale cache values returned."""
    _write_cache(isolated_cache, {"claude-opus-4-7": 1_000_000},
                 fetched_at=_stale_iso(25))
    with patch.object(models, "_fetch_from_litellm", return_value=None):
        result = models.max_context_for("claude-opus-4-7")
    assert result == 1_000_000


def test_d_fetch_failure_stale_cache_misses_falls_to_offline_floor(isolated_cache):
    """Stale cache exists but doesn't have the requested model, fetch
    fails → offline floor."""
    _write_cache(isolated_cache, {"some-other-model": 99},
                 fetched_at=_stale_iso(48))
    with patch.object(models, "_fetch_from_litellm", return_value=None):
        result = models.max_context_for("claude-opus-4-7")
    assert result == 1_000_000  # from OFFLINE_FLOOR


def test_d2_fable_has_an_offline_floor(isolated_cache):
    """No cache, fetch fails → fable still resolves. It's the sous's own
    default tier, so it's the most common model in a brigade, and a None
    here degrades the ctx tag that cook rotation is driven off."""
    with patch.object(models, "_fetch_from_litellm", return_value=None):
        assert models.max_context_for("claude-fable-5") == 1_000_000


def test_e_all_sources_miss_returns_none(isolated_cache):
    """Brand new model, no cache, fetch fails, not in offline floor."""
    with patch.object(models, "_fetch_from_litellm", return_value=None):
        result = models.max_context_for("claude-future-9000")
    assert result is None


def test_f_http_call_uses_litellm_url_and_5s_timeout(isolated_cache):
    """The fetch hits the documented LiteLLM URL with a 5s timeout."""
    body = json.dumps({
        "claude-opus-4-7": {"max_input_tokens": 1_000_000},
        "gpt-4": {"max_input_tokens": 8192},  # non-claude must be filtered
    }).encode()
    fake_resp = MagicMock()
    fake_resp.read.return_value = body
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False

    with patch("claude_kitchen.models.urllib.request.urlopen",
               return_value=fake_resp) as mock_urlopen:
        result = models.max_context_for("claude-opus-4-7")

    assert result == 1_000_000
    mock_urlopen.assert_called_once()
    args, kwargs = mock_urlopen.call_args
    assert args[0] == models.LITELLM_URL
    assert kwargs.get("timeout") == 5
    # gpt-4 must not have leaked into the cache.
    written = json.loads(isolated_cache.read_text())
    assert "gpt-4" not in written["models"]
    assert "claude-opus-4-7" in written["models"]


def test_g_corrupt_cache_does_not_crash(isolated_cache):
    """Truncated/invalid cache → fall through to fetch (or floor)."""
    isolated_cache.parent.mkdir(parents=True, exist_ok=True)
    isolated_cache.write_text("{not valid json")
    with patch.object(models, "_fetch_from_litellm", return_value=None):
        # No crash; falls to offline floor.
        result = models.max_context_for("claude-opus-4-7")
    assert result == 1_000_000


def test_h_atomic_write_via_temp_then_replace(isolated_cache, monkeypatch):
    """Cache writes go through the same atomic helper state.write_status
    uses — verified by intercepting os.replace."""
    seen = {}
    real_replace = models.atomic_write_json.__wrapped__ if hasattr(models.atomic_write_json, "__wrapped__") else None
    import claude_kitchen.state as state_mod
    real = state_mod.os.replace

    def spy(src, dst):
        seen["src"] = src
        seen["dst"] = str(dst)
        return real(src, dst)

    monkeypatch.setattr(state_mod.os, "replace", spy)
    with patch.object(models, "_fetch_from_litellm",
                      return_value={"claude-opus-4-7": 1_000_000}):
        models.max_context_for("claude-opus-4-7")

    # Atomic-write contract: writes go to a temp file in the cache dir
    # and are then renamed onto the final path. No direct write to the
    # final path bypassing the temp file.
    assert seen["dst"] == str(isolated_cache)
    assert seen["src"].startswith(str(isolated_cache.parent / f".{isolated_cache.name}."))
    assert seen["src"].endswith(".tmp")


def test_substring_match_against_cache(isolated_cache):
    """Date-suffixed model identifiers hit the bare base ID via substring."""
    _write_cache(isolated_cache, {"claude-opus-4-7": 1_000_000})
    result = models.max_context_for("claude-opus-4-7-20260601")
    assert result == 1_000_000


def test_substring_match_longest_wins(isolated_cache):
    """When two keys both substring-match, the longer wins."""
    _write_cache(isolated_cache, {
        "claude-opus": 500_000,
        "claude-opus-4-7": 1_000_000,
    })
    result = models.max_context_for("claude-opus-4-7")
    assert result == 1_000_000


def test_filesystem_write_failure_swallowed(isolated_cache, monkeypatch):
    """If the cache file can't be written (permissions etc), the fetched
    value still serves the current caller — same posture as network
    failures."""
    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(models, "atomic_write_json", boom)
    with patch.object(models, "_fetch_from_litellm",
                      return_value={"claude-opus-4-7": 1_000_000}):
        result = models.max_context_for("claude-opus-4-7")
    assert result == 1_000_000
    assert not isolated_cache.exists()
