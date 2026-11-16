// Browser-side tool implementations. Each entry is a function the BE
// can invoke via `call_fn`. Pure callbacks live here; tools that need
// to drive React state (show_report, close_report) live inside
// CallFnRouter.tsx where Recoil setters are in scope.

export type Primitive = (args: Record<string, unknown>) => Promise<unknown> | unknown;

interface VoittaApi {
  getShadowRoot: () => ShadowRoot;
}

function shadowRoot(): ShadowRoot | null {
  const api = (window as unknown as { VoittaBookmarklet?: VoittaApi }).VoittaBookmarklet;
  return api ? api.getShadowRoot() : null;
}

// ─────────────────────────────────────────────────────────────────────
// screenshot_report — rasterise the active report pane.
//
// Every report is an HTML iframe (``iframe.report-panel-iframe``),
// same-origin with our backend, hosting html2canvas via the report
// shim. We resize the iframe with two-phase auto-sizing (probe at
// small + large heights to discriminate natural-max vs stretch-fill
// content), then postMessage ``voittaAction:screenshot`` and await the
// response. The shim composites nested three_scene canvases via the
// ``voitta_three_capture`` protocol.
//
// The result envelope uses the ``_image`` sentinel the BE looks for —
// see ``app.agent``'s image-block injection — so the rendered PNG
// reaches the model as an inline image block in the tool result.
// ─────────────────────────────────────────────────────────────────────

interface ScreenshotArgs {
  scale?: number;
  quality?: number;
  format?: "webp" | "png";
  timeout_ms?: number;
  expand_height?: number;
  expand_width?: number;
  expand_settle_ms?: number;
}

interface ScreenshotResponse {
  type: "voitta_report_event";
  kind: "screenshot_response";
  requestId: string;
  ok: boolean;
  message?: string;
  dataUrl?: string;
  width?: number;
  height?: number;
  full_width?: number;
  full_height?: number;
  scale?: number;
  format?: string;
  nested_scenes_captured?: number;
  doc_dims?: Record<string, number>;
}

// Per-technique result emitted by the shim's screenshot_multi handler.
// ok=true → ``stash_id`` references bytes already uploaded BE-side
// via /api/screenshot-stash. The shim never returns ``dataUrl``
// through postMessage anymore — multi-MB PNGs would truncate at the
// chainlit socket. ok=false → ``message`` describes what failed.
interface ScreenshotMultiResult {
  label: string;
  ok: boolean;
  stash_id?: string;
  media_type?: string;
  bytes_b64_len?: number;
  width?: number;
  height?: number;
  ms?: number;
  message?: string;
}

interface ScreenshotMultiResponse {
  type: "voitta_report_event";
  kind: "screenshot_multi_response";
  requestId: string;
  ok: boolean;
  message?: string;
  results?: ScreenshotMultiResult[];
}

let screenshotCounter = 0;

function findReportIframe(): HTMLIFrameElement | null {
  const root = shadowRoot();
  if (!root) return null;
  // With multiple report tabs all iframes are mounted simultaneously (hidden
  // via CSS). Query the active panel first, fall back to first visible.
  const activePanel = root.querySelector('[data-active="true"]');
  if (activePanel) {
    const iframe = activePanel.querySelector("iframe.report-panel-iframe") as HTMLIFrameElement | null;
    if (iframe) return iframe;
  }
  // Fallback: first iframe in an active tab panel, then any iframe.
  const inActivePanel = root.querySelector('.report-tab-panel.active iframe.report-panel-iframe') as HTMLIFrameElement | null;
  if (inActivePanel) return inActivePanel;
  return root.querySelector("iframe.report-panel-iframe") as HTMLIFrameElement | null;
}

async function screenshotPanelIframe(
  iframe: HTMLIFrameElement,
  args: ScreenshotArgs,
): Promise<unknown> {
  if (!iframe.contentWindow) {
    return { error: "report iframe has no contentWindow" };
  }
  const requestId = `ss_${Date.now().toString(36)}_${++screenshotCounter}`;
  const scale = typeof args.scale === "number" ? args.scale : 1;
  const quality = typeof args.quality === "number" ? args.quality : 0.85;
  const format = args.format === "png" ? "png" : "webp";
  const timeout = typeof args.timeout_ms === "number" ? args.timeout_ms : 60_000;
  const expandSettleMs =
    typeof args.expand_settle_ms === "number" ? args.expand_settle_ms : 500;

  // The shim measures content extent inside the iframe with four
  // different strategies (body-scroll / deep-content / bokeh-root /
  // generous). We then run one screenshot per strategy × technique
  // pair so the user can pick the cleanest combination from chat.
  // No more guessing or "stretch-fill detection" heuristics — the
  // iframe tells us every plausible height, we capture all of them.
  const explicitExpand =
    typeof args.expand_height === "number" ? args.expand_height : null;

  const restoreStyle = {
    height: iframe.style.height,
    minHeight: iframe.style.minHeight,
    maxHeight: iframe.style.maxHeight,
    width: iframe.style.width,
    minWidth: iframe.style.minWidth,
    maxWidth: iframe.style.maxWidth,
  };
  function setIframeHeight(px: number): void {
    iframe.style.height = `${px}px`;
    iframe.style.minHeight = `${px}px`;
    iframe.style.maxHeight = `${px}px`;
  }
  function setIframeWidth(px: number): void {
    iframe.style.width = `${px}px`;
    iframe.style.minWidth = `${px}px`;
    iframe.style.maxWidth = `${px}px`;
  }
  function restoreIframe(): void {
    iframe.style.height = restoreStyle.height;
    iframe.style.minHeight = restoreStyle.minHeight;
    iframe.style.maxHeight = restoreStyle.maxHeight;
    iframe.style.width = restoreStyle.width;
    iframe.style.minWidth = restoreStyle.minWidth;
    iframe.style.maxWidth = restoreStyle.maxWidth;
  }

  function reflowIframe(timeoutMs = 3000): Promise<void> {
    // Ask the iframe to dispatch a ``resize`` event so Bokeh/Panel
    // re-evaluates layout for the new iframe dimensions. Without this,
    // an iframe that was 1076 wide at mount keeps its stacked-column
    // layout even after we resize it to 1600 — Bokeh caches its
    // initial measurements.
    return new Promise((res) => {
      const rrid = `rf_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
      const t = setTimeout(() => {
        window.removeEventListener("message", onMsg);
        res(); // best-effort; proceed even on timeout
      }, timeoutMs);
      function onMsg(e: MessageEvent) {
        const d = e.data as {
          type?: string;
          kind?: string;
          requestId?: string;
        } | null;
        if (
          !d ||
          d.type !== "voitta_report_event" ||
          d.kind !== "reflow_response" ||
          d.requestId !== rrid
        ) {
          return;
        }
        if (e.source !== iframe.contentWindow) return;
        clearTimeout(t);
        window.removeEventListener("message", onMsg);
        res();
      }
      window.addEventListener("message", onMsg);
      iframe.contentWindow!.postMessage(
        { voittaAction: "reflow", requestId: rrid, settle_ms: expandSettleMs },
        "*",
      );
    });
  }

  type Extents = {
    bodyScroll: number;
    deepContent: number;
    bokehRoot: number;
    generous: number;
  };

  // Single measurement call — the shim runs all measurement
  // strategies inside the iframe and returns a candidate height
  // for each. We then run one screenshot per (strategy, technique)
  // pair so the user can compare and pick.
  function measureExtentsAt(probeH: number, timeoutMs = 5000): Promise<Extents> {
    return new Promise((resolveM, rejectM) => {
      const mrid = `mm_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
      setIframeHeight(probeH);
      const t = setTimeout(() => {
        window.removeEventListener("message", onMsg);
        rejectM(new Error(`measure timeout at probeH=${probeH}`));
      }, timeoutMs);
      function onMsg(e: MessageEvent) {
        const d = e.data as {
          type?: string;
          kind?: string;
          requestId?: string;
          scrollH?: number;
          bodyScrollH?: number;
          extents?: Partial<Extents>;
        } | null;
        if (
          !d ||
          d.type !== "voitta_report_event" ||
          d.kind !== "measure_response" ||
          d.requestId !== mrid
        ) {
          return;
        }
        if (e.source !== iframe.contentWindow) return;
        clearTimeout(t);
        window.removeEventListener("message", onMsg);
        const ext = d.extents || {};
        resolveM({
          bodyScroll: ext.bodyScroll ?? d.bodyScrollH ?? d.scrollH ?? probeH,
          deepContent: ext.deepContent ?? 0,
          bokehRoot: ext.bokehRoot ?? 0,
          generous: ext.generous ?? 5000,
        });
      }
      window.addEventListener("message", onMsg);
      iframe.contentWindow!.postMessage(
        { voittaAction: "measure", requestId: mrid, settle_ms: expandSettleMs },
        "*",
      );
    });
  }

  // Force a desktop-class width during capture so responsive reports
  // render in their multi-column layout instead of collapsing to a
  // single stacked column. The iframe may visually overflow the chat
  // pane during the capture window; that's fine — restoreIframe()
  // puts it back at the end.
  //
  // Width strategy:
  //   - Honour an explicit ``expand_width`` if the LLM passes one.
  //   - Otherwise take the largest of: the current iframe width, the
  //     PARENT window's inner width (so we match the user's actual
  //     monitor), and a desktop floor of 1920. The floor matters when
  //     the chat is open on a narrow screen — without it we'd capture
  //     at the chat-pane width and Panel's multi-card row collapses.
  //   - 1920 is wide enough for 4 Panel cards across (each ~400 +
  //     gaps). Tested against the executive-dashboard report; 1600
  //     was too narrow and forced single-column.
  const currentW = iframe.getBoundingClientRect().width || 0;
  const explicitWidth =
    typeof args.expand_width === "number" ? args.expand_width : null;
  const TARGET_DESKTOP_W = 1920;
  const parentInnerW = window.innerWidth || 0;
  const chosenWidth =
    explicitWidth !== null && explicitWidth > 0
      ? explicitWidth
      : Math.max(currentW, parentInnerW, TARGET_DESKTOP_W);
  if (chosenWidth > currentW) {
    setIframeWidth(chosenWidth);
    // Let Bokeh re-flow at the new width BEFORE we probe height —
    // otherwise the two probes see the old narrow-layout content.
    await reflowIframe();
  }

  // ─── multi-strategy height resolution ────────────────────────────
  // Measure ONCE at a generous probe height (8000), then derive a
  // candidate height per strategy. We capture once per strategy ×
  // technique pair so the user can compare and pick.
  const PROBE_H = 8000;
  let extents: Extents = {
    bodyScroll: 0, deepContent: 0, bokehRoot: 0, generous: 5000,
  };
  if (explicitExpand !== null && explicitExpand > 0) {
    // Explicit override → just one strategy ("explicit").
    extents = {
      bodyScroll: explicitExpand,
      deepContent: 0,
      bokehRoot: 0,
      generous: 0,
    };
  } else {
    try {
      extents = await measureExtentsAt(PROBE_H);
    } catch {
      // Probe failed; fall back to current iframe height for all.
      const h = Math.max(iframe.getBoundingClientRect().height || 0, 900);
      extents = { bodyScroll: h, deepContent: 0, bokehRoot: 0, generous: h };
    }
  }

  // Strategy list: name + height. Skip strategies that returned
  // unusable heights (< 200 px). Dedupe strategies whose heights
  // match within 50 px — no point re-rendering the same capture.
  type Strategy = { name: string; height: number };
  const allCandidates: Strategy[] = [
    { name: "body-scroll",  height: extents.bodyScroll },
    { name: "deep-content", height: extents.deepContent },
    { name: "bokeh-root",   height: extents.bokehRoot },
    { name: "generous",     height: extents.generous },
  ].filter((s) => s.height >= 200);

  // Single best strategy: bokeh-root (empirically the cleanest for
  // Panel/Bokeh reports). If bokeh-root returns no height (a non-
  // Panel report), fall back to deep-content; if THAT also fails,
  // fall back to whichever candidate has the largest height.
  const bokeh = allCandidates.find((s) => s.name === "bokeh-root");
  const deep = allCandidates.find((s) => s.name === "deep-content");
  const tallest = [...allCandidates].sort((a, b) => b.height - a.height)[0];
  const picked = bokeh ?? deep ?? tallest;
  if (!picked) {
    restoreIframe();
    return { error: "no usable height candidates from any strategy" };
  }
  const strategies: Strategy[] = [picked];

  // Take one screenshot batch per strategy. The strategy sets the
  // iframe height + reflows; the shim then runs every technique
  // against that geometry, uploads each result to /api/screenshot-stash,
  // and returns only the stash IDs through postMessage. The bytes
  // never traverse the chainlit socket so multi-MB PNGs ship fine.
  const allStashIds: Array<Record<string, unknown>> = [];
  const allErrors: Array<{ label: string; message: string; ms?: number }> = [];

  // Per-strategy capture. Each call uses its own ``rid`` so stale
  // responses from a previous strategy can't be picked up by the
  // current listener. The legacy ``requestId`` from the outer scope
  // would have caused cross-talk in the multi-strategy loop —
  // first response wins, subsequent listeners would match it too.
  let captureCounter = 0;
  function captureAtHeight(): Promise<ScreenshotMultiResponse | null> {
    const rid = `${requestId}_s${++captureCounter}`;
    return new Promise((resolve) => {
      const t = setTimeout(() => {
        window.removeEventListener("message", onMsg);
        console.warn(`[screenshot] strategy capture timed out (rid=${rid})`);
        resolve(null);
      }, timeout);
      function onMsg(e: MessageEvent) {
        const d = e.data as ScreenshotMultiResponse | null;
        if (
          !d ||
          d.type !== "voitta_report_event" ||
          d.kind !== "screenshot_multi_response" ||
          d.requestId !== rid
        ) return;
        if (e.source !== iframe.contentWindow) return;
        clearTimeout(t);
        window.removeEventListener("message", onMsg);
        resolve(d);
      }
      window.addEventListener("message", onMsg);
      iframe.contentWindow!.postMessage(
        {
          voittaAction: "screenshot_multi",
          requestId: rid,
          scale, quality, format,
          settle_ms: expandSettleMs,
        },
        "*",
      );
    });
  }

  console.info(
    `[screenshot] starting capture across ${strategies.length} strategies`,
    strategies,
  );
  try {
    for (const strat of strategies) {
      const sT0 = performance.now();
      console.info(`[screenshot] → strategy="${strat.name}" h=${strat.height}`);
      setIframeHeight(strat.height);
      await reflowIframe();
      await new Promise((r) => setTimeout(r, expandSettleMs));
      const res = await captureAtHeight();
      const sT1 = performance.now();
      if (!res || !res.ok || !Array.isArray(res.results)) {
        console.warn(
          `[screenshot] strategy="${strat.name}" failed (${Math.round(sT1 - sT0)}ms):`,
          res?.message,
        );
        allErrors.push({
          label: `${strat.name}__all`,
          message: res?.message || "screenshot_multi returned ok=false",
        });
        continue;
      }
      console.info(
        `[screenshot] strategy="${strat.name}" got ${res.results.length} techniques (${Math.round(sT1 - sT0)}ms):`,
        res.results.map((r) => `${r.label}=${r.ok ? "ok" : "FAIL:" + r.message}`).join("  "),
      );
      for (const r of res.results) {
        const composedLabel = `${strat.name}__${r.label}`;
        if (r.ok && r.stash_id) {
          allStashIds.push({
            label: composedLabel,
            strategy: strat.name,
            technique: r.label,
            target_height: strat.height,
            stash_id: r.stash_id,
            media_type: r.media_type ?? "image/png",
            width: r.width,
            height: r.height,
            ms: r.ms,
            bytes_b64_len: r.bytes_b64_len,
          });
        } else {
          allErrors.push({
            label: composedLabel,
            message: r.message || "unknown failure",
            ms: r.ms,
          });
        }
      }
    }
  } finally {
    restoreIframe();
  }
  console.info(
    `[screenshot] complete — ${allStashIds.length} ok, ${allErrors.length} failed`,
  );

  // ``_images_stash`` sentinel: the agent loop calls /api/screenshot-stash/...
  // for each id, attaches the bytes to the Chainlit step as inline
  // elements, and evicts. NO bytes traverse the socket — only the
  // (tiny) id list. The LLM sees the metadata summary only.
  return {
    ok: allStashIds.length > 0,
    _images_stash: allStashIds,
    errors: allErrors.length ? allErrors : undefined,
    strategies: strategies.map((s) => ({ name: s.name, height: s.height })),
    techniques_per_strategy: 3,
    total_captures: allStashIds.length,
  };
}

async function screenshotReport(rawArgs: Record<string, unknown>): Promise<unknown> {
  // Every report is an HTML iframe — the shadow-DOM fallback path
  // was removed when we collapsed the kind hierarchy to "html only".
  const args = (rawArgs || {}) as ScreenshotArgs;
  const iframe = findReportIframe();
  if (!iframe) {
    return { error: "no report mounted (iframe.report-panel-iframe not found)" };
  }
  return screenshotPanelIframe(iframe, args);
}

async function getPageDump(): Promise<unknown> {
  // Snapshot of the host page for the MCP ``mcp_page`` debugging tool.
  // Returns enough to inspect any page state without a separate eval
  // round-trip. No size cap on ``html`` — the caller decides what to
  // do with multi-MB DOMs.
  return {
    ok: true,
    url: location.href,
    title: document.title,
    host: location.host,
    pathname: location.pathname,
    search: location.search,
    hash: location.hash,
    user_agent: navigator.userAgent,
    html: document.documentElement.outerHTML,
    ts: Date.now(),
  };
}

// read_page returns at most this many characters: the agent loop cuts a tool
// result at 32,000 (MAX_TOOL_RESULT_BYTES in backend/app/agent.py).
const READ_PAGE_MAX_CHARS = 20_000;

function readPage(args: Record<string, unknown>): unknown {
  // Read the page through DOM APIs only, for pages whose CSP forbids eval
  // (eval_js cannot run there). A selector is data, not code. An invalid one
  // throws, and CallFnRouter returns the message to the caller.
  // shortcut: light DOM of this document only, walk open shadow roots and
  // same-origin frames when a site needs them.
  const selector = typeof args?.selector === "string" ? args.selector.trim() : "";
  const format = args?.format === "html" ? "html" : "text";
  const nodes: Element[] = selector
    ? Array.from(document.querySelectorAll(selector))
    : [document.body ?? document.documentElement];
  const items: string[] = [];
  let room = READ_PAGE_MAX_CHARS;
  let truncated = false;
  for (const node of nodes) {
    if (room <= 0) {
      truncated = true;
      break;
    }
    // innerText is undefined on SVG elements.
    const content = format === "html"
      ? node.outerHTML
      : (node as HTMLElement).innerText ?? node.textContent ?? "";
    truncated = content.length > room;
    items.push(truncated ? content.slice(0, room) : content);
    room -= content.length;
  }
  // items goes last: if the agent loop cuts the result, it cuts content, not these fields.
  const retval = {
    ok: true,
    url: location.href,
    title: document.title,
    format,
    selector: selector || null,
    matches: nodes.length,
    truncated,
    items,
  };
  return retval;
}

async function evalJs(args: Record<string, unknown>): Promise<unknown> {
  // Evaluate arbitrary JS in the bookmarklet's page context. Wraps the
  // body in an AsyncFunction so ``await`` at top level works and
  // ``return X`` ships ``X`` back. Console output is captured.
  // ``await_ms`` is advisory — we honour it as a hard timeout via
  // Promise.race so a runaway script can't hang the call_fn round-trip.
  const js = String(args?.js ?? "");
  const awaitMs = Number(args?.await_ms ?? 30_000) || 30_000;
  if (!js) return { ok: false, error: "bad_request", message: "js is required" };

  const logs: { level: string; args: unknown[] }[] = [];
  const wrap = (level: string, orig: (...a: unknown[]) => void) =>
    (...a: unknown[]) => {
      try { logs.push({ level, args: a.map((x) => safeStringify(x)) }); } catch { /* noop */ }
      try { orig.apply(console, a); } catch { /* noop */ }
    };
  const origLog = console.log, origWarn = console.warn, origErr = console.error;
  console.log = wrap("log", origLog);
  console.warn = wrap("warn", origWarn);
  console.error = wrap("error", origErr);

  const t0 = performance.now();
  try {
    // Run the body as an async IIFE through *indirect* eval rather than the
    // AsyncFunction constructor. Both honour top-level ``return`` and
    // ``await`` and execute in global scope — but Salesforce Lightning Web
    // Security distorts the Function/AsyncFunction constructor into a no-op
    // (the body silently never runs, the call returns undefined) while
    // leaving indirect eval working. Indirect eval is how the widget itself
    // is injected on hardened pages, so it's the portable path. Both are
    // gated by CSP ``'unsafe-eval'`` identically, so there's no regression on
    // ordinary sites.
    const indirectEval = eval; // assigning to a var makes the call indirect (global scope)
    const wrapped = `(async () => {\n${js}\n})()`;
    const result = await Promise.race([
      // eslint-disable-next-line no-eval
      Promise.resolve().then(() => indirectEval(wrapped)),
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error(`eval timed out after ${awaitMs}ms`)), awaitMs),
      ),
    ]);
    return {
      ok: true,
      result: safeStringify(result),
      logs,
      ms: Math.round(performance.now() - t0),
    };
  } catch (err) {
    return {
      ok: false,
      error: "eval_threw",
      message: err instanceof Error ? err.message : String(err),
      stack: err instanceof Error ? err.stack : undefined,
      logs,
      ms: Math.round(performance.now() - t0),
    };
  } finally {
    console.log = origLog;
    console.warn = origWarn;
    console.error = origErr;
  }
}

function safeStringify(v: unknown): unknown {
  // Try to make ``v`` JSON-serialisable. Functions, DOM nodes,
  // circular structures all get a string fallback.
  try {
    JSON.stringify(v);
    return v;
  } catch {
    try { return String(v); } catch { return "[unstringifiable]"; }
  }
}

import { installDevtoolsCapture, getDevtoolsData, clearDevtoolsData } from "./devtools";

export const primitives: Record<string, Primitive> = {
  get_page_title: () => ({ title: document.title }),
  screenshot_report: (args) => screenshotReport(args),
  get_page_dump: () => getPageDump(),
  read_page: (args) => readPage(args),
  eval_js: (args) => evalJs(args),
  // Devtools capture — install interceptors then poll for data via MCP.
  install_devtools_capture: () => installDevtoolsCapture(),
  get_devtools_data: (args) => getDevtoolsData(args as Parameters<typeof getDevtoolsData>[0]),
  clear_devtools_data: () => clearDevtoolsData(),
};

// Plugin entry point. Each plugin's ``frontend/widget.ts`` is
// glob-imported by ``widget.tsx`` and registers its browser-side tools
// here. Primitive names live in a flat namespace — plugin authors
// should pick distinctive ones. We warn on collision rather than throw
// so a buggy plugin doesn't take the whole widget down.
export function registerPrimitive(name: string, fn: Primitive): void {
  if (primitives[name] !== undefined) {
    console.warn(`[voitta] primitive "${name}" already registered; overwriting`);
  }
  primitives[name] = fn;
}
