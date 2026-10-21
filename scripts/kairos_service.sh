#!/bin/bash
# Run Kairos 24/7 on this Mac as a launchd user agent: starts at login, restarts if it crashes,
# keeps the Mac from idle-sleeping while it runs (caffeinate). Closing the lid still sleeps the Mac.
#   scripts/kairos_service.sh install | uninstall | restart | status | logs
set -euo pipefail

LABEL="com.kairos.bots"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/kairos.log"
DOMAIN="gui/$(id -u)"

install() {
  [ -x "$DIR/.venv/bin/uvicorn" ] || { echo "missing $DIR/.venv — run 'make install' first"; exit 1; }
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string><string>-s</string>
    <string>$DIR/.venv/bin/uvicorn</string><string>src.main:app</string>
    <string>--host</string><string>127.0.0.1</string><string>--port</string><string>8000</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PAPER_AUTOSTART</key><string>1</string>
    <key>PAPER_CYCLE_SEC</key><string>300</string>
    <key>PAPER_DB_PATH</key><string>./paper.db</string>
    <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST"
  echo "installed: $PLIST (logs: $LOG) — dashboard http://127.0.0.1:8000"
}

case "${1:-status}" in
  install) install ;;
  uninstall) launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true; rm -f "$PLIST"; echo "removed $LABEL" ;;
  restart) launchctl kickstart -k "$DOMAIN/$LABEL" ;;
  status) launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit code" || echo "not installed" ;;
  logs) tail -n 100 -f "$LOG" ;;
  *) echo "usage: $0 install|uninstall|restart|status|logs"; exit 2 ;;
esac
