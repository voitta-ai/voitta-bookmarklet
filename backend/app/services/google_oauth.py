"""Google OAuth 2.0 — confidential web-app flow for Google API access.

Multi-account: the user can register several named Google accounts, each
with its own OAuth client credentials and its own token grant. Lives
entirely on the backend (token store + token refresh); the frontend's
only role is the Settings panel that drives the connect flow per account.

Storage (``settings.googleOAuth`` in the backend-owned settings file —
``~/.config/voitta-bookmarklet/settings.json`` on desktop, per-user file in
server mode)::

    "googleOAuth": {
      "defaultAccount": "acc_1a2b3c4d",
      "accounts": {
        "acc_1a2b3c4d": {
          "label": "Agnitio work",
          "clientId": "...", "clientSecret": "...",
          "createdAt": "...",
          "tokens": {"access_token": ..., "refresh_token": ...,
                      "expires_at": ..., "scope": ...,
                      "account_email": "roman@agnitio.ai", ...}
        },
        ...
      }
    }

Identity model (three layers, each with one job):

  * ``id``    — ``acc_<hex>``, random, immutable. Internal key: dict key,
                token-cache key, HTTP API parameter.
  * ``email`` — set by the OAuth callback from the ID token. THE durable
                identity: script pins, ``drive://...?account=`` refs and
                snapshot origins all store the email. One connected email
                per account entry, enforced at token exchange.
  * ``label`` — freely renameable display / LLM alias. Never stored in
                anything durable.

``resolve_account`` accepts any of the three (selector), so LLM tool
args can say ``account="roman@agnitio.ai"`` or ``account="Agnitio work"``.

Legacy migration: the pre-multi-account flat shape
(``googleOAuth.clientId/clientSecret/tokens``) is migrated in place to a
single account entry on first read, preserving the existing grant.

Scope policy: ``drive.readonly`` + ``spreadsheets`` (cell-level
read/write for the Sheets plugin) plus ``openid email`` so we can show
the connected account's email in the UI without an extra API call.

If an account was connected under an older scope set, its status entry
carries ``needs_reauth: True`` so the UI can prompt a reconnect.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.services import user_settings

logger = logging.getLogger(__name__)


# OAuth endpoints
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# What we ask Google for.
# - drive.readonly:  read-only Drive access — no upload, share, or metadata mutation.
# - spreadsheets:    cell-level read/write for the Sheets plugin.
# - openid email:    decode the returned ID token to surface account email in the UI.
SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

# Scopes (excluding openid/email) that must be present in a stored token
# for all Google plugins to function. Used by per-account needs_reauth.
_REQUIRED_API_SCOPES = frozenset([DRIVE_SCOPE, SHEETS_SCOPE])

# Where Google redirects with the authorization code when the caller
# can't derive a better URI from the live request (desktop default).
# Must exactly match a URI registered for the OAuth client in Google
# Cloud Console.
DEFAULT_REDIRECT_URI = "https://127.0.0.1:12358/api/google/oauth/callback"

# Refresh tokens slightly before expiry so a slow request doesn't
# 401 mid-flight.
REFRESH_GRACE_S = 60.0


class UnknownAccount(ValueError):
    """Raised when an account selector matches no configured account."""


# ---- in-memory CSRF state ------------------------------------------------


@dataclass
class _PendingState:
    """One in-flight OAuth connect. ``state`` (the dict key) is the CSRF
    nonce; the record pins which account entry the callback must write
    to, the exact redirect_uri used on the authorize leg (Google requires
    the token exchange to repeat it), and — in server mode — which user
    started the flow."""

    account_id: str
    redirect_uri: str
    user_email: str | None = None
    created_at: float = field(default_factory=time.time)


# state nonce → bookkeeping. Cleared after callback consumes or after
# 10 min, whichever first.
_pending: dict[str, _PendingState] = {}
_PENDING_TTL_S = 10 * 60

# In-memory token cache — avoids blocking file I/O on the event loop
# every time get_access_token() is called from a coroutine. Keyed by
# (active settings file path, account id) so each user's and each
# account's tokens are cached separately.
_token_cache: dict[tuple[str, str], dict] = {}


def _cache_key(account_id: str) -> tuple[str, str]:
    return (str(user_settings._settings_path()), account_id)


def _gc_pending() -> None:
    cutoff = time.time() - _PENDING_TTL_S
    for k, v in list(_pending.items()):
        if v.created_at < cutoff:
            _pending.pop(k, None)


# ---- settings blob access + legacy migration ------------------------------


def _new_account_id() -> str:
    return "acc_" + secrets.token_hex(4)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _load_oauth() -> dict:
    """Return the ``googleOAuth`` blob in the multi-account shape,
    migrating a legacy flat blob (single implicit account) in place —
    the migrated shape is persisted so the migration runs once per
    settings file."""
    s = user_settings.read()
    oauth = s.get("googleOAuth")
    if not isinstance(oauth, dict):
        return {"defaultAccount": None, "accounts": {}}
    if isinstance(oauth.get("accounts"), dict):
        return oauth
    # Legacy flat shape → one account entry.
    legacy_keys = ("clientId", "clientSecret", "tokens")
    if not any(k in oauth for k in legacy_keys):
        return {"defaultAccount": None, "accounts": {}}
    account_id = _new_account_id()
    tokens = oauth.get("tokens")
    email = tokens.get("account_email") if isinstance(tokens, dict) else None
    entry: dict[str, Any] = {
        "label": email or "Default",
        "clientId": oauth.get("clientId") or "",
        "clientSecret": oauth.get("clientSecret") or "",
        "createdAt": _now_iso(),
    }
    if isinstance(tokens, dict):
        entry["tokens"] = tokens
    migrated = {"defaultAccount": account_id, "accounts": {account_id: entry}}
    s["googleOAuth"] = migrated
    user_settings.write(s)
    logger.info(
        "googleOAuth settings migrated to multi-account shape "
        "(account %s, email %s)", account_id, email or "(not connected)",
    )
    return migrated


def _save_oauth(oauth: dict) -> None:
    s = user_settings.read()
    s["googleOAuth"] = oauth
    user_settings.write(s)


def _get_account(oauth: dict, account_id: str) -> dict:
    acct = (oauth.get("accounts") or {}).get(account_id)
    if not isinstance(acct, dict):
        raise UnknownAccount(f"no Google account with id {account_id!r}")
    return acct


# ---- account resolution ----------------------------------------------------


def resolve_account(selector: str | None = None) -> str:
    """Map a selector (account id, connected email, or label — the two
    text forms case-insensitive) to an account id.

    ``None`` / empty → the default account. Raises :class:`UnknownAccount`
    with a message listing the configured accounts when nothing matches
    or a label is ambiguous."""
    oauth = _load_oauth()
    accounts: dict[str, dict] = oauth.get("accounts") or {}
    if not selector:
        default = oauth.get("defaultAccount")
        if default and default in accounts:
            return default
        raise UnknownAccount(
            "no default Google account is set"
            + (f" — configured accounts: {_account_summary(accounts)}"
               if accounts else " (no accounts configured)")
        )
    sel = selector.strip()
    if sel in accounts:
        return sel
    sel_lower = sel.lower()
    email_hits = [
        aid for aid, a in accounts.items()
        if ((a.get("tokens") or {}).get("account_email") or "").lower() == sel_lower
    ]
    if len(email_hits) == 1:
        return email_hits[0]
    label_hits = [
        aid for aid, a in accounts.items()
        if (a.get("label") or "").strip().lower() == sel_lower
    ]
    if len(label_hits) == 1:
        return label_hits[0]
    if len(label_hits) > 1:
        raise UnknownAccount(
            f"Google account label {selector!r} is ambiguous "
            f"({len(label_hits)} accounts share it) — use the account "
            f"email instead: {_account_summary(accounts)}"
        )
    raise UnknownAccount(
        f"no Google account matches {selector!r} — configured accounts: "
        f"{_account_summary(accounts) or '(none)'}"
    )


def _account_summary(accounts: dict[str, dict]) -> str:
    parts = []
    for aid, a in accounts.items():
        email = (a.get("tokens") or {}).get("account_email")
        label = a.get("label") or aid
        parts.append(f"{label} <{email}>" if email else f"{label} (not connected)")
    return ", ".join(parts)


def account_order() -> list[str]:
    """Account ids in deterministic order: default first, then creation
    (dict insertion) order."""
    oauth = _load_oauth()
    accounts = oauth.get("accounts") or {}
    default = oauth.get("defaultAccount")
    order = [aid for aid in accounts if aid == default]
    order += [aid for aid in accounts if aid != default]
    return order


def connected_account_ids(required_scope: str | None = None) -> list[str]:
    """Ids of connected accounts (holding a refresh token), in
    :func:`account_order`. With ``required_scope``, only accounts whose
    grant includes that scope. This is the canonical iteration order for
    the read-probe fallback — deterministic by construction."""
    out = []
    for aid in account_order():
        if not is_connected(aid):
            continue
        if required_scope and required_scope not in _granted_scopes(aid):
            continue
        out.append(aid)
    return out


def account_email(account_id: str) -> str | None:
    """Connected email for an account, or None if not connected."""
    try:
        oauth = _load_oauth()
        acct = _get_account(oauth, account_id)
    except UnknownAccount:
        return None
    return (acct.get("tokens") or {}).get("account_email")


def account_label(account_id: str) -> str:
    try:
        oauth = _load_oauth()
        acct = _get_account(oauth, account_id)
    except UnknownAccount:
        return account_id
    return acct.get("label") or account_id


def _display(account_id: str) -> str:
    """Human-readable account name for error messages/logs."""
    email = account_email(account_id)
    label = account_label(account_id)
    if email and label and label != email:
        return f"{label} <{email}>"
    return email or label


def describe_accounts() -> str:
    """One-line roster of connected accounts, appended to Drive/Sheets
    tool descriptions at list-build time (see ToolSpec.dynamic_description).
    Empty string when nothing is connected."""
    oauth = _load_oauth()
    default = oauth.get("defaultAccount")
    rows = []
    for aid in account_order():
        acct = (oauth.get("accounts") or {}).get(aid) or {}
        tok = acct.get("tokens") or {}
        email = tok.get("account_email")
        if not tok.get("refresh_token") or not email:
            continue
        label = acct.get("label") or ""
        bits = email
        if label and label.lower() != email.lower():
            bits += f" (label: {label!r})"
        if aid == default:
            bits += " [default]"
        if SHEETS_SCOPE not in set((tok.get("scope") or "").split()):
            bits += " [no Sheets scope]"
        rows.append(bits)
    if not rows:
        return ""
    return (
        "\n\nConnected Google accounts: " + "; ".join(rows) + ". "
        "Pass `account` (email or label) to pick one; omit for the default."
    )


# ---- account CRUD ----------------------------------------------------------


def create_account(label: str, client_id: str, client_secret: str) -> str:
    """Register a new named account with its OAuth client credentials.
    Returns the new account id. The first account ever created becomes
    the default automatically."""
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError("client_id is required")
    if not isinstance(client_secret, str) or not client_secret.strip():
        raise ValueError("client_secret is required")
    label = (label or "").strip()
    if not label:
        raise ValueError("label is required")
    oauth = _load_oauth()
    accounts = oauth.setdefault("accounts", {})
    account_id = _new_account_id()
    accounts[account_id] = {
        "label": label,
        "clientId": client_id.strip(),
        "clientSecret": client_secret.strip(),
        "createdAt": _now_iso(),
    }
    if not oauth.get("defaultAccount"):
        oauth["defaultAccount"] = account_id
    _save_oauth(oauth)
    return account_id


async def update_account(
    account_id: str,
    *,
    label: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> None:
    """Patch an account. Changing the client credentials revokes + clears
    the account's tokens — they were issued against the old client and
    would silently keep working against the wrong project otherwise.
    A label change alone never touches the grant."""
    oauth = _load_oauth()
    acct = _get_account(oauth, account_id)

    creds_changed = (
        (client_id is not None and client_id.strip() != acct.get("clientId"))
        or (client_secret is not None and client_secret.strip() != acct.get("clientSecret"))
    )
    if creds_changed and (acct.get("tokens") or {}).get("refresh_token"):
        await disconnect(account_id)
        oauth = _load_oauth()  # re-read: disconnect() persisted
        acct = _get_account(oauth, account_id)

    if label is not None:
        if not label.strip():
            raise ValueError("label must be non-empty")
        acct["label"] = label.strip()
    if client_id is not None:
        if not client_id.strip():
            raise ValueError("client_id must be non-empty")
        acct["clientId"] = client_id.strip()
    if client_secret is not None:
        if not client_secret.strip():
            raise ValueError("client_secret must be non-empty")
        acct["clientSecret"] = client_secret.strip()
    _save_oauth(oauth)


async def delete_account(account_id: str) -> None:
    """Revoke (best-effort) and remove an account entry. If it was the
    default, the default moves to the first remaining account (creation
    order) or clears."""
    oauth = _load_oauth()
    _get_account(oauth, account_id)  # raises UnknownAccount if absent
    await disconnect(account_id)
    oauth = _load_oauth()
    oauth.get("accounts", {}).pop(account_id, None)
    if oauth.get("defaultAccount") == account_id:
        remaining = list(oauth.get("accounts") or {})
        oauth["defaultAccount"] = remaining[0] if remaining else None
    _save_oauth(oauth)
    _token_cache.pop(_cache_key(account_id), None)


def set_default_account(account_id: str) -> None:
    oauth = _load_oauth()
    _get_account(oauth, account_id)  # validate
    oauth["defaultAccount"] = account_id
    _save_oauth(oauth)


def get_client_credentials(account_id: str) -> tuple[str, str] | None:
    """``(client_id, client_secret)`` for one account, or None if that
    account has no credentials saved."""
    try:
        oauth = _load_oauth()
        acct = _get_account(oauth, account_id)
    except UnknownAccount:
        return None
    cid = acct.get("clientId")
    csec = acct.get("clientSecret")
    if isinstance(cid, str) and isinstance(csec, str) and cid and csec:
        return cid, csec
    return None


def get_client_config(account_id: str) -> dict[str, str]:
    """Raw saved label/client_id/client_secret for the Settings UI
    prefill. Localhost/authenticated-only endpoint, so returning the
    secret value is acceptable — same trust boundary as the LLM API
    keys already exposed via ``GET /api/settings``."""
    oauth = _load_oauth()
    acct = _get_account(oauth, account_id)
    return {
        "label": str(acct.get("label") or ""),
        "clientId": str(acct.get("clientId") or ""),
        "clientSecret": str(acct.get("clientSecret") or ""),
    }


# ---- token store (per account) ---------------------------------------------


def _get_tokens(account_id: str) -> dict | None:
    key = _cache_key(account_id)
    cached = _token_cache.get(key)
    if cached is not None:
        return cached
    try:
        oauth = _load_oauth()
        acct = _get_account(oauth, account_id)
    except UnknownAccount:
        return None
    tok = acct.get("tokens")
    if not isinstance(tok, dict):
        return None
    _token_cache[key] = tok
    return tok


def _set_tokens(account_id: str, tokens: dict) -> None:
    _token_cache[_cache_key(account_id)] = tokens
    oauth = _load_oauth()
    acct = _get_account(oauth, account_id)
    acct["tokens"] = tokens
    _save_oauth(oauth)


def _clear_tokens(account_id: str) -> None:
    _token_cache.pop(_cache_key(account_id), None)
    oauth = _load_oauth()
    try:
        acct = _get_account(oauth, account_id)
    except UnknownAccount:
        return
    acct.pop("tokens", None)
    _save_oauth(oauth)


# ---- public state ---------------------------------------------------------


def is_configured(account_id: str | None = None) -> bool:
    """With an id: that account has client credentials. Without: ANY
    account does."""
    if account_id is not None:
        return get_client_credentials(account_id) is not None
    oauth = _load_oauth()
    return any(
        (a.get("clientId") and a.get("clientSecret"))
        for a in (oauth.get("accounts") or {}).values()
    )


def is_connected(account_id: str | None = None) -> bool:
    """With an id: that account holds a refresh token. Without: ANY
    account does — this zero-arg form is the runtime visibility gate for
    Drive tools (they appear once at least one account is connected)."""
    if account_id is not None:
        tok = _get_tokens(account_id)
        return bool(tok and tok.get("refresh_token"))
    oauth = _load_oauth()
    return any(
        (a.get("tokens") or {}).get("refresh_token")
        for a in (oauth.get("accounts") or {}).values()
    )


def _granted_scopes(account_id: str) -> set[str]:
    tok = _get_tokens(account_id)
    if not tok:
        return set()
    return set((tok.get("scope") or "").split())


def has_sheets_scope(account_id: str | None = None) -> bool:
    """With an id: that account's grant includes the Sheets scope.
    Without: ANY connected account's does — the zero-arg form is the
    visibility gate for Sheets tools."""
    if account_id is not None:
        return SHEETS_SCOPE in _granted_scopes(account_id)
    return any(
        SHEETS_SCOPE in _granted_scopes(aid) for aid in account_order()
        if is_connected(aid)
    )


def _needs_reauth(account_id: str) -> bool:
    """Connected but missing one or more required API scopes — the user
    must re-authorise this account to get them."""
    if not is_connected(account_id):
        return False
    return bool(_REQUIRED_API_SCOPES - _granted_scopes(account_id))


def status() -> dict[str, Any]:
    """Status payload for the Settings UI: ordered account list + which
    one is the default."""
    oauth = _load_oauth()
    accounts_out: list[dict[str, Any]] = []
    for aid in account_order():
        acct = (oauth.get("accounts") or {}).get(aid) or {}
        tok = acct.get("tokens") or {}
        entry: dict[str, Any] = {
            "id": aid,
            "label": acct.get("label") or aid,
            "configured": bool(acct.get("clientId") and acct.get("clientSecret")),
            "connected": bool(tok.get("refresh_token")),
            "needs_reauth": _needs_reauth(aid),
            "has_sheets_scope": has_sheets_scope(aid),
        }
        if tok:
            entry["account_email"] = tok.get("account_email")
            entry["scopes"] = (tok.get("scope") or "").split()
            entry["expires_in_s"] = max(
                0, int((tok.get("expires_at") or 0) - time.time())
            )
        accounts_out.append(entry)
    return {
        "default_account": oauth.get("defaultAccount"),
        "accounts": accounts_out,
        # Aggregates the panel + legacy callers key off.
        "configured": is_configured(),
        "connected": is_connected(),
    }


# ---- authorization URL ---------------------------------------------------


def build_authorize_url(
    account_id: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    user_email: str | None = None,
) -> tuple[str, str]:
    """Return ``(url, state)`` for connecting one account. The pending
    state pins the account id, the exact redirect_uri (the token
    exchange must repeat it), and — in server mode — the user who
    started the flow, so the callback can refuse a cross-user replay."""
    creds = get_client_credentials(account_id)
    if creds is None:
        raise RuntimeError(
            f"Google OAuth client_id/secret not configured for account "
            f"{_display(account_id)}"
        )
    client_id, _ = creds

    state = secrets.token_urlsafe(24)
    _gc_pending()
    _pending[state] = _PendingState(
        account_id=account_id, redirect_uri=redirect_uri, user_email=user_email,
    )

    from urllib.parse import urlencode

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # ``access_type=offline`` requests a refresh token. ``prompt=consent``
        # forces re-consent so a refresh token is RE-issued even if the
        # user already authorised this app once before (Google only
        # includes refresh_token on first consent otherwise).
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}", state


def consume_state(state: str) -> _PendingState | None:
    """Verify and atomically remove ``state``. Returns the pending
    record (account id + redirect_uri + user) or None if invalid."""
    _gc_pending()
    pending = _pending.pop(state, None)
    if pending is None:
        return None
    if time.time() - pending.created_at > _PENDING_TTL_S:
        return None
    return pending


# ---- token exchange + refresh --------------------------------------------


async def exchange_code(
    code: str, account_id: str, redirect_uri: str = DEFAULT_REDIRECT_URI
) -> dict:
    """Exchange an authorization code for access + refresh tokens for
    one account. Enforces one-entry-per-email: if the email Google
    returns is already connected on a DIFFERENT account entry, the
    exchange is rejected (email is the durable identity scripts and
    refs pin — duplicates would make those ambiguous). Persists and
    returns the stored token dict."""
    creds = get_client_credentials(account_id)
    if creds is None:
        raise RuntimeError(
            f"client_id/secret not configured for account {_display(account_id)}"
        )
    client_id, client_secret = creds

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if r.status_code != 200:
        raise RuntimeError(
            f"token exchange failed {r.status_code}: {r.text[:300]}"
        )
    payload = r.json()

    access_token = payload["access_token"]
    refresh_token = payload.get("refresh_token")
    expires_in = int(payload.get("expires_in", 3600))
    scope = payload.get("scope", "")
    id_token = payload.get("id_token")
    acct_email = _email_from_id_token(id_token)

    if acct_email:
        oauth = _load_oauth()
        for other_id, other in (oauth.get("accounts") or {}).items():
            if other_id == account_id:
                continue
            other_email = (other.get("tokens") or {}).get("account_email")
            if (
                other_email
                and other_email.lower() == acct_email.lower()
                and (other.get("tokens") or {}).get("refresh_token")
            ):
                raise RuntimeError(
                    f"{acct_email} is already connected on account "
                    f"{other.get('label') or other_id!r}. Disconnect it "
                    f"there first, or connect a different Google account "
                    f"here."
                )

    tok = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + expires_in,
        "scope": scope,
        "account_email": acct_email,
        "token_type": payload.get("token_type", "Bearer"),
    }
    _set_tokens(account_id, tok)
    return tok


async def get_access_token(
    account: str | None = None, *, force_refresh: bool = False
) -> str:
    """Return a valid access token for the selected account, refreshing
    if within ``REFRESH_GRACE_S`` of expiry.

    ``account`` is a selector (id, email, or label); None → default
    account. ``force_refresh=True`` skips the expiry check and refreshes
    unconditionally — the ONE sanctioned way to recover from a 401 on a
    token the grace window still considers valid (never poke
    ``expires_at`` in the settings file; that bypasses the cache).

    Raises :class:`UnknownAccount` for a bad selector and RuntimeError
    if the account isn't connected or the refresh fails."""
    account_id = resolve_account(account)
    tok = _get_tokens(account_id)
    if not tok:
        raise RuntimeError(
            f"Google account {_display(account_id)} is not connected — "
            "no OAuth tokens stored"
        )
    if not force_refresh and time.time() < (tok.get("expires_at") or 0) - REFRESH_GRACE_S:
        return tok["access_token"]
    # Refresh.
    refresh_token = tok.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            f"access token for {_display(account_id)} expired and no "
            "refresh_token available — user must re-authorise"
        )
    creds = get_client_credentials(account_id)
    if creds is None:
        raise RuntimeError(
            f"client_id/secret not configured for account {_display(account_id)}"
        )
    client_id, client_secret = creds
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if r.status_code != 200:
        raise RuntimeError(
            f"token refresh failed for {_display(account_id)} "
            f"{r.status_code}: {r.text[:300]}"
        )
    payload = r.json()
    new_access = payload["access_token"]
    expires_in = int(payload.get("expires_in", 3600))
    # Google's refresh response usually omits refresh_token; keep the
    # original. It also sometimes returns a new id_token.
    id_token = payload.get("id_token")
    if id_token:
        em = _email_from_id_token(id_token)
        if em:
            tok["account_email"] = em
    tok["access_token"] = new_access
    tok["expires_at"] = time.time() + expires_in
    if "scope" in payload:
        tok["scope"] = payload["scope"]
    _set_tokens(account_id, tok)
    return new_access


async def disconnect(account_id: str) -> None:
    """Revoke one account's refresh token (best-effort) and clear its
    local tokens. Keeps the account entry + credentials so reconnecting
    doesn't require re-pasting them."""
    tok = _get_tokens(account_id)
    if tok:
        rt = tok.get("refresh_token") or tok.get("access_token")
        if rt:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(REVOKE_URL, data={"token": rt})
            except Exception:
                pass
    _clear_tokens(account_id)


# ---- ID token (account email) -------------------------------------------


def _email_from_id_token(id_token: str | None) -> str | None:
    """Decode the email claim from a Google ID token. We DON'T verify
    the JWT signature — the token came back over a TLS-protected
    request to Google's token endpoint, so the source is trusted; we
    only need to read the payload claim."""
    if not id_token or not isinstance(id_token, str):
        return None
    try:
        parts = id_token.split(".")
        if len(parts) != 3:
            return None
        import base64
        payload_b64 = parts[1]
        # JWT uses base64url without padding.
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        em = payload.get("email")
        return em if isinstance(em, str) else None
    except Exception:
        return None
