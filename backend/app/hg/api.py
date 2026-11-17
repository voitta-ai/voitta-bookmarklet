"""The hg.voitta.ai app — everything that host serves, and nothing else.

* ``/``                 landing page with the bookmarklet
* ``/hg/panel.js``      the panel bundle the bookmarklet loads into app.stocktrak.com
* ``/hg/keys`` …        create / regenerate a key, read and change its settings, its activity
* ``/hg/link``          WebSocket: the panel's connection (app.hg.link)
* ``/v1/…``             REST, shaped like a broker API (Bearer key)
* ``/mcp``              MCP with the Hedgehog tools only (app.hg.mcp)
* ``/docs``, ``/openapi.json``

No cookies are read or set anywhere here; the key is sent explicitly (``Authorization: Bearer``),
so cross-site requests can't act for anyone (CORS is open for that reason).
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, Path as P, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app.hg import link, runner, store

# ── errors: one envelope everywhere ──────────────────────────────────────

_STATUS = {
    "unauthorized": 401, "trading_disabled": 403, "not_allowed": 403, "not_found": 404,
    "tab_offline": 503, "tab_unreachable": 503, "network_error": 503,
    "session_expired": 409, "site_check": 409,
    "rate_limited": 429,
    "rule_violation": 422, "bad_args": 422, "preview_required": 422, "preview_stale": 422,
    "rejected_by_stocktrak": 422, "declined": 422,
    "unexpected_page": 502, "server_error_page": 502, "stocktrak_error": 502, "bad_json": 502,
    "internal_error": 500,
}
_RETRYABLE = {"tab_offline", "tab_unreachable", "network_error", "rate_limited"}


def _err(code: str, message: str, hint: str | None = None, status: int | None = None) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "retryable": code in _RETRYABLE}}
    if hint:
        body["error"]["hint"] = hint
    return JSONResponse(body, status_code=status or _STATUS.get(code, 400))


def _from_tool(res: dict[str, Any]) -> JSONResponse | dict[str, Any]:
    if res.get("ok") is False:
        return _err(str(res.get("error") or "error"), str(res.get("message") or res.get("error") or ""),
                    res.get("hint"))
    out = {k: v for k, v in res.items() if k != "ok"}
    return out


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip()[:64] if fwd else (request.client.host if request.client else None)


def _key_row(request: Request) -> dict | None:
    from app.services.mcp_auth import bearer

    return store.lookup(bearer(request.headers))


_UNAUTH = ("Send 'Authorization: Bearer <key>'. Your key is in the Hedgehog panel on app.stocktrak.com "
           "(open it with the bookmarklet from hg.voitta.ai).")


async def _tool(request: Request, name: str, args: dict[str, Any]):
    row = _key_row(request)
    if row is None:
        return _err("unauthorized", _UNAUTH)
    return _from_tool(await runner.run(row, name, args, via="rest"))


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


# ── app ──────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Hedgehog API",
    version="1",
    description=(
        "Trade and read your Hedgehog Camp (StockTrak) account from scripts, Claude Code and other tools.\n\n"
        "**How it works:** open app.stocktrak.com, click the Hedgehog bookmarklet from "
        "[hg.voitta.ai](/). The panel shows your API key. Every call runs in that StockTrak tab, so keep "
        "it open and logged in.\n\n"
        "**Auth:** `Authorization: Bearer hgk_…`.\n\n"
        "**Errors** are always `{\"error\": {\"code\", \"message\", \"hint\"?, \"retryable\"}}`. "
        "`tab_offline` (503): no StockTrak tab connected. `session_expired` (409): StockTrak logged you out."
    ),
    docs_url="/docs",
    redoc_url=None,
    openapi_tags=[
        {"name": "status", "description": "Is your StockTrak tab connected?"},
        {"name": "account", "description": "Account, positions, orders, history."},
        {"name": "trading", "description": "Place, change and cancel orders (needs 'Allow trading' on)."},
        {"name": "market", "description": "Quotes, option chains, symbol search, watchlist."},
        {"name": "game", "description": "Hedgehog Camp ranking and H-Token rewards."},
    ],
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"],
                   allow_headers=["*"], max_age=600)


@app.exception_handler(404)
async def _not_found(request: Request, exc) -> JSONResponse:
    return _err("not_found", f"{request.url.path} doesn't exist on the Hedgehog API.", status=404)


@app.exception_handler(405)
async def _not_allowed(request: Request, exc) -> JSONResponse:
    return _err("not_found", f"{request.method} {request.url.path} doesn't exist on the Hedgehog API.", status=405)


# ── panel: keys, settings, activity, link, bundle ────────────────────────


class Settings(BaseModel):
    trading: bool | None = Field(None, description="Allow placing and changing orders through the API.")
    keepalive: bool | None = Field(None, description="Keep the StockTrak session alive while the panel is open.")


def _me(row: dict) -> dict[str, Any]:
    return {**store.public(row), "tab": link.status(row["id"])}


@app.post("/hg/keys", include_in_schema=False)
async def mint_key(request: Request):
    try:
        key, info = store.mint(_client_ip(request))
    except store.HgError as exc:
        return _err("rate_limited", str(exc))
    return {"key": key, **info}


@app.post("/hg/keys/regenerate", include_in_schema=False)
async def regenerate_key(request: Request):
    from app.services.mcp_auth import bearer

    old = bearer(request.headers)
    row = store.lookup(old)
    if row is None:
        return _err("unauthorized", _UNAUTH)
    key, info = store.regenerate(old, _client_ip(request))
    await link.retire(row["id"])  # tabs on the old key stop receiving calls now and are told to reconnect
    return {"key": key, **info}


@app.get("/hg/me", include_in_schema=False)
async def me(request: Request):
    row = _key_row(request)
    return _me(row) if row else _err("unauthorized", _UNAUTH)


@app.patch("/hg/settings", include_in_schema=False)
async def settings(request: Request, body: Settings):
    row = _key_row(request)
    if row is None:
        return _err("unauthorized", _UNAUTH)
    store.update_settings(row["id"], trading=body.trading, keepalive=body.keepalive)
    return _me(store.get(row["id"]))


@app.get("/hg/activity", include_in_schema=False)
async def activity(request: Request, limit: int = 30):
    row = _key_row(request)
    return {"activity": store.activity(row["id"], limit)} if row else _err("unauthorized", _UNAUTH)


@app.websocket("/hg/link")
async def ws_link(ws: WebSocket):
    await link.serve(ws)


_PANEL_JS: Path | None = None


def _panel_path() -> Path:
    from app import config

    return Path(config.FRONTEND_DIST) / "hg-panel.js"


@app.get("/hg/panel.js", include_in_schema=False)
async def panel_js():
    p = _panel_path()
    if not p.is_file():
        return Response("console.error('Hedgehog panel not built');", media_type="application/javascript",
                        status_code=503)
    return Response(p.read_bytes(), media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})


# ── /v1: status ──────────────────────────────────────────────────────────


@app.get("/v1/status", tags=["status"], summary="Your StockTrak tab: connected? logged in?")
async def v1_status(request: Request, check: bool = Query(True, description="Also ask the tab whether StockTrak is logged in.")):
    row = _key_row(request)
    if row is None:
        return _err("unauthorized", _UNAUTH)
    out: dict[str, Any] = {"key": store.public(row), "tab": link.status(row["id"])}
    if check and out["tab"]["connected"]:
        res = await runner.run(row, "hedgehog_page_context", {}, via="rest")
        out["stocktrak"] = ({k: res.get(k) for k in ("signed_in", "username", "game_name", "page_type", "url")}
                            if res.get("ok") is not False else {"error": res.get("error")})
    return out


@app.get("/v1/clock", tags=["status"], summary="Market clock (StockTrak's server time).")
async def v1_clock(request: Request):
    return await _tool(request, "hedgehog_clock", {})


# ── /v1: account ─────────────────────────────────────────────────────────


@app.get("/v1/account", tags=["account"], summary="Cash, buying power, portfolio value, trades left.")
async def v1_account(request: Request):
    return await _tool(request, "hedgehog_account", {})


@app.get("/v1/positions", tags=["account"], summary="Open positions.")
async def v1_positions(request: Request, security_type: Literal["equity", "option"] = "equity"):
    return await _tool(request, "hedgehog_positions", {"security_type": security_type})


@app.get("/v1/orders", tags=["account"], summary="Open orders, or order history.")
async def v1_orders(request: Request,
                    view: Literal["open", "history"] = "open",
                    start_date: str | None = Query(None, description="MM/DD/YYYY (history)"),
                    end_date: str | None = Query(None, description="MM/DD/YYYY (history)"),
                    status: str | None = None, page: int | None = None, page_size: int | None = None):
    return await _tool(request, "hedgehog_orders", _clean({"view": view, "start_date": start_date,
                                                            "end_date": end_date, "status": status,
                                                            "page": page, "page_size": page_size}))


@app.get("/v1/history/{report}", tags=["account"], summary="Transactions, trade notes, closed positions… (CSV-backed).")
async def v1_history(request: Request,
                     report: Literal["transactions", "trade_notes", "closed_positions", "open_positions",
                                     "historical_open_positions", "portfolio_summary"],
                     start_date: str | None = None, end_date: str | None = None, date: str | None = None,
                     security_type: Literal["equity", "option"] | None = None, limit: int | None = None):
    return await _tool(request, "hedgehog_history", _clean({"report": report, "start_date": start_date,
                                                             "end_date": end_date, "date": date,
                                                             "security_type": security_type, "limit": limit}))


# ── /v1: trading ─────────────────────────────────────────────────────────


class OrderIn(BaseModel):
    symbol: str = Field(..., description="Ticker (the underlying for options), e.g. F")
    side: Literal["buy", "sell", "short", "cover", "buy_to_open", "sell_to_close", "sell_to_open", "buy_to_close"]
    quantity: int = Field(..., ge=1, description="Shares, or contracts for options")
    trade_note: str = Field(..., min_length=1, description="Required by the game: why you're making this trade")
    order_type: Literal["market", "limit", "stop", "trailing_stop_pct"] = "market"
    price: float | None = Field(None, description="Limit/stop price")
    trailing_pct: float | None = Field(None, description="Trailing stop distance in percent")
    duration: Literal["day", "gtc", "gtd"] = "day"
    gtd_date: str | None = Field(None, description="MM/DD/YYYY when duration=gtd")
    security_type: Literal["equity", "option"] = "equity"
    option_type: Literal["call", "put"] | None = None
    expiration: str | None = Field(None, description="Option expiration, MM/DD/YYYY")
    strike: float | None = None


class SpreadIn(BaseModel):
    spread_type: int = Field(..., description="1–22, see /v1/options/spread-template")
    symbol: str
    legs: list[dict[str, Any]] = Field(..., description="From /v1/options/spread-template")
    trade_note: str = Field(..., min_length=1)
    expiration: str | None = None


async def _preview_then_place(request: Request, preview_tool: str, args: dict[str, Any], place: bool):
    row = _key_row(request)
    if row is None:
        return _err("unauthorized", _UNAUTH)
    preview = await runner.run(row, preview_tool, args, via="rest")
    if preview.get("ok") is False or not place:
        return _from_tool(preview)
    placed = await runner.run(row, "hedgehog_place_order", {"preview_id": preview.get("preview_id")}, via="rest")
    out = _from_tool(placed)
    if isinstance(out, dict):
        out = {**out, "preview": {k: preview.get(k) for k in ("summary", "estimated_cost", "warnings")}}
    return out


@app.post("/v1/orders", tags=["trading"], summary="Place an order (previews it, then places it).")
async def v1_place(request: Request, order: OrderIn):
    return await _preview_then_place(request, "hedgehog_preview_order", order.model_dump(exclude_none=True), True)


@app.post("/v1/orders/preview", tags=["trading"], summary="Preview an order without placing it.")
async def v1_preview(request: Request, order: OrderIn):
    return await _preview_then_place(request, "hedgehog_preview_order", order.model_dump(exclude_none=True), False)


@app.post("/v1/spreads", tags=["trading"], summary="Place an option spread (previews it, then places it).")
async def v1_spread(request: Request, spread: SpreadIn):
    return await _preview_then_place(request, "hedgehog_preview_spread", spread.model_dump(exclude_none=True), True)


@app.post("/v1/spreads/preview", tags=["trading"], summary="Preview an option spread.")
async def v1_spread_preview(request: Request, spread: SpreadIn):
    return await _preview_then_place(request, "hedgehog_preview_spread", spread.model_dump(exclude_none=True), False)


@app.delete("/v1/orders/{order_id}", tags=["trading"], summary="Cancel an open order.")
async def v1_cancel(request: Request, order_id: str):
    return await _tool(request, "hedgehog_cancel_order", {"order_id": order_id})


class EditIn(BaseModel):
    price: float
    gtd_date: str | None = None


@app.patch("/v1/orders/{order_conf}", tags=["trading"], summary="Change an open order's price.")
async def v1_edit(request: Request, order_conf: str, body: EditIn):
    return await _tool(request, "hedgehog_edit_order", _clean({"order_conf": order_conf, **body.model_dump()}))


@app.post("/v1/orders/{order_conf}/notes", tags=["trading"], summary="Add a trade note to an order.")
async def v1_note(request: Request, order_conf: str, note: str = Body(..., embed=True)):
    return await _tool(request, "hedgehog_add_trade_note", {"order_conf": order_conf, "note": note})


class ExerciseIn(BaseModel):
    security_id: str
    underlying_security_id: str
    option_symbol: str
    quantity: int | None = None
    allow_out_of_the_money: bool | None = None


@app.post("/v1/options/exercise", tags=["trading"], summary="Exercise an option position.")
async def v1_exercise(request: Request, body: ExerciseIn):
    return await _tool(request, "hedgehog_exercise_option", body.model_dump(exclude_none=True))


# ── /v1: market ──────────────────────────────────────────────────────────


@app.get("/v1/quotes/{symbol}", tags=["market"], summary="Last price and quote.")
async def v1_quote(request: Request, symbol: str):
    return await _tool(request, "hedgehog_quote", {"symbol": symbol})


@app.get("/v1/options/chain", tags=["market"], summary="Option chain for a symbol.")
async def v1_chain(request: Request, symbol: str, expiration: str | None = None,
                   option_type: Literal["call", "put"] | None = None, strike: float | None = None):
    return await _tool(request, "hedgehog_option_chain", _clean({"symbol": symbol, "expiration": expiration,
                                                                  "option_type": option_type, "strike": strike}))


@app.get("/v1/options/spread-template", tags=["market"], summary="Leg template for spread strategy 1–22.")
async def v1_spread_template(request: Request, spread_type: int, symbol: str, quantity: int | None = None):
    return await _tool(request, "hedgehog_spread_template", _clean({"spread_type": spread_type, "symbol": symbol,
                                                                     "quantity": quantity}))


@app.get("/v1/symbols", tags=["market"], summary="Search symbols by name or ticker.")
async def v1_symbols(request: Request, q: str, limit: int | None = None):
    return await _tool(request, "hedgehog_symbol_search", _clean({"query": q, "limit": limit}))


@app.get("/v1/watchlist", tags=["market"], summary="Your StockTrak watchlist.")
async def v1_watchlist(request: Request):
    return await _tool(request, "hedgehog_watchlist", {"action": "list"})


@app.post("/v1/watchlist", tags=["market"], summary="Add symbols to the watchlist.")
async def v1_watchlist_add(request: Request, symbols: list[str] = Body(..., embed=True)):
    return await _tool(request, "hedgehog_watchlist", {"action": "add", "symbols": symbols})


@app.delete("/v1/watchlist/{security_id}", tags=["market"], summary="Remove a watchlist entry.")
async def v1_watchlist_remove(request: Request, security_id: str):
    return await _tool(request, "hedgehog_watchlist", {"action": "remove", "security_id": security_id})


@app.get("/v1/stocktrak/raw", tags=["market"], summary="Escape hatch: any read-only StockTrak endpoint on the allowlist.")
async def v1_raw(request: Request, path: str, method: Literal["GET", "POST"] = "GET",
                 params: str | None = Query(None, description="JSON object of parameters")):
    try:
        p = json.loads(params) if params else {}
    except ValueError:
        return _err("bad_args", "params must be a JSON object")
    return await _tool(request, "hedgehog_api_read", {"path": path, "method": method, "params": p})


# ── /v1: game ────────────────────────────────────────────────────────────


@app.get("/v1/rankings/me", tags=["game"], summary="Your StockTrak rank in the game.")
async def v1_rank(request: Request):
    return await _tool(request, "hedgehog_my_rank", {})


@app.get("/v1/rewards/{section}", tags=["game"], summary="Hedgehog Camp H-Token rewards.")
async def v1_rewards(request: Request, section: Literal["my_standing", "my_week", "weeks", "config"],
                     username: str | None = None, week_id: int | None = None, limit: int | None = None):
    return await _tool(request, "hedgehog_rewards", _clean({"section": section, "username": username,
                                                             "week_id": week_id, "limit": limit}))


# ── landing ──────────────────────────────────────────────────────────────


def _public_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{request.headers.get('host') or request.url.netloc}"


def bookmarklet(base: str) -> str:
    js = ("(()=>{if(window.__voittaHg){window.__voittaHg.show();return}"
          f"fetch('{base}/hg/panel.js').then(r=>r.text()).then(c=>{{window.__voittaHgBase='{base}';(0,eval)(c)}})"
          ".catch(e=>alert('Hedgehog panel failed to load: '+e))})()")
    return "javascript:" + js


@app.get("/", include_in_schema=False)
async def landing(request: Request):
    base = _public_base(request)
    bm = html.escape(bookmarklet(base), quote=True)
    doc = f"""<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Hedgehog API</title>
<style>
:root{{--bg:#fff;--fg:#1a1d23;--muted:#5f6673;--border:#d9dde3;--accent:#2563eb;--card:#f7f8fa}}
@media (prefers-color-scheme:dark){{:root{{--bg:#14171c;--fg:#e8eaee;--muted:#9aa1ad;--border:#2c323b;--accent:#3b82f6;--card:#1b1f26}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,sans-serif}}
main{{max-width:680px;margin:0 auto;padding:48px 20px}}
h1{{font-size:28px;margin:0 0 6px}} .muted{{color:var(--muted)}}
.bm{{display:inline-block;margin:18px 0 6px;padding:12px 22px;border-radius:10px;background:var(--accent);color:#fff;
  font-weight:700;text-decoration:none;cursor:grab}}
ol{{padding-left:22px}} li{{margin:6px 0}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-top:22px}}
code{{font-family:ui-monospace,Menlo,monospace;font-size:13.5px}} a{{color:var(--accent)}}
</style>
<main>
<h1>Hedgehog API</h1>
<p class=muted>Trade and read your Hedgehog Camp account from Claude Code, scripts and other tools.</p>
<a class=bm href="{bm}" onclick="event.preventDefault();alert('Drag this button to your bookmarks bar, then click it on app.stocktrak.com.')">🦔 Hedgehog</a>
<ol>
<li>Drag the <b>🦔 Hedgehog</b> button to your browser's bookmarks bar.</li>
<li>Open <a href="https://app.stocktrak.com">app.stocktrak.com</a> and log in.</li>
<li>Click the bookmark. The panel shows your <b>API key</b> and how to connect Claude Code, Claude Desktop or any REST client.</li>
<li>Keep that StockTrak tab open: every API call runs in it.</li>
</ol>
<div class=card>
<b>Docs:</b> <a href="/docs">REST API reference</a> · MCP endpoint <code>{html.escape(base)}/mcp/</code><br>
<span class=muted>No sign-up: your key lives in your browser. Trading through the API is on by default; switch it off in the panel.</span>
</div>
</main></html>"""
    return HTMLResponse(doc, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                      "Content-Security-Policy": "frame-ancestors 'none'"})
