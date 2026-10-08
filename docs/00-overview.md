# Overview

Voitta Bookmarklet is a local AI assistant that runs on your Mac and injects a chat widget into any browser tab via a bookmarklet.

## What it does

- Drops a floating chat widget into any page (shadow DOM, no style collisions).
- Connects to Anthropic / OpenAI / Gemini via your own API keys.
- Executes server-side Python scripts that produce HTML reports.
- Evaluates arbitrary JavaScript in the user's active tab (`browser_eval`).
- Takes screenshots of rendered reports using `html-to-image`.
- Maintains persistent conversation history per thread (SQLite).

## What it is not

- Not a cloud service — the backend runs at `127.0.0.1:12358`.
- Not a notebook — reports are standalone HTML strings, not live reactive cells.
- Not a data platform — workspace stores snapshots as flat files; no database.

## Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI + Chainlit (Python) |
| Frontend | React + Vite, compiled to IIFE widget |
| Widget injection | Bookmarklet, shadow DOM |
| LLM providers | Anthropic, OpenAI, Gemini |
| Report screenshots | `html-to-image` (SVG foreignObject) |
| Persistence | SQLite at `~/Library/Application Support/Voitta Bookmarklet/backend/conversations.sqlite` |
| Settings | JSON at `~/.config/voitta-bookmarklet/settings.json` |

## Key entry points

- `backend/app/main.py` — FastAPI app, Chainlit mounted at `/chainlit`
- `backend/app/chainlit_app.py` — `@cl.on_chat_start` / `@cl.on_message` glue
- `backend/app/agent.py` — tool-use agent loop
- `backend/app/tools/load.py` — side-effect imports that register all tools
- `frontend/src/widget.tsx` — React widget root
