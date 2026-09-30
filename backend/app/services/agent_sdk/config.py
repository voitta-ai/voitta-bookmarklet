"""Per-user paths, subprocess env, and engine-availability probe for the
Claude Agent SDK brain.

Two invariants this module enforces, both load-bearing:

* **Stable per-user working directory.** Claude Code keys its session store
  by an encoded form of the cwd (``<CLAUDE_CONFIG_DIR>/projects/<encoded-cwd>``).
  If the cwd drifts between turns, prior sessions disappear from
  ``list_sessions()``. We pin a single deterministic dir per user so history
  is always listable and resumable.

* **No ``ANTHROPIC_API_KEY`` in the subprocess env.** The API key outranks the
  subscription OAuth token in Claude Code's credential precedence — a stray key
  silently bypasses the token the user pasted. :func:`subprocess_env` strips it.

Both ``CLAUDE_CONFIG_DIR`` and the cwd resolve under the current user's data
root (via the ``current_user`` contextvar), so server mode isolates each user's
credentials *and* session store with no extra plumbing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from app.services.current_user import user_data_root

# The selector value that picks this brain. Intercepted in the chat layer
# before the normalised provider factory is ever consulted, so it never has
# to be a member of ``ProviderId``.
BRAIN_PROVIDER = "claude_code"
BRAIN_LABEL = "Claude (subscription)"

# Default model when the user hasn't pinned one for this brain. Sourced from
# the model catalog (bundled snapshot for claude_code — the subscription brain
# has no machine-readable model list; see agent_sdk/models.py) so the default
# lives in one place, not as a scattered literal. Falls back to a concrete id
# if the catalog is somehow unreadable, so behaviour stays predictable.
def _default_model() -> str:
    try:
        from app.services import models_catalog

        return models_catalog.default_model_for(BRAIN_PROVIDER) or "claude-opus-4-8"
    except Exception:
        return "claude-opus-4-8"


DEFAULT_MODEL = _default_model()

# In-process MCP server name; tools surface to the engine as
# ``mcp__<MCP_SERVER_NAME>__<tool>``.
MCP_SERVER_NAME = "voitta"

# ``model`` the engine stamps on assistant messages it fabricates itself
# (never sent to the API) — e.g. "No response requested." or an API-error echo.
SYNTHETIC_MODEL = "<synthetic>"


def _brain_root() -> Path:
    """Per-user root for everything this brain stores on disk."""
    return Path(user_data_root()) / "claude_code"


def config_dir() -> Path:
    """Per-user ``CLAUDE_CONFIG_DIR`` (credentials + session transcripts)."""
    p = _brain_root() / "config"
    p.mkdir(parents=True, exist_ok=True)
    return p


def workspace_dir() -> Path:
    """Stable per-user cwd for the engine subprocess.

    Pinned and deterministic — see the module docstring for why this must not
    drift between turns.
    """
    p = _brain_root() / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


def subprocess_env() -> dict[str, str]:
    """Env for the engine subprocess: inherit the parent, point
    ``CLAUDE_CONFIG_DIR`` at the per-user dir, inject the stored subscription
    token as ``CLAUDE_CODE_OAUTH_TOKEN``, and strip ``ANTHROPIC_API_KEY`` so the
    subscription token wins (the API key would otherwise outrank it)."""
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir())
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    # AskUserQuestion option previews. The TS-only ``toolConfig`` SDK option is
    # just a setter for this env var (verified in the bundled engine binary);
    # the Python SDK lacks the option, so set the var directly. "markdown" —
    # previews render through our own Markdown component, no HTML sanitizer.
    env["CLAUDE_CODE_QUESTION_PREVIEW_FORMAT"] = "markdown"
    # Lazy import avoids a config<->credentials import cycle.
    from app.services.agent_sdk.credentials import load_token

    token = load_token()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    else:
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    return env


@lru_cache(maxsize=1)
def _cli_path_cached() -> str | None:
    """Locate the Claude Code CLI binary once.

    Checks ``$PATH`` and the common user-local install location. Cached because
    it's hit on every availability check and turn; the binary doesn't move
    within a process lifetime.
    """
    found = shutil.which("claude")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "claude"
    if candidate.exists():
        return str(candidate)
    return None


def cli_path() -> str | None:
    return _cli_path_cached()


def _parse_version(text: str | None) -> tuple[int, ...] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(g) for g in m.groups()) if m else None


def _bundled_engine() -> tuple[str, tuple[int, ...]] | None:
    """The engine binary shipped inside the ``claude_agent_sdk`` wheel, with
    its version (read from the package — no subprocess needed)."""
    try:
        import claude_agent_sdk
        from claude_agent_sdk._cli_version import __cli_version__
    except Exception:
        return None
    name = "claude.exe" if os.name == "nt" else "claude"
    path = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    version = _parse_version(__cli_version__)
    if not path.is_file() or version is None:
        return None
    return str(path), version


@lru_cache(maxsize=8)
def _binary_version(real_path: str, _mtime_ns: int) -> tuple[int, ...] | None:
    """``<binary> --version``, cached per (resolved path, mtime) so an in-place
    ``claude update`` — which retargets the symlink — is picked up without a
    restart."""
    try:
        out = subprocess.run(
            [real_path, "--version"], capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return None
    return _parse_version(out.stdout)


def engine_cli_path() -> str | None:
    """The newest Claude Code engine on this machine: the SDK-bundled binary
    or the system ``claude`` install, whichever reports the higher version.

    Left to itself the SDK always runs its bundled binary, which is frozen at
    whatever version the wheel shipped with — pip only refreshes it on a new
    app release. New models require new engines ("Claude Code 2.1.277 does not
    support this model; version 2.1.280 or newer is required"), while a system
    install auto-updates. Preferring the newer of the two keeps newly released
    models usable. Ties go to the bundled binary (the SDK's tested pairing).

    Returns None when neither is found, which leaves the SDK's own discovery
    in charge (and its CLINotFoundError). May spawn ``claude --version`` on
    first use — call it off the event loop.
    """
    bundled = _bundled_engine()
    system = _cli_path_cached()
    system_version = None
    if system:
        try:
            real = os.path.realpath(system)
            system_version = _binary_version(real, os.stat(real).st_mtime_ns)
        except OSError:
            system_version = None
    if bundled and (system_version is None or bundled[1] >= system_version):
        return bundled[0]
    return system


def is_available() -> bool:
    """True if the brain can actually run: the Claude Code engine binary is
    on disk **and** the ``claude_agent_sdk`` Python driver is importable.

    Cheap (path lookup + spec check) so it's safe to call on the settings
    request that decides whether to offer the brain in the selector (Phase 4).
    The module check is uncached because app.installer installs the SDK after
    first boot — availability must flip to True without a restart.
    """
    import importlib.util

    if _cli_path_cached() is None:
        return False
    try:
        return importlib.util.find_spec("claude_agent_sdk") is not None
    except Exception:
        return False
