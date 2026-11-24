// Report frame: the widget's report pane frames this extension page instead of
// the backend's /api/html-report, because a page whose CSP has no frame-src for
// the backend (robot.voitta.ai: default-src 'self') blocks that iframe, while
// Chrome lets a page frame an extension's web-accessible page.
//
// This page frames the report itself and relays postMessage both ways, so the
// report and screenshot_report (frontend/src/lib/primitives.ts) each see the
// other as their direct neighbour: the report checks e.source === window.parent,
// and the widget checks e.source === iframe.contentWindow.
import { getBackend } from "./config.js";

const fail = (text) => {
  document.body.style.cssText = "font: 13px system-ui; color: #888; padding: 16px";
  document.body.textContent = text;
};

async function main() {
  const backend = await getBackend();
  let report;
  try {
    report = new URL(new URLSearchParams(location.search).get("u") || "");
  } catch {
    fail("Voitta: no report to show.");
    return;
  }
  if (report.origin !== backend) {
    fail("Voitta: this frame only shows reports from the Voitta backend.");
    return;
  }
  // The page that embeds the widget, as the browser reports it. The backend
  // applies that site's approval to the report (app.services.site_check).
  const site = location.ancestorOrigins?.[0];
  if (!site) {
    fail("Voitta: this frame only works inside the Voitta widget.");
    return;
  }
  // Chrome sends neither Origin nor Referer when an extension page loads an
  // iframe, so the report request can't say which site it is for. Get a
  // short-lived ticket for this site first (a tagged request, like the tunnel's);
  // the backend only issues one for an approved site.
  let ticket;
  try {
    const r = await fetch(`${backend}/api/html-report/ticket`, {
      method: "POST", credentials: "include", headers: { "X-Voitta-Via": site },
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    ({ ticket } = await r.json());
  } catch (err) {
    fail(`Voitta: the backend refused to show this report here (${err.message}).`);
    return;
  }
  report.searchParams.set("voitta_ticket", ticket);

  const frame = document.createElement("iframe");
  frame.src = report.href;
  frame.title = "Voitta report";
  document.body.appendChild(frame);

  window.addEventListener("message", (e) => {
    if (e.source === window.parent) frame.contentWindow?.postMessage(e.data, "*");
    else if (e.source === frame.contentWindow) window.parent.postMessage(e.data, site);
  });
}

main();
