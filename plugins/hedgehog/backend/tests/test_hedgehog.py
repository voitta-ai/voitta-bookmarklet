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
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, ctx, method, path, params=None, json_body=None, *, write=False, **_):
        self.calls.append({"method": method, "path": path, "params": params, "json": json_body, "write": write})
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
