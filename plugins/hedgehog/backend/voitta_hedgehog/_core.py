"""Shared plumbing: the StockTrak call, settings, error envelopes.

Every StockTrak call goes through the ``hedgehog_request`` browser
primitive (plugins/hedgehog/frontend/widget.ts): a same-origin fetch from
the user's own StockTrak tab. The session cookie never reaches this
process; only the parsed response does.

Envelope convention (same as the other plugins):

    {"ok": True, ...payload}
    {"ok": False, "error": kind, "message": str, "hint"?: str}
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.services import user_settings
from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx

from voitta_hedgehog.rules import option_type_from_symbol

_logger = logging.getLogger(__name__)

PLUGIN = "hedgehog"
ET = ZoneInfo("America/New_York")

_NO_WRITE_RETRY = (
    " Do NOT retry orders, notes, edits, cancels or exercises on your own. When the user says they're back: "
    "first check hedgehog_orders (open and history) for anything already placed, then re-preview; every "
    "order needs the user's confirmation again."
)
HINTS = {
    "session_expired": (
        "The StockTrak session has expired (a banner in the chat already asks the user to log in again in a new "
        "tab). Stop and tell the user; wait for them." + _NO_WRITE_RETRY
    ),
    "site_check": (
        "StockTrak's bot check (Incapsula) answered instead of StockTrak. The banner asks the user to open "
        "StockTrak in a new tab, which passes the check. Stop and wait for the user." + _NO_WRITE_RETRY
    ),
    "unexpected_page": (
        "StockTrak returned a web page instead of data. Possibly a logout, an outage or a changed endpoint. "
        "Try one read-only call (hedgehog_inspect_page); if that fails too, stop and tell the user." + _NO_WRITE_RETRY
    ),
    "tab_unreachable": (
        "The StockTrak tab stopped answering: it navigated away (e.g. logged out by another StockTrak tab), "
        "was closed, or the bookmarklet isn't open there. Ask the user to open StockTrak, log in if needed, and "
        "click the bookmarklet again." + _NO_WRITE_RETRY
    ),
    "wrong_host": "Ask the user to open the bookmarklet on their app.stocktrak.com tab.",
    "server_error_page": (
        "Usually a missing/invalid parameter or nothing to show (e.g. no positions of that type). "
        "Check docs/03-stocktrak-api.md (rag_query) before retrying."
    ),
    "path_not_allowed": "Use one of the documented endpoints (rag_query 'hedgehog stocktrak api').",
}


class StockTrakError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def fail(kind: str, message: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "error": kind, "message": message}
    if kind in HINTS:
        out["hint"] = HINTS[kind]
    out.update(extra)
    return out


async def st_call(
    ctx: ToolCtx,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    *,
    write: bool = False,
    keep_html: bool = False,
    timeout_ms: int = 30_000,
) -> dict[str, Any]:
    """One StockTrak request via the browser. Returns the primitive payload
    ({status, kind: json|csv|html, body|rows, ...}); raises StockTrakError."""
    args: dict[str, Any] = {"method": method, "path": path, "params": params or {}, "write": write,
                            "keep_html": keep_html}
    if json_body is not None:
        args["json_body"] = json_body
    try:
        # Our own deadline: Chainlit's call_fn waits 5 minutes and then returns None (no error) when
        # the tab is gone — e.g. StockTrak's cross-tab logout navigated it to /logout mid-call.
        res = await asyncio.wait_for(call_browser("hedgehog_request", args, ctx, timeout_ms=timeout_ms),
                                     timeout=timeout_ms / 1000 + 5)
    except asyncio.TimeoutError:
        res = None
    except BrowserToolError as exc:
        kind = "unexpected_page" if exc.kind == "bad_json" else exc.kind
        if kind in SESSION_BREAKS:
            note_session_break(ctx)
        raise StockTrakError(kind, str(exc)) from None
    if not isinstance(res, dict):
        note_session_break(ctx)
        what = ("The order/change may or may not have reached StockTrak. " if write else "")
        raise StockTrakError("tab_unreachable", f"The StockTrak tab didn't answer {method} {path} within "
                                                f"{timeout_ms // 1000 + 5} s. {what}")
    return res


# ── session breaks ─────────────────────────────────────────────────────────
# After the StockTrak session drops, nothing previewed before it may be placed, and the next order
# asks the user even when "don't ask" is on. Per chat session, in memory.

SESSION_BREAKS = {"session_expired", "site_check", "unexpected_page", "tab_unreachable"}
_breaks: dict[str, float] = {}


def note_session_break(ctx: ToolCtx) -> None:
    _breaks[ctx.session_id or "default"] = time.time()


def last_session_break(ctx: ToolCtx) -> float | None:
    return _breaks.get(ctx.session_id or "default")


def clear_session_break(ctx: ToolCtx) -> None:
    _breaks.pop(ctx.session_id or "default", None)


def body_of(res: dict[str, Any]) -> dict[str, Any]:
    """The JSON body of a primitive result, case-folding the Success flag."""
    body = res.get("body") if isinstance(res.get("body"), dict) else {}
    if "success" in body and "Success" not in body:
        body["Success"] = body["success"]
    return body


def rows_of(body: dict[str, Any], key: str = "Html") -> list[dict[str, Any]]:
    """Parsed rows of an HTML-fragment field (see parsers.ts ParsedHtml)."""
    frag = body.get(key)
    return frag.get("rows", []) if isinstance(frag, dict) else []


def text_of(body: dict[str, Any], key: str) -> str:
    frag = body.get(key)
    if isinstance(frag, dict):
        return frag.get("text", "")
    return str(frag or "")


def el_attr(row: dict[str, Any], cls: str, attr: str) -> str | None:
    """data-* attribute of the first element with class ``cls`` in a parsed row."""
    for el in row.get("els") or []:
        if el.get("cls") == cls and attr in (el.get("data") or {}):
            return el["data"][attr]
    return None


def csv_records(rows: list[list[str]]) -> list[dict[str, str]]:
    """Header row + data rows → list of dicts (header keys stripped)."""
    if not rows:
        return []
    head = [h.strip() for h in rows[0]]
    return [dict(zip(head, (c.strip() for c in r))) for r in rows[1:] if any(c.strip() for c in r)]


# ---- value normalisation -------------------------------------------------------
# StockTrak hands every number back as a string, in mixed formats
# ("1,517.18", "1190141.4096", "$0.00", "($0.29)", "-12.3%"). Tools return
# plain numbers instead: money rounded to cents, everything else to 4 dp.
# Percent values stay in percent units (12.3 means 12.3 %).

_NUMLIKE = re.compile(r"^\(?[-+]?\$?[-+]?(\d{1,3}(,\d{3})+|\d*)(\.\d+)?%?\)?$")
_MONEY_WORDS = ("value", "profit", "amount", "cash", "balance", "cost", "commission", "proceeds", "interest",
                "equity", "power", "loan", "requirement", "price paid")
_PCT_WORDS = ("%", "percent", "return", "rate")

def num(value: Any) -> float | int | None:
    """'1,517.18' / '$0.00' / '($0.29)' / '-12.3%' / '770' → number; None if not purely numeric."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    s = str(value or "").strip()
    if not s or not any(ch.isdigit() for ch in s) or not _NUMLIKE.match(s):
        return None
    neg = (s.startswith("(") and s.endswith(")")) or "-" in s
    digits = re.sub(r"[^0-9.]", "", s)
    try:
        v = float(digits)
    except ValueError:
        return None
    v = -v if neg else v
    return int(v) if v.is_integer() and "." not in digits else v

def _is_money_key(key: str) -> bool:
    k = key.lower()
    return any(w in k for w in _MONEY_WORDS) and not any(w in k for w in _PCT_WORDS)

def norm_value(key: str, value: Any) -> Any:
    """One field → number (rounded by kind) or the original string."""
    n = num(value)
    if n is None or isinstance(n, int):
        return value if n is None else n
    return round(n, 2) if _is_money_key(key) else round(n, 4)

def norm_record(rec: dict[str, Any]) -> dict[str, Any]:
    return {k: norm_value(k, v) for k, v in rec.items()}

# StockTrak typos in CSV labels. Normalised so code keeps working if they are
# ever fixed upstream (both spellings map to the corrected key).
_KEY_TYPOS = {"Accured": "Accrued", "Matket": "Market"}

def fix_key(key: str) -> str:
    for bad, good in _KEY_TYPOS.items():
        key = key.replace(bad, good)
    return key

# "Your Market Buy for 573 shares of SPY" — StockTrak's own confirmation. It
# can name FEWER shares than were ordered (it silently caps an order at the
# remaining buying power) while order history still says "Filled <ordered>".
_CONFIRMED_QTY = re.compile(
    r"\bYour\s+[A-Za-z /-]{0,40}?\bfor\s+([\d,]+)\s+(shares?|contracts?)\s+of\s+([A-Z0-9][A-Z0-9.\-]*)", re.I)

def all_text(body: dict[str, Any]) -> str:
    """Every text-ish value of a parsed body, joined (HTML fragments by their text)."""
    parts: list[str] = []
    for v in body.values():
        if isinstance(v, dict) and isinstance(v.get("text"), str):
            parts.append(v["text"])
        elif isinstance(v, str):
            parts.append(v)
    return " ".join(parts)

def confirmed_quantity(text: str, symbol: str | None = None) -> int | None:
    """Quantity in StockTrak's 'Your … for N shares of SYM' text (None if absent)."""
    for m in _CONFIRMED_QTY.finditer(text or ""):
        if symbol and m.group(3).upper().rstrip(".") != symbol.upper():
            continue
        return int(m.group(1).replace(",", ""))
    return None

def keep_type(recs: list[dict[str, Any]], stype: str) -> list[dict[str, Any]]:
    """ExportOpenPositions ignores securityType=Options and returns the equity rows too;
    keep only rows of the requested type (option rows have option symbols, e.g. F2620K12.5)."""
    want_option = stype == "Options"
    return [r for r in recs if (option_type_from_symbol(str(r.get("Symbol") or "")) is not None) == want_option]


async def open_positions(ctx: ToolCtx, stype: str) -> list[dict[str, Any]]:
    """Open positions of one type ('Equities' | 'Options') from the CSV export, filtered and normalised."""
    csv = await st_call(ctx, "GET", "/admin/ExportOpenPositions/", {"securityType": stype})
    return [norm_record(r) for r in keep_type(csv_records(csv.get("rows") or []), stype)]


# ---- order ↔ fill matching -------------------------------------------------------
# Order history shows the ORDERED quantity as "Filled" even when StockTrak
# capped the order. The transactions export has the real fills but no order
# id, so fills are matched to orders by symbol, side and time.

# Seen live (11/2026): order history "11/04/2026 13:00", transactions export
# "11/04/2026 - 13:00" (note the " - ").
_TIME_FORMATS = ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p",
                 "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%Y-%m-%d")
# Fill timestamps can be a little earlier than the order's (separate clocks).
_FILL_SLOP_S = 120
# During the session a market order's fill lands within minutes (13:00 → 13:02
# seen); one entered after hours fills at the next open, up to a weekend later.
_NEAR_S = 10 * 60
_LATE_S = 4 * 86_400

def parse_st_time(value: Any) -> tuple[datetime, bool] | None:
    """StockTrak date/time text → (naive ET datetime, has_time). None if unparseable."""
    s = " ".join(str(value or "").replace("\n", " ").split())
    s = re.sub(r"\s*\b(ET|EST|EDT)\b\s*$", "", s, flags=re.I).split(".")[0]
    s = re.sub(r"^(\d{1,2}/\d{1,2}/\d{4})\s+-\s+", r"\1 ", s)  # "11/04/2026 - 13:00"
    for f in _TIME_FORMATS:
        try:
            return datetime.strptime(s, f), "%H" in f or "%I" in f
        except ValueError:
            continue
    return None

def side_of(text: Any) -> str | None:
    """buy | sell | short | cover from an order or transaction label ('Market - Buy', 'Buy to Cover', …)."""
    t = str(text or "").lower()
    for side in ("cover", "short", "sell", "buy"):
        if side in t:
            return side
    return None

def _delay_s(o_when: tuple[datetime, bool], f_when: tuple[datetime, bool]) -> float | None:
    """Seconds from order to fill, or None if this fill can't belong to the order."""
    (o_at, o_t), (f_at, f_t) = o_when, f_when
    if o_t and f_t:
        d = (f_at - o_at).total_seconds()
        return max(d, 0.0) if -_FILL_SLOP_S <= d <= _LATE_S else None
    days = (f_at.date() - o_at.date()).days
    return days * 86_400.0 if 0 <= days <= _LATE_S // 86_400 else None

def _price_ok(a: Any, b: Any) -> bool:
    """Same execution price, or one side unknown (masked $XXX.xx, missing)."""
    return not (isinstance(a, (int, float)) and isinstance(b, (int, float))) or abs(a - b) < 0.006

def attach_fills(orders: list[dict[str, Any]], txs: list[dict[str, Any]]) -> int:
    """Add filled_qty / fill_price / fill_time / partial to 'Filled' orders, in place.

    The transactions export has no order id. A fill belongs to an order of the
    same symbol and side, at or shortly after the order time, at the order's
    execution price when both are known (order history shows it, e.g. $768.95
    for the capped 770 SPY order). Each fill is used once. Two passes, orders
    oldest first: fills within 10 minutes, then (for orders entered after
    hours) the first fill up to 4 days later. Returns the number matched."""
    fills = []
    for t in txs:
        ttype = str(t.get("TransactionType") or "")
        side = side_of(ttype)
        when = parse_st_time(t.get("CreateDate"))
        q = num(t.get("Quantity"))
        if "proceeds" in ttype.lower():
            # An equity short fills as "Short Proceeds" with a NEGATIVE quantity;
            # covering it adds a second "Short Proceeds" row with a positive one
            # (alongside the real "Market - Cover" row). Only the first is a fill.
            if q is None or q >= 0:
                continue
            side = "short"
        if side and when and q:
            fills.append({"symbol": str(t.get("Symbol") or "").upper(), "side": side, "at": when,
                          "qty": abs(q), "price": num(t.get("Price")), "time": t.get("CreateDate"), "used": False})

    filled = [o for o in orders if "fill" in str(o.get("status") or "").lower()]
    timed = [(parse_st_time(o.get("date")), i, o) for i, o in enumerate(filled)]
    # Unparseable order times go last; page order is newest first, so reverse.
    timed.sort(key=lambda x: (x[0] is None, x[0][0] if x[0] else datetime.max, -x[1]))

    def best(when, o, max_delay: float):
        sym, side = str(o.get("symbol") or "").upper(), side_of(o.get("order"))
        cands = []
        for f in fills:
            if f["used"] or f["symbol"] != sym or f["side"] != side:
                continue
            if not _price_ok(o.get("executed_price"), f["price"]):
                continue
            d = _delay_s(when, f["at"]) if when else 0.0
            if d is not None and d <= max_delay:
                cands.append((d, f["at"][0], f))
        return min(cands, key=lambda c: (c[0], c[1]))[2] if cands else None

    matched = 0
    pending = []
    for max_delay in (_NEAR_S, _LATE_S):
        pending = []
        for when, i, o in timed:
            if "fill_match" in o:
                continue
            hit = best(when, o, max_delay)
            if hit is None:
                pending.append((when, i, o))
                continue
            hit["used"] = True
            matched += 1
            ordered = num(o.get("quantity"))
            fq = int(hit["qty"]) if float(hit["qty"]).is_integer() else hit["qty"]
            o.update(filled_qty=fq, fill_price=hit["price"], fill_time=hit["time"],
                     partial=bool(ordered is not None and fq < abs(ordered)), fill_match="matched")
        timed = pending
    for _, _, o in pending:
        o.update(filled_qty=None, partial=None, fill_match="not_found")
    return matched

def earliest_date(orders: list[dict[str, Any]]) -> str | None:
    """M/D/YYYY of the oldest parseable order date (for the transactions export range)."""
    days = [p[0].date() for o in orders if (p := parse_st_time(o.get("date")))]
    if not days:
        return None
    d = min(days)
    return f"{d.month}/{d.day}/{d.year}"

def today_et() -> str:
    """Today's date in New York as StockTrak's M/D/YYYY."""
    d = datetime.now(ET)
    return f"{d.month}/{d.day}/{d.year}"

def now_et_iso() -> str:
    """Fetch time, ET, to the second — the as-of stamp on read tools."""
    return datetime.now(ET).isoformat(timespec="seconds")


def setting(key: str, default: Any = None) -> Any:
    """Read plugins.hedgehog.<key> (canonical, written by the Settings panel)."""
    try:
        blob = user_settings.read()
    except Exception:
        return default
    plugins = blob.get("plugins") if isinstance(blob, dict) else None
    mine = plugins.get(PLUGIN) if isinstance(plugins, dict) else None
    if isinstance(mine, dict) and key in mine:
        return mine[key]
    flat = blob.get(f"plugins.{PLUGIN}.{key}") if isinstance(blob, dict) else None
    return default if flat is None else flat


def trading_enabled() -> bool:
    return bool(setting("trading_enabled", False))


def confirm_orders() -> bool:
    # Unset == ask (the safe default, and what an unchecked box in Settings means).
    return not bool(setting("skip_order_confirmation", False))
