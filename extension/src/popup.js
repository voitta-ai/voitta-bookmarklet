// Toolbar popup: approves the current site (this is an extension page, so the
// site itself can't click Allow) and asks the worker to launch the widget.
import { getBackend, originOf } from "./config.js";

const $ = (id) => document.getElementById(id);
const status = (text) => { $("status").textContent = text; };
$("options").onclick = (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); };

async function launch(tabId) {
  status("Opening Voitta…");
  const r = await chrome.runtime.sendMessage({ t: "launch", tabId });
  if (r?.ok) window.close();
  else status(`Could not open Voitta here: ${r?.error || "unknown error"}`);
}

async function main() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const site = tab && originOf(tab.url);
  if (!site) { status("Voitta only runs on http and https pages."); return; }

  const backend = await getBackend();
  let s;
  try {
    const r = await fetch(`${backend}/api/auth/sites/status?origin=${encodeURIComponent(site)}`,
      { credentials: "include" });
    s = r.ok ? await r.json() : null;
    if (r.status === 403) {
      status(`The Voitta backend at ${backend} does not trust this extension yet. ` +
        "Update Voitta, or add this extension's ID to VOITTA_TRUSTED_EXTENSIONS.");
      return;
    }
  } catch {
    status(`Can't reach Voitta at ${backend}. Is it running?`);
    return;
  }

  // Same rule as the bookmarklet bridge: approved, or nothing to approve with.
  if (!s || s.approved || !s.signed_in) { await launch(tab.id); return; }

  $("title").innerHTML = 'Allow Voitta on <span class="site"></span>?';
  $("title").querySelector(".site").textContent = site.replace(/^https?:\/\//, "");
  status(s.email ? `Signed in as ${s.email}` : "Voitta will be able to act for you on this site.");
  $("ask").hidden = false;
  $("no").onclick = () => window.close();
  $("yes").onclick = async () => {
    $("yes").disabled = true;
    try {
      const r = await fetch(`${backend}/api/auth/sites/approve`, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ origin: site }),
      });
      if (!r.ok) throw new Error(`status ${r.status}`);
      $("ask").hidden = true;
      await launch(tab.id);
    } catch (err) {
      status(`Could not allow this site (${err.message}).`);
      $("yes").disabled = false;
    }
  };
}

main();
