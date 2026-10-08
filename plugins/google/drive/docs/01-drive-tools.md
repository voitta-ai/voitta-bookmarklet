# Google Drive tools (OAuth, read-only)

Five LLM-facing tools, all server-side, all backed by the Google Drive
REST API v3. Read-only — there are no upload/share/move/delete tools
on purpose. The user controls visibility entirely through the Settings
panel: tools are hidden from the LLM until OAuth is connected.

| Tool | Purpose |
| ---- | ------- |
| `drive_list_files` | List a folder's contents (default `root`); optional recursive walk (capped 5 000) |
| `drive_search` | Drive's full-text query syntax — `name contains 'invoice'`, `mimeType = 'application/pdf'`, `modifiedTime > '2025-01-01'`, etc. |
| `drive_get_file` | Single-file metadata, full field set |
| `drive_download_to_python_storage` | Binary content (PDF, image, .csv, .xlsx upload, …) → `python_storage` snapshot, returns handle |
| `drive_export_to_python_storage` | Google native files (Doc / Sheet / Slide / Drawing) → exported format → `python_storage` snapshot |

**Design rule**: no tool returns file CONTENT to the LLM. Tools return metadata + a python_storage handle; reads happen in compute / report scripts via `ctx.snapshot(handle)`. This keeps the LLM's context clean and forces analysis to live in code, where it scales.

## Setup (~5 minutes, one time)

1. Visit [console.cloud.google.com](https://console.cloud.google.com/) signed in with the Gmail account whose Drive you want to read. Use a personal Gmail unless your Workspace admin has enabled OAuth for end users.
2. Create a project (any name).
3. Enable the **Google Drive API** (APIs & Services → Library).
4. Configure the OAuth consent screen via the **Google Auth Platform** items in the left nav:
   - **Branding** — fill in app name + your email.
   - **Audience** — User type **External**, add your Gmail to **Test users**.
   - **Data Access** — add scope `.../auth/drive.readonly`.
5. Create an **OAuth 2.0 Client ID** (Credentials → Create Credentials):
   - Type: **Web application**.
   - Authorized redirect URI: `https://127.0.0.1:12358/api/google/oauth/callback` (exact match required).
6. Copy **Client ID** + **Client Secret** into [`~/.config/voitta-bookmarklet/settings.json`](../backend/app/services/user_settings.py) under:
   ```json
   "googleOAuth": {
     "clientId": "...apps.googleusercontent.com",
     "clientSecret": "GOCSPX-..."
   }
   ```
7. Open the bookmarklet → Settings panel → scroll to **Google Drive** → **Sign in with Google** → consent in the popup.

After that the six tools above appear in the LLM's tool list automatically.

## Why no DOM-scrape / no SAPISIDHASH

A previous iteration of this project tried to read Drive without OAuth: scrape the DOM for file IDs, trigger downloads via `<a download>`, attempt SAPISIDHASH against the public REST API. None of it works in 2026:

- **DOM scrape** is brittle (Drive's rendered structure shifts) and only sees what's currently in the virtualised grid.
- **`<a download>` + `~/Downloads` pickup** works for binaries but not for native Google formats; no metadata access; no search; race conditions on filenames.
- **REST + cookies** is blocked by design (Drive returns *"App configured for first-party authentication, but used NONE"* or *"Origin doesn't match Host for XD3"* — Google explicitly added the XD3 layer to defeat third-party cookie reuse).

OAuth is the only path. The 5-minute setup is the price of doing it right.

## How a typical workflow looks

```
LLM call: drive_search({query: "name contains 'invoice' and mimeType = 'application/pdf'", page_size: 10})
  → returns [{id, name, mime_type, modified_time, ...}]
LLM call: drive_download_to_python_storage({file_id: "1Maf...", name: "invoice 10.pdf"})
  → returns {handle: "py_xxxxxxx", stored_name: "invoice 10.pdf", path: "/Users/roman/.../snapshot_xxxxxxx", bytes: 88321}
LLM call: run_compute({code: "...", args: {handle: "py_xxxxxxx"}})
  → reads bytes via ctx.snapshot(handle), runs pdfplumber, returns extracted text
```

For Google Docs / Sheets / Slides:

```
LLM call: drive_export_to_python_storage({file_id: "1HOz...", format: "txt"})  # Doc → text
  → returns {handle: "py_xxx", stored_name: "<doc name>.txt", path: "...", bytes: 4521}
LLM call: run_compute({code: "...", args: {handle: "py_xxx"}})
  → reads via ctx.snapshot(handle), e.g. summarises with pandas / re / nltk
```

The LLM never sees the document body — that lives in the `python_storage` snapshot until a compute script consumes it. This is intentional: it keeps the LLM context bounded and forces the actual analysis into code that can be re-run, edited, and audited.

## Dedup

Both `drive_download_to_python_storage` and `drive_export_to_python_storage` reuse an existing snapshot for the same `(file_id, optional export_mime)` within 24 h. The reply has `reused: true, reused_age_s: N`. Pass `force_refresh: true` to bypass.

In-flight dedup also coalesces concurrent calls for the same `file_id` — second caller gets `coalesced: true` and the same handle.

## Error handling

| Scenario | Reply shape |
| -------- | ----------- |
| User hasn't connected OAuth | Tools simply aren't in the LLM's tool list. LLM has no way to call them. |
| Token expired & refresh failed | `{ok: false, error: "drive_auth_failed", hint: "user may need to re-connect"}` |
| Native file passed to `drive_download_to_python_storage` | `{ok: false, error: "google_native_format", message: "use drive_export_to_python_storage"}` |
| Binary file passed to `drive_export_to_python_storage` | `{ok: false, error: "not_google_native", message: "use drive_download_to_python_storage"}` |

## Settings storage

OAuth state lives in `~/.config/voitta-bookmarklet/settings.json` under `googleOAuth`:

```json
{
  "googleOAuth": {
    "clientId":     "...apps.googleusercontent.com",
    "clientSecret": "GOCSPX-...",
    "tokens": {
      "access_token":  "ya29....",
      "refresh_token": "1//...",
      "expires_at":     1777854000.0,
      "scope":          "openid email https://www.googleapis.com/auth/drive.readonly",
      "account_email":  "you@gmail.com",
      "token_type":     "Bearer"
    }
  }
}
```

File mode `0600`, dir `0700`. PUT writes from the Settings panel **merge** into the existing blob (top-level keys), so saving LLM API keys doesn't wipe `googleOAuth`.

The 7-day refresh-token expiry on External-Testing OAuth apps means you'll re-authenticate weekly via the Settings panel button. To remove that limit you'd publish the app (still External, but not Testing) — overkill for personal use.

## The `drive://` ref scheme — reports reference upstream

Core rule: reports never bake `py_xxx` handles into their source.
For anything sourced from Google Drive, the canonical ref is:

    drive://file_id=<FILE_ID>[&export=<mime>]

Keys (URL-encode values that contain `/` or `;`):

- `file_id` — required. The Drive file id (the `1AbCdEf...` string,
  not the file name). Stable for the lifetime of the file.
- `export` — only for Google-native types (Docs, Sheets, Slides).
  Picks an export format: `text/csv`, `application/pdf`,
  `text/plain`, etc. Omitted for normal binary files (.csv, .pdf,
  .xlsx already uploaded) — those go through plain download, not
  export. If you don't know whether a file is native or binary,
  `drive_get_file(file_id)` tells you (look for `is_google_native`).

Examples:

    drive://file_id=1aBcDeF...XYZ
    drive://file_id=1aBcDeF...XYZ&export=text/csv
    drive://file_id=1aBcDeF...XYZ&export=application/pdf

### How the resolver works

`ctx.ensure_local("drive://...")` hides two pieces of nuance:

1. **Auth refresh.** OAuth tokens expire. The Drive download
   tools already transparently refresh on 401 — `ensure_local`
   just calls them, so report code never thinks about token
   freshness.
2. **Native vs binary.** When `export` is set, the resolver calls
   `drive_export_to_python_storage(file_id, format=export)`.
   Otherwise it calls `drive_download_to_python_storage(file_id)`.
   The resulting snapshot's `meta.json::origin` records the
   canonical `drive://` ref so subsequent `ensure_local` calls
   hit the cache.

### Correct pattern at report-authoring time

```python
def build(ctx):
    csv_path = ctx.ensure_local(
        "drive://file_id=1AbCdEf...XYZ&export=text/csv"
    )
    df = pd.read_csv(csv_path)
    # ...
```

### Wrong pattern (handle baked into persisted code)

```python
def build(ctx):
    rec = ctx.snapshot("py_a1b2c3")      # ← brittle, will break
    df = pd.read_csv(rec["path"] + "/" + rec["meta"]["stored_name"])
```

### When a handle IS fine

One-shot analysis (you're not authoring a report — just
inspecting). The handle's lifetime matches the conversation.
The upstream-ref rule applies to **persisted** code
(`define_script`), where someone else will run it later.
