# voitta-rag-enterprise MCP integration

The `voitta-enterprise` plugin bridges the bookmarklet chat backend to a
[voitta-rag-enterprise](../../../../voitta-rag-enterprise/) MCP server.
No Python code in this plugin — the bridge is declared entirely in
`manifest.json` and materialised by Voitta core's MCP infrastructure
(`backend/app/services/mcp/`).

## What the LLM sees automatically

On `enterprise.voitta.ai` the backend appends [`prompt.md`](../prompt.md) to the chat system prompt — that's wired by the manifest's `system_prompt: "prompt.md"` field plus the plugin-prompt mechanism described in [docs/13-plugins.md § System prompt addendum](../../../docs/13-plugins.md#system-prompt-addendum). The LLM therefore already has:

- A pointer at `vre_search` as the platform-docs oracle.
- A mandatory-lookup rule before touching `.FCStd` files, `cad_mesh`, `cad_projection`, or `request_asset` for CAD — the LLM must `vre_search` the docs first so it doesn't guess at slugs, BOM layout, or `x-fcstd-kind` semantics.

No user setup needed. The rules fire only on hosts matching this plugin's `host_patterns`; on every other site the chat prompt is core-only.

## What it exposes

Every tool the remote MCP server lists becomes an LLM-visible tool with
the prefix `vre_`. As of voitta-rag-enterprise 0.x the list is:

| Remote tool             | Local name (with prefix)        |
|-------------------------|---------------------------------|
| `search`                | `vre_search`                    |
| `search_images`         | `vre_search_images`             |
| `get_file`              | `vre_get_file`                  |
| `get_chunk_range`       | `vre_get_chunk_range`           |
| `get_chunk_images`      | `vre_get_chunk_images`          |
| `get_image`             | `vre_get_image`                 |
| `list_indexed_folders`  | `vre_list_indexed_folders`      |
| `list_page_images`      | `vre_list_page_images`          |
| `get_page_image`        | `vre_get_page_image`            |
| `get_page_layout`       | `vre_get_page_layout`           |
| `get_workbook`          | `vre_get_workbook`              |
| `resolve_url`           | `vre_resolve_url`               |
| `list_assets`           | `vre_list_assets`               |
| `request_asset`         | `vre_request_asset`             |

Tools are gated to `enterprise.voitta.ai` via the plugin's
`host_patterns` — they're hidden from the LLM on every other host
(matching how every other plugin scopes its tools).

## The `vre://` ref scheme — reports reference upstream

Core rule (Voitta-wide): reports never bake `py_xxx` handles
into their source. For anything sourced through VRE, the
canonical ref is:

    vre://<folder_display_name>/<relative/path/to/file>[?params]

Path-based: the folder display name and the file's relative path
within that folder. No integer IDs — stable across re-indexing.

Query params (all optional):

- `asset` — asset type: `original` (default) / `cad_projection` /
  `cad_mesh` (or any future type `vre_list_assets` exposes).
- `slug` — required for `cad_projection`; required for
  per-component `cad_mesh`; omitted for whole-file `cad_mesh`
  or `original`.
- `export` — for `cad_projection`, picks one view: `front` /
  `top` / `side` / `iso`. Omitting it means the ref resolves to
  a directory containing all four PNGs.

Param order doesn't matter; use `vre_list_indexed_folders` to
discover folder names and file paths.

Examples:

    vre://McKinsey Quarterly/2025_q1.pdf
    vre://Stella NFS/stella_park_prepared/docs/report.pdf
    vre://Stella NFS/parts/base-frame.glb?asset=cad_mesh
    vre://Stella NFS/parts/rail-l.glb?asset=cad_projection&slug=base-frame/rail-l
    vre://Stella NFS/parts/rail-l.glb?asset=cad_projection&slug=base-frame/rail-l&export=iso

### How the resolver works

`ctx.ensure_local("vre://...")` hides the signed-URL TTL. When
the ref is requested, the resolver:

1. Looks up the folder display name → folder id via
   `list_indexed_folders` (module-level cache; refreshed on miss).
2. Looks up the file path within the folder → `file_id`.
3. Calls `request_asset(file_id, asset_type, slug=...)` → fresh
   signed URL(s).
4. Streams the file(s) into a snapshot dir whose
   `meta.json::origin` records the canonical `vre://` ref.

The 2-step flow below is documented for one-shot manual use
(you're not authoring a report — you're doing a one-shot fetch
for analysis). For persisted code, write the ref and let
`ensure_local` run the steps.

## Working with the file bytes — three-step flow

`vre_request_asset` returns a signed URL, not bytes. To get the
file into `python_storage` where `run_compute` can read it, chain
the call with `fetch_to_python_storage`:

```python
# 1. Find the file
hits = vre_search(query="McKinsey Q1")
file_id = hits[0]["file_id"]
file_path = hits[0]["file_path"]   # e.g. "mckinsey_quarterly_2025_q1.pdf"

# 2. Mint a signed URL for the source bytes
asset = vre_request_asset(file_id=file_id, asset_type="original")
url = asset["urls"]["file"]
# asset also has expires_at — fetch within the TTL (default 1 hour)

# 3. Pull the URL into python_storage
snap = fetch_to_python_storage(url=url, name=file_path)
handle = snap["handle"]            # "py_…"

# 4. Process the file in a compute script
run_compute(code=f"""
    rec = ctx.snapshot({handle!r})
    import pypdf
    reader = pypdf.PdfReader(rec['path'] + '/' + rec['meta']['stored_name'])
    ctx.text(reader.pages[0].extract_text()[:500])
""")
```

`fetch_to_python_storage` is the generic "URL → handle" tool — it
works for any HTTPS URL (signed asset URLs, public web downloads,
etc.), not just rag-enterprise responses. It adds no auth headers;
the URL is the credential. See [fetch_to_python_storage tool docs](
../../../docs/02-tool-catalogue.md#fetch_to_python_storage) for the
full signature.

## Asset types — what `request_asset` can produce

`vre_request_asset(file_id, asset_type, slug=None, params=None)`
always returns a dict with `asset_type` and exactly one of `inline`
(structured data the LLM consumes directly) or `urls` (variant
name → signed URL + `expires_at`). Call `vre_list_assets(file_id)`
first to discover what a specific file exposes.

| `asset_type`      | Applies to                    | Response shape                          | Notes                                                                                                      |
|-------------------|-------------------------------|-----------------------------------------|------------------------------------------------------------------------------------------------------------|
| `original`        | every indexed file            | `urls["file"]`                          | Source bytes of the indexed file. No `slug`, no `params`.                                                  |
| `md`              | every indexed file with a parser-produced `text.md` (PDF / DOCX / XLSX / PPTX / ipynb / text / Google Workspace files synced into the index) | `urls["md"]`                            | Parser's normalised markdown extract, served as `text/markdown`. Same content `vre_get_file` returns — but as a fetchable URL, so the LLM can pipe it into `fetch_to_python_storage` → `run_compute` instead of pulling it through tool-result context. No `slug`, no `params`. Use `original` instead when you need the source format. |
| `cad_projection`  | `.step` / `.stp` / `.FCStd`   | `urls` keyed by `front`/`top`/`side`/`iso` | Four PNG views. Requires `slug` naming the component (use `vre_list_assets` for slugs). Optional `params={"size": 320}`. |
| `cad_mesh`        | `.step` / `.stp` / `.iges` / `.igs` / `.FCStd` | `urls["mesh"]`                          | Binary glTF (`.glb`, `model/gltf-binary`). Without `slug`: whole assembly. With a `cad_projection` slug: just that component. Optional `params={"linear_deflection": 0.5}` controls tessellation tolerance in mm (range 0.001–5.0; smaller = more triangles, larger file). The GLB scene contains one named node per component, so a viewer can list / hide / colour parts by `node.name`. |

### Bulk-text flow — `md`

When the user wants the LLM to do regex / pandas / NLP work over a
long DOCX, XLSX, or PPTX, fetch the parser's markdown extract
directly into `python_storage` instead of reading it inline:

```python
asset = vre_request_asset(file_id=N, asset_type="md")
snap  = fetch_to_python_storage(
            url=asset["urls"]["md"],
            name="<filename>.md",
        )
run_compute(code=f"""
    rec = ctx.snapshot({snap['handle']!r})
    text = (pathlib.Path(rec['path']) / rec['meta']['stored_name']).read_text()
    # ... regex / dataframe / NLP over `text` ...
""")
```

`md` is the right choice when:
- The extract is large enough that piping it through `vre_get_file`
  would burn a lot of tool-result context.
- You want to run code (split-on-headings, count tokens, build a
  dataframe from fenced tables) rather than read.

Use `original` instead when:
- You need the source format (custom OCP, openpyxl on the raw
  workbook, layout-preserving PDF parser).
- The parser's markdown lost something you care about (precise
  table positions, embedded objects, page numbering).

### CAD 3D-viewer flow — `cad_mesh`

When the user wants to **see** a CAD file (vs read its parsed
component tree), chain `vre_request_asset` → `fetch_to_python_storage`
→ render in a report:

```python
# 1. Find a CAD file
hits = vre_search(query="bracket assembly")
file_id = hits[0]["file_id"]
file_path = hits[0]["file_path"]   # e.g. "bracket_v3.FCStd"

# 2. Request the whole-assembly GLB
asset = vre_request_asset(file_id=file_id, asset_type="cad_mesh")
url = asset["urls"]["mesh"]        # short-lived signed URL
# For just one component:
#   asset = vre_request_asset(file_id=file_id, asset_type="cad_mesh",
#                             slug="bracket-arm")
# For higher-fidelity tessellation:
#   asset = vre_request_asset(file_id=file_id, asset_type="cad_mesh",
#                             params={"linear_deflection": 0.05})

# 3. Pull the GLB into python_storage
snap = fetch_to_python_storage(url=url, name=file_path + ".glb")
handle = snap["handle"]

# 4. Embed in a report. `ctx.three_scene` takes a JavaScript string,
#    not a path — load the GLB via dynamic-import GLTFLoader, base64-
#    inline so the sandboxed iframe (null origin) can read it.
#    Full recipe + the wrong patterns that fail noisily:
#    docs/recipes/three.md "Loading a CAD GLB into the scene"
#    into ctx.three_scene".
```

```python
# Minimum viable report — see the doc above for the full annotated version.
def build(ctx):
    rec = ctx.snapshot(handle)
    import base64, pathlib
    b64 = base64.b64encode(
        (pathlib.Path(rec["path"]) / rec["meta"]["stored_name"]).read_bytes()
    ).decode()
    return ctx.three_scene(f"""
        const {{ GLTFLoader }} = await import('three/addons/loaders/GLTFLoader.js');
        const bin = Uint8Array.from(atob("{b64}"), c => c.charCodeAt(0));
        const url = URL.createObjectURL(new Blob([bin], {{type: 'model/gltf-binary'}}));
        const gltf = await new GLTFLoader().loadAsync(url);
        scene.add(gltf.scene);
        camera.position.set(5, 3, 8);
    """, height=600)
```

`cad_mesh` preserves the part hierarchy: one named node per
component in the GLB scene. Downstream three.js code can iterate
`scene.children` to list parts, toggle visibility, or recolour by
`node.name`. The `slug` parameter (when used) matches the same
slugs `cad_projection` emits, so a single `vre_list_assets` call
gives the LLM the vocabulary for both view types.

> **Up-axis gotcha.** FreeCAD (and STEP, IGES, Rhino, Revit) is
> **Z-up**; Three.js is **Y-up**. The GLB bakes the source convention
> in — without correction, the model renders tipped 90° on its side.
> Fix is one line on the root group, applied **before** the
> bounding-box pass:
>
> ```js
> model.rotation.x = -Math.PI / 2;
> model.updateMatrixWorld(true);
> ```
>
> Then compute the bounding box, centre, and scale. See
> [docs/recipes/three.md](../../../docs/recipes/three.md)
> for the full ordering and a cheat sheet covering Rhino / Revit /
> Blender.

## CAD retrieval — the component-slug model

Every indexed CAD file (`.fcstd`, `.step` / `.stp`, `.iges` / `.igs`) is a **tree of addressable components**, each named by a filesystem-shaped slug. Slugs are the single vocabulary the LLM uses to request anything CAD-related — projections, meshes, sub-assemblies, individual parts.

```
4-post-lift                                   ← whole assembly (top container)
4-post-lift/base-frame                        ← sub-assembly
4-post-lift/base-frame/longitudinal-rail-l    ← individual Part::Feature
4-post-lift/scissor-arms/upper-cross-arm-r
4-post-lift/hydraulic/cylinder-rod
```

A slug names either a **container** (App::Part — renders the whole subtree) or a **feature** (single Part::Feature). Both accept the same `vre_request_asset` calls. Slugs are stable across UI renames in FreeCAD because each spec also carries an `x-fcstd-internal-path` for identity round-trip.

### Discovery — how to find the slug you need

Three tools, used in that order most of the time:

| Tool | When to use it | What it returns |
|---|---|---|
| `vre_search(query, folder_ids=…, limit=20)` | "Find me the upper deck" — hybrid (dense + BM25) over the index. CAD files are indexed **one chunk per component**, so a search for `"longitudinal rail L"` lands on the exact feature's chunk. The chunk text contains the slug verbatim. | Hits with `chunk.text` — read the `Slug:` line out of it and pass it straight to `vre_request_asset`. |
| `vre_list_assets(file_id)` | "What's renderable in this file?" — full menu of derived views for one file. | `original` + `cad_mesh` (whole-file) + one `cad_projection` entry per addressable component, each with `slug`, `x-fcstd-path` (label segments), `x-fcstd-internal-path` (FreeCAD internal names), `x-fcstd-kind` (`container` / `feature` / `orphans` / `whole`), `x-fcstd-members` (the brp list the renderer composes). |
| `vre_list_indexed_folders(prefix=None)` | Browse storage as a filesystem. `prefix=None` → visible folders; `prefix="MyFolder/sub"` → subdirs + files. Hidden files (`.voitta.meta`, `.DS_Store`, …) are filtered. | Folder/file listing — use to discover `file_id`s when you don't have a search query. |

The component chunk text looks like this (verbatim — the LLM can copy-paste from it):

```
## Component: Longitudinal Rail L
Slug: `4-post-lift/base-frame/longitudinal-rail-l`
Internal path: `Part001 / Part005 / Body017`
Kind: Part::Feature (single addressable part)
Renderable via:
- vre_request_asset(file_id=<N>, asset_type="cad_projection", slug="4-post-lift/base-frame/longitudinal-rail-l")
- vre_request_asset(file_id=<N>, asset_type="cad_mesh",       slug="4-post-lift/base-frame/longitudinal-rail-l")
```

### Asset-type reference (CAD-specific)

| `asset_type`     | `slug` | `params` | Response | Notes |
|---|---|---|---|---|
| `cad_mesh`       | optional — omit for whole file, supply for one component | `linear_deflection` in mm (range 0.001–5.0; default 0.5; smaller = more triangles) | `urls["mesh"]` → GLB (`model/gltf-binary`) | Whole-file or single-component three.js scene with one named node per Part::Feature (FCStd) / per top-level XCAF label (STEP). |
| `cad_projection` | **required** — slug names the component | `size` in pixels (64–2048; default 512) | `urls = {front, top, side, iso}` → four PNGs | Use for thumbnail strips, side-by-side feature comparisons, and any "show me what this part looks like" prompt where 3D interactivity isn't needed. |
| `original`       | not used | not used | `urls["file"]` → raw source bytes | Stream into your own pipeline (custom OCP, openpyxl, etc.). No Voitta extraction. |

### Typical flows

**"Show me the upper deck of this lift."**
1. `vre_search("upper deck", folder_ids=[42])` → top hit's chunk contains slug `4-post-lift/upper-deck-assembly`.
2. `vre_request_asset(file_id, "cad_mesh", slug="4-post-lift/upper-deck-assembly")` → GLB URL.
3. `fetch_to_python_storage` → `ctx.three_scene` (see the recipe above and [docs/recipes/three.md](../../../docs/recipes/three.md) for the full pattern).

**"Render all four scissor arms side by side."**
1. `vre_search("scissor arm")` → chunks for the four feature slugs.
2. Four `vre_request_asset(file_id, "cad_projection", slug=…)` calls in parallel.
3. Display the 4-up grid of PNG URLs inline.

**"Ingest a STEP file into my own OCP pipeline."**
1. `vre_request_asset(file_id, "original")` → signed URL.
2. Stream into your `STEPControl_Reader` (no Voitta tessellation involved).

### Lifetime and errors

- Signed URLs **embed the credential** — no `Authorization` header needed on the fetch. They're short-lived (`expires_at` ≈ 1 h). Don't cache; re-mint via `vre_request_asset`.
- A URL is per-request: hitting the same URL after `expires_at` gets a 401, not a 410.
- If a file is deleted between mint and fetch → **410** on the asset URL.
- If a file was reindexed and the slug is stale → `KeyError: slug … not in asset menu` from `vre_request_asset` itself, before any URL is minted. Re-run `vre_list_assets` to get the fresh slugs.
- `vre_request_asset(asset_type="cad_mesh")` on a non-CAD extension → `ValueError: cad_mesh: unsupported extension …`. Check the file's extension via `vre_get_file` or `vre_list_assets` first.

### FreeCAD-specific behavior (`.FCStd`) — via the `vre_*` MCP tools

Everything in this subsection is surfaced through the **MCP tools** documented above (`vre_search`, `vre_list_assets`, `vre_request_asset`, `vre_get_chunk_range`) — there is no separate FreeCAD endpoint. The notes below describe what you'll see in those tools' responses when the source file is a `.FCStd`.

The system prompt on `enterprise.voitta.ai` instructs the LLM to `vre_search` the docs corpus before touching `.FCStd` files (see [`prompt.md`](../prompt.md)) — this section is what those search hits land in.

`.FCStd` files are parsed natively (zip + XML — no FreeCAD install on the server). Three extras land in the chunk index — reachable via `vre_search` / `vre_get_chunk_range` — that STEP / IGES don't have:

1. **Spreadsheet workbench tabs.** Every `Spreadsheet::Sheet` object becomes part of the file's markdown, grouped under a `## Spreadsheets` section with one `### Sheet: <label>` per tab. Cell values **and the formulas themselves** are indexed (formulas keep their source expression, e.g. `=A1*0.5`, not the evaluated result), so `vre_search("M8 bolt torque spec")` can hit a torque table embedded in a CAD doc. Each cell appears as `` `A1`: <content> `` in spreadsheet-natural order (row-then-column). No special asset call — `vre_search` finds it, `vre_get_chunk_range` retrieves the surrounding context.

2. **Per-component BOM and engineering notes inside each component chunk.** Every addressable component lands in its own chunk (the parser emits one `## Component: <name>` heading per slug and the chunker splits on those headings). Each chunk carries:
   - `Slug:` / `Internal path:` / `Kind:` header lines, plus the literal `request_asset(...)` invocations the LLM can copy-paste.
   - **Fabricated parts** — distinct leaf-label list for `Part::Feature` descendants whose label has no `[bracket]` suffix.
   - **Standard hardware** — quantity-counted list (`12× M8 hex nut`) of `Part::Feature` descendants whose label ends in `[bracket]` (see label-convention note below).
   - **Engineering notes** — any `App::PropertyString` named `Description`, `Note`, `Comment`, `Remark`, or `EngineeringNote` (case-insensitive, plural and underscored variants accepted) on the component *or any descendant*. Surfaced as `**<part>** — _<prop>_: <value>`. Designers use these for material specs, vendor SKUs, tolerances, assembly notes — `vre_search("stainless 316 hex nut")` lands on the component whose property mentions it.

3. **Label conventions: BOM routing only — not slug structure.** Two patterns on `Part::Feature` *labels* (not on App::Part containers) discriminate hardware from fabricated parts within a component:
   - `Foo :: Bar` (space-colon-colon-space) → the prefix is stripped; the part is listed as a **fabricated part** named `Bar`.
   - `Foo [hardware]` (trailing `[group]`) → the part is listed under **Standard hardware** as `Foo` with a count.
   Trailing FreeCAD instance digits (`Foo123`) are stripped from the displayed name.
   These conventions **do not** alter slugs or the assembly tree — slugs come strictly from the `App::Part` hierarchy in `Document.xml`. STEP / IGES do not apply these conventions at all.

Four `x-fcstd-kind` values appear in `vre_list_assets` output:

- `container` — an `App::Part`. Rendering returns the entire subtree.
- `feature` — a single `Part::Feature`. Rendering returns just that part.
- `orphans` — synthetic bucket for `Part::Feature` objects that aren't reachable from any `App::Part`. Common in older or hand-built docs.
- `whole` — synthetic "render everything" aggregate, emitted only when the document lacks a single top-level `App::Part` to play that role. (When a wrapper App::Part already exists, no `whole` is emitted — the wrapper is `container`.)

`App::Part`s labelled `Origin*` (FreeCAD's per-body coordinate-system containers, geometry-less) are filtered out of `vre_list_assets`.

> **Leaf-placement quirk.** A `Part::Feature` slug renders the part in its *local* coordinates, not its position in the parent assembly — FreeCAD bakes the leaf placement into the embedded `.brp` already, and re-applying it would double-transform. Sub-assembly and whole-assembly renders compose placements correctly. If a single-part projection looks "centered" while the parent shows it offset, that's expected.

### Refreshing after a source change

If a CAD file changed in the source folder, hit **Reindex** on that folder in the SPA (or `POST /api/folders/{id}/reindex`). The new component breakdown appears on the next `vre_list_assets` / `vre_search` call. No service restart; no cache to bust on the bookmarklet side.

## Setup

1. **Run a voitta-rag-enterprise server.** From that repo:

   ```bash
   # local single-user dev (no API key required)
   VOITTA_SINGLE_USER=true \
   VOITTA_ROOT_PATH=/tmp/vre-data \
   .venv/bin/uvicorn voitta_rag_enterprise.main:app \
     --host 0.0.0.0 --port 8000
   ```

   Or for the dedicated MCP-only server: `make mcp` (default port 8001).

2. **Mint an API key** (production only — single-user mode skips this).
   In the rag-enterprise SPA: Settings → API keys → New token.
   The token starts with `vk_`.

3. **Configure the bookmarklet plugin.** Open Voitta Settings, switch
   to the `voitta-enterprise` tab, fill in:

   - **Server URL** — `http://localhost:8000/mcp` for the unified app,
     `http://localhost:8001/mcp` for `make mcp`.
   - **API key** — paste the `vk_…` token. Leave blank in single-user
     mode.

   Hit **Save**. The schema renderer auto-saves each field on input.

   The plugin's [`prompt.md`](../prompt.md) ships in the plugin folder — no configuration required. The backend picks it up at startup via the `system_prompt` manifest field.

4. **Click "Refresh tool list."** The plugin probes the MCP server,
   pulls `list_tools()`, and synthesises a `ToolSpec` per remote tool.
   The status badge flips to **● Connected — N tools**. On a fresh
   chat with the `enterprise.voitta.ai` plugin host (or in the
   developer's chat, when this plugin is active globally), the LLM
   sees all `vre_*` tools.

## Status badge legend

| Badge              | Meaning                                                       |
|--------------------|---------------------------------------------------------------|
| ● Connected — N tools | `list_tools()` succeeded; N tools are live.                   |
| ○ Not configured   | URL field is empty; no probe attempted.                       |
| ⚠ Unreachable      | Connection refused, TLS error, or timeout. Existing tools (if any) stay registered so a brief outage doesn't rip the chat's tool catalog out mid-conversation. |
| ✕ Unauthorized     | Server returned 401/403. Tools are dropped — they wouldn't work anyway. Mint a new token. |
| · Not probed yet   | Connector registered, hasn't been refreshed since startup.    |

## When to click Refresh

Tool lists are pulled **only** on backend startup and when the user
clicks the Refresh button — never per chat turn (the latency would add
up on every conversation). Refresh after:

* Upgrading the voitta-rag-enterprise server to a version with new
  tools.
* Changing the URL or API key (the credentials are picked up live on
  every tool *call*, so this is only for the tool *catalog*).

## Settings keys

The plugin's settings live under the `plugins.voitta-enterprise.*`
namespace in `~/.config/voitta-bookmarklet/settings.json`:

```json
{
  "plugins": {
    "voitta-enterprise": {
      "mcp": {
        "url": "http://localhost:8000/mcp",
        "api_key": "vk_..."
      }
    }
  }
}
```

Same dot-paths are referenced from `manifest.json` (`url_setting`,
`token_setting`) — wire format matches.
