"""Game-rule guardrails and StockTrak order encoding. Pure functions, no I/O.

StockTrak's server does NOT enforce several of the session's own rules
(verified 2026-11-02, see docs/05-quirks.md): it accepts fractional
quantities, orders without trade notes, and previews disallowed security
types. These checks enforce the rules client-side so the plugin never
places an order that breaks them.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any

# Session "MIT 2026 Trading Simulation Game" rules.
ALLOWED_SECURITY_TYPES = {"equity": "Equities", "option": "Options"}
MAX_NOTE_CHARS = 300

EQUITY_SIDES = {"buy": 1, "sell": 2, "short": 3, "cover": 4}
OPTION_SIDES = {"buy_to_open": 1, "buy": 1, "sell_to_close": 2, "sell": 2, "sell_to_open": 3, "buy_to_close": 4}
ORDER_TYPES = {"market": 1, "limit": 2, "stop": 3, "trailing_stop_pct": 4}
DURATIONS = {"day": 1, "gtc": 2, "gtd": 3}
SPREAD_TYPES = {
    1: "Covered Call", 2: "Covered Put", 3: "Married Call", 4: "Married Put", 5: "Bear Call", 6: "Bull Call",
    7: "Bear Put", 8: "Bull Put", 9: "Long Straddle", 10: "Short Straddle", 11: "Long Strangle",
    12: "Short Strangle", 13: "Long Butterfly Call", 14: "Short Butterfly Call", 15: "Long Butterfly Put",
    16: "Short Butterfly Put", 17: "Collar", 18: "Long Condor Call", 19: "Long Condor Put",
    20: "Short Condor Call", 21: "Short Condor Put", 22: "Iron Condor",
}

_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class RuleError(ValueError):
    """An order that breaks a game rule or is malformed."""


def _date(s: Any, field: str) -> str:
    m = _DATE.match(str(s or "").strip())
    if not m:
        raise RuleError(f"{field} must be MM/DD/YYYY, got {s!r}")
    mo, d, y = map(int, m.groups())
    date(y, mo, d)  # raises on impossible dates
    return f"{mo}/{d}/{y}"


def _strike_str(x: Any) -> str:
    v = float(x)
    if v <= 0:
        raise RuleError("strike must be positive")
    return f"{v:.4f}".rstrip("0").rstrip(".")


def option_symbol(root: str, expiration: str, option_type: str, strike: Any) -> str:
    """StockTrak option symbol: ROOT + YY + DD + month letter + strike.

    Month letter: calls A–L = Jan–Dec, puts M–X = Jan–Dec
    (verified: AAPL2611K337.5 = 11/11/2026 call, AAPL2611W337.5 = put).
    """
    mo, d, y = map(int, _date(expiration, "expiration").split("/"))
    base = ord("A") if option_type.lower() == "call" else ord("M")
    return f"{root.upper()}{y % 100:02d}{d:02d}{chr(base + mo - 1)}{_strike_str(strike)}"


_OPTION_SYMBOL = re.compile(r"^([A-Z][A-Z.]*?)(\d{2})(\d{2})([A-X])(\d+(?:\.\d+)?)$")


def option_type_from_symbol(sym: str) -> str | None:
    """'call' / 'put' from the month letter (A–L calls, M–X puts); None if not an option symbol."""
    m = _OPTION_SYMBOL.match(str(sym or "").strip().upper())
    if not m:
        return None
    return "call" if m.group(4) <= "L" else "put"


def _qty(x: Any) -> int:
    try:
        f = float(x)
    except (TypeError, ValueError):
        raise RuleError(f"quantity must be a number, got {x!r}") from None
    if f <= 0 or f != int(f):
        raise RuleError("quantity must be a positive whole number (fractional trading is not allowed in this game)")
    return int(f)


def _note(x: Any) -> str:
    note = str(x or "").strip()
    if not note:
        raise RuleError("trade_note is required by the game rules (the server does not check this; the plugin does)")
    if len(note) > MAX_NOTE_CHARS:
        raise RuleError(f"trade_note is limited to {MAX_NOTE_CHARS} characters")
    return note


def build_order_form(order: dict[str, Any]) -> tuple[dict[str, str], str]:
    """Validate a model-level order and return (StockTrak form fields, human summary)."""
    st = str(order.get("security_type") or "equity").lower()
    if st not in ALLOWED_SECURITY_TYPES:
        raise RuleError(f"security_type {st!r} is not allowed in this game; only equity (stocks/ETFs) and option")
    symbol = str(order.get("symbol") or "").strip().upper()
    if not _SYMBOL.match(symbol):
        raise RuleError(f"invalid symbol {symbol!r} (US listings only)")
    side_key = str(order.get("side") or "").lower()
    sides = EQUITY_SIDES if st == "equity" else OPTION_SIDES
    if side_key not in sides:
        raise RuleError(f"side must be one of {sorted(sides)} for {st}")
    qty = _qty(order.get("quantity"))
    otype = str(order.get("order_type") or "market").lower()
    if otype not in ORDER_TYPES:
        raise RuleError(f"order_type must be one of {sorted(ORDER_TYPES)}")
    price = 0.0
    if otype in ("limit", "stop"):
        price = float(order.get("price") or 0)
        if price <= 0:
            raise RuleError(f"{otype} orders need a positive price")
    elif otype == "trailing_stop_pct":
        price = float(order.get("trailing_pct") or 0)
        if not 0 < price <= 100:
            raise RuleError("trailing_stop_pct needs trailing_pct in (0, 100]; StockTrak treats the value as a PERCENT")
    dur = str(order.get("duration") or "day").lower()
    if dur not in DURATIONS:
        raise RuleError(f"duration must be one of {sorted(DURATIONS)}")
    gtd = _date(order.get("gtd_date"), "gtd_date") if dur == "gtd" else ""
    note = _note(order.get("trade_note"))

    form = {
        "Symbol": symbol, "OrderSide": str(sides[side_key]), "Quantity": str(qty),
        "OrderType": str(ORDER_TYPES[otype]), "Price": f"{price:g}", "OrderExpiration": str(DURATIONS[dur]),
        "ExpirationDate": gtd, "SecurityType": ALLOWED_SECURITY_TYPES[st], "CompanyName": "", "Currency": "USD",
        "Exchange": "US", "TradeNotes": note,
    }
    desc_sym = symbol
    if st == "equity":
        form.update({"PrimaryMarket": "", "QuantityType": "Amount", "UnitPrice": "", "SelectedRegion": "0"})
    else:
        ctype = str(order.get("option_type") or "").lower()
        if ctype not in ("call", "put"):
            raise RuleError("options need option_type 'call' or 'put'")
        exp = _date(order.get("expiration"), "expiration")
        strike = _strike_str(order.get("strike"))
        osym = option_symbol(symbol, exp, ctype, strike)
        form.update({"OptionType": ctype.capitalize(), "OptionExpiry": exp, "OptionStrikePrice": strike, "OptionSymbol": osym})
        desc_sym = f"{osym} ({symbol} {exp} {strike} {ctype})"

    px = {"market": "at market", "limit": f"limit ${price:g}", "stop": f"stop ${price:g}",
          "trailing_stop_pct": f"trailing stop {price:g}%"}[otype]
    unit = "contract" if st == "option" else "share"
    summary = f"{side_key.replace('_', ' ').upper()} {qty} {unit}{'s' if qty != 1 else ''} {desc_sym} {px}, {dur.upper()}"
    if gtd:
        summary += f" until {gtd}"
    return form, summary


def build_spread_body(spread: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Validate a spread and return (PlaceOptionSpreadsV2 JSON body, summary)."""
    try:
        stype = int(spread.get("spread_type"))
    except (TypeError, ValueError):
        raise RuleError("spread_type must be an integer 1–22") from None
    if stype not in SPREAD_TYPES:
        raise RuleError("spread_type must be an integer 1–22")
    symbol = str(spread.get("symbol") or "").strip().upper()
    if not _SYMBOL.match(symbol):
        raise RuleError(f"invalid symbol {symbol!r}")
    legs = spread.get("legs") or []
    if not isinstance(legs, list) or not legs:
        raise RuleError("legs required; get the template from hedgehog_spread_template")
    orders, parts = [], []
    for i, leg in enumerate(legs):
        side = str(leg.get("side") or "")
        if side not in ("Buy", "Short"):
            raise RuleError(f"legs[{i}].side must be 'Buy' or 'Short' (as in the template)")
        qty = _qty(leg.get("quantity"))
        ctype = str(leg.get("option_type") or "")
        exp = _date(leg.get("expiration") or spread.get("expiration"), f"legs[{i}].expiration")
        if ctype:
            if ctype not in ("Call", "Put"):
                raise RuleError(f"legs[{i}].option_type must be 'Call', 'Put' or '' (stock leg)")
            strike = _strike_str(leg.get("strike"))
            parts.append(f"{side} {qty} {symbol} {exp} {strike} {ctype}")
        else:
            strike = ""
            parts.append(f"{side} {qty} {symbol} shares")
        orders.append({"Symbol": symbol, "Quantity": str(qty), "OrderSide": side, "OptionType": ctype,
                       "OptionStrikePrice": strike, "OptionExpiryDate": exp, "Exchange": "US"})
    body = {"OptionSpreadType": str(stype), "Orders": orders, "TradeNotes": _note(spread.get("trade_note"))}
    return body, f"{SPREAD_TYPES[stype]} on {symbol}: " + "; ".join(parts) + " (each leg a market GTC order)"


def fingerprint(kind: str, payload: dict[str, Any]) -> str:
    """Stable id for a previewed order: the place call must match it exactly."""
    blob = json.dumps({"kind": kind, "payload": payload}, sort_keys=True, separators=(",", ":"))
    return "pv_" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def option_moneyness(option_type: str, strike: float, stock_last: float) -> str:
    """'itm' | 'atm' | 'otm' for a call/put given the underlying's last price."""
    if abs(stock_last - strike) < 1e-9:
        return "atm"
    if option_type.lower() == "call":
        return "itm" if stock_last > strike else "otm"
    return "itm" if stock_last < strike else "otm"


def parse_money(s: Any) -> float | None:
    """'$1,234.50' / '($0.29)' / '-12.3%' → float; None when not a number."""
    t = str(s or "").strip()
    if not t:
        return None
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-")
    t = re.sub(r"[^0-9.]", "", t)
    if not t or t == ".":
        return None
    v = float(t)
    return -v if neg else v
