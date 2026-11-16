# Tool Catalog

All tools are `ToolSpec` instances registered with `app.tools.registry`. They are loaded at startup by `app/tools/load.py`.

## Report / script management

| Tool | Description |
|---|---|
| `run_script` | Execute a saved script; dispatches HTML to the report pane or inline to chat |
| `define_script` | Create a new script (smoke-tests before saving) |
| `edit_script` | Replace the source of an existing script (smoke-tests before saving) |
| `get_script` | Return the source code of a saved script |
| `list_scripts` | List all saved scripts, optionally filtered by folder |
| `delete_script` | Delete a saved script |
| `get_script_errors` | Return the last runtime errors for a script |
| `verify_script` | Smoke-test a script source without saving or running |
| `get_active_report` | Return the slug of the report currently mounted in the pane |
| `screenshot_report` | Capture the currently-mounted report as a PNG via html-to-image |

## Workspace / data

| Tool | Description |
|---|---|
| `list_data` | List python_storage snapshots available in the workspace |
| `preview_data` | Return a preview of a snapshot (head rows, schema, sample) |

## Folder management

| Tool | Description |
|---|---|
| `create_folder` | Create a script folder |
| `delete_folder` | Delete a folder (and optionally its scripts) |
| `move_to_folder` | Move a script into a folder |

## Browser tools

| Tool | Description |
|---|---|
| `browser_eval` | Execute arbitrary JavaScript in the user's active tab; returns result + console logs |
| `get_page_title` | Return the `document.title` of the current tab |
| `read_page` | Read the page's rendered text or HTML through the DOM, whole page or by CSS selector, without running JavaScript; works where the page's CSP forbids eval |

## Utility

| Tool | Description |
|---|---|
| `get_current_time` | Return the current server time (ISO 8601) |

## RAG (when configured)

| Tool | Description |
|---|---|
| `rag_search` | Vector + BM25 search over the indexed docs corpus |
| `rag_index` | Trigger a re-index of the docs corpus |

## Google plugin tools (Drive / Sheets)

Registered by the `google` plugins; visible only when at least one Google
account is connected (Drive) / has the `spreadsheets` scope (Sheets).

| Tool | Description |
|---|---|
| `drive_list_files` | List a Drive folder (optionally recursive, capped) |
| `drive_search` | Drive native query syntax search |
| `drive_get_file` | Single-file metadata by ID |
| `drive_download_to_python_storage` | Download binary content into python_storage (24 h dedup) |
| `drive_export_to_python_storage` | Export a Google-native file (Doc/Sheet/Slide/Drawing) to txt/pdf/csv/… |
| `drive_pickup_to_python_storage` | No-OAuth fallback: browser-session download + Downloads-folder watcher (visible only when OAuth is NOT connected and `plugins.google.driveDownloadViaPickup` is on) |
| `sheets_get_metadata` | Workbook sheets: names, GIDs, dimensions |
| `sheets_read_range` | Read cells (A1 notation) |
| `sheets_write_range` | Overwrite a range — **never probes accounts** |
| `sheets_append_rows` | Append rows — **never probes accounts** |

**Multi-account semantics** (all of the above except pickup):

- Optional `account` arg — email or label of a connected account; the
  connected roster is appended to each tool description per turn
  (`ToolSpec.dynamic_description`). Omitted → the settings default account.
- **Reads** that 403/404 under a non-explicit account are retried across the
  other connected, scope-qualified accounts in deterministic order (default
  first, then creation order); the serving account's email comes back in the
  result (`account`) and in the snapshot origin. 401s never probe.
- **Writes** use exactly the selected account; a scope/permission failure is
  a hard error naming which accounts do qualify.

## Host visibility

`registry.visible_for_host(host)` filters the tool list based on each `ToolSpec.host_pattern`. Tools with `global_tool=True` or `host_pattern=None` are always visible. Plugin-contributed tools inherit the plugin's `host_patterns` from the manifest unless they declare their own.
