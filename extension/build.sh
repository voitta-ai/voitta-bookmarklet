#!/usr/bin/env bash
# Build the unpacked extension: bundle the widget and copy it plus the icons in.
# Load the result from chrome://extensions → Developer mode → Load unpacked → extension/.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"

if [[ "${1:-}" != "--no-frontend" ]]; then
  (cd "$root/frontend" && { [[ -d node_modules ]] || npm ci; } && npm run build)
fi

mkdir -p "$here/vendor" "$here/icons"
# chrome.scripting refuses a file holding Unicode noncharacters (the bundle has
# U+FFFF literals), so re-emit it with every non-ASCII character escaped.
"$root/frontend/node_modules/.bin/esbuild" "$root/frontend/dist/widget.js" \
  --charset=ascii --log-level=warning --outfile="$here/vendor/widget.js"
for s in 16 32 128; do
  cp "$root/icons/voitta.iconset/icon_${s}x${s}.png" "$here/icons/icon-$s.png"
done
echo "extension ready: $here (widget $(wc -c < "$here/vendor/widget.js") bytes)"
