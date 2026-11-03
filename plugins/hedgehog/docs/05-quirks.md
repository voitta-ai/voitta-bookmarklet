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
- **Trailing stop is always a percent.** The UI offers "$" and "%", but both post `OrderType=4`; a value
  of 50 became a stop at last × 1.5.
- `OrderType=5` → server error.
- Market orders entered while the market is closed queue and fill at the next open (09:30–09:33 ET seen).
- Pending orders don't reduce cash or buying power; "Trades Made" counts **fills** (each spread leg is one).
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

## Endpoints
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
- dH = H₀ × R × S has the sign of R × S: consistently losing weeks earn tokens, profitable but choppy weeks
  lose them (see docs/02). A scoring flaw to report, not a strategy.
- Players created in the portal show as `portal:<username>` until linked to StockTrak by identical username.
- Several public Rewards API endpoints return player emails and wallet addresses without auth. The plugin
  never calls them and scrubs any it sees.
