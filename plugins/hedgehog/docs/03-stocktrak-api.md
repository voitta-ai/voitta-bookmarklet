# StockTrak private web API (as used by the plugin)

No public API exists. The web app (ASP.NET MVC, IIS, behind Imperva) calls `/<controller>/<action>`
endpoints that mostly return **JSON wrapping server-rendered HTML** (`{"Html": "<tr>…"}`). The plugin's
`hedgehog_request` primitive parses those fragments in the browser into
`{headers, rows: [{cells, data, els, split?}], options, fields, legs, text}`.

Reverse-engineered and live-tested 2026-11-02/03 (reads, every order type, options, spreads, edit,
cancel, closing trades, exercise, short/cover, exports).

## Conventions

| Thing | Value |
|---|---|
| Auth | Cookie session (`.STMVC-Auth-Prod`). No CSRF token. Expires overnight unless "Remember me". |
| Unauthenticated | Redirect to `/login` → `session_expired` |
| Encoding | Form-encoded (`X-Requested-With: XMLHttpRequest`); spreads use JSON |
| Errors | Often HTTP 200 with the HTML page "Oops, something went wrong" → `server_error_page` (bad/missing params, or nothing to show) |
| Success flag | `Success` mostly; `success` on getEstimatedCost, getmaxshares, PreviewOptionSpreadsV2; `IsSuccess` on savetradingnote |
| Dates | `M/D/YYYY` |
| Exchange | `US` only in this session |

## Enumerations

- **OrderSide** equities: 1 Buy, 2 Sell, 3 Short, 4 Cover. Options: 1 Buy to open, 2 Sell to close, 3 Sell to open, 4 Buy to close.
  Spread legs use strings `Buy` / `Short`.
- **OrderType**: 1 Market, 2 Limit, 3 Stop, 4 Trailing stop (**value is a percent**; the UI's "$" variant posts the same 4). 5 → server error.
- **OrderExpiration**: 1 Day, 2 GTC, 3 Good-til-date (`ExpirationDate`).
- **SecurityType**: `Equities`, `Options` (allowed); `MutualFunds, Bonds, Futures, FutureOptions, Spots, CashSpots` exist but are disallowed here.
- **Option symbol**: ROOT + YY + DD + month letter + strike. Calls A–L = Jan–Dec, puts M–X = Jan–Dec.
  `AAPL2611K337.5` = AAPL 11/11/2026 337.5 call; `AAPL2611W337.5` = the put; `F2620K12.5` = F 11/20/2026 12.5 call.
- **OptionSpreadType** 1–22: 1 Covered Call, 2 Covered Put, 3 Married Call, 4 Married Put, 5 Bear Call,
  6 Bull Call, 7 Bear Put, 8 Bull Put, 9 Long Straddle, 10 Short Straddle, 11 Long Strangle,
  12 Short Strangle, 13/14 Long/Short Butterfly Call, 15/16 Long/Short Butterfly Put, 17 Collar,
  18/19 Long Condor Call/Put, 20/21 Short Condor Call/Put, 22 Iron Condor.

## Order form (previeworderv2 / placeorderv2)

Equity: `Symbol, OrderSide, Quantity, OrderType, Price (0 for market), OrderExpiration, ExpirationDate,
SecurityType=Equities, CompanyName, Currency=USD, PrimaryMarket, Exchange=US, QuantityType=Amount,
UnitPrice, SelectedRegion=0, TradeNotes`.
Option: same minus PrimaryMarket/QuantityType/UnitPrice/SelectedRegion, plus `OptionType (Call|Put),
OptionExpiry, OptionStrikePrice, OptionSymbol`, `SecurityType=Options`.

Spread (PreviewOptionSpreadsV2 / PlaceOptionSpreadsV2, JSON):
```json
{"OptionSpreadType": "6",
 "Orders": [{"Symbol": "F", "Quantity": "1", "OrderSide": "Buy",   "OptionType": "Call", "OptionStrikePrice": "12", "OptionExpiryDate": "11/20/2026", "Exchange": "US"},
            {"Symbol": "F", "Quantity": "1", "OrderSide": "Short", "OptionType": "Call", "OptionStrikePrice": "13", "OptionExpiryDate": "11/20/2026", "Exchange": "US"}],
 "TradeNotes": "…"}
```
Stock legs (covered/married): `OptionType ""`, `OptionStrikePrice ""`, quantity 100 per contract.
Preview adds `"isOrderPreview": true` and omits TradeNotes.

## Endpoints on the plugin allowlist

### Trading (reads)
| Method | Path | Params | Returns |
|---|---|---|---|
| POST | /trading/getpricev2 | symbol, exchange, securityType, monthCode, year, equitiesType=All, isOptionSpreads=false | Success, Symbol, CompanyName, LastPrice, Currency, Exchange, TradingHalted, symbolPrice(html) |
| POST | /trading/ismarketopen | tab=NorthAmerica, exchange=US | Success, IsOpen, Message |
| GET | /trading/getmaxshares | OrderSide, SecurityType, Symbol, Price | success, maxshares |
| POST | /trading/getEstimatedCost(ForOptions) | order form | success, estimatedcost, costvalue |
| POST | /trading/previeworderv2 | order form | Success, ErrorMessage, OrderInfoHtml (validation; places nothing) |
| POST | /trading/getoptionexpiry | symbol | Html with `<option>M/D/YYYY</option>` |
| POST | /trading/getstrikeprice | symbol, optionType, expiration, isFullList (or isOptionSpreads=true → options carry value attrs) | Html options |
| POST | /trading/getoptionpricev2 | symbol, exchange, optionType, expiration, strikePrice | Html: line-text fields + OptionSymbol |
| POST | /trading/GetOptionSpreadLegs | type 1–22, quantity, symbol | Html: `.option-spread-leg-form` per leg |
| POST | /trading/PreviewOptionSpreadsV2 | JSON (above) | success, html, cost, estimatedCost, OrderInfoHtml |
| POST | /trading/GetPayoffDiagram | prm… names | chart data |

### Trading (writes; trading tools only)
| Method | Path | Params | Notes |
|---|---|---|---|
| POST | /trading/placeorderv2 | order form | Places. Market orders in market hours fill in seconds. |
| POST | /trading/PlaceOptionSpreadsV2 | JSON + TradeNotes | One market GTC order per leg |
| POST | /trading/EditOrder | orderConf (GUID), price, expiryDate | |
| POST | /account/cancelorder | orderID (open-orders `a.cancelorder[data-status]`) | Cancels the whole spread if one leg |
| POST | /account/exerciseoption | securityID, quantity | 100 shares/contract at strike; no ITM check; 2 trades |
| POST | /account/savetradingnote | orderConf, note | IsSuccess |
| POST | /dashboard/addwatchlist, /dashboard/removewatchlist | symbols+exchange / securityID | |

### Account (reads)
| Method | Path | Params | Notes |
|---|---|---|---|
| GET | /account/getaccountsnapshotv2 | – | Html snapshot |
| GET | /account/openpositionsbysecuritytypeV2 | securityType, pageIndex, pageSize, sortField, sortDirection | rows carry data-option-symbol, data-securityid, data-underlying-securityid, data-quantity |
| GET | /account/gettrimmedopenpositionsV2 | securityType, portfolioID, accountID (+paging) | error page if that type holds nothing |
| GET | /account/openpositionsmoredataV2 | type, symbol, exchange | {HTML} fundamentals / contract details |
| GET | /account/getoptiondata | symbol (option), exchange, underlyingSecurityID, quantity | OptionStrikePrice, StockLastPrice, TotalShares, TotalCost |
| GET | /account/openorders | v2=true | rows: cancel id in data-status |
| GET | /account/getorderhistory | pageIndex, pageSize, startDate, endDate, sortField, sortDirection, status, v2 | bare `<tr>` rows; a.edit-order (id, conf), a.view-trade-notes (conf GUID) |
| GET | /account/gettransactions | …, transactionType=1 (required), v2 | |
| GET | /account/getclosepositions | portfolioID, **securityID (specific; 0 = nothing)**, pageIndex, pageSize, v2 | bare rows, open/close split by `<br>` |
| GET | /account/getsecuritytype | accountID | [{portfolioID, securityTypeID}] (1 Equities, 2 Options) |
| GET | /account/getsecurities | portfolioID | [{SecurityID, Symbol}] ever ordered |
| GET | /account/getrankingsv2 | pageIndex, pageSize, rankingType=Overall, date, filterType | UserRankHtml "rank / N"; rows data-percentage |
| GET | /account/getgraphportfolioV2 | symbol | empty when flat |
| GET | /account/dashboardhistoricalportfoliovalues | – | {table}: one EOD row per day |
| GET | /account/gettradenotes, /account/gettradingnotesbyorder | paging / orderConf | |

### CSV exports (structured, real prices)
| GET | Params | Content |
|---|---|---|
| /account/ExportToExcelPortfolioSummary | date | label/value pairs across columns: cash, market values, portfolio value, % return, buying power, Trades Made/Allowed |
| /admin/ExportOpenPositions/ | securityType | Symbol, Description, Quantity, Currency, LastPrice, PricePaid, DayChange, ProfitLoss, MarketValue, ProfitLossPercentage |
| /admin/ExportTransactionHistory | from, to | Symbol, TransactionType, CompanyName, Exchange, Quantity, FXRate, Currency, SecurityType, Price, Amount, CreateDate |
| /account/exportclosepositions | – | "Realized Gains": DATETIME, SYMBOL, …, ORDER SIDE, QUANTITY, MULTIPLIER, AVG PRICE, …, PROFIT LOSS PER POSITION |
| /account/ExportTradeNotes | from, to | trade notes |
| /account/ExportToExcelHistoricalOP | date | historical open positions |
| /dashboard/ExportPieChartData | chartType (lowercase) | allocation |

### Dashboard / research (reads)
`/dashboard/getdataforpiechart` (structured allocation), `/dashboard/getwatchlist`,
`/dashboard/getmarketsummary`, `/gettopgainers`, `/gettoplosers`, `/dashboard/StandardDashboardAssignments`,
`/quotes/FullTextSeachSymbol` (sic; quote, exchanges, securityTypes, equitiesType → [{Symbol, Company, …}]).

Research pages (`/quotes/quotes?type=…`) load QuoteMedia QMod widgets (licensed data, webmaster 101185).
The plugin doesn't call QuoteMedia; use StockTrak's own price endpoints.

## Hedgehog Camp Rewards API (public)

Base `https://d2hmf7qxwvlr38.cloudfront.net`, FastAPI, spec at `/openapi.json`. Used by `hedgehog_rewards`:
`/config`, `/api/v1/rewards/weeks?limit≤100`, `/api/v1/rewards/weeks/{id}/preview`,
`/api/v1/rewards/leaderboard?sort_by=token_balance&limit`. Writes need a server-held `X-Api-Secret`.
