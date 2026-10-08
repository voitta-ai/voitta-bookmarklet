"""RAG index builder with code-corpus hash protection.

Wraps ``scripts/build_rag.py`` and adds a stamp-based skip: if the
lib-sources submodule SHAs haven't changed since the last successful
build, the (slow) code corpus reindex is skipped.

Stamp resolution:
  - Frozen .app: reads ``voitta_bookmarklet/resources/code_sources_version.txt``
    (written by ``build_app.sh`` via ``git submodule status lib-sources``).
  - Dev checkout:  runs ``git submodule status lib-sources`` live.

Deployed stamp lives at ``<user_data>/rag/.code_source_hash``.
If current stamp == deployed stamp AND code chroma dir exists → skip.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

ProgressCb = Callable[[str], None]

last_failure_detail: str = ""


# ---------------------------------------------------------------------------
# Stamp helpers
# ---------------------------------------------------------------------------

def _bundle_resources() -> Path | None:
    try:
        import voitta_bookmarklet
        return Path(voitta_bookmarklet.__file__).resolve().parent / "resources"
    except ImportError:
        return None


def _current_code_stamp() -> str:
    """Return the canonical submodule-SHA fingerprint for the current build."""
    resources = _bundle_resources()
    if resources:
        stamp_file = resources / "code_sources_version.txt"
        if stamp_file.is_file():
            return stamp_file.read_text(encoding="utf-8").strip()

    # Dev fallback: ask git
    from app.config import PROJECT_ROOT
    repo_root = PROJECT_ROOT.parent
    try:
        result = subprocess.run(
            ["git", "submodule", "status", "lib-sources"],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _deployed_stamp_path() -> Path:
    from app.config import USER_CONFIG_DIR
    return USER_CONFIG_DIR / ".code_source_hash"


def _docs_stamp_path() -> Path:
    from app.config import USER_CONFIG_DIR
    return USER_CONFIG_DIR / ".docs_content_hash"


def _docs_content_hash() -> str:
    """SHA-256 over the sorted (relative-path, content) of every file fed to the docs corpus.

    Mirrors the file selection in ``assemble_docs_chunks()``:
    - all .md files under DOCS_DIR
    - all .md files under PLUGINS_DIR/<plugin>/docs/
    - all .md files under PLUGIN_DOCS_DIR (fetched at install time)

    The last one is what makes a freshly-cloned plugin docs repo trigger a
    re-index: the hash changes, the stamp no longer matches, and build_all()
    rebuilds the docs corpus on the launch that fetched it.
    """
    from app.config import DOCS_DIR, PLUGIN_DOCS_DIR, PLUGINS_DIR

    h = hashlib.sha256()
    pairs: list[tuple[str, Path]] = []

    if DOCS_DIR.is_dir():
        for f in sorted(DOCS_DIR.rglob("*.md")):
            if f.is_file():
                pairs.append((str(f.relative_to(DOCS_DIR.parent)), f))

    if PLUGINS_DIR.is_dir():
        for plugin_dir in sorted(PLUGINS_DIR.iterdir()):
            docs_dir = plugin_dir / "docs"
            if docs_dir.is_dir():
                for f in sorted(docs_dir.rglob("*.md")):
                    if f.is_file():
                        pairs.append((str(f.relative_to(PLUGINS_DIR.parent)), f))

    if PLUGIN_DOCS_DIR.is_dir():
        for f in sorted(PLUGIN_DOCS_DIR.rglob("*.md")):
            if f.is_file():
                pairs.append((str(f.relative_to(PLUGIN_DOCS_DIR.parent)), f))

    for rel, f in pairs:
        h.update(rel.encode())
        h.update(f.read_bytes())

    return h.hexdigest() if pairs else ""


def _code_index_current() -> bool:
    """True when the code corpus exists AND its stamp matches the current build."""
    from app.config import RAG_DIR
    if not (RAG_DIR / ".chroma_code").is_dir():
        return False
    stamp_path = _deployed_stamp_path()
    if not stamp_path.is_file():
        return False
    current = _current_code_stamp()
    return bool(current) and stamp_path.read_text(encoding="utf-8").strip() == current


def _docs_index_current() -> bool:
    """True when docs chroma+bm25 exist AND content hash matches."""
    from app.config import RAG_DIR
    if not (RAG_DIR / ".chroma").is_dir() or not (RAG_DIR / ".bm25").is_dir():
        return False
    stamp = _docs_stamp_path()
    if not stamp.is_file():
        return False  # no stored hash → cold start → rebuild
    current = _docs_content_hash()
    # Empty hash means no source docs exist; treat as up-to-date so we don't
    # loop on machines with no docs directory.
    if not current:
        return True
    return stamp.read_text(encoding="utf-8").strip() == current


def is_built() -> bool:
    """Both corpora present and code stamp up to date."""
    return _docs_index_current() and _code_index_current()


# ---------------------------------------------------------------------------
# Build helpers
# ---------------------------------------------------------------------------

def _find_build_rag_script() -> Path:
    """Locate scripts/build_rag.py whether running from repo or bundle."""
    from app.config import PROJECT_ROOT

    # Dev: <repo>/scripts/build_rag.py
    dev = PROJECT_ROOT.parent / "scripts" / "build_rag.py"
    if dev.is_file():
        return dev

    # Frozen: briefcase includes scripts/ alongside app/ under Resources/app/
    bundle = Path(__file__).resolve().parent.parent / "scripts" / "build_rag.py"
    if bundle.is_file():
        return bundle

    raise FileNotFoundError(
        f"scripts/build_rag.py not found (looked in {dev} and {bundle})"
    )


def _run_build(corpus: str, progress_cb: ProgressCb) -> bool:
    """Run build_rag.py inline via runpy (no subprocess).

    briefcase macOS bundles have no standalone python3 binary — only the
    Python.framework dylib. Spawning sys.executable would relaunch the app.
    Instead we run the script in-process using runpy.run_path with
    __name__="__main__", which honours the if-__name__-guard correctly
    and uses the real sys.argv for argparse.
    """
    import io
    import runpy

    from app.config import (
        PROJECT_ROOT, DOCS_DIR, PLUGIN_DOCS_DIR, PLUGINS_DIR, RAG_DIR,
    )

    libs_dir = PROJECT_ROOT.parent / "lib-sources"
    script = _find_build_rag_script()

    os.environ.update({
        "VOITTA_DOCS_DIR":        str(DOCS_DIR),
        "VOITTA_PLUGINS_DIR":     str(PLUGINS_DIR),
        "VOITTA_PLUGIN_DOCS_DIR": str(PLUGIN_DOCS_DIR),
        "VOITTA_LIBS_DIR":        str(libs_dir),
        "VOITTA_RAG_DIR":         str(RAG_DIR),
    })

    class _Capture(io.RawIOBase):
        """Minimal writable stream that routes lines to progress_cb."""
        def write(self, b) -> int:  # type: ignore[override]
            text = b.decode("utf-8", errors="replace") if isinstance(b, (bytes, bytearray)) else b
            if text and text.strip():
                for ln in text.splitlines():
                    if ln.strip():
                        progress_cb(ln)
            return len(b)
        def writable(self) -> bool:
            return True

    cap = io.TextIOWrapper(_Capture(), encoding="utf-8", line_buffering=True)

    old_argv = sys.argv[:]
    old_out, old_err = sys.stdout, sys.stderr
    sys.argv = [str(script), "--corpus", corpus]
    sys.stdout = sys.stderr = cap
    try:
        runpy.run_path(str(script), run_name="__main__")
        return True
    except SystemExit as exc:
        return (exc.code or 0) == 0
    except Exception as exc:  # noqa: BLE001
        global last_failure_detail
        last_failure_detail = str(exc)
        import traceback
        progress_cb(f"!!! error in build_rag ({corpus}): {exc}")
        for ln in traceback.format_exc().splitlines():
            if ln.strip():
                progress_cb(ln)
        return False
    finally:
        sys.stdout = old_out
        sys.stderr = old_err
        sys.argv = old_argv


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_all(progress_cb: ProgressCb) -> bool:
    """Build docs + code corpora, skipping code if stamp matches.

    ``progress_cb(line)`` is called on the worker thread with log lines.
    Returns True on full success, False if any corpus build fails.
    """
    from app.config import RAG_DIR

    global last_failure_detail
    last_failure_detail = ""

    from app.config import USER_CONFIG_DIR
    RAG_DIR.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Docs corpus — skip if content hash matches.
    current_docs_hash = _docs_content_hash()
    if _docs_index_current():
        progress_cb("Docs corpus unchanged — skipping re-index.")
    else:
        progress_cb("Building docs corpus…")
        if not _run_build("docs", progress_cb):
            return False
        if current_docs_hash:
            _docs_stamp_path().write_text(current_docs_hash, encoding="utf-8")
        progress_cb("Docs corpus ready.")

    # Code corpus — skip if stamp matches.
    current_stamp = _current_code_stamp()
    if _code_index_current():
        progress_cb("Code corpus unchanged — skipping re-index.")
        return True

    from app.config import PROJECT_ROOT
    libs_dir = PROJECT_ROOT.parent / "lib-sources"
    if not libs_dir.is_dir():
        progress_cb("lib-sources/ not found — skipping code corpus.")
        return True

    progress_cb("Building code corpus (this takes a few minutes)…")
    if not _run_build("code", progress_cb):
        return False

    # Write stamp so next launch skips this.
    if current_stamp:
        _deployed_stamp_path().write_text(current_stamp, encoding="utf-8")
        progress_cb(f"Code stamp written: {current_stamp[:40]}…")

    return True
