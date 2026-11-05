# Hedgehog plugin: tool catalog

Host-gated to `stocktrak.com` (the game engine, `app.stocktrak.com`) and `hedgehogcamp.ai` (the rewards
platform). StockTrak tools need the bookmarklet open **on the app.stocktrak.com tab**: every call is a
same-origin request from that tab with the user's own session (no credentials leave the browser). On
hedgehogcamp.ai only `hedgehog_rewards` works; the others return `wrong_host`.

## Always first

| Tool | Returns |
|---|---|
| `hedgehog_page_context` | `on_stocktrak, page_type, signed_in, username, account_id, tournament_id, market_open, market_message` |

`signed_in: false` or any `session_expired` error → ask the user to sign in at app.stocktrak.com with
"Remember me" ticked (sessions without it expire overnight), then retry once.

## Read tools (always available)

| Tool | Purpose | Key args |
|---|---|---|
| `hedgehog_account` | Cash, long/short market value, portfolio value, % return, buying power, loan, trades made / allowed / remaining (StockTrak's summary CSV, as numbers). Adds `as_of_et`, `checks` (long − short + cash − loan vs portfolio value, gross leverage), `warnings` (fields from different moments; margin requirement $0.00 with a loan → `null`) and `not_reported_by_stocktrak` (empty fields, e.g. the SPY benchmark ones) | – |
| `hedgehog_positions` | Open positions of ONE type (empty list when none) as numbers + ids for exercise (`security_id`, `underlying_security_id`). `as_of_et`; LastPrice can be the previous close early in the session | `security_type`: equity \| option |
| `hedgehog_orders` | `view=open`: pending orders with `order_id` (cancel). `view=history`: all orders with status and `order_conf` (edit, notes). `quantity` is the ORDERED quantity, even when StockTrak filled fewer; filled orders also carry `filled_qty`, `fill_price`, `fill_time` and `partial` (from the transactions export, matched by symbol + side + time; `fill_match: not_found` if no fill matched), and the result lists `partial_fills`. Quantities and prices are numbers (a masked `$XXX.xx` stays a string) | `start_date, end_date, status, page, page_size` |
| `hedgehog_history` | CSV-backed history with real prices: `transactions`, `closed_positions` (realized P&L), `open_positions`, `portfolio_summary`, `trade_notes`, `historical_open_positions` | `report, start_date, end_date, date, limit` |
| `hedgehog_export_csv` | Same reports saved to python_storage (returns a `handle`) for analysis / voitta reports | same as above |
| `hedgehog_quote` | Delayed last price (a number) + quote panel text for a US stock/ETF | `symbol` |
| `hedgehog_symbol_search` | Ticker / company lookup | `query` |
| `hedgehog_option_chain` | symbol → expirations; + expiration → strikes; + strike → contract quote incl. `OptionSymbol` | `symbol, expiration, option_type, strike` |
| `hedgehog_spread_template` | Leg template for strategy 1–22 | `spread_type, symbol, quantity` |
| `hedgehog_my_rank` | Own StockTrak rank (`rank / players`) + top-10 return % (no names) | – |
| `hedgehog_rewards` | Hedgehog Camp H-Tokens: `my_standing`, `my_week` (own R, S, H₀, dH), `weeks`, `config`. Returns `units`; before any credited week (`weeks_participated` 0) S is `null` (`scored: false`, provisional value in `sharpe_ratio_uncredited`) | `section, username, week_id` |
| `hedgehog_watchlist` | list / add / remove (doesn't affect the score) | `action, symbols, security_id` |
| `hedgehog_api_read` | Escape hatch for any allowlisted READ endpoint (docs/03) with raw params. The payload is untouched; known traps come back in `caveat` (e.g. the cumulative Trades column of `dashboardhistoricalportfoliovalues`) | `path, method, params` |

## Trading tools (only when Settings → Plugins → Hedgehog → "Enable trading" is on)

| Tool | Purpose |
|---|---|
| `hedgehog_preview_order` | Validate an equity/option order (game rules + StockTrak preview). Returns `preview_id`, summary, estimated cost, `max_shares` (equity buy/short: what buying power allows) and `warnings` (e.g. order larger than `max_shares`). Places nothing. |
| `hedgehog_preview_spread` | Same for a 1–22 option strategy (legs from `hedgehog_spread_template`). |
| `hedgehog_place_order` | Place a previewed order/spread by `preview_id` (valid 15 min, same session). Asks the user in the chat first unless they turned confirmations off. Returns `order` (what was requested), `quantity` {requested, stocktrak_confirmed (parsed from StockTrak's "Your … for N shares of SYM"), position_before/after, position_change, check: full \| partial \| not_filled_yet \| unexpected \| unavailable}, `trade_note_saved` and `warnings`. |
| `hedgehog_cancel_order` | Cancel by `order_id`. Cancelling one spread leg cancels the whole spread. |
| `hedgehog_edit_order` | New price / good-til date for an open limit or stop order (`order_conf`). |
| `hedgehog_add_trade_note` | Attach a trade note to an order (`order_conf`). |
| `hedgehog_exercise_option` | Exercise a held option; refuses out-of-the-money unless `allow_out_of_the_money=true`; asks the user. |

### Order arguments (`hedgehog_preview_order`)

| Field | Values |
|---|---|
| `security_type` | `equity` (stocks, ETFs) \| `option`. Nothing else is allowed in this game. |
| `symbol` | Underlying ticker (also for options), US only |
| `side` | equity: `buy, sell, short, cover`. option: `buy_to_open, sell_to_close, sell_to_open, buy_to_close` |
| `quantity` | Whole number. Shares, or contracts (100 shares each) |
| `order_type` | `market, limit, stop, trailing_stop_pct` (+ `price` or `trailing_pct`) |
| `duration` | `day, gtc, gtd` (+ `gtd_date` MM/DD/YYYY) |
| `option_type, expiration, strike` | options only (from `hedgehog_option_chain`) |
| `trade_note` | **Required**, ≤ 300 chars: why this trade |

## Guardrails (enforced in code)

- Trading tools are invisible unless enabled; the primitive refuses write endpoints unless a trading tool sets `write: true`.
- Whole shares, trade note, allowed security types: checked before StockTrak sees the order (the server itself doesn't check these).
- Place requires a matching, fresh preview; the user confirms via the chat question card (the model can't answer it).
- Exercise checks moneyness first.
- `hedgehog_rewards` never returns other players' rows and scrubs emails / wallets / internal ids.

## Error kinds

| `error` | Meaning / what to do |
|---|---|
| `session_expired` | Signed out. Ask the user to sign in (Remember me), retry once. |
| `wrong_host` | Bookmarklet isn't on app.stocktrak.com. |
| `server_error_page` | StockTrak's "Oops" page: missing/invalid param, or nothing to show. Check docs/03. |
| `rule_violation` | A game rule would be broken (fractional qty, no note, disallowed type…). Fix the order. |
| `rejected_by_stocktrak` | StockTrak said no (e.g. "Sell order requires an existing long position"). |
| `preview_required` | Preview first; place exactly what was previewed. |
| `declined` / `not_confirmed` | The user said no or didn't answer. Don't retry. |
| `trading_disabled` | Ask the user to enable trading in Settings if they want it. |
| `not_in_the_money` | Exercise refused; selling the option is usually better. |
