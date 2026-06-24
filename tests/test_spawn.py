"""Tests for spawn logic."""
import os
import shlex
import subprocess
from pathlib import Path

import pytest
from unittest.mock import patch, MagicMock
from claude_kitchen.spawn import build_shell_cmd, spawn_sous, build_sous_cmd, spawn_sous_window


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


class TestNoMemory:
    def test_no_memory_sets_disable_env_on_claude(self):
        """--no-memory wires CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 as a temp env
        assignment on the exec'd claude (verified to suppress the MEMORY.md
        injection while keeping subscription auth + hooks)."""
        cmd = build_shell_cmd(
            backend="claude", name="eval1", session="ck-r",
            status_dir="/tmp/state", no_memory=True,
        )
        assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 exec claude" in cmd

    def test_default_omits_disable_env(self):
        cmd = build_shell_cmd(
            backend="claude", name="eng", session="ck-r",
            status_dir="/tmp/state",
        )
        assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in cmd


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


def _sous_argv_from_cmd(cmd: str) -> list[str]:
    """Reproduce claude's argv from build_sous_cmd's `bash -lc '<inner>'`.
    Inner is `export ...; exec claude <args>` — return what follows `exec`."""
    outer = shlex.split(cmd)
    assert outer[:2] == ["bash", "-lc"], f"unexpected outer shape: {outer[:2]}"
    inner = shlex.split(outer[2])
    i = inner.index("exec")
    return inner[i + 1:]


class TestBuildSousCmd:
    def test_core_claude_flags(self, tmp_path):
        cmd = build_sous_cmd("widget-child", tmp_path, tmp_path / "sous-chef.md")
        argv = _sous_argv_from_cmd(cmd)
        assert argv[0] == "claude"
        assert "--dangerously-skip-permissions" in argv
        # Channel server loaded so the child can RECEIVE its own cooks.
        i = argv.index("--dangerously-load-development-channels")
        assert argv[i + 1] == "server:kitchen"
        # MCP config points at THIS kitchen's own renamed config (NOT a
        # discoverable .mcp.json — see state.MCP_CONFIG_NAME).
        j = argv.index("--mcp-config")
        assert argv[j + 1] == str(tmp_path / "kitchen-mcp.json")

    def test_prompt_via_file_not_inlined(self, tmp_path):
        """Sous prompt arrives as a file path (cook role-file pattern), never
        inlined — same shell-quoting-safety rationale as cook roles."""
        sous_md = tmp_path / "sous-chef.md"
        argv = _sous_argv_from_cmd(build_sous_cmd("c", tmp_path, sous_md))
        k = argv.index("--append-system-prompt-file")
        assert argv[k + 1] == str(sous_md)
        assert "--append-system-prompt" not in argv  # the bare (inlining) form

    def test_no_remote_control(self, tmp_path):
        """POC decision: the child sous does NOT get --remote-control."""
        cmd = build_sous_cmd("widget-child", tmp_path, tmp_path / "s.md")
        assert "--remote-control" not in cmd
        assert "--remote-control-session-name-prefix" not in cmd

    def test_identity_env(self, tmp_path):
        cmd = build_sous_cmd("widget-child", tmp_path, tmp_path / "s.md")
        assert "AGENT_NAME=sous" in cmd
        assert "AGENT_SESSION=ck-widget-child" in cmd
        # STATUS_DIR stays THIS kitchen's base (not the parent's).
        assert f"STATUS_DIR={shlex.quote(str(tmp_path))}" in cmd

    def test_parent_status_dir_exported_when_given(self, tmp_path):
        parent = tmp_path / "parent"
        cmd = build_sous_cmd("c", tmp_path, tmp_path / "s.md", parent_base=parent)
        assert f"PARENT_STATUS_DIR={shlex.quote(str(parent))}" in cmd

    def test_parent_status_dir_omitted_when_none(self, tmp_path):
        cmd = build_sous_cmd("c", tmp_path, tmp_path / "s.md")
        assert "PARENT_STATUS_DIR" not in cmd

    def test_wiki_notes_env_when_slug(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cmd = build_sous_cmd("widget-child", tmp_path, tmp_path / "s.md", slug="widget")
        assert "KITCHEN_WIKI=" in cmd
        assert "KITCHEN_NOTES=" in cmd

    def test_wiki_notes_env_omitted_without_slug(self, tmp_path):
        cmd = build_sous_cmd("widget-child", tmp_path, tmp_path / "s.md")
        assert "KITCHEN_WIKI=" not in cmd
        assert "KITCHEN_NOTES=" not in cmd


class TestSpawnSousWindow:
    @patch("claude_kitchen.spawn.tmux")
    def test_spawns_window_kills_placeholder_writes_pid(self, mock_tmux, tmp_path):
        mock_tmux.return_value = MagicMock(returncode=0, stdout="4242\n")
        ok = spawn_sous_window("widget-child", tmp_path, tmp_path / "s.md",
                               Path("/tmp/child"))
        assert ok is True
        first = mock_tmux.call_args_list[0]
        assert first.args[0] == "new-window"
        assert "ck-widget-child" in first.args
        assert "sous" in first.args
        kinds = [c.args[0] for c in mock_tmux.call_args_list]
        # _placeholder removed; pane pid queried for sous.pid.
        assert "kill-window" in kinds
        assert "list-panes" in kinds
        assert (tmp_path / "sous.pid").read_text().strip() == "4242"

    @patch("claude_kitchen.spawn.tmux")
    def test_returns_false_when_new_window_fails(self, mock_tmux, tmp_path):
        """new-window failure → False (cmd_open then tears the kitchen down),
        no placeholder kill, no sous.pid."""
        mock_tmux.return_value = MagicMock(returncode=1, stdout="")
        ok = spawn_sous_window("widget-child", tmp_path, tmp_path / "s.md",
                               Path("/tmp/child"))
        assert ok is False
        assert not (tmp_path / "sous.pid").exists()
        # Bailed right after the failed new-window — no kill/list-panes.
        assert [c.args[0] for c in mock_tmux.call_args_list] == ["new-window"]

    @patch("claude_kitchen.spawn.tmux")
    def test_list_panes_timeout_does_not_fail_launch(self, mock_tmux, tmp_path):
        """A TimeoutExpired on the list-panes pid query — AFTER new-window
        succeeded — must NOT propagate (cmd_open would treat it as a launch
        failure and tear down a live window). The sous already launched; the
        pid is best-effort, so swallow it and still return True."""
        def side(*args, **kwargs):
            if args[0] == "list-panes":
                raise subprocess.TimeoutExpired(cmd="tmux", timeout=15)
            return MagicMock(returncode=0, stdout="")
        mock_tmux.side_effect = side
        ok = spawn_sous_window("widget-child", tmp_path, tmp_path / "s.md",
                               Path("/tmp/child"))
        assert ok is True                        # launch stands despite the timeout
        assert not (tmp_path / "sous.pid").exists()  # pid skipped, best-effort
