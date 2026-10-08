# Voitta Compute

An AI assistant you open on any web page with a bookmarklet. It reads the
page you are on, runs Python for analysis and reports, and keeps your
conversations and projects on your own machine. Bring your own model: an
Anthropic, OpenAI, Gemini or Requesty API key, or a Claude subscription.

## Contents

- [Quick start (Docker)](#quick-start-docker)
- [First run](#first-run)
  - [Install the bookmarklet](#install-the-bookmarklet)
  - [Choose a model](#choose-a-model)
  - [Optional: HTTPS for the standard bookmarklet](#optional-https-for-the-standard-bookmarklet)
- [Other ways to run it](#other-ways-to-run-it)
  - [From source (macOS or Linux)](#from-source-macos-or-linux)
  - [macOS menu-bar app](#macos-menu-bar-app)
  - [Shared server with Google sign-in](#shared-server-with-google-sign-in)
- [Upgrade, back up, remove](#upgrade-back-up-remove)
- [Configuration](#configuration)
- [Development](#development)
  - [Layout](#layout)
  - [lib-sources: vendored libraries](#lib-sources-vendored-libraries)
  - [RAG](#rag)
  - [MCP debugging](#mcp-debugging)
  - [Docs](#docs)
  - [Tests](#tests)
- [License](#license)

## Quick start (Docker)

You need Docker (Docker Desktop, Rancher Desktop, or Docker Engine on Linux).
Nothing else is installed on your machine.

```bash
docker run -d --name voitta-compute --restart unless-stopped \
  -p 127.0.0.1:12358:12358 -p 127.0.0.1:12359:12359 \
  -v voitta-compute-data:/data \
  ghcr.io/voitta-ai/voitta-compute:latest
```

Then open <http://127.0.0.1:12358/bookmarklets> and continue with
[First run](#first-run).

- Keep the `127.0.0.1:` prefix on both ports. A single-user install has no
  login, so it must not be reachable from other machines.
- Everything you create (conversations, projects, scripts, settings, API
  keys) lives in the `voitta-compute-data` volume, not in the container.
- To build the image yourself instead of pulling it:
  `git clone https://github.com/voitta-ai/voitta-compute && cd voitta-compute && docker build -t voitta-compute .`,
  then use `voitta-compute` as the image name above.

## First run

### Install the bookmarklet

Open <http://127.0.0.1:12358/bookmarklets>. It offers two bookmarklets. Drag
the one you need to your bookmarks bar:

- **Voitta (Salesforce)**, the bridge bookmarklet, works on any page without
  certificates. It opens a small popup window that relays to the backend, so
  allow pop-ups for the site the first time. **Use this one with Docker
  unless you set up HTTPS.**
- **Voitta**, the standard bookmarklet, loads the assistant straight into the
  page. Browsers block a web page from loading scripts from `http://127.0.0.1`,
  so this one needs [HTTPS](#optional-https-for-the-standard-bookmarklet).

Click the bookmarklet on any page. The assistant panel opens beside it.

### Choose a model

The first time it opens, the panel shows Settings (the gear icon brings it
back later). Pick a provider and paste its API key. Anthropic, OpenAI, Google
Gemini and Requesty are supported. If you have a Claude Pro or Max
subscription, choose **Claude (subscription)** if it is listed, and paste a
token from `claude setup-token`.

Keys are stored in `settings.json` in your data directory (the data volume
under Docker).

### Optional: HTTPS for the standard bookmarklet

Use [mkcert](https://github.com/FiloSottile/mkcert) to create a certificate
your browser trusts for `127.0.0.1`, and mount it into the container:

```bash
mkcert -install                        # once per machine
mkdir -p ~/.voitta-certs && cd ~/.voitta-certs
mkcert -cert-file 127.0.0.1+1.pem -key-file 127.0.0.1+1-key.pem 127.0.0.1 localhost

docker rm -f voitta-compute
docker run -d --name voitta-compute --restart unless-stopped \
  -p 127.0.0.1:12358:12358 -p 127.0.0.1:12359:12359 \
  -v voitta-compute-data:/data \
  -v ~/.voitta-certs:/app/backend/certs:ro \
  ghcr.io/voitta-ai/voitta-compute:latest
```

The backend then serves `https://127.0.0.1:12358`. Open
<https://127.0.0.1:12358/bookmarklets> again and drag the standard
bookmarklet. The bridge port, 12359, stays plain HTTP by design.

## Other ways to run it

### From source (macOS or Linux)

Needs Python 3.11+, Node.js 20+ and git.

```bash
git clone https://github.com/voitta-ai/voitta-compute
cd voitta-compute
git submodule update --init --recursive --depth 1   # optional: source for the code RAG corpus
./build.sh                                          # frontend bundle + backend venv
./start.sh                                          # http://127.0.0.1:12358
```

For the standard bookmarklet, create the TLS pair in `backend/certs/` before
`./start.sh`. The pair is per-machine and git-ignored:

```bash
mkcert -install
mkdir -p backend/certs && (cd backend/certs && \
  mkcert -cert-file 127.0.0.1+1.pem -key-file 127.0.0.1+1-key.pem 127.0.0.1 localhost)
```

Data defaults to `~/Library/Application Support/Voitta Compute/backend`. Set
`VOITTA_DATA_ROOT` to put it elsewhere.

### macOS menu-bar app

`./tray.sh` runs the same backend from a menu-bar icon. The menu has About,
Open, Copy bookmarklet, Settings (with the MCP-debug toggle), Show data
folder, (Re)create TLS certs, Reset and Quit. `./build_app.sh` packages it as
`Voitta Compute.app`. See [OPERATIONS.md §15](OPERATIONS.md#15-packaging--release).

### Shared server with Google sign-in

`./server-start.sh` sets up a headless Linux server: it builds, indexes the
docs and serves plain HTTP for a reverse proxy that terminates TLS. Copy
`.env.example` to `.env` and set the Google sign-in client to require login.
Each user then gets their own data. See [OPERATIONS.md](OPERATIONS.md).

## Upgrade, back up, remove

```bash
docker pull ghcr.io/voitta-ai/voitta-compute:latest
docker rm -f voitta-compute      # the data volume is kept
# then re-run the docker run command from Quick start

docker run --rm -v voitta-compute-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/voitta-compute-data.tgz -C /data .     # back up

docker rm -f voitta-compute && docker volume rm voitta-compute-data   # remove everything
```

## Configuration

Settings are edited in the panel (gear icon). It has a Global tab and one tab
for each plugin with settings. They are stored in
`~/.config/voitta-compute/settings.json`, or `/data/config/settings.json`
under Docker. You don't need to edit that file by hand.

Plugins add site-specific tools and prompts. Six ship today: `default`
(always on), `ebay`, `google`, `linkedin`, `veed` and `voitta-enterprise`. See
[docs/05-plugins.md](docs/05-plugins.md).

## Development

### Layout

```
voitta-compute/
├── backend/          FastAPI + Chainlit, agent loop, tool registry
├── frontend/         Vite IIFE bundle, React widget, primitives
├── plugins/          Host-scoped extensions (manifest + BE module + FE widget + docs + prompt)
├── docs/             *** MASTER COPY of all prose docs ***
├── lib-sources/      Vendored libraries as git submodules (see below)
├── rag/              Built RAG indexes (gitignored, rebuildable)
├── scripts/          Dev tooling (RAG builder)
├── Dockerfile        Container image (frontend build + backend + docs RAG)
├── start.sh          Run uvicorn directly (also the container's entrypoint)
├── tray.sh           macOS menu-bar tray (uvicorn on a daemon thread)
└── build.sh          Install FE deps, build FE bundle, set up BE venv
```

Chainlit owns the chat context. The React frontend talks to it through
`@chainlit/react-client`. One FastAPI process at `127.0.0.1:12358` serves
both the Chainlit socket and the built bookmarklet bundle. A plain-HTTP
sibling listener on `:12359` serves the bridge for pages with a strict CSP.

> **Single source of truth for docs:**
> - Core docs → `docs/`
> - Plugin docs → `plugins/<name>/docs/`
>
> `build_app.sh` copies both into `src/voitta_compute/resources/` at build
> time. The `resources/` subdirectories (`docs/`, `frontend_dist/`,
> `plugins/`, `vendor_js/`) are gitignored — never edit them directly.

### lib-sources: vendored libraries

These libraries live here as git submodules so the LLM can grep through
their source via the RAG `code` corpus:

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

The Docker image leaves `lib-sources/` out, so it has the docs corpus only.

### RAG

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

### MCP debugging

The BE exposes a FastMCP server at `/mcp` for external MCP clients
(Claude Desktop, `mcp-cli`, etc.). Gated three ways: tray-flag
(`mcpDebugEnabled` off by default) + loopback-only peer + no browser
`Origin` header. Tools: `mcp_sessions`, `mcp_page`, `mcp_eval`,
`mcp_screenshot`. See [`backend/app/services/mcp_server.py`](backend/app/services/mcp_server.py).
Under Docker the peer is the container bridge, not loopback, so `/mcp`
refuses it; run from source to use it.

### Docs

[`docs/`](docs/) has the numbered prose docs (overview, architecture,
frontend, providers, tool catalogue, plugins, reports, workspace). They're
indexed into the `docs` RAG corpus alongside every plugin's `docs/` tree,
so the LLM can look up its own design without leaving the chat.
[OPERATIONS.md](OPERATIONS.md) is the operator's reference.

### Tests

```bash
cd backend && ./.venv/bin/python -m pytest
```

## License

[AGPL-3.0-or-later](LICENSE).
