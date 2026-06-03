"""Tests for spawn logic."""
import os
import shlex
from pathlib import Path

import pytest
from unittest.mock import patch, MagicMock
from claude_kitchen.spawn import build_shell_cmd, spawn_sous


def _codex_argv_from_shell_cmd(cmd: str) -> list[str]:
    """Reproduce what codex sees as argv after bash -lc parsing the shell
    command produced by build_shell_cmd. The outer string is
    `bash -lc '<inner>'`; bash -lc would parse <inner> through shell rules.
    shlex.split with posix=True matches that parsing for our quoting shapes."""
    outer = shlex.split(cmd)
    assert outer[:2] == ["bash", "-lc"], f"unexpected outer shape: {outer[:2]}"
    inner_tokens = shlex.split(outer[2])
    # The inner is `export ...; exec codex <args...>`. Find `exec codex` and
    # return what follows as codex's argv (program + args).
    i = inner_tokens.index("exec")
    return inner_tokens[i + 1:]


class TestBuildShellCmd:
    def test_claude_cook(self):
        cmd = build_shell_cmd(
            backend="claude", name="eng", session="ck-risotto",
            status_dir="/tmp/state",
        )
        assert "claude" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "AGENT_NAME=" in cmd
        assert "eng" in cmd

    def test_codex_cook(self):
        cmd = build_shell_cmd(
            backend="codex", name="reviewer", session="ck-risotto",
            status_dir="/tmp/state",
        )
        assert "codex" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd

    def test_codex_cook_has_notify_override(self):
        """Codex cook argv must contain `-c notify=["kitchen","hook-codex"]`
        as two adjacent tokens, surviving the bash -lc shell-quoting layer.
        Bypasses any global notify wrapper (SkyComputerUseClient et al.)."""
        cmd = build_shell_cmd(
            backend="codex", name="rev", session="ck-r",
            status_dir="/tmp/state",
        )
        argv = _codex_argv_from_shell_cmd(cmd)
        assert argv[0] == "codex"
        # Find -c notify=... in argv (there may be other -c flags too).
        notify_seen = False
        for i, tok in enumerate(argv):
            if tok == "-c" and i + 1 < len(argv) and argv[i + 1].startswith("notify="):
                assert argv[i + 1] == 'notify=["kitchen","hook-codex"]', (
                    f"notify override value malformed: {argv[i+1]!r}"
                )
                notify_seen = True
                break
        assert notify_seen, f"-c notify=... not found in codex argv: {argv}"

    def test_claude_cook_has_no_notify_override(self):
        """Claude has no equivalent notify mechanism; the override is codex-only."""
        cmd = build_shell_cmd(
            backend="claude", name="eng", session="ck-r",
            status_dir="/tmp/state",
        )
        assert "notify=" not in cmd, "notify override leaked to claude cook"

    def test_codex_notify_override_coexists_with_effort(self):
        """Effort flag and notify override must both make it through, in
        the right argv shape (each behind its own -c)."""
        cmd = build_shell_cmd(
            backend="codex", name="rev", session="ck-r",
            status_dir="/tmp/state", effort="high",
        )
        argv = _codex_argv_from_shell_cmd(cmd)
        # Two -c flags expected, in order: model_reasoning_effort, notify
        c_pairs = [
            (argv[i], argv[i + 1])
            for i in range(len(argv) - 1)
            if argv[i] == "-c"
        ]
        keys = [pair[1].split("=", 1)[0] for pair in c_pairs]
        assert "model_reasoning_effort" in keys
        assert "notify" in keys

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown backend"):
            build_shell_cmd(
                backend="gpt", name="eng", session="ck-risotto",
                status_dir="/tmp/state",
            )

    def test_propagates_kitchen_prefixed_env(self, monkeypatch):
        monkeypatch.setenv("KITCHEN_NOTES", "/tmp/n")
        monkeypatch.setenv("KITCHEN_WIKI", "/tmp/w")
        monkeypatch.setenv("FOO", "bar")
        cmd = build_shell_cmd(
            backend="claude", name="eng", session="ck-risotto",
            status_dir="/tmp/state",
        )
        assert "KITCHEN_NOTES=/tmp/n" in cmd
        assert "KITCHEN_WIKI=/tmp/w" in cmd
        assert "FOO=bar" not in cmd

    def test_no_kitchen_env_omits_vars(self, monkeypatch):
        for k in list(os.environ):
            if k.startswith("KITCHEN_"):
                monkeypatch.delenv(k)
        cmd = build_shell_cmd(
            backend="codex", name="eng", session="ck-risotto",
            status_dir="/tmp/state",
        )
        assert "KITCHEN_" not in cmd


class TestRoleInjection:
    def test_claude_role_passes_file_path(self, tmp_path):
        role_file = tmp_path / "roles" / "eng.md"
        role_file.parent.mkdir()
        role_file.write_text("# eng — implementer\nbody with 'quotes' & specials\nmultiline")
        cmd = build_shell_cmd(
            backend="claude", name="eng", session="ck-r",
            status_dir="/tmp/state", role_path=role_file,
        )
        assert "--append-system-prompt-file" in cmd
        assert str(role_file) in cmd
        # Contents must NOT be inlined — that would re-introduce the quoting risk
        assert "implementer" not in cmd
        assert "multiline" not in cmd

    def test_claude_no_role_omits_flag(self):
        cmd = build_shell_cmd(
            backend="claude", name="cook1", session="ck-r",
            status_dir="/tmp/state",
        )
        assert "--append-system-prompt-file" not in cmd


class TestSpawnSous:
    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_chdir_to_project(self, mock_exec, mock_chdir, tmp_path, monkeypatch):
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        spawn_sous("risotto", tmp_path, "prompt text", project=Path("/tmp/myproject"), slug="gh-x-y")
        mock_chdir.assert_called_once_with(Path("/tmp/myproject"))

    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_no_chdir_when_no_project(self, mock_exec, mock_chdir, tmp_path, monkeypatch):
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        spawn_sous("risotto", tmp_path, "prompt text", slug="gh-x-y")
        mock_chdir.assert_not_called()

    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_remote_control_enabled_with_kitchen_prefix(
        self, mock_exec, mock_chdir, tmp_path, monkeypatch,
    ):
        """Sous launches with --remote-control + prefix=<kitchen>. The pair
        must appear as adjacent argv tokens — claude parses the flag value
        positionally."""
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        spawn_sous("risotto", tmp_path, "prompt", slug="gh-x-y")
        argv = mock_exec.call_args.args[1]
        assert "--remote-control" in argv
        i = argv.index("--remote-control-session-name-prefix")
        assert argv[i + 1] == "risotto"

    def test_cook_argv_has_no_remote_control(self):
        """Cooks must NOT get --remote-control; the flag is sous-only."""
        for backend in ("claude", "codex"):
            cmd = build_shell_cmd(
                backend=backend, name="eng", session="ck-r",
                status_dir="/tmp/state",
            )
            assert "--remote-control" not in cmd, f"{backend} cook leaked RC flag"

    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_exports_wiki_and_notes_env(self, mock_exec, mock_chdir, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("KITCHEN_WIKI", raising=False)
        monkeypatch.delenv("KITCHEN_NOTES", raising=False)
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        state = tmp_path / ".claude-kitchen" / "risotto"
        state.mkdir(parents=True)
        spawn_sous(
            "risotto", state,
            "prompt", project=Path("/tmp/p"), slug="gh-acme-widget",
        )
        import os
        assert os.environ["KITCHEN_WIKI"] == str(
            tmp_path / ".claude-kitchen" / "projects" / "gh-acme-widget" / "wiki"
        )
        assert os.environ["KITCHEN_NOTES"] == str(
            tmp_path / ".claude-kitchen" / "risotto" / "notes"
        )
        # Register post-spawn values with monkeypatch so its teardown restores them
        monkeypatch.setenv("KITCHEN_WIKI", os.environ["KITCHEN_WIKI"])
        monkeypatch.setenv("KITCHEN_NOTES", os.environ["KITCHEN_NOTES"])


class TestSpawnSousOverview:
    @patch("claude_kitchen.spawn.os.chdir")
    @patch("claude_kitchen.spawn.os.execvp")
    def test_overview_env_prefix_and_role(self, mock_exec, mock_chdir, tmp_path, monkeypatch):
        """overview=True: wiki/notes are scoped to the overview state dir (set
        unconditionally, no slug), session is ck-overview, RC prefix is
        'overview', and the role prompt is inlined via --append-system-prompt."""
        monkeypatch.delenv("KITCHEN_WIKI", raising=False)
        monkeypatch.delenv("KITCHEN_NOTES", raising=False)
        for k in ("AGENT_NAME", "AGENT_SESSION", "STATUS_DIR"):
            monkeypatch.setenv(k, "")
        base = tmp_path / ".claude-kitchen" / "overview"
        base.mkdir(parents=True)

        spawn_sous("overview", base, "OVERVIEW ROLE PROMPT", overview=True)

        import os
        assert os.environ["AGENT_NAME"] == "sous"
        assert os.environ["AGENT_SESSION"] == "ck-overview"
        assert os.environ["STATUS_DIR"] == str(base)
        assert os.environ["KITCHEN_WIKI"] == str(base / "wiki")
        assert os.environ["KITCHEN_NOTES"] == str(base / "notes")

        argv = mock_exec.call_args.args[1]
        i = argv.index("--dangerously-load-development-channels")
        assert argv[i + 1] == "server:kitchen"
        assert argv[argv.index("--mcp-config") + 1] == str(base / ".mcp.json")
        assert "--remote-control" in argv
        assert argv[argv.index("--remote-control-session-name-prefix") + 1] == "overview"
        assert argv[argv.index("--append-system-prompt") + 1] == "OVERVIEW ROLE PROMPT"
        # overview has no project root, so no chdir
        mock_chdir.assert_not_called()

        monkeypatch.setenv("KITCHEN_WIKI", os.environ["KITCHEN_WIKI"])
        monkeypatch.setenv("KITCHEN_NOTES", os.environ["KITCHEN_NOTES"])
