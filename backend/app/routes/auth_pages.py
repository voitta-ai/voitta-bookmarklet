"""Pages on our own origin for sign-in and site approval: /auth/signin, /auth/approve,
/auth/password, plus the result pages the Google callback renders.

Passwords are only ever typed here — never into the widget, which runs inside someone else's page.
Every page refuses to be framed (``frame-ancestors 'none'``), so another site can't overlay or
click-jack it, and the Allow button only arms itself a second after the popup has focus.
"""

from __future__ import annotations

import html
import json

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.services import login_auth

router = APIRouter()

_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
}

_CSS = """
:root{--bg:#ffffff;--fg:#1a1d23;--muted:#5f6673;--border:#d9dde3;--field:#ffffff;--accent:#2563eb;
--accent-fg:#ffffff;--err:#b42318;--ok:#067647;--btn:#f3f4f6}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--fg:#e8eaee;--muted:#9aa1ad;--border:#2c323b;
--field:#1b1f26;--accent:#3b82f6;--accent-fg:#ffffff;--err:#f97066;--ok:#47cd89;--btn:#222831}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:380px;margin:0 auto;padding:40px 20px}
h1{font-size:20px;margin:0 0 4px}
.brand{font-weight:700;font-size:13px;letter-spacing:.04em;color:var(--muted);margin-bottom:22px}
p{margin:10px 0}.muted{color:var(--muted)}.err{color:var(--err);min-height:1.4em}.ok{color:var(--ok)}
label{display:block;font-size:13px;color:var(--muted);margin:12px 0 4px}
input{width:100%;padding:9px 11px;border:1px solid var(--border);border-radius:7px;background:var(--field);
color:var(--fg);font:inherit}
input:focus{outline:2px solid var(--accent);outline-offset:-1px}
button,.btn{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:10px 14px;
border-radius:8px;border:1px solid var(--border);background:var(--btn);color:var(--fg);font:inherit;
font-weight:600;cursor:pointer;text-decoration:none;margin-top:14px}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
button:disabled{opacity:.55;cursor:default}
.row{display:flex;gap:10px}.row button{margin-top:18px}
.or{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:13px;margin:20px 0 4px}
.or:before,.or:after{content:"";flex:1;border-top:1px solid var(--border)}
.site{font-family:ui-monospace,Menlo,monospace;word-break:break-all}
"""

_G = (
    '<svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true">'
    '<path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9 3.6l6.7-6.7C35.6 2.6 30.2 0 24 0 14.6 0 6.4 5.4 2.5 13.2l7.8 6.1C12.2 13.3 17.6 9.5 24 9.5z"/>'
    '<path fill="#4285F4" d="M46.5 24.5c0-1.6-.1-3.2-.4-4.7H24v9h12.7c-.6 3-2.3 5.5-4.8 7.2l7.4 5.7c4.3-4 6.8-9.9 6.8-17.2z"/>'
    '<path fill="#FBBC05" d="M10.3 19.3l-7.8-6.1C.9 16.5 0 20.1 0 24s.9 7.5 2.5 10.8l7.8-6.1c-.5-1.5-.8-3-.8-4.7s.3-3.2.8-4.7z"/>'
    '<path fill="#34A853" d="M24 48c6.2 0 11.5-2 15.3-5.5l-7.4-5.7c-2 1.4-4.7 2.3-7.9 2.3-6.4 0-11.8-3.8-13.7-9.3l-7.8 6.1C6.4 42.6 14.6 48 24 48z"/>'
    "</svg>"
)

# Tell the opener (the widget) we're done, then close. The widget also notices the popup closing.
_DONE_JS = """
function voittaDone(msg){
  try{ if(window.opener) window.opener.postMessage(msg,'*'); }catch(e){}
  setTimeout(function(){ try{ window.close(); }catch(e){} }, 700);
}
"""


def _page(title: str, body: str, script: str = "", status: int = 200) -> HTMLResponse:
    doc = (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)} · Voitta</title><style>{_CSS}</style>"
        f'<main><div class=brand>VOITTA</div>{body}</main>'
        f"<script>{_DONE_JS}{script}</script></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=_HEADERS)


def message(title: str, text: str, status: int = 200) -> HTMLResponse:
    return _page(title, f"<h1>{html.escape(title)}</h1><p class=muted>{html.escape(text)}</p>", status=status)


def signed_in(email: str) -> HTMLResponse:
    return _page(
        "Signed in",
        f"<h1>Signed in</h1><p>as <b>{html.escape(email)}</b>.</p><p class=muted>You can close this window.</p>",
        "voittaDone('voitta-auth');",
    )


def refused(email: str, why: str) -> HTMLResponse:
    e = html.escape(email or "This account")
    if why == "disabled":
        title, text = "Account disabled", f"{e} is disabled. Ask your admin."
    elif why == "mismatch":
        title, text = ("Another Google account",
                       f"{e} is already linked to a different Google account. Sign in with that one, "
                       "or use your password.")
    else:
        title, text = "Not invited", f"{e} isn't on the list. Ask the admin to invite it."
    body = (f"<h1>{title}</h1><p class=muted>{text}</p>"
            '<a class=btn href="/auth/signin">Sign in another way</a>')
    return _page(title, body, status=403)


@router.get("/auth/signin", include_in_schema=False)
async def signin_page(then: str = "") -> HTMLResponse:
    if not login_auth.is_enabled():
        return message("No sign-in needed", "This Voitta app runs on your computer and doesn't need a sign-in.")
    body = f"""
<h1>Sign in</h1>
<a class=btn href="/api/auth/google/start">{_G}Continue with Google</a>
<div class=or>or</div>
<form id=f novalidate>
  <label for=email>Email</label><input id=email name=email type=email autocomplete=username required>
  <label for=pw>Password</label><input id=pw name=password type=password autocomplete=current-password required>
  <button class=primary id=go type=submit>Sign in</button>
  <p class=err id=err role=alert></p>
</form>
<p class=muted style="font-size:13px">Invite only. Ask your admin for access.</p>"""
    script = f"""
var THEN = {json.dumps(then if then in ("approve",) else "")};
var f = document.getElementById('f'), err = document.getElementById('err'), go = document.getElementById('go');
document.getElementById('email').focus();
f.addEventListener('submit', async function(e){{
  e.preventDefault(); err.textContent=''; go.disabled=true; go.textContent='Signing in…';
  try {{
    var r = await fetch('/api/auth/password', {{method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{email: f.email.value, password: f.password.value}})}});
    var b = {{}}; try {{ b = await r.json(); }} catch(_ ) {{}}
    if (r.ok) {{
      if (THEN === 'approve') {{ location.replace('/auth/approve'); return; }}
      document.querySelector('main').innerHTML = '<div class=brand>VOITTA</div><h1>Signed in</h1>' +
        '<p>as <b></b>.</p><p class=muted>You can close this window.</p>';
      document.querySelector('main b').textContent = b.email || '';
      voittaDone('voitta-auth'); return;
    }}
    err.textContent = b.detail || 'Sign-in failed.';
  }} catch(_ ) {{ err.textContent = 'Could not reach Voitta. Try again.'; }}
  go.disabled=false; go.textContent='Sign in';
}});"""
    return _page("Sign in", body, script)


_EMBED_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    # Framable by any site ON PURPOSE: it's the inline "Allow" inside the widget. Its protection
    # against a page hiding/covering it is IntersectionObserver v2 (below), not framing headers.
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
        "form-action 'none'; base-uri 'none'; frame-ancestors *"
    ),
}

_EMBED_HTML = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Allow Voitta</title>
<style>
html,body{margin:0;background:#141922;color:#e5e7eb;font:13px/1.45 system-ui,-apple-system,sans-serif}
#box{padding:12px 14px;text-align:center}
.q{font-weight:600;font-size:14px}
.site{font-family:ui-monospace,Menlo,monospace;word-break:break-all}
.m{opacity:.7;font-size:12px;margin-top:3px}
button{font:inherit;font-weight:600;margin-top:10px;padding:7px 22px;border-radius:7px;cursor:pointer;
  border:1px solid #3b82f6;background:#3b82f6;color:#fff}
/* No opacity on anything observed: IntersectionObserver v2 treats opacity < 1 as "not visible",
   so a faded disabled button could never arm itself. Grey it out with colour instead. */
button:disabled{background:#475569;border-color:#475569;color:#cbd5e1;cursor:default}
.err{color:#f97066;font-size:12px;margin-top:6px;min-height:1em}
</style>
<div id=box>
  <div class=q id=q>Checking…</div>
  <div class=m id=m></div>
  <button id=yes type=button disabled hidden>Allow</button>
  <div class=err id=err role=alert></div>
</div>
<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  // The site to allow = the page this frame sits in, as the browser reports it (not page input).
  var SITE = (location.ancestorOrigins && location.ancestorOrigins.length) ? location.ancestorOrigins[0] : null;
  var IO2 = typeof IntersectionObserverEntry !== "undefined" && "isVisible" in IntersectionObserverEntry.prototype;
  var ARM_MS = 1000, MIN_W = 200, MIN_H = 100, armT = null;
  function post(t) { try { parent.postMessage({ t: t }, SITE || "*"); } catch (e) {} }
  function fail(m) { $("err").textContent = m; }

  if (window.top === window || !SITE) { $("q").textContent = "Open this from the Voitta widget."; return; }
  if (!IO2) { post("voitta-embed-unsupported"); $("q").textContent = "Use the Voitta window to allow this site."; return; }
  post("voitta-embed-ready");

  var host = SITE.replace(/^https?:\\/\\//, "");
  function disarm() { clearTimeout(armT); armT = null; $("yes").disabled = true; }
  function onVisibility(visible) {
    // Anti-clickjacking: Allow is clickable only while this frame is fully on screen, not covered,
    // not transparent / transformed / filtered (IntersectionObserver v2 checks all that), big enough
    // to read, and has been like that for a second (so it can't be slid under the cursor).
    var bigEnough = window.innerWidth >= MIN_W && window.innerHeight >= MIN_H;
    if (!visible || !bigEnough) { disarm(); return; }
    if (armT === null && $("yes").disabled) armT = setTimeout(function () { armT = null; $("yes").disabled = false; }, ARM_MS);
  }

  fetch("/api/auth/sites/status?origin=" + encodeURIComponent(SITE), { credentials: "include" })
    .then(function (r) { return r.json(); })
    .then(function (s) {
      if (!s.signed_in) { post("voitta-embed-signin"); $("q").textContent = "Sign in to Voitta first."; return; }
      if (s.approved) { post("voitta-site-approved"); $("q").textContent = "Connected."; return; }
      $("q").innerHTML = 'Allow Voitta on <span class=site></span>?';
      $("q").querySelector(".site").textContent = host;
      $("m").textContent = (s.email ? s.email + " · " : "") + "Voitta will be able to act for you on this site.";
      $("yes").hidden = false;
      // The question (what you're allowing) and the button must both be fully on screen.
      var seen = { q: false, yes: false };
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) { seen[e.target.id] = e.isVisible && e.intersectionRatio >= 0.99; });
        onVisibility(seen.q && seen.yes);
      }, { trackVisibility: true, delay: 100, threshold: [0, 0.99, 1] });
      io.observe($("q"));
      io.observe($("yes"));
      window.addEventListener("resize", function () { disarm(); });
    })
    .catch(function () { fail("Could not reach Voitta."); });

  $("yes").onclick = function (ev) {
    if ($("yes").disabled || !ev.isTrusted) return;
    disarm();
    fetch("/api/auth/sites/approve", {
      method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ origin: SITE })
    }).then(function (r) {
      if (!r.ok) throw new Error("status " + r.status);
      $("yes").hidden = true; $("m").textContent = ""; $("q").textContent = "Connected.";
      post("voitta-site-approved");
    }).catch(function (e) { fail("Could not connect the site (" + e.message + ")."); });
  };
})();
</script></html>"""


@router.get("/auth/approve-embed", include_in_schema=False)
async def approve_embed_page() -> HTMLResponse:
    """The inline "Allow Voitta on <site>?" shown inside the widget on normal sites (no popup)."""
    return HTMLResponse(_EMBED_HTML, headers=_EMBED_HEADERS)


@router.get("/auth/approve", include_in_schema=False)
async def approve_page() -> HTMLResponse:
    body = """
<h1 id=t>Connect a site</h1>
<p id=who class=muted></p>
<div id=ask hidden>
  <p class=muted>Voitta will be able to act for you on this site when you open it there.</p>
  <div class=row><button id=no type=button>Not now</button>
  <button id=yes class=primary type=button disabled>Allow</button></div>
</div>
<p class=err id=err role=alert></p>"""
    script = """
var opener = window.opener, ORIGIN = null, ARM_MS = 1000, armT = null;
var $ = function(id){ return document.getElementById(id); };
function fail(m){ $('err').textContent = m; }
function arm(){ clearTimeout(armT); $('yes').disabled = true;
  if (document.visibilityState === 'visible' && document.hasFocus())
    armT = setTimeout(function(){ $('yes').disabled = false; }, ARM_MS); }
window.addEventListener('focus', arm); window.addEventListener('blur', arm);
document.addEventListener('visibilitychange', arm);
if (!opener) fail('Open this from the Voitta widget on the site you want to connect.');
window.addEventListener('message', function(e){
  if (e.source !== opener || ORIGIN || !e.data || e.data.t !== 'voitta-approve-origin') return;
  ORIGIN = e.origin; start();
});
var tries = 0, hello = setInterval(function(){
  if (ORIGIN || !opener) { clearInterval(hello); return; }
  if (++tries > 40) { clearInterval(hello); fail('The Voitta widget did not answer. Close this window and try again.'); return; }
  try { opener.postMessage({t:'voitta-approve-hello'}, '*'); } catch(_ ) {}
}, 250);
async function start(){
  var host = ORIGIN.replace(/^https?:\\/\\//, '');
  $('t').innerHTML = 'Allow Voitta on <span class=site></span>?';
  $('t').querySelector('.site').textContent = host;
  var s;
  try { s = await (await fetch('/api/auth/sites/status?origin=' + encodeURIComponent(ORIGIN))).json(); }
  catch(_ ) { fail('Could not reach Voitta.'); return; }
  if (!s.signed_in) {
    $('who').innerHTML = 'Sign in first, then connect the site. <a href="/auth/signin?then=approve">Sign in</a>';
    return;
  }
  if (s.approved) { $('who').textContent = 'Already connected.'; voittaDone({t:'voitta-site-approved'}); return; }
  if (s.email) $('who').textContent = 'Signed in as ' + s.email;
  $('ask').hidden = false; arm();
}
$('no').onclick = function(){ window.close(); };
$('yes').onclick = async function(){
  $('yes').disabled = true;
  try {
    var r = await fetch('/api/auth/sites/approve', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({origin: ORIGIN})});
    if (r.ok) { $('ask').hidden = true; $('who').textContent = 'Connected.'; voittaDone({t:'voitta-site-approved'}); return; }
    var b = {}; try { b = await r.json(); } catch(_ ) {}
    fail(b.detail || 'Could not connect the site.');
  } catch(_ ) { fail('Could not reach Voitta.'); }
  arm();
};"""
    return _page("Connect a site", body, script)


@router.get("/auth/password", include_in_schema=False)
async def password_page() -> HTMLResponse:
    if not login_auth.is_enabled():
        return message("No password here", "This Voitta app runs on your computer and has no accounts.")
    body = """
<h1>Change password</h1>
<p id=who class=muted></p>
<form id=f novalidate hidden>
  <div id=cur><label for=c>Current password</label>
  <input id=c type=password autocomplete=current-password></div>
  <label for=n>New password</label><input id=n type=password autocomplete=new-password required>
  <label for=a>New password again</label><input id=a type=password autocomplete=new-password required>
  <button class=primary id=go type=submit>Change password</button>
  <p class=err id=err role=alert></p>
</form>"""
    script = """
var $ = function(id){ return document.getElementById(id); };
(async function(){
  var me = await (await fetch('/api/auth/me')).json();
  if (!me.authenticated) { $('who').innerHTML = 'Sign in first. <a href="/auth/signin">Sign in</a>'; return; }
  $('who').textContent = me.email;
  if (!me.has_password) $('cur').hidden = true;
  $('f').hidden = false; (me.has_password ? $('c') : $('n')).focus();
})();
$('f').addEventListener('submit', async function(e){
  e.preventDefault(); $('err').textContent = '';
  if ($('n').value !== $('a').value) { $('err').textContent = "The new passwords don't match."; return; }
  $('go').disabled = true;
  try {
    var r = await fetch('/api/auth/change-password', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({current_password: $('c').value, new_password: $('n').value})});
    var b = {}; try { b = await r.json(); } catch(_ ) {}
    if (r.ok) { $('f').hidden = true; $('who').textContent = 'Password changed. Your other sessions were signed out.';
      voittaDone('voitta-password-changed'); return; }
    $('err').textContent = b.detail || 'Could not change the password.';
  } catch(_ ) { $('err').textContent = 'Could not reach Voitta.'; }
  $('go').disabled = false;
});"""
    return _page("Change password", body, script)
