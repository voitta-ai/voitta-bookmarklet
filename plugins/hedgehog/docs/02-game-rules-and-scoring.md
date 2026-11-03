# Hedgehog Camp game: rules and how it is actually scored

Two systems score the same trading:

1. **StockTrak** (the "HCamp Game Engine", `app.stocktrak.com`, client `mit-hcamp`, session
   "MIT 2026 Trading Simulation Game", tournament id 358408) ranks players daily by portfolio return.
2. **Hedgehog Camp** (`hedgehogcamp.ai`) converts each week's StockTrak result into **H-Tokens**
   (an ERC-20 on the Base Sepolia testnet). Tokens are the currency of the platform: they rank the
   token leaderboard and pay for job applications at partner firms.

The H-Token formula below was **verified against the public weekly reward data: 4,286 player-weeks over
35 weeks (2026-03-02 → 2026-10-30), exact match on every row.**

## Game rules

| Rule | Value | Source |
|---|---|---|
| Tournament | 3/1/2026 – 12/31/2026 (H Camp). StockTrak's session runs 03/02/2026 09:30 → 10/01/2035. | hedgehogcamp.ai/rules, StockTrak session rules |
| Starting capital | $1,000,000 virtual cash | both |
| Universe | US stocks & ETFs, plus options (StockTrak enables Options) | both |
| Leverage | 2× (buying power $2,000,000); margin and short selling allowed | both |
| Interest | 0% on cash and on loans | both |
| Market data | 15-minute delayed | H Camp |
| Trade notes | **Required** ("high-quality trade notes distinguish luck from disciplined strategy") | H Camp |
| Trades | Unlimited per H Camp; StockTrak caps at 10,000 per session | both |
| Fractional shares | Not allowed | StockTrak rules |
| Commission | $0 stocks; rules say $10 per option trade, but **$0 was charged on every fill in testing** | StockTrak rules / test 2026-11-03 |
| Min marginable price | $3.00; position limits 200% per security | StockTrak rules |
| Day trading | Allowed. Orders entered while the market is closed execute at the next open. | StockTrak |
| Rankings | Daily (StockTrak), by % return; weekly token settlement (H Camp) | both |

## H-Token scoring (verified)

Each weekly period (one trading week, Friday close to Friday close; dated Sunday → Friday from week 3
on, e.g. 10/25–10/30/2026; week 1 was 3/2–3/6) the admin uploads StockTrak balance exports; the
Rewards API computes, per player:

```
R  = (end_balance − start_balance) / start_balance × 100      weekly return, % (exact on 4,276/4,286 rows)
S  = consistency score in [−1, 1]                              API field name: "sharpe_ratio" (it is NOT a Sharpe ratio)
H₀ = previous_token_balance                                    tokens held going into the week

dH = H₀ × (R / 100) × S                                       "return_based_reward" (exact on 4,286/4,286 rows)
H₁ = H₀ + participation_reward + dH
```

- **S** per Hedgehog Camp (and the MIT 18.642 investment-game assignment it came from) is
  **S = (G − L) / (G + L)**, with G = sum of daily gains and L = sum of daily losses. +1 means every
  day was up, −1 every day down. (The data confirms S ∈ [−1, 1] and multiplies exactly; the daily
  inputs aren't public, so the G/L definition is taken from the rules page.)
- **Starting allocation:** 100 H, booked as `participation_reward = 100` in a player's first week
  (73 cases, all with `initial_mint_tokens = 100`). Otherwise `participation_reward = 0`
  (`/config`: `participation_reward: 0`, `competition_initial_token_balance: 100`).
- **Settlement:** positive dH is minted from the treasury, negative dH is burned (4,009/4,009 rows).
  The balance never went to zero in the data (lowest 32 H).
- **Compounding:** dH is proportional to H₀, so the token balance compounds like a portfolio.
- **Benchmark** shown alongside: S&P 500 price return (FRED), as `excess_return_pp`. It does not enter dH.
- Players per week: 28 → 168 as the field grew.

### What the formula rewards

dH has the sign of **R × S**, not of R:

| Week | R | S | dH |
|---|---|---|---|
| Steady gains | + | near +1 | **largest gain** (≈ H₀ × R) |
| One big up day, several down days | + | < 0 | **tokens lost** despite a profit (454 of 454 such weeks) |
| Steady losses | − | < 0 | **tokens gained** despite a loss (868 of 868 such weeks, +877 H in total) |
| Choppy loss with a few big up days | − | > 0 | tokens lost |

Practical reading:
- The game rewards **smooth, consistent** daily P&L, not just total return. A +2% week made of five
  small up days beats a +5% week made of one jump and four dips.
- Because of the R × S sign, **a consistently losing week currently earns tokens.** That is a flaw in
  the scoring, not a strategy: losing on purpose games the system, could be fixed retroactively, and
  undermines the job-application signal the tokens exist for. Report it; don't exploit it.
- Volatility drag on S: big swings in both directions push S toward 0, which shrinks dH whatever R is.

## Eligibility and identity

- Rewards need the **same username** in the Hedgehog Camp portal (`portal.hedgehogcamp.ai`) and in
  StockTrak. Portal-created players appear as `portal:<username>` until the StockTrak account is linked.
- Status "Not eligible yet" = no linked StockTrak results in an uploaded week. Results appear after the
  admin's next weekly upload.
- Referral bonus: +50 H (site announcement).

## What tokens buy

- **Job applications:** 20 H per firm application via the portal (9 partner firms, 37 open roles at the
  time of writing). With 0 H you can't apply.
- Tokens are transferable ERC-20s on chain (contract `0xD0d5…A326`, "Hedgehog Token", HTOKEN), even
  though the site calls them non-monetary.

## StockTrak ranking

`hedgehog_my_rank` returns `rank / players` (e.g. `0 / 168` = not ranked yet: no filled trade).
Ranking type "Overall" sorts by % return on the $1M start. "Trades Made" counts **filled** legs only;
an option exercise counts as 2.

## Origin: MIT 18.642 investment game

The S score comes from the optional investment game in MIT 18.642 *Topics in Mathematics with
Applications in Finance* (Fall 2024; Kempthorne, Strela, Xia): $10,000 hypothetical, one position held
from a Friday close to a later Friday close (one switch allowed), track daily P&L, report total P&L
and (G − L)/(G + L). Hedgehog Camp scales that up: a live simulator, weekly settlement, tokens.
See docs/06-mit-course.md.
