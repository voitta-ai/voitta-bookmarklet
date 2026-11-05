// Hedgehog plugin: pure parsers for StockTrak's response shapes.
//
// StockTrak's AJAX endpoints mostly answer JSON whose payload is a
// server-rendered HTML fragment ({"Html": "<tr>…"}). These helpers turn
// those fragments into plain JSON in the browser, so the backend needs
// no HTML parser. No DOM side effects: everything goes through
// DOMParser on a detached document (works under jsdom in tests).

export interface HtmlRow {
  cells: string[];
  // Every data-* attribute found anywhere in the row (first wins).
  // Carries the ids the write endpoints need: data-status (cancel id),
  // data-order-conf, data-securityid, data-underlying-securityid, …
  data: Record<string, string>;
  // The same attributes per element, keyed by the element's first class
  // (several links in one row reuse data-order-id with different meanings:
  // a.edit-order carries the numeric id, a.view-trade-notes the conf GUID).
  els: { cls: string; data: Record<string, string> }[];
  // Cells that contain <br>-separated values (open/close pairs in
  // closed positions) are split here; absent when no cell has a <br>.
  split?: string[][];
}

export interface SpreadLeg {
  side: string | null;          // "Buy" | "Short"
  quantity: string | null;
  option_type: string;          // "Call" | "Put" | "" (stock leg)
  needs_strike: boolean;
}

export interface ParsedHtml {
  headers: string[];
  rows: HtmlRow[];
  options?: { value: string; text: string }[];
  fields?: Record<string, string>;
  legs?: SpreadLeg[];
  text: string;
}

const MAX_TEXT = 4000;

function collapse(s: string | null | undefined): string {
  return (s || "").replace(/\s+/g, " ").trim();
}

function parseFragment(html: string): Document {
  // Bare <tr> fragments (getclosepositions, getorderhistory) are dropped by
  // the HTML parser outside a table, so wrap them first.
  const wrapped = /^\s*<tr[\s>]/i.test(html) ? `<table><tbody>${html}</tbody></table>` : html;
  const doc = new DOMParser().parseFromString(`<!doctype html><body>${wrapped}</body>`, "text/html");
  // Fragments embed page JS (getpricev2's quote panel: `$(function () { getpriceforslider(); … })`,
  // the order preview's setupCharacterCounter). textContent would include it; drop it.
  doc.querySelectorAll("script, style, noscript, template").forEach((el) => el.remove());
  return doc;
}

function rowData(tr: Element): Pick<HtmlRow, "data" | "els"> {
  const data: Record<string, string> = {};
  const els: HtmlRow["els"] = [];
  for (const el of [tr, ...Array.from(tr.querySelectorAll("*"))]) {
    const own: Record<string, string> = {};
    for (const a of Array.from(el.attributes)) {
      if (!a.name.startsWith("data-")) continue;
      own[a.name] = a.value;
      if (!(a.name in data)) data[a.name] = a.value;
    }
    if (Object.keys(own).length) els.push({ cls: el.classList[0] || el.tagName.toLowerCase(), data: own });
  }
  return { data, els };
}

function cellParts(td: Element): string[] {
  // Split on <br> into separate values, keeping text order.
  const parts: string[] = [];
  let cur = "";
  td.childNodes.forEach(function walk(n: ChildNode) {
    if (n.nodeName === "BR") {
      parts.push(collapse(cur));
      cur = "";
    } else if (n.nodeType === 3) {
      cur += n.textContent || "";
    } else {
      n.childNodes.forEach(walk);
    }
  });
  parts.push(collapse(cur));
  return parts.filter((p) => p !== "");
}

/** Parse an HTML fragment into header/rows/options/fields/text. */
export function parseHtml(html: string): ParsedHtml {
  const doc = parseFragment(html);
  const headers = Array.from(doc.querySelectorAll("thead th, tr:first-child th")).map((th) =>
    collapse(th.textContent),
  );
  const rows: HtmlRow[] = [];
  for (const tr of Array.from(doc.querySelectorAll("tr"))) {
    const tds = Array.from(tr.querySelectorAll(":scope > td"));
    if (tds.length === 0) continue; // header row
    const row: HtmlRow = { cells: tds.map((td) => collapse(td.textContent)), ...rowData(tr) };
    if (tds.some((td) => td.querySelector("br"))) row.split = tds.map(cellParts);
    rows.push(row);
  }
  const out: ParsedHtml = { headers, rows, text: collapse(doc.body.textContent).slice(0, MAX_TEXT) };

  const opts = Array.from(doc.querySelectorAll("option"))
    .map((o) => ({ value: o.getAttribute("value") ?? collapse(o.textContent), text: collapse(o.textContent) }))
    .filter((o) => o.value !== "-1" && o.value !== "");
  if (opts.length) out.options = opts;

  // .line-text > .title / .value pairs (option quote, order preview panels).
  const fields: Record<string, string> = {};
  for (const lt of Array.from(doc.querySelectorAll(".line-text"))) {
    const t = collapse(lt.querySelector(".title")?.textContent);
    const v = collapse(lt.querySelector(".value")?.textContent);
    if (t && !(t in fields)) fields[t] = v;
  }
  const strong = doc.querySelector("h2 strong");
  if (strong) fields["OptionSymbol"] = collapse(strong.textContent);
  if (Object.keys(fields).length) out.fields = fields;

  // GetOptionSpreadLegs: one .option-spread-leg-form per leg, values in inputs.
  const val = (f: Element, c: string) => (f.querySelector("." + c) as HTMLInputElement | null)?.getAttribute("value") ?? null;
  const legs = Array.from(doc.querySelectorAll(".option-spread-leg-form")).map((f) => ({
    side: val(f, "leg-order-side"),
    quantity: val(f, "leg-qty"),
    option_type: val(f, "leg-option-type") || "",
    needs_strike: Boolean(f.querySelector(".leg-option-strike")),
  }));
  if (legs.length) out.legs = legs;
  return out;
}

/** True when a string looks like an HTML fragment worth parsing. */
export function looksLikeHtml(v: unknown): v is string {
  return typeof v === "string" && v.length > 20 && /<[a-z][\s\S]*>/i.test(v);
}

/** Replace every HTML-fragment string value in a JSON body with its parse. */
export function parseJsonBody(body: unknown, keepHtml = false): unknown {
  if (!body || typeof body !== "object" || Array.isArray(body)) return body;
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(body as Record<string, unknown>)) {
    if (looksLikeHtml(v)) {
      out[k] = parseHtml(v);
      if (keepHtml) out[`${k}__html`] = v;
    } else {
      out[k] = v;
    }
  }
  return out;
}

/** Minimal RFC-4180 CSV parser (quoted fields, embedded commas/quotes/newlines). */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let q = false;
  const s = text.replace(/^﻿/, "");
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (q) {
      if (c === '"' && s[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') q = false;
      else field += c;
    } else if (c === '"') q = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && s[i + 1] === "\n") i++;
      row.push(field); field = "";
      if (row.some((x) => x !== "")) rows.push(row);
      row = [];
    } else field += c;
  }
  row.push(field);
  if (row.some((x) => x !== "")) rows.push(row);
  return rows;
}

/** StockTrak's generic error page ("Oops, something went wrong"). */
export function isErrorPage(html: string): boolean {
  return /<title>\s*Oops, something went wrong/i.test(html);
}

/** The login page (session expired / not signed in). */
export function isLoginPage(finalUrl: string, html: string): boolean {
  try {
    if (new URL(finalUrl).pathname.toLowerCase() === "/login") return true;
  } catch { /* relative or invalid: fall through */ }
  return /<title>\s*Log In\b/i.test(html) && /name="Password"/i.test(html);
}
