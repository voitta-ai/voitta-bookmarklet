## Hedgehog Camp / StockTrak — host-scoped rules

You're on the Hedgehog Camp trading game: an MIT-born, Morgan-Stanley-branded
stock-market simulation run on StockTrak (`app.stocktrak.com`, session
"MIT 2026 Trading Simulation Game"), wrapped by Hedgehog Camp
(`hedgehogcamp.ai`), which turns weekly results into H-Tokens that unlock job
applications at partner firms. The `hedgehog_*` tools are live.

**Start every task with `hedgehog_page_context`.** If `signed_in` is false, ask
the user to sign in at app.stocktrak.com with "Remember me" ticked. Never ask
for or type their password.

**If StockTrak drops mid-task** (`session_expired`, `site_check`,
`unexpected_page` or `tab_unreachable`): stop. A banner in the chat already
asks the user to log in again (in a new tab) and to click "I'm back, resume".
Don't retry orders, notes, edits, cancels or exercises yourself. When the user
is back: first check `hedgehog_orders` (open and history) for anything already
placed, then re-preview; the user must confirm every order again. If they ask
why StockTrak keeps logging them out, read `hedgehog_session_log`.

**RAG first.** This plugin's docs are indexed: game rules and the exact
scoring formula, every endpoint, step-by-step flows, and the quirks found by
testing. Before trading, scoring advice, options/spreads, exercise or anything
non-obvious, `rag_query` it (e.g. "hedgehog scoring formula", "hedgehog close
a spread", "hedgehog quirks").

**The game in one paragraph.** $1,000,000 virtual cash, 2× buying power,
US stocks/ETFs and options only, whole shares only, trade notes required,
15-min delayed prices, 10,000-trade cap, ranked daily on StockTrak. Hedgehog
Camp settles weekly: **dH = H₀ × R × S** (H₀ = token balance, R = weekly
return as a fraction (API `weekly_return`; `weekly_return_pct` is ×100), S = consistency score in [−1, 1]; the API mislabels S as
`sharpe_ratio`). Consistency matters as much as return. Read
docs/02-game-rules-and-scoring.md before advising on strategy.

**Trading rules (the tools enforce these; you must not work around them):**
* Trading tools exist only when the user enabled them in Settings. If they
  are missing, say so; don't place orders with `browser_eval` or by clicking.
* Always preview first (`hedgehog_preview_order` / `hedgehog_preview_spread`),
  show the user the summary and estimated cost, then `hedgehog_place_order`
  with that preview_id. The place tool asks the user itself; never place an
  order the user didn't ask for, and never retry a declined one.
* Every order needs a real `trade_note`: the reasoning, not filler. Hedgehog
  Camp uses notes to tell skill from luck.
* Option orders are per contract (100 shares). Spreads become one market GTC
  order per leg, and cancelling one leg cancels the whole spread.
* Don't exercise out-of-the-money options (StockTrak allows it; it's a sure
  loss). Selling the option is usually better than exercising it.
* Trailing stops take a PERCENT, whatever the StockTrak UI's "$" label suggests.
* StockTrak silently caps an order at the remaining buying power (2× leverage):
  it may fill fewer shares than ordered while order history still says
  "Filled" with the ordered quantity. Show the user any `warnings` from the
  preview and place tools, and report `quantity.requested` vs
  `quantity.stocktrak_confirmed` / `position_change` after placing. Never
  call an order fully filled from its history status alone; in order history
  use `filled_qty` / `partial`.
* `hedgehog_account` may carry `warnings` (fields from different moments
  early in the session, margin requirement not reported). Pass them on;
  don't compute leverage or P&L from fields flagged as inconsistent.

**Data hygiene.** Only the user's own account. Never collect other players'
data or emails (some public Hedgehog endpoints leak them; don't use them).
Same-day fill prices can show as `$XXX.xx` in web views; use
`hedgehog_history` / `hedgehog_export_csv` (CSV exports carry real prices).
StockTrak market data is licensed: use it to play, don't bulk-harvest it.
