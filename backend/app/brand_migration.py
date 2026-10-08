"""One-time adoption of user data from the app's previous names.

The app shipped as "Voitta Compute" (and before that "Voitta Chainlit") and
is now "Voitta Bookmarklet" — a name the *original* app also used, so on a
long-lived machine the target dirs may already exist holding that original
app's stale May-era data. :func:`migrate` therefore, once per dir (guarded by
a marker file inside it):

  1. moves an unmarked target aside to ``<name>.legacy`` — kept, never
     deleted; the launcher backfills a few settings keys from the legacy
     config (see ``_backfill_legacy_bookmarklet_keys`` in __main__.py);
  2. renames the newest predecessor dir into place (same volume → atomic, no
     multi-GB copy);
  3. re-keys the embedded Claude Code session store, which is indexed by the
     absolute workspace path (see app.services.agent_sdk.config) and would
     otherwise lose every resumable session.

Stdlib-only and import-light: the .app launcher runs it before any other
app.* module, and the dev scripts run ``python -m app.brand_migration``.
"""

from __future__ import annotations

import fcntl
import re
import sys
from pathlib import Path

CONFIG_NAME = "voitta-bookmarklet"
APP_SUPPORT_NAME = "Voitta Bookmarklet"

# Newest first — the first one that exists is adopted.
_CONFIG_PREDECESSORS = ("voitta-compute", "voitta-bookmarklet-chainlit")
_APP_SUPPORT_PREDECESSORS = ("Voitta Compute", "Voitta Chainlit")

_MARKER = ".brand-migrated"


def _config_root() -> Path:
    return Path.home() / ".config"


def _app_support_root() -> Path:
    return Path.home() / "Library" / "Application Support"


def legacy_config_dir() -> Path:
    """Where the original voitta-bookmarklet app's config is moved aside to."""
    return _config_root() / f"{CONFIG_NAME}.legacy"


def _held_by_running_app(data_dir: Path) -> bool:
    """True if a pre-rename app is still running from *data_dir* (it holds
    the flock on its instance lock file — see __main__._acquire_instance_lock)."""
    lock = data_dir / ".voitta.lock"
    if not lock.is_file():
        return False
    try:
        with lock.open("a") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    return False


def _set_aside(path: Path) -> None:
    n = 1
    while True:
        dest = path.with_name(f"{path.name}.legacy" + (f"-{n}" if n > 1 else ""))
        if not dest.exists():
            path.rename(dest)
            return
        n += 1


def _adopt(new: Path, predecessors: list[Path]) -> Path | None:
    """Make *new* the live dir, adopting the first existing predecessor.
    Returns the adopted predecessor, or None (already done / nothing found)."""
    # A symlink was put there on purpose — the Docker image links the config
    # dir into its /data volume. Moving it aside would unhook persistence.
    if new.is_symlink() or (new / _MARKER).exists():
        return None
    if new.exists():
        _set_aside(new)
    src = next((p for p in predecessors if p.is_dir()), None)
    if src is not None:
        src.rename(new)
    new.mkdir(parents=True, exist_ok=True)
    (new / _MARKER).write_text(f"{src or ''}\n", encoding="utf-8")
    return src


def _claude_project_key(path: Path) -> str:
    # Claude Code names a project's session dir after its cwd with every
    # non-alphanumeric character replaced by "-".
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _rekey_claude_sessions(new: Path, old: Path) -> None:
    old_key, new_key = _claude_project_key(old), _claude_project_key(new)
    backend = new / "backend"
    stores = [
        backend / "claude_code" / "config" / "projects",
        *backend.glob("users/*/claude_code/config/projects"),
    ]
    for store in stores:
        if not store.is_dir():
            continue
        for d in store.iterdir():
            if d.name.startswith(old_key):
                target = store / (new_key + d.name[len(old_key):])
                if not target.exists():
                    d.rename(target)


def migrate() -> bool:
    """Adopt the predecessor config + data dirs. Idempotent and cheap once
    done (one stat per dir). Returns False — having changed nothing — while
    the pre-rename app is still running; the caller should exit."""
    data = _app_support_root() / APP_SUPPORT_NAME
    old_data = [_app_support_root() / n for n in _APP_SUPPORT_PREDECESSORS]
    # The Application Support layout only exists on macOS; Linux servers keep
    # their data under VOITTA_DATA_ROOT, which never changed name.
    data_pending = sys.platform == "darwin" and not (data / _MARKER).exists()
    if data_pending and any(_held_by_running_app(p) for p in old_data):
        return False

    _adopt(
        _config_root() / CONFIG_NAME,
        [_config_root() / n for n in _CONFIG_PREDECESSORS],
    )
    if data_pending:
        src = _adopt(data, old_data)
        if src is not None:
            _rekey_claude_sessions(data, src)
    return True


if __name__ == "__main__":
    if not migrate():
        print(
            "Voitta Compute is still running — quit it from the menu bar, "
            "then start again so its data can move to Voitta Bookmarklet.",
            file=sys.stderr,
        )
        sys.exit(1)
