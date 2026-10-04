"""First-launch installer for heavy Python packages.

These are pulled out of the bundle to keep the .app smaller. On first
launch the desktop entry point calls :func:`is_complete`; if False it
shows the installer window (phase 1) and runs :func:`install_all` on
a worker thread.

State is tracked in ``<user_data>/install_state.json`` so a partial
install resumes on the next launch instead of redoing everything.

The state file records ``app_version`` and ``installed: [name, ...]``.
A version bump (new .app) wipes ``userbase/`` and ``rag/`` via
``ensure_fresh_deploy`` so pip and RAG both rerun cleanly.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

# (import-name, pip-spec)
_CORE_HEAVY_PACKAGES: list[tuple[str, str]] = [
    # Capped below 4.0: fastmcp 4.x requires a newer `mcp` than chainlit 2.11.1
    # (installed right after) allows, so pip backtracks to mcp 1.30.0 — which
    # lacks `AuthorizationCodeResult` and makes `fastmcp.Client` fail to import,
    # killing the backend on first launch. Lift when chainlit's mcp pin catches up.
    ("fastmcp",    "fastmcp>=2.0,<4"),
    ("chainlit",   "chainlit==2.11.1"),
    ("anthropic",  "anthropic>=0.39"),
    ("openai",     "openai>=1.50"),
    ("google.genai", "google-genai>=0.3"),
    # Claude Agent SDK — the 4th "brain" (Claude Pro/Max subscription via the
    # Claude Code engine). The engine binary itself is a separate install the
    # brain probes for at runtime; this is just the Python driver.
    ("claude_agent_sdk", "claude-agent-sdk>=0.2.82"),
    # Argument validation for the agent tools on /mcp (mcp_registry).
    ("jsonschema", "jsonschema>=4"),
    # Upper bound matches numba's numpy ceiling. numba (pulled in by the voice
    # assistant's mlx_whisper) requires numpy<2.5; without this cap the main
    # install resolves numpy 2.5, the live process imports it, and a later voice
    # install downgrades numpy ON DISK to 2.4.x — but the in-memory 2.5 stays,
    # so numba's import-time check fails ("Got NumPy 2.5") and the voice thread
    # crashes. Capping here keeps a single numba-compatible numpy from boot, so
    # there's no in-process version skew. Bump when numba supports numpy 2.5+.
    ("numpy",      "numpy>=1.26,<2.5"),
    ("PIL",        "pillow>=10.0"),
    ("pypdf",      "pypdf>=5.0"),
    ("bm25s",      "bm25s>=0.2.6"),
    ("Stemmer",    "PyStemmer>=2.2.0"),
    ("scipy",      "scipy>=1.11"),
    ("pandas",     "pandas"),
    ("pyarrow",    "pyarrow>=15.0"),
    ("h5py",       "h5py>=3.10"),
    ("matplotlib", "matplotlib>=3.8"),
    ("plotly",     "plotly>=5.20"),
    ("chromadb",   "chromadb>=0.5.20"),  # biggest: pulls onnxruntime + grpc
    ("rdflib",     "rdflib>=7.0"),
    ("networkx",   "networkx>=3.0"),
    ("aiosqlite",  "aiosqlite>=0.17"),
    ("sqlalchemy", "sqlalchemy[asyncio]>=2.0"),
    # Generic, programmatic PDF builder — pure-Python, wheels-only. Report
    # scripts construct PDFs directly (pages/text/tables/images/vector).
    # Import name: fpdf.
    ("fpdf", "fpdf2>=2.7"),
]


def _plugin_dependencies() -> list[tuple[str, str]]:
    """Walk plugin manifests and collect ``python_dependencies`` entries."""
    import json as _json

    seen: dict[str, str] = {}
    candidate_dirs: list[Path] = []
    here = Path(__file__).resolve()
    repo_root = here.parents[2]
    for d in (repo_root / "plugins",):
        if d.is_dir():
            candidate_dirs.append(d)
    try:
        import voitta_compute
        bundled = Path(voitta_compute.__file__).resolve().parent / "resources" / "plugins"
        if bundled.is_dir() and bundled not in candidate_dirs:
            candidate_dirs.append(bundled)
    except Exception:
        pass

    for plugins_root in candidate_dirs:
        for plugin_dir in plugins_root.iterdir():
            if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                continue
            mf = plugin_dir / "manifest.json"
            if not mf.is_file():
                continue
            try:
                manifest = _json.loads(mf.read_text())
            except Exception:
                continue
            for d in manifest.get("python_dependencies") or []:
                if not isinstance(d, dict):
                    continue
                name = d.get("import")
                spec = d.get("spec") or name
                if isinstance(name, str) and isinstance(spec, str):
                    seen.setdefault(name, spec)
    return list(seen.items())


HEAVY_PACKAGES: list[tuple[str, str]] = _CORE_HEAVY_PACKAGES + _plugin_dependencies()

PACKAGE_BLURBS: dict[str, str] = {
    "chainlit":    "chainlit: chat UI framework…",
    "anthropic":   "anthropic: Claude API client…",
    "openai":      "openai: OpenAI API client…",
    "google.genai":"google-genai: Google AI client…",
    "claude_agent_sdk": "claude-agent-sdk: Claude subscription brain…",
    "numpy":       "numpy: numerical arrays…",
    "PIL":         "pillow: image processing…",
    "pypdf":       "pypdf: PDF reading…",
    "scipy":       "scipy: numerical computing — FFT, stats, linear algebra…",
    "pandas":      "pandas: data tables and time series…",
    "pyarrow":     "pyarrow: Parquet and Arrow columnar data…",
    "h5py":        "h5py: HDF5 (.h5) reading and writing…",
    "matplotlib":  "matplotlib: static and animated plots…",
    "plotly":      "plotly: interactive WebGL charts…",
    "chromadb":    "chromadb: vector database for RAG search (biggest download)…",
    "rdflib":      "rdflib: RDF knowledge graph — triples, SPARQL, OWL…",
    "networkx":    "networkx: graph algorithms and layout for flowcharts…",
    "fpdf":        "fpdf2: build PDFs directly — pages, tables, images, vector…",
}

ProgressCb = Callable[[int, int, str, "str | None"], None]

last_failure_detail: str = ""


def _user_site() -> Path:
    prefix = os.environ.get("PIP_PREFIX")
    py_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    if prefix:
        return Path(prefix) / "lib" / py_dir / "site-packages"
    return (
        Path.home()
        / "Library" / "Application Support" / "Voitta Compute"
        / "userbase" / "lib" / py_dir / "site-packages"
    )


def _user_data_root() -> Path:
    # _user_site() = <user_data>/userbase/lib/pythonX.Y/site-packages
    # four parents up = <user_data>
    return _user_site().parent.parent.parent.parent


def _state_path() -> Path:
    return _user_data_root() / "install_state.json"


def _deploy_stamp_path() -> Path:
    return _user_data_root() / ".deployed_version"


def current_app_version() -> str:
    # Preferred: version stamped into the package at build time by build_app.sh.
    try:
        from voitta_compute import __version__ as _v
        if isinstance(_v, str) and _v and _v != "unknown":
            return _v
    except Exception:
        pass
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version("voitta-compute")
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        pp = parent / "pyproject.toml"
        if not pp.is_file():
            continue
        try:
            import tomllib
            with open(pp, "rb") as f:
                data = tomllib.load(f)
            v = data.get("project", {}).get("version") or data.get("tool", {}).get("briefcase", {}).get("version")
            if isinstance(v, str) and v:
                return v
        except Exception:
            pass
        break
    return "unknown"


def ensure_fresh_deploy(log) -> None:
    """On a version bump, wipe userbase/ + rag/ so install + RAG rerun cleanly."""
    user_root = _user_data_root()
    stamp = _deploy_stamp_path()
    current = current_app_version()
    stored = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else None
    if stored == current:
        return
    log.info("deploy: stamp=%r current=%r — wiping userbase/", stored, current)
    p = user_root / "userbase"
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    certs_dir = user_root / "backend" / "certs"
    if certs_dir.is_dir():
        shutil.rmtree(certs_dir, ignore_errors=True)
    _state_path().unlink(missing_ok=True)
    _user_site().mkdir(parents=True, exist_ok=True)


def mark_deploy_complete() -> None:
    stamp = _deploy_stamp_path()
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(current_app_version(), encoding="utf-8")


def is_complete() -> bool:
    """All heavy packages importable in the current sys.path?"""
    for import_name, _ in HEAVY_PACKAGES:
        try:
            importlib.import_module(import_name)
        except ImportError:
            return False
    return True


def _lib_sources_dest() -> Path:
    return _user_data_root() / "lib-sources"


def _lib_sources_stamp_path() -> Path:
    return _user_data_root() / ".lib_sources_sha"


def lib_sources_need_update() -> bool:
    """True if lib-sources are absent or at a different SHA than the bundle stamp."""
    dest = _lib_sources_dest()
    stamp = _lib_sources_stamp_path()
    if not dest.is_dir() or not stamp.is_file():
        return True
    try:
        import voitta_compute
        bundled_stamp = (
            Path(voitta_compute.__file__).resolve().parent
            / "resources" / "code_sources_version.txt"
        )
        if not bundled_stamp.is_file():
            return False  # no stamp bundled — skip
        return stamp.read_text().strip() != bundled_stamp.read_text().strip()
    except Exception:
        return False


def clone_lib_sources(progress_cb: "Callable[[str], None]") -> bool:
    """Clone/update source submodules into the user data dir.

    Reads repo URLs from the bundled .gitmodules and SHAs from
    code_sources_version.txt, then does a shallow clone (or fetch+checkout)
    of each submodule at the pinned SHA.
    Returns True on success, False on any failure.
    """
    global last_failure_detail
    import configparser
    import re
    import subprocess as _sp

    try:
        import voitta_compute
        res = Path(voitta_compute.__file__).resolve().parent / "resources"
    except Exception as exc:
        last_failure_detail = f"Cannot locate bundle resources: {exc}"
        return False

    gitmodules = res / "gitmodules"
    version_txt = res / "code_sources_version.txt"

    if not gitmodules.is_file():
        progress_cb("lib-sources: no .gitmodules in bundle — skipping")
        return True

    # Parse submodule URLs from bundled gitmodules.
    cfg = configparser.RawConfigParser()
    cfg.read_string(gitmodules.read_text())
    submodules: list[tuple[str, str]] = []  # (name, url)
    for section in cfg.sections():
        m = re.match(r'submodule "(.+)"', section)
        if m:
            url = cfg.get(section, "url", fallback=None)
            if url:
                submodules.append((m.group(1), url))

    if not submodules:
        progress_cb("lib-sources: no submodules found — skipping")
        return True

    # Parse pinned SHAs from bundled code_sources_version.txt.
    # Format: " SHA path (describe)" — same as git submodule status output.
    pinned: dict[str, str] = {}
    if version_txt.is_file():
        for line in version_txt.read_text().splitlines():
            line = line.strip().lstrip("+-U")
            parts = line.split()
            if len(parts) >= 2:
                pinned[parts[1]] = parts[0]  # path → sha

    dest_root = _lib_sources_dest()
    dest_root.mkdir(parents=True, exist_ok=True)

    if not _can_reach_pypi():  # reuse network check (hits github.com too)
        last_failure_detail = "No network — cannot clone lib-sources."
        progress_cb("lib-sources: offline — skipping (RAG code corpus unavailable)")
        return True  # non-fatal: RAG will skip code corpus

    for name, url in submodules:
        dest = dest_root / name.split("/")[-1]  # e.g. lib-sources/three.js → three.js
        sha = pinned.get(name, "")
        if dest.is_dir():
            progress_cb(f"lib-sources: updating {name}…")
            try:
                _sp.run(["git", "fetch", "--depth=1", "origin", sha or "HEAD"],
                        cwd=dest, check=True, capture_output=True)
                _sp.run(["git", "checkout", sha or "FETCH_HEAD"],
                        cwd=dest, check=True, capture_output=True)
            except _sp.CalledProcessError as exc:
                progress_cb(f"lib-sources: {name} fetch failed: {exc.stderr.decode()[:200]}")
        else:
            progress_cb(f"lib-sources: cloning {name}…")
            cmd = ["git", "clone", "--depth=1", "--no-tags"]
            if sha:
                cmd += ["--no-checkout"]
            cmd += [url, str(dest)]
            try:
                _sp.run(cmd, check=True, capture_output=True)
                if sha:
                    _sp.run(["git", "fetch", "--depth=1", "origin", sha],
                            cwd=dest, check=True, capture_output=True)
                    _sp.run(["git", "checkout", sha],
                            cwd=dest, check=True, capture_output=True)
            except _sp.CalledProcessError as exc:
                err = exc.stderr.decode()[:300] if exc.stderr else str(exc)
                progress_cb(f"lib-sources: {name} clone failed: {err}")
                last_failure_detail = err
                return False

    # Write stamp so we don't re-clone on next launch.
    if version_txt.is_file():
        _lib_sources_stamp_path().write_text(version_txt.read_text())

    progress_cb("lib-sources: done")
    return True


def _load_plugin_manifests() -> "list[tuple[str, Path, dict]]":
    """Return (plugin_name, plugin_dir, manifest) for every deployed plugin."""
    import json as _json

    from app.config import PLUGINS_DIR

    out: list[tuple[str, Path, dict]] = []
    if not PLUGINS_DIR.is_dir():
        return out
    for mf in sorted(PLUGINS_DIR.glob("**/manifest.json")):
        try:
            manifest = _json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = manifest.get("name") or mf.parent.name
        out.append((str(name), mf.parent, manifest))
    return out


def _run_docs_transform(
    plugin_dir: Path, spec: str, src: Path, dst: Path
) -> int:
    """Import ``spec`` ("module.py:function") from the plugin and run it.

    The callable takes (src_dir, dst_dir) and returns the number of files
    written. Transforms live with the plugin, so a plugin that ships docs in
    some other source format brings its own converter rather than teaching
    the installer about it.
    """
    import importlib.util

    mod_name, _, fn_name = spec.partition(":")
    fn_name = fn_name or "convert_tree"
    mod_path = plugin_dir / mod_name
    if not mod_path.is_file():
        raise FileNotFoundError(f"transform module not found: {mod_path}")

    ispec = importlib.util.spec_from_file_location(
        f"_plugin_docs_transform_{plugin_dir.name}", mod_path
    )
    if ispec is None or ispec.loader is None:
        raise ImportError(f"cannot load transform: {mod_path}")
    module = importlib.util.module_from_spec(ispec)
    ispec.loader.exec_module(module)
    fn = getattr(module, fn_name, None)
    if not callable(fn):
        raise AttributeError(f"{mod_path}: no callable {fn_name!r}")
    return int(fn(src, dst) or 0)


def sync_plugin_docs(progress_cb: "Callable[[str], None]") -> bool:
    """Fetch third-party docs declared by plugin manifests, at install time.

    A plugin opts in with::

        "docs_repo": {
          "url": "https://github.com/runpod/docs.git",
          "ref": "main",
          "transform": "convert.py:convert_tree"
        }

    The checkout lands in ``plugin-docs-src/<plugin>/`` and the indexable
    markdown in ``plugin-docs/<plugin>/`` — both siblings of plugins/, which
    the launcher re-seeds on every start. ``transform`` is optional; without
    it the repo's own .md files are copied across as-is.

    Non-fatal by design: docs are an enhancement, so a failure here logs and
    returns True rather than blocking the install. Returns False only if a
    transform corrupts state badly enough that indexing should be skipped.
    """
    import shutil as _shutil
    import subprocess as _sp
    import traceback

    from app.config import PLUGIN_DOCS_DIR, PLUGIN_DOCS_SRC_DIR

    wanted = [
        (name, pdir, m["docs_repo"])
        for name, pdir, m in _load_plugin_manifests()
        if isinstance(m.get("docs_repo"), dict) and m["docs_repo"].get("url")
    ]
    if not wanted:
        return True

    if not _can_reach_pypi():
        progress_cb("plugin-docs: offline — skipping (docs will index next launch)")
        return True

    for name, plugin_dir, spec in wanted:
        url = str(spec["url"])
        ref = str(spec.get("ref") or "HEAD")
        src = PLUGIN_DOCS_SRC_DIR / name
        dst = PLUGIN_DOCS_DIR / name
        head_file = src / ".git" / "HEAD"

        try:
            if head_file.exists():
                progress_cb(f"plugin-docs: updating {name}…")
                _sp.run(["git", "fetch", "--depth=1", "origin", ref],
                        cwd=src, check=True, capture_output=True)
                _sp.run(["git", "checkout", "--force", "FETCH_HEAD"],
                        cwd=src, check=True, capture_output=True)
            else:
                progress_cb(f"plugin-docs: cloning {name} from {url}…")
                if src.exists():
                    _shutil.rmtree(src, ignore_errors=True)
                src.parent.mkdir(parents=True, exist_ok=True)
                _sp.run(
                    ["git", "clone", "--depth=1", "--no-tags",
                     "--branch", ref, url, str(src)],
                    check=True, capture_output=True,
                )
        except _sp.CalledProcessError as exc:
            err = (exc.stderr.decode()[:300] if exc.stderr else str(exc)).strip()
            progress_cb(f"plugin-docs: {name} fetch failed — {err}")
            continue
        except Exception as exc:  # noqa: BLE001
            progress_cb(f"plugin-docs: {name} fetch failed — {exc}")
            continue

        # Rebuild the converted tree from scratch so pages deleted upstream
        # don't linger and keep getting indexed.
        tmp = dst.with_name(dst.name + ".new")
        _shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            transform = spec.get("transform")
            if transform:
                n = _run_docs_transform(plugin_dir, str(transform), src, tmp)
            else:
                n = 0
                for md in sorted(src.rglob("*.md")):
                    rel = md.relative_to(src)
                    out = tmp / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    _shutil.copy2(md, out)
                    n += 1
        except Exception as exc:  # noqa: BLE001
            _shutil.rmtree(tmp, ignore_errors=True)
            progress_cb(f"plugin-docs: {name} transform failed — {exc}")
            for ln in traceback.format_exc().splitlines()[-4:]:
                if ln.strip():
                    progress_cb(f"plugin-docs:   {ln.strip()}")
            continue

        if not n:
            _shutil.rmtree(tmp, ignore_errors=True)
            progress_cb(f"plugin-docs: {name} produced no markdown — skipping")
            continue

        # Swap in only after a successful build, so a mid-transform crash
        # leaves the previous good tree in place.
        _shutil.rmtree(dst, ignore_errors=True)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp.replace(dst)
        progress_cb(f"plugin-docs: {name} ready ({n} markdown file(s))")

    return True


def force_rebuild_stamps() -> None:
    """Wipe install + RAG stamps so the next setup run does everything fresh.

    Does NOT delete userbase/ or rag/ — the installer and RAG builder will
    overwrite them in-place. This means pip re-runs and RAG re-indexes without
    throwing away the existing wheels cache.
    """
    import shutil as _shutil
    from app.config import RAG_DIR

    from app.config import USER_CONFIG_DIR
    _state_path().unlink(missing_ok=True)
    _deploy_stamp_path().unlink(missing_ok=True)
    _lib_sources_stamp_path().unlink(missing_ok=True)
    (USER_CONFIG_DIR / ".code_source_hash").unlink(missing_ok=True)
    (USER_CONFIG_DIR / ".docs_content_hash").unlink(missing_ok=True)
    # Remove chroma stores so RAG builder recreates them cleanly.
    for sub in ("chroma_docs", "chroma_code", ".chroma", ".bm25", ".chroma_code"):
        p = RAG_DIR / sub
        if p.is_dir():
            _shutil.rmtree(p, ignore_errors=True)


def installed_set() -> set[str]:
    p = _state_path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text())
        inst = data.get("installed") if isinstance(data, dict) else None
        return set(inst) if isinstance(inst, list) else set()
    except Exception:
        return set()


def _save_state(installed: set[str]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"installed": sorted(installed), "ts": time.time()}, indent=2))


def _can_reach_pypi(timeout_s: float = 3.0) -> bool:
    import socket
    try:
        with socket.create_connection(("pypi.org", 443), timeout=timeout_s):
            return True
    except (OSError, socket.error):
        return False


def _tail_lines(text: str, n: int) -> str:
    if not text:
        return "(no output)"
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def _is_phantom(mod) -> bool:
    """True if ``mod`` is a ``sys.modules`` leftover whose files are deleted.

    ``ensure_fresh_deploy`` wipes ``userbase/`` on a version bump, but anything
    imported earlier in this same process survives in ``sys.modules``. A plain
    ``importlib.import_module`` probe then returns that phantom and the package
    is skipped as "already installed" — it never gets reinstalled, and the
    first import of a *submodule* (which was never cached) fails at runtime.
    That is a real incident: an early import of ``agent_sdk.config`` pulled in
    chainlit, the wipe deleted it, the installer skipped it, and uvicorn died
    on ``chainlit.server``.

    Only a module with a ``__file__`` that no longer exists counts. Namespace
    packages legitimately have ``__file__ is None``, so those are checked
    through ``__path__`` instead and otherwise trusted.
    """
    f = getattr(mod, "__file__", None)
    if f:
        return not Path(f).exists()
    paths = list(getattr(mod, "__path__", None) or [])
    return bool(paths) and not any(Path(p).exists() for p in paths)


def _resolvable(import_name: str) -> bool:
    """Cheap check that ``import_name`` still exists, without executing it.

    Used to audit the state file's claims. ``find_spec`` consults the loaded
    module when there is one, so a phantom is screened out the same way as in
    the probe below.
    """
    try:
        mod = sys.modules.get(import_name)
        if mod is not None:
            return not _is_phantom(mod)
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def install_all(progress_cb: ProgressCb) -> bool:
    """Install every package whose import probe fails and isn't in the state file.

    ``progress_cb(current, total, label, log_line)`` — called on the worker thread.
    Returns True on full success, False on first pip failure.
    """
    global last_failure_detail
    last_failure_detail = ""

    state = installed_set()
    todo: list[tuple[str, str]] = []
    for import_name, spec in HEAVY_PACKAGES:
        # The state file is a fast path, not the truth: a run that recorded a
        # package as installed and then lost it (an interrupted wipe, a
        # phantom slipping through) would otherwise skip it on every launch
        # forever, with no way back short of a version bump.
        if import_name in state and _resolvable(import_name):
            continue
        state.discard(import_name)
        try:
            mod = importlib.import_module(import_name)
            if _is_phantom(mod):
                raise ImportError(f"{import_name} cached but files are gone")
            state.add(import_name)
            continue
        except ImportError:
            pass
        todo.append((import_name, spec))

    _save_state(state)
    if not todo:
        return True

    total = len(todo)

    if not _can_reach_pypi():
        last_failure_detail = (
            "Could not reach pypi.org.\n\n"
            "Voitta needs internet access to download required packages on "
            "first launch. Connect and relaunch — the installer resumes from "
            "where it stopped."
        )
        progress_cb(0, total, "Offline — cannot reach pypi.org", "!!! offline")
        return False

    from pip._internal.cli.main import main as pip_main

    # Redirect pip's tempdir (and that of PEP-517 build subprocesses) to a
    # path under USER_DATA_DIR. macOS App Translocation sandbox-namespaces
    # /var/folders/ so parent and child processes see different real paths for
    # the same /var/folders/... string — output.json written by the subprocess
    # is invisible to the parent. ~/Library/Application Support/ is not
    # namespaced, so both sides always resolve it to the same real path.
    import tempfile
    from app.config import USER_DATA_ROOT
    build_tmp = USER_DATA_ROOT / "build-tmp"
    build_tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(build_tmp)
    tempfile.tempdir = str(build_tmp)

    for i, (import_name, spec) in enumerate(todo):
        blurb = PACKAGE_BLURBS.get(import_name, f"Installing {import_name}…")
        progress_cb(i, total, blurb, f">>> pip install {spec}")

        args = ["install", "--no-warn-script-location"]
        try:
            import voitta_compute
            _whl = Path(voitta_compute.__file__).resolve().parent / "resources" / "wheels"
        except Exception:
            _whl = Path(__file__).resolve().parent.parent.parent / "wheels"
        if _whl.is_dir():
            args += ["--find-links", str(_whl)]
        args.append(spec)
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        try:
            with (
                contextlib.redirect_stdout(out_buf),
                contextlib.redirect_stderr(err_buf),
            ):
                rc = pip_main(args)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:  # noqa: BLE001
            print(f"=== pip install {spec} (exception) ===", file=sys.stderr)
            print(out_buf.getvalue(), file=sys.stderr)
            print(err_buf.getvalue(), file=sys.stderr)
            tail = _tail_lines(err_buf.getvalue() or out_buf.getvalue() or str(exc), 12)
            last_failure_detail = (
                f"Failed at: {import_name} ({spec})\n"
                f"Reason: {type(exc).__name__}: {exc}\n\nLast pip output:\n{tail}"
            )
            progress_cb(i, total, f"pip crashed on {import_name}", f"!!! {type(exc).__name__}: {exc}")
            return False

        if rc != 0:
            print(f"=== pip install {spec} (rc={rc}) ===", file=sys.stderr)
            print(out_buf.getvalue(), file=sys.stderr)
            print(err_buf.getvalue(), file=sys.stderr)
            tail = _tail_lines(err_buf.getvalue() or out_buf.getvalue(), 12)
            last_failure_detail = (
                f"Failed at: {import_name} ({spec})\npip exit code: {rc}\n\nLast pip output:\n{tail}"
            )
            progress_cb(i, total, f"Failed: {import_name} (pip exit {rc})", f"!!! rc={rc}\n{tail}")
            return False

        print(f"=== pip install {spec} OK ===", file=sys.stderr)
        state.add(import_name)
        _save_state(state)
        importlib.invalidate_caches()
        progress_cb(i + 1, total, f"Installed {import_name}", None)

    return True


def pip_install_runtime(specs: list[str]) -> dict[str, Any]:
    """Install pip specs into the live runtime, ad-hoc, mid-session.

    Reuses install_all's in-process pip + the same writable user-site (the
    default install scheme this process is configured with) and the same
    App-Translocation-safe TMPDIR + bundled-wheel find-links. The package is
    importable in-process immediately — ``importlib.invalidate_caches()`` is
    called, and the user-site is already on ``sys.path``, so no restart.

    Blocking (pip is sync) — call from a worker thread, not the event loop.
    Returns a structured envelope; never raises. Backs the ``pip_install``
    tool. NOTE: only packages with compatible prebuilt wheels (macOS arm64 /
    CPython 3.12) install reliably — the bundle has no compiler for source
    builds. Packages land in the userbase, which a version-bump deploy wipes;
    for permanence add to ``_CORE_HEAVY_PACKAGES`` instead.
    """
    specs = [s.strip() for s in specs if isinstance(s, str) and s.strip()]
    if not specs:
        return {"ok": False, "error": "no_packages", "message": "no packages given"}

    if not _can_reach_pypi():
        return {
            "ok": False, "error": "offline", "specs": specs,
            "message": "Could not reach pypi.org — pip install needs internet.",
        }

    from pip._internal.cli.main import main as pip_main

    # Translocation-safe tempdir (see install_all for the why).
    import tempfile
    from app.config import USER_DATA_ROOT
    build_tmp = USER_DATA_ROOT / "build-tmp"
    build_tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(build_tmp)
    tempfile.tempdir = str(build_tmp)

    args = ["install", "--no-warn-script-location"]
    try:
        import voitta_compute
        _whl = Path(voitta_compute.__file__).resolve().parent / "resources" / "wheels"
    except Exception:  # noqa: BLE001
        _whl = Path(__file__).resolve().parent.parent.parent / "wheels"
    if _whl.is_dir():
        args += ["--find-links", str(_whl)]
    args += specs

    out_buf = io.StringIO()
    err_buf = io.StringIO()
    try:
        with (
            contextlib.redirect_stdout(out_buf),
            contextlib.redirect_stderr(err_buf),
        ):
            rc = pip_main(args)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # noqa: BLE001
        tail = _tail_lines(err_buf.getvalue() or out_buf.getvalue() or str(exc), 20)
        return {
            "ok": False, "error": "pip_crashed", "specs": specs,
            "message": f"{type(exc).__name__}: {exc}", "output": tail,
        }

    importlib.invalidate_caches()
    output = _tail_lines(out_buf.getvalue() or err_buf.getvalue(), 20)
    if rc != 0:
        return {"ok": False, "error": "pip_failed", "specs": specs,
                "returncode": rc, "output": output}
    return {"ok": True, "specs": specs, "returncode": rc, "output": output}
