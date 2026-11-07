import shutil
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _private_tmux(monkeypatch):
    """Every tmux socket a test touches lives under its own tmp dir, so a test
    whose mocks miss a call can never reach (or kill) a live kitchen. Under
    /tmp, not tmp_path: macOS's tmp_path is too deep for a unix socket path."""
    d = tempfile.mkdtemp(prefix="ck-", dir="/tmp")
    monkeypatch.setenv("TMUX_TMPDIR", d)
    yield
    shutil.rmtree(d, ignore_errors=True)
