# Hedgehog plugin: flows

Each flow lists the tool calls in order. All were exercised live on 2026-11-02/03.

## 0. Every session
1. `hedgehog_page_context` → `signed_in`, `username`, `market_open`.
2. Not signed in / `session_expired` → ask the user to sign in at app.stocktrak.com ("Remember me"), retry once.

## 1. "How am I doing?"
1. `hedgehog_account` → portfolio value, % return, cash, buying power, trades remaining.
2. `hedgehog_positions(equity)` and `hedgehog_positions(option)`.
3. `hedgehog_my_rank` → StockTrak rank.
4. `hedgehog_rewards(section="my_standing", username)` → token balance and token rank; then
   `hedgehog_rewards(section="weeks")` and `my_week` for the latest week to explain dH = H₀ × R × S.

## 2. Buy or sell a stock / ETF
1. `hedgehog_quote(symbol)` (delayed price).
2. `hedgehog_preview_order({security_type: "equity", symbol, side: "buy", quantity, order_type, …, trade_note})`.
3. Show the user `summary` + `estimated_cost`.
4. `hedgehog_place_order(preview_id)`: the user confirms in the chat card.
5. `hedgehog_orders(view="open")`: pending orders; market orders in market hours fill within seconds
   (then they're in `hedgehog_positions`). Orders entered while closed fill at the next open.

Closing a long: side `sell`. Short: `short`, then `cover` to close (cover is accepted even if the short
isn't visible in positions yet).

## 3. Options
1. `hedgehog_option_chain(symbol)` → expirations.
2. `hedgehog_option_chain(symbol, expiration, option_type)` → strikes.
3. `hedgehog_option_chain(symbol, expiration, option_type, strike)` → quote + `OptionSymbol`.
4. Preview with `security_type: "option"`, `side: buy_to_open | sell_to_open | sell_to_close | buy_to_close`,
   `option_type, expiration, strike`. Quantity is in contracts (×100).
5. Place as above.

Naked selling (`sell_to_open` without stock) is allowed. `sell_to_close` / `buy_to_close` without a
matching position is rejected by StockTrak.

## 4. Option strategies (spreads)
1. `hedgehog_spread_template(spread_type, symbol, quantity)` → legs (side, quantity, option_type, needs_strike).
2. Pick an expiration and strikes from `hedgehog_option_chain`; fill every leg with `needs_strike`.
3. `hedgehog_preview_spread({spread_type, symbol, expiration, legs, trade_note})`: `cost` (negative = net credit).
4. `hedgehog_place_order(preview_id)`.
5. Each leg becomes its own **market GTC** order. To close a spread later, close each leg separately
   (`sell_to_close` the long leg, `buy_to_close` the short leg). Cancelling one pending leg cancels all.

Verified: Bull Call (6) placed and closed leg by leg; Covered Call (1) placed; Long/Short Straddle,
Long Butterfly Call and Iron Condor previewed.

## 5. Change or cancel a pending order
- Cancel: `hedgehog_orders(view="open")` → `order_id` → `hedgehog_cancel_order(order_id)`.
- Re-price: `hedgehog_orders(view="history")` → `order_conf` → `hedgehog_edit_order(order_conf, price, gtd_date?)`.

## 6. Trade notes
Notes go in with the order (`trade_note`, required). To add one later:
`hedgehog_orders(view="history")` → `order_conf` → `hedgehog_add_trade_note(order_conf, note)`.
Write the reasoning (thesis, risk, exit plan), not filler.

## 7. Exercise an option
1. `hedgehog_positions(security_type="option")` → `security_id`, `underlying_security_id`, symbol.
2. `hedgehog_exercise_option(security_id, underlying_security_id, option_symbol, quantity)`.
   The tool checks moneyness and refuses out-of-the-money exercises (StockTrak would still deliver
   shares at the strike). The user confirms in the chat.
3. Shares arrive at the strike; booked as option "Sell @ $0" + stock buy; counts as 2 trades.
Selling the option instead usually keeps its time value; exercise only deep in-the-money near expiry.

## 8. History, P&L and analysis
- Quick look: `hedgehog_history(report="transactions", start_date, end_date)` or `report="closed_positions"`.
- Analysis: `hedgehog_export_csv(report=…)` → python_storage handle → pandas / a voitta report.
- Realized P&L lives on the closing row of each closed position.

## 9. Scoring questions
Read docs/02-game-rules-and-scoring.md. For a specific week:
`hedgehog_rewards(section="my_week", week_id, username)` returns `formula_check` with H₀, R, S and dH.
