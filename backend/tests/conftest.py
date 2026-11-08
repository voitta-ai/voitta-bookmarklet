"""Pytest fixtures.

Every test that touches the filesystem operates inside a tmp_path so
nothing leaks across tests. We monkeypatch ``app.reports.paths`` to
point at the temp dir; the package's own functions accept a ``root=``
override too, used directly in some tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.reports import paths, render_events


@pytest.fixture(autouse=True)
def _isolated_users_and_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep users.sqlite in tmp (never the developer's real data dir) and let
    Starlette's TestClient host ("testserver") pass the desktop loopback-Host
    check, which otherwise (correctly) refuses non-loopback Hosts."""
    from app.services import passwords, site_check, users

    monkeypatch.setattr(users, "db_path", lambda: tmp_path / "users.sqlite")
    monkeypatch.setattr(passwords, "N", 2**10)
    monkeypatch.setattr(site_check, "LOOPBACK_HOSTS", site_check.LOOPBACK_HOSTS | {"testserver"})
    users.reset_for_tests()
    yield
    users.reset_for_tests()


@pytest.fixture
def scripts_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp ``scripts/`` root, swapped into the paths module."""
    root = tmp_path / "scripts"
    root.mkdir()
    folders = root / "folders"
    monkeypatch.setattr(paths, "SCRIPTS_DIR", root)
    monkeypatch.setattr(paths, "SCRIPTS_FOLDERS_DIR", folders)
    # ``store`` reads SCRIPTS_DIR at call time via module attr — but
    # ``store`` imported it once, so we patch there too. Both constants,
    # or list_scripts() walks the developer's REAL foldered scripts.
    from app.reports import store

    monkeypatch.setattr(store, "SCRIPTS_DIR", root)
    monkeypatch.setattr(store, "SCRIPTS_FOLDERS_DIR", folders)
    return root


@pytest.fixture
def render_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate render-event disk state per test."""
    errs = tmp_path / "errors"
    inv = tmp_path / "inventory"
    errs.mkdir()
    inv.mkdir()
    monkeypatch.setattr(paths, "ERROR_LOGS_DIR", errs)
    monkeypatch.setattr(paths, "INVENTORY_DIR", inv)
    monkeypatch.setattr(render_events, "ERROR_LOGS_DIR", errs)
    monkeypatch.setattr(render_events, "INVENTORY_DIR", inv)
    render_events._reset_for_tests()
    yield tmp_path
    render_events._reset_for_tests()
