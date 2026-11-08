#!/usr/bin/env bash
# One-time: bring the data of the old host install (server-start.sh, before Docker) into data/.
#
#   deploy/migrate-from-host.sh [OLD_DATA_ROOT] [OLD_CONFIG_DIR]
#     defaults: ~/.local/share/voitta   ~/.config/voitta-bookmarklet
#
# Copies (never moves — the originals stay as a backup) into ./data, which deploy/compose.yml
# mounts at /data. Refuses if data/ already has user data, so it can't clobber a live server.
set -euo pipefail
cd "$(dirname "$0")/.."
OLD="${1:-$HOME/.local/share/voitta}"
OLD_CFG="${2:-$HOME/.config/voitta-bookmarklet}"

[ -d "$OLD" ] || { echo "[migrate] $OLD not found" >&2; exit 1; }
if [ -e data/users ] || [ -e data/conversations.sqlite ] || [ -e data/users.sqlite ]; then
  echo "[migrate] data/ already holds user data — refusing to overwrite" >&2
  exit 1
fi
mkdir -p data/config
cp -a "$OLD"/. data/
if [ -f "$OLD_CFG/settings.json" ] && [ ! -e data/config/settings.json ]; then
  cp -a "$OLD_CFG/settings.json" data/config/settings.json
fi
chmod -R go-rwx data
echo "[migrate] copied $OLD → data/ ($(du -sh data | cut -f1)); originals left in place"
ls data/users 2>/dev/null | sed 's/^/  user folder: /'
