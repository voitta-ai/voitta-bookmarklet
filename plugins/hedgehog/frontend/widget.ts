// Hedgehog Camp plugin: frontend primitives for app.stocktrak.com.
// Side-effect import; voitta core globs every plugin's frontend/widget.ts
// and bundles it into widget.js.
//
//   • hedgehog_inspect_page   — which StockTrak page, signed in?, username,
//                                account / tournament ids
//   • hedgehog_request        — one same-origin AJAX call to StockTrak,
//                                path-allowlisted, response parsed to JSON
//
// Auth model: the user's StockTrak session cookie never leaves the browser.
// Calls go out from this tab with the page's own cookies (same-origin
// fetch); only the parsed response crosses the bridge. A redirect to
// /login surfaces as PrimitiveError("session_expired").

import { PrimitiveError, registerPrimitive } from "../../../frontend/src/lib/bridge";
import { isErrorPage, isLoginPage, parseCsv, parseHtml, parseJsonBody } from "./parsers";


// ---- allowlist ------------------------------------------------------------
//
// Read endpoints are prefix-matched. Write endpoints (anything that changes
// orders, positions or account state) are exact-matched and only run with
// write: true, which the backend sets after its own guardrails pass.

const READ_PREFIXES = [
  "/trading/getpricev2", "/trading/ismarketopen", "/trading/getmaxshares",
  "/trading/getestimatedcost", "/trading/previeworderv2", "/trading/previewoptionspreadsv2",
  "/trading/getoptionexpiry", "/trading/getstrikeprice", "/trading/getoptionpricev2",
  "/trading/getoptionspreadlegs", "/trading/getpayoffdiagram", "/trading/getexchangeinfo",
  "/trading/getlogo",
  "/account/getaccountsnapshotv2", "/account/openpositionsbysecuritytypev2",
  "/account/gettrimmedopenpositionsv2", "/account/openpositionsmoredatav2",
  "/account/historicalopenpositions", "/account/getclosepositions", "/account/gettransactions",
  "/account/getorderhistory", "/account/openorders", "/account/getoptiondata",
  "/account/hasopenposition", "/account/getsecuritytype", "/account/getsecurities",
  "/account/getrankingsv2", "/account/getgraphportfoliov2",
  "/account/dashboardhistoricalportfoliovalues", "/account/gettradenotes",
  "/account/gettradingnotesbyorder", "/account/getuserbadgesprogress",
  "/account/exporttoexcelportfoliosummary", "/account/exportclosepositions",
  "/account/exporttoexcelhistoricalop", "/account/exporttradenotes",
  "/admin/exportopenpositions", "/admin/exporttransactionhistory",
  "/dashboard/getdataforpiechart", "/dashboard/exportpiechartdata", "/dashboard/getwatchlist",
  "/dashboard/standarddashboardassignments", "/dashboard/getmarketsummary",
  "/dashboard/gettopgainers", "/dashboard/gettoplosers",
  "/quotes/fulltextseachsymbol",
  "/account/closedpositions", // full page: carries the ThisAccount.AccountID hidden input
];

const WRITE_PATHS = new Set([
  "/trading/placeorderv2", "/trading/placeoptionspreadsv2", "/trading/editorder",
  "/account/cancelorder", "/account/exerciseoption", "/account/savetradingnote",
  "/dashboard/addwatchlist", "/dashboard/removewatchlist",
]);

const MAX_ROWS = 500;

function onStockTrak(): boolean {
  return /(^|\.)stocktrak\.com$/i.test(location.hostname);
}

function requireStockTrak(): void {
  if (!onStockTrak()) {
    throw new PrimitiveError(
      "wrong_host",
      `this tab is ${location.hostname}; StockTrak tools need the bookmarklet open on app.stocktrak.com`,
    );
  }
}

function normPath(p: string): string {
  // Keep the caller's exact path for the request (some routes were only
  // verified with a trailing slash); the allowlist key drops it.
  const path = String(p || "").split("?")[0].split("#")[0];
  if (!path.startsWith("/") || path.includes("..") || path.startsWith("//") || path.includes("\\")) {
    throw new PrimitiveError("bad_path", `path must be a site-relative path, got ${JSON.stringify(p)}`);
  }
  return path;
}

function capRows<T>(rows: T[]): { rows: T[]; truncated: boolean } {
  return rows.length > MAX_ROWS ? { rows: rows.slice(0, MAX_ROWS), truncated: true } : { rows, truncated: false };
}


// ---- hedgehog_inspect_page ------------------------------------------------

function classify(path: string): string {
  const p = path.toLowerCase();
  if (p === "/login") return "login";
  if (p.startsWith("/dashboard")) return "dashboard";
  if (p.startsWith("/trading/optionspreads")) return "trade_option_spreads";
  if (p.startsWith("/trading/options")) return "trade_options";
  if (p.startsWith("/trading/")) return "trade_equities";
  if (p.startsWith("/account/portfoliosummary")) return "portfolio_summary";
  if (p.startsWith("/account/")) return "account";
  if (p.startsWith("/quotes")) return "research";
  if (p.startsWith("/content") || p.startsWith("/assignment")) return "learning";
  return "other";
}

registerPrimitive("hedgehog_inspect_page", async (rawArgs) => {
  const resolveIds = Boolean(rawArgs?.resolve_ids ?? true);
  const base: Record<string, unknown> = {
    url: location.href,
    host: location.hostname,
    on_stocktrak: onStockTrak(),
    page_type: onStockTrak() ? classify(location.pathname) : "not_stocktrak",
    title: document.title,
  };
  if (!onStockTrak()) return base;

  const username = document.querySelector("#header-username")?.textContent?.trim() || null;
  base.username = username;
  base.signed_in = Boolean(username) && classify(location.pathname) !== "login";
  base.game_name = document.querySelector(".tournament-name, .tournament-dropdown .current")?.textContent?.trim() || null;

  const fromDom = (document.querySelector('input[name="ThisAccount.AccountID"]') as HTMLInputElement | null)?.value;
  let accountId = fromDom || null;
  let tournamentId: string | null = null;
  const tm = document.documentElement.innerHTML.match(/tournamentID=' \+ (\d+)/);
  if (tm) tournamentId = tm[1];

  if (resolveIds && (!accountId || !tournamentId)) {
    // One page fetch carries both ids; also tells us if the session is alive.
    const r = await fetch("/account/closedpositions", { credentials: "same-origin" });
    const html = await r.text();
    if (isLoginPage(r.url, html)) {
      base.signed_in = false;
      return base;
    }
    accountId = accountId || html.match(/name="ThisAccount\.AccountID"[^>]*value="(\d+)"/)?.[1] || null;
    tournamentId = tournamentId || html.match(/tournamentID=' \+ (\d+)/)?.[1] || null;
    base.signed_in = true;
  }
  base.account_id = accountId;
  base.tournament_id = tournamentId;
  return base;
});


// ---- hedgehog_request -----------------------------------------------------

registerPrimitive("hedgehog_request", async (rawArgs) => {
  requireStockTrak();
  const method = String(rawArgs?.method || "GET").toUpperCase();
  if (method !== "GET" && method !== "POST") throw new PrimitiveError("bad_method", `method ${method} not allowed`);
  const path = normPath(String(rawArgs?.path || ""));
  const key = path.toLowerCase().replace(/\/+$/, "");
  const write = Boolean(rawArgs?.write);

  if (WRITE_PATHS.has(key)) {
    if (!write) throw new PrimitiveError("write_not_authorised", `${path} changes the account; call it through a hedgehog trading tool`);
  } else if (!READ_PREFIXES.some((p) => key === p || key.startsWith(p + "/"))) {
    throw new PrimitiveError("path_not_allowed", `${path} is not on the hedgehog plugin allowlist`);
  }

  const params = (rawArgs?.params || {}) as Record<string, unknown>;
  const jsonBody = rawArgs?.json_body;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    if (Array.isArray(v)) v.forEach((x) => qs.append(k, String(x)));
    else qs.append(k, String(v));
  }

  const headers: Record<string, string> = { "X-Requested-With": "XMLHttpRequest" };
  let url = path;
  let body: string | undefined;
  if (method === "GET") {
    if ([...qs].length) url += "?" + qs.toString();
  } else if (jsonBody !== undefined) {
    headers["Content-Type"] = "application/json; charset=utf-8";
    body = JSON.stringify(jsonBody);
  } else {
    headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8";
    body = qs.toString();
  }

  const started = performance.now();
  const r = await fetch(url, { method, headers, body, credentials: "same-origin", cache: "no-store" });
  const ctype = r.headers.get("content-type") || "";
  const text = await r.text();
  const meta = { status: r.status, path, ms: Math.round(performance.now() - started) };

  if (isLoginPage(r.url, text)) {
    throw new PrimitiveError("session_expired", "StockTrak redirected to /login: the session has expired", meta);
  }
  if (/csv/i.test(ctype) || /attachment/i.test(r.headers.get("content-disposition") || "")) {
    const { rows, truncated } = capRows(parseCsv(text));
    const disp = r.headers.get("content-disposition") || "";
    return { ...meta, kind: "csv", filename: disp.match(/filename="?([^";]+)/)?.[1] || null, rows, truncated, csv_text: truncated ? null : text };
  }
  if (/json/i.test(ctype)) {
    let parsed: unknown;
    try { parsed = JSON.parse(text); } catch { throw new PrimitiveError("bad_json", "response claimed JSON but did not parse", meta); }
    const out = parseJsonBody(parsed, Boolean(rawArgs?.keep_html));
    return { ...meta, kind: "json", body: out };
  }
  if (isErrorPage(text)) {
    throw new PrimitiveError(
      "server_error_page",
      "StockTrak returned its 'Oops, something went wrong' page. This almost always means a missing or invalid parameter, " +
        "or that there is nothing to show (e.g. no positions of that security type).",
      meta,
    );
  }
  const parsed = parseHtml(text);
  const { rows, truncated } = capRows(parsed.rows);
  return { ...meta, kind: "html", body: { ...parsed, rows, truncated } };
});
