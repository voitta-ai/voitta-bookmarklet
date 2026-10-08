"""Voitta Compute → Voitta Bookmarklet data-dir adoption (app.brand_migration),
run against a fake $HOME laid out like a long-lived machine: the original
voitta-bookmarklet app's stale dirs, plus the live Voitta Compute ones."""

from __future__ import annotations

import fcntl
import sys
from pathlib import Path

import pytest

from app import brand_migration as bm

AS = Path("Library") / "Application Support"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    return tmp_path


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _seed(home: Path) -> str:
    _write(home / ".config/voitta-bookmarklet/settings.json", "original")
    _write(home / AS / "Voitta Bookmarklet/voitta.log", "original")
    _write(home / ".config/voitta-compute/settings.json", "compute")
    _write(home / AS / "Voitta Compute/backend/conversations.sqlite", "compute")
    old_key = bm._claude_project_key(home / AS / "Voitta Compute/backend/claude_code/workspace")
    _write(home / AS / "Voitta Compute/backend/claude_code/config/projects" / old_key / "s.jsonl", "{}")
    return old_key


def test_adopts_compute_and_sets_original_aside(home: Path) -> None:
    _seed(home)
    assert bm.migrate()

    assert (home / ".config/voitta-bookmarklet/settings.json").read_text() == "compute"
    assert (home / ".config/voitta-bookmarklet.legacy/settings.json").read_text() == "original"
    assert not (home / ".config/voitta-compute").exists()

    data = home / AS / "Voitta Bookmarklet"
    assert (data / "backend/conversations.sqlite").read_text() == "compute"
    assert (home / AS / "Voitta Bookmarklet.legacy/voitta.log").read_text() == "original"
    assert not (home / AS / "Voitta Compute").exists()

    assert bm.legacy_config_dir() == home / ".config/voitta-bookmarklet.legacy"


def test_rekeys_claude_session_store(home: Path) -> None:
    old_key = _seed(home)
    bm.migrate()
    projects = home / AS / "Voitta Bookmarklet/backend/claude_code/config/projects"
    new_key = bm._claude_project_key(home / AS / "Voitta Bookmarklet/backend/claude_code/workspace")
    assert [d.name for d in projects.iterdir()] == [new_key]
    assert (projects / new_key / "s.jsonl").is_file()
    assert old_key != new_key


def test_idempotent(home: Path) -> None:
    _seed(home)
    bm.migrate()
    # The new app writes data; a later run must neither move it nor re-adopt.
    _write(home / AS / "Voitta Bookmarklet/backend/new.txt", "x")
    _write(home / ".config/voitta-compute/settings.json", "stray")
    assert bm.migrate()
    assert (home / AS / "Voitta Bookmarklet/backend/new.txt").is_file()
    assert (home / ".config/voitta-bookmarklet/settings.json").read_text() == "compute"
    assert not (home / ".config/voitta-bookmarklet.legacy-2").exists()


def test_fresh_install_creates_marked_dirs(home: Path) -> None:
    assert bm.migrate()
    assert (home / ".config/voitta-bookmarklet" / bm._MARKER).is_file()
    assert (home / AS / "Voitta Bookmarklet" / bm._MARKER).is_file()
    assert not (home / ".config/voitta-bookmarklet.legacy").exists()


def test_falls_back_to_chainlit(home: Path) -> None:
    _write(home / ".config/voitta-bookmarklet-chainlit/settings.json", "chainlit")
    _write(home / AS / "Voitta Chainlit/backend/x", "chainlit")
    bm.migrate()
    assert (home / ".config/voitta-bookmarklet/settings.json").read_text() == "chainlit"
    assert (home / AS / "Voitta Bookmarklet/backend/x").read_text() == "chainlit"


def test_leaves_symlinked_config_alone(home: Path) -> None:
    # The Docker image links ~/.config/voitta-bookmarklet → /data/config.
    volume = home / "data/config"
    _write(volume / "settings.json", "volume")
    (home / ".config").mkdir()
    (home / ".config/voitta-bookmarklet").symlink_to(volume)
    assert bm.migrate()
    assert (home / ".config/voitta-bookmarklet").is_symlink()
    assert (home / ".config/voitta-bookmarklet/settings.json").read_text() == "volume"
    assert not (home / ".config/voitta-bookmarklet.legacy").exists()


def test_refuses_while_compute_is_running(home: Path) -> None:
    _seed(home)
    lock = home / AS / "Voitta Compute/.voitta.lock"
    with lock.open("w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not bm.migrate()
    # Nothing moved — not even the config dir.
    assert (home / ".config/voitta-compute/settings.json").is_file()
    assert (home / ".config/voitta-bookmarklet/settings.json").read_text() == "original"
    assert bm.migrate()
    assert (home / AS / "Voitta Bookmarklet/backend/conversations.sqlite").is_file()
