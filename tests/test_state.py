"""Tests for state read/write."""
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from claude_kitchen.state import (
    state_dir, write_status, read_status, update_status,
    project_slug, namespaced, wiki_dir, notes_dir,
)


def test_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = state_dir("risotto")
    assert d == tmp_path / ".claude-kitchen" / "risotto"


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

    @patch("claude_kitchen.state.subprocess.run")
    def test_collapses_when_requested_equals_slug(self, mock_run, tmp_path):
        # `kitchen open` with no name from a repo root sets requested to the
        # dir name, which usually equals the slug. Don't double it:
        # `seed-domo`, not `seed-domo-seed-domo`.
        mock_run.return_value = self._git_remote("git@github.com:acme/seed-domo.git")
        assert namespaced(tmp_path, "seed-domo") == "seed-domo"
        # A genuinely different requested name is still slug-scoped.
        assert namespaced(tmp_path, "feature") == "seed-domo-feature"

    @patch("claude_kitchen.state.subprocess.run")
    def test_idempotent_for_already_prefixed_name(self, mock_run, tmp_path):
        # An already slug-scoped name must not be re-prefixed — otherwise
        # re-opening (or opening from a worktree dir already named for the
        # kitchen) stacks the slug, the source of the on-disk
        # my-project-my-project-my-project-feature-x triple.
        mock_run.return_value = self._git_remote("git@github.com:acme/my-project.git")
        once = namespaced(tmp_path, "feature-x")
        assert once == "my-project-feature-x"
        # Feeding the namespaced name back in is a no-op, repeatedly.
        twice = namespaced(tmp_path, once)
        assert twice == "my-project-feature-x"
        assert namespaced(tmp_path, twice) == "my-project-feature-x"
        # A near-miss sharing the slug as a prefix but not at a `-` boundary
        # is still scoped, not mistaken for already-namespaced.
        assert namespaced(tmp_path, "my-projecty") == "my-project-my-projecty"


class TestWikiAndNotesDirs:
    def test_wiki_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        d = wiki_dir("acme-widget")
        assert d == tmp_path / ".claude-kitchen" / "projects" / "acme-widget" / "wiki"

    def test_notes_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        d = notes_dir("risotto")
        assert d == tmp_path / ".claude-kitchen" / "risotto" / "notes"
