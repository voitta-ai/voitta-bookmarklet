"""Read-only tools: page context, account, positions, orders, history, market data,
rankings, CSV exports into python_storage, and Hedgehog Camp rewards.

All StockTrak calls go through the user's own tab (see _core.st_call).
None of these tools changes the account; the trading tools live in trading.py.
"""

from __future__ import annotations

from typing import Any

from app.services import python_storage
from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry

from voitta_hedgehog import rewards
from voitta_hedgehog._core import (
    StockTrakError, body_of, csv_records, el_attr, fail, rows_of, st_call, text_of, today_et,
)
from voitta_hedgehog.rules import SPREAD_TYPES

_NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}


def _reg(name: str, description: str, schema: dict[str, Any], handler: Any, timeout_s: float | None = None) -> None:
    registry.register(ToolSpec(name=name, description=description, input_schema=schema, handler=handler,
                               side="hybrid", timeout_s=timeout_s))


def _guard(fn: Any) -> Any:
    """Turn StockTrakError / BrowserToolError / ValueError into the standard envelope."""
    async def wrapped(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
        try:
            return await fn(args or {}, ctx)
        except StockTrakError as exc:
            return fail(exc.kind, str(exc))
        except BrowserToolError as exc:
            return fail(exc.kind, str(exc))
        except ValueError as exc:
            return fail("bad_args", str(exc))
    wrapped.__name__ = fn.__name__
    return wrapped


# ---- page context ----------------------------------------------------------

@_guard
async def _page_context(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    info = await call_browser("hedgehog_inspect_page", {"resolve_ids": True}, ctx)
    out: dict[str, Any] = {"ok": True, **info}
    if info.get("on_stocktrak") and info.get("signed_in"):
        try:
            m = body_of(await st_call(ctx, "POST", "/trading/ismarketopen", {"tab": "NorthAmerica", "exchange": "US"}))
            out["market_open"] = m.get("IsOpen")
            out["market_message"] = m.get("Message")
        except StockTrakError as exc:
            out["market_open_error"] = exc.kind
    return out

_reg("hedgehog_page_context",
     "Call this FIRST for any Hedgehog Camp / StockTrak task. Returns {on_stocktrak, page_type, signed_in, "
     "username, account_id, tournament_id, market_open}. If signed_in is false, ask the user to sign in at "
     "app.stocktrak.com (tick 'Remember me') before anything else.",
     _NO_ARGS, _page_context)


# ---- account ---------------------------------------------------------------

def _summary_pairs(rows: list[list[str]]) -> dict[str, str]:
    """ExportToExcelPortfolioSummary is label/value pairs laid across columns."""
    out: dict[str, str] = {}
    for r in rows:
        for i in range(0, len(r) - 1, 2):
            k, v = r[i].strip(), r[i + 1].strip()
            if k.endswith(":") and k[:-1] not in ("First Name", "Last Name"):
                out[k[:-1]] = v
    return out


@_guard
async def _account(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    csv = await st_call(ctx, "GET", "/account/ExportToExcelPortfolioSummary", {"date": today_et()})
    summary = _summary_pairs(csv.get("rows") or [])
    tm = summary.get("Trades Made/Allowed", "")
    made, _, allowed = tm.partition("/")
    return {"ok": True, "summary": summary,
            "trades_made": int(made) if made.isdigit() else None,
            "trades_remaining": int(allowed) if allowed.isdigit() else None,
            "note": "Same-day fill prices can show as $XXX.xx in the web views; this CSV has real values."}

_reg("hedgehog_account",
     "Portfolio summary from StockTrak's own CSV export: cash, market value of long/short positions, portfolio "
     "value, % return, buying power, trades made / remaining. Use this for 'how am I doing'.",
     _NO_ARGS, _account)


# ---- positions -------------------------------------------------------------

@_guard
async def _positions(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    stype = {"equity": "Equities", "option": "Options"}.get(str(args.get("security_type") or "equity"), "Equities")
    html = body_of(await st_call(ctx, "GET", "/account/openpositionsbysecuritytypeV2", {
        "securityType": stype, "pageIndex": 1, "pageSize": 100, "sortField": "CreateDate", "sortDirection": "DESC"}))
    ids = {}
    for row in rows_of(html):
        d = row.get("data") or {}
        sym = d.get("data-option-symbol")
        if sym:
            ids[sym] = {"security_id": d.get("data-securityid"), "underlying_security_id": d.get("data-underlying-securityid"),
                        "quantity": d.get("data-quantity"), "company": d.get("data-company")}
    try:
        csv = await st_call(ctx, "GET", "/admin/ExportOpenPositions/", {"securityType": stype})
        recs = csv_records(csv.get("rows") or [])
    except StockTrakError:
        recs = []
    for r in recs:
        r.update({k: v for k, v in (ids.get(r.get("Symbol", "")) or {}).items() if v})
    if not recs:  # CSV empty: fall back to ids only
        recs = [{"Symbol": s, **{k: v for k, v in i.items() if v}} for s, i in ids.items()]
    return {"ok": True, "security_type": stype, "count": len(recs), "positions": recs,
            "note": "security_id / underlying_security_id are what hedgehog_exercise_option needs."}

_reg("hedgehog_positions",
     "Open positions for one security type (equity | option): symbol, quantity, last price, price paid, P&L, "
     "market value (from the CSV export), plus the ids the trading tools need. Short positions can take a few "
     "seconds to appear after a fill.",
     {"type": "object", "properties": {"security_type": {"type": "string", "enum": ["equity", "option"], "default": "equity"}},
      "additionalProperties": False}, _positions)


# ---- orders ----------------------------------------------------------------

@_guard
async def _orders(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    view = str(args.get("view") or "open")
    if view == "open":
        rows = rows_of(body_of(await st_call(ctx, "GET", "/account/openorders", {"v2": "true"})))
        out = []
        for r in rows:
            c = r["cells"]
            if len(c) < 10:
                continue
            out.append({"order_id": el_attr(r, "cancelorder", "data-status") or (r.get("data") or {}).get("data-status"),
                        "date": c[0].replace("Cancel", "").strip(), "order": c[1], "symbol": c[2], "quantity": c[3],
                        "price": c[4], "type": c[6], "status": c[8], "expires": c[9]})
        return {"ok": True, "view": "open", "orders": out,
                "note": "order_id is what hedgehog_cancel_order takes. A spread's legs cancel together."}
    params = {"pageIndex": int(args.get("page") or 1), "pageSize": int(args.get("page_size") or 50),
              "startDate": args.get("start_date") or "", "endDate": args.get("end_date") or "",
              "sortField": "CreateDate", "sortDirection": "DESC", "status": args.get("status") or "", "v2": "true"}
    rows = rows_of(body_of(await st_call(ctx, "GET", "/account/getorderhistory", params)))
    out = []
    for r in rows:
        c = r["cells"]
        if len(c) < 6:
            continue
        out.append({"order_id": el_attr(r, "edit-order", "data-order-id"),
                    "order_conf": el_attr(r, "edit-order", "data-order-conf") or el_attr(r, "view-trade-notes", "data-order-id"),
                    "date": c[1], "order": c[2], "symbol": c[3], "quantity": c[4], "price": c[5],
                    "status": c[9] if len(c) > 9 else None})
    return {"ok": True, "view": "history", "orders": out,
            "note": "order_conf is what hedgehog_edit_order / hedgehog_add_trade_note take. "
                    "The 8-char confirmation shown to users is its last 8 hex chars, uppercased."}

_reg("hedgehog_orders",
     "Orders. view='open' → pending orders with order_id (for cancel). view='history' → all orders with status "
     "(Open, Filled, UserCancelled, …) and order_conf (for edit / trade notes); optional start_date/end_date "
     "(MM/DD/YYYY), status, page, page_size.",
     {"type": "object", "properties": {
         "view": {"type": "string", "enum": ["open", "history"], "default": "open"},
         "start_date": {"type": "string"}, "end_date": {"type": "string"}, "status": {"type": "string"},
         "page": {"type": "integer", "minimum": 1}, "page_size": {"type": "integer", "minimum": 1, "maximum": 200}},
      "additionalProperties": False}, _orders)


# ---- history (CSV exports) -------------------------------------------------

_REPORTS = {
    "transactions": ("/admin/ExportTransactionHistory", True),
    "closed_positions": ("/account/exportclosepositions", False),
    "open_positions": ("/admin/ExportOpenPositions/", False),
    "portfolio_summary": ("/account/ExportToExcelPortfolioSummary", False),
    "trade_notes": ("/account/ExportTradeNotes", True),
    "historical_open_positions": ("/account/ExportToExcelHistoricalOP", False),
}


async def _report(ctx: ToolCtx, report: str, args: dict[str, Any]) -> dict[str, Any]:
    if report not in _REPORTS:
        raise ValueError(f"report must be one of {sorted(_REPORTS)}")
    path, ranged = _REPORTS[report]
    params: dict[str, Any] = {}
    if ranged:
        params = {"from": args.get("start_date") or "1/1/2026", "to": args.get("end_date") or today_et()}
    elif report == "open_positions":
        params = {"securityType": {"option": "Options"}.get(str(args.get("security_type")), "Equities")}
    elif report in ("portfolio_summary", "historical_open_positions"):
        params = {"date": args.get("date") or today_et()}
    return await st_call(ctx, "GET", path, params)


@_guard
async def _history(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    report = str(args.get("report") or "transactions")
    res = await _report(ctx, report, args)
    recs = csv_records(res.get("rows") or [])
    limit = int(args.get("limit") or 200)
    return {"ok": True, "report": report, "count": len(recs), "rows": recs[:limit], "truncated": len(recs) > limit}

_reg("hedgehog_history",
     "Structured history from StockTrak's CSV exports (real prices even for same-day fills). report: "
     "transactions (start_date/end_date MM/DD/YYYY) | closed_positions (realized gains: one row per side, P&L on "
     "the closing row) | open_positions (security_type) | portfolio_summary | trade_notes | "
     "historical_open_positions (date). For analysis in Python, use hedgehog_export_csv instead.",
     {"type": "object", "properties": {
         "report": {"type": "string", "enum": sorted(_REPORTS), "default": "transactions"},
         "start_date": {"type": "string"}, "end_date": {"type": "string"}, "date": {"type": "string"},
         "security_type": {"type": "string", "enum": ["equity", "option"]},
         "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200}},
      "additionalProperties": False}, _history)


@_guard
async def _export_csv(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    report = str(args.get("report") or "transactions")
    res = await _report(ctx, report, args)
    rows = res.get("rows") or []
    snap = python_storage.put(
        kind="hedgehog_csv",
        response_body={"report": report, "columns": rows[0] if rows else [], "rows": rows[1:], "csv_text": res.get("csv_text")},
        meta={"origin": python_storage.make_origin(source="stocktrak", path=report, host="app.stocktrak.com",
                                                    extra={"filename": res.get("filename")})},
    )
    return {"ok": True, "report": report, "handle": snap["handle"], "rows": max(0, len(rows) - 1),
            "columns": rows[0] if rows else []}

_reg("hedgehog_export_csv",
     "Save a StockTrak CSV export into python_storage for analysis/reports (same reports and arguments as "
     "hedgehog_history). Returns a python_storage handle.",
     {"type": "object", "properties": {
         "report": {"type": "string", "enum": sorted(_REPORTS), "default": "transactions"},
         "start_date": {"type": "string"}, "end_date": {"type": "string"}, "date": {"type": "string"},
         "security_type": {"type": "string", "enum": ["equity", "option"]}},
      "additionalProperties": False}, _export_csv)


# ---- market data -----------------------------------------------------------

@_guard
async def _quote(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    sym = str(args.get("symbol") or "").strip().upper()
    if not sym:
        raise ValueError("symbol required")
    b = body_of(await st_call(ctx, "POST", "/trading/getpricev2", {
        "symbol": sym, "exchange": "US", "securityType": "Equities", "monthCode": "", "year": "",
        "equitiesType": "All", "isOptionSpreads": "false"}))
    if not b.get("Success"):
        return fail("not_found", b.get("ErrorMessage") or f"no price for {sym}")
    keep = ("Symbol", "CompanyName", "LastPrice", "Currency", "Exchange", "TradingHalted")
    out = {"ok": True, **{k: b.get(k) for k in keep}}
    out["quote_text"] = text_of(b, "symbolPrice")[:600]
    out["note"] = "StockTrak prices are delayed (Hedgehog Camp rules: 15 min)."
    return out

_reg("hedgehog_quote",
     "Delayed last price + quote panel for a US stock/ETF symbol (the price StockTrak will fill against).",
     {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"], "additionalProperties": False},
     _quote)


@_guard
async def _symbol_search(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    q = str(args.get("query") or "").strip()
    if not q:
        raise ValueError("query required")
    res = await st_call(ctx, "GET", "/quotes/FullTextSeachSymbol", {
        "quote": q, "exchanges": "US", "securityTypes": "Equities", "equitiesType": "All"})
    hits = res.get("body") if isinstance(res.get("body"), list) else []
    return {"ok": True, "query": q, "results": hits[: int(args.get("limit") or 20)]}

_reg("hedgehog_symbol_search",
     "Find US stock/ETF symbols by name or ticker fragment. Returns [{Symbol, Company, SecurityType, Market}].",
     {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
      "required": ["query"], "additionalProperties": False}, _symbol_search)


def _opts(body: dict[str, Any]) -> list[str]:
    frag = body.get("Html")
    return [o["value"] for o in (frag.get("options") or [])] if isinstance(frag, dict) else []


@_guard
async def _option_chain(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    sym = str(args.get("symbol") or "").strip().upper()
    if not sym:
        raise ValueError("symbol required")
    ctype = "Put" if str(args.get("option_type") or "call").lower() == "put" else "Call"
    exp = args.get("expiration")
    strike = args.get("strike")
    exps = _opts(body_of(await st_call(ctx, "POST", "/trading/getoptionexpiry", {"symbol": sym})))
    out: dict[str, Any] = {"ok": True, "symbol": sym, "expirations": exps}
    if not exp:
        out["next"] = "Pass expiration (one of expirations) to list strikes."
        return out
    out["option_type"] = ctype
    out["expiration"] = exp
    out["strikes"] = _opts(body_of(await st_call(ctx, "POST", "/trading/getstrikeprice", {
        "symbol": sym, "optionType": ctype, "expiration": exp, "isFullList": "false"})))
    if strike is not None:
        b = body_of(await st_call(ctx, "POST", "/trading/getoptionpricev2", {
            "symbol": sym, "exchange": "US", "optionType": ctype, "expiration": exp, "strikePrice": strike}))
        frag = b.get("Html") if isinstance(b.get("Html"), dict) else {}
        out["quote"] = frag.get("fields") or {}
        out["quote_text"] = frag.get("text", "")[:800]
    return out

_reg("hedgehog_option_chain",
     "Option chain, step by step: symbol → expirations; + expiration (MM/DD/YYYY) → strikes for option_type "
     "call|put; + strike → that contract's quote incl. OptionSymbol (e.g. F2620K12.5). Contracts are 100 shares.",
     {"type": "object", "properties": {
         "symbol": {"type": "string"}, "expiration": {"type": "string"},
         "option_type": {"type": "string", "enum": ["call", "put"], "default": "call"}, "strike": {"type": "number"}},
      "required": ["symbol"], "additionalProperties": False}, _option_chain)


@_guard
async def _spread_template(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    stype = int(args.get("spread_type") or 0)
    if stype not in SPREAD_TYPES:
        raise ValueError("spread_type must be 1–22")
    sym = str(args.get("symbol") or "").strip().upper()
    b = body_of(await st_call(ctx, "POST", "/trading/GetOptionSpreadLegs", {
        "type": stype, "quantity": int(args.get("quantity") or 1), "symbol": sym}))
    frag = b.get("Html") if isinstance(b.get("Html"), dict) else {}
    return {"ok": True, "spread_type": stype, "name": SPREAD_TYPES[stype], "symbol": sym, "legs": frag.get("legs") or [],
            "next": "Fill strike (and expiration) for every leg with needs_strike=true, then hedgehog_preview_spread."}

_reg("hedgehog_spread_template",
     "Leg template for one of the 22 option strategies (1 Covered Call … 6 Bull Call … 9 Long Straddle … "
     "22 Iron Condor): side (Buy/Short), quantity, option_type (Call/Put/'' = stock leg), needs_strike.",
     {"type": "object", "properties": {
         "spread_type": {"type": "integer", "minimum": 1, "maximum": 22}, "symbol": {"type": "string"},
         "quantity": {"type": "integer", "minimum": 1, "default": 1}},
      "required": ["spread_type", "symbol"], "additionalProperties": False}, _spread_template)


# ---- rankings --------------------------------------------------------------

@_guard
async def _my_rank(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    b = body_of(await st_call(ctx, "GET", "/account/getrankingsv2", {
        "pageIndex": 1, "pageSize": 25, "rankingType": "Overall", "date": "", "filterType": ""}))
    rank_txt = text_of(b, "UserRankHtml").strip() or str(b.get("UserRankHtml") or "").strip()
    pcts = []
    for r in rows_of(b):
        for el in r.get("els") or []:
            p = (el.get("data") or {}).get("data-percentage")
            if p is not None:
                pcts.append(p)
                break
    return {"ok": True, "my_rank": rank_txt, "total_pages": b.get("TotalPages"),
            "top_returns_pct": pcts[:10],
            "note": "Own rank only; '0 / N' means not ranked yet (no filled trades). Other players are not listed."}

_reg("hedgehog_my_rank",
     "The user's StockTrak ranking in this session ('rank / players') plus the top-10 return percentages "
     "(no names). For Hedgehog Camp token standing use hedgehog_rewards.",
     _NO_ARGS, _my_rank)


# ---- generic read ----------------------------------------------------------

@_guard
async def _api_get(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    method = str(args.get("method") or "GET").upper()
    res = await st_call(ctx, method, str(args.get("path") or ""), args.get("params") or {})
    return {"ok": True, **res}

_reg("hedgehog_api_read",
     "Escape hatch: call any READ endpoint on the plugin allowlist (docs/03-stocktrak-api.md) with raw params; "
     "HTML fragments come back parsed {headers, rows[{cells, data, els}], options, fields, text}. Write "
     "endpoints are refused here; use the trading tools. Prefer the specific tools above.",
     {"type": "object", "properties": {
         "path": {"type": "string"}, "method": {"type": "string", "enum": ["GET", "POST"], "default": "GET"},
         "params": {"type": "object"}},
      "required": ["path"], "additionalProperties": False}, _api_get)


# ---- watchlist (does not affect the score) ---------------------------------

@_guard
async def _watchlist(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    action = str(args.get("action") or "list")
    if action == "add":
        syms = args.get("symbols") or []
        b = body_of(await st_call(ctx, "POST", "/dashboard/addwatchlist",
                                  {"symbols": ",".join(syms), "exchange": "US"}, write=True))
        return {"ok": bool(b.get("Success", True)), "added": syms}
    if action == "remove":
        b = body_of(await st_call(ctx, "POST", "/dashboard/removewatchlist",
                                  {"securityID": args.get("security_id")}, write=True))
        return {"ok": bool(b.get("Success", True)), "removed": args.get("security_id")}
    b = body_of(await st_call(ctx, "GET", "/dashboard/getwatchlist", {
        "sortField": "Symbol", "sortDirection": "ASC", "pageSize": 50, "currentPageIndex": 1, "isv3": "true"}))
    items = []
    for r in rows_of(b):
        sid = next(((el.get("data") or {}).get(k) for el in r.get("els") or []
                    for k in ("data-securityid", "data-security-id") if (el.get("data") or {}).get(k)), None)
        items.append({"cells": r["cells"], "security_id": sid})
    return {"ok": True, "watchlist": items}

_reg("hedgehog_watchlist",
     "The StockTrak watchlist: action list | add (symbols: [..]) | remove (security_id from list). "
     "Changes only the watchlist, never positions or the score.",
     {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["list", "add", "remove"], "default": "list"},
         "symbols": {"type": "array", "items": {"type": "string"}}, "security_id": {"type": "string"}},
      "additionalProperties": False}, _watchlist)


# ---- Hedgehog Camp rewards -------------------------------------------------

async def _rewards(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    section = str(args.get("section") or "my_standing")
    username = str(args.get("username") or "").strip()
    try:
        if section == "config":
            return {"ok": True, "config": await rewards.config()}
        if section == "weeks":
            return {"ok": True, "weeks": await rewards.weeks(int(args.get("limit") or 100))}
        if not username:
            return fail("bad_args", "username required (hedgehog_page_context returns it)")
        if section == "my_week":
            return {"ok": True, **await rewards.my_week(int(args.get("week_id") or 0), username)}
        return {"ok": True, **await rewards.my_standing(username)}
    except Exception as exc:  # network / upstream
        return fail("rewards_api_error", f"{type(exc).__name__}: {exc}")

_reg("hedgehog_rewards",
     "Hedgehog Camp H-Token data (public Rewards API). section: my_standing (own leaderboard row: token balance, "
     "total return, S, rank) | my_week (week_id: own R, S, H0 and dH = H0 × R × S for that week) | weeks (all "
     "reward weeks, totals) | config (starting balance, chain, dates). Never returns other players.",
     {"type": "object", "properties": {
         "section": {"type": "string", "enum": ["my_standing", "my_week", "weeks", "config"], "default": "my_standing"},
         "username": {"type": "string"}, "week_id": {"type": "integer"}, "limit": {"type": "integer"}},
      "additionalProperties": False}, _rewards)
