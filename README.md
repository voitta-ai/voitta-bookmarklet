# Voitta Compute

LLM assistant injected into any web page via a bookmarklet. Chainlit
owns the chat context; the React frontend talks to it through
`@chainlit/react-client`. Single FastAPI process at `127.0.0.1:12358`
serves both the Chainlit socket and the built bookmarklet bundle.

## Layout

```
voitta-compute/
├── backend/          FastAPI + Chainlit, agent loop, tool registry
├── frontend/         Vite IIFE bundle, React widget, primitives
├── plugins/          Host-scoped extensions (manifest + BE module + FE widget + docs + prompt)
├── docs/             *** MASTER COPY of all prose docs ***
│                     Bundled into the .app by build_app.sh (step 5b).
│                     DO NOT create or edit docs under src/voitta_compute/resources/docs/ —
│                     that directory is build-generated and gitignored.
├── lib-sources/      Vendored libraries as git submodules (see below)
├── rag/              Built RAG indexes (gitignored — rebuildable)
├── scripts/          Dev tooling (RAG builder)
├── start.sh          Run uvicorn directly (dev mode)
├── tray.sh           macOS menu-bar tray (uvicorn on a daemon thread)
└── build.sh          Install FE deps, build FE bundle, set up BE venv
```

> **Single source of truth for docs:**
> - Core docs → `docs/`
> - Plugin docs → `plugins/<name>/docs/`
>
> `build_app.sh` copies both into `src/voitta_compute/resources/` at build
> time. The `resources/` subdirectories (`docs/`, `frontend_dist/`,
> `plugins/`, `vendor_js/`) are gitignored — never edit them directly.

Six plugins ship today: `default` (always-on system prompt), `ebay`,
`google`, `linkedin`, `veed`, `voitta-enterprise`. See [`docs/05-plugins.md`](docs/05-plugins.md)
for the plugin model.

## Setup

```bash
git clone <url> voitta-compute
cd voitta-compute
git submodule update --init --recursive --depth 1   # pulls lib-sources/*
./build.sh
mkcert -install                                      # once per machine: trust mkcert's local CA
mkdir -p backend/certs && (cd backend/certs && \
  mkcert -cert-file 127.0.0.1+1.pem -key-file 127.0.0.1+1-key.pem 127.0.0.1 localhost)
```

`build.sh` installs the FE deps, builds the bookmarklet bundle, and
sets up the BE venv with chromadb, bm25s, fastmcp, rumps, etc.
The TLS pair in `backend/certs/` is per-machine and git-ignored — without
it `start.sh` serves plain HTTP, which HTTPS pages block as mixed content.

## Run

**Terminal mode**:
```bash
./start.sh                       # https://127.0.0.1:12358
```

**macOS tray mode** (uvicorn on a daemon thread, rumps owns the main
thread):
```bash
./tray.sh
```

The tray menu has About / Open / Copy bookmarklet / Settings (with
the MCP-debug toggle) / Show data folder / (Re)create TLS certs /
Reset / Quit.

Open `https://127.0.0.1:12358/` once in a browser to accept the
self-signed cert. Then drag the bookmarklet (Copy bookmarklet from
the tray, or build your own pointing at `/widget.js`) to your
bookmarks bar.

## Configuration

Per-user settings at
`~/.config/voitta-compute/settings.json`:

```json
{
  "provider": "anthropic",
  "api_keys": { "anthropic": "sk-ant-..." },
  "models":   { "anthropic": "claude-sonnet-4-6" },
  "googleOAuth": { "clientId": "...", "clientSecret": "..." },
  "plugins": { "voitta-enterprise": { "mcp": { "url": "...", "api_key": "..." } } }
}
```

You don't edit this file by hand — open the in-page Settings panel
(gear icon in the widget), which has tabs for Global + each plugin
that ships configurable fields.

## `lib-sources/` — vendored libraries

Four libraries live here as git submodules so the LLM can grep
through their source via the RAG `code` corpus:

| Submodule              | Indexed roots                  | Why                          |
|------------------------|--------------------------------|------------------------------|
| `pallets/jinja`        | `src/jinja2/`, `docs/`, `examples/` | Python — Jinja2 template engine source + RST API docs |
| `kieler/elkjs`         | `src/`, `typings/`             | TypeScript — ELK layout engine (used by `kind="elk"`) |
| `eclipse/elk`          | `plugins/`, `docs/`, `test/`   | Java — Eclipse ELK algorithm implementations |
| `mrdoob/three.js`      | `src/`, `docs/`, `examples/jsm/` | JavaScript — three.js core + API docs |

After cloning, run:
```bash
git submodule update --init --recursive --depth 1
```

To bump pinned versions later:
```bash
git submodule update --remote --merge
git add lib-sources/<repo>
git commit -m "Bump <repo> to <sha>"
```

## RAG

Two corpora, both Chroma (dense) + bm25s (sparse) with hybrid score
fusion:

```bash
python scripts/build_rag.py                       # both corpora
python scripts/build_rag.py --corpus docs         # docs/ + plugins/*/docs/  (fast, ~1s)
python scripts/build_rag.py --corpus code         # lib-sources/*  (slower, ~1 min)
python scripts/build_rag.py --corpus code --repo three.js          # one repo only
python scripts/build_rag.py --corpus code --repo three.js,elkjs    # subset
```

Each run is a **full rewrite** of the named corpus — `--repo three.js`
REPLACES the code corpus with just three.js chunks, it doesn't merge.
Use it for fast iteration after bumping a submodule, then rebuild
all when you're done.

**After editing any file under `docs/` or `plugins/*/docs/`, always run:**
```bash
python scripts/build_rag.py --corpus docs
```
`--corpus docs` only touches the docs index, so you don't pay the
cost of re-walking `lib-sources/`.

The LLM queries them via the `rag_query` tool — pass `corpus="docs"`
(default) or `corpus="code"`. The code corpus returns chunks with
`repo`, `path`, `folder`, `lang`, `kind` (module / class / function /
method), and `symbol` metadata so the model can navigate to a
specific file or pull neighbouring chunks via `rag_get_chunk_range`.

Indexes live under `rag/.chroma{,_code}/` + `rag/.bm25{,_code}/` —
gitignored, ~few MB combined, rebuilt in ~1 min.

## MCP debugging

The BE exposes a FastMCP server at `/mcp` for external MCP clients
(Claude Desktop, `mcp-cli`, etc.). Gated three ways: tray-flag
(`mcpDebugEnabled` off by default) + loopback-only peer + no browser
`Origin` header. Tools: `mcp_sessions`, `mcp_page`, `mcp_eval`,
`mcp_screenshot`. See [`backend/app/services/mcp_server.py`](backend/app/services/mcp_server.py).

## Docs

[`docs/`](docs/) has seven numbered prose docs (overview,
architecture, frontend, providers, tool catalogue, plugins, reports).
They're indexed into the `docs` RAG corpus alongside every plugin's
`docs/` tree, so the LLM can look up its own design without leaving
the chat.

## Tests

```bash
cd backend && ./.venv/bin/python -m pytest
```
