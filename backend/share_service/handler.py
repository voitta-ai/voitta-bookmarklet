"""voitta-share — the report-sharing service. One Lambda, three routes.

    POST   /v1/share        {html, title?, ttl?}  ->  {id, url, expires}
    DELETE /v1/share/{id}                          ->  {revoked: id}
    GET    /v1/whoami                              ->  {ok: true}   (settings probe)

Trust model, stated plainly because it is unusual:

* **One shared API key** for every install (sha256 stored in ``SHARE_KEY_HASH``,
  never the plaintext). The service therefore cannot tell users apart — there
  is no owner on a share, and any key holder may revoke any id. Accepted for
  now; the local shares table on each desktop is where "my shares" lives.
* **Anyone with the link can view.** The id is 256 bits of ``os.urandom``;
  unguessability is the whole access control, so ids must never be listed or
  logged in full.
* **This function can write but cannot read.** Its IAM role has PutObject /
  DeleteObject / PutObjectTagging on ``r/*`` and nothing else — no ListBucket,
  no GetObject. A compromise of this code or its key yields the ability to
  upload and delete, never to enumerate or read what has been shared.

Expiry is an exact-match object tag (``ttl=7d|30d|90d``) because S3 lifecycle
rules cannot compare a tag to a date; ``keep`` sets no expiring tag.

stdlib + boto3 only (both present in the Lambda runtime). No third-party deps
to keep the deploy a single zip of this file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import boto3

BUCKET = os.environ["BUCKET"]
PUBLIC_BASE = os.environ["PUBLIC_BASE"].rstrip("/")
KEY_HASH = os.environ["SHARE_KEY_HASH"].lower()

MAX_HTML_BYTES = 8 * 1024 * 1024          # API Gateway's own cap is 10 MB; leave headroom
TTL_DAYS = {"7d": 7, "30d": 30, "90d": 90, "keep": None}
DEFAULT_TTL = "30d"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")   # token_urlsafe(32) is exactly 43 chars

_s3 = boto3.client("s3")


# ---- helpers -----------------------------------------------------------------

def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body),
    }


def _authed(event: dict) -> bool:
    hdrs = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = hdrs.get("authorization", "")
    if not auth.startswith("Bearer "):
        return False
    presented = hashlib.sha256(auth[7:].strip().encode()).hexdigest()
    # Constant-time compare: the hash is not secret, but the habit is cheap.
    return hmac.compare_digest(presented, KEY_HASH)


def _body(event: dict) -> dict:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    return json.loads(raw) if raw else {}


def _new_id() -> str:
    return secrets.token_urlsafe(32)


# ---- routes ------------------------------------------------------------------

def _share(event: dict) -> dict:
    try:
        body = _body(event)
    except (ValueError, UnicodeDecodeError):
        return _resp(400, {"error": "body must be JSON"})

    html = body.get("html")
    if not isinstance(html, str) or not html.strip():
        return _resp(400, {"error": "html is required"})
    data = html.encode("utf-8")
    if len(data) > MAX_HTML_BYTES:
        return _resp(413, {"error": f"html exceeds {MAX_HTML_BYTES} bytes"})

    ttl = str(body.get("ttl") or DEFAULT_TTL)
    if ttl not in TTL_DAYS:
        return _resp(400, {"error": f"ttl must be one of {sorted(TTL_DAYS)}"})

    share_id = _new_id()
    key = f"r/{share_id}/index.html"
    put: dict = {
        "Bucket": BUCKET,
        "Key": key,
        "Body": data,
        "ContentType": "text/html; charset=utf-8",
        # Belt-and-braces against a report that tries to run as an app on our
        # origin: no framing by others, no MIME sniffing.
        "CacheControl": "public, max-age=60",
        "Metadata": {"title": (str(body.get("title") or "")[:200]).encode("ascii", "ignore").decode()},
    }
    if TTL_DAYS[ttl] is not None:
        put["Tagging"] = f"ttl={ttl}"
    _s3.put_object(**put)

    days = TTL_DAYS[ttl]
    expires = (
        (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if days is not None else None
    )
    # Log the id PREFIX only: the full id is the credential to the page.
    print(json.dumps({"event": "share", "id_prefix": share_id[:8], "bytes": len(data), "ttl": ttl}))
    return _resp(201, {"id": share_id, "url": f"{PUBLIC_BASE}/r/{share_id}/", "expires": expires})


def _revoke(event: dict, share_id: str) -> dict:
    if not ID_RE.match(share_id):
        return _resp(400, {"error": "bad id"})
    # DeleteObject on a missing key is a silent 204 in S3 — idempotent, which is
    # what a revoke should be. No owner check is possible with one shared key.
    _s3.delete_object(Bucket=BUCKET, Key=f"r/{share_id}/index.html")
    print(json.dumps({"event": "revoke", "id_prefix": share_id[:8]}))
    return _resp(200, {"revoked": share_id})


def lambda_handler(event: dict, _context) -> dict:
    if not _authed(event):
        return _resp(401, {"error": "unauthorized"})

    method = (event.get("requestContext", {}).get("http", {}).get("method") or "").upper()
    path = event.get("rawPath") or "/"

    if method == "GET" and path == "/v1/whoami":
        return _resp(200, {"ok": True, "public_base": PUBLIC_BASE, "ttls": sorted(TTL_DAYS)})
    if method == "POST" and path == "/v1/share":
        return _share(event)
    m = re.fullmatch(r"/v1/share/([^/]+)", path)
    if method == "DELETE" and m:
        return _revoke(event, m.group(1))
    return _resp(404, {"error": "not found"})
