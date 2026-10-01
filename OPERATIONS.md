# Voitta Compute — Operations & Data Flow

> A detailed, diagram-first walkthrough of how the system actually works at
> runtime: the menu-bar shell and first-launch installer, TLS + bookmarklet
> delivery, the chat turn and agent loop, tool registry and dispatch, the
> hybrid browser round-trip, plugins and MCP connectors, the scripts/reports
> compute runtime, python_storage, the RAG indexes, settings, persistence,
> and packaging.
>
> Diagrams are [Mermaid](https://mermaid.js.org/). GitHub, VS Code (with the
> Mermaid extension), and most markdown viewers render them inline.

## Contents

1. [System overview](#1-system-overview)
2. [Process lifecycle: menu-bar shell & installer](#2-process-lifecycle-menu-bar-shell--installer)
3. [Ports, TLS & bookmarklet delivery](#3-ports-tls--bookmarklet-delivery)
4. [The chat turn: from page host to agent loop](#4-the-chat-turn-from-page-host-to-agent-loop)
5. [Tool registry & dispatch](#5-tool-registry--dispatch)
6. [Browser primitives: the hybrid round-trip](#6-browser-primitives-the-hybrid-round-trip)
7. [Plugin system](#7-plugin-system)
8. [MCP connectors](#8-mcp-connectors)
9. [Scripts & reports: the compute runtime](#9-scripts--reports-the-compute-runtime)
10. [Workspace & python_storage](#10-workspace--python_storage)
11. [RAG subsystem](#11-rag-subsystem)
12. [Settings & persistence](#12-settings--persistence)
13. [Embedded MCP debug server](#13-embedded-mcp-debug-server)
14. [Paths, env vars & logging](#14-paths-env-vars--logging)
15. [Packaging & release](#15-packaging--release)

---

## 1. System overview

Voitta Compute is a macOS menu-bar app ([backend/app/desktop.py](backend/app/desktop.py),
`rumps`) that hosts a single FastAPI/Chainlit ASGI app
([backend/app/main.py](backend/app/main.py)) on two local listeners — TLS on
`127.0.0.1:12358` and plain-HTTP on `127.0.0.1:12359`. A **bookmarklet**
injects a self-contained React widget (`widget.js`) into any page the user is
on; the widget opens a Chainlit websocket back to the local backend, where an
LLM **agent loop** runs chat turns with a host-gated tool registry. The user
picks one of **four "brains"**: three API providers (`anthropic`, `openai`,
`gemini`) that share the normalised in-process agent loop, plus
**`claude_code`** — the Claude Agent SDK driven by a Claude Pro/Max
*subscription* (no API key; a one-time OAuth token), which runs the Claude Code
engine as a subprocess and owns its own conversation + session store. Tools are
either *server-side* (Python: scripts, RAG, storage) or *hybrid* (round-trip
into the user's browser tab: `browser_eval`, screenshots, plugin scrapers); the
same tool suite is bridged to the subscription brain over an in-process MCP
server.
Plugins extend every layer — system prompt, Python tools, browser primitives,
settings panels, and remote **MCP servers** whose tools get prefixed and
merged into the same registry.

```mermaid
flowchart TB
    subgraph page["Any web page (user's browser tab)"]
        BM["bookmarklet"] -->|injects| W["widget.js<br/>React in closed shadow DOM"]
        PRIM["browser primitives<br/>(core + plugin widget.ts)"]
    end

    subgraph shell["Menu-bar shell (rumps, desktop.py)"]
        TRAY["V glyph · activity colors<br/>menu: bookmarklets · settings ·<br/>restart · rebuild · reset"]
        INST["first-launch installer<br/>certs → pip → lib-sources → RAG"]
    end

    subgraph app["FastAPI app (app.main:app) — :12358 TLS / :12359 plain"]
        CL["Chainlit mount /chainlit<br/>(socket.io, threads, steps)"]
        AG["agent.py run_turn()<br/>LLM loop: anthropic · openai · gemini"]
        ASDK["services/agent_sdk/*<br/>4th brain: Claude Code engine<br/>(subscription, own session store)"]
        REG["tools/registry.py<br/>host-gated ToolSpec registry"]
        PLG["plugins.py loader<br/>manifest → prompt · tools · MCP"]
        MCPC["services/mcp/*<br/>connectors → synthetic ToolSpecs"]
        SCR["reports/* compute runtime<br/>build(ctx) → HTML report"]
        RAGQ["tools/rag/*<br/>Chroma + BM25 hybrid"]
        ROUTES["routes/*: settings · plugins ·<br/>workspace · reports · auth · mcp"]
    end

    subgraph stores["Stores (per-user in server mode)"]
        SQL[("conversations.sqlite")]
        SCRD[("scripts/ + scripts_state/")]
        PYS[("python_storage/cache/")]
        RAGI[("rag/.chroma · .bm25 ·<br/>.chroma_code · .bm25_code")]
        SET[("~/.config/voitta-compute/<br/>settings.json")]
    end

    REMOTE["Remote MCP servers<br/>(e.g. voitta-rag-enterprise)"]

    W <-->|"socket.io ws"| CL
    CL --> AG --> REG
    CL -->|"provider == claude_code"| ASDK
    ASDK -->|in-process MCP bridge| REG
    ASDK -->|subprocess| ENG["Claude Code engine (Node)<br/>own session store under<br/>CLAUDE_CONFIG_DIR/projects/"]
    AG -->|hybrid tools| PRIM
    PLG --> REG
    PLG --> MCPC -->|streamable-http| REMOTE
    MCPC --> REG
    REG --> SCR & RAGQ
    SCR --> PYS & SCRD
    RAGQ --> RAGI
    CL --> SQL
    ROUTES --> SET
    TRAY --> app
    INST --> RAGI
```

**Key invariants:**

- The backend binds **loopback only** — nothing is reachable from the network.
  Server deployments (multi-user) add Google-OAuth auth and per-user data
  isolation on the same codebase.
- Tool visibility is recomputed **every turn** from the page host
  (`registry.visible_for_host`), so what the LLM can call depends on which
  site the bookmarklet was clicked on.
- Settings are re-read from disk **per turn / per call** (no restart needed
  for provider, model, API key, or MCP credential changes).
- The three API providers share the normalised loop and are **freely
  interchangeable mid-conversation** (the transcript is provider-neutral). The
  `claude_code` brain is the exception: it owns its own session, so its history
  lives in the Claude Code engine's store (not `conversations.sqlite`) and is
  surfaced by a mode-aware dropdown rather than via Chainlit thread resume.
- The script runtime is a **power tool, not a sandbox** — user scripts run
  with full Python builtins in the backend process (thread-pooled, 120 s
  timeout).

---

## 2. Process lifecycle: menu-bar shell & installer

### Boot sequence

Entry point in the frozen `.app` is
[src/voitta_compute/\_\_main\_\_.py](src/voitta_compute/__main__.py); dev mode
runs uvicorn directly via [start.sh](start.sh).

```mermaid
flowchart TB
    M["__main__.py"] --> LOCK["instance flock<br/>~/…/Voitta Compute/.voitta.lock<br/>(second launch exits)"]
    LOCK --> SEED["seed bundle resources →<br/>frontend/dist · docs/ · plugins/ (reseeded each launch) ·<br/>lib-sources/ (stamp-gated)"]
    SEED --> ENV["set env: VOITTA_PROJECT_ROOT ·<br/>VOITTA_DATA_ROOT · PIP_PREFIX=userbase/ ·<br/>redirect stdout/stderr → voitta.log"]
    ENV --> DESK["desktop.py main()"]
    DESK --> CAR["CHAINLIT_APP_ROOT + VOITTA_FRONTEND_DIST<br/>set before any chainlit import"]
    CAR --> FRESH["installer.ensure_fresh_deploy():<br/>version stamp changed ⇒ wipe userbase/ + certs/<br/>+ install_state.json"]
    FRESH --> NEED{"certs present ∧ packages complete ∧<br/>lib-sources current ∧ RAG built?"}
    NEED -->|no| WIN["InstallWindow — phased setup<br/>on a worker thread"]
    NEED -->|yes| UV["_start_uvicorn(): two daemon threads<br/>TLS :12358 + plain :12359"]
    WIN --> UV
    UV --> RUMPS["rumps event loop (menu bar)"]
```

### First-launch installer phases

The `.app` bundles only lightweight boot deps (FastAPI, uvicorn, rumps,
pyobjc, pip). Everything heavy is installed at first launch into a user-owned
`userbase/` prefix — keeps the bundle small and the heavy deps upgradeable
without re-notarising.

| Phase | Check | Action | Where it lands |
|-------|-------|--------|----------------|
| 0 Certificates | `certs.is_present()` | `mkcert -install` + `mkcert 127.0.0.1 localhost` (bundled mkcert binary preferred) | `<PROJECT_ROOT>/certs/127.0.0.1+1{,-key}.pem` |
| 1 Python packages | `installer.is_complete()` (install_state.json) | pip-install `_CORE_HEAVY_PACKAGES` (fastmcp, chainlit, anthropic, openai, google-genai, claude-agent-sdk, numpy, pandas, scipy, matplotlib, plotly, chromadb, bm25s, sqlalchemy, …) + plugin-declared deps, `--find-links` bundled wheels | `userbase/lib/python3.x/site-packages` (PIP_PREFIX) |
| 2 Source libraries | `installer.lib_sources_need_update()` (SHA stamp vs bundled `code_sources_version.txt`) | shallow-clone the pinned submodules | `<data>/lib-sources/` (elk, elkjs, jinja, three.js) |
| 3 RAG indexes | `rag_build.is_built()` (content/SHA stamps) | run [scripts/build_rag.py](scripts/build_rag.py) in-process via `runpy` | `<PROJECT_ROOT.parent>/rag/` |

Version-gated wipe ([backend/app/installer.py](backend/app/installer.py)
`ensure_fresh_deploy`): when `.deployed_version` ≠ the running version, the
shell wipes `userbase/`, `backend/certs/`, and `install_state.json` so phases
0–1 re-run cleanly. The RAG index is **not** wiped — its own content stamps
decide whether a rebuild is needed.

Phase 1 installs the `claude-agent-sdk` **Python driver**, but the Claude Code
**engine** (Node binary on `$PATH`) is a separate user-side prerequisite the
app does not bundle. The subscription brain probes for it at runtime
(`is_available()`) and the selector option stays hidden when it's absent — so
the three API providers work with no extra install.

> **numpy is pinned `>=1.26,<2.5`.** The ceiling is numba's (numba is pulled in
> by the voice assistant's `mlx_whisper`). Without it the core install resolves
> numpy 2.5, the running process imports it, and a *later* voice install
> downgrades numpy on disk to 2.4.x while the live process keeps 2.5 in memory —
> numba's import-time check then crashes the voice thread (red tray icon). The
> cap keeps one numba-compatible numpy from boot. Bump it when numba supports
> numpy 2.5+. See §12 (Voice assistant).

### Tray menu

| Item | Action |
|------|--------|
| About Voitta | version + brand text |
| Open in browser | `https://127.0.0.1:12358` (http fallback if no certs) |
| Copy bookmarklet | normal injector (most sites) |
| Copy bookmarklet (Salesforce/strict CSP) | bridge/popup variant — see §3 |
| Settings… | server URL, TLS/RAG/plugins/MCP status, **MCP-debug toggle** (§13) |
| Server: status / Restart server | `should_exit` → 1.5 s port-free pause → relaunch threads |
| Workspace… | opens `{server_url}?workspace=1` |
| (Re)create TLS certificates… | interactive mkcert re-run, prompts restart |
| Rebuild… | clears install stamps → installer phases re-run |
| Reset… | deletes `rag/`, `scripts_state/`, `python_storage/`, `settings.json` |
| Quit Voitta | exit (daemon threads reap with the process) |

The glyph is always **"V"**, colored by the topmost active tool category
([backend/app/activity.py](backend/app/activity.py)): red `error`/`code_edit`,
purple `compute`, blue `report`, yellow `drive_download`, green `rag`, orange
`web`, gray `generic`. Tool dispatch brackets every handler with
`activity.begin/end`, and the tray polls at 0.4 s.

---

## 3. Ports, TLS & bookmarklet delivery

| Listener | Port | Purpose |
|----------|------|---------|
| TLS | `127.0.0.1:12358` (`PORT`, [backend/app/config.py](backend/app/config.py)) | primary origin — `https:` so the widget is a Secure Context on https pages (no mixed-content blocks) |
| Plain HTTP | `127.0.0.1:12359` (`PLAINTEXT_PORT`) | bridge popup for hardened-CSP sites (localhost http is still a Secure Context) |

Both listeners serve the **same ASGI app**. TLS uses mkcert-issued certs for
`127.0.0.1` + `localhost`, signed by a local CA that `mkcert -install` puts in
the system trust store — the private key never leaves the machine.

### Widget serving

`widget.js` is a single ~3.6 MB IIFE bundle (Vite lib build,
`inlineDynamicImports: true` — a bookmarklet can only inject one file). The
FastAPI catch-all route serves files from `FRONTEND_DIST`
(env `VOITTA_FRONTEND_DIST`, dev fallback `frontend/dist/`) with
`no-cache` headers, falling back to `index.html`, then to `widget.js` itself.

### Two bookmarklets ([backend/app/bookmarklets.py](backend/app/bookmarklets.py))

```mermaid
sequenceDiagram
    autonumber
    participant P as Page
    participant B as Bookmarklet
    participant POP as Bridge popup (:12359)
    participant BE as Backend

    rect rgb(240,248,255)
    note over P,BE: NORMAL — ordinary sites
    B->>P: append <script src="https://127.0.0.1:12358/widget.js">
    P->>BE: GET /widget.js (TLS origin)
    P->>BE: socket.io → /chainlit/ws/socket.io
    end

    rect rgb(255,248,240)
    note over P,BE: BRIDGE — hardened CSP (script-src/connect-src blocked)
    B->>POP: window.open("http://127.0.0.1:12359/bridge")
    POP->>BE: GET /bridge + relay.js (same-origin)
    POP-->>P: postMessage boot code → eval in page
    note over P: window.__voittaBridge = true<br/>fetch/WebSocket shims installed
    P-->>POP: postMessage request frames
    POP->>BE: same-origin fetch / ws relay
    end
```

- The widget derives its backend origin from `window.__voittaBackendOrigin`
  (bridge), else `document.currentScript.src`, else `location.origin`
  ([frontend/src/widget.tsx](frontend/src/widget.tsx)).
- Under the bridge, the Chainlit client is forced WebSocket-only (XHR polling
  isn't shimmed) and chat images are fetched into `blob:` URLs to dodge
  `img-src` CSP ([frontend/src/chat/BridgeImg.tsx](frontend/src/chat/BridgeImg.tsx)).
- The widget mounts in a **closed shadow DOM** under a fixed host element
  (`#voitta-compute-host`, z-index 2147483647); all CSS is inlined into one
  `<style>` node, themes switch via `data-theme`, layout (`chat-left`/
  `chat-right`) via `data-layout`. Browser-side primitives that need DOM
  access get the shadow root through `window.VoittaBookmarklet.getShadowRoot()`.
- **Hostile-page event guard**
  ([frontend/src/lib/event-guard.ts](frontend/src/lib/event-guard.ts), installed
  at mount): sites like Google Sheets/Docs register document-capture listeners
  that *act on* keyboard/clipboard/mouse events globally (own context menu,
  paste-into-grid, focus stealing), swallowing input aimed at the widget. The
  guard does a full takeover for widget-origin events (`e.target === host` at
  window capture — exact, thanks to closed-shadow retargeting):
  `stopImmediatePropagation()` hides the event from the page entirely (browser
  defaults survive — text insertion, native context menu, selection), then a
  clone (`composed: false`, `clipboardData`/`dataTransfer` forwarded via
  getters) is dispatched synchronously on the real inner target so React
  handlers run normally; `preventDefault` on the clone is mirrored to the
  original. Known limit: a page hijacker on *window* capture registered before
  the bookmarklet still precedes the guard (not seen in the wild — Sheets/Docs/
  Notion all use document).

### HTTP plumbing quirks ([backend/app/main.py](backend/app/main.py))

Three non-obvious pieces keep cross-origin loopback traffic alive:

- **Chrome Private Network Access**: a raw ASGI middleware short-circuits
  combined CORS+PNA preflights *before* CORSMiddleware (which would never
  echo `Access-Control-Allow-Private-Network`) and stamps the header on every
  response — without it, modern Chrome blocks public-site → 127.0.0.1
  requests with `net::ERR_FAILED`.
- **engineio tuning**: `max_decode_packets` is raised 16 → 128 after the
  Chainlit import (multi-MB screenshot frames arrive as packet bursts that
  would otherwise be dropped); a companion patch
  ([backend/app/services/_bokeh_ws_patch.py](backend/app/services/_bokeh_ws_patch.py))
  applies the same to Bokeh's server.
- **Screenshot stash** (`/api/screenshot-stash`): the report iframe uploads
  full-size captures over HTTP and hands back stash IDs, so large images
  never ride the socket.io channel; the agent loop fetches them by ID.

---

## 4. The chat turn: from page host to agent loop

The page host is the pivot of the whole system: the widget posts it over the
Chainlit window-message channel, and the backend uses it to assemble the
system prompt (which plugins apply) and to filter the tool registry.

```mermaid
sequenceDiagram
    autonumber
    participant W as Widget (ChatPane)
    participant CL as chainlit_app.py
    participant S as settings.json
    participant P as plugins.for_host()
    participant A as agent.run_turn()
    participant L as LLM provider
    participant R as registry.dispatch()

    W->>CL: window message "host:{location.host}"
    note over CL: cl.user_session["host"] = host (port preserved)
    W->>CL: user message
    CL->>S: read provider · api_key · model · caps (fresh every turn)
    CL->>P: for_host(host) → matching plugins
    note over CL: system prompt = join(plugin.system_prompt …)<br/>default plugin ("*") always contributes the base prompt
    CL->>A: run_turn(ToolCtx(session_id, host, email))

    loop until stop_reason ≠ "tool_use" (≤ max_tool_iterations, default 25)
        A->>R: visible_for_host(host) → tool schemas
        A->>L: stream(NormalisedRequest: system, messages, tools)
        L-->>A: BlockStart/Delta/Stop … (text streams into cl.Message,<br/>tool args stream into cl.Step)
        alt tool_use blocks present
            A->>R: dispatch ALL tool calls in parallel
            R-->>A: ToolResult{ok, result | error, latency_ms}
            A->>A: append assistant blocks + tool_result message
        else final text
            A-->>CL: persist assistant message, return
        end
    end
```

Details that matter operationally
([backend/app/agent.py](backend/app/agent.py)):

- **Providers**: `anthropic`, `openai`, `gemini` behind one normalised
  streaming protocol ([backend/app/services/llm/](backend/app/services/llm/)).
  Anthropic blocks are the canonical content shape; the other adapters
  convert. The Anthropic adapter pins `https://api.anthropic.com` (strips
  `ANTHROPIC_BASE_URL`) and places two `cache_control: ephemeral` markers
  (last system block, last message block) for prompt caching.
- **Parallel tool dispatch**: all tool_use blocks in one assistant message are
  dispatched concurrently.
- **Images in tool results**: tools can return `_image`/`_images`
  (inline), `_images_chat_only` (chat preview only), or `_images_stash`
  (full-size posted to chat + downsized WebP into LLM context — Anthropic
  only; other providers get a textual note).
- **Caps**: `max_tool_iterations` (default 25) and `max_tokens` (default
  24576) come from settings on every turn.

### The 4th brain: Claude (subscription) via the Agent SDK

When the selected provider is `claude_code`,
[chainlit_app.py](backend/app/chainlit_app.py) forks **before** the normalised
agent loop and routes to
[backend/app/services/agent_sdk/](backend/app/services/agent_sdk/) instead. The
three API providers above are untouched by this path.

- **Runtime** ([runtime.py](backend/app/services/agent_sdk/runtime.py)): each
  turn is one `query()` against the Claude Code engine (a Node subprocess).
  Multi-turn continuity is `resume=<session_id>` (continue-only — no fork). SDK
  events map to the same `cl.Message` / `cl.Step` primitives the main loop uses.
- **Tools** ([bridge.py](backend/app/services/agent_sdk/bridge.py)): the *same*
  host-gated registry suite is exposed to the engine via an in-process MCP
  server (`mcp__voitta__<tool>`); a `can_use_tool` allowlist denies the engine's
  own built-in bash/file/web tools so it acts only through Voitta's tools.
- **Isolation** ([config.py](backend/app/services/agent_sdk/config.py)): a
  per-user `CLAUDE_CONFIG_DIR` and a **pinned, deterministic `cwd`** (sessions
  are keyed by encoded cwd, so it must not drift) under the user's data root.
  The subprocess env injects `CLAUDE_CODE_OAUTH_TOKEN` and strips
  `ANTHROPIC_API_KEY` (the API key would otherwise outrank the subscription
  token).
- **Lazy import**: `claude-agent-sdk` is installed at runtime by the installer
  (like the other heavy LLM deps), so every SDK import is guarded — a missing
  SDK degrades the brain to "unavailable" rather than crashing app boot.
- **History**: sessions are listed from the engine's own store via
  [sessions.py](backend/app/services/agent_sdk/sessions.py) +
  [routes/agent_sdk.py](backend/app/routes/agent_sdk.py); picking one restores
  it on **two** tracks — a backend resume latch (continuity) and a frontend
  transcript replay (display), since the engine store and Chainlit's store are
  separate. Full flow in §12.
- **Busy feedback**: a single self-animating status line (`cl.Step` `type="run"`,
  rendered by [StepView.tsx](frontend/src/chat/StepView.tsx)) shows
  `⠋ Working… · Ns · N tokens` — driven by a 1 s background ticker so the turn
  stays lively during the silent thinking gaps, with the token count summed
  from each `AssistantMessage.usage`. It's the only "busy" element (tool steps
  render themselves) and is `remove()`d when the turn ends — no footer.

---

## 5. Tool registry & dispatch

[backend/app/tools/registry.py](backend/app/tools/registry.py) holds one
global `ToolRegistry` of `ToolSpec`s:

```
ToolSpec: name · description · input_schema · async handler(args, ToolCtx)
          side: "server" | "hybrid"
          host_pattern: str | list | None      (None = visible everywhere)
          global_tool: bool                    (back-fill skips these)
          visibility_check: () -> bool | None  (runtime gate, every turn)
          plugin_name: str | None              (owner, for user extra-hosts)
```

### Visibility: three gates, per turn

```mermaid
flowchart LR
    T["ToolSpec"] --> H{"host_pattern?"}
    H -->|None| V
    H -->|set| M{"strict-suffix match on bare hostname<br/>OR user extra_hosts match<br/>(port-aware, §7)"}
    M -->|no| HIDE["hidden"]
    M -->|yes| V{"visibility_check()?"}
    V -->|False / raises| HIDE
    V -->|True / none| SHOW["in LLM tool list"]
```

- Host matching is **strict suffix** on the bare hostname: `ebay.com` matches
  `www.ebay.com` but not `ebay.com.evil.com`. User-added activation hosts
  (`plugins.<name>.extra_hosts` in settings, managed from Settings → Plugins)
  are OR'd in via [backend/app/services/host_activation.py](backend/app/services/host_activation.py)
  and may pin a **port** (`127.0.0.1:8756` matches only that port).
- `visibility_check` covers state the LLM can't control — e.g. Drive tools
  only appear when at least one Google account is connected; MCP tools only
  while their connector status is `ok`.
- `dynamic_description` (optional callable, same per-turn evaluation as
  `visibility_check`) appends runtime state to a tool's description — e.g.
  the roster of connected Google accounts on every Drive/Sheets tool. A
  raising callable degrades to the static description, never hides the tool.
- Name collisions **log and skip** (keep the prior spec) rather than raise, so
  one bad plugin can't drop its siblings.
- `dispatch()` wraps every handler with the activity tracker (tray glyph) and
  returns a uniform `ToolResult{ok, result, error{kind, message, details},
  latency_ms}` — exceptions never escape to the agent loop.

### Core tools (22)

| Group | Tools | Side |
|-------|-------|------|
| Diagnostics | `now` | server |
| Page context | `get_page_title`, `get_active_theme` | hybrid / server |
| Escape hatch | `browser_eval` (arbitrary JS in the tab; default 30 s, max 120 s) | hybrid |
| Scripts | `list_scripts`, `get_script`, `define_script`, `edit_script`, `delete_script`, `run_script`, `verify_script`, `get_script_errors`, `get_active_report`, `screenshot_report` | server (+1 hybrid) |
| Data / workspace | `list_data`, `preview_data`, `create_folder`, `delete_folder`, `move_to_folder` | server |
| Knowledge | `rag_query`, `rag_get_chunk_range` | server |
| Runtime | `pip_install` (into the live process + user-site) | server |

Plugins and MCP connectors add more on top (host-gated). Load order
([backend/app/tools/load.py](backend/app/tools/load.py)): core tools import
first, then `plugins.load_all()` — plugin code can rely on the registry
being initialised.

---

## 6. Browser primitives: the hybrid round-trip

A *hybrid* tool's Python handler delegates to JavaScript running inside the
user's tab via `call_browser(name, args, ctx)`
([backend/app/tools/browser/](backend/app/tools/browser/)):

```mermaid
sequenceDiagram
    autonumber
    participant H as Tool handler (Python)
    participant CF as cl.CopilotFunction
    participant SOCK as Chainlit socket
    participant CFR as CallFnRouter (Recoil)
    participant PR as primitives[name]()

    H->>CF: call_browser("screenshot_report", args)
    CF->>SOCK: call_fn event
    SOCK->>CFR: callFnState update
    CFR->>PR: dispatch by name
    PR-->>CFR: result | {ok:false, error:{kind, message, details}}
    CFR-->>SOCK: callback
    SOCK-->>H: result (or BrowserToolError raised)
```

- Core primitives live in
  [frontend/src/lib/primitives.ts](frontend/src/lib/primitives.ts):
  `eval_js`, `get_page_title`, `screenshot_report`, `get_page_dump`,
  devtools capture helpers. `show_html_report` / `close_report` are handled
  directly in [frontend/src/chat/CallFnRouter.tsx](frontend/src/chat/CallFnRouter.tsx)
  (they mutate report-pane state).
- Plugins extend the same flat namespace with `registerPrimitive()` from
  their `frontend/widget.ts` (eager-globbed into the bundle at build time) —
  e.g. Google Drive's `download_in_modal`, eBay's `ebay_scrape_search`.
- Errors come back as a structured envelope; the Python side raises
  `BrowserToolError(kind, message, details)` which the registry converts to a
  failed `ToolResult`.

---

## 7. Plugin system

A plugin is a directory under [plugins/](plugins/) with a `manifest.json`
(discovered recursively, so `plugins/google/drive/` works). Current tree:

| Plugin | Hosts | What it adds |
|--------|-------|--------------|
| `default` | `*` | base Voitta system prompt (no tools) |
| `ebay` | ebay.com | browser-side DOM scrapers (`ebay_scrape_search`, `ebay_scrape_item`, …) |
| `google` (drive) | drive.google.com | Drive download tools (multi-account: per-call `account` arg + read probe), OAuth-gated (`visibility_check`), download-modal primitives, custom settings panel (account list, add/connect/default/delete) |
| `google-sheets` | docs.google.com | Sheets tools (multi-account, writes never probe); scripts separately get `ctx.sheets` bound to the script's pinned account |
| `linkedin` | linkedin.com | browser-side page-context extraction |
| `veed` | www.veed.io | reads composition/media/subtitle state from VEED's page context |
| `voitta-enterprise` | enterprise.voitta.ai | branding + the `vre` MCP connector (§8) |

```json
{
  "name": "voitta-enterprise",
  "agent_name": "Voitta",
  "host_patterns": ["enterprise.voitta.ai"],
  "python_module": "voitta_enterprise",
  "frontend_bundle": "frontend/widget.ts",
  "system_prompt": "prompt.md",
  "docs_dir": "docs",
  "mcp_servers": [ { "id": "vre", "url_template": "{host}/mcp",
                     "auth": {"type": "bearer", "token_setting": "plugins.voitta-enterprise.mcp.api_key"},
                     "tool_prefix": "vre_", "expose_tools": "*",
                     "transport": "streamable-http" } ],
  "settings_schema": { "title": "…", "fields": [ … ], "status_probe": "mcp:vre" }
}
```

What each manifest field wires up
([backend/app/plugins.py](backend/app/plugins.py)):

| Field | Effect |
|-------|--------|
| `host_patterns` | gates everything: system prompt contribution, tool visibility, FE widget behaviors. `"*"` = everywhere. Users can extend per plugin via **Settings → Plugins** (`extra_hosts`, port-aware, live-matched — no restart) |
| `system_prompt` | file contents appended to the system prompt when the plugin applies |
| `python_module` | `plugins/<name>/backend/` goes on `sys.path`; importing the module registers ToolSpecs as side effects. Specs without their own `host_pattern` are back-filled with the manifest's; all specs get tagged `plugin_name` |
| `frontend_bundle` | `frontend/widget.ts`, eager-bundled into widget.js via `import.meta.glob`; registers browser primitives |
| `mcp_servers[]` | declares remote MCP connectors (§8) |
| `settings_schema` / `settings_panel` | declarative settings (schema-rendered inside the plugin's expandable card on the Plugins tab) or a custom React panel (`frontend/settings-panel.tsx`, which gets its own tab) |
| `docs_dir` | plugin docs are indexed into the RAG `docs` corpus |

The Settings UI ([frontend/src/SettingsView.tsx](frontend/src/SettingsView.tsx))
shows: **Global** (provider, keys, models, layout, theme) · **Plugins**
(one expandable card per plugin: activation hosts as chips, and — for
schema plugins — the settings form + MCP connector status behind a
chevron) · one tab per plugin that ships a *custom* panel (Google). Schema
fields save through `PUT /api/settings` as dotted-path patches
(`plugins.<name>.<…>`); empty string / null deletes the key.

---

## 8. MCP connectors

Plugins integrate remote MCP servers (e.g. a local voitta-rag-enterprise) as
first-class tools. Design contract: remote tools are **listed at startup and
on explicit refresh only** — never per chat turn; URL and bearer token are
resolved **at call time**, so credential edits apply instantly.

Two endpoint modes per `mcp_servers[]` entry:

- `url_setting` — a dotted settings path holding an explicit URL.
- `url_template: "{host}/mcp"` — the endpoint is **derived from the page
  host per call** (http for loopback hosts, https otherwise), so the same
  plugin talks to whichever instance the bookmarklet is on
  (`https://enterprise.voitta.ai/mcp` on the portal,
  `http://127.0.0.1:8756/mcp` locally). Refresh probes **every** candidate —
  explicit URL, then the template applied to each manifest pattern and user
  extra-host — recording a per-endpoint status (the settings card shows one
  row per instance), registering the **union** of their tools (merged by
  name; calls route per page host), and remembering the first reachable URL
  (`active_url`) as the fallback for host-less callers (the `vre://`
  resolver). An explicit settings URL always wins over the template.

Bearer auth is **per instance**: `auth.token_map_setting` points at a
`{host: key}` map (Settings → Plugins renders one key field per activation
host); the token is matched to the host of the URL being *called* — exact
`host:port` first, then bare-hostname suffix — with the legacy single
`token_setting` as the any-host fallback.

```mermaid
sequenceDiagram
    autonumber
    participant L as plugins.load_all()
    participant CR as mcp/registry.py (connectors)
    participant S as settings.json
    participant RM as Remote MCP server
    participant TR as tool registry
    participant A as agent loop

    L->>CR: register_connector(MCPServerDecl) — no network
    note over CR: startup refresh_all() / POST /api/plugins/{name}/refresh
    CR->>S: read url_setting + token_setting
    CR->>RM: fastmcp Client (streamable-http, bearer) → tools/list
    alt unreachable
        CR->>CR: status=unreachable — keep prior tools (transient blip)
    else 401/403
        CR->>CR: status=unauth — drop owned tools
    else ok
        CR->>TR: drop owned specs, synth_tool_spec per remote tool:<br/>name = tool_prefix + remote name (vre_search) ·<br/>description tagged [MCP · plugin:id] ·<br/>handler re-reads url/token per call ·<br/>visibility_check: status == "ok"
    end

    A->>TR: dispatch("vre_search", args)
    TR->>RM: call_tool() → normalised result (text · structured · images)
```

- Connector status (`ok` / `unauth` / `unreachable` / `not_configured` /
  `unknown`) is surfaced in the plugin's settings tab via the
  `status_probe: "mcp:<id>"` badge. Each activation-host row carries its own
  **Connect/Refresh** button (`POST /api/plugins/{name}/refresh?host=…`),
  which re-probes only that endpoint and re-merges the registration with the
  others' cached results; **Refresh all** (no `?host=`) re-probes every
  candidate.
- Synthesised specs inherit the plugin's `host_patterns` (plus user
  `extra_hosts`) — if the page host doesn't match, the agent **does not see**
  the `vre_*` tools at all. That is the first thing to check when the agent
  starts improvising with `browser_eval` against an MCP endpoint instead of
  calling first-class tools.
- `GET /api/plugins` returns plugins + connector status + `extra_hosts`
  without re-probing.

---

## 9. Scripts & reports: the compute runtime

The LLM authors persistent Python scripts whose `build(ctx)` returns an HTML
string; the result renders in an iframe **report pane** inside the widget.

### Authoring & execution

```mermaid
sequenceDiagram
    autonumber
    participant A as Agent (LLM)
    participant DS as define_script / edit_script
    participant SB as sandbox.py
    participant ST as store (scripts/{slug}/)
    participant RS as run_script → dispatch.py
    participant RH as renderers/html.py
    participant FE as Report pane (iframe)
    participant EV as render_events

    A->>DS: define_script(name, code)
    DS->>SB: smoke_test: compile + build(ctx) with throwaway ctx
    alt build raises
        SB-->>A: traceback — nothing written to disk
    else ok
        DS->>ST: atomic write code.py + meta.json
    end

    A->>RS: run_script(name, args?)
    RS->>SB: asyncio.to_thread(exec build(ctx)) — 120 s cap, Agg matplotlib
    SB-->>RS: RunResult{ok, result, ctx.inline[], traceback?}
    RS->>FE: ctx.inline items → chat messages (text/image/json)
    alt build returned str (HTML)
        RS->>RH: render_html: inject _panel_shim.js + html2canvas shim,<br/>meta voitta-slug/render-id; cache (slug, render_id) LRU-64
        RS->>FE: call_fn show_html_report(iframe URL /api/html-report?id&render_id)
        FE->>EV: POST /api/report-render-events {ready|error, inventory}
        EV-->>RS: wake awaiter (default ~8 s) → status
    else returned None
        RS-->>A: status "no-render" (inline only)
    end
    RS-->>A: {ok, status: ready|no-render|errored|timeout|error, elapsed_ms, inventory?}
```

### Script typing: declared kind + observed effects

Every script's meta carries two axes
([script_typing.py](backend/app/reports/script_typing.py)):

- **`kind`** — the *declared* contract: `report` (returns HTML) · `chat`
  (emits inline) · `job` (side effects are the point). Set at
  `define_script` (explicit `kind` arg wins, else inferred from the
  dry-run smoke outcome), re-declarable via `edit_script(kind=…)`.
  Legacy scripts are `None` until classified (seeded to `report` if
  `last_kind == "html"`, else stamped on their first run).
- **`effects`** — *observed* facts, recorded from every smoke + live run
  as a **sticky union** (`renders_html`, `emits_inline`,
  `writes_external`; bits only turn on — a conditional writer must stay
  gated after a run that happened not to write). The only reset is an
  explicit `kind` re-declare on `edit_script`, which re-baselines to
  that edit's smoke observation.

Consequences wired into the runtime:

- **Smoke tests dry-run external writes** — `RecordingSheetsClient`
  stubs `put`/`post` with shape-correct synthetic responses at
  define/edit time (a script must never touch Google before it's saved)
  while still recording `writes_external`, so a sheets-writing script
  is gated from birth.
- **`kind: report` returning `None` is a named error**, not a silent
  `no-render` — the largest source of "ran fine, showed nothing".
- **Confirm gate**: `run_script` on a script with sticky
  `writes_external` (or an unclassified legacy script whose source
  mentions `ctx.sheets`) returns `status: "needs-confirmation"` until
  called with `confirm: true` — the tool description mandates asking
  the user first (soft gate, same trust model as `sheets_write_range`).
  Exempt: scripts defined/edited in the same session within ~10 min
  (in-memory latch; single-process). The workspace pane's run button is
  ungated — the human sees the `✍ sheets` badge.
- **Drift** (declared vs observed disagreement) is computed at read
  time — never stored — and surfaced as ⚠ in `list_scripts` and the
  workspace pane (`script_kind`, `effects`, `drift` fields; named
  `script_kind` on the wire because `kind` is the item-type
  discriminator there).

### The `ctx` API ([backend/app/reports/ctx.py](backend/app/reports/ctx.py))

| Surface | Calls |
|---------|-------|
| Inline emit | `ctx.text(md)` · `ctx.image(bytes\|b64, mime, alt)` · `ctx.json(value)` · `ctx.log(*parts)` |
| Inputs | `ctx.args` (from `run_script(args=…)`) · `ctx.host` (page host — drives theming) |
| Theme | `ctx.theme()` → the active plugin palette as CSS-variable dict, so reports match the host site's skin |
| Data | `ctx.snapshot(handle)` · `ctx.file(handle, name?)` · `ctx.dataframe(handle)` (curves.pkl) · `ctx.raw(handle)` · `ctx.ensure_local(ref)` |
| Sheets | `ctx.sheets` — Google Sheets client bound to the script's **pinned account** (email captured at `define_script` time, stored in script meta; re-pin via `edit_script(google_account=…)`). Missing/disconnected/scope-less pin → hard error naming the account on first use; no pin → the settings-default account. Wrapped in a `RecordingSheetsClient`: writes set `effects.writes_external`; **smoke tests always dry-run writes** (shape-correct synthetic responses — a script never touches Google at define/edit time) |

### Observability

- **The panel shim** (`/api/_panel_shim.js`, injected into every report):
  signals render readiness (waits for Bokeh/Panel if present), aggregates
  `window.onerror` / `unhandledrejection` / `console.error` into render
  events, answers `measure`/`reflow` postMessages from the widget, and
  composites nested-iframe WebGL canvases (three.js scenes) during capture.
- **Render events** ([backend/app/reports/render_events.py](backend/app/reports/render_events.py)):
  per-slug in-memory ring (64, consecutive-dup coalescing) + on-disk JSONL at
  `scripts_state/errors/<slug>.jsonl` (200-line FIFO). `get_script_errors`
  merges both; `verify_script` reads the last render's **inventory** snapshot
  (`scripts_state/inventory/<slug>.json`, written by the FE shim) — a way to
  check what actually rendered without taking a screenshot.
- **`screenshot_report`** (hybrid): html2canvas inside the iframe, optional
  reflow to a 1920 px desktop layout; full-size PNG to chat, downsized WebP
  into LLM context.
- The report pane reports its active tab to `POST /api/workspace/active`, so
  `get_active_report` tells the LLM what the user is looking at.

Scripts persist under `scripts/{slug}/code.py` + `meta.json` (or
`scripts/folders/{folder}/{slug}/…`), atomic-written; example scripts ship in
[scripts/reports/](scripts/reports/).

---

## 10. Workspace & python_storage

`python_storage` is the snapshot store for data the agent pulls in (downloads,
Drive files, scraped frames, remote MCP assets) — keeping bytes **out of the
LLM context** and addressable by handle.

```
python_storage/cache/
├── snapshot_py_<8 hex>/         handle = py_xxxxxxxx (secrets.token_hex(4))
│   ├── meta.json                kind · created_at · files[] · origin{source, url, …} · mime_type
│   ├── raw.json                 verbatim JSON body (when applicable)
│   ├── curves.pkl               pickled DataFrame (curves-kind only)
│   └── <data files>             the actual payload (csv, png, …)
└── folders/<name>/              user folders (folder.json + snapshots)
```

- LLM-facing: `list_data` (enumerate handles + provenance), `preview_data`
  (inline image/text preview), folder management tools; scripts read via
  `ctx.file/dataframe/raw`.
- HTTP-facing: the workspace router
  ([backend/app/routes/workspace.py](backend/app/routes/workspace.py)) serves
  the merged scripts+data+folders listing, file downloads
  (`/api/workspace/data/{handle}/files/{name}`), move/delete operations, and
  an in-band script-run endpoint for the **Workspace panel** (a draggable
  panel in the widget; also reachable via tray → Workspace…).
- In server mode every path above is per-user: `UserPath` re-resolves against
  the current-user contextvar on each access (§12).

---

## 11. RAG subsystem

Self-knowledge for the agent: two corpora, hybrid retrieval, no network.

| Corpus | Sources | Chunking |
|--------|---------|----------|
| `docs` | `docs/*.md` + `plugins/*/docs/*.md` | markdown-header split, target 700 chars (overlap 150, hard max 900 ≈ 256 WordPiece tokens) |
| `code` | `lib-sources/` submodules: **elk, elkjs, jinja, three.js** | Python AST-bounded / JS-TS regex-bounded, hard max 900, fallback windowing |

```mermaid
flowchart LR
    subgraph build["scripts/build_rag.py (in-process via rag_build.py)"]
        SRC["docs + plugin docs · lib-sources"] --> CH["chunkers"]
        CH --> EMB["all-MiniLM-L6-v2<br/>(Chroma ONNX: CoreML / CUDA / CPU)"]
        EMB --> CHR[(".chroma / .chroma_code")]
        CH --> BM[(".bm25 / .bm25_code<br/>+ manifest.json")]
    end
    subgraph query["tools/rag (lazy singleton per corpus)"]
        Q["rag_query(query, top_k, dense_weight, corpus)"] --> D["dense: cosine sim"]
        Q --> S["sparse: BM25"]
        D & S --> F["fuse: w·dense + (1−w)·sparse<br/>default dense_weight 0.9"]
        F --> R["hits → rag_get_chunk_range<br/>stitches ±N chunks of a file"]
    end
    CHR --> D
    BM --> S
```

**Build triggers & stamps** ([backend/app/rag_build.py](backend/app/rag_build.py)):
installer phase 3, or lazily on first `rag_query` if missing. Rebuilds are
skipped when stamps match: docs = SHA-256 over every `.md` (path, content)
under `DOCS_DIR` + `PLUGINS_DIR`; code = the lib-sources submodule SHAs (dev:
`git submodule status`; frozen app: bundled `code_sources_version.txt`).
Stamps live at `~/.config/voitta-compute/.docs_content_hash` /
`.code_source_hash`; the index lives in `<repo>/rag/` (dev) or the seeded data
dir (app). `RagNotBuilt` errors from the tools carry a diagnostic of which
piece is missing. Embedding runs on CoreML (Apple Silicon) or CUDA when
available — [server-start.sh](server-start.sh) auto-detects `nvidia-smi` on
Linux and swaps in `onnxruntime-gpu` + cuDNN.

> The sibling repo `voitta-rag-data` is **not** consumed by this pipeline —
> the runtime index is built exclusively from docs + lib-sources.

---

## 12. Settings & persistence

### Settings (`~/.config/voitta-compute/settings.json`, 0600)

One nested JSON blob, atomic-written (tmpfile + `os.replace`)
([backend/app/services/user_settings.py](backend/app/services/user_settings.py)):

```json
{
  "provider": "anthropic",
  "api_keys":  {"anthropic": "sk-…"},
  "models":    {"anthropic": "claude-sonnet-4-6", "openai": "gpt-4o",
                "gemini": "gemini-2.0-flash-exp", "claude_code": "claude-opus-4-8"},
  "layout": "chat-right", "theme": "auto",
  "max_tool_iterations": 25, "max_tokens": 24576,
  "mcpDebugEnabled": false,
  "googleOAuth": { "defaultAccount": "acc_1a2b3c4d",
                   "accounts": { "acc_1a2b3c4d": { "label": "Work", "clientId": "…",
                                                   "clientSecret": "…", "tokens": { } },
                                 "acc_9e8f7a6b": { "label": "Personal", "clientId": "…",
                                                   "clientSecret": "…" } } },
  "plugins": { "voitta-enterprise": { "mcp": {"api_keys": {"enterprise.voitta.ai": "vk_…",
                                                            "127.0.0.1:8756": "vk_…"}},
                                      "extra_hosts": ["127.0.0.1:8756"] } }
}
```

`provider` may be `claude_code` (the subscription brain); it has a `models`
entry but **no `api_keys` entry** — its credential is the subscription OAuth
token, stored separately (see below), never in `settings.json`.

- Wire shape is **redacted**: `GET /api/settings` returns
  `has_api_keys: {provider: bool}` instead of keys, and strips every
  `googleOAuth.accounts.*.tokens` blob (each account keeps only its
  `account_email`; a not-yet-migrated legacy flat blob is redacted too).
  Keys are write-only. It also returns
  `agent_sdk: {available, has_token}` — whether the Claude Code engine is
  installed (gates the brain in the selector) and whether this user has a
  stored subscription token.
- `PUT /api/settings` takes typed fields and/or a `dotted` map
  (`"plugins.x.y": value`; `""`/`null` deletes) — the mechanism behind every
  plugin settings panel.
- Read fresh on every consumer access — no caching layer to invalidate.

### Conversations

Chainlit threads/steps persist in SQLite via a custom data layer
([backend/app/data/sqlite_layer.py](backend/app/data/sqlite_layer.py)) at
`<USER_DATA_ROOT>/conversations.sqlite`
(`~/Library/Application Support/Voitta Compute/backend/conversations.sqlite`
on desktop). Desktop/dev maps everything to a single "local" user; uploads go
through a `LocalStorageClient` into a user-scoped folder.

### Identity: desktop vs server mode

```mermaid
flowchart LR
    subgraph desktop["Desktop / dev (default)"]
        D1["no auth — single local user"] --> D2["data under USER_DATA_ROOT directly"]
    end
    subgraph server["Server mode (.env VOITTA_GOOGLE_AUTH_CLIENT_ID/SECRET)"]
        S1["_AuthGuard middleware on /api/* · /chainlit* · /mcp*"]
        S2["/api/auth/google/start → consent →<br/>callback mints Chainlit JWT cookie"]
        S3["set_current_email(email) contextvar<br/>per request / per turn"]
        S4["UserPath lazily re-roots every data path →<br/>USER_DATA_ROOT/users/&lt;email-slug&gt;/…"]
        S1 --> S3 --> S4
        S2 --> S1
    end
```

`UserPath` ([backend/app/services/current_user.py](backend/app/services/current_user.py))
is a lazy pathlib proxy: every operation re-resolves against the current-user
contextvar, so the same module-level constants (`SCRIPTS_DIR`, `STORAGE_ROOT`,
settings dir) serve both modes unchanged. The Chainlit JWT secret comes from
`VOITTA_AUTH_SECRET` or a generated value persisted at
`<USER_DATA_ROOT>/auth_secret`. Desktop mode also patches Chainlit's
`resume_thread` to skip user-identity checks so threads resume without login.

### Projects (workspace isolation + memory)

A **project** ([backend/app/services/projects.py](backend/app/services/projects.py))
is a named root under `USER_DATA_ROOT[/users/<u>]/projects/<slug>/` owning the
four scoped trees — `scripts/`, `python_storage/`, `scripts_state/`,
`uploads/` — plus `PROJECT.md` (memory) and `project.json` (display name).
Global (never project-scoped): settings.json, auth_secret,
conversations.sqlite, `claude_code/`, logs, voice.

- **Mechanism**: `project_data_root()` inserts one segment below
  `user_data_root()` — the UserPath lazy-resolution trick one level deeper.
  Only the four trees' resolvers point at it (paths.py, python_storage,
  uploads dir + uploads route).
- **Active project**: `settings.json.activeProject` (per-user for free),
  2 s TTL cache; switch via `POST /api/projects/active`. Routes:
  `GET/POST /api/projects`, `PATCH/DELETE /api/projects/{slug}`. Delete =
  **archive** into `legacy/_archived/<slug>` (never destroys); Legacy is
  undeletable.
- **Legacy adoption**: first `project_data_root()` on a pre-projects data
  root moves the four trees into `projects/legacy/` once (lock + marker =
  legacy's project.json). Lazy ⇒ works per-user in server mode.
- **Threads**: one global conversations.sqlite; the data layer stamps
  `metadata.project` on thread upsert (first write wins — a thread stays in
  its birth project) and `list_threads` post-filters by the active slug
  (untagged = legacy). Uploads for old threads live in Legacy, consistent
  because old threads *are* Legacy.
- **Memory**: `PROJECT.md` injected into every system prompt (both brain
  paths via `_compose_system_prompt`), appended by the `project_remember`
  tool; 64 KB tail-cap.
- **Cross-project access is explicit and read-only**: `list_scripts` /
  `get_script` take a `project` arg; `copy_from_project` is the one
  sanctioned cross-project write (copies a script + its kind/effects/
  google_account meta into the active project). Run/write tools never take
  a project arg.
- **FE**: `ProjectChip` in the drawer header (name + dropdown: switch /
  inline create / delete); switching re-keys the chat pane (project is
  fixed per thread) and fires `voitta-project-switched` so the workspace
  pane refetches.

### Google OAuth (Drive / Sheets data access) — multi-account

Separate from server-mode *login* auth:
[backend/app/routes/google.py](backend/app/routes/google.py) +
[backend/app/services/google_oauth.py](backend/app/services/google_oauth.py)
run per-user consent flows for the Google plugins. The user registers any
number of **named accounts** (Settings → Google), each with its own OAuth
client credentials and its own grant; "add account" can copy an existing
account's client id/secret (one-time value copy — one Google Cloud OAuth
client may authorize many Google accounts).

**Identity model** — three layers, each with one job:

| Layer | Example | Role |
|-------|---------|------|
| `id` | `acc_1a2b3c4d` | random, immutable; dict key, token-cache key, HTTP param |
| `email` | `roman@agnitio.ai` | set by the OAuth callback; **the durable identity** — script pins, `drive://…?account=` refs, snapshot origins all store the email. One connected email per account entry (enforced at token exchange) |
| `label` | `Work` | freely renameable display / LLM alias; never stored in anything durable |

**Selection at runtime** (everything funnels through
`get_access_token(selector, force_refresh=)` — selector = id, email, or
label, case-insensitive; `None` = the settings default):

- **Tool calls (late binding):** every Drive/Sheets tool takes an optional
  `account` arg; the roster of connected accounts (email, label, default
  marker, missing-scopes marker) is appended to each tool description per
  turn via `ToolSpec.dynamic_description`. No arg → default account.
- **Read probe:** a read that 403/404s under a *non-explicit* account is
  retried across the other connected, scope-qualified accounts in
  deterministic order (default first, then creation order) — Drive file /
  spreadsheet IDs are globally unique, so a hit elsewhere is the same file.
  Every fallback hit is logged and the **serving** account's email is
  stamped into the tool result and the snapshot origin. 401s never probe
  (one forced token refresh + retry, then surface). **Writes never probe** —
  wrong account on a Sheets write is a hard, named error.
- **Code (early binding):** scripts pin the account **email at save time**
  (`define_script`/`edit_script` optional `google_account` arg; default =
  the default account's email) into script meta; `run_and_dispatch` passes
  the pin to the sandbox, so re-runs are reproducible regardless of later
  default changes. A pinned account that is missing/disconnected/scope-less
  raises a hard error naming the account — never a silent fall-through.
  `drive://<file_id>?account=<email>` refs pin the fetching account;
  account-less legacy refs resolve via default + read probe.

**Flow mechanics:** the OAuth `state` pins `{account_id, redirect_uri,
user_email}`; the callback rejects cross-user replays (server mode) and
repeats the exact redirect_uri on the token exchange. The redirect URI is
derived from the live request (`url_for`), overridable with
`GOOGLE_OAUTH_REDIRECT_URI` — so desktop (127.0.0.1) and server hostnames
both work. Routes: `GET status` (ordered account list + default) ·
`POST accounts` (create) · `POST accounts/{id}/configure|default|disconnect`
· `DELETE accounts/{id}` · `GET oauth/start?account=` · `GET oauth/callback`.
Changing an account's client credentials revokes + clears its tokens
(label-only changes keep the grant).

**Storage & compat:** tokens live per account under
`googleOAuth.accounts.<id>.tokens` (redacted from the wire), cached in
memory keyed `(settings_path, account_id)` — per-user isolation in server
mode. The pre-multi-account flat blob (`googleOAuth.clientId/tokens`)
migrates in place to a single account entry on first read, grant preserved.
Tool visibility gates stay zero-arg ("any account connected / any account
has the Sheets scope"); per-call scope checks run against the *resolved*
account and name which accounts qualify on failure.

### Claude subscription brain: auth & sessions

The `claude_code` brain authenticates with a Claude Pro/Max **subscription
OAuth token** (`sk-ant-oat01-…`), minted by the user running
`claude setup-token` (in a terminal on desktop, or on their own machine in
server mode). It is **not** a claude.ai login flow inside the product — the app
*accepts* a user-minted token, the defensible posture under the Agent SDK terms.

- **Onboarding** ([onboarding.py](backend/app/services/agent_sdk/onboarding.py)):
  the first `claude_code` turn that fails auth (no token / expired / rejected)
  triggers an **in-chat** prompt explaining `setup-token`, then a masked input
  rendered inline in the chat pane ([TokenPromptModal.tsx](frontend/src/TokenPromptModal.tsx),
  driven by the `prompt_claude_token` `call_fn` round-trip). The token rides the
  socket ACK only — it is **never** sent as a chat message, so it never lands in
  a Chainlit step or `conversations.sqlite`.
- **Storage** ([credentials.py](backend/app/services/agent_sdk/credentials.py)):
  written 0600 to `<CLAUDE_CONFIG_DIR>/voitta_oauth_token` (per-user), injected
  into the engine subprocess as `CLAUDE_CODE_OAUTH_TOKEN`. Validated with a
  throwaway probe turn before the original turn resumes. `Disconnect` (Settings
  → Global) clears it via `POST /api/agent_sdk/disconnect`.
- **Sessions / history dropdown**
  ([ThreadPicker.tsx](frontend/src/ThreadPicker.tsx) ·
  [routes/agent_sdk.py](backend/app/routes/agent_sdk.py)): in `claude_code`
  mode the conversation dropdown lists the engine's own sessions
  (`GET /api/agent_sdk/sessions` → SDK `list_sessions`) instead of Chainlit
  threads. Selecting one drives **two independent restores** — the SDK
  session store and Chainlit's message store are separate, so both are moved:
    1. **Continuity (backend resume).** `POST /api/agent_sdk/select` latches a
       per-user "resume this session" choice
       ([selection.py](backend/app/services/agent_sdk/selection.py)
       `set_pending`), consumed at the next turn by `take_pending` in
       [chainlit_app.py](backend/app/chainlit_app.py) — which overrides the
       live continuation id, so new turns continue *that* engine session
       (`resume=<id>`, continue-only).
    2. **Display (frontend transcript replay).** The engine's sessions live in
       its own store, *not* `conversations.sqlite`, so Chainlit's thread-resume
       can't render them. Instead
       [Drawer.tsx](frontend/src/Drawer.tsx) re-keys the chat pane (fresh mount
       + socket) for the picked session and
       [ChatPane.tsx](frontend/src/ChatPane.tsx) fetches
       `GET /api/agent_sdk/sessions/{id}/messages` (→ `get_brain_transcript`,
       user/assistant text rows) and seeds Chainlit's Recoil `messagesState`
       directly (mapping `{role, text}` → `user_message`/`assistant_message`
       steps with synthetic monotonic `createdAt`). The seed is gated on the
       socket being up so a connect-time reset can't wipe it, and runs once per
       mount. "**+ New conversation**" latches a null resume id and mounts a
       blank pane.

  This store is per-user and single-instance (disk under `CLAUDE_CONFIG_DIR`);
  a multi-instance server would back it with an external `SessionStore`.

> **Mode boundary:** desktop is the clean case (user's own machine, own
> subscription). Multi-tenant server custody of users' subscription tokens is
> built but is the item to confirm against Anthropic's commercial terms before a
> commercial launch; the unambiguous server default is per-user API keys on the
> three API providers.

### Voice assistant ("hey voitta")

Off by default and **not bundled** — opt-in. Toggling Voice on runs a 3-phase
[InstallWindow](backend/app/install_window.py) ([voice_install.py](backend/app/services/voice_install.py)):
pip packages (`sounddevice`, `sherpa-onnx`, `mlx-whisper`, …) → wake-word + VAD
models → whisper model. Packages land in the same version-bump-wiped `userbase/`;
models live under `<USER_DATA_ROOT>/voice` which **survives** the wipe (only the
package phase re-runs after an update). The pipeline
([voice.py](backend/app/services/voice.py)) runs on a daemon thread: sherpa-onnx
wake word → Silero VAD endpointing → mlx-whisper transcription → inject the
transcript into the last-focused session via the same `call_fn` /
`submit_user_text` path the MCP debug tools use. The desktop layer polls
`voice.snapshot()` to render the menu-bar meter (Cocoa-free, like `app.activity`).

Two install-phase gotchas, both fixed and worth remembering:

- **Presence is probed with `find_spec`, never `import`.** These packages wrap
  native extensions; importing a half-written `.so` mid-install **hard-aborts
  the process** (SIGABRT, uncatchable). `packages_missing()` only reads importer
  metadata so the boot-time autostart gate can't crash.
- **numpy is capped `<2.5` in the main installer** (numba's ceiling) — see §2.
  Otherwise installing Voice downgrades numpy on disk while the live process
  holds the newer one, and numba crashes the voice thread.

### Uploads & Chainlit config

Chat attachments go through a `LocalStorageClient`
([backend/app/data/local_storage.py](backend/app/data/local_storage.py)) into
a user-scoped uploads dir served back at `/api/uploads/...`. Session
timeouts, upload size caps, and Chainlit feature flags live in
[.chainlit/config.toml](.chainlit/config.toml).

---

## 13. Embedded MCP debug server

[backend/app/routes/mcp.py](backend/app/routes/mcp.py) mounts a FastMCP
server at **`/mcp`** on the same app — a debugging back door that lets an
external MCP client (e.g. Claude Code) enumerate live widget sessions and
drive them. Tools ([backend/app/services/mcp_server.py](backend/app/services/mcp_server.py)):
`mcp_sessions` (list live bookmarklet sessions), `mcp_session_check`,
`mcp_page` (outerHTML of a session's page), `mcp_eval` (JS in a session's
page), `mcp_inject_text` (inject + run a user message in a session's chat),
`mcp_screenshot` (report pane as PNG), and
`mcp_devtools_install/read/clear`. Three gates, checked per request by
middleware:

1. `mcpDebugEnabled` setting — toggled from the tray Settings dialog,
   effective without restart; default **off**.
2. Loopback-only peer address (127.0.0.1 / ::1).
3. **No `Origin` header** — rejects browser tabs, defends against DNS
   rebinding; only non-browser clients pass.

Don't confuse this **server** (`/mcp`, this app exposing itself) with the MCP
**connectors** of §8 (this app consuming remote servers). When a chat agent
on some page probes `POST /mcp` via `browser_eval`, it is talking to whatever
app is on *that page's origin* — not necessarily this backend.

---

## 14. Paths, env vars & logging

### Environment variables

| Var | Meaning | Default |
|-----|---------|---------|
| `VOITTA_PROJECT_ROOT` | root for certs/, rag/ siblings | set by `.app` launcher; dev: `backend/` |
| `VOITTA_DATA_ROOT` | user data root | `~/Library/Application Support/Voitta Compute/backend` |
| `VOITTA_FRONTEND_DIST` | compiled widget dir | `.app` bundle path; dev: `frontend/dist` |
| `VOITTA_HOST` / `VOITTA_PORT` / `VOITTA_BRIDGE_PORT` | listener overrides (shell mode, [start.sh](start.sh)) | `127.0.0.1` / `12358` / `12359` |
| `VOITTA_PUBLIC_BASE_URL` | public origin for server deployments (bookmarklet links) | request base URL |
| `REQUESTY_BASE_URL` | Requesty router base for the `requesty` provider (private deployments) | `https://router.requesty.ai` |
| `CODEX_HOME` | codex CLI home holding `auth.json` + `models_cache.json` for the `codex` provider | `~/.codex` |
| `VOITTA_GOOGLE_AUTH_CLIENT_ID/SECRET` | enables server-mode auth | unset (desktop: no auth) |
| `VOITTA_AUTH_SECRET` | Chainlit JWT key | generated, persisted at `<data>/auth_secret` |
| `CHAINLIT_APP_ROOT` | set early — chainlit resolves FILES_DIRECTORY at import | set by desktop.py |
| `PIP_PREFIX` | runtime installs target | `…/Voitta Compute/userbase` (.app) |
| `VOITTA_DOCS_DIR` / `VOITTA_PLUGINS_DIR` / `VOITTA_LIBS_DIR` / `VOITTA_RAG_DIR` | RAG build inputs/output | derived from config |

### On-disk map (desktop `.app`)

```
~/Library/Application Support/Voitta Compute/
├── .voitta.lock                  single-instance flock
├── backend/                      ← VOITTA_PROJECT_ROOT = VOITTA_DATA_ROOT
│   ├── logs/                     all logs; fresh each launch, prior runs → logs/previous/run-*
│   │   ├── voitta.log            launcher stdout/stderr (incl. pip install output)
│   │   ├── voitta-app.log(.1-3)  backend logging, DEBUG for voitta/app/chainlit, 5MB rotating
│   │   ├── voitta-stacks.log     SIGUSR2 / watchdog thread dumps
│   │   └── previous/run-1..3     rolling archive of the last 3 runs (run-1 newest)
│   ├── certs/                    mkcert pair (wiped on version bump)
│   ├── rag/                      .chroma · .bm25 · .chroma_code · .bm25_code
│   ├── conversations.sqlite      chat threads/steps
│   ├── scripts/ + scripts_state/ LLM-authored scripts · errors · inventory
│   ├── python_storage/cache/     data snapshots by handle
│   ├── claude_code/              4th brain (per-user): config/ = CLAUDE_CONFIG_DIR
│   │                             (incl. voitta_oauth_token 0600 + projects/<cwd>/<sid>.jsonl
│   │                             session store) · workspace/ = pinned engine cwd
│   ├── auth_secret · install_state.json · .deployed_version
│   └── users/<slug>/…            (server mode only: per-user roots — incl. its own claude_code/)
├── userbase/                     PIP_PREFIX site-packages (wiped on version bump)
├── frontend/dist/ · docs/ · plugins/ · lib-sources/   seeded from the bundle
~/.config/voitta-compute/
├── settings.json                 keys · models · plugin config (0600)
└── .docs_content_hash · .code_source_hash   RAG build stamps
```

In dev (`./start.sh` from a git checkout) the same layout applies except
PROJECT_ROOT is the repo's `backend/`, so `certs/` and `rag/` live in the
repo tree (git-ignored), and the widget is served from `frontend/dist`.

### Logging

All app-mode logs live under `backend/logs/`, reachable from the tray via
**Open logs folder…** (reveals the folder in Finder for drag-drop into a
support email). Each launch starts fresh; the previous runs are archived to
`backend/logs/previous/run-1..3` (run-1 newest) first, so a crash-before-
capture stays recoverable. Retention is capped at the last 3 runs
(`LOG_KEEP_RUNS` in `__main__.py`) to bound total growth.

| Surface | Where | Notes |
|---------|-------|-------|
| Launcher (app mode) | `backend/logs/voitta.log` | stdout/stderr redirect (incl. pip install output); **fresh each launch**, no rotation |
| Backend (app mode) | `backend/logs/voitta-app.log` (+ `.1`–`.3`) | `voitta`/`app`/`chainlit` at **DEBUG**, uvicorn at INFO; 5 MB × 3 rotating; survives uvicorn's dictConfig |
| Thread stacks | `backend/logs/voitta-stacks.log` | `kill -USR2 <pid>` or the health watchdog dumps all Python thread stacks here |
| Backend (dev) | terminal | `logging.basicConfig` INFO, `%(asctime)s %(levelname)s %(name)s` |
| Tool/agent diagnostics | same stream | `run_turn: host=… visible=…/… hidden=[…]` lines show exactly why a tool is missing — first grep for tool-visibility issues |
| Script errors | `scripts_state/errors/<slug>.jsonl` | also via `get_script_errors` tool |

---

## 15. Packaging & release

- **Briefcase** macOS app, configured in the repo-root
  [pyproject.toml](pyproject.toml): bundle id `ai.voitta.voitta-compute`,
  `LSUIElement` (menu-bar only, no Dock), arm64-only, macOS 14+. Bundled
  sources: `src/voitta_compute/` (launcher + `resources/`: frontend_dist,
  docs, plugins, mkcert binary, wheels, gitmodules pins), `backend/app/`,
  `scripts/`.
- Hardened-runtime entitlements for notarisation: `allow-jit`,
  `allow-unsigned-executable-memory`, `disable-library-validation`,
  `allow-dyld-environment-variables`.
- **Build**: `./build.sh` (venv + deps + wheels) then `./build_app.sh`
  (briefcase → `build/voitta-compute/macos/app/Voitta Compute.app`;
  `--package`/`--release` produce a signed, notarised DMG under `dist/` and
  `--release` bumps the patch version in pyproject).
- **Known pitfall**: `build_app.sh` picks `briefcase update` vs `create` by
  testing `[ -d build ]` — a `build/` dir whose briefcase scaffold was
  removed (post-clean state) makes `update` fail with `FileNotFoundError`
  on `…/Contents/Resources/app` while the script still exits 0 (the error
  is masked by the output pipe). Recovery:
  `rm -rf build/voitta-compute && briefcase create macOS app && briefcase build macOS app`.
- **Version**: [src/voitta_compute/_version.py](src/voitta_compute/_version.py);
  read at runtime by `installer.current_app_version()` and compared against
  `.deployed_version` to trigger the fresh-deploy wipe (§2).
- **Frontend**: `cd frontend && npm run build` → `dist/widget.js`. The build
  must be copied into `src/voitta_compute/resources/frontend_dist/` for the
  frozen app (release workflow); dev serves `frontend/dist/` directly.

---

*Generated from a source trace of `backend/app/`, `frontend/src/`,
`src/voitta_compute/`, and `plugins/`. File paths are the durable anchors —
re-verify line-level specifics against the code.*
