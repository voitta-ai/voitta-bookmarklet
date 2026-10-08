# Architecture

## Request flow (happy path)

```
Browser tab
  └─ bookmarklet injects widget.js (IIFE)
       └─ React widget mounts in shadow DOM
            └─ user sends message
                 └─ Chainlit socket.io (/chainlit/ws/socket.io)
                      └─ chainlit_app.py: on_message
                           └─ agent.py: run_turn()
                                ├─ LLM call (streaming) via services/llm/<provider>
                                ├─ tool dispatch: registry.dispatch(name, args, ctx)
                                │    ├─ server-side tools: run in-process
                                │    └─ browser-side tools: cl.CopilotFunction.acall()
                                │         └─ call_fn → React widget → page JS → result
                                └─ streams text tokens back to widget via cl.Message
```

## Key modules

| Module | Role |
|---|---|
| `app/main.py` | FastAPI app factory; CORS + PNA middleware; Chainlit mount; screenshot stash |
| `app/chainlit_app.py` | Chainlit event handlers; session init; plugin loading per host |
| `app/agent.py` | Tool-use loop: LLM stream → tool dispatch → iterate until stop |
| `app/plugins.py` | Discovers `plugins/**/manifest.json`; host-gated system prompt composition |
| `app/tools/registry.py` | `ToolSpec` store; `registry.dispatch()`; `registry.visible_for_host()` |
| `app/tools/load.py` | Imports all tool modules at startup (side-effect registration) |
| `app/reports/sandbox.py` | `exec`-based script runner; `asyncio.to_thread`; 120 s timeout |
| `app/reports/ctx.py` | `ScriptContext` — emitters, theme, data access |
| `app/services/llm/` | Provider abstraction: `NormalisedRequest`, `stream()`, per-provider impls |
| `app/data/sqlite_layer.py` | Chainlit `DataLayer` implementation over SQLite |
| `app/services/user_settings.py` | Raw read/write for `settings.json` |
| `app/settings.py` | Typed view over `settings.json`; defaults; `redacted_for_wire()` |

## Tool sides

Tools declare a `side` field:
- `"server"` — executed in the backend process.
- `"browser"` — round-trips to the frontend via `cl.CopilotFunction.acall()`.
- `"hybrid"` — server-side handler that itself calls the browser (e.g. `browser_eval`).

## Host context

`ToolCtx.host` carries the hostname of the page the bookmarklet is mounted on. The agent loop reads it from the Chainlit window message at session start and passes it to every tool call. Plugins use it for host gating; `ctx.theme()` uses it to pick the matching plugin palette.

## CORS / PNA

The bookmarklet runs on third-party origins and must reach `127.0.0.1`. FastAPI:
- Echoes the request `Origin` via `allow_origin_regex=".*"` (CORS cannot use `*` with credentials).
- A raw ASGI `_PNAMiddleware` handles Chrome's Private Network Access preflights (`Access-Control-Request-Private-Network: true`) that `CORSMiddleware` never answers.

## Conversation persistence

`app/data/sqlite_layer.py` implements Chainlit's `DataLayer` interface.  
DB path: `~/Library/Application Support/Voitta Bookmarklet/backend/conversations.sqlite`.  
Thread history is exposed via a patched `/chainlit/project/threads` endpoint (Chainlit's upstream requires auth; the patch serves history without it).
