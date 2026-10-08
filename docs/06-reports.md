# Reports

A report is a user-authored Python script that produces an HTML string. The string is served in an iframe in the widget's report pane.

## Script contract

Scripts live at `~/Library/Application Support/Voitta Bookmarklet/backend/scripts/<name>/code.py`.

Every script must define a `build(ctx)` function at the top level. It must either:
- Return a raw HTML string, **or**
- Use `ctx` emitters (which produce inline chat content — see below). Returning `None` is fine when you only use emitters.

```python
def build(ctx):
    return "<h1>Hello</h1><p>World</p>"
```

## Execution model

- Scripts run in `asyncio.to_thread()` (a thread pool), never the main event loop.
- Hard timeout: **120 seconds**. A `TimeoutError` is surfaced to the model.
- `matplotlib` is switched to the `Agg` backend before user code runs (no GUI).
- The namespace has normal Python builtins. No import restrictions.
- The script is `compile()`d first — syntax errors are caught before execution.

## ctx API

`ctx` is a `ScriptContext` instance injected by the runner.

### Inputs

```python
ctx.args          # dict — forwarded from run_script(args={...})
ctx.host          # str | None — hostname of the user's current page
```

### Inline emitters

These surface content into the chat alongside the report pane.

```python
ctx.text("## Summary\nSome markdown")   # emit Markdown
ctx.image(fig_bytes, "image/png")        # emit base64 <img> (bytes or base64 str)
ctx.json({"key": "value"})              # emit collapsible JSON block
ctx.log("debug message")                # append to tool-result log lines
```

### Theme

```python
t = ctx.theme()   # dict: {"--voitta-bg": "#1a1a2e", "--voitta-accent": "#7c3aed", ...}
```

Returns CSS-variable name → value pairs from the active plugin's theme. Use to style your report consistently with the surrounding UI.

### Data access

```python
ctx.snapshot("handle")            # return python_storage snapshot record dict
ctx.file("handle")                # return Path to first data file in snapshot
ctx.file("handle", "data.csv")    # return Path to named file in snapshot
ctx.dataframe("handle")           # load curves.pkl as a pandas DataFrame
ctx.raw("handle")                 # load raw.json from snapshot, return parsed value
ctx.ensure_local("scheme://...")  # download upstream artefact ref, return local path
```

`drive://` refs may pin a Google account:
`ctx.ensure_local("drive://<file_id>?account=roman%40agnitio.ai")`. Without
the param the default account is used, with a read-probe fallback across the
other connected accounts (Drive file IDs are globally unique).

### Google Sheets (`ctx.sheets`)

```python
data = ctx.sheets.get(f"{sid}/values/Sheet1!A1:D20")   # raw Sheets API v4
ctx.sheets.put(f"{sid}/values/Sheet1!A1", {...}, valueInputOption="USER_ENTERED")
ctx.sheets.post(f"{sid}:batchUpdate", {"requests": [...]})
meta = ctx.sheets.get_metadata(sid)
```

`ctx.sheets` is bound to the script's **pinned Google account** — the account
email captured when the script was saved (`define_script` /`edit_script`
optional `google_account` arg, email-or-label; omitted → the settings-default
account's email at save time). The pin lives in the script's meta, so re-runs
use the same account no matter what the default is changed to later. If the
pinned account is missing, disconnected, or lacks the `spreadsheets` scope,
any `ctx.sheets` use raises a hard error naming that account — there is no
silent fall-through to another account. Legacy scripts without a pin resolve
the default account at run time.

## Output: one format only

Reports produce **one thing**: a raw HTML string. Return it from `build(ctx)`.

```python
def build(ctx):
    import base64, io
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    ax.plot([1, 2, 3], [4, 1, 3])
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f'<html><body><img src="data:image/png;base64,{b64}"></body></html>'
```

The HTML is served at `/api/html-report?id=<slug>` and rendered in a same-origin `<iframe>`.

If you need a **PDF file** as a deliverable, build it directly with `fpdf2` —
see [`08-pdf-export.md`](08-pdf-export.md).

## Script kinds & effects

Every script carries a declared **kind** and observed **effects** in its meta:

- `kind: "report"` — `build(ctx)` returns an HTML string. Returning `None`
  is a **named error** (not a silent no-render).
- `kind: "chat"` — returns `None`, emits via `ctx.text/image/json`.
- `kind: "job"` — side effects are the point (data manipulation, Sheets
  writes); no visual output expected.

Declare via `define_script(kind=…)` or let it be inferred from the smoke
run; re-declare via `edit_script(kind=…)`. Effects (`renders_html`,
`emits_inline`, `writes_external`) are recorded from every run as a sticky
union — an explicit `kind` re-declare is the only reset.

**Confirm gate:** `run_script` on a script whose effects include
`writes_external` returns `status: "needs-confirmation"` until re-called
with `confirm: true` — ask the user first; re-running repeats the write.
Scripts you defined/edited in the same session run without the confirm.

## Smoke testing

`define_script` and `edit_script` run a smoke test (`sandbox.smoke_test()`) before persisting the code. The script must not crash during a bare `build(ctx)` call. If it does, the error is returned to the model without saving.

Smoke tests **dry-run external writes**: `ctx.sheets.put/post` return
shape-correct synthetic responses (`{"dryRun": true, …}`) instead of hitting
Google — a script never performs a real write at define/edit time. Reads
pass through normally.

## Script tools summary

| Tool | Action |
|---|---|
| `define_script(name, code, google_account?, kind?)` | Create; smoke-test first; pins the Google account email for `ctx.sheets` (default: current default account); `kind` declared or inferred from smoke |
| `edit_script(name, edits, google_account?, kind?)` | Apply search-replace edits; smoke-test first; keeps existing pin/kind unless re-declared (`kind` re-declare also resets recorded effects) |
| `run_script(name, args?, wait_s?, confirm?)` | Execute; dispatch HTML to pane; `confirm: true` required for scripts that write to Google Sheets |
| `list_scripts(project?)` / `get_script(name, project?)` | Default: active project; `project` reads another project (read-only) |
| `copy_from_project(project, script, new_name?)` | Copy a script (+ its kind/effects/account pin) into the active project |
| `project_remember(text)` | Append a durable note to the active project's PROJECT.md (injected into every future conversation) |
| `verify_script(name, code)` | Smoke-test without saving |
| `get_script(name)` | Read source |
| `get_script_errors(name)` | Read last runtime errors |
| `delete_script(name)` | Remove |
