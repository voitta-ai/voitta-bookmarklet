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

import logging
import re
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

HINTS = {
    "session_expired": (
        "The StockTrak session has expired. Ask the user to sign in at app.stocktrak.com "
        "(tick 'Remember me'), then retry once. Do not loop."
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
        return await call_browser("hedgehog_request", args, ctx, timeout_ms=timeout_ms)
    except BrowserToolError as exc:
        raise StockTrakError(exc.kind, str(exc)) from None


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

_TIME_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
                 "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%Y-%m-%d")
# Fill timestamps can be a little earlier than the order's (separate clocks).
_FILL_SLOP_S = 120

def parse_st_time(value: Any) -> tuple[datetime, bool] | None:
    """StockTrak date/time text → (naive ET datetime, has_time). None if unparseable."""
    s = " ".join(str(value or "").replace("\n", " ").split())
    s = re.sub(r"\s*\b(ET|EST|EDT)\b\s*$", "", s, flags=re.I).split(".")[0]
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

def attach_fills(orders: list[dict[str, Any]], txs: list[dict[str, Any]]) -> int:
    """Add filled_qty / fill_price / fill_time / partial to 'Filled' orders, in place.

    Each fill is used once; orders are matched oldest first to the first unused
    fill of the same symbol and side at or after the order time. Returns the
    number of orders matched."""
    fills = []
    for t in txs:
        side = side_of(t.get("TransactionType"))
        when = parse_st_time(t.get("CreateDate"))
        q = num(t.get("Quantity"))
        if side and when and q:
            fills.append({"symbol": str(t.get("Symbol") or "").upper(), "side": side, "at": when,
                          "qty": abs(q), "price": num(t.get("Price")), "time": t.get("CreateDate"), "used": False})
    fills.sort(key=lambda f: f["at"][0])

    filled = [o for o in orders if "fill" in str(o.get("status") or "").lower()]
    timed = [(parse_st_time(o.get("date")), i, o) for i, o in enumerate(filled)]
    # Unparseable order times go last; page order is newest first, so reverse.
    timed.sort(key=lambda x: (x[0] is None, x[0][0] if x[0] else datetime.max, -x[1]))
    matched = 0
    for when, _, o in timed:
        sym, side = str(o.get("symbol") or "").upper(), side_of(o.get("order"))
        ordered = num(o.get("quantity"))
        hit = None
        for f in fills:
            if f["used"] or f["symbol"] != sym or f["side"] != side:
                continue
            if when:
                (o_at, o_has_t), (f_at, f_has_t) = when, f["at"]
                if o_has_t and f_has_t:
                    if (f_at - o_at).total_seconds() < -_FILL_SLOP_S:
                        continue
                elif f_at.date() < o_at.date():
                    continue
            hit = f
            break
        if hit is None:
            o.update(filled_qty=None, partial=None, fill_match="not_found")
            continue
        hit["used"] = True
        matched += 1
        fq = int(hit["qty"]) if float(hit["qty"]).is_integer() else hit["qty"]
        o.update(filled_qty=fq, fill_price=hit["price"], fill_time=hit["time"],
                 partial=bool(ordered is not None and fq < abs(ordered)), fill_match="symbol+side+time")
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
