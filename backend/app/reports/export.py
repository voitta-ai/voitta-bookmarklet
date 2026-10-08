"""Package a rendered HTML report as a self-contained, offline zip.

The zip holds ``index.html`` (the report, cleaned) and ``README.txt``.
Nothing else is needed: ``build(ctx)`` returns a string that *is* the
report, and script authors embed images as ``data:`` URIs (there is no
asset-attachment channel in :class:`~app.reports.ctx.ScriptContext`), so
a report is already one document by construction.

Two transformations make that document work with the backend gone:

1. **Strip the platform injection.** :func:`render_html` adds two
   ``<meta voitta-*>`` tags, a favicon link, and the screenshot shim
   (``/api/_html_to_image.js`` + ``/api/_panel_shim.js``). The shim POSTs
   render events to ``/api/report-render-events`` — fire-and-forget and
   swallowed, but a 404 in the console of every exported report, and dead
   weight. Every one of those goes.

2. **Inline backend-local references.** A script can still hand-write
   ``<img src="/api/workspace/data/<handle>/files/chart.png">`` or point
   at ``/api/uploads/…``. Those bytes live only on this machine; the
   export embeds them as ``data:`` URIs so the page survives the trip.
   Anything ``https://`` is left alone and listed in the README — it is
   the author's dependency, not ours, and it still resolves online.

Stdlib only. The userbase has no HTML parser, and the report HTML is
LLM-authored free-form, so regexes over attribute values are both the
available tool and the honest one.
"""

from __future__ import annotations

import base64
import io
import logging
import mimetypes
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ---- the injection, in reverse --------------------------------------------

_INJECTED_RE = re.compile(
    r"""[ \t]*(?:
        <meta\s+name="voitta-(?:slug|render-id)"[^>]*>
      | <link\s+rel="icon"[^>]*href="/favicon\.svg"[^>]*>
      | <script\s+src="/api/_html_to_image\.js"[^>]*>\s*</script>
      | <script\s+src="/api/_panel_shim\.js"[^>]*>\s*</script>
    )[ \t]*\r?\n?""",
    re.IGNORECASE | re.VERBOSE,
)


def strip_platform_injection(html: str) -> str:
    """Remove exactly what :func:`render_html` added — nothing the author wrote."""
    return _INJECTED_RE.sub("", html)


# ---- backend-local references ---------------------------------------------

# ``src=`` / ``href=`` values that point at this backend's file routes.
_LOCAL_REF_RE = re.compile(
    r"""(?P<attr>\b(?:src|href)\s*=\s*)(?P<q>["'])(?P<url>/api/(?:workspace/data/[^"'?#]+/files/[^"'?#]+|uploads/[^"'?#]+))(?:[?#][^"']*)?(?P=q)""",
    re.IGNORECASE,
)
_REMOTE_REF_RE = re.compile(
    r"""\b(?:src|href)\s*=\s*["'](?P<url>https?://[^"']+)["']""", re.IGNORECASE
)

_MAX_INLINE_BYTES = 25 * 1024 * 1024  # a single inlined asset; keeps the zip sane


@dataclass
class ExportReport:
    inlined: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)   # local refs we could not read
    remote: list[str] = field(default_factory=list)       # left as-is, need the network


def _resolve_local(url: str) -> Path | None:
    """Map a backend file route to the file it would serve, or None.

    Mirrors the two serving routes' own guards (snapshot-dir containment,
    uploads-dir containment) so the export cannot read anything the route
    would have refused.
    """
    m = re.fullmatch(r"/api/workspace/data/([^/]+)/files/(.+)", url)
    if m:
        from app.services.python_storage import get as ps_get

        rec = ps_get(m.group(1))
        if rec is None:
            return None
        snap = Path(rec["path"]).resolve()
        try:
            target = (snap / m.group(2)).resolve()
            target.relative_to(snap)
        except (ValueError, OSError):
            return None
        return target if target.is_file() else None

    m = re.fullmatch(r"/api/uploads/(.+)", url)
    if m:
        from app.services.projects import project_data_root

        base = (project_data_root() / "uploads").resolve()
        target = (base / m.group(1)).resolve()
        if not str(target).startswith(str(base) + "/"):
            return None
        return target if target.is_file() else None
    return None


def inline_local_refs(html: str, report: ExportReport) -> str:
    """Replace backend-local ``src``/``href`` values with ``data:`` URIs."""

    def sub(m: re.Match) -> str:
        url = m.group("url")
        path = _resolve_local(url)
        if path is None:
            report.unresolved.append(url)
            return m.group(0)
        try:
            data = path.read_bytes()
        except OSError:
            report.unresolved.append(url)
            return m.group(0)
        if len(data) > _MAX_INLINE_BYTES:
            report.unresolved.append(f"{url} (too large: {len(data)} bytes)")
            return m.group(0)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        b64 = base64.b64encode(data).decode("ascii")
        report.inlined.append(url)
        return f"{m.group('attr')}{m.group('q')}data:{mime};base64,{b64}{m.group('q')}"

    out = _LOCAL_REF_RE.sub(sub, html)
    report.remote = sorted({m.group("url") for m in _REMOTE_REF_RE.finditer(out)})
    return out


# ---- the zip ------------------------------------------------------------------

def _readme(slug: str, render_id: str, title: str | None, report: ExportReport) -> str:
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"{title or slug}",
        "=" * len(title or slug),
        "",
        "Open index.html in any browser. No Voitta server is needed.",
    ]
    if report.remote:
        lines.append("An internet connection IS needed — see the list below.")
    else:
        lines.append("The report is fully self-contained; it works offline.")
    lines += [
        "",
        f"Exported from Voitta Bookmarklet on {when}.",
        f"Script: {slug}   Render: {render_id}",
    ]
    if report.inlined:
        lines += ["", f"{len(report.inlined)} local asset(s) were embedded into the page:"]
        lines += [f"  - {u}" for u in report.inlined]
    if report.remote:
        lines += ["", "These references point at the internet and were left as written;",
                  "they will load only while online:"]
        lines += [f"  - {u}" for u in report.remote]
    if report.unresolved:
        lines += ["", "WARNING: these local references could not be embedded and will",
                  "not load from the exported copy:"]
        lines += [f"  - {u}" for u in report.unresolved]
    return "\n".join(lines) + "\n"


def build_zip(html: str, *, slug: str, render_id: str, title: str | None = None) -> bytes:
    """Return the zip bytes for one rendered report body."""
    report = ExportReport()
    cleaned = strip_platform_injection(html)
    cleaned = inline_local_refs(cleaned, report)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", cleaned)
        zf.writestr("README.txt", _readme(slug, render_id, title, report))
    logger.info(
        "report export %s/%s: inlined=%d remote=%d unresolved=%d",
        slug, render_id, len(report.inlined), len(report.remote), len(report.unresolved),
    )
    return buf.getvalue()


def zip_filename(slug: str, render_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", slug).strip("-") or "report"
    return f"{safe}-{render_id[:8]}.zip"
