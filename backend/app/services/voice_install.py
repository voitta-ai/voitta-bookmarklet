"""Lazy installer for the voice assistant's packages and models.

Voice is off by default, so none of this ships in the bundle or the
first-run install. The first time the user toggles Voice on, the tray
shows a 3-phase InstallWindow driven by these functions:

  1. pip packages   — into the same userbase the main installer uses
                      (wiped on version bump; cheap to reinstall)
  2. wake-word model — sherpa-onnx KWS zipformer (~15 MB GitHub tarball)
  3. speech model    — mlx-community/whisper-base-mlx (~150 MB HF snapshot)

Models live under ``USER_DATA_ROOT/voice`` which SURVIVES version-bump
userbase wipes — after an app update only the package phase re-runs.
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import ssl
import tarfile
import urllib.request
from pathlib import Path
from typing import Callable

from app.config import USER_DATA_ROOT

_log = logging.getLogger("voitta.voice_install")

# (import_name, pip_spec) — same shape as installer.HEAVY_PACKAGES.
# NOTE: numpy is intentionally absent here — it's pinned in the MAIN installer
# to ``numpy>=1.26,<2.5`` (numba's ceiling). mlx_whisper pulls numba, which
# requires numpy<2.5; if the main install ever resolved numpy 2.5, installing
# voice would downgrade numpy on disk while the live process kept 2.5 in
# memory, and numba's import-time check would crash the voice thread. The main
# cap prevents that — do not add an unpinned/newer numpy here.
VOICE_PACKAGES: list[tuple[str, str]] = [
    ("sounddevice", "sounddevice>=0.4.6"),
    ("onnxruntime", "onnxruntime>=1.17"),      # usually present (chromadb)
    ("openwakeword", "openwakeword>=0.6.0"),   # only for its bundled Silero VAD
    # Upper bound is load-bearing: sherpa-onnx 1.13.4 silently broke keyword
    # spotting with the gigaspeech-3.3M KWS model — it spots NOTHING, not even
    # the model's own shipped demo wavs (verified 2026-07). 1.13.0–1.13.3 work.
    # Without the cap, a userbase rebuild pulls the latest wheel and voice goes
    # dead with no error. Re-test the demo wavs before raising this ceiling.
    ("sherpa_onnx", "sherpa-onnx>=1.13,<1.13.4"),
    ("sentencepiece", "sentencepiece>=0.1.99"),
    ("mlx_whisper", "mlx-whisper>=0.4"),       # pulls mlx + torch — the big one
]

VOICE_DIR = USER_DATA_ROOT / "voice"
MODELS_DIR = VOICE_DIR / "models"

KWS_MODEL_NAME = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
KWS_MODEL_DIR = MODELS_DIR / KWS_MODEL_NAME
KWS_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
    f"{KWS_MODEL_NAME}.tar.bz2"
)

WHISPER_REPO = "mlx-community/whisper-large-v3-turbo"
WHISPER_DIR = VOICE_DIR / "whisper-large-v3-turbo"

# Silero VAD (endpointing). The openwakeword wheel ships NO model files —
# its VAD class expects silero_vad.onnx to have been downloaded separately,
# so we fetch it ourselves and pass the path explicitly.
SILERO_VAD_PATH = MODELS_DIR / "silero_vad.onnx"
SILERO_VAD_URL = (
    "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/silero_vad.onnx"
)

# Mirrors installer.last_failure_detail — surfaced in the failure alert.
last_failure_detail: str | None = None

# progress_cb(current, total, label) — current/total in phase-specific
# units (packages: count; downloads: bytes).
ProgressCb = Callable[[int, int, str], None]


def packages_missing() -> list[tuple[str, str]]:
    """Which voice packages aren't installed yet.

    Presence is checked with ``find_spec`` — which *locates* the module
    without executing it — NOT ``import_module``. These packages wrap native
    extensions (PortAudio via sounddevice, Metal/torch via mlx_whisper,
    onnxruntime, sherpa-onnx); importing one mid-install — e.g. while a
    version-bump is repopulating ``userbase/`` and the ``.so`` is half-written
    or missing a dependent dylib — can **hard-abort the process** (SIGABRT),
    which a ``try/except ImportError`` cannot catch. This probe runs on every
    boot where Voice was enabled (the autostart gate), so it must never load a
    native lib just to answer "is it there?". ``find_spec`` only reads importer
    metadata, so it's crash-safe."""
    missing = []
    for import_name, spec in VOICE_PACKAGES:
        try:
            found = importlib.util.find_spec(import_name) is not None
        except Exception:
            # A half-present parent package can make find_spec raise — treat
            # that as "not installed" rather than letting it propagate.
            found = False
        if not found:
            missing.append((import_name, spec))
    return missing


def kws_model_missing() -> bool:
    if not KWS_MODEL_DIR.is_dir():
        return True
    needed = ["tokens.txt", "bpe.model"]
    present = {p.name for p in KWS_MODEL_DIR.iterdir()}
    if any(n not in present for n in needed):
        return True
    for part in ("encoder", "decoder", "joiner"):
        if not any(name.startswith(part) and name.endswith(".onnx") for name in present):
            return True
    return False


def vad_model_missing() -> bool:
    return not SILERO_VAD_PATH.is_file()


def whisper_model_missing() -> bool:
    if not (WHISPER_DIR / "config.json").is_file():
        return True
    return not any(WHISPER_DIR.glob("*.safetensors")) and not any(WHISPER_DIR.glob("weights*"))


def models_missing() -> bool:
    return kws_model_missing() or vad_model_missing() or whisper_model_missing()


def is_ready() -> bool:
    return not packages_missing() and not models_missing()


def install_packages(progress_cb: ProgressCb) -> bool:
    """pip-install the missing voice packages one at a time, with progress.
    Blocking — call from a worker thread."""
    global last_failure_detail
    from app.installer import pip_install_runtime

    missing = packages_missing()
    total = len(missing)
    for i, (import_name, spec) in enumerate(missing):
        progress_cb(i, total, f"Installing {spec} …")
        res = pip_install_runtime([spec])
        if not res.get("ok"):
            last_failure_detail = (
                f"pip install {spec} failed: {res.get('message') or res.get('error')}\n"
                f"{res.get('output') or ''}"
            )
            _log.error("voice package install failed: %s", last_failure_detail)
            return False
        progress_cb(i + 1, total, f"Installed {import_name}")
    return True


def _ssl_context() -> ssl.SSLContext:
    """TLS context that works inside the .app bundle.

    The bundled Python has no system CA store (urllib's default OpenSSL
    paths don't exist there), so verification fails on every HTTPS URL.
    certifi ships with the bundle (httpx depends on it) — use its CA
    bundle when available, else fall back to the platform default.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def _download_with_progress(url: str, dest: Path, progress_cb: ProgressCb, label: str) -> None:
    """Stream ``url`` to ``dest`` reporting (bytes_done, bytes_total)."""
    req = urllib.request.Request(url, headers={"User-Agent": "voitta-bookmarklet"})
    with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                progress_cb(done, max(total, done), f"{label} — {done // (1024*1024)} MB")


def download_kws_model(progress_cb: ProgressCb) -> bool:
    """Fetch + extract the sherpa-onnx KWS model tarball, plus the
    Silero VAD model (small, same phase)."""
    global last_failure_detail
    if vad_model_missing():
        try:
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            tmp = SILERO_VAD_PATH.with_suffix(".onnx.part")
            _download_with_progress(SILERO_VAD_URL, tmp, progress_cb, "VAD model")
            tmp.replace(SILERO_VAD_PATH)
        except Exception as exc:  # noqa: BLE001
            last_failure_detail = f"VAD model download failed: {exc}"
            _log.exception("silero VAD download failed")
            return False
    if not kws_model_missing():
        return True
    build_tmp = USER_DATA_ROOT / "build-tmp"
    build_tmp.mkdir(parents=True, exist_ok=True)
    archive = build_tmp / f"{KWS_MODEL_NAME}.tar.bz2"
    try:
        _download_with_progress(KWS_URL, archive, progress_cb, "Wake-word model")
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        progress_cb(0, 1, "Extracting …")
        # Extract to a staging dir then move, so a partial extract never
        # passes the kws_model_missing() probe.
        staging = build_tmp / "kws-extract"
        shutil.rmtree(staging, ignore_errors=True)
        with tarfile.open(archive, "r:bz2") as tf:
            tf.extractall(staging)
        shutil.rmtree(KWS_MODEL_DIR, ignore_errors=True)
        shutil.move(str(staging / KWS_MODEL_NAME), str(KWS_MODEL_DIR))
        shutil.rmtree(staging, ignore_errors=True)
        progress_cb(1, 1, "Done")
        return True
    except Exception as exc:  # noqa: BLE001
        last_failure_detail = f"wake-word model download failed: {exc}"
        _log.exception("KWS model download failed")
        return False
    finally:
        archive.unlink(missing_ok=True)


def download_whisper_model(progress_cb: ProgressCb) -> bool:
    """Download the whisper model from Hugging Face with real byte progress.

    Not ``snapshot_download`` — its ``tqdm_class`` hook counts *files*,
    and this repo is essentially one 1.6 GB safetensors file, so the
    progress bar sits at 0/1 for the whole download. Instead we list the
    repo files + sizes and stream each one ourselves, reporting
    cumulative bytes across the whole snapshot.

    ``mlx_whisper.transcribe(path_or_hf_repo=str(WHISPER_DIR))`` then
    loads from disk and never touches the network at runtime.
    """
    global last_failure_detail
    if not whisper_model_missing():
        return True
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(WHISPER_REPO, files_metadata=True)
        files = [
            (s.rfilename, s.size or 0)
            for s in (info.siblings or [])
            if not s.rfilename.startswith(".")
        ]
        if not files:
            raise RuntimeError(f"no files listed for {WHISPER_REPO}")
        total = sum(size for _, size in files)
        total_mb = total // (1024 * 1024)
        done_base = 0

        WHISPER_DIR.mkdir(parents=True, exist_ok=True)
        for rfilename, size in files:
            dest = WHISPER_DIR / rfilename
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_file() and size and dest.stat().st_size == size:
                done_base += size  # resume: already complete
                continue
            url = f"https://huggingface.co/{WHISPER_REPO}/resolve/main/{rfilename}"
            tmp = dest.with_suffix(dest.suffix + ".part")
            base = done_base  # bind per-file offset for the callback

            def _cb(done: int, _t: int, _label: str, base: int = base) -> None:
                progress_cb(
                    base + done, max(total, base + done),
                    f"Speech model — {(base + done) // (1024*1024)} / {total_mb} MB",
                )

            _download_with_progress(url, tmp, _cb, rfilename)
            tmp.replace(dest)
            done_base += size or tmp_size_fallback(dest)
        progress_cb(total, total, "Done")
        return True
    except Exception as exc:  # noqa: BLE001
        last_failure_detail = f"speech model download failed: {exc}"
        _log.exception("whisper model download failed")
        return False


def tmp_size_fallback(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
