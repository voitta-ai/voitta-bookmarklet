# Voitta Chrome extension

For pages where neither bookmarklet works. The normal bookmarklet loads
`widget.js` with a `<script>` tag, which `script-src` blocks; the bridge
bookmarklet `eval`s it, which a CSP without `'unsafe-eval'` blocks
(robot.voitta.ai is one such page). The extension injects the widget with
`chrome.scripting`, which the page's CSP does not govern, and carries the
widget's backend traffic itself, so `connect-src` doesn't matter either.

```
page (MAIN world)                 content script (ISOLATED)       service worker
widget.js + src/page-shim.js ── MessagePort ──▶ src/relay.js ── runtime Port ──▶ fetch / WebSocket ──▶ backend
```

- `src/page-shim.js` replaces `fetch`, `XMLHttpRequest` and `WebSocket` for
  backend URLs only, as the bridge's boot shim (`backend/app/bridge.py`) does
  for the first and last. It sets bridge mode, so socket.io uses WebSockets and
  images load as `blob:` URLs. Chainlit uploads attachments with XHR; a
  tunnelled XHR stays a real `XMLHttpRequest` whose events are dispatched on
  it, and its upload is reported as one 100% progress step.
- `src/sw.js` makes the requests and tags each with the tab's
  browser-verified origin (`X-Voitta-Via` / `?voitta_via=`), so the backend
  applies that site's approval.
- `src/popup.js` is the toolbar popup. It approves the site (an extension
  page, so the site can't click Allow) and asks the worker to launch.

The backend trusts the tag only from the extension ID that `manifest.json`'s
`key` pins, `cgcipboiieipdcbonhefjcgcikliboic`
(`app.services.site_check.DEFAULT_EXTENSION_IDS`). Set
`VOITTA_TRUSTED_EXTENSIONS` to replace it, for example with a Web Store ID, or
set it empty to trust none.

## Reports on these pages

The report pane is an iframe of the backend's `/api/html-report`, which a
`frame-src` (or `default-src`) without the backend blocks. Under the extension
the pane frames `src/report-frame.html?u=<report URL>` instead. Chrome lets a
page frame an extension's web-accessible page whatever its CSP. That page
frames the report and relays `postMessage` both ways, so the report and
`screenshot_report` work unchanged.

Chrome sends neither `Origin` nor `Referer` when an extension page loads an
iframe, so the report request can't say which site it's for. The frame first
asks `POST /api/html-report/ticket` with `X-Voitta-Via: <embedding site>`
(`location.ancestorOrigins[0]`); the guard lets that through only for an
approved site. It then loads the report with `?voitta_ticket=`. A ticket is an
HMAC over the site, the path and an expiry (10 minutes), with a key held in
memory (`app.services.site_check.issue_ticket`).

Listing `report-frame.html` as web-accessible lets any site detect that the
extension is installed by trying to load it.

## browser_eval on these pages

The widget runs in the page's own world, where the page's CSP forbids `eval`,
so `browser_eval` can't run its scripts there. Under the extension,
`evalJs` (`frontend/src/lib/primitives.ts`) notices that `eval` is blocked
and sends the script through the tunnel instead. The worker runs it with
`chrome.userScripts.execute` in a `USER_SCRIPT` world: same DOM, its own JS
globals, and a CSP the extension sets (`script-src 'self' 'unsafe-eval'`). The
script runs only in the document whose relay asked (`documentIds`). The result
has the same envelope as before, plus `world: "user_script"`.

- Chrome allows this only after the user turns on **Allow User Scripts** on
  the extension's Details page (Chrome 138+). Until then the tool returns
  `error: "user_scripts_disabled"`, and its description tells the agent to ask
  for it.
- Scripts see the DOM, but not the page's own JavaScript variables
  (`window.someAppState` is `undefined`).
- On pages that allow `eval`, nothing changes: scripts still run in the
  page's world.
- Any script on the page can use this path too, as it can the network tunnel.
  That gives the page `eval` inside the user-script world, which its own CSP
  would otherwise forbid.

## Build and load

```sh
./extension/build.sh          # builds frontend/dist, copies widget.js + icons
./extension/build.sh --no-frontend   # reuse an existing frontend/dist
```

Without a checkout: each GitHub release after 0.3.0 carries the built
extension as `voitta-extension-X.Y.Z.zip`. It holds one folder,
`voitta-extension/`, with the release's version in its manifest; load that
folder instead of `extension/`.

Then chrome://extensions → Developer mode → Load unpacked → `extension/`, and
on the extension's Details page turn on **Allow User Scripts** (for
`browser_eval`).
The backend is set on the extension's options page: right-click the toolbar
icon and choose **Options**. It is not in the assistant panel's Settings, and
the popup's "Settings" link is hard to reach, because the popup closes as soon
as it has launched the widget. The default is the desktop app's plain-HTTP
listener, `http://127.0.0.1:12359`. A backend that isn't on loopback needs a
host permission, which Save asks for.

`build.sh` re-emits the bundle with non-ASCII characters escaped:
`chrome.scripting` refuses a file that contains Unicode noncharacters, and the
widget bundle has `U+FFFF` literals.

## Limits

- The widget is bundled into the extension, so it can lag the backend. Rebuild
  and reload the extension after a frontend change.
- Only the top frame. The tunnel is as trusted as the page: page scripts can
  call the shimmed `fetch`, as with the bridge, and the backend still applies
  the site's approval.
- Chrome may stop the service worker. Open sockets then close and the widget's
  socket.io client reconnects; this path hasn't been exercised yet.
