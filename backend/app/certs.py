"""TLS cert provisioning via mkcert.

Called from the first-launch installer (phase 0) and from the
"(Re)create TLS certificates…" tray menu item.

mkcert resolution order:
  1. Binary bundled in the .app (voitta_bookmarklet/resources/bin/mkcert)
  2. System PATH (``brew install mkcert`` in dev)
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from app.config import PROJECT_ROOT, TLS_CERT_PATH, TLS_KEY_PATH

log = logging.getLogger(__name__)

CERTS_DIR = PROJECT_ROOT / "certs"

ProgressCb = Callable[[str], None]


class CertError(RuntimeError):
    """Provisioning failed in a way the caller should surface."""


def is_present() -> bool:
    # Trust-aware: a cert that exists but no longer verifies (CA removed from
    # the keychain, expired, etc.) must trigger re-provisioning, not be skipped.
    return TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists() and _ca_trusted()


def _mkcert_path() -> str | None:
    """Locate mkcert. Prefer the binary bundled inside the .app."""
    try:
        import voitta_bookmarklet
        bundled = Path(voitta_bookmarklet.__file__).resolve().parent / "resources" / "bin" / "mkcert"
        if bundled.is_file():
            import os, stat
            mode = bundled.stat().st_mode
            if not (mode & stat.S_IXUSR):
                bundled.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            return str(bundled)
    except Exception:  # noqa: BLE001
        pass
    return shutil.which("mkcert")


def _caroot(mkcert: str) -> str:
    """Pin CAROOT so the user-run (creates the CA) and admin-run (trusts it)
    agree on one location — else root defaults to /var/root and mints a 2nd CA."""
    try:
        proc = subprocess.run([mkcert, "-CAROOT"], capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except OSError:
        pass
    return str(Path.home() / "Library" / "Application Support" / "mkcert")


def _login_keychain() -> str:
    """Path to the user's login keychain (user-owned → writable without root)."""
    base = Path.home() / "Library" / "Keychains"
    db = base / "login.keychain-db"
    return str(db if db.exists() else base / "login.keychain")


def _trust_ca_user(caroot: str) -> tuple[bool, str]:
    """Trust the mkcert rootCA in the user's LOGIN keychain (user domain).

    NOT the System keychain: that needs root to write, and a detached root
    process can't show the trust dialog (``SecTrustSettings: no user
    interaction was possible``). Browsers and ``security verify-cert`` honor
    user-domain roots. ``-r trustRoot`` marks it a root; omitting ``-d`` keeps
    it in the user domain so no admin auth is required.
    """
    root = Path(caroot) / "rootCA.pem"
    if not root.is_file():
        return False, f"rootCA.pem not found in {caroot}"
    try:
        proc = subprocess.run(
            ["security", "add-trusted-cert", "-r", "trustRoot",
             "-k", _login_keychain(), str(root)],
            capture_output=True, text=True,
        )
    except OSError as exc:
        return False, f"security add-trusted-cert could not run: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr.strip() or "add-trusted-cert failed")[:300]
    return True, proc.stdout.strip()


def _verify_cert(cert: Path) -> bool:
    """Does ``cert`` chain to a trusted root? Same evaluation the browser does."""
    if not cert.exists():
        return False
    try:
        p = subprocess.run(
            ["security", "verify-cert", "-c", str(cert)],
            capture_output=True, text=True,
        )
        if p.returncode != 0:
            log.info("certs: verify-cert %s -> %s", cert.name,
                     (p.stdout or p.stderr).strip()[:200])
        return p.returncode == 0
    except OSError as exc:
        log.error("certs: verify-cert could not run: %s", exc)
        return False


def _ca_trusted() -> bool:
    """True if the generated leaf cert verifies as trusted."""
    return _verify_cert(TLS_CERT_PATH)


def _root_trusted(caroot: str) -> bool:
    """True if the mkcert rootCA verifies as trusted."""
    return _verify_cert(Path(caroot) / "rootCA.pem")


def provision(force: bool = False) -> Path:
    """Generate a 127.0.0.1+localhost cert pair and trust the CA.

    Returns the cert path on success. Raises ``CertError`` on any failure —
    there is no insecure fallback.
    """
    mkcert = _mkcert_path()
    if mkcert is None:
        raise CertError(
            "mkcert not found. Install with `brew install mkcert` or ensure "
            "the .app bundle includes resources/bin/mkcert."
        )

    CERTS_DIR.mkdir(parents=True, exist_ok=True)

    if force:
        for pem in CERTS_DIR.glob("*.pem"):
            pem.unlink(missing_ok=True)

    caroot = _caroot(mkcert)
    env = {**os.environ, "CAROOT": caroot}

    # Create the CA as the user. mkcert also tries to trust it via sudo, which
    # has no TTY in a .app and fails — that's expected; ignore the return code.
    subprocess.run([mkcert, "-install"], capture_output=True, text=True, env=env)

    # Trust the CA via the user's login keychain (no root, dialog shows
    # in-session). Skip if it already verifies.
    if not _root_trusted(caroot):
        ok, msg = _trust_ca_user(caroot)
        if not ok:
            raise CertError(f"Could not trust local CA: {msg}")
        if not _root_trusted(caroot):
            raise CertError("CA still not trusted after add-trusted-cert.")

    gen_proc = subprocess.run(
        [mkcert, "127.0.0.1", "localhost"],
        cwd=CERTS_DIR,
        capture_output=True, text=True, env=env,
    )
    if gen_proc.returncode != 0:
        raise CertError(
            f"mkcert generation failed.\nstderr: {gen_proc.stderr.strip()}"
        )

    cert = CERTS_DIR / "127.0.0.1+1.pem"
    if not cert.exists():
        raise CertError(
            f"mkcert reported success but {cert.name} is missing in {CERTS_DIR}."
        )
    if not _ca_trusted():
        raise CertError("Generated certificate does not verify as trusted.")
    return cert


def provision_with_progress(progress_cb: ProgressCb) -> bool:
    """Provision + trust the local TLS CA, reporting to ``progress_cb``.

    Returns True ONLY if the leaf cert exists AND verifies as trusted. There
    is no insecure HTTP fallback — every failure path returns False so the
    caller can abort startup. Called from the phase-0 installer worker thread.
    """
    if is_present():
        progress_cb("TLS certificates already present and trusted — skipping.")
        return True

    mkcert = _mkcert_path()
    if mkcert is None:
        progress_cb("mkcert not found — cannot set up local HTTPS CA.")
        log.error("certs: mkcert binary not found")
        return False

    caroot = _caroot(mkcert)
    env = {**os.environ, "CAROOT": caroot}
    CERTS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Create the CA as the user. mkcert's own sudo trust step has no TTY in
    #    a .app and fails — that's expected; ignore the return code.
    progress_cb("Running mkcert -install (creates local CA)…")
    try:
        proc = subprocess.run(
            [mkcert, "-install"],
            capture_output=True, text=True, env=env,
        )
        log.info("certs: mkcert -install rc=%s %s", proc.returncode,
                 (proc.stderr or proc.stdout).strip()[:200])
    except OSError as exc:
        progress_cb(f"mkcert -install could not run: {exc}")
        log.error("certs: mkcert -install could not run: %s", exc)
        return False

    # 2. Trust the CA in the user's login keychain (no root; dialog in-session).
    if not _root_trusted(caroot):
        progress_cb("Trusting local CA in your login keychain…")
        ok, msg = _trust_ca_user(caroot)
        if not ok:
            progress_cb(f"Could not trust local CA: {msg}")
            log.error("certs: trust failed: %s", msg)
            return False
        if not _root_trusted(caroot):
            progress_cb("CA still not trusted after add-trusted-cert.")
            log.error("certs: CA not trusted after add-trusted-cert")
            return False

    # 3. Generate the 127.0.0.1+localhost leaf, signed by the trusted CA.
    progress_cb("Generating TLS certificate pair for 127.0.0.1 + localhost…")
    try:
        proc = subprocess.run(
            [mkcert, "127.0.0.1", "localhost"],
            cwd=CERTS_DIR,
            capture_output=True, text=True, env=env,
        )
        if proc.returncode != 0:
            progress_cb(f"mkcert generation failed: {proc.stderr.strip()[:200]}")
            log.error("certs: leaf generation failed rc=%s: %s",
                      proc.returncode, proc.stderr.strip()[:300])
            return False
    except OSError as exc:
        progress_cb(f"mkcert generation could not run: {exc}")
        log.error("certs: leaf generation could not run: %s", exc)
        return False

    # 4. Hard gate: leaf files must exist AND the cert must verify as trusted.
    if not (TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists()):
        progress_cb("Leaf certificate missing after mkcert run.")
        log.error("certs: leaf files missing after generation")
        return False
    if not _ca_trusted():
        progress_cb("Generated certificate does not verify as trusted.")
        log.error("certs: leaf does not verify as trusted")
        return False

    progress_cb("TLS certificates ready and trusted.")
    return True
