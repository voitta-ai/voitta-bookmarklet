"""Trading tools: preview → (user confirmation) → place, plus cancel / edit / notes / exercise.

Guardrails enforced HERE, not just in the prompt:

* Hidden unless Settings → Plugins → Hedgehog → "Enable trading" is on
  (``plugins.hedgehog.trading_enabled``, default off). Every order counts
  toward the user's scored game.
* Game rules the StockTrak server does not enforce (whole shares, trade
  note required, equities/options only) are checked in rules.py.
* Place only accepts a preview_id from a successful server preview of the
  exact same order in this session, younger than PREVIEW_TTL_S.
* Unless ``plugins.hedgehog.skip_order_confirmation`` is on, the tool itself
  asks the user through ask_user_question and only proceeds on an
  explicit "Place" click; the model cannot answer for the user.
* Exercise checks moneyness first (the server has no in-the-money check).
"""

from __future__ import annotations

import time
from typing import Any

from app.tools.registry import ToolCtx, ToolSpec, registry
from app.tools.server import ask_user

from voitta_hedgehog._core import (
    StockTrakError, body_of, confirm_orders, fail, st_call, text_of, trading_enabled,
)
from voitta_hedgehog.rules import (
    RuleError, build_order_form, build_spread_body, fingerprint, option_moneyness, option_type_from_symbol, parse_money,
)

PREVIEW_TTL_S = 900
_previews: dict[str, dict[str, dict[str, Any]]] = {}  # session → preview_id → record

_PLACE, _CANCEL = "Place it", "Don't place"


def _session(ctx: ToolCtx) -> dict[str, dict[str, Any]]:
    return _previews.setdefault(ctx.session_id or "default", {})


def _remember(ctx: ToolCtx, kind: str, payload: dict[str, Any], summary: str) -> str:
    pid = fingerprint(kind, payload)
    store = _session(ctx)
    now = time.time()
    for k in [k for k, v in store.items() if now - v["ts"] > PREVIEW_TTL_S]:
        store.pop(k, None)
    store[pid] = {"kind": kind, "payload": payload, "summary": summary, "ts": now}
    return pid


async def confirm_with_user(ctx: ToolCtx, question: str, header: str = "Order") -> dict[str, Any] | None:
    """Ask the human; return None on an explicit go-ahead, else a refusal envelope."""
    if not confirm_orders():
        return None
    try:
        res = await ask_user._handler({"questions": [{
            "question": question, "header": header,
            "options": [{"label": _PLACE, "description": "Send it to StockTrak now"},
                        {"label": _CANCEL, "description": "Do nothing"}],
        }]}, ctx)
    except Exception as exc:  # no interactive user, a question already pending, …
        return fail("confirmation_unavailable", f"could not ask the user to confirm: {exc}")
    if not res.get("answered"):
        return fail("not_confirmed", "the user did not confirm; nothing was placed", reason=res.get("reason"))
    chosen = res.get("response") or next(iter((res.get("answers") or {}).values()), "")
    if str(chosen).strip() != _PLACE:
        return fail("declined", "the user declined; nothing was placed", answer=chosen)
    return None


def _reg(name: str, description: str, schema: dict[str, Any], handler: Any, timeout_s: float | None = None) -> None:
    registry.register(ToolSpec(name=name, description=description, input_schema=schema, handler=handler,
                               side="hybrid", visibility_check=trading_enabled, timeout_s=timeout_s))


def _guard(fn: Any) -> Any:
    async def wrapped(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
        if not trading_enabled():
            return fail("trading_disabled", "Trading is off. The user can enable it in Settings → Plugins → Hedgehog.")
        try:
            return await fn(args or {}, ctx)
        except RuleError as exc:
            return fail("rule_violation", str(exc))
        except StockTrakError as exc:
            return fail(exc.kind, str(exc))
        except (TypeError, ValueError) as exc:
            return fail("bad_args", str(exc))
    wrapped.__name__ = fn.__name__
    return wrapped


_ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "security_type": {"type": "string", "enum": ["equity", "option"], "default": "equity"},
        "symbol": {"type": "string", "description": "Underlying ticker, e.g. F (also for options)."},
        "side": {"type": "string", "enum": ["buy", "sell", "short", "cover", "buy_to_open", "sell_to_close",
                                            "sell_to_open", "buy_to_close"]},
        "quantity": {"type": "integer", "minimum": 1, "description": "Shares, or contracts (×100) for options."},
        "order_type": {"type": "string", "enum": ["market", "limit", "stop", "trailing_stop_pct"], "default": "market"},
        "price": {"type": "number", "description": "Limit/stop price."},
        "trailing_pct": {"type": "number", "description": "Trailing stop distance in PERCENT."},
        "duration": {"type": "string", "enum": ["day", "gtc", "gtd"], "default": "day"},
        "gtd_date": {"type": "string", "description": "MM/DD/YYYY when duration=gtd."},
        "option_type": {"type": "string", "enum": ["call", "put"]},
        "expiration": {"type": "string", "description": "MM/DD/YYYY (from hedgehog_option_chain)."},
        "strike": {"type": "number"},
        "trade_note": {"type": "string", "description": "Required by the game rules; ≤300 chars. Say WHY."},
    },
    "required": ["symbol", "side", "quantity", "trade_note"],
    "additionalProperties": False,
}


# ---- single orders ---------------------------------------------------------

@_guard
async def _preview_order(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    form, summary = build_order_form(args)
    b = body_of(await st_call(ctx, "POST", "/trading/previeworderv2", form))
    if not b.get("Success"):
        return fail("rejected_by_stocktrak", b.get("ErrorMessage") or "preview rejected", summary=summary)
    est = None
    path = "/trading/getEstimatedCostForOptions" if form["SecurityType"] == "Options" else "/trading/getEstimatedCost"
    try:
        e = body_of(await st_call(ctx, "POST", path, form))
        est = e.get("estimatedcost")
    except StockTrakError:
        pass
    pid = _remember(ctx, "order", form, summary)
    return {"ok": True, "preview_id": pid, "summary": summary, "estimated_cost": est,
            "stocktrak_preview": text_of(b, "OrderInfoHtml")[:1200],
            "next": f"Show the user the summary; then hedgehog_place_order(preview_id='{pid}'). Valid {PREVIEW_TTL_S // 60} min."}

_reg("hedgehog_preview_order",
     "Validate an equity or option order against the game rules and StockTrak's own preview (no order is "
     "placed). Returns preview_id, a plain-English summary, estimated cost and StockTrak's preview text. "
     "Option sides: buy_to_open, sell_to_close, sell_to_open (naked writing is allowed), buy_to_close.",
     _ORDER_SCHEMA, _preview_order)


@_guard
async def _preview_spread(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    body, summary = build_spread_body(args)
    preview = {k: v for k, v in body.items() if k != "TradeNotes"}
    b = body_of(await st_call(ctx, "POST", "/trading/PreviewOptionSpreadsV2", json_body={**preview, "isOrderPreview": True}))
    if not b.get("Success"):
        return fail("rejected_by_stocktrak", b.get("error") or b.get("ErrorMessage") or "spread preview rejected", summary=summary)
    pid = _remember(ctx, "spread", body, summary)
    return {"ok": True, "preview_id": pid, "summary": summary, "cost": b.get("cost"),
            "estimated_cost": b.get("estimatedCost"),
            "stocktrak_preview": (text_of(b, "OrderInfoHtml") or text_of(b, "html"))[:1200],
            "next": f"Show the user the summary; then hedgehog_place_order(preview_id='{pid}')."}

_reg("hedgehog_preview_spread",
     "Validate and preview an option strategy (no order placed). Input: spread_type 1–22, symbol, legs from "
     "hedgehog_spread_template with strike/expiration filled, trade_note. Negative cost = net credit.",
     {"type": "object", "properties": {
         "spread_type": {"type": "integer", "minimum": 1, "maximum": 22}, "symbol": {"type": "string"},
         "expiration": {"type": "string", "description": "Default MM/DD/YYYY for legs without their own."},
         "legs": {"type": "array", "items": {"type": "object", "properties": {
             "side": {"type": "string", "enum": ["Buy", "Short"]}, "quantity": {"type": "integer", "minimum": 1},
             "option_type": {"type": "string", "enum": ["Call", "Put", ""]}, "strike": {"type": "number"},
             "expiration": {"type": "string"}}, "required": ["side", "quantity", "option_type"]}},
         "trade_note": {"type": "string"}},
      "required": ["spread_type", "symbol", "legs", "trade_note"], "additionalProperties": False}, _preview_spread)


@_guard
async def _place_order(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    pid = str(args.get("preview_id") or "")
    rec = _session(ctx).get(pid)
    if not rec or time.time() - rec["ts"] > PREVIEW_TTL_S:
        return fail("preview_required", "No fresh preview with that id in this session. Call hedgehog_preview_order "
                                        "(or hedgehog_preview_spread) first; place exactly what was previewed.")
    refusal = await confirm_with_user(ctx, f"Place this StockTrak order? {rec['summary']}. "
                                           "It counts toward your scored Hedgehog Camp game.")
    if refusal:
        return refusal
    if rec["kind"] == "spread":
        b = body_of(await st_call(ctx, "POST", "/trading/PlaceOptionSpreadsV2", json_body=rec["payload"], write=True))
    else:
        b = body_of(await st_call(ctx, "POST", "/trading/placeorderv2", rec["payload"], write=True))
    _session(ctx).pop(pid, None)
    if not b.get("Success"):
        return fail("rejected_by_stocktrak", b.get("ErrorMessage") or b.get("error") or "order rejected", summary=rec["summary"])
    return {"ok": True, "placed": rec["summary"], "stocktrak": text_of(b, "TradeButtonsHtml")[:600],
            "next": "Check hedgehog_orders(view='open') for the order_id; market orders during market hours fill in seconds, "
                    "otherwise at the next open."}

_reg("hedgehog_place_order",
     "Place a previously previewed order or spread (by preview_id). Asks the user to confirm in the chat unless "
     "they turned confirmations off. Never retry a declined or failed placement without the user asking.",
     {"type": "object", "properties": {"preview_id": {"type": "string"}}, "required": ["preview_id"],
      "additionalProperties": False}, _place_order, timeout_s=1900)


# ---- manage open orders ----------------------------------------------------

@_guard
async def _cancel_order(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    oid = str(args.get("order_id") or "").strip()
    if not oid.isdigit():
        return fail("bad_args", "order_id is the numeric id from hedgehog_orders(view='open')")
    b = body_of(await st_call(ctx, "POST", "/account/cancelorder", {"orderID": oid}, write=True))
    return {"ok": bool(b.get("Success")), "cancelled": oid if b.get("Success") else None,
            "message": b.get("ErrorMessage"), "note": "Cancelling one leg of a spread cancels the whole spread."}

_reg("hedgehog_cancel_order",
     "Cancel a pending order by order_id (from hedgehog_orders view='open'). Cancelling any spread leg cancels the spread.",
     {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"],
      "additionalProperties": False}, _cancel_order)


@_guard
async def _edit_order(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    conf = str(args.get("order_conf") or "").strip()
    price = float(args.get("price"))
    if price <= 0:
        return fail("bad_args", "price must be positive")
    b = body_of(await st_call(ctx, "POST", "/trading/EditOrder",
                              {"orderConf": conf, "price": f"{price:g}", "expiryDate": args.get("gtd_date") or ""}, write=True))
    return {"ok": bool(b.get("Success")), "message": b.get("ErrorMessage")}

_reg("hedgehog_edit_order",
     "Change the price (and good-til date) of an open limit/stop order. order_conf is the GUID from "
     "hedgehog_orders(view='history').",
     {"type": "object", "properties": {"order_conf": {"type": "string"}, "price": {"type": "number"},
                                       "gtd_date": {"type": "string"}},
      "required": ["order_conf", "price"], "additionalProperties": False}, _edit_order)


@_guard
async def _add_trade_note(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    note = str(args.get("note") or "").strip()
    if not note:
        return fail("bad_args", "note required")
    b = body_of(await st_call(ctx, "POST", "/account/savetradingnote",
                              {"orderConf": args.get("order_conf"), "note": note[:300]}, write=True))
    return {"ok": bool(b.get("IsSuccess", b.get("Success"))), "message": b.get("Message")}

_reg("hedgehog_add_trade_note",
     "Attach a trade note (the reasoning behind a trade; required by Hedgehog Camp rules) to an existing order.",
     {"type": "object", "properties": {"order_conf": {"type": "string"}, "note": {"type": "string"}},
      "required": ["order_conf", "note"], "additionalProperties": False}, _add_trade_note)


# ---- exercise --------------------------------------------------------------

@_guard
async def _exercise(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    sid, usid = str(args.get("security_id") or ""), str(args.get("underlying_security_id") or "")
    osym, qty = str(args.get("option_symbol") or ""), int(args.get("quantity") or 1)
    if not (sid.isdigit() and usid.isdigit() and osym):
        return fail("bad_args", "security_id, underlying_security_id and option_symbol come from hedgehog_positions(security_type='option')")
    q = body_of(await st_call(ctx, "GET", "/account/getoptiondata",
                              {"symbol": osym, "exchange": "US", "underlyingSecurityID": usid, "quantity": qty}))
    strike, last = parse_money(q.get("OptionStrikePrice")), parse_money(q.get("StockLastPrice"))
    ctype = option_type_from_symbol(osym)
    money = option_moneyness(ctype, strike, last) if ctype and strike is not None and last is not None else "unknown"
    if money != "itm" and not args.get("allow_out_of_the_money"):
        return fail("not_in_the_money",
                    f"{osym} is {money.upper()} (strike {strike}, stock {last}). StockTrak would still exercise it and "
                    "deliver shares at the strike: a guaranteed loss. Pass allow_out_of_the_money=true only if the user insists.",
                    quote=q)
    refusal = await confirm_with_user(ctx, f"Exercise {qty} × {osym} ({money.upper()}): {q.get('TotalShares')} shares at "
                                           f"${strike}, total {q.get('TotalCost')}? Counts as 2 trades.", header="Exercise")
    if refusal:
        return refusal
    b = body_of(await st_call(ctx, "POST", "/account/exerciseoption", {"securityID": sid, "quantity": qty}, write=True))
    return {"ok": bool(b.get("Success")), "message": b.get("ErrorMessage"), "moneyness": money,
            "note": "Shares arrive at the strike price; check hedgehog_positions(security_type='equity')."}

_reg("hedgehog_exercise_option",
     "Exercise a held option (100 shares per contract at the strike). Refuses out-of-the-money exercises unless "
     "allow_out_of_the_money=true, because StockTrak itself does not check. Asks the user to confirm.",
     {"type": "object", "properties": {
         "security_id": {"type": "string"}, "underlying_security_id": {"type": "string"},
         "option_symbol": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1, "default": 1},
         "allow_out_of_the_money": {"type": "boolean", "default": False}},
      "required": ["security_id", "underlying_security_id", "option_symbol"], "additionalProperties": False},
     _exercise, timeout_s=1900)
