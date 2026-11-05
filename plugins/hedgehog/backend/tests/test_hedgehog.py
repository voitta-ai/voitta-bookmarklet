"""Hedgehog plugin: rule encoding and trading guardrails.

Run from backend/ (its pytest config supplies asyncio_mode=auto):
    PYTHONPATH=../plugins/hedgehog/backend .venv/bin/python -m pytest -q \
        -c pyproject.toml --rootdir . ../plugins/hedgehog/backend/tests

The browser bridge is replaced by a recorder, so these tests check exactly what
would be sent to StockTrak. Expected request bodies are the ones that
succeeded in live testing (2026-11-02/03).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from voitta_hedgehog import _core, rewards, trading
from voitta_hedgehog.rules import (
    RuleError, build_order_form, build_spread_body, fingerprint, option_moneyness, option_symbol,
    option_type_from_symbol, parse_money,
)

NOTE = "Test: thesis, risk and exit plan"


# ---- rules -------------------------------------------------------------------

@pytest.mark.parametrize("root,exp,kind,strike,expected", [
    ("AAPL", "11/11/2026", "call", 337.5, "AAPL2611K337.5"),
    ("AAPL", "11/11/2026", "put", 337.5, "AAPL2611W337.5"),
    ("F", "11/20/2026", "call", 12.5, "F2620K12.5"),
    ("F", "11/20/2026", "call", 12, "F2620K12"),
    ("F", "1/2/2027", "put", "13.00", "F2702M13"),
])
def test_option_symbol(root, exp, kind, strike, expected):
    assert option_symbol(root, exp, kind, strike) == expected
    assert option_type_from_symbol(expected) == kind


def test_option_type_from_symbol_rejects_stock():
    assert option_type_from_symbol("AAPL") is None


def test_equity_form_matches_verified_body():
    form, summary = build_order_form({"symbol": "f", "side": "sell", "quantity": 1, "trade_note": NOTE})
    assert form == {
        "Symbol": "F", "OrderSide": "2", "Quantity": "1", "OrderType": "1", "Price": "0", "OrderExpiration": "1",
        "ExpirationDate": "", "SecurityType": "Equities", "CompanyName": "", "Currency": "USD", "Exchange": "US",
        "TradeNotes": NOTE, "PrimaryMarket": "", "QuantityType": "Amount", "UnitPrice": "", "SelectedRegion": "0",
    }
    assert summary.startswith("SELL 1 share F at market")


def test_option_form_matches_verified_body():
    form, _ = build_order_form({"security_type": "option", "symbol": "F", "side": "sell_to_close", "quantity": 1,
                                "option_type": "call", "expiration": "11/20/2026", "strike": 12, "trade_note": NOTE})
    assert {k: form[k] for k in ("Symbol", "OrderSide", "OptionType", "OptionExpiry", "OptionStrikePrice",
                                 "SecurityType", "OptionSymbol")} == {
        "Symbol": "F", "OrderSide": "2", "OptionType": "Call", "OptionExpiry": "11/20/2026",
        "OptionStrikePrice": "12", "SecurityType": "Options", "OptionSymbol": "F2620K12"}
    assert "PrimaryMarket" not in form


@pytest.mark.parametrize("bad,msg", [
    ({"quantity": 0.5}, "whole number"),
    ({"trade_note": "  "}, "trade_note is required"),
    ({"trade_note": "x" * 301}, "300"),
    ({"security_type": "mutual_fund"}, "not allowed"),
    ({"side": "short", "security_type": "option"}, "side must be"),
    ({"order_type": "limit"}, "positive price"),
    ({"order_type": "trailing_stop_pct", "trailing_pct": 150}, "PERCENT"),
    ({"duration": "gtd", "gtd_date": "2026-11-20"}, "MM/DD/YYYY"),
    ({"symbol": "../etc"}, "invalid symbol"),
])
def test_rule_violations(bad, msg):
    order = {"symbol": "F", "side": "buy", "quantity": 1, "trade_note": NOTE, **bad}
    with pytest.raises(RuleError, match=msg):
        build_order_form(order)


def test_spread_body_matches_verified_covered_call():
    body, summary = build_spread_body({"spread_type": 1, "symbol": "F", "expiration": "11/20/2026", "trade_note": NOTE,
                                       "legs": [{"side": "Buy", "quantity": 100, "option_type": ""},
                                                {"side": "Short", "quantity": 1, "option_type": "Call", "strike": 13}]})
    assert body["Orders"] == [
        {"Symbol": "F", "Quantity": "100", "OrderSide": "Buy", "OptionType": "", "OptionStrikePrice": "",
         "OptionExpiryDate": "11/20/2026", "Exchange": "US"},
        {"Symbol": "F", "Quantity": "1", "OrderSide": "Short", "OptionType": "Call", "OptionStrikePrice": "13",
         "OptionExpiryDate": "11/20/2026", "Exchange": "US"}]
    assert body["OptionSpreadType"] == "1" and body["TradeNotes"] == NOTE
    assert summary.startswith("Covered Call on F")


def test_fingerprint_is_order_sensitive_and_stable():
    a = fingerprint("order", {"x": 1, "y": 2})
    assert a == fingerprint("order", {"y": 2, "x": 1})
    assert a != fingerprint("order", {"x": 1, "y": 3}) and a != fingerprint("spread", {"x": 1, "y": 2})


def test_moneyness_and_money():
    assert option_moneyness("call", 12.5, 12.36) == "otm"
    assert option_moneyness("call", 12.0, 12.36) == "itm"
    assert option_moneyness("put", 13.0, 12.36) == "itm"
    assert parse_money("($0.29)") == -0.29 and parse_money("$1,250.00") == 1250.0 and parse_money("") is None


# ---- trading guardrails ------------------------------------------------------

class Bridge:
    """Records st_call invocations and answers with canned bodies."""

    def __init__(self, replies: dict[str, dict[str, Any]]):
        self.replies = replies
        # path → queue of raw primitive results (e.g. CSV {kind, rows}); the last one repeats.
        self.raw: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, ctx, method, path, params=None, json_body=None, *, write=False, **_):
        self.calls.append({"method": method, "path": path, "params": params, "json": json_body, "write": write})
        if path in self.raw:
            q = self.raw[path]
            return q.pop(0) if len(q) > 1 else q[0]
        return {"kind": "json", "body": dict(self.replies.get(path, {"Success": True}))}

    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]


@pytest.fixture
def env(monkeypatch):
    state = {"enabled": True, "confirm": True, "answer": "Place it"}
    monkeypatch.setattr(trading, "trading_enabled", lambda: state["enabled"])
    monkeypatch.setattr(trading, "confirm_orders", lambda: state["confirm"])
    asked: list[Any] = []

    async def fake_ask(args, ctx):
        asked.append(args)
        if state["answer"] is None:
            return {"answered": False, "reason": "timeout"}
        return {"answered": True, "answers": {args["questions"][0]["question"]: state["answer"]}}

    monkeypatch.setattr(trading.ask_user, "_handler", fake_ask)
    bridge = Bridge({"/trading/previeworderv2": {"Success": True, "OrderInfoHtml": {"text": "Buy 1 F"}},
                     "/trading/getEstimatedCost": {"success": True, "estimatedcost": "-$12.36"}})
    monkeypatch.setattr(trading, "st_call", bridge)
    monkeypatch.setattr(_core, "st_call", bridge)  # open_positions() lives in _core
    monkeypatch.setattr(trading, "FILL_POLL_DELAYS_S", (0, 0, 0))
    trading._previews.clear()
    return SimpleNamespace(state=state, asked=asked, bridge=bridge, ctx=SimpleNamespace(session_id="s1", extras={}))


ORDER = {"symbol": "F", "side": "buy", "quantity": 1, "trade_note": NOTE}


async def test_disabled_trading_refuses(env):
    env.state["enabled"] = False
    r = await trading._preview_order(dict(ORDER), env.ctx)
    assert r["error"] == "trading_disabled" and env.bridge.calls == []


async def test_place_requires_preview(env):
    r = await trading._place_order({"preview_id": "pv_nope"}, env.ctx)
    assert r["error"] == "preview_required" and env.bridge.calls == []


async def test_rule_violation_never_reaches_stocktrak(env):
    r = await trading._preview_order({**ORDER, "quantity": 0.5}, env.ctx)
    assert r["error"] == "rule_violation" and env.bridge.calls == []


async def test_preview_then_confirmed_place_sends_exact_form(env):
    p = await trading._preview_order(dict(ORDER), env.ctx)
    assert p["ok"] and p["estimated_cost"] == "-$12.36"
    r = await trading._place_order({"preview_id": p["preview_id"]}, env.ctx)
    assert r["ok"], r
    place = [c for c in env.bridge.calls if c["path"] == "/trading/placeorderv2"]
    assert len(place) == 1 and place[0]["write"] is True
    preview = next(c for c in env.bridge.calls if c["path"] == "/trading/previeworderv2")
    assert place[0]["params"] == preview["params"]  # placed exactly what was previewed
    assert len(env.asked) == 1 and "BUY 1 share F" in env.asked[0]["questions"][0]["question"]
    # preview ids are single-use
    again = await trading._place_order({"preview_id": p["preview_id"]}, env.ctx)
    assert again["error"] == "preview_required"


@pytest.mark.parametrize("answer,kind", [("Don't place", "declined"), (None, "not_confirmed")])
async def test_no_place_without_explicit_yes(env, answer, kind):
    p = await trading._preview_order(dict(ORDER), env.ctx)
    env.state["answer"] = answer
    r = await trading._place_order({"preview_id": p["preview_id"]}, env.ctx)
    assert r["error"] == kind
    assert "/trading/placeorderv2" not in env.bridge.paths()


async def test_preview_is_session_scoped(env):
    p = await trading._preview_order(dict(ORDER), env.ctx)
    other = SimpleNamespace(session_id="s2", extras={})
    r = await trading._place_order({"preview_id": p["preview_id"]}, other)
    assert r["error"] == "preview_required"


async def test_skip_confirmation_setting(env):
    env.state["confirm"] = False
    p = await trading._preview_order(dict(ORDER), env.ctx)
    r = await trading._place_order({"preview_id": p["preview_id"]}, env.ctx)
    assert r["ok"] and env.asked == []


async def test_exercise_refuses_out_of_the_money(env):
    env.bridge.replies["/account/getoptiondata"] = {"Success": True, "OptionStrikePrice": "12.50",
                                                    "StockLastPrice": "12.36", "TotalShares": 100, "TotalCost": "$1,250.00"}
    r = await trading._exercise({"security_id": "27078109", "underlying_security_id": "1", "option_symbol": "F2620K12.5"}, env.ctx)
    assert r["error"] == "not_in_the_money"
    assert "/account/exerciseoption" not in env.bridge.paths() and env.asked == []


async def test_exercise_in_the_money_asks_then_exercises(env):
    env.bridge.replies["/account/getoptiondata"] = {"Success": True, "OptionStrikePrice": "12.00",
                                                    "StockLastPrice": "12.36", "TotalShares": 100, "TotalCost": "$1,200.00"}
    r = await trading._exercise({"security_id": "27074188", "underlying_security_id": "1", "option_symbol": "F2620K12"}, env.ctx)
    assert r["ok"] and r["moneyness"] == "itm" and len(env.asked) == 1
    ex = [c for c in env.bridge.calls if c["path"] == "/account/exerciseoption"]
    assert ex[0]["write"] is True and ex[0]["params"] == {"securityID": "27074188", "quantity": 1}


# ---- settings + rewards ------------------------------------------------------

def test_settings_defaults(monkeypatch):
    monkeypatch.setattr(_core.user_settings, "read", lambda: {})
    assert _core.trading_enabled() is False and _core.confirm_orders() is True
    monkeypatch.setattr(_core.user_settings, "read",
                        lambda: {"plugins": {"hedgehog": {"trading_enabled": True, "skip_order_confirmation": True}}})
    assert _core.trading_enabled() is True and _core.confirm_orders() is False


def test_rewards_scrub():
    out = rewards.scrub({"game_id": "x@y.edu", "email": "a@b.com", "wallet_address": "0xabc", "account_id": 5,
                         "nested": [{"note": "mail me at c@d.org", "token_balance": 1.5}]})
    assert out == {"game_id": "<email>", "nested": [{"note": "mail me at <email>", "token_balance": 1.5}]}


def test_rewards_matches_portal_prefixed_username():
    assert rewards._match({"game_id": "portal:voitta"}, "voitta")
    assert rewards._match({"game_id": "Voitta"}, "voitta")
    assert not rewards._match({"game_id": "voitta2"}, "voitta")


# ---- live-use report 11/4–11/5/2026 --------------------------------------------

from voitta_hedgehog import tools  # noqa: E402


def _csv(rows):
    return {"kind": "csv", "rows": rows}


_POS_HEAD = ["Symbol", "Description", "Quantity", "Currency", "LastPrice", "PricePaid", "DayChange", "ProfitLoss",
             "MarketValue", "ProfitLossPercentage"]


def _pos(qty):
    """SPY as /admin/ExportOpenPositions/ returns it (whatever securityType is asked for)."""
    return _csv([_POS_HEAD, ["SPY", "SPDR S&P 500", str(qty), "USD", "768.2051", "767.399223946785", "0.5", "1,517.18",
                             "1190141.4096", "0.105"]])


CTX = SimpleNamespace(session_id="s", extras={})


def test_numbers_are_normalised():
    rec = _core.norm_record({"ProfitLoss": "1,517.18", "MarketValue": "1190141.4096", "PricePaid": "767.399223946785",
                             "LastPrice": "768.2051", "Quantity": "770", "Interest Earn Rate": "$0.00",
                             "Change": "($0.29)", "% Return": "-12.3%", "Symbol": "SPY", "Date": "11/4/2026",
                             "Trades Made/Allowed": "14/10000", "Note": "N/A", "Option": "F2620K12.5"})
    assert rec == {"ProfitLoss": 1517.18, "MarketValue": 1190141.41, "PricePaid": 767.3992, "LastPrice": 768.2051,
                   "Quantity": 770, "Interest Earn Rate": 0.0, "Change": -0.29, "% Return": -12.3, "Symbol": "SPY",
                   "Date": "11/4/2026", "Trades Made/Allowed": "14/10000", "Note": "N/A", "Option": "F2620K12.5"}


def test_stocktrak_typos_are_normalised():
    assert _core.fix_key("Accured Interest") == "Accrued Interest"
    assert _core.fix_key("Net Matket Value") == "Net Market Value"
    assert _core.fix_key("Net Market Value") == "Net Market Value"  # still fine once StockTrak fixes it


def test_confirmed_quantity():
    t = "Order Confirmation Your Market Buy for 573 shares of SPY has been placed."
    assert _core.confirmed_quantity(t, "SPY") == 573
    assert _core.confirmed_quantity(t, "QQQ") is None
    assert _core.confirmed_quantity("Your Limit Sell to Close for 2 contracts of F2620K12.5.", "F2620K12.5") == 2
    assert _core.confirmed_quantity("Your Market Buy for 1,200 shares of F") == 1200
    assert _core.confirmed_quantity("nothing here") is None


def test_account_flags_mixed_moments_and_missing_margin():
    # The 11/5 09:40 ET snapshot from the report.
    summary = _core.norm_record({
        "Market Value of Long Positions": "$1,940,522.20", "Market Value of Short Positions": "$0.00",
        "Portfolio Value": "$1,009,748.04", "Cash Balance": "$0.00", "Loan Balance": "$962,637.09",
        "Margin Requirements": "$0.00"})
    warnings, checks = tools.account_checks(summary)
    assert checks["identity"]["difference"] == pytest.approx(31862.93, abs=0.01)
    assert summary["Margin Requirements"] is None
    assert len(warnings) == 2 and "different moments" in warnings[0] and "missing value" in warnings[1]
    assert checks["gross_leverage"] == pytest.approx(1.922, abs=0.001)


def test_account_consistent_snapshot_has_no_warnings():
    summary = {"Market Value of Long Positions": 1_972_385.0, "Portfolio Value": 1_009_747.91, "Cash Balance": 0.0,
               "Loan Balance": 962_637.09}
    assert tools.account_checks(summary)[0] == []


async def test_account_tool_numbers_typos_and_empty_fields(monkeypatch):
    rows = [["Cash Balance:", "$0.00", "Portfolio Value:", "$1,009,748.04"],
            ["Market Value of Long Positions:", "$1,940,522.20", "Loan Balance:", "$962,637.09"],
            ["SPY ETF % Return:", "", "Accured Interest:", "$0.00"],
            ["Trades Made/Allowed:", "26/9974", "", ""]]

    async def fake(ctx, *a, **k):
        return _csv(rows)

    monkeypatch.setattr(tools, "st_call", fake)
    r = await tools._account({}, CTX)
    assert r["ok"] and r["not_reported_by_stocktrak"] == ["SPY ETF % Return"]
    assert "SPY ETF % Return" not in r["summary"]
    assert r["summary"]["Accrued Interest"] == 0.0 and r["summary"]["Portfolio Value"] == 1009748.04
    # 26/9974 is made / remaining (counts down from 10,000), not made / allowed
    assert (r["trades_made"], r["trades_remaining"], r["trades_allowed"]) == (26, 9974, 10000)
    assert r["warnings"] and r["as_of_et"]


async def test_option_positions_exclude_equity_rows(monkeypatch):
    async def fake(ctx, method, path, params=None, **k):
        if path == "/admin/ExportOpenPositions/":
            return _pos(1353)  # ignores securityType=Options, like the real endpoint
        return {"kind": "json", "body": {"Html": {"rows": []}}}

    monkeypatch.setattr(tools, "st_call", fake)
    monkeypatch.setattr(_core, "st_call", fake)
    r = await tools._positions({"security_type": "option"}, CTX)
    assert r["count"] == 0 and r["positions"] == []
    r = await tools._positions({"security_type": "equity"}, CTX)
    assert r["count"] == 1
    assert r["positions"][0]["Quantity"] == 1353 and r["positions"][0]["MarketValue"] == 1190141.41


async def test_preview_warns_when_buying_power_short(env):
    env.bridge.replies["/trading/getpricev2"] = {"Success": True, "LastPrice": "768.2051"}
    env.bridge.replies["/trading/getmaxshares"] = {"success": True, "maxshares": "573"}
    env.bridge.replies["/trading/previeworderv2"] = {"Success": True,
                                                     "OrderInfoHtml": {"text": "Buy 770 SPY Trade notes 0 / 300"}}
    p = await trading._preview_order({**ORDER, "symbol": "SPY", "quantity": 770}, env.ctx)
    assert p["ok"] and p["max_shares"] == 573 and "573 of the 770" in p["warnings"][0]
    assert "/ 300" not in p["stocktrak_preview"] and p["trade_note_sent"] == NOTE
    mx = next(c for c in env.bridge.calls if c["path"] == "/trading/getmaxshares")
    assert mx["params"]["Price"] == "768.2051"


async def test_trailing_stop_percent_is_not_used_as_price(env):
    env.bridge.replies["/trading/getpricev2"] = {"Success": True, "LastPrice": "12.36"}
    await trading._preview_order({**ORDER, "side": "short", "order_type": "trailing_stop_pct", "trailing_pct": 5},
                                 env.ctx)
    mx = next(c for c in env.bridge.calls if c["path"] == "/trading/getmaxshares")
    assert mx["params"]["Price"] == "12.36"


async def test_partial_fill_is_flagged(env):
    env.bridge.replies["/trading/placeorderv2"] = {"Success": True,
                                                   "TradeButtonsHtml": {"text": "Your Market Buy for 573 shares of SPY"}}
    env.bridge.raw["/admin/ExportOpenPositions/"] = [_pos(780), _pos(780), _pos(1353)]
    p = await trading._preview_order({**ORDER, "symbol": "SPY", "quantity": 770}, env.ctx)
    r = await trading._place_order({"preview_id": p["preview_id"]}, env.ctx)
    assert r["ok"] and r["order"].startswith("BUY 770 shares SPY")
    q = r["quantity"]
    assert (q["requested"], q["stocktrak_confirmed"], q["position_change"], q["check"]) == (770, 573, 573, "partial")
    assert sum("PARTIAL" in w for w in r["warnings"]) == 2


async def test_full_fill_and_note_saved(env):
    env.bridge.raw["/admin/ExportOpenPositions/"] = [_pos(780), _pos(1550)]
    env.bridge.raw["/account/ExportTradeNotes"] = [_csv([["Date", "Symbol", "Note"], ["11/4/2026", "SPY", NOTE]])]
    p = await trading._preview_order({**ORDER, "symbol": "SPY", "quantity": 770}, env.ctx)
    r = await trading._place_order({"preview_id": p["preview_id"]}, env.ctx)
    assert r["quantity"]["check"] == "full" and r["trade_note_saved"] is True and r["warnings"] == []


async def test_missing_note_is_flagged(env):
    env.bridge.raw["/account/ExportTradeNotes"] = [_csv([["Date", "Symbol", "Note"]])]
    p = await trading._preview_order(dict(ORDER), env.ctx)
    r = await trading._place_order({"preview_id": p["preview_id"]}, env.ctx)
    assert r["trade_note_saved"] is False and any("trade note" in w for w in r["warnings"])


async def test_empty_rejection_gets_a_reason(env):
    env.bridge.replies["/trading/previeworderv2"] = {"Success": False, "ErrorMessage": ""}
    r = await trading._preview_order(dict(ORDER), env.ctx)
    assert r["error"] == "rejected_by_stocktrak" and "empty ErrorMessage" in r["message"]


def test_unscored_player_has_no_S():
    row = rewards.unscored({"weeks_participated": 0, "sharpe_ratio": -1.0, "total_return_pct": -0.206})
    assert row["sharpe_ratio"] is None and row["sharpe_ratio_uncredited"] == -1.0 and row["scored"] is False
    scored = {"weeks_participated": 3, "sharpe_ratio": 0.2}
    assert rewards.unscored(scored) is scored


# ---- fills joined into order history (11/5 re-check) --------------------------

def _orders_html(*rows):
    """getorderhistory rows: cells[1]=date, [2]=order, [3]=symbol, [4]=qty, [5]=price, [9]=status."""
    return {"kind": "json", "body": {"Html": {"rows": [
        {"cells": ["", d, o, s, q, p, "", "", "", st], "els": [], "data": {}} for d, o, s, q, p, st in rows]}}}

def _tx(*rows):
    head = ["Symbol", "TransactionType", "Quantity", "Price", "CreateDate"]
    return {"kind": "csv", "rows": [head, *[list(r) for r in rows]]}

def test_attach_fills_flags_capped_orders():
    orders = [  # newest first, like the page
        {"date": "11/4/2026 1:00:40 PM", "order": "Market - Buy", "symbol": "QQQ", "quantity": 560, "status": "Filled"},
        {"date": "11/4/2026 1:00:12 PM", "order": "Market - Buy", "symbol": "SPY", "quantity": 770, "status": "Filled"},
        {"date": "11/3/2026 10:02:00 AM", "order": "Market - Buy", "symbol": "SPY", "quantity": 100, "status": "Filled"},
        {"date": "11/4/2026 2:00:00 PM", "order": "Limit - Sell", "symbol": "SPY", "quantity": -63, "status": "Open"},
    ]
    txs = [
        {"Symbol": "SPY", "TransactionType": "Market - Buy", "Quantity": "100", "Price": "741.10", "CreateDate": "11/3/2026 10:02:01 AM"},
        {"Symbol": "SPY", "TransactionType": "Market - Buy", "Quantity": "573", "Price": "749.00", "CreateDate": "11/4/2026 1:00:13 PM"},
        {"Symbol": "QQQ", "TransactionType": "Market - Buy", "Quantity": "288", "Price": "612.5", "CreateDate": "11/4/2026 1:00:41 PM"},
        {"Symbol": "SPY", "TransactionType": "Dividend", "Quantity": "", "Price": "", "CreateDate": "11/4/2026"},
    ]
    assert _core.attach_fills(orders, txs) == 3
    qqq, spy, spy_old, open_ = orders
    assert (spy["filled_qty"], spy["partial"], spy["fill_price"]) == (573, True, 749.0)
    assert (qqq["filled_qty"], qqq["partial"]) == (288, True)
    assert (spy_old["filled_qty"], spy_old["partial"]) == (100, False)  # not stolen by the 11/4 order
    assert "filled_qty" not in open_

def test_attach_fills_marks_unmatched():
    orders = [{"date": "11/4/2026 1:00 PM", "order": "Market - Sell", "symbol": "SPY", "quantity": -10, "status": "Filled"}]
    txs = [{"Symbol": "SPY", "TransactionType": "Market - Buy", "Quantity": "10", "Price": "1", "CreateDate": "11/4/2026 1:00 PM"}]
    assert _core.attach_fills(orders, txs) == 0
    assert orders[0]["fill_match"] == "not_found" and orders[0]["filled_qty"] is None

def test_side_of_prefers_cover_and_short():
    assert [_core.side_of(t) for t in ("Buy to Cover", "Sell Short", "Market - Sell", "Limit - Buy", "Dividend")] == \
        ["cover", "short", "sell", "buy", None]

async def test_order_history_has_filled_qty_and_numbers(monkeypatch):
    async def fake(ctx, method, path, params=None, **k):
        if path == "/account/getorderhistory":
            return _orders_html(("11/4/2026 1:00:12 PM", "Market - Buy", "SPY", "770", "$XXX.xx", "Filled"),
                                ("11/4/2026 3:10:00 PM", "Market - Sell", "SPY", "-63", "$750.10", "Filled"))
        if path == "/admin/ExportTransactionHistory":
            assert params["from"] == "11/4/2026"
            return _tx(("SPY", "Market - Buy", "573", "749.00", "11/4/2026 1:00:13 PM"),
                       ("SPY", "Market - Sell", "-63", "750.10", "11/4/2026 3:10:02 PM"))
        raise AssertionError(path)

    monkeypatch.setattr(tools, "st_call", fake)
    r = await tools._orders({"view": "history"}, CTX)
    buy, sell = r["orders"]
    assert (buy["quantity"], buy["filled_qty"], buy["partial"], buy["fill_price"]) == (770, 573, True, 749.0)
    assert buy["price"] == "$XXX.xx"  # masked by StockTrak; the real price is fill_price
    assert (sell["quantity"], sell["filled_qty"], sell["partial"], sell["price"]) == (-63, 63, False, 750.1)
    assert r["partial_fills"] == [{"symbol": "SPY", "date": "11/4/2026 1:00:12 PM", "ordered": 770, "filled": 573}]

async def test_quote_last_price_is_a_number(monkeypatch):
    async def fake(ctx, *a, **k):
        return {"kind": "json", "body": {"Success": True, "Symbol": "SPY", "LastPrice": "$749.00", "symbolPrice": "SPY"}}

    monkeypatch.setattr(tools, "st_call", fake)
    assert (await tools._quote({"symbol": "spy"}, CTX))["LastPrice"] == 749.0

async def test_raw_read_adds_caveat_without_touching_payload(monkeypatch):
    body = {"rows": [["Date", "Trades"], ["11/3/2026", "14"], ["11/4/2026", "22"]]}

    async def fake(ctx, *a, **k):
        return {"kind": "json", "body": body}

    monkeypatch.setattr(tools, "st_call", fake)
    r = await tools._api_get({"path": "/account/dashboardhistoricalportfoliovalues"}, CTX)
    assert "CUMULATIVE" in r["caveat"] and r["body"] == body
