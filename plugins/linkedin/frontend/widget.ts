// LinkedIn plugin — frontend primitives. Side-effect import; voitta
// core globs every plugin's frontend/widget.ts and bundles them into
// widget.js.
//
// Two primitives:
//   • linkedin_inspect_page  — what page is the user on?
//   • linkedin_read_profile  — full text of a member profile, section by section

import { PrimitiveError, registerPrimitive } from "../../../frontend/src/lib/bridge";


function _classifyPage(): string {
  const p = location.pathname;
  if (p === "/" || p === "/feed" || p.startsWith("/feed/")) return "feed";
  if (p.startsWith("/in/")) return "profile";
  if (p.startsWith("/company/")) return "company";
  if (p.startsWith("/jobs/view/")) return "job";
  if (p.startsWith("/jobs/")) return "jobs";
  if (p.startsWith("/messaging/")) return "messaging";
  if (p.startsWith("/mynetwork/")) return "mynetwork";
  if (p.startsWith("/notifications/")) return "notifications";
  if (p.startsWith("/search/")) return "search";
  return "other";
}


registerPrimitive("linkedin_inspect_page", async () => {
  const params = Object.fromEntries(new URLSearchParams(location.search));
  return {
    url: location.href,
    pathname: location.pathname,
    title: document.title,
    page_type: _classifyPage(),
    profile_id: location.pathname.match(/^\/in\/([^/]+)/)?.[1] || null,
    company_slug: location.pathname.match(/^\/company\/([^/]+)/)?.[1] || null,
    job_id: location.pathname.match(/^\/jobs\/view\/(\d+)/)?.[1] || null,
    params,
  };
});

// ---- linkedin_read_profile ------------------------------------------------
//
// LinkedIn serves profiles as an SSR shell; every section renders client-side
// and lazily, and the top-level fetch() of /in/<id>/ returns chrome only. What
// works (field-tested): load each page in a same-origin iframe that is ON
// screen (lazy rendering keys off visibility — hence opacity, not offscreen),
// then read innerText once it stops growing. The top card + About come from
// /in/<id>/; each full section list from /in/<id>/details/<section>/.

const DEFAULT_SECTIONS = [
  "experience", "education", "skills", "certifications", "projects", "languages",
];
const TOTAL_BUDGET_MS = 100_000; // under the backend's 150 s tool ceiling
const PAGE_BUDGET_MS = 15_000;
const SECTION_CHAR_CAP = 12_000;

const _sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Strip LinkedIn's nav/footer/recommendation chrome around the content.
function _trimChrome(text: string): string {
  let t = text;
  const start = t.indexOf("For Business");
  if (start >= 0 && start < 2000) t = t.slice(start + "For Business".length);
  for (const end of [
    "Profile language", "Who your viewers also viewed", "People you may know",
    "You might like", "People also viewed",
  ]) {
    const i = t.indexOf(end);
    if (i > 0) t = t.slice(0, i);
  }
  return t.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}

async function _readInFrame(
  frame: HTMLIFrameElement, url: string, deadline: number,
): Promise<{ text: string; landed: string }> {
  await new Promise<void>((resolve) => {
    const done = () => resolve();
    frame.addEventListener("load", done, { once: true });
    setTimeout(done, Math.min(PAGE_BUDGET_MS, Math.max(0, deadline - Date.now())));
    frame.src = url;
  });
  const until = Math.min(Date.now() + PAGE_BUDGET_MS, deadline);
  let text = "";
  let stable = 0;
  while (Date.now() < until) {
    await _sleep(800);
    const doc = frame.contentDocument;
    if (!doc?.body) continue;
    // Walk lazy sections into view a screen at a time (jumping straight to
    // the bottom skips the ones in between). The profile scrolls inside
    // #workspace, not the document.
    const scroller = doc.getElementById("workspace") || doc.scrollingElement;
    const atBottom =
      !scroller || scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 5;
    if (scroller && !atBottom) scroller.scrollTop += 900;
    const next = doc.body.innerText || "";
    stable = atBottom && next.length > 1500 && next.length === text.length ? stable + 1 : 0;
    text = next;
    if (stable >= 2) break;
  }
  let landed = url;
  try {
    landed = frame.contentWindow?.location.pathname || url;
  } catch {
    // cross-origin redirect (authwall etc.) — keep the requested url
  }
  return { text, landed };
}

registerPrimitive("linkedin_read_profile", async (rawArgs) => {
  const id =
    (typeof rawArgs?.profile_id === "string" && rawArgs.profile_id.trim()) ||
    location.pathname.match(/^\/in\/([^/]+)/)?.[1] ||
    "";
  if (!id) {
    throw new PrimitiveError(
      "no_profile",
      "not on a /in/<id>/ profile page and no profile_id given — pass " +
        "profile_id, or ask the user to open the profile",
    );
  }
  const sections: string[] = Array.isArray(rawArgs?.sections) && rawArgs.sections.length
    ? rawArgs.sections.map(String)
    : DEFAULT_SECTIONS;

  const deadline = Date.now() + TOTAL_BUDGET_MS;
  const base = `${location.origin}/in/${encodeURIComponent(id)}`;
  const frame = document.createElement("iframe");
  frame.setAttribute("aria-hidden", "true");
  // Near-invisible and click-through, but genuinely on screen and on top —
  // the field-tested setup; an offscreen frame never renders its sections.
  frame.style.cssText =
    "position:fixed;left:0;top:0;width:1100px;height:2400px;opacity:0.01;" +
    "pointer-events:none;border:0;z-index:2147480000;";
  document.body.appendChild(frame);

  const out: Record<string, string> = {};
  const missing: string[] = [];
  let truncated = false;
  try {
    const main = await _readInFrame(frame, `${base}/`, deadline);
    out.main = _trimChrome(main.text).slice(0, SECTION_CHAR_CAP);
    for (const s of sections) {
      if (Date.now() > deadline - 3000) {
        truncated = true;
        break;
      }
      const { text, landed } = await _readInFrame(frame, `${base}/details/${s}/`, deadline);
      // A section the member doesn't have redirects back to the profile.
      const body = landed.includes("/details/") ? _trimChrome(text) : "";
      if (!body || /Nothing to see for now/i.test(body)) {
        missing.push(s);
        continue;
      }
      out[s] = body.slice(0, SECTION_CHAR_CAP);
    }
  } finally {
    frame.remove();
  }
  return { profile_id: id, sections: out, empty_or_missing: missing, truncated };
});
