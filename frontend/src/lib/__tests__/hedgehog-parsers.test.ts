// Hedgehog plugin parsers (plugins/hedgehog/frontend/parsers.ts).
// Fixtures are synthetic but copy the exact markup shapes StockTrak returned
// during live testing (2026-11-02/03): bare <tr> fragments, data-* ids on
// links, <br>-split cells, spread leg forms, option lists.

import { describe, expect, it } from "vitest";
import {
  isErrorPage, isLoginPage, looksLikeHtml, parseCsv, parseHtml, parseJsonBody,
} from "../../../../plugins/hedgehog/frontend/parsers";

const OPEN_ORDERS = `
<table><thead><tr><th>Date</th><th>Order</th><th>Symbol</th></tr></thead><tbody>
<tr><td>11/2/2026 <a class="cancelorder" data-status="168915606">Cancel</a></td><td>Buy</td><td>F</td>
<td>1</td><td>$12.00</td><td>x</td><td>Limit</td><td>x</td><td>Open</td><td>GTC</td></tr>
</tbody></table>`;

const ORDER_HISTORY_BARE = `
<tr><th>#</th><th>Date</th></tr>
<tr><td>1</td><td>11/3/2026</td><td>Sell</td><td>F</td><td>1</td><td>$XXX.xx</td><td></td><td></td><td></td><td>Filled</td>
<td><a class="view-trade-notes" data-order-id="0f6e…bc4ec7edca57">Notes</a>
<a class="edit-order" data-order-id="169000001" data-order-conf="aaaa-bbbb">Edit</a></td></tr>`;

const CLOSED_BARE = `<tr><td>11/3/2026<br/>11/3/2026</td><td>F</td><td>Buy<br/>Sell</td><td>12.63<br/>12.34</td><td>($0.29)</td></tr>`;

const LEGS = `
<div class="option-spread-leg-form"><input class="leg-order-side" value="Buy"/><input class="leg-qty" value="1"/>
<input class="leg-option-type" value="Call"/><select class="leg-option-strike"></select></div>
<div class="option-spread-leg-form"><input class="leg-order-side" value="Short"/><input class="leg-qty" value="1"/>
<input class="leg-option-type" value="Call"/><select class="leg-option-strike"></select></div>`;

describe("parseHtml", () => {
  it("reads table rows and the cancel id from a.cancelorder[data-status]", () => {
    const p = parseHtml(OPEN_ORDERS);
    expect(p.headers).toEqual(["Date", "Order", "Symbol"]);
    expect(p.rows).toHaveLength(1);
    expect(p.rows[0].cells[2]).toBe("F");
    expect(p.rows[0].data["data-status"]).toBe("168915606");
  });

  it("parses bare <tr> fragments and keeps per-element ids apart", () => {
    const p = parseHtml(ORDER_HISTORY_BARE);
    expect(p.rows).toHaveLength(1); // header row skipped
    const els = p.rows[0].els;
    expect(els.find((e) => e.cls === "edit-order")?.data["data-order-conf"]).toBe("aaaa-bbbb");
    expect(els.find((e) => e.cls === "view-trade-notes")?.data["data-order-id"]).toBe("0f6e…bc4ec7edca57");
    expect(p.rows[0].cells[9]).toBe("Filled");
  });

  it("splits <br>-separated open/close values", () => {
    const p = parseHtml(CLOSED_BARE);
    expect(p.rows[0].split?.[2]).toEqual(["Buy", "Sell"]);
    expect(p.rows[0].split?.[3]).toEqual(["12.63", "12.34"]);
  });

  it("extracts option lists, with and without value attributes", () => {
    expect(parseHtml(`<option value="-1">Select</option><option>11/20/2026</option><option>12/18/2026</option>`).options)
      .toEqual([{ value: "11/20/2026", text: "11/20/2026" }, { value: "12/18/2026", text: "12/18/2026" }]);
    expect(parseHtml(`<option value="12.5">12.50</option>`).options).toEqual([{ value: "12.5", text: "12.50" }]);
  });

  it("extracts .line-text fields and the option symbol", () => {
    const p = parseHtml(`<h2>Contract <strong>F2620K12.5</strong></h2>
      <div class="line-text"><span class="title">Last</span><span class="value">0.42</span></div>`);
    expect(p.fields).toEqual({ Last: "0.42", OptionSymbol: "F2620K12.5" });
  });

  it("extracts spread leg templates", () => {
    expect(parseHtml(LEGS).legs).toEqual([
      { side: "Buy", quantity: "1", option_type: "Call", needs_strike: true },
      { side: "Short", quantity: "1", option_type: "Call", needs_strike: true },
    ]);
  });
});

describe("parseJsonBody", () => {
  it("replaces only HTML-looking string fields", () => {
    const out = parseJsonBody({ Success: true, Html: OPEN_ORDERS, TotalPages: 1, Note: "plain text" }) as Record<string, unknown>;
    expect(out.Success).toBe(true);
    expect(out.TotalPages).toBe(1);
    expect(out.Note).toBe("plain text");
    expect((out.Html as { rows: unknown[] }).rows).toHaveLength(1);
  });

  it("can keep the raw HTML alongside", () => {
    const out = parseJsonBody({ Html: OPEN_ORDERS }, true) as Record<string, unknown>;
    expect(out.Html__html).toBe(OPEN_ORDERS);
  });

  it("leaves arrays (symbol search) untouched", () => {
    const arr = [{ Symbol: "F", Company: "Ford" }];
    expect(parseJsonBody(arr)).toBe(arr);
    expect(looksLikeHtml("<b>x</b> is bold text here")).toBe(true);
    expect(looksLikeHtml("$12.34")).toBe(false);
  });
});

describe("parseCsv", () => {
  it("handles BOM, quoted commas, escaped quotes and CRLF", () => {
    const csv = '﻿Symbol,Amount,Note\r\nF,"$1,250.00","say ""hi"""\r\n\r\nAAPL,1,"a\nb"\r\n';
    expect(parseCsv(csv)).toEqual([["Symbol", "Amount", "Note"], ["F", "$1,250.00", 'say "hi"'], ["AAPL", "1", "a\nb"]]);
  });

  it("parses the portfolio-summary pair layout row by row", () => {
    const rows = parseCsv('Cash Balance:,"$999,890.37",Market Value of Long Positions:,$96.33\n');
    expect(rows[0]).toEqual(["Cash Balance:", "$999,890.37", "Market Value of Long Positions:", "$96.33"]);
  });
});

describe("page detection", () => {
  it("recognises the login redirect and the error page", () => {
    expect(isLoginPage("https://app.stocktrak.com/login?ReturnUrl=%2f", "")).toBe(true);
    expect(isLoginPage("https://app.stocktrak.com/dashboard", '<title>Log In</title><input name="Password">')).toBe(true);
    expect(isLoginPage("https://app.stocktrak.com/dashboard", "<title>Dashboard - StockTrak</title>")).toBe(false);
    expect(isErrorPage("<html><title>Oops, something went wrong - StockTrak</title>")).toBe(true);
    expect(isErrorPage("<title>Dashboard</title>")).toBe(false);
  });
});
