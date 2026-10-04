"""Briefcase entry point for the Voitta Compute .app bundle.

Responsibilities (all before importing any app.* module):
  1. Bail out if we're a multiprocessing spawn child.
  2. Prepare the writable user data dir under ~/Library/Application Support/.
  3. Set VOITTA_PROJECT_ROOT so app.config resolves paths to the user data dir.
  4. Configure PIP_PREFIX so lazy installs land in userbase/, not the bundle.
  5. Redirect stdout/stderr to logs/voitta.log (visible in Console.app).
  6. Seed resources (frontend, docs, plugins, lib-sources) into the user data dir.
  7. Hand off to app.desktop.main().
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import signal
import sys
from pathlib import Path

# The Briefcase stub starts Python with an isolated config, which skips
# Python's own signal setup — including ignoring SIGPIPE. Without this, a
# write to a socket the peer already closed (an MCP client dropping a
# connection mid-response, a browser closing a tab) kills the whole app
# instantly: exit 141, no traceback, no crash report. Ignored, the write
# raises BrokenPipeError instead, which the servers handle.
signal.signal(signal.SIGPIPE, signal.SIG_IGN)

# freeze_support() must be at module level in a frozen executable so that
# multiprocessing spawn children (which re-run this module) exit immediately
# after running their target function without touching Cocoa or any UI code.
multiprocessing.freeze_support()


# ---------------------------------------------------------------------------
# Multiprocessing / subprocess guard
# ---------------------------------------------------------------------------

def _is_mp_child() -> bool:
    """Return True if this process was spawned by Python's multiprocessing."""
    # Standard spawn marker added to sys.argv by multiprocessing.spawn
    if any("--multiprocessing-fork" in a for a in sys.argv):
        return True
    # Older macOS / fork-server style
    if os.environ.get("MULTIPROCESSING_FORKING_DISABLE"):
        return True
    return False


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_APP_SUPPORT_NAME = "Voitta Compute"


def _user_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / _APP_SUPPORT_NAME


def _bundle_resources_dir() -> Path:
    """Resources subtree shipped inside the .app bundle."""
    try:
        import voitta_compute
        return Path(voitta_compute.__file__).resolve().parent / "resources"
    except Exception:
        pass
    rp = os.environ.get("RESOURCEPATH")
    if rp:
        return Path(rp)
    return Path(sys.executable).resolve().parent.parent / "Resources"


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

def _seed_overwrite(src: Path, dst: Path) -> None:
    """Copy src → dst, overwriting every file. For frontend/docs/plugins."""
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def _seed_lib_sources(src: Path, dst: Path, stamp_src: Path) -> None:
    """Seed lib-sources only when the submodule stamp changed.

    Avoids copying ~80 MB of text files on every launch when nothing changed.
    """
    if not src.is_dir():
        return
    if not stamp_src.is_file():
        return
    new_stamp = stamp_src.read_text(encoding="utf-8").strip()
    deployed_stamp = dst / ".sources_version"
    if deployed_stamp.is_file() and deployed_stamp.read_text(encoding="utf-8").strip() == new_stamp:
        return  # already up to date
    shutil.copytree(src, dst, dirs_exist_ok=True)
    deployed_stamp.write_text(new_stamp, encoding="utf-8")


def _migrate_settings() -> None:
    """One-time migration: copy settings from old Voitta Chainlit config dir."""
    old_config = Path.home() / ".config" / "voitta-bookmarklet-chainlit" / "settings.json"
    new_config = Path.home() / ".config" / "voitta-compute" / "settings.json"
    if old_config.is_file() and not new_config.is_file():
        new_config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_config, new_config)

    _backfill_legacy_bookmarklet_keys(new_config)


# Settings that only ever existed in the original bookmarklet app's blob.
# The chainlit-era schema rewrite dropped them, so the chainlit→compute
# copy above can't carry them; pull them straight from the original file.
_LEGACY_BOOKMARKLET_KEYS = ("googleOAuth", "driveDownloadViaPickup", "pickupDownloadsDir")


def _backfill_legacy_bookmarklet_keys(new_config: Path) -> None:
    """Backfill keys lost in the bookmarklet→chainlit schema rewrite.

    Google OAuth credentials/tokens (and the Drive pickup settings) were
    configured in the original voitta-bookmarklet app but never made it
    into the chainlit-era blob, so users who migrated through chainlit
    land in voitta-compute with OAuth silently unconfigured. Runs on
    every launch but only writes when a key is missing here and present
    in the legacy file — existing values are never overwritten.
    """
    legacy = Path.home() / ".config" / "voitta-bookmarklet" / "settings.json"
    if not legacy.is_file() or not new_config.is_file():
        return
    try:
        legacy_blob = json.loads(legacy.read_text(encoding="utf-8"))
        blob = json.loads(new_config.read_text(encoding="utf-8"))
        if not isinstance(legacy_blob, dict) or not isinstance(blob, dict):
            return
        missing = [
            k for k in _LEGACY_BOOKMARKLET_KEYS
            if k not in blob and k in legacy_blob
        ]
        if not missing:
            return
        for k in missing:
            blob[k] = legacy_blob[k]
        tmp = new_config.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(blob, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, new_config)
    except Exception:
        # Never block app launch on a best-effort backfill.
        pass

    old_app_support = Path.home() / "Library" / "Application Support" / "Voitta Chainlit"
    new_app_support = Path.home() / "Library" / "Application Support" / "Voitta Compute"
    if old_app_support.is_dir() and not new_app_support.exists():
        shutil.copytree(old_app_support, new_app_support, dirs_exist_ok=False)


def _prepare_user_data_dir() -> Path:
    _migrate_settings()
    user = _user_data_dir()
    user.mkdir(parents=True, exist_ok=True)

    res = _bundle_resources_dir()

    # frontend/dist — overwrite every launch so .app upgrades propagate.
    _seed_overwrite(res / "frontend_dist", user / "frontend" / "dist")

    # docs + plugins — replace every launch (small, fast). rmtree first so
    # removed or reorganised files don't survive across .app upgrades.
    _seed_overwrite(res / "docs", user / "docs")
    plugins_dst = user / "plugins"
    if plugins_dst.exists():
        shutil.rmtree(plugins_dst)
    _seed_overwrite(res / "plugins", plugins_dst)

    # lib-sources — stamp-gated copy (may be large).
    _seed_lib_sources(
        res / "lib-sources",
        user / "lib-sources",
        res / "code_sources_version.txt",
    )

    # Ensure writable scaffolding exists.
    (user / "backend" / "certs").mkdir(parents=True, exist_ok=True)
    (user / "scripts" / "compute").mkdir(parents=True, exist_ok=True)
    (user / "scripts" / "reports").mkdir(parents=True, exist_ok=True)

    return user


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _acquire_instance_lock() -> bool:
    """Return True if this is the first (and only) running instance.

    Uses a lock file + flock so the lock releases automatically when the
    process exits, even on a crash.
    """
    import fcntl
    lock_path = Path.home() / "Library" / "Application Support" / _APP_SUPPORT_NAME / ".voitta.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Keep the file descriptor alive for the process lifetime by storing
        # it on the module — if it goes out of scope the lock is released.
        _acquire_instance_lock._fd = lock_path.open("w")
        fcntl.flock(_acquire_instance_lock._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, BlockingIOError):
        return False


def main() -> int:
    if _is_mp_child():
        return 0

    if not _acquire_instance_lock():
        # Another instance is already running — silently exit.
        return 0

    user = _prepare_user_data_dir()

    # VOITTA_PROJECT_ROOT = user_data/backend so that app.config computes:
    #   PROJECT_ROOT     = <user>/backend
    #   TLS_CERT_PATH    = <user>/backend/certs/...
    #   RAG_DIR          = <user>/rag
    #   PLUGINS_DIR      = <user>/plugins
    #   DOCS_DIR         = <user>/docs
    os.environ["VOITTA_PROJECT_ROOT"] = str(user / "backend")

    # USER_DATA_ROOT resolves from VOITTA_DATA_ROOT (see app.config). In the
    # bundle it's the same <user>/backend dir as PROJECT_ROOT; set it
    # explicitly so the config default (a bare macOS path) is never relied on.
    os.environ["VOITTA_DATA_ROOT"] = str(user / "backend")

    # Route pip installs to userbase/ — prefix layout so pip's resolver
    # sees already-bundled packages and skips them (saves ~300 MB vs --target).
    py_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    user_prefix = user / "userbase"
    user_site = user_prefix / "lib" / py_dir / "site-packages"
    user_site.mkdir(parents=True, exist_ok=True)
    os.environ["PIP_PREFIX"] = str(user_prefix)
    if str(user_site) not in sys.path:
        sys.path.insert(0, str(user_site))

    # Log directory: all logs (launcher voitta.log, app voitta-app.log,
    # voitta-stacks.log) live under backend/logs/ so the tray "Open logs
    # folder…" item reveals a clean folder the user can drag into an email.
    # Each launch starts fresh; the previous runs are archived under
    # logs/previous/run-1 (most recent) … run-N (oldest) so a crash-before-
    # capture is still recoverable — support flow: ask a remote user to
    # reproduce, then send logs/. Retention is capped at LOG_KEEP_RUNS to
    # bound total growth. This runs before any log file for THIS run is
    # opened, so it only ever moves prior runs' files.
    LOG_KEEP_RUNS = 3
    logs_dir = user / "backend" / "logs"
    try:
        archive = logs_dir / "previous"
        if logs_dir.is_dir():
            archive.mkdir(parents=True, exist_ok=True)
            # Drop the oldest, then shift run-i → run-(i+1) from oldest to
            # newest so no rename clobbers a slot still in use.
            oldest = archive / f"run-{LOG_KEEP_RUNS}"
            if oldest.exists():
                shutil.rmtree(oldest, ignore_errors=True)
            for i in range(LOG_KEEP_RUNS - 1, 0, -1):
                src = archive / f"run-{i}"
                if src.exists():
                    try:
                        src.rename(archive / f"run-{i + 1}")
                    except OSError:
                        pass
            # This run's just-finished files become run-1.
            run1 = archive / "run-1"
            run1.mkdir(parents=True, exist_ok=True)
            for entry in logs_dir.iterdir():
                if entry.name == "previous":
                    continue
                try:
                    entry.rename(run1 / entry.name)
                except OSError:
                    pass
        logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    # Redirect stdout/stderr to logs/voitta.log (frozen .app has no terminal).
    # Fresh each launch — prior runs were just archived to logs/previous/run-*.
    log_path = logs_dir / "voitta.log"
    try:
        log_fp = log_path.open("w", buffering=1, encoding="utf-8")
        sys.stdout = log_fp
        sys.stderr = log_fp
    except OSError:
        pass

    # Clear MCP dump files from the previous session.
    dumps_dir = user / "mcp_dumps"
    try:
        if dumps_dir.exists():
            shutil.rmtree(dumps_dir)
        dumps_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    from app.desktop import main as desktop_main
    return desktop_main()


if __name__ == "__main__":
    sys.exit(main())
