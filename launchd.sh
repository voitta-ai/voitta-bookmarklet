#!/usr/bin/env bash
# Run ./start.sh as a per-user macOS LaunchAgent: starts at login, restarts if it exits.
#
#   ./launchd.sh install     write the plist, load it, wait for /health
#   ./launchd.sh uninstall   unload it and remove the plist
#   ./launchd.sh status      launchd's view of the job
#   ./launchd.sh plist       print the plist that install would write
#
# The plist hard-codes this checkout's path: uninstall before moving or deleting it.
# The job gets launchd's environment (launchctl setenv), not your shell's.
# shortcut: no log rotation (launchd appends to one file), truncate it by hand when large.
# shortcut: no XML escaping of paths, plutil -lint refuses a path containing & or <.
set -euo pipefail
cd "$(dirname "$0")"

ROOT="$(pwd)"
LABEL="ai.voitta.bookmarklet"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/voitta-bookmarklet"
DOMAIN="gui/$(id -u)"
MAIN_PORT=12358     # start.sh defaults
BRIDGE_PORT=12359

[ "$(uname)" = "Darwin" ] || { echo "[launchd.sh] macOS only" >&2; exit 1; }

cmd_plist() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/start.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOGDIR/server.log</string>
  <key>StandardErrorPath</key>
  <string>$LOGDIR/server.log</string>
</dict>
</plist>
EOF
}

stop_job() {
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
}

# Wait up to 30s per port for it to free (a stopping job lingers until its exit timeout).
# Returns 1 and names the holder if something still listens.
wait_ports_free() {
  local port pid i
  for port in $MAIN_PORT $BRIDGE_PORT; do
    i=0
    while pid="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -1)" && [ -n "$pid" ]; do
      i=$((i + 1))
      if [ "$i" -gt 30 ]; then
        echo "[launchd.sh] port $port is held by pid $pid: $(ps -o command= -p "$pid" | cut -c1-100)" >&2
        return 1
      fi
      sleep 1
    done
  done
}

wait_health() {
  local i
  for i in $(seq 1 60); do
    if curl -skf -m 2 "https://127.0.0.1:$MAIN_PORT/health" > /dev/null \
      || curl -sf -m 2 "http://127.0.0.1:$MAIN_PORT/health" > /dev/null; then
      echo "[launchd.sh] $LABEL is up; log: $LOGDIR/server.log"
      return 0
    fi
    sleep 1
  done
  echo "[launchd.sh] $LABEL not healthy after 60s; see $LOGDIR/server.log" >&2
  return 1
}

# Roll back, so a failed install leaves nothing that loads at the next login or crash-loops.
fail_install() {
  echo "[launchd.sh] install failed; last log lines:" >&2
  { tail -n 20 "$LOGDIR/server.log" || true; } >&2
  stop_job
  rm -f "$PLIST"
  exit 1
}

cmd_install() {
  [ -x backend/.venv/bin/uvicorn ] || { echo "[launchd.sh] backend/.venv missing - run ./build.sh first" >&2; exit 1; }
  mkdir -p "$(dirname "$PLIST")" "$LOGDIR"
  stop_job
  wait_ports_free || { echo "[launchd.sh] stop it, then run install again" >&2; exit 1; }
  cmd_plist > "$PLIST.tmp"
  plutil -lint "$PLIST.tmp" > /dev/null || { rm -f "$PLIST.tmp"; exit 1; }
  mv "$PLIST.tmp" "$PLIST"
  launchctl bootstrap "$DOMAIN" "$PLIST" || fail_install
  wait_health || fail_install
}

cmd_uninstall() {
  stop_job
  rm -f "$PLIST"
  wait_ports_free || true
  echo "[launchd.sh] removed $LABEL"
}

cmd_status() {
  local out
  if out="$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null)"; then
    printf '%s\n' "$out" | grep -E '^[[:space:]]*(state|pid|last exit code) = ' || true
  else
    echo "not loaded"
  fi
  echo "plist: $PLIST"
  echo "log:   $LOGDIR/server.log"
}

case "${1:-}" in
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  status)    cmd_status ;;
  plist)     cmd_plist ;;
  *) echo "usage: ./launchd.sh install|uninstall|status|plist" >&2; exit 2 ;;
esac
