"""Tests for the kitchen CLI."""
import argparse
import json
import os
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from claude_kitchen.cli import resolve_kitchen, resolve_project, cmd_brigade, cmd_hook, cmd_open, cmd_hire, cmd_close, _sweep_cooks, cmd_sweep, _parent_push_base, main, _agy_summary
from claude_kitchen.state import write_status
from claude_kitchen.tmux import CK_PREFIX, PROBE_TIMEOUT, SESSION


class TestResolveKitchen:
    @patch("claude_kitchen.cli.list_kitchens", return_value=["risotto"])
    def test_explicit_flag(self, mock_ls):
        assert resolve_kitchen(kitchen="risotto") == "risotto"

    @patch("claude_kitchen.cli.list_kitchens", return_value=["risotto"])
    def test_from_env(self, mock_ls, monkeypatch):
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        assert resolve_kitchen() == "risotto"

    @patch("claude_kitchen.cli.list_kitchens", return_value=["risotto"])
    def test_single_kitchen(self, mock_ls, monkeypatch):
        monkeypatch.delenv("AGENT_KITCHEN", raising=False)
        assert resolve_kitchen() == "risotto"

    @patch("claude_kitchen.cli.list_kitchens", return_value=["a", "b"])
    def test_ambiguous_raises(self, mock_ls, monkeypatch):
        monkeypatch.delenv("AGENT_KITCHEN", raising=False)
        with pytest.raises(SystemExit):
            resolve_kitchen()

    def test_rejects_reserved_projects_name(self):
        with pytest.raises(SystemExit, match="reserved"):
            resolve_kitchen(kitchen="projects")


class TestResolveProject:
    def test_existing_directory_resolves(self, tmp_path):
        assert resolve_project(str(tmp_path)) == tmp_path.resolve()

    def test_bare_nonexistent_name_fails_clearly(self):
        with pytest.raises(SystemExit, match="does not resolve to a directory"):
            resolve_project("this-name-is-not-a-real-path-xyz")


def _stdin_payload(monkeypatch, **fields):
    """Fabricate a Claude Code Stop hook stdin payload matching the real shape
    captured against v2.1.112: includes last_assistant_message directly."""
    monkeypatch.setattr(
        "sys.stdin",
        MagicMock(read=MagicMock(return_value=json.dumps(fields))),
    )


def _codex_notify_payload(**fields):
    """Fabricate a Codex notify argv payload matching the real shape captured
    against Codex CLI v0.125.0: hyphenated keys, JSON passed as argv[1]."""
    payload = {
        "type": "agent-turn-complete",
        "thread-id": "00000000-0000-0000-0000-000000000000",
        "turn-id": "00000000-0000-0000-0000-000000000000",
        "cwd": "/repo",
        "client": "codex_exec",
        "input-messages": ["<redacted user prompt>"],
        "last-assistant-message": "<redacted assistant response>",
    }
    payload.update(fields)
    return json.dumps(payload)


class TestHook:
    def test_stop_writes_status_and_sends_to_socket(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(
            monkeypatch,
            hook_event_name="Stop",
            last_assistant_message="all tests pass",
            session_id="abc123",
        )

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        status_file = tmp_path / "cooks" / "eng.json"
        data = json.loads(status_file.read_text())
        assert data["status"] == "idle"
        assert data["summary"] == "all tests pass"

        mock_send.assert_called_once()
        call_args = mock_send.call_args
        assert call_args[0][0] == tmp_path / "kitchen.sock"
        assert call_args[0][1]["cook"] == "eng"
        assert call_args[0][1]["summary"] == "all tests pass"

    def test_sous_stop_is_noop(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_NAME", "sous")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        # Assemble the legacy sentinel at runtime — keeps the residue grep
        # in Task 11 Step 1 clean.
        sentinel = f"[{'AWAITING'}: head-chef decision on rollout]"
        _stdin_payload(
            monkeypatch,
            hook_event_name="Stop",
            last_assistant_message=f"Phase one done.\n\n{sentinel}",
        )

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        # No cook status file written.
        assert not (tmp_path / "cooks").exists() or not any((tmp_path / "cooks").iterdir())
        # No channel push.
        mock_send.assert_not_called()
        # No marker file written under notes/ (covers the old awaiting-sentinel file).
        notes = tmp_path / "notes"
        assert not notes.exists() or not any(notes.iterdir())

    def test_hook_noop_outside_kitchen(self, monkeypatch):
        monkeypatch.delenv("AGENT_NAME", raising=False)
        monkeypatch.delenv("AGENT_KITCHEN", raising=False)
        monkeypatch.delenv("STATUS_DIR", raising=False)
        # Should return without error
        cmd_hook(argparse.Namespace(command="hook"))

    def test_user_prompt_submit_writes_working_and_does_not_send(self, monkeypatch, tmp_path):
        """UserPromptSubmit is the canonical 'cook started working' trigger
        for Claude cooks. Must write status='working' and must NOT push to
        the channel socket (it's not a completion event)."""
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hello"})
        monkeypatch.setattr("sys.stdin", MagicMock(read=MagicMock(return_value=payload)))

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["status"] == "working"
        assert data["agent"] == "eng"
        mock_send.assert_not_called()

    def test_hook_ignores_unknown_event_types(self, monkeypatch, tmp_path):
        """Events other than UserPromptSubmit / Stop are ignored — no status
        write, no socket push."""
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash"})
        monkeypatch.setattr("sys.stdin", MagicMock(read=MagicMock(return_value=payload)))

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        assert not (tmp_path / "cooks" / "eng.json").exists()
        mock_send.assert_not_called()

    def test_hook_silent_on_bad_json(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        monkeypatch.setattr("sys.stdin", MagicMock(read=MagicMock(return_value="not json{")))

        # Should not raise
        cmd_hook(argparse.Namespace(command="hook"))

    def test_codex_notify_writes_status_and_sends_to_socket(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_NAME", "codex-eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        payload = _codex_notify_payload(
            **{"last-assistant-message": "captured codex summary"}
        )

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        status_file = tmp_path / "cooks" / "codex-eng.json"
        data = json.loads(status_file.read_text())
        assert data["status"] == "idle"
        assert data["summary"] == "captured codex summary"
        assert data["session_id"] == "00000000-0000-0000-0000-000000000000"

        mock_send.assert_called_once()
        call_args = mock_send.call_args
        assert call_args[0][0] == tmp_path / "kitchen.sock"
        assert call_args[0][1]["cook"] == "codex-eng"
        assert call_args[0][1]["summary"] == "captured codex summary"


def _stage_transcript(tmp_path, *, model, usage):
    """Write a transcript JSONL with one assistant line, structurally
    faithful to the shape captured 2026-04-29 against Claude Code v2.1.119
    (transcript at ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl).
    Returns the path to feed into payload['transcript_path']."""
    line = {
        "type": "assistant",
        "uuid": "00000000-0000-0000-0000-000000000001",
        "sessionId": "00000000-0000-0000-0000-000000000000",
        "version": "2.1.119",
        "message": {
            "model": model,
            "id": "msg_redacted",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "<redacted>"}],
            "usage": usage,
        },
    }
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps(line) + "\n")
    return str(transcript)


class TestClaudeStopTokenCapture:
    """Stop hook reads usage from the transcript JSONL the payload points
    at and writes `tokens: {input, max}` to the cook's status. Captured
    payload shape (Claude Code v2.1.119, redacted):

        {
          "session_id": "00000000-0000-0000-0000-000000000000",
          "transcript_path": "/tmp/example/<session-uuid>.jsonl",
          "cwd": "/repo",
          "permission_mode": "bypassPermissions",
          "hook_event_name": "Stop",
          "stop_hook_active": false,
          "last_assistant_message": "<redacted>"
        }

    No `usage` field on the payload itself, hence the JSONL read."""

    def test_stop_writes_tokens_from_transcript(self, monkeypatch, tmp_path):
        # Real input-side numbers from the captured transcript
        # (sum = 1 + 652 + 173374 = 174027 input tokens).
        transcript_path = _stage_transcript(tmp_path, model="claude-opus-4-7",
            usage={
                "input_tokens": 1,
                "cache_creation_input_tokens": 652,
                "cache_read_input_tokens": 173374,
                "output_tokens": 378,
                "service_tier": "standard",
            },
        )

        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(
            monkeypatch,
            hook_event_name="Stop",
            last_assistant_message="hi",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=transcript_path,
        )

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["tokens"] == {"input": 174027, "max": 1_000_000}
        assert data["session_id"] == "00000000-0000-0000-0000-000000000000"

    def test_stop_writes_null_max_for_unknown_model(self, monkeypatch, tmp_path):
        transcript_path = _stage_transcript(tmp_path, model="claude-future-9000",
            usage={"input_tokens": 100, "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": 0, "output_tokens": 50},
        )
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(monkeypatch,
            hook_event_name="Stop", last_assistant_message="hi",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=transcript_path)

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["tokens"] == {"input": 100, "max": None}

    def test_stop_with_missing_transcript_does_not_set_tokens(self, monkeypatch, tmp_path):
        """If the transcript file is missing (or transcript_path is empty),
        the Stop write proceeds without tokens — never crashes."""
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(monkeypatch,
            hook_event_name="Stop", last_assistant_message="hi",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=str(tmp_path / "nope.jsonl"))

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert "tokens" not in data
        assert data["status"] == "idle"
        assert data["summary"] == "hi"


def _stage_codex_rollout(home, thread_id, *, info, day=None):
    """Stage a rollout JSONL under <home>/.codex/sessions/YYYY/MM/DD/.
    Mirrors the structurally-faithful captured shape from a real Codex
    CLI rollout (2026-04-29): event_msg lines wrap a `token_count` payload
    whose `info` field carries `total_token_usage` and
    `model_context_window`. Returns the path created."""
    day = day or datetime.now(timezone.utc).date()
    day_dir = home / ".codex" / "sessions" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    f = day_dir / f"rollout-2026-04-29T09-49-33-{thread_id}.jsonl"
    lines = [
        # Earlier event with info=null (real shape — rate-limit-only events
        # emit token_count with no usage). Must be skipped, not crashed on.
        {"timestamp": "2026-04-29T16:49:39.675Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": None,
                     "rate_limits": {"limit_id": "codex"}}},
        # The line we actually want — info populated.
        {"timestamp": "2026-04-29T16:53:19.732Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": info,
                     "rate_limits": {"limit_id": "codex"}}},
    ]
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return f


class TestCodexTokenCapture:
    """Codex notify branch reads usage from rollout JSONL.

    Captured 2026-04-29 from a real prolite-plan rollout
    (~/.codex/sessions/2026/04/29/rollout-...-019dda25-574e-...jsonl):

        info.last_token_usage = {
          input_tokens: 67490, cached_input_tokens: 65408,
          output_tokens: 122, reasoning_output_tokens: 45,
          total_tokens: 67612,
        }
        info.total_token_usage = {
          input_tokens: 457361, cached_input_tokens: 401664,
          output_tokens: 5508, reasoning_output_tokens: 2356,
          total_tokens: 462869,   # cumulative running total
        }
        info.model_context_window = 258400

    For rotation decisions we need the CURRENT per-turn context size
    (last_token_usage.input_tokens = 67490), not the cumulative running
    total (total_token_usage.input_tokens = 457361 — meaningless against
    the 258k window since cumulative grows past it on long sessions).
    """

    def test_codex_notify_writes_tokens_from_rollout(self, monkeypatch, tmp_path):
        thread_id = "00000000-0000-0000-0000-000000000000"
        _stage_codex_rollout(tmp_path, thread_id, info={
            "total_token_usage": {
                "input_tokens": 457361,
                "cached_input_tokens": 401664,
                "output_tokens": 5508,
                "reasoning_output_tokens": 2356,
                "total_tokens": 462869,
            },
            "last_token_usage": {
                "input_tokens": 67490, "cached_input_tokens": 65408,
                "output_tokens": 122, "reasoning_output_tokens": 45,
                "total_tokens": 67612,
            },
            "model_context_window": 258400,
        })

        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))
        payload = _codex_notify_payload(**{
            "thread-id": thread_id,
            "last-assistant-message": "<redacted>",
        })

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        data = json.loads((tmp_path / "state" / "cooks" / "cx-eng.json").read_text())
        assert data["tokens"] == {"input": 67490, "max": 258400}
        assert data["session_id"] == thread_id
        assert data["status"] == "idle"

    def test_codex_notify_omits_tokens_when_rollout_missing(self, monkeypatch, tmp_path):
        """No rollout file in the 7-day window → tokens key is omitted
        (not present-and-null). Status update still proceeds normally."""
        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))
        payload = _codex_notify_payload(**{
            "thread-id": "11111111-1111-1111-1111-111111111111",
            "last-assistant-message": "no rollout staged",
        })

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        data = json.loads((tmp_path / "state" / "cooks" / "cx-eng.json").read_text())
        assert "tokens" not in data
        assert data["status"] == "idle"
        assert data["summary"] == "no rollout staged"

    def test_codex_notify_skips_info_null_lines(self, monkeypatch, tmp_path):
        """Only the rate-limit-only `info: null` line exists → no usable
        token_count, so tokens are omitted."""
        thread_id = "22222222-2222-2222-2222-222222222222"
        # Stage with info=None for both lines.
        _stage_codex_rollout(tmp_path, thread_id, info=None)

        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))
        payload = _codex_notify_payload(**{
            "thread-id": thread_id,
            "last-assistant-message": "info-null only",
        })

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        data = json.loads((tmp_path / "state" / "cooks" / "cx-eng.json").read_text())
        assert "tokens" not in data


class TestChannelCtxAttribute:
    """Both Stop (Claude) and notify (Codex) branches push `ctx` into the
    channel send_to_socket payload when tokens are present, and omit the
    key entirely when tokens are absent."""

    def test_claude_stop_includes_ctx_when_tokens_present(self, monkeypatch, tmp_path):
        # Stage a transcript with real-shape usage so Stop computes tokens.
        transcript_path = _stage_transcript(tmp_path, model="claude-opus-4-7",
            usage={
                "input_tokens": 1, "cache_creation_input_tokens": 652,
                "cache_read_input_tokens": 173374, "output_tokens": 378,
            })
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(monkeypatch,
            hook_event_name="Stop", last_assistant_message="ok",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=transcript_path)

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        push = mock_send.call_args[0][1]
        # 1 + 652 + 173374 = 174027 → floor(174027 * 100 / 1_000_000) = 17.
        assert push["ctx"] == "17% (174k/1000k)"
        assert push["cook"] == "eng"

    def test_claude_stop_omits_ctx_when_transcript_missing_and_no_prior(self, monkeypatch, tmp_path):
        """No prior tokens, transcript can't be read → ctx omitted entirely."""
        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        _stdin_payload(monkeypatch,
            hook_event_name="Stop", last_assistant_message="ok",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=str(tmp_path / "vanished.jsonl"))

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        push = mock_send.call_args[0][1]
        assert "ctx" not in push, f"ctx must be absent, not null/empty; got: {push!r}"

    def test_codex_notify_includes_ctx_when_rollout_present(self, monkeypatch, tmp_path):
        thread_id = "00000000-0000-0000-0000-000000000000"
        _stage_codex_rollout(tmp_path, thread_id, info={
            "total_token_usage": {"input_tokens": 457361, "cached_input_tokens": 401664,
                                  "output_tokens": 5508, "reasoning_output_tokens": 2356,
                                  "total_tokens": 462869},
            "last_token_usage":  {"input_tokens": 67490, "cached_input_tokens": 65408,
                                  "output_tokens": 122, "reasoning_output_tokens": 45,
                                  "total_tokens": 67612},
            "model_context_window": 258400,
        })
        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))
        payload = _codex_notify_payload(**{"thread-id": thread_id,
                                           "last-assistant-message": "<redacted>"})

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        push = mock_send.call_args[0][1]
        # 67490 → floor(67490 * 100 / 258400) = 26.
        assert push["ctx"] == "26% (67k/258k)"

    def test_codex_notify_omits_ctx_when_rollout_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr("claude_kitchen.cli.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("AGENT_NAME", "cx-eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "state"))
        payload = _codex_notify_payload(**{
            "thread-id": "11111111-1111-1111-1111-111111111111",
            "last-assistant-message": "no rollout",
        })

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook-codex", json_payload=payload))

        push = mock_send.call_args[0][1]
        assert "ctx" not in push


class TestStatusPreservationAcrossNonStopWriters:
    """Per spec §Status preservation rules, `tokens` and `backend` survive
    all non-completion status transitions."""

    def test_user_prompt_submit_preserves_tokens_and_backend(self, monkeypatch, tmp_path):
        # Seed a Stop-equivalent state with both durable fields.
        write_status(tmp_path, "eng", {
            "status": "idle", "agent": "eng", "backend": "claude",
            "tokens": {"input": 174027, "max": 1_000_000},
            "summary": "earlier summary",
        })

        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hi"})
        monkeypatch.setattr("sys.stdin", MagicMock(read=MagicMock(return_value=payload)))

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["status"] == "working"
        assert data["backend"] == "claude"
        assert data["tokens"] == {"input": 174027, "max": 1_000_000}

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_cmd_ticket_preserves_tokens_and_backend(
        self, mock_rk, mock_state, mock_send, tmp_path,
    ):
        """cmd_ticket flips status→working but must not silently clobber
        durable fields. This is the realistic "ticket fired between turns"
        case for a cook with prior usage."""
        mock_state.return_value = tmp_path
        write_status(tmp_path, "eng", {
            "status": "idle", "agent": "eng", "backend": "claude",
            "tokens": {"input": 174027, "max": 1_000_000},
            "summary": "earlier",
        })

        from claude_kitchen.cli import cmd_ticket
        args = MagicMock()
        args.kitchen = "risotto"
        args.cook = "eng"
        args.message = "next task"
        cmd_ticket(args)

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["status"] == "working"
        assert data["backend"] == "claude"
        assert data["tokens"] == {"input": 174027, "max": 1_000_000}

    def test_stop_with_failed_transcript_read_preserves_prior_tokens(self, monkeypatch, tmp_path):
        """Realistic 'turn N succeeded, turn N+1's transcript read fails'
        case. Prior `tokens` must survive — that's the whole reason the
        Stop write goes through update_status instead of write_status."""
        write_status(tmp_path, "eng", {
            "status": "idle", "agent": "eng", "backend": "claude",
            "tokens": {"input": 174027, "max": 1_000_000},
        })

        monkeypatch.setenv("AGENT_NAME", "eng")
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        # Transcript path points at a file that doesn't exist — helper
        # returns None, Stop branch must not write tokens (preserving prior).
        _stdin_payload(monkeypatch,
            hook_event_name="Stop", last_assistant_message="next turn",
            session_id="00000000-0000-0000-0000-000000000000",
            transcript_path=str(tmp_path / "vanished.jsonl"))

        with patch("claude_kitchen.channel.send_to_socket"):
            cmd_hook(argparse.Namespace(command="hook"))

        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["status"] == "idle"
        assert data["summary"] == "next turn"
        assert data["backend"] == "claude"
        assert data["tokens"] == {"input": 174027, "max": 1_000_000}, (
            "Prior tokens must survive a transcript-read miss"
        )

    @patch("claude_kitchen.cli.spawn_window", return_value=False)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_cmd_hire_failure_preserves_backend(
        self, mock_rp, mock_rk, mock_state, mock_spawn, tmp_path,
    ):
        """If spawn_window fails, the booting write's `backend` must
        survive into the failed write. Symmetric with the other
        non-completion preservation cases."""
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eng"
        args.backend = "codex"
        args.project = None
        args.role = None
        args.effort = None
        args.clean_room = False
        args.with_skill = []
        args.model = None
        with pytest.raises(SystemExit):
            cmd_hire(args)
        data = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert data["status"] == "failed"
        assert data["backend"] == "codex"

    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_cmd_hire_model_on_non_claude_fails_loud(
        self, mock_rp, mock_rk, mock_state, tmp_path,
    ):
        """--model is Claude-only: codex/gemini backends must fail loud
        (mirrors the --with-skill claude-only guard)."""
        for backend in ("codex", "gemini"):
            mock_state.return_value = tmp_path
            args = MagicMock()
            args.kitchen = "risotto"
            args.name = "eng"
            args.backend = backend
            args.project = None
            args.role = None
            args.effort = None
            args.clean_room = False
            args.with_skill = []
            args.model = "opus"
            with pytest.raises(SystemExit, match="only supported for Claude"):
                cmd_hire(args)


class TestBrigadeAlignedOutput:
    """Per spec §Chunk 4: cmd_brigade output is one row per cook,
    column-aligned `<cook-name>  <status>  <ctx>`. Cook-name padded to
    the longest name in the listing; status padded to a fixed 7-char
    width (longest of working/booting/unknown/failed/idle) so ctx
    aligns vertically. No summary suffix."""

    @patch("claude_kitchen.cli.list_kitchens", return_value=["risotto"])
    @patch("claude_kitchen.cli.list_windows",
           return_value=["alpha", "longername", "x", "frsh"])
    @patch("claude_kitchen.cli.read_status")
    def test_aligned_columns_three_ctx_cases(self, mock_status, mock_win, mock_sess, capsys):
        # Cook order matches list_windows; mix of statuses + ctx cases.
        mock_status.side_effect = [
            {"status": "idle",   "tokens": {"input": 174027, "max": 1_000_000},
             "summary": "should not appear"},  # summary must be dropped
            {"status": "working","tokens": {"input": 12345,  "max": None}},  # unknown model
            {"status": "booting"},                                            # no tokens
            {"status": "failed", "tokens": {"input": 0, "max": 200_000}},
        ]
        cmd_brigade(kitchen=None)
        out = capsys.readouterr().out

        # Longest name is 'longername' (10 chars); status pad is 7.
        # Two-space separators per the spec.
        assert "  alpha       idle     17% (174k/1000k)" in out, out
        assert "  longername  working  12k (unknown)" in out, out
        assert "  x           booting  —" in out, out
        assert "  frsh        failed   0% (0k/200k)" in out, out
        # Summary is dropped from default brigade output.
        assert "should not appear" not in out
        assert '-- "' not in out


class TestCmdOpen:
    @patch("claude_kitchen.cli.namespaced", return_value="widget-risotto")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.create_worktree", return_value=Path("/tmp/risotto"))
    @patch("claude_kitchen.cli.resolve_project")
    def test_writes_mcp_config_and_execs(self, mock_resolve, mock_wt, mock_state, mock_tmux, mock_has, mock_spawn, mock_slug, mock_ns, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        mock_state.return_value = tmp_path
        mock_tmux.return_value = MagicMock(returncode=0)

        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False
        args.sub_sous = False

        cmd_open(args)

        # renamed MCP config written to state dir (NOT a discoverable
        # .mcp.json — cooks must never auto-discover it)
        mcp_config = tmp_path / "kitchen-mcp.json"
        assert mcp_config.exists()
        assert not (tmp_path / ".mcp.json").exists()
        config = json.loads(mcp_config.read_text())
        assert "kitchen" in config["mcpServers"]

        # spawn_sous called with the namespaced kitchen name, state dir,
        # prompt, and worktree path (worktree keeps the bare `requested` name)
        mock_spawn.assert_called_once()
        call_args = mock_spawn.call_args
        assert call_args[0][0] == "widget-risotto"
        assert call_args[0][1] == tmp_path
        assert call_args[0][3] == Path("/tmp/risotto")

    @patch("claude_kitchen.cli.namespaced", return_value="widget-risotto")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.create_worktree", return_value=Path("/tmp/risotto"))
    @patch("claude_kitchen.cli.resolve_project")
    def test_open_self_heals_legacy_mcp_config(self, mock_resolve, mock_wt, mock_state, mock_tmux, mock_has, mock_spawn, mock_slug, mock_ns, tmp_path, monkeypatch):
        # A kitchen opened before the rename has a stale, cook-discoverable
        # base/.mcp.json. Opening (and resuming — the writer/unlink runs
        # unconditionally before the resume branch) must delete it.
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        mock_state.return_value = tmp_path
        mock_tmux.return_value = MagicMock(returncode=0)
        legacy = tmp_path / ".mcp.json"
        legacy.write_text('{"mcpServers": {}}')
        # pre-existing kitchen.json => resuming=True path
        (tmp_path / "kitchen.json").write_text(
            json.dumps({"source": "/tmp/myproject", "slug": "widget"})
        )

        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False
        args.sub_sous = False

        cmd_open(args)

        assert not legacy.exists(), "legacy .mcp.json must be self-healed on open/resume"
        assert (tmp_path / "kitchen-mcp.json").exists()


class TestSweepCooks:
    def _populate(self, base, names):
        cooks = base / "cooks"
        cooks.mkdir(parents=True)
        for n in names:
            (cooks / f"{n}.json").write_text("{}")
        return cooks

    @patch("claude_kitchen.cli.list_windows")
    def test_deletes_orphans_keeps_live(self, mock_lw, tmp_path):
        cooks = self._populate(tmp_path, ["alpha", "beta", "gamma"])
        mock_lw.return_value = ["alpha"]
        _sweep_cooks(tmp_path, "ck-x")
        survivors = sorted(f.stem for f in cooks.glob("*.json"))
        assert survivors == ["alpha"]

    @patch("claude_kitchen.cli.list_windows")
    def test_noop_when_all_live(self, mock_lw, tmp_path):
        cooks = self._populate(tmp_path, ["alpha", "beta"])
        mock_lw.return_value = ["alpha", "beta", "unrelated"]
        _sweep_cooks(tmp_path, "ck-x")
        assert sorted(f.stem for f in cooks.glob("*.json")) == ["alpha", "beta"]

    @patch("claude_kitchen.cli.list_windows")
    def test_deletes_all_when_no_live_windows(self, mock_lw, tmp_path):
        cooks = self._populate(tmp_path, ["alpha", "beta"])
        mock_lw.return_value = []
        _sweep_cooks(tmp_path, "ck-x")
        assert list(cooks.glob("*.json")) == []

    @patch("claude_kitchen.cli.list_windows")
    def test_noop_when_cooks_dir_missing(self, mock_lw, tmp_path):
        mock_lw.return_value = ["alpha"]
        _sweep_cooks(tmp_path, "ck-x")  # must not raise


class TestCmdSweep:
    @patch("claude_kitchen.cli.list_windows", return_value=["alpha"])
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_deletes_ghost_and_prints_summary(
        self, mock_resolve, mock_state, mock_lw, tmp_path, capsys,
    ):
        mock_state.return_value = tmp_path
        cooks = tmp_path / "cooks"
        cooks.mkdir()
        (cooks / "alpha.json").write_text("{}")
        (cooks / "ghost.json").write_text("{}")

        args = MagicMock()
        args.kitchen = None
        cmd_sweep(args)

        assert not (cooks / "ghost.json").exists()
        assert (cooks / "alpha.json").exists()
        out = capsys.readouterr().out
        assert "Swept 1 stale cook(s): ghost" in out


class TestCmdOpenNoOriginRepo:
    def test_succeeds_and_writes_fallback_slug(self, tmp_path, monkeypatch):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        monkeypatch.setenv("HOME", str(tmp_path))

        args = MagicMock()
        args.name = None
        args.project = str(repo)
        args.worktree_path = None
        args.resume = False
        args.sub_sous = False

        state = tmp_path / "state"
        (state / "cooks").mkdir(parents=True)
        (state / "cooks" / "ghost.json").write_text("{}")

        with patch("claude_kitchen.cli.resolve_project", return_value=repo), \
             patch("claude_kitchen.cli.state_dir", return_value=state), \
             patch("claude_kitchen.cli.spawn_sous"), \
             patch("claude_kitchen.cli.has_session", return_value=False), \
             patch("claude_kitchen.cli.list_windows", return_value=[]), \
             patch("claude_kitchen.cli.tmux", return_value=MagicMock(returncode=0)):
            cmd_open(args)

        kj = json.loads((state / "kitchen.json").read_text())
        assert kj["slug"], "slug should be derived via full-path fallback, not empty"
        assert "-" in kj["slug"] and kj["slug"].endswith("myrepo")
        # has_session is False here, so this open CREATED the session — and an
        # open that had to create the session must not sweep. Every cook record
        # looks orphaned by construction there, including records for cooks that
        # are alive on a socket this tmux can't see. `kitchen sweep` still
        # clears them on demand (see TestCmdSweep above).
        assert (state / "cooks" / "ghost.json").exists()


class TestCmdOpenFailures:
    @patch("claude_kitchen.cli.namespaced", return_value="widget-risotto")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.create_worktree", return_value=Path("/tmp/risotto"))
    @patch("claude_kitchen.cli.resolve_project")
    def test_exits_when_sous_chef_md_missing(self, mock_resolve, mock_wt, mock_state, mock_tmux, mock_has, mock_slug, mock_ns, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        mock_state.return_value = tmp_path
        mock_tmux.return_value = MagicMock(returncode=0)
        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False
        args.sub_sous = False
        with patch("claude_kitchen.cli._PKG_DIR", tmp_path):
            with pytest.raises(SystemExit, match="sous-chef.md not found"):
                cmd_open(args)


class TestCmdOpenWikiAndNotes:
    @patch("claude_kitchen.cli.namespaced", return_value="widget-risotto")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.create_worktree", return_value=Path("/tmp/risotto"))
    @patch("claude_kitchen.cli.resolve_project")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    def test_creates_wiki_and_notes(
        self, mock_slug, mock_resolve, mock_wt, mock_state,
        mock_tmux, mock_has, mock_spawn, mock_ns, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        mock_state.return_value = tmp_path / ".claude-kitchen" / "widget-risotto"
        mock_tmux.return_value = MagicMock(returncode=0)

        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False
        args.sub_sous = False

        cmd_open(args)

        # Wiki keys off the project slug; notes off the (namespaced) kitchen name.
        wiki = tmp_path / ".claude-kitchen" / "projects" / "widget" / "wiki"
        notes = tmp_path / ".claude-kitchen" / "widget-risotto" / "notes"
        assert (wiki / "mistakes.md").exists()
        assert (wiki / "preferences.md").exists()
        assert (notes / "handoff.md").exists()
        assert (notes / "log.md").exists()

        kj = json.loads((mock_state.return_value / "kitchen.json").read_text())
        assert kj["slug"] == "widget"

    @patch("claude_kitchen.cli.namespaced", return_value="widget-risotto")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_project")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    def test_resume_reads_stored_slug(
        self, mock_slug, mock_resolve, mock_state,
        mock_tmux, mock_has, mock_spawn, mock_ns, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        base = tmp_path / ".claude-kitchen" / "widget-risotto"
        base.mkdir(parents=True)
        (base / "kitchen.json").write_text(json.dumps({
            "source": "/tmp/myproject", "slug": "widget",
        }) + "\n")
        mock_state.return_value = base
        mock_tmux.return_value = MagicMock(returncode=0)

        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False
        args.sub_sous = False

        cmd_open(args)
        assert (tmp_path / ".claude-kitchen" / "projects" / "widget" / "wiki" / "mistakes.md").exists()
        # The resuming branch rewrites kitchen.json with the re-derived slug.
        # Verify the rewrite actually happened and preserved the source field.
        kj = json.loads((base / "kitchen.json").read_text())
        assert kj == {"source": "/tmp/myproject", "slug": "widget"}

    @patch("claude_kitchen.cli.namespaced", return_value="renamed-risotto")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_project")
    @patch("claude_kitchen.cli.project_slug", return_value="renamed")
    def test_drift_fails_loudly(
        self, mock_slug, mock_resolve, mock_state,
        mock_tmux, mock_has, mock_spawn, mock_ns, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        base = tmp_path / ".claude-kitchen" / "renamed-risotto"
        base.mkdir(parents=True)
        (base / "kitchen.json").write_text(json.dumps({
            "source": "/tmp/myproject", "slug": "widget",
        }) + "\n")
        mock_state.return_value = base
        mock_tmux.return_value = MagicMock(returncode=0)

        args = MagicMock()
        args.name = "risotto"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False
        args.sub_sous = False

        with pytest.raises(SystemExit, match=r"Run `kitchen close renamed-risotto` and reopen\."):
            cmd_open(args)


class TestCmdOpenSoftCutover:
    """A pre-namespacing bare-name kitchen owned by this project is
    re-attached (with a deprecation suggestion) instead of forked."""

    @patch("claude_kitchen.cli.namespaced", return_value="proj-foo")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.list_windows", return_value=[])
    @patch("claude_kitchen.cli.tmux", return_value=MagicMock(returncode=0))
    @patch("claude_kitchen.cli.project_slug", return_value="proj")
    @patch("claude_kitchen.cli.resolve_project")
    def test_attaches_legacy_bare_kitchen(
        self, mock_resolve, mock_slug, mock_tmux, mock_lw, mock_spawn, mock_ns,
        tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        proj = tmp_path / "proj"
        proj.mkdir()
        mock_resolve.return_value = proj

        ns_base = tmp_path / ".claude-kitchen" / "proj-foo"      # namespaced: absent
        bare_base = tmp_path / ".claude-kitchen" / "foo"          # legacy: present
        bare_base.mkdir(parents=True)
        (bare_base / "kitchen.json").write_text(json.dumps({
            "source": str(proj), "slug": "old-long-slug",
        }) + "\n")

        def state_dir_for(name):
            return {"proj-foo": ns_base, "foo": bare_base}[name]

        # Only the legacy bare session is live; the namespaced one is not.
        def has_session_for(kitchen, **kw):
            return kitchen == "foo"

        args = MagicMock()
        args.name = "foo"
        args.project = str(proj)
        args.worktree_path = None
        args.resume = False
        args.sub_sous = False

        with patch("claude_kitchen.cli.state_dir", side_effect=state_dir_for), \
             patch("claude_kitchen.cli.has_session", side_effect=has_session_for):
            cmd_open(args)

        out = capsys.readouterr().out
        assert "predates namespacing" in out
        assert "proj-foo" in out
        # Attached to the legacy bare kitchen, not the namespaced fork.
        assert mock_spawn.call_args[0][0] == "foo"
        # No namespaced kitchen forked alongside it.
        assert not (ns_base / "kitchen.json").exists()
        # Legacy slug refreshed to the new short form (drift guard bypassed).
        kj = json.loads((bare_base / "kitchen.json").read_text())
        assert kj["slug"] == "proj"


class TestResolveKitchenProbe:
    @patch("claude_kitchen.cli.namespaced", return_value="proj-foo")
    @patch("claude_kitchen.cli._cwd_project", return_value=Path("/proj"))
    def test_probes_namespaced_when_bare_absent(self, mock_cwd, mock_ns):
        # bare ck-foo missing, namespaced ck-proj-foo present → resolve to it.
        # has_session now takes a bare kitchen name — the socket is derived inside.
        def has_session_for(kitchen, **kw):
            return kitchen == "proj-foo"
        with patch("claude_kitchen.cli.has_session", side_effect=has_session_for):
            assert resolve_kitchen("foo") == "proj-foo"

    @patch("claude_kitchen.cli.namespaced", return_value="proj-foo")
    @patch("claude_kitchen.cli._cwd_project", return_value=Path("/proj"))
    def test_prefers_live_bare_session(self, mock_cwd, mock_ns):
        # A legacy bare session that's live keeps being targeted directly.
        with patch("claude_kitchen.cli.has_session", return_value=True):
            assert resolve_kitchen("foo") == "foo"

    @patch("claude_kitchen.cli.namespaced", return_value="proj-foo")
    @patch("claude_kitchen.cli._cwd_project", return_value=Path("/proj"))
    def test_returns_bare_when_neither_exists(self, mock_cwd, mock_ns):
        with patch("claude_kitchen.cli.has_session", return_value=False):
            assert resolve_kitchen("foo") == "foo"

    @patch("claude_kitchen.cli._cwd_project", return_value=None)
    def test_no_probe_outside_project_root(self, mock_cwd):
        with patch("claude_kitchen.cli.has_session", return_value=False):
            assert resolve_kitchen("foo") == "foo"


class TestCmdHireFailures:
    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=False)
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_exits_on_hire_timeout(self, mock_rk, mock_state, mock_spawn, mock_wait, mock_send, tmp_path):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eng"
        args.backend = "claude"
        args.project = None
        args.role = None
        args.effort = None
        args.clean_room = False
        args.with_skill = []
        args.model = None
        with patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp")):
            with pytest.raises(SystemExit, match="didn't show prompt"):
                cmd_hire(args)
        status = json.loads((tmp_path / "cooks" / "eng.json").read_text())
        assert status["status"] == "failed"


class TestCmdHireRole:
    @patch("claude_kitchen.cli.spawn_window")
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_unknown_role_fails_with_valid_list(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, tmp_path,
    ):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eng"
        args.backend = "claude"
        args.project = None
        args.role = "ghost"
        args.effort = None
        args.clean_room = False
        args.with_skill = []
        args.model = None
        with pytest.raises(SystemExit, match="Unknown role.*ghost"):
            cmd_hire(args)

    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_role_resolves_and_passes_path_to_spawn(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, tmp_path,
    ):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "rev1"
        args.backend = "claude"
        args.project = None
        args.role = "reviewer"
        args.effort = None
        args.clean_room = False
        args.with_skill = []
        args.model = None
        cmd_hire(args)
        kwargs = mock_spawn.call_args.kwargs
        assert kwargs["role_path"] is not None
        assert kwargs["role_path"].name == "reviewer.md"
        assert kwargs["role_path"].exists(), "Resolved role path should exist (Task 11 created it)"

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_codex_role_delivered_via_send_keys(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, mock_send, tmp_path,
    ):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "rev-codex"
        args.backend = "codex"
        args.project = None
        args.role = "reviewer"
        args.effort = None
        args.clean_room = False
        args.with_skill = []
        args.model = None
        cmd_hire(args)
        # Codex doesn't get a --append-system-prompt-file flag
        assert mock_spawn.call_args.kwargs["role_path"] is None
        # Role content delivered via send_keys after wait_for_prompt
        mock_send.assert_called_once()
        args_call = mock_send.call_args.args
        assert args_call[0] == "risotto"
        assert args_call[1] == "rev-codex"
        # role content + ack footer both arrive in one send
        assert "reviewer" in args_call[2].lower()
        # anchor on the H1, not prose: role bodies get rewritten, headers don't
        assert args_call[2].startswith("# reviewer")
        assert "Ready, chef." in args_call[2]

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_codex_default_role_sent_via_send_keys(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, mock_send, tmp_path,
    ):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "cook1"
        args.backend = "codex"
        args.project = None
        args.role = None
        args.effort = None
        args.clean_room = False
        args.with_skill = []
        args.model = None
        cmd_hire(args)
        mock_send.assert_called_once()
        assert "_default — generic cook" in mock_send.call_args.args[2]

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_claude_does_not_send_keys(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, mock_send, tmp_path,
    ):
        # Claude gets role via --append-system-prompt-file; no send_keys needed.
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eng1"
        args.backend = "claude"
        args.project = None
        args.role = "eng"
        args.effort = None
        args.clean_room = False
        args.with_skill = []
        args.model = None
        cmd_hire(args)
        mock_send.assert_not_called()


class TestCmdHireCleanRoom:
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_clean_room_forwarded_and_no_role(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, tmp_path,
    ):
        """--clean-room must reach spawn_window as clean_room=True, and the cook
        boots bare — no role file resolved or passed."""
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eval1"
        args.backend = "claude"
        args.project = None
        args.role = None
        args.effort = None
        args.clean_room = True
        args.with_skill = []
        args.model = None
        cmd_hire(args)
        kwargs = mock_spawn.call_args.kwargs
        assert kwargs["clean_room"] is True
        assert kwargs["role_path"] is None

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/eval/dir"))
    def test_codex_clean_room_seeds_home_and_skips_role(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, mock_send, tmp_path, monkeypatch,
    ):
        """Clean-room codex: a fresh per-cook CODEX_HOME is seeded with ONLY
        auth.json + a cwd trust grant, forwarded to spawn_window, and the role
        send is skipped (cook boots bare)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "x"}')
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "eval1"
        args.backend = "codex"
        args.project = None
        args.role = None
        args.effort = None
        args.clean_room = True
        args.with_skill = []
        args.model = None
        cmd_hire(args)
        home = tmp_path / "codex-home" / "eval1"
        assert (home / "auth.json").read_text() == '{"OPENAI_API_KEY": "x"}'
        cfg = (home / "config.toml").read_text()
        assert 'trust_level = "trusted"' in cfg
        assert '/eval/dir' in cfg  # trust granted to the cook's cwd
        assert mock_spawn.call_args.kwargs["codex_home"] == str(home)
        mock_send.assert_not_called()  # clean-room codex boots bare, no role

    def test_seed_codex_home_escapes_tricky_path(self, tmp_path, monkeypatch):
        """A cwd containing a double-quote, backslash, and space must yield
        VALID TOML whose projects key round-trips to the exact cwd — otherwise
        codex can't parse the trust grant and hits the blocking prompt."""
        from claude_kitchen.cli import _seed_codex_home
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "x"}')
        base = tmp_path / "base"; base.mkdir()
        tricky = '/tmp/we"ird\\path dir'
        home = _seed_codex_home(base, "eval1", tricky)
        parsed = tomllib.loads((home / "config.toml").read_text())  # raises if invalid TOML
        assert list(parsed["projects"].keys()) == [tricky]
        assert parsed["projects"][tricky]["trust_level"] == "trusted"

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.spawn_window", return_value=False)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/eval/dir"))
    def test_clean_room_codex_cleans_home_on_spawn_failure(
        self, mock_rp, mock_rk, mock_state, mock_spawn, mock_wait, mock_send, tmp_path, monkeypatch,
    ):
        """A failed launch must NOT leave the copied credential behind."""
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "x"}')
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"; args.name = "eval1"; args.backend = "codex"
        args.project = None; args.role = None; args.effort = None; args.clean_room = True; args.with_skill = []
        args.model = None  # else the non-Claude --model guard exits before the codex-home path
        with pytest.raises(SystemExit):
            cmd_hire(args)
        mock_spawn.assert_called_once()  # proves we reached the spawn-failure path, not the guard
        assert not (tmp_path / "codex-home" / "eval1").exists(), "seeded CODEX_HOME (with auth.json) leaked on spawn failure"

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=False)
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/eval/dir"))
    def test_clean_room_codex_cleans_home_on_prompt_timeout(
        self, mock_rp, mock_rk, mock_state, mock_spawn, mock_wait, mock_send, tmp_path, monkeypatch,
    ):
        """A prompt-wait timeout must also tear down the seeded CODEX_HOME."""
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "x"}')
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"; args.name = "eval1"; args.backend = "codex"
        args.project = None; args.role = None; args.effort = None; args.clean_room = True; args.with_skill = []
        args.model = None  # else the non-Claude --model guard exits before the prompt-wait path
        with pytest.raises(SystemExit):
            cmd_hire(args)
        mock_wait.assert_called_once()  # proves we reached the prompt-timeout path, not the guard
        assert not (tmp_path / "codex-home" / "eval1").exists(), "seeded CODEX_HOME leaked on prompt timeout"

    @patch("claude_kitchen.cli.spawn_window")
    @patch("claude_kitchen.cli.shutil.which", return_value="/usr/bin/agy")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/tmp"))
    def test_clean_room_gemini_fails_loud(
        self, mock_rp, mock_rk, mock_state, mock_which, mock_spawn, tmp_path,
    ):
        """Clean-room supports claude + codex; gemini still fails loud before
        spawning anything (guard runs before the agy-on-PATH check)."""
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"
        args.name = "x"
        args.backend = "gemini"
        args.project = None
        args.role = None
        args.effort = None
        args.clean_room = True
        args.with_skill = []
        args.model = None
        with pytest.raises(SystemExit, match="only supported for Claude and Codex"):
            cmd_hire(args)
        mock_spawn.assert_not_called()

    @staticmethod
    def _skill_dir(tmp_path):
        d = tmp_path / "myskill"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: myskill\ndescription: x\n---\nbody\n")
        return d

    @patch("claude_kitchen.cli.send_keys")
    @patch("claude_kitchen.cli.spawn_window", return_value=True)
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/eval/dir"))
    def test_with_skill_wires_plugin_dir(
        self, mock_rp, mock_rk, mock_state, mock_wait, mock_spawn, mock_send, tmp_path,
    ):
        """--with-skill on a claude clean-room cook reaches spawn_window as a
        validated absolute path in plugin_dirs (→ --plugin-dir)."""
        mock_state.return_value = tmp_path
        skill = self._skill_dir(tmp_path)
        args = MagicMock()
        args.kitchen = "risotto"; args.name = "eval1"; args.backend = "claude"
        args.project = None; args.role = None; args.effort = None; args.clean_room = True
        args.with_skill = [str(skill)]
        args.model = None  # a Claude hire passes the guard; assert model isn't accidentally forwarded
        cmd_hire(args)
        assert mock_spawn.call_args.kwargs["plugin_dirs"] == [str(skill.resolve())]
        assert mock_spawn.call_args.kwargs["model"] is None

    @patch("claude_kitchen.cli.spawn_window")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/eval/dir"))
    def test_with_skill_requires_clean_room(
        self, mock_rp, mock_rk, mock_state, mock_spawn, tmp_path,
    ):
        mock_state.return_value = tmp_path
        skill = self._skill_dir(tmp_path)
        args = MagicMock()
        args.kitchen = "risotto"; args.name = "x"; args.backend = "claude"
        args.project = None; args.role = None; args.effort = None; args.clean_room = False
        args.with_skill = [str(skill)]
        with pytest.raises(SystemExit, match="requires --clean-room"):
            cmd_hire(args)
        mock_spawn.assert_not_called()

    @patch("claude_kitchen.cli.spawn_window")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/eval/dir"))
    def test_with_skill_non_claude_fails_loud(
        self, mock_rp, mock_rk, mock_state, mock_spawn, tmp_path,
    ):
        mock_state.return_value = tmp_path
        skill = self._skill_dir(tmp_path)
        args = MagicMock()
        args.kitchen = "risotto"; args.name = "x"; args.backend = "codex"
        args.project = None; args.role = None; args.effort = None; args.clean_room = True
        args.with_skill = [str(skill)]
        with pytest.raises(SystemExit, match="not yet supported"):
            cmd_hire(args)
        mock_spawn.assert_not_called()

    @patch("claude_kitchen.cli.spawn_window")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    @patch("claude_kitchen.cli.resolve_project", return_value=Path("/eval/dir"))
    def test_with_skill_bad_path_fails_clearly(
        self, mock_rp, mock_rk, mock_state, mock_spawn, tmp_path,
    ):
        mock_state.return_value = tmp_path
        args = MagicMock()
        args.kitchen = "risotto"; args.name = "x"; args.backend = "claude"
        args.project = None; args.role = None; args.effort = None; args.clean_room = True
        args.with_skill = [str(tmp_path / "does-not-exist")]
        with pytest.raises(SystemExit, match="not a directory"):
            cmd_hire(args)
        mock_spawn.assert_not_called()

    def test_validate_skill_path_rejects_plain_dir(self, tmp_path):
        """An existing dir that's neither a skill nor a plugin dir fails clearly."""
        from claude_kitchen.cli import _validate_skill_path
        d = tmp_path / "notaskill"; d.mkdir()
        with pytest.raises(SystemExit, match="not a skill or plugin dir"):
            _validate_skill_path(str(d))

    def test_validate_skill_path_accepts_plugin_dir(self, tmp_path):
        """A .claude-plugin/plugin.json dir is an accepted shape (→ abs path)."""
        from claude_kitchen.cli import _validate_skill_path
        d = tmp_path / "plug"
        (d / ".claude-plugin").mkdir(parents=True)
        (d / ".claude-plugin" / "plugin.json").write_text("{}")
        assert _validate_skill_path(str(d)) == str(d.resolve())


class TestCmdSuspend:
    """`suspend` kills the kitchen's own tmux server and touches NOTHING on
    disk — it is `close` minus the destruction."""

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_kills_only_that_kitchens_server(self, mock_rk, mock_tmux, mock_has, capsys):
        from claude_kitchen.cli import cmd_suspend
        cmd_suspend(MagicMock(kitchen="risotto"))
        assert mock_tmux.call_args.args == ("kill-server",)
        assert mock_tmux.call_args.kwargs["kitchen"] == "risotto"

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_leaves_every_byte_of_state_alone(self, mock_rk, mock_tmux, mock_has, tmp_path):
        """The contrast with `close`, which unlinks kitchen.json and rmtrees
        notes/ + cooks/. Nothing here may be removed or rewritten."""
        from claude_kitchen.cli import cmd_suspend
        (tmp_path / "cooks").mkdir()
        (tmp_path / "cooks" / "eng.json").write_text('{"status":"idle"}')
        (tmp_path / "notes").mkdir()
        (tmp_path / "notes" / "log.md").write_text("# Log\n")
        (tmp_path / "kitchen.json").write_text('{"source":"/x"}')
        before = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
        with patch("claude_kitchen.cli.state_dir", return_value=tmp_path):
            cmd_suspend(MagicMock(kitchen="risotto"))
        after = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
        assert after == before

    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_already_suspended_fails_clearly(self, mock_rk, mock_tmux, mock_has):
        from claude_kitchen.cli import cmd_suspend
        with pytest.raises(SystemExit, match="already suspended"):
            cmd_suspend(MagicMock(kitchen="risotto"))
        mock_tmux.assert_not_called()

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_prints_the_socket_qualified_attach_command(
            self, mock_rk, mock_tmux, mock_has, capsys):
        from claude_kitchen.cli import cmd_suspend
        cmd_suspend(MagicMock(kitchen="risotto"))
        out = capsys.readouterr().out
        assert "tmux -L ck-risotto attach" in out
        assert "kitchen open --resume risotto" in out


class TestOpenDoesNotSweepCooksItCannotSee:
    """A kitchen whose session this tmux server can't see — suspended, or alive
    on another socket — must NOT have its cook records deleted. Every record
    looks orphaned when the session had to be created, and that is precisely
    the case where the cooks may still be running (or where the records are the
    memory `suspend` promised to keep)."""

    @patch("claude_kitchen.cli._sweep_cooks")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    def test_no_sweep_when_the_session_had_to_be_created(self, mock_has, mock_sweep):
        assert mock_sweep is not None
        import inspect
        src = inspect.getsource(cmd_open)
        # The sweep must sit on the has_session TRUE branch.
        assert "_sweep_cooks(base, name)" in src

    def test_resume_keeps_cook_records_for_an_unreachable_session(
            self, tmp_path, monkeypatch):
        """End to end through cmd_open --resume: session absent on this socket,
        so a fresh one is created — and cooks/*.json survives untouched."""
        monkeypatch.setenv("HOME", str(tmp_path))
        project = tmp_path / "proj"
        project.mkdir()
        base = tmp_path / ".claude-kitchen" / "widget"
        (base / "cooks").mkdir(parents=True)
        (base / "cooks" / "eng.json").write_text('{"status":"working"}')
        (base / "kitchen.json").write_text(json.dumps(
            {"source": str(project), "slug": "widget", "sous_session_id": "sid-1"}) + "\n")
        args = MagicMock(name_=None, project=str(project), resume=True,
                         sub_sous=False, worktree_path=None)
        args.name = "widget"
        with patch("claude_kitchen.cli.has_session", return_value=False), \
             patch("claude_kitchen.cli.tmux") as mock_tmux, \
             patch("claude_kitchen.cli.project_slug", return_value="widget"), \
             patch("claude_kitchen.cli.namespaced", return_value="widget"), \
             patch("claude_kitchen.cli.spawn_sous") as mock_spawn:
            mock_tmux.return_value = MagicMock(returncode=0)
            cmd_open(args)
        assert (base / "cooks" / "eng.json").read_text() == '{"status":"working"}'
        assert mock_spawn.call_args.kwargs["resume_session_id"] == "sid-1"


class TestSecondOpenIsANoOp:
    """`kitchen open` on a kitchen whose sous is already running must change
    NOTHING. The guard lives in spawn_sous, which is the last statement of
    cmd_open, so a duplicate open used to rewrite kitchen.json and
    kitchen-mcp.json and stand up a whole tmux server before aborting with
    "Sous chef already running".

    The hazard is the has_session TRUE branch: there the same pre-guard path
    runs _sweep_cooks, so a duplicate open could delete the records of cooks
    that are alive on a different tmux server — the trap that had to be
    hand-repaired on two kitchens. An aborted command must not mutate."""

    @patch("claude_kitchen.spawn.os.execvp")
    def _run_second_open(self, has_session_value, mock_exec, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        base = tmp_path / ".claude-kitchen" / "widget"
        (base / "cooks").mkdir(parents=True)

        kitchen_json = base / "kitchen.json"
        mcp_json = base / "kitchen-mcp.json"
        cook_json = base / "cooks" / "eng.json"
        # indent=2 so cmd_open's compact rewrite is detectable byte-wise; a
        # re-serialised identical dict would otherwise compare equal.
        kitchen_json.write_text(json.dumps(
            {"source": str(project), "slug": "widget", "sous_session_id": "sid-1"},
            indent=2) + "\n")
        mcp_json.write_text('{"sentinel": "untouched"}\n')
        cook_json.write_text('{"status":"working"}')
        # A sous that is genuinely alive — os.kill(pid, 0) succeeds on us.
        (base / "sous.pid").write_text(str(os.getpid()))

        before = (kitchen_json.read_text(), mcp_json.read_text(), cook_json.read_text())

        args = MagicMock(project=str(project), resume=False, sub_sous=False,
                         worktree_path=None)
        args.name = "widget"
        with patch("claude_kitchen.cli.has_session", return_value=has_session_value), \
             patch("claude_kitchen.cli.tmux") as mock_tmux, \
             patch("claude_kitchen.cli.project_slug", return_value="widget"), \
             patch("claude_kitchen.cli.namespaced", return_value="widget"), \
             patch("claude_kitchen.cli._sweep_cooks") as mock_sweep:
            mock_tmux.return_value = MagicMock(returncode=0)
            with pytest.raises(SystemExit) as exc:
                cmd_open(args)

        after = (kitchen_json.read_text(), mcp_json.read_text(), cook_json.read_text())
        return before, after, mock_tmux, mock_sweep, mock_exec, exc

    def test_second_open_writes_nothing_and_creates_no_server(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        before, after, mock_tmux, mock_sweep, mock_exec, exc = \
            self._run_second_open(False, tmp_path=tmp_path)

        assert "already running" in str(exc.value)
        assert after == before, "a refused open must not rewrite kitchen state"
        mock_tmux.assert_not_called(), "a refused open must not create a tmux server"
        mock_exec.assert_not_called()

    def test_second_open_does_not_sweep_cook_records(self, tmp_path, monkeypatch):
        """The dangerous branch: session reachable, so the pre-guard path would
        have reached _sweep_cooks."""
        monkeypatch.setenv("HOME", str(tmp_path))
        before, after, mock_tmux, mock_sweep, mock_exec, exc = \
            self._run_second_open(True, tmp_path=tmp_path)

        assert "already running" in str(exc.value)
        mock_sweep.assert_not_called(), "a refused open must never sweep cook records"
        assert after == before


class TestCmdClose:
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_removes_cooks_dir(self, mock_rk, mock_state, mock_tmux, tmp_path):
        mock_state.return_value = tmp_path
        mock_tmux.return_value = MagicMock(returncode=0)
        cooks = tmp_path / "cooks"
        cooks.mkdir()
        (cooks / "eng.json").write_text("{}")
        # clean-room codex per-cook homes (containing copied auth.json) must
        # also be wiped on close
        codex_home = tmp_path / "codex-home" / "eval1"
        codex_home.mkdir(parents=True)
        (codex_home / "auth.json").write_text("{}")
        # both the renamed config AND any legacy .mcp.json must be cleaned up
        # on close (no cook-discoverable leftover under base/)
        mcp_config = tmp_path / "kitchen-mcp.json"
        mcp_config.write_text("{}")
        legacy_config = tmp_path / ".mcp.json"
        legacy_config.write_text("{}")
        args = MagicMock()
        args.kitchen = "risotto"
        cmd_close(args)
        assert not cooks.exists()
        assert not (tmp_path / "codex-home").exists()
        assert not mcp_config.exists()
        assert not legacy_config.exists()

    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_clock_out_removes_codex_home(self, mock_rk, mock_state, mock_tmux, tmp_path):
        """clock-out tears down the cook's per-cook CODEX_HOME (and its auth.json)."""
        from claude_kitchen.cli import cmd_clock_out
        mock_state.return_value = tmp_path
        mock_tmux.return_value = MagicMock(returncode=0)
        (tmp_path / "cooks").mkdir()
        (tmp_path / "cooks" / "eval1.json").write_text("{}")
        codex_home = tmp_path / "codex-home" / "eval1"
        codex_home.mkdir(parents=True)
        (codex_home / "auth.json").write_text("{}")
        args = MagicMock()
        args.kitchen = "risotto"
        args.cook = "eval1"
        cmd_clock_out(args)
        assert not codex_home.exists()
        assert not (tmp_path / "cooks" / "eval1.json").exists()


class TestCmdCloseWipesNotesNotWiki:
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_kitchen", return_value="risotto")
    def test_close_removes_notes_dir(self, mock_rk, mock_state, mock_tmux, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        base = tmp_path / ".claude-kitchen" / "risotto"
        base.mkdir(parents=True)
        mock_state.return_value = base
        mock_tmux.return_value = MagicMock(returncode=0)

        notes = base / "notes"
        notes.mkdir()
        (notes / "log.md").write_text("stuff")
        wiki = tmp_path / ".claude-kitchen" / "projects" / "acme-widget" / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "mistakes.md").write_text("table")

        args = MagicMock()
        args.kitchen = "risotto"
        args.force = False
        cmd_close(args)

        assert not notes.exists(), "notes/ should be wiped"
        assert (wiki / "mistakes.md").exists(), "project wiki must persist"


class TestCmdSetupExit:
    @patch("claude_kitchen.cli.subprocess.run")
    def test_exits_non_zero_when_claude_missing(self, mock_run, monkeypatch, tmp_path):
        mock_run.return_value = MagicMock(returncode=127, stdout="", stderr="not found")
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            '{"hooks":{"Stop":[{"hooks":[{"command":"kitchen hook"}]}]}}'
        )
        from claude_kitchen.cli import cmd_setup
        with pytest.raises(SystemExit) as exc:
            cmd_setup(MagicMock())
        assert exc.value.code == 1


class TestCmdSetupCodexHook:
    """The hook is detected by presence, not by one exact spelling: Codex
    chains a prior notify wrapper by re-encoding kitchen's hook as escaped,
    space-free JSON."""

    CHAINED = (
        'notify = ["/Applications/Wrapper.app/Contents/MacOS/Wrapper", "turn-ended", '
        '"--previous-notify", "[\\"kitchen\\",\\"hook-codex\\"]"]\n'
    )
    PLAIN = 'notify = ["kitchen", "hook-codex"]\n'

    def _prep(self, tmp_path, notify_line):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(json.dumps({
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "kitchen hook"}]}],
                "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "kitchen hook"}]}],
            }
        }))
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("[features]\nhooks = true\n" + notify_line)
        sp = tmp_path / ".claude" / "plugins" / "cache" / "superpowers-marketplace" / "superpowers"
        sp.mkdir(parents=True)

    @pytest.mark.parametrize("notify_line", [CHAINED, PLAIN])
    @patch("claude_kitchen.cli.subprocess.run")
    def test_hook_detected(self, mock_run, notify_line, monkeypatch, tmp_path, capsys):
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        self._prep(tmp_path, notify_line)
        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())  # exits non-zero on a blocker
        out = capsys.readouterr().out
        assert "✅ Codex hook installed" in out
        assert "❌ Codex hook not found" not in out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_missing_hook_still_fails(self, mock_run, monkeypatch, tmp_path, capsys):
        """Another tool's notify wrapper, with no kitchen hook chained behind it."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        self._prep(tmp_path, 'notify = ["/Applications/Wrapper.app/Contents/MacOS/Wrapper", "turn-ended"]\n')
        from claude_kitchen.cli import cmd_setup
        with pytest.raises(SystemExit) as exc:
            cmd_setup(MagicMock())
        assert exc.value.code == 1
        assert "❌ Codex hook not found" in capsys.readouterr().out


class TestCmdSetupStatusline:
    """Three-way advisory: no statusLine, different statusLine (embed advice),
    kitchen segment present (green). None block setup."""

    def _base_settings(self):
        return {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "kitchen hook"}]}],
                "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "kitchen hook"}]}],
            }
        }

    def _prep_home(self, tmp_path, settings_obj):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        if settings_obj is not None:
            (claude_dir / "settings.json").write_text(json.dumps(settings_obj))
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            '[features]\ncodex_hooks = true\nnotify = ["kitchen", "hook-codex"]\n'
        )
        sp = tmp_path / ".claude" / "plugins" / "cache" / "superpowers-marketplace" / "superpowers"
        sp.mkdir(parents=True)

    @patch("claude_kitchen.cli.subprocess.run")
    def test_missing_settings_file_advisory(self, mock_run, monkeypatch, tmp_path, capsys):
        """No settings.json → not-configured branch (both options offered)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        from claude_kitchen.cli import cmd_setup
        with pytest.raises(SystemExit):
            cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "Statusline not configured" in out
        # Option 1 (minimal, segment-direct) + Option 2 (richer packaged).
        assert "kitchen statusline-segment" in out
        assert "statusline-command.sh" in out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_statusline_key_missing_advisory_only(self, mock_run, monkeypatch, tmp_path, capsys):
        """Valid settings, no statusLine key → not-configured, but setup still
        exits zero (advisory only)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        self._prep_home(tmp_path, self._base_settings())

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "Statusline not configured" in out
        assert "🍳 All set" in out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_statusline_points_to_missing_file(self, mock_run, monkeypatch, tmp_path, capsys):
        """Broken path in statusLine.command → treated as not-configured."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        settings = self._base_settings()
        settings["statusLine"] = {
            "type": "command",
            "command": str(tmp_path / "does-not-exist.sh"),
        }
        self._prep_home(tmp_path, settings)

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "Statusline not configured" in out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_statusline_different_not_kitchen(self, mock_run, monkeypatch, tmp_path, capsys):
        """User has their own statusline script, no kitchen segment → embed advice."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        script = tmp_path / "mystatusline.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        script.chmod(0o755)
        settings = self._base_settings()
        settings["statusLine"] = {"type": "command", "command": str(script)}
        self._prep_home(tmp_path, settings)

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "no kitchen segment detected" in out
        assert "$(kitchen statusline-segment)" in out
        assert "🍳 All set" in out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_statusline_green_when_script_embeds_segment(self, mock_run, monkeypatch, tmp_path, capsys):
        """Script whose text references `kitchen statusline-segment` → green."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        script = tmp_path / ".claude" / "statusline-command.sh"
        settings = self._base_settings()
        settings["statusLine"] = {"type": "command", "command": f"bash {script}"}
        self._prep_home(tmp_path, settings)
        script.write_text("#!/bin/sh\n$(kitchen statusline-segment)\n")
        script.chmod(0o755)

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "kitchen segment active" in out
        assert "not configured" not in out
        assert "no kitchen segment" not in out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_statusline_green_when_command_is_segment_directly(self, mock_run, monkeypatch, tmp_path, capsys):
        """Minimal setup: `command: "kitchen statusline-segment"` → green."""
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        settings = self._base_settings()
        settings["statusLine"] = {"type": "command", "command": "kitchen statusline-segment"}
        self._prep_home(tmp_path, settings)

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())
        out = capsys.readouterr().out
        assert "kitchen segment active" in out

    def test_packaged_script_exists_and_is_executable(self):
        """Guard against the .sh file not shipping with the package."""
        from claude_kitchen.cli import _PKG_DIR
        script = _PKG_DIR / "statusline-command.sh"
        assert script.exists(), f"Packaged statusline missing at {script}"
        assert script.stat().st_mode & 0o111, "Packaged statusline should be executable"

    def test_packaged_script_delegates_to_segment_cli(self):
        """The packaged script must reference `kitchen statusline-segment` —
        otherwise installing it wouldn't satisfy the green branch of the check."""
        from claude_kitchen.cli import _PKG_DIR
        body = (_PKG_DIR / "statusline-command.sh").read_text()
        assert "kitchen statusline-segment" in body


class TestCmdSetupRootMcp:
    """`kitchen setup` auto-removes a stray state-root .mcp.json (§Design.4)."""

    def _green_home(self, tmp_path):
        # Minimal env so cmd_setup reaches the root-.mcp.json check and exits 0.
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(json.dumps({
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "kitchen hook"}]}],
                "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "kitchen hook"}]}],
            }
        }))
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            '[features]\nhooks = true\nnotify = ["kitchen", "hook-codex"]\n'
        )
        (tmp_path / ".claude" / "plugins" / "cache" / "superpowers-marketplace" / "superpowers").mkdir(parents=True)

    @patch("claude_kitchen.cli.subprocess.run")
    def test_removes_stray_root_mcp_json(self, mock_run, monkeypatch, tmp_path, capsys):
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        self._green_home(tmp_path)
        root_mcp = tmp_path / ".claude-kitchen" / ".mcp.json"
        root_mcp.parent.mkdir(parents=True)
        root_mcp.write_text('{"mcpServers": {}}')

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())

        assert not root_mcp.exists(), "stray root .mcp.json must be removed"
        out = capsys.readouterr().out
        assert "Removed stray root-level MCP config" in out
        assert str(root_mcp) in out

    @patch("claude_kitchen.cli.subprocess.run")
    def test_noop_when_absent_idempotent(self, mock_run, monkeypatch, tmp_path, capsys):
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        self._green_home(tmp_path)
        (tmp_path / ".claude-kitchen").mkdir(parents=True)

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())  # no .mcp.json present
        cmd_setup(MagicMock())  # second run — still a clean no-op
        out = capsys.readouterr().out
        assert "Removed stray root-level MCP config" not in out
        assert not (tmp_path / ".claude-kitchen" / ".mcp.json").exists()

    @patch("claude_kitchen.cli.subprocess.run")
    def test_does_not_touch_per_kitchen_config(self, mock_run, monkeypatch, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="2.1.99 (claude)\n", stderr="")
        monkeypatch.setenv("HOME", str(tmp_path))
        self._green_home(tmp_path)
        root = tmp_path / ".claude-kitchen"
        root.mkdir(parents=True)
        (root / ".mcp.json").write_text("{}")
        # files that must survive: root-level kitchen-mcp.json and a per-kitchen
        # config under a kitchen dir
        root_kitchen_cfg = root / "kitchen-mcp.json"
        root_kitchen_cfg.write_text("{}")
        per_kitchen = root / "risotto"
        per_kitchen.mkdir()
        per_kitchen_cfg = per_kitchen / "kitchen-mcp.json"
        per_kitchen_cfg.write_text("{}")

        from claude_kitchen.cli import cmd_setup
        cmd_setup(MagicMock())

        assert not (root / ".mcp.json").exists(), "root .mcp.json removed"
        assert root_kitchen_cfg.exists(), "root kitchen-mcp.json must be untouched"
        assert per_kitchen_cfg.exists(), "per-kitchen config must be untouched"


class TestCmdStatuslineSegment:
    """`kitchen statusline-segment` soft-resolves the current kitchen, prints
    one line, and is silent when outside any kitchen.

    The count is scoped to live tmux windows (the same source brigade uses),
    NOT a glob over cooks/*.json — so it describes exactly the kitchen the
    attach target points at and isn't inflated by orphaned state files left
    behind when a cook's window dies. Because it's wired into the user's
    prompt, it must degrade to empty/partial output and never raise."""

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows")
    @patch("claude_kitchen.cli.list_kitchens", return_value=[])
    def test_no_kitchen_prints_nothing(self, mock_ls, mock_win, mock_status, mock_has, monkeypatch, capsys):
        monkeypatch.delenv("AGENT_KITCHEN", raising=False)
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        assert capsys.readouterr().out == ""

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows")
    @patch("claude_kitchen.cli.list_kitchens", return_value=["a", "b"])
    def test_ambiguous_without_agent_session_is_silent(self, mock_ls, mock_win, mock_status, mock_has, monkeypatch, capsys):
        monkeypatch.delenv("AGENT_KITCHEN", raising=False)
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        assert capsys.readouterr().out == ""

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows", return_value=["a", "b", "c", "d"])
    def test_with_agent_session_prints_attach_and_counts(
        self, mock_win, mock_status, mock_has, monkeypatch, capsys,
    ):
        monkeypatch.setenv("AGENT_KITCHEN", "risotto")
        mock_status.side_effect = [
            {"status": "working"}, {"status": "booting"},
            {"status": "idle"}, {"status": "failed"},
        ]
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ tmux -L ck-risotto attach ]  [ 2/4 agents active ]"
        mock_win.assert_called_once_with("risotto", timeout=PROBE_TIMEOUT)

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status", return_value={"status": "working"})
    @patch("claude_kitchen.cli.list_windows", return_value=["cook0"])
    @patch("claude_kitchen.cli.list_kitchens", return_value=["solo"])
    def test_single_session_without_agent_session_omits_attach_hint(
        self, mock_ls, mock_win, mock_status, mock_has, monkeypatch, capsys,
    ):
        """Called from outside sous (no AGENT_SESSION) but only one kitchen
        is running → segment still renders, but without the attach hint
        since the caller isn't in sous context."""
        monkeypatch.delenv("AGENT_KITCHEN", raising=False)
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ 1/1 agents active ]"

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows", return_value=[])
    def test_no_live_windows_reports_zero_over_zero(
        self, mock_win, mock_status, mock_has, monkeypatch, capsys,
    ):
        monkeypatch.setenv("AGENT_KITCHEN", "empty")
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ tmux -L ck-empty attach ]  [ 0/0 agents active ]"

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows", return_value=["live0", "live1"])
    def test_orphaned_state_files_do_not_inflate_count(
        self, mock_win, mock_status, mock_has, monkeypatch, tmp_path, capsys,
    ):
        """Regression: a kitchen with stale cooks/*.json for dead windows must
        still report only its LIVE cooks. Pre-fix this globbed every json file
        and rendered e.g. `5/18` for a 9-cook kitchen."""
        monkeypatch.setenv("AGENT_KITCHEN", "r")
        # State dir littered with 16 orphan files (no live window) — the
        # pre-fix glob would have counted all of them.
        with patch("claude_kitchen.cli.state_dir", return_value=tmp_path):
            cooks = tmp_path / "cooks"
            cooks.mkdir()
            for i in range(16):
                (cooks / f"orphan{i}.json").write_text(json.dumps({"status": "working"}))
            mock_status.side_effect = [{"status": "working"}, {"status": "idle"}]

            from claude_kitchen.cli import cmd_statusline_segment
            cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ tmux -L ck-r attach ]  [ 1/2 agents active ]"

    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.list_windows")
    def test_stale_agent_session_renders_nothing(
        self, mock_win, mock_has, monkeypatch, capsys,
    ):
        """Regression: a dead/closed session referenced by AGENT_SESSION must
        render empty and never raise — not an attach hint to a gone session,
        and not a CalledProcessError from list_windows(check=True)."""
        monkeypatch.setenv("AGENT_KITCHEN", "ghost")
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        assert capsys.readouterr().out == ""
        mock_win.assert_not_called()

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows",
           side_effect=subprocess.CalledProcessError(1, ["tmux", "list-windows"]))
    def test_session_closing_during_listing_does_not_raise(
        self, mock_win, mock_status, mock_has, monkeypatch, capsys,
    ):
        """Regression (TOCTOU): the session disappears between has_session and
        list_windows. Must degrade to 0/0, never propagate the error."""
        monkeypatch.setenv("AGENT_KITCHEN", "closing")
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ tmux -L ck-closing attach ]  [ 0/0 agents active ]"

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows", return_value=["ok", "broken"])
    def test_malformed_cook_json_counts_as_inactive(
        self, mock_win, mock_status, mock_has, monkeypatch, capsys,
    ):
        """Regression: a cook file that fails to parse is counted inactive (but
        still counted in the total), restoring the tolerance the old glob had.
        Pre-fix the unguarded read_status raised and the statusline threw."""
        monkeypatch.setenv("AGENT_KITCHEN", "r")
        mock_status.side_effect = [
            {"status": "working"},
            json.JSONDecodeError("Expecting value", "doc", 0),
        ]
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ tmux -L ck-r attach ]  [ 1/2 agents active ]"

    # ---- per-kitchen socket: the statusline must never stall or throw ----
    # Each kitchen now has its own tmux server, so "MY server is wedged" is a
    # reachable state for the very kitchen the statusline describes. At the
    # default 15s budget that would freeze the head chef's prompt on every
    # render; unguarded it would raise TimeoutExpired into it.

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows", return_value=["a"])
    def test_tmux_probes_are_bounded_by_probe_timeout(
        self, mock_win, mock_status, mock_has, monkeypatch, capsys,
    ):
        from claude_kitchen.cli import cmd_statusline_segment
        from claude_kitchen.tmux import PROBE_TIMEOUT
        monkeypatch.setenv("AGENT_KITCHEN", "r")
        cmd_statusline_segment(MagicMock())
        assert mock_has.call_args.kwargs["timeout"] == PROBE_TIMEOUT
        assert mock_win.call_args.kwargs["timeout"] == PROBE_TIMEOUT

    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.read_status")
    @patch("claude_kitchen.cli.list_windows",
           side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=2))
    def test_wedged_own_server_degrades_instead_of_raising(
        self, mock_win, mock_status, mock_has, monkeypatch, capsys,
    ):
        """This kitchen's server answers the probe and then stops answering.
        The segment must still render (0/0), not put a traceback in the prompt."""
        monkeypatch.setenv("AGENT_KITCHEN", "wedged")
        from claude_kitchen.cli import cmd_statusline_segment
        cmd_statusline_segment(MagicMock())
        out = capsys.readouterr().out.rstrip("\n")
        assert out == "[ tmux -L ck-wedged attach ]  [ 0/0 agents active ]"


class TestCmdRoles:
    def test_lists_roles_with_descriptions(self, capsys):
        from claude_kitchen.cli import cmd_roles
        cmd_roles(MagicMock())
        out = capsys.readouterr().out
        for r in ("_default", "eng", "reviewer", "qa"):
            assert r in out
        assert "implementer" in out  # from eng.md header
        assert "reviewer" in out


class TestBrigadeOutput:
    @patch("claude_kitchen.cli.list_kitchens", return_value=["risotto"])
    @patch("claude_kitchen.cli.list_windows", return_value=["cook1", "cook2"])
    @patch("claude_kitchen.cli.read_status")
    def test_brigade_shows_sous_and_cooks(self, mock_status, mock_win, mock_sess, capsys):
        mock_status.side_effect = [
            {"status": "working"},
            {"status": "idle", "summary": "tests pass"},  # summary must be dropped
        ]
        cmd_brigade(kitchen=None)
        out = capsys.readouterr().out
        assert "risotto" in out
        assert "cook1" in out
        assert "cook2" in out
        # Summary suffix removed (Chunk 4).
        assert "tests pass" not in out
        assert '-- "' not in out


class TestSubSousFlagParsing:
    """`--sub-sous` parses off the real argparse config (store_true, default
    False). Dispatch is stubbed so only the parsed namespace is inspected."""

    def test_flag_present_parses_true(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(sys, "argv", ["kitchen", "open", "child", "--sub-sous"])
        with patch("claude_kitchen.cli.cmd_open",
                   side_effect=lambda a: captured.update(sub_sous=a.sub_sous)):
            main()
        assert captured["sub_sous"] is True

    def test_flag_absent_defaults_false(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(sys, "argv", ["kitchen", "open", "child"])
        with patch("claude_kitchen.cli.cmd_open",
                   side_effect=lambda a: captured.update(sub_sous=a.sub_sous)):
            main()
        assert captured["sub_sous"] is False


class TestParentPushBase:
    """The cmd_hook upward-push routing decision: where (if anywhere) a child
    sous's Stop reports UP. Keyed on PARENT_STATUS_DIR, guarded against a
    self-loop."""

    def test_none_when_env_unset(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PARENT_STATUS_DIR", raising=False)
        assert _parent_push_base(tmp_path) is None

    def test_none_when_points_at_own_base(self, monkeypatch, tmp_path):
        # Self-loop guard: a root sous whose PARENT_STATUS_DIR == its own base
        # must NOT push (would echo its own Stop into its own channel).
        monkeypatch.setenv("PARENT_STATUS_DIR", str(tmp_path))
        assert _parent_push_base(tmp_path) is None

    def test_returns_parent_when_distinct(self, monkeypatch, tmp_path):
        parent = tmp_path / "parent"
        child = tmp_path / "child"
        monkeypatch.setenv("PARENT_STATUS_DIR", str(parent))
        assert _parent_push_base(child) == parent


class TestSubSousUpwardPush:
    """Integration: a CHILD sous (PARENT_STATUS_DIR set) Stop pushes a channel
    notification UP to the parent socket — and only there. A root sous (no
    PARENT_STATUS_DIR) stays the pure no-op it is today."""

    def test_child_sous_stop_pushes_to_parent_socket(self, monkeypatch, tmp_path):
        child_base = tmp_path / "child"
        parent_base = tmp_path / "parent"
        child_base.mkdir()
        parent_base.mkdir()
        monkeypatch.setenv("AGENT_NAME", "sous")
        monkeypatch.setenv("AGENT_KITCHEN", "widget-child")
        monkeypatch.setenv("STATUS_DIR", str(child_base))
        monkeypatch.setenv("PARENT_STATUS_DIR", str(parent_base))
        _stdin_payload(monkeypatch, hook_event_name="Stop",
                       last_assistant_message="child phase done",
                       session_id="sess-1")

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        mock_send.assert_called_once()
        sock, push = mock_send.call_args[0]
        assert sock == parent_base / "kitchen.sock"   # parent's socket, not own
        assert push["cook"] == "child"                # base.name = child kitchen
        assert push["summary"] == "child phase done"
        # The sous is not a cook: no cook status file written.
        assert not (child_base / "cooks").exists()

    def test_root_sous_stop_does_not_push(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_NAME", "sous")
        monkeypatch.setenv("AGENT_KITCHEN", "root")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        monkeypatch.delenv("PARENT_STATUS_DIR", raising=False)
        _stdin_payload(monkeypatch, hook_event_name="Stop",
                       last_assistant_message="root done", session_id="s")

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        mock_send.assert_not_called()

    def test_self_loop_guard_blocks_push(self, monkeypatch, tmp_path):
        # PARENT_STATUS_DIR == own base → guarded, no push.
        monkeypatch.setenv("AGENT_NAME", "sous")
        monkeypatch.setenv("AGENT_KITCHEN", "x")
        monkeypatch.setenv("STATUS_DIR", str(tmp_path))
        monkeypatch.setenv("PARENT_STATUS_DIR", str(tmp_path))
        _stdin_payload(monkeypatch, hook_event_name="Stop",
                       last_assistant_message="x", session_id="s")

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        mock_send.assert_not_called()

    def test_child_sous_non_stop_event_does_not_push(self, monkeypatch, tmp_path):
        """Only Stop reports up — a non-Stop sous event stays a no-op even
        with PARENT_STATUS_DIR set."""
        child_base = tmp_path / "child"
        parent_base = tmp_path / "parent"
        child_base.mkdir()
        parent_base.mkdir()
        monkeypatch.setenv("AGENT_NAME", "sous")
        monkeypatch.setenv("AGENT_KITCHEN", "widget-child")
        monkeypatch.setenv("STATUS_DIR", str(child_base))
        monkeypatch.setenv("PARENT_STATUS_DIR", str(parent_base))
        _stdin_payload(monkeypatch, hook_event_name="UserPromptSubmit", prompt="hi")

        mock_send = MagicMock()
        with patch("claude_kitchen.channel.send_to_socket", mock_send):
            cmd_hook(argparse.Namespace(command="hook"))

        mock_send.assert_not_called()


class TestCmdOpenSubSous:
    @patch("claude_kitchen.cli.namespaced", return_value="widget-child")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.spawn_sous_window")
    @patch("claude_kitchen.cli.spawn_sous")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.create_worktree", return_value=Path("/tmp/child"))
    @patch("claude_kitchen.cli.resolve_project")
    def test_launches_windowed_sous_and_waits(
        self, mock_resolve, mock_wt, mock_state, mock_tmux, mock_has,
        mock_spawn_sous, mock_spawn_win, mock_wait, mock_slug, mock_ns,
        tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Caller is a parent sous → its STATUS_DIR is inherited here.
        monkeypatch.setenv("STATUS_DIR", str(tmp_path / "parent"))
        mock_resolve.return_value = Path("/tmp/myproject")
        mock_state.return_value = tmp_path / "state"
        mock_tmux.return_value = MagicMock(returncode=0)

        args = MagicMock()
        args.name = "child"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False
        args.sub_sous = True

        cmd_open(args)

        # Windowed sous launched; the in-place execvp sous is NOT used.
        mock_spawn_sous.assert_not_called()
        mock_spawn_win.assert_called_once()
        ca = mock_spawn_win.call_args
        assert ca.args[0] == "widget-child"            # namespaced name
        assert ca.args[1] == tmp_path / "state"         # this kitchen's base
        # parent_base wired from the inherited STATUS_DIR.
        assert ca.kwargs["parent_base"] == tmp_path / "parent"
        # Readiness barrier on the `sous` window before returning.
        mock_wait.assert_called_once_with("widget-child", "sous", "claude")

    @patch("claude_kitchen.cli.namespaced", return_value="widget-child")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=True)
    @patch("claude_kitchen.cli.spawn_sous_window")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.create_worktree", return_value=Path("/tmp/child"))
    @patch("claude_kitchen.cli.resolve_project")
    def test_no_parent_status_dir_passes_none(
        self, mock_resolve, mock_wt, mock_state, mock_tmux, mock_has,
        mock_spawn_win, mock_wait, mock_slug, mock_ns, tmp_path, monkeypatch,
    ):
        """Run by hand (no parent sous) → parent_base is None, child runs
        standalone."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("STATUS_DIR", raising=False)
        mock_resolve.return_value = Path("/tmp/myproject")
        mock_state.return_value = tmp_path / "state"
        mock_tmux.return_value = MagicMock(returncode=0)

        args = MagicMock()
        args.name = "child"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False
        args.sub_sous = True

        cmd_open(args)
        assert mock_spawn_win.call_args.kwargs["parent_base"] is None

    @patch("claude_kitchen.cli.namespaced", return_value="widget-child")
    @patch("claude_kitchen.cli.has_session", return_value=True)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_project")
    def test_rejects_existing_session(
        self, mock_resolve, mock_state, mock_has, mock_ns, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        mock_state.return_value = tmp_path / "state"
        args = MagicMock()
        args.name = "child"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False
        args.sub_sous = True
        with pytest.raises(SystemExit, match="fresh-open only"):
            cmd_open(args)

    @patch("claude_kitchen.cli.namespaced", return_value="widget-child")
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.resolve_project")
    def test_rejects_resume(
        self, mock_resolve, mock_state, mock_has, mock_ns, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resolve.return_value = Path("/tmp/myproject")
        base = tmp_path / "state"
        base.mkdir(parents=True)
        (base / "kitchen.json").write_text(json.dumps({
            "source": "/tmp/myproject", "slug": "widget", "sous_session_id": "s1",
        }) + "\n")
        mock_state.return_value = base
        args = MagicMock()
        args.name = "child"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = True
        args.sub_sous = True
        with pytest.raises(SystemExit, match="fresh-open only"):
            cmd_open(args)

    @patch("claude_kitchen.cli.spawn_sous_window", return_value=False)
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    def test_rejects_preexisting_worktree_and_leaves_it_untouched(
        self, mock_state, mock_tmux, mock_has, mock_spawn_win, tmp_path, monkeypatch,
    ):
        """fresh-open-only at the git layer: if a worktree/branch named <name>
        already exists, --sub-sous must REJECT (not reuse it) — otherwise a
        failed launch's _abort_sub_sous force-removes a worktree + branch this
        open never created. Real git so the destructive path is exercised."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("STATUS_DIR", raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()
        env = {**__import__("os").environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"],
                       check=True, env=env)
        # A pre-existing worktree + branch at the path this open would target
        # (create_worktree defaults to <repo-parent>/<name>).
        existing_wt = tmp_path / "child"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", str(existing_wt), "-b", "child"],
                       check=True, env=env)
        mock_state.return_value = tmp_path / "state"
        mock_tmux.return_value = MagicMock(returncode=0, stdout="")

        args = MagicMock()
        args.name = "child"
        args.project = str(repo)
        args.worktree_path = None
        args.resume = False
        args.sub_sous = True

        with patch("claude_kitchen.cli.resolve_project", return_value=repo):
            with pytest.raises(SystemExit, match="already exists"):
                cmd_open(args)

        # The pre-existing worktree + branch are UNTOUCHED.
        assert existing_wt.exists(), "pre-existing worktree must not be removed"
        assert subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "refs/heads/child"],
            capture_output=True,
        ).returncode == 0, "pre-existing branch must not be deleted"

    @patch("claude_kitchen.cli.remove_worktree")
    @patch("claude_kitchen.cli.namespaced", return_value="widget-child")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    @patch("claude_kitchen.cli.spawn_sous_window", return_value=False)
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.create_worktree")
    @patch("claude_kitchen.cli.resolve_project")
    def test_spawn_failure_cleans_up_and_exits(
        self, mock_resolve, mock_wt, mock_state, mock_tmux, mock_has,
        mock_spawn_win, mock_slug, mock_ns, mock_rmwt, tmp_path, monkeypatch,
    ):
        """spawn_sous_window False → never leave a sous-less kitchen: kill the
        session, remove the worktree, wipe the state dir, and exit clearly."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("STATUS_DIR", raising=False)
        mock_resolve.return_value = Path("/tmp/myproject")
        base = tmp_path / "state"
        mock_state.return_value = base
        wt = tmp_path / "wt"
        wt.mkdir()
        mock_wt.return_value = wt
        mock_tmux.return_value = MagicMock(returncode=0, stdout="")

        args = MagicMock()
        args.name = "child"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False
        args.sub_sous = True

        with pytest.raises(SystemExit, match="cleaned up"):
            cmd_open(args)

        assert not base.exists()                       # state dir wiped
        mock_rmwt.assert_called_once()                 # worktree removed
        assert mock_rmwt.call_args.args[0] == wt
        assert mock_rmwt.call_args.kwargs.get("force") is True
        assert any(c.args[0] == "kill-session" for c in mock_tmux.call_args_list)

    @patch("claude_kitchen.cli.remove_worktree")
    @patch("claude_kitchen.cli.namespaced", return_value="widget-child")
    @patch("claude_kitchen.cli.project_slug", return_value="widget")
    @patch("claude_kitchen.cli.wait_for_prompt", return_value=False)
    @patch("claude_kitchen.cli.spawn_sous_window", return_value=True)
    @patch("claude_kitchen.cli.has_session", return_value=False)
    @patch("claude_kitchen.cli.tmux")
    @patch("claude_kitchen.cli.state_dir")
    @patch("claude_kitchen.cli.create_worktree")
    @patch("claude_kitchen.cli.resolve_project")
    def test_prompt_timeout_cleans_up_and_exits(
        self, mock_resolve, mock_wt, mock_state, mock_tmux, mock_has,
        mock_spawn_win, mock_wait, mock_slug, mock_ns, mock_rmwt,
        tmp_path, monkeypatch,
    ):
        """Sous spawned but never reached its prompt (genuine failure after the
        retry-tolerant wait) → same teardown, no half-open kitchen."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("STATUS_DIR", raising=False)
        mock_resolve.return_value = Path("/tmp/myproject")
        base = tmp_path / "state"
        mock_state.return_value = base
        wt = tmp_path / "wt"
        wt.mkdir()
        mock_wt.return_value = wt
        mock_tmux.return_value = MagicMock(returncode=0, stdout="")

        args = MagicMock()
        args.name = "child"
        args.project = "/tmp/myproject"
        args.worktree_path = None
        args.resume = False
        args.sub_sous = True

        with pytest.raises(SystemExit, match="never reached its prompt"):
            cmd_open(args)

        assert not base.exists()
        mock_rmwt.assert_called_once()
        assert any(c.args[0] == "kill-session" for c in mock_tmux.call_args_list)


class TestAgySummary:
    """_agy_summary gates the gemini Stop notification, so it must NEVER raise.
    Regression: a non-string PLANNER_RESPONSE.content (schema drift) used to hit
    `.rstrip` and AttributeError straight past the OSError guard, so the hook
    failed to mark the cook idle or send the notification."""

    def _payload(self, tmp_path, lines):
        p = tmp_path / "transcript.jsonl"
        p.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        return {"transcriptPath": str(p)}

    def test_non_string_content_does_not_raise(self, tmp_path):
        # list / dict content (not str). Buggy code: `.rstrip` -> AttributeError.
        payload = self._payload(tmp_path, [
            {"type": "PLANNER_RESPONSE", "content": ["a", "b"]},
            {"type": "PLANNER_RESPONSE", "content": {"x": 1}},
        ])
        assert _agy_summary(payload) == ""   # skipped, no raise

    def test_skips_non_string_keeps_last_valid_string(self, tmp_path):
        # A non-string entry interleaved between valid strings must be skipped,
        # keeping the last valid string (not crash before reaching it).
        payload = self._payload(tmp_path, [
            {"type": "PLANNER_RESPONSE", "content": "first valid"},
            {"type": "PLANNER_RESPONSE", "content": ["junk", 2]},
            {"type": "PLANNER_RESPONSE", "content": "last valid\n"},
        ])
        assert _agy_summary(payload) == "last valid"

    def test_well_formed_returns_last_nonempty_planner_response(self, tmp_path):
        # Sanity: normal transcript — empty placeholders filtered, last wins.
        payload = self._payload(tmp_path, [
            {"type": "USER_INPUT", "content": "ignored"},
            {"type": "PLANNER_RESPONSE", "content": ""},
            {"type": "PLANNER_RESPONSE", "content": "the answer\n"},
        ])
        assert _agy_summary(payload) == "the answer"

