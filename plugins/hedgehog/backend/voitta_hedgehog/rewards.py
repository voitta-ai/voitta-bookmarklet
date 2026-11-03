"""Hedgehog Camp Rewards API (public, no auth): weeks, config, the user's own standing.

Base: https://d2hmf7qxwvlr38.cloudfront.net (FastAPI; spec at /openapi.json).

Privacy: several public endpoints leak player emails and wallet addresses
(reported separately). This module never returns other players' rows,
and scrubs emails / wallets / internal ids from everything it returns.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

BASE = "https://d2hmf7qxwvlr38.cloudfront.net"
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_DROP = {"email", "wallet_address", "account_id", "trading_account_id", "player_id", "tx_hash"}


def scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if k not in _DROP}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        return _EMAIL.sub("<email>", obj)
    return obj


async def _get(path: str, **params: Any) -> Any:
    async with httpx.AsyncClient(base_url=BASE, timeout=20.0, headers={"User-Agent": "voitta-hedgehog/0.1"}) as c:
        r = await c.get(path, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json()


async def config() -> dict[str, Any]:
    return scrub(await _get("/config"))


async def weeks(limit: int = 100) -> list[dict[str, Any]]:
    data = await _get("/api/v1/rewards/weeks", limit=min(int(limit), 100))
    return scrub(data if isinstance(data, list) else data.get("weeks", []))


def _match(row: dict[str, Any], username: str) -> bool:
    gid = str(row.get("game_id") or "").lower()
    u = username.lower()
    return gid == u or gid.endswith(":" + u)  # portal-created players show as "portal:<name>"


async def my_standing(username: str) -> dict[str, Any]:
    """Own row of the current token leaderboard + field size. No other players."""
    data = await _get("/api/v1/rewards/leaderboard", sort_by="token_balance", limit=1000)
    rows = data.get("leaderboard", []) if isinstance(data, dict) else data
    mine = [r for r in rows if _match(r, username)]
    return {
        "username": username,
        "found": bool(mine),
        "entries": scrub(mine),
        "players_on_leaderboard": len(rows),
        "benchmark": scrub(data.get("benchmark")) if isinstance(data, dict) else None,
        "note": None if mine else (
            "No leaderboard row for this username. Rewards need the SAME username in the Hedgehog "
            "Camp portal and in StockTrak; rows appear after the admin's next weekly upload."
        ),
    }


async def my_week(week_id: int, username: str) -> dict[str, Any]:
    """Week aggregates + the user's own reward breakdown (R, S, H0, dH) for that week."""
    data = await _get(f"/api/v1/rewards/weeks/{int(week_id)}/preview")
    rewards = data.get("rewards", []) if isinstance(data, dict) else []
    mine = [r for r in rewards if _match(r, username)]
    agg = {k: data.get(k) for k in ("week_number", "start_date", "end_date", "total_participants",
                                    "total_distributed", "total_burned", "net_tokens_issued")}
    out: dict[str, Any] = {"week": agg, "benchmark": scrub(data.get("benchmark")), "found": bool(mine)}
    if mine:
        r = mine[0]
        out["mine"] = scrub(r)
        # The API calls the consistency score S "sharpe_ratio"; it is not a Sharpe ratio.
        out["formula_check"] = {
            "H0_previous_tokens": r.get("previous_token_balance"),
            "R_weekly_return_pct": r.get("weekly_return_pct"),
            "S_consistency": r.get("sharpe_ratio"),
            "dH_computed": round((r.get("previous_token_balance") or 0) * (r.get("weekly_return_pct") or 0) / 100
                                 * (r.get("sharpe_ratio") or 0), 4),
            "dH_reported": r.get("return_based_reward"),
        }
    return out
