"""Tests for state read/write."""
import json
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from datetime import datetime, timezone

import claude_kitchen.state as state
from claude_kitchen.state import (
    state_dir, write_status, read_status, update_status,
    project_slug, namespaced, wiki_dir, notes_dir, overview_state_dir,
    update_sous_status, read_sous_status, _render_kitchen_status_footer,
    classify_kitchen, _transcript_slug, transcript_path_for,
)


def test_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = state_dir("risotto")
    assert d == tmp_path / ".claude-kitchen" / "risotto"


def test_overview_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert overview_state_dir() == tmp_path / ".claude-kitchen" / "overview"


class TestSousStatus:
    def test_roundtrip_at_state_dir_root(self, tmp_path):
        # Sous status lives at <state-dir>/sous.json, NOT under cooks/ — keeps
        # it out of brigade/statusline cook counts and `kitchen sweep`.
        update_sous_status(tmp_path, status="idle", summary="done", agent="sous")
        assert (tmp_path / "sous.json").exists()
        assert not (tmp_path / "cooks").exists()
        data = read_sous_status(tmp_path)
        assert data["status"] == "idle"
        assert data["summary"] == "done"

    def test_update_merges(self, tmp_path):
        update_sous_status(tmp_path, status="working", agent="sous")
        update_sous_status(tmp_path, status="idle", summary="reply")
        data = read_sous_status(tmp_path)
        assert data["status"] == "idle"
        assert data["summary"] == "reply"
        assert data["agent"] == "sous"  # durable field preserved across merge

    def test_read_missing_returns_none(self, tmp_path):
        assert read_sous_status(tmp_path) is None


class TestKitchenStatusFooter:
    def _mk(self, root, name, kj, sous=None, mtime_age_s=None):
        d = root / name
        d.mkdir(parents=True)
        (d / "kitchen.json").write_text(json.dumps(kj))
        if sous is not None:
            p = d / "sous.json"
            p.write_text(json.dumps(sous))
            if mtime_age_s is not None:
                t = time.time() - mtime_age_s
                os.utime(p, (t, t))

    def test_four_classifications_sorted_and_filtered(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        root = tmp_path / ".claude-kitchen"
        # idle status within the 10-min window → waiting on you
        self._mk(root, "alpha", {"sous_session_id": "x"},
                 {"status": "idle", "summary": "need your call"}, mtime_age_s=60)
        # working
        self._mk(root, "bravo", {"sous_session_id": "y"},
                 {"status": "working", "summary": "crunching"}, mtime_age_s=5)
        # idle status but stale (>10min) → idle
        self._mk(root, "charlie", {"sous_session_id": "z"},
                 {"status": "idle", "summary": "all done"}, mtime_age_s=20 * 60)
        # no sous_session_id, no sous.json → booting
        self._mk(root, "delta", {})
        # excluded: the overview kitchen itself
        self._mk(root, "overview", {"slug": "overview"})
        # excluded: a sub-sous (parent_kitchen set)
        self._mk(root, "subby", {"sous_session_id": "p", "parent_kitchen": "alpha"},
                 {"status": "working"})

        out = _render_kitchen_status_footer()

        assert "⏳ alpha" in out and "waiting on you" in out
        assert "🔄 bravo" in out and "working" in out
        assert "💤 charlie" in out and "idle" in out
        assert "🐣 delta" in out and "booting" in out
        # one-line context pulled from sous summary
        assert "└─ need your call" in out
        # exclusions
        assert "overview" not in out
        assert "subby" not in out
        # sort order: waiting → working → booting → idle
        assert out.index("alpha") < out.index("bravo") < out.index("delta") < out.index("charlie")
        assert "KITCHEN STATUS" in out

    def test_empty_when_no_kitchens(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude-kitchen").mkdir()
        out = _render_kitchen_status_footer()
        assert "no other kitchens" in out
        assert "KITCHEN STATUS" in out

    def test_stale_working_ages_out_to_idle(self, tmp_path):
        # A kitchen stuck on status='working' whose status file hasn't moved in
        # >10min must age out to idle (sous finished/stalled/crashed) — age
        # dominates the stored status. classify_kitchen takes base directly.
        base = tmp_path / "stalekit"
        base.mkdir()
        (base / "kitchen.json").write_text(json.dumps({"sous_session_id": "x"}))
        update_sous_status(base, status="working", summary="busy")
        old = time.time() - 20 * 60
        os.utime(base / "sous.json", (old, old))
        result = classify_kitchen(base, datetime.now(timezone.utc))
        assert result["state"] == "idle"

    def test_fresh_working_stays_working(self, tmp_path):
        # Sanity counterpart: a recent 'working' kitchen is still working.
        base = tmp_path / "freshkit"
        base.mkdir()
        (base / "kitchen.json").write_text(json.dumps({"sous_session_id": "x"}))
        update_sous_status(base, status="working", summary="busy")
        assert classify_kitchen(base, datetime.now(timezone.utc))["state"] == "working"

    def test_skips_kitchen_that_races_closed(self, tmp_path):
        # If a kitchen closes mid-render (stat raises), classify_kitchen must
        # skip it (return None), not propagate — one racing kitchen can't be
        # allowed to crash the whole footer/forward.
        base = tmp_path / "racer"
        base.mkdir()
        (base / "kitchen.json").write_text(json.dumps({"sous_session_id": "x"}))
        with patch.object(state.Path, "stat", side_effect=OSError("closed mid-render")):
            assert classify_kitchen(base, datetime.now(timezone.utc)) is None

    def test_footer_skips_unreadable_kitchen(self, tmp_path, monkeypatch):
        # A malformed kitchen.json is skipped; the rest of the footer renders.
        monkeypatch.setenv("HOME", str(tmp_path))
        root = tmp_path / ".claude-kitchen"
        self._mk(root, "good", {"sous_session_id": "x"}, {"status": "idle"}, mtime_age_s=30)
        bad = root / "bad"
        bad.mkdir(parents=True)
        (bad / "kitchen.json").write_text("{not valid json")
        out = _render_kitchen_status_footer()
        assert "good" in out
        assert "bad" not in out

    def test_dormancy_filter_summarizes_stale(self, tmp_path, monkeypatch):
        # Kitchens idle > 24h drop from the per-kitchen lines and collapse into
        # a single trailing count.
        monkeypatch.setenv("HOME", str(tmp_path))
        root = tmp_path / ".claude-kitchen"
        self._mk(root, "active", {"sous_session_id": "x"}, {"status": "idle"}, mtime_age_s=5 * 60)
        self._mk(root, "old1", {"sous_session_id": "y"}, {"status": "idle"}, mtime_age_s=30 * 3600)
        self._mk(root, "old2", {"sous_session_id": "z"}, {"status": "idle"}, mtime_age_s=48 * 3600)
        out = _render_kitchen_status_footer()
        assert "active" in out
        assert "old1" not in out and "old2" not in out
        assert "2 dormant kitchens (idle > 24h)" in out

    def test_dormancy_singular(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        root = tmp_path / ".claude-kitchen"
        self._mk(root, "old1", {"sous_session_id": "y"}, {"status": "idle"}, mtime_age_s=30 * 3600)
        out = _render_kitchen_status_footer()
        assert "1 dormant kitchen (idle > 24h)" in out


class TestTranscriptPath:
    def test_slug_rule_examples(self):
        # Per spec §transcript-path slug rule (empirically verified on disk):
        # every non-alphanumeric char → '-', per character, no run-collapsing.
        assert _transcript_slug("/Users/plucas/.gemini/config/projects") == \
            "-Users-plucas--gemini-config-projects"
        assert _transcript_slug("/Users/plucas/cncorp/plow/.bare") == \
            "-Users-plucas-cncorp-plow--bare"
        assert _transcript_slug("/Users/plucas/cncorp/codel/lark-terraform-codel") == \
            "-Users-plucas-cncorp-codel-lark-terraform-codel"

    def test_path_none_without_cwd_or_session(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert transcript_path_for("/proj", None) is None
        assert transcript_path_for(None, "sid") is None

    def test_path_none_when_file_missing_else_resolves(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert transcript_path_for("/proj", "sid") is None  # file not on disk
        p = tmp_path / ".claude" / "projects" / "-proj" / "sid.jsonl"
        p.parent.mkdir(parents=True)
        p.write_text("{}")
        assert transcript_path_for("/proj", "sid") == p


class TestStatus:
    def test_write_and_read(self, tmp_path):
        write_status(tmp_path, "eng", {"status": "booting", "agent": "eng"})
        result = read_status(tmp_path, "eng")
        assert result["status"] == "booting"
        assert result["agent"] == "eng"

    def test_read_missing_returns_none(self, tmp_path):
        assert read_status(tmp_path, "nobody") is None

    def test_write_cook(self, tmp_path):
        write_status(tmp_path, "eng", {"status": "idle"})
        assert (tmp_path / "cooks" / "eng.json").exists()

    def test_sequential_writes_leave_no_temp_files(self, tmp_path):
        """Successful writes must not leak tempfiles into cooks/. Anything
        other than the final <name>.json is a leak."""
        write_status(tmp_path, "eng", {"status": "working", "agent": "eng"})
        write_status(tmp_path, "eng", {"status": "idle", "agent": "eng"})
        write_status(tmp_path, "qa", {"status": "booting", "agent": "qa"})

        cooks = tmp_path / "cooks"
        files = sorted(p.name for p in cooks.iterdir())
        assert files == ["eng.json", "qa.json"], (
            f"Unexpected files in cooks/ — temp file leaked: {files}"
        )

    def test_temp_file_holds_complete_json_before_replace(self, tmp_path, monkeypatch):
        """The atomic-write contract: at the moment os.replace fires, the
        source tempfile already contains a complete, parseable JSON object.
        This is what guarantees readers never see an empty or partial file
        on the final path — they see either the old version or the new
        version, both well-formed."""
        import os as _os
        captured = {}

        real_replace = _os.replace

        def spying_replace(src, dst):
            captured["src_text"] = Path(src).read_text()
            return real_replace(src, dst)

        monkeypatch.setattr("claude_kitchen.state.os.replace", spying_replace)
        write_status(tmp_path, "eng", {"status": "working", "agent": "eng"})

        parsed = json.loads(captured["src_text"])
        assert parsed == {"status": "working", "agent": "eng"}

    def test_update_status_preserves_durable_fields(self, tmp_path):
        """update_status merges new fields onto whatever is already in the
        status file. Durable fields like `tokens` and `backend` survive
        non-completion transitions."""
        write_status(tmp_path, "eng", {
            "status": "idle", "agent": "eng", "backend": "claude",
            "tokens": {"input": 12345, "max": 200_000},
        })
        update_status(tmp_path, "eng", status="working")

        merged = read_status(tmp_path, "eng")
        assert merged["status"] == "working"
        assert merged["backend"] == "claude"
        assert merged["tokens"] == {"input": 12345, "max": 200_000}

    def test_update_status_seeds_when_no_prior_file(self, tmp_path):
        """First update_status against a fresh cook just writes the fields
        — no prior file is fine, no exception."""
        update_status(tmp_path, "eng", status="working", agent="eng")
        result = read_status(tmp_path, "eng")
        assert result == {"status": "working", "agent": "eng"}

    def test_failed_write_cleans_up_temp_file(self, tmp_path, monkeypatch):
        """If os.replace (or the JSON write) raises, the temp file must be
        unlinked and the exception re-raised. Otherwise repeated failures
        accumulate stale .tmp files in cooks/."""
        def boom(src, dst):
            raise OSError("simulated rename failure")

        monkeypatch.setattr("claude_kitchen.state.os.replace", boom)

        with pytest.raises(OSError, match="simulated rename failure"):
            write_status(tmp_path, "eng", {"status": "working", "agent": "eng"})

        cooks = tmp_path / "cooks"
        leftovers = sorted(p.name for p in cooks.iterdir())
        assert leftovers == [], (
            f"Failed write left tempfile(s) behind: {leftovers}"
        )


class TestProjectSlug:
    def _git_remote(self, url):
        m = MagicMock()
        m.returncode = 0
        m.stdout = url + "\n"
        return m

    @patch("claude_kitchen.state.subprocess.run")
    def test_https_github(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("https://github.com/acme/widget.git")
        assert project_slug(tmp_path) == "widget"

    @patch("claude_kitchen.state.subprocess.run")
    def test_ssh_github(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("git@github.com:acme/widget.git")
        assert project_slug(tmp_path) == "widget"

    @patch("claude_kitchen.state.subprocess.run")
    def test_ssh_scheme_strips_userinfo(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("ssh://git@github.com/acme/widget.git")
        assert project_slug(tmp_path) == "widget"

    @patch("claude_kitchen.state.subprocess.run")
    def test_ssh_scheme_strips_userinfo_and_port(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("ssh://git@github.com:22/acme/widget.git")
        assert project_slug(tmp_path) == "widget"

    @patch("claude_kitchen.state.subprocess.run")
    def test_no_dot_git_suffix(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("https://github.com/acme/widget")
        assert project_slug(tmp_path) == "widget"

    @patch("claude_kitchen.state.subprocess.run")
    def test_subgroup_takes_repo_basename(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("https://gitlab.com/acme/subgroup/widget")
        assert project_slug(tmp_path) == "widget"

    @patch("claude_kitchen.state.subprocess.run")
    def test_cross_forge_collision_is_accepted(self, mock_run, tmp_path):
        # Short slug drops forge/owner, so the same repo name on different
        # forges collides by design — the user disambiguates with an explicit
        # kitchen name. Documenting the collision, not guarding against it.
        mock_run.return_value = self._git_remote("https://github.com/x/y")
        gh = project_slug(tmp_path)
        mock_run.return_value = self._git_remote("https://gitlab.com/x/y")
        gl = project_slug(tmp_path)
        assert gh == gl == "y"

    @patch("claude_kitchen.state.subprocess.run")
    def test_unknown_host_uses_repo_basename(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("https://bitbucket.org/team/repo")
        assert project_slug(tmp_path) == "repo"

    @patch("claude_kitchen.state.subprocess.run")
    def test_self_hosted_collision_is_accepted(self, mock_run, tmp_path):
        mock_run.return_value = self._git_remote("https://git.corp-a.example/x/y")
        a = project_slug(tmp_path)
        mock_run.return_value = self._git_remote("https://git.corp-b.example/x/y")
        b = project_slug(tmp_path)
        assert a == b == "y"

    @patch("claude_kitchen.state.subprocess.run")
    def test_no_remote_falls_back_to_full_toplevel_path(self, mock_run, tmp_path):
        no_remote = MagicMock(returncode=1, stdout="", stderr="")
        toplevel = MagicMock(returncode=0, stdout="/Users/dev/work/myrepo\n", stderr="")
        mock_run.side_effect = [no_remote, toplevel]
        assert project_slug(tmp_path) == "users-dev-work-myrepo"

    @patch("claude_kitchen.state.subprocess.run")
    def test_no_remote_different_paths_produce_different_slugs(self, mock_run, tmp_path):
        no_remote = MagicMock(returncode=1, stdout="", stderr="")
        tl_a = MagicMock(returncode=0, stdout="/work/alpha/repo\n", stderr="")
        tl_b = MagicMock(returncode=0, stdout="/work/beta/repo\n", stderr="")
        mock_run.side_effect = [no_remote, tl_a]
        a = project_slug(tmp_path)
        mock_run.side_effect = [no_remote, tl_b]
        b = project_slug(tmp_path)
        assert a != b, "Unrelated repos sharing a basename must not collide"
        assert a == "work-alpha-repo" and b == "work-beta-repo"

    @patch("claude_kitchen.state.subprocess.run")
    def test_not_a_git_repo_fails_loudly(self, mock_run, tmp_path):
        no_remote = MagicMock(returncode=1, stdout="", stderr="")
        not_a_repo = MagicMock(returncode=128, stdout="", stderr="fatal: not a git repository")
        mock_run.side_effect = [no_remote, not_a_repo]
        with pytest.raises(SystemExit, match="not a git repository"):
            project_slug(tmp_path)


class TestNamespaced:
    def _git_remote(self, url):
        return MagicMock(returncode=0, stdout=url + "\n")

    @patch("claude_kitchen.state.subprocess.run")
    def test_scopes_name_by_project_slug(self, mock_run, tmp_path):
        # The SAME requested name in two different projects yields two
        # distinct namespaced kitchens — never a shared bare name.
        mock_run.return_value = self._git_remote("git@github.com:acme/a.git")
        assert namespaced(tmp_path, "foo") == "a-foo"
        mock_run.return_value = self._git_remote("git@github.com:acme/b.git")
        assert namespaced(tmp_path, "foo") == "b-foo"


class TestWikiAndNotesDirs:
    def test_wiki_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        d = wiki_dir("acme-widget")
        assert d == tmp_path / ".claude-kitchen" / "projects" / "acme-widget" / "wiki"

    def test_notes_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        d = notes_dir("risotto")
        assert d == tmp_path / ".claude-kitchen" / "risotto" / "notes"
