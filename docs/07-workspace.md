# Workspace

The workspace holds data snapshots and script files, organized in folders.

## Data snapshots (python_storage)

Snapshots are named artefacts stored under `~/Library/Application Support/Voitta Bookmarklet/backend/python_storage/<handle>/`. Each snapshot is a directory with a `meta.json` plus one or more data files.

Snapshot kinds:

| Kind | Data file | Loaded via |
|---|---|---|
| `curves` | `curves.pkl` (pandas DataFrame) | `ctx.dataframe(handle)` |
| `raw` | `raw.json` | `ctx.raw(handle)` |
| Generic file | any file | `ctx.file(handle)` or `ctx.file(handle, filename)` |

Snapshots are created by tools that download or process data (e.g. drive pickup, data imports). The model uses `list_data` to discover them and `preview_data` to inspect their shape.

## Scripts

Scripts live at `~/Library/Application Support/Voitta Bookmarklet/backend/scripts/<name>/code.py`.

- Script names are slugs: lowercase alphanumeric + hyphens, max 64 chars.
- Each script is a directory containing `code.py` and optionally `errors.json`.

## Folders

Scripts can be organized into folders for grouping. Folder operations:

| Tool | Action |
|---|---|
| `create_folder(name)` | Create a folder |
| `delete_folder(name)` | Delete a folder |
| `move_to_folder(script, folder)` | Move a script into a folder |
| `list_scripts(folder?)` | List scripts, optionally filtered by folder |

Folders are logical groupings only — they don't affect script execution.

## Settings location

User settings (`settings.json`) live at `~/.config/voitta-bookmarklet/settings.json`, separate from the workspace.

## Uploads

File attachments from Chainlit messages are stored at `~/Library/Application Support/Voitta Bookmarklet/backend/uploads/` and served at `/api/uploads/`.
