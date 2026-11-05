# Hedgehog plugin: quirks found by testing

Behaviour observed on the live game (2026-11-02/03) that differs from the rules or from what the UI suggests.

## Rules the StockTrak server does not enforce
| Rule | Server behaviour | Plugin |
|---|---|---|
| No fractional trading | A 0.5-share order was accepted | `rule_violation` before sending |
| Trade notes required | Orders without a note are accepted (only the page JS checks) | note required |
| Only Equities + Options | `previeworderv2` accepts a mutual-fund order; placement untested | other types refused |
| $10 option commission | $0 charged on every fill (equities and options) | – |
| Exercise only when in the money | Out-of-the-money exercise is accepted and delivers shares at the strike | refused unless forced |

## Order mechanics
- **Orders are silently capped at the remaining buying power** (seen 11/4/2026 at 1.95× leverage):
  BUY 770 SPY → StockTrak's confirmation said "Your Market Buy for 573 shares of SPY" and the position grew
  by 573, yet order history shows quantity 770, status `Filled` (QQQ: 560 ordered, 288 filled, history
  "Filled 560"). History status is not proof of a full fill. The plugin checks `getmaxshares` at preview,
  parses the confirmed quantity at placement and compares the position change with the order;
  `hedgehog_orders(view=history)` joins the transactions export in (it has no order id, so fills are
  matched by symbol, side and time) and adds `filled_qty` / `partial`.
- **Trailing stop is always a percent.** The UI offers "$" and "%", but both post `OrderType=4`; a value
  of 50 became a stop at last × 1.5.
- `OrderType=5` → server error.
- Market orders entered while the market is closed queue and fill at the next open (09:30–09:33 ET seen).
- Pending orders don't reduce cash or buying power; "Trades Made" counts **fills** (each spread leg is one).
- "Trades Made/Allowed" is made / **remaining**: 18/9982 on 11/4, 26/9974 on 11/5 (the second number counts
  down from 10,000). `hedgehog_account` returns `trades_made`, `trades_remaining` and `trades_allowed` = their sum.
- An option exercise counts as **2 trades**.
- A spread is placed as separate market GTC leg orders; cancelling any pending leg cancels all legs.
- A new short may not appear in open positions for several seconds, but `cover` is accepted immediately.
- Transactions show two "Short Proceeds" cash rows per short; short-side rows report the exchange as `Equities` (data bug).

## Display
- **Same-day fill prices are masked as `$XXX.xx`** in order history, positions and transactions until later
  in the day. CSV exports show real prices immediately; the plugin's history tools use them.
- Order history rows carry two links: `a.edit-order` (numeric order id + conf GUID, open orders only)
  and `a.view-trade-notes` (whose `data-order-id` is the conf GUID). The user-visible confirmation number
  is the conf GUID's last 8 hex characters, uppercased.
- Open-orders cancel id is `a.cancelorder[data-status]` (not `data-order-id`).

## Account data (seen 11/5/2026, 09:40 ET)
- **The portfolio-summary CSV mixes moments early in the session**: Market Value of Long Positions was priced
  live while Portfolio Value was still the previous close (long − loan + cash was $31.9k off), and the
  open-positions CSV still showed previous-close prices. `hedgehog_account` checks the identity
  long − short + cash − loan = portfolio value and warns when it doesn't hold.
- **Margin Requirements reads $0.00** even with a $962k loan at 1.95× leverage: not filled in by StockTrak.
  The plugin returns `null` with a warning instead of a reassuring zero.
- The SPY benchmark fields (`SPY ETF % Return`, `SPY ETF at start date`, `Current SPY ETF`) are always empty;
  the plugin drops empty fields and lists them in `not_reported_by_stocktrak`.
- Labels carry typos (`Accured Interest`, `Net Matket Value`); the plugin returns them corrected.
- `Interest Earn Rate` is formatted as dollars (`$0.00`) though it is a rate; the plugin returns plain numbers
  everywhere (money to cents, prices to 4 dp, percentages in percent units).
- `/admin/ExportOpenPositions/?securityType=Options` returns the equity rows too; the plugin keeps only
  option symbols for options and drops them for equities.
- `dashboardhistoricalportfoliovalues`: the Trades column is cumulative, not per day.

## Endpoints
- `previeworderv2` without `CompanyName`, `Currency` and `UnitPrice` → `{"Success": false, "ErrorMessage": ""}`
  (no reason given). Its OrderInfoHtml shows the trade-note counter as `0 / 300` even when a note was sent:
  the template renders before the note is attached. The plugin strips the counter and, after placing, looks
  the note up in the trade-notes export (`trade_note_saved`).
- Quote and preview fragments embed page JavaScript (`getpriceforslider`, `setupCharacterCounter`); the
  browser-side parser drops `<script>`/`<style>` before extracting text.
- `gettransactions` without `transactionType` → error page; use 1.
- `getclosepositions` needs a specific `securityID` (0 → "no closed positions").
- `gettrimmedopenpositionsV2` needs `securityType + portfolioID + accountID`; error page when that type is empty.
- `getstrikeprice` returns `<option value=…>` only with `isOptionSpreads=true`; otherwise text-only options.
- The "Oops, something went wrong" page comes back with HTTP 200: the usual sign of a bad parameter.
- No rate-limit headers; pace requests (the plugin never bursts).

## Session
- The login cookie expires overnight unless "Remember me" is ticked; requests then redirect to `/login`.

## Hedgehog Camp
- The scoring API field `sharpe_ratio` is the consistency score S = (G − L)/(G + L), not a Sharpe ratio.
- Units: `weekly_return` is a fraction (0.0144) and is R in dH = H₀ × R × S; `weekly_return_pct` is the same in
  percent (1.44). `total_return_pct` and `benchmark_return_pct` are percent; `excess_return_pp` is points.
- Players with `weeks_participated = 0` mostly have `sharpe_ratio = null`, but some carry a provisional value
  from partial data (−1.0 seen, which reads as "every day was a loss"). It is not used for rewards until a
  week is credited; `hedgehog_rewards` returns S as `null` with `scored: false` for them.
- dH = H₀ × R × S has the sign of R × S: consistently losing weeks earn tokens, profitable but choppy weeks
  lose them (see docs/02). A scoring flaw to report, not a strategy.
- Players created in the portal show as `portal:<username>` until linked to StockTrak by identical username.
- Several public Rewards API endpoints return player emails and wallet addresses without auth. The plugin
  never calls them and scrubs any it sees.
