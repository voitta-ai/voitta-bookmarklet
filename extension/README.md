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

- `src/page-shim.js` replaces `fetch` and `WebSocket` for backend URLs only, as
  the bridge's boot shim (`backend/app/bridge.py`) does, and sets bridge mode
  so socket.io uses WebSockets and images load as `blob:` URLs.
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

## Build and load

```sh
./extension/build.sh          # builds frontend/dist, copies widget.js + icons
./extension/build.sh --no-frontend   # reuse an existing frontend/dist
```

Then chrome://extensions → Developer mode → Load unpacked → `extension/`.
Settings (the extension's options page) sets the backend; the default is the
desktop app's plain-HTTP listener, `http://127.0.0.1:12359`. A backend that
isn't on loopback needs a host permission, which Save asks for.

`build.sh` re-emits the bundle with non-ASCII characters escaped:
`chrome.scripting` refuses a file that contains Unicode noncharacters, and the
widget bundle has `U+FFFF` literals.

## Limits

- The widget is bundled into the extension, so it can lag the backend. Rebuild
  and reload the extension after a frontend change.
- `browser_eval` still can't run on these pages (no `'unsafe-eval'` in the
  page's world); `read_page` (#35) is the eval-free way to read them.
- Only the top frame. The tunnel is as trusted as the page: page scripts can
  call the shimmed `fetch`, as with the bridge, and the backend still applies
  the site's approval.
- Chrome may stop the service worker. Open sockets then close and the widget's
  socket.io client reconnects; this path hasn't been exercised yet.
