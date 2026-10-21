#!/bin/bash
# Public HTTPS address for the dashboard (Cloudflare quick tunnel -> 127.0.0.1:8000), 24/7 via launchd.
# The app itself asks for login and is read-only from outside (src/api/auth.py).
# The quick-tunnel address changes when cloudflared restarts (reboot): `url` prints the current one, and
# `sync` points the Vercel site at it (needs VERCEL_TOKEN in .env).
#   scripts/kairos_tunnel.sh install | uninstall | url | sync
set -euo pipefail
LABEL="com.kairos.tunnel"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/kairos-tunnel.log"
DOMAIN="gui/$(id -u)"
CF="$(command -v cloudflared || echo /opt/homebrew/bin/cloudflared)"
VERCEL_PROJECT="${VERCEL_PROJECT:-kairos-bots}"

url() { grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" 2>/dev/null | tail -1; }

sync() {
  local u last token
  u="$(url)"; [ -n "$u" ] || { echo "no tunnel url yet"; exit 1; }
  last="$(cat "$DIR/.tunnel_synced" 2>/dev/null || true)"
  [ "$u" = "$last" ] && { echo "vercel already points to $u"; return; }
  token="$(grep -E '^VERCEL_TOKEN=' "$DIR/.env" | cut -d= -f2- || true)"
  [ -n "$token" ] || { echo "tunnel is $u — add VERCEL_TOKEN to .env to update Vercel automatically"; return; }
  python3 - "$u" "$VERCEL_PROJECT" > /tmp/kairos_vercel.json <<'PY'
import json, sys
u, name = sys.argv[1], sys.argv[2]
cfg = {"rewrites": [{"source": "/(.*)", "destination": f"{u}/$1"}]}
print(json.dumps({"name": name, "project": name, "target": "production",
                  "files": [{"file": "vercel.json", "data": json.dumps(cfg)}],
                  "projectSettings": {"framework": None}}))
PY
  curl -fsS -X POST "https://api.vercel.com/v13/deployments?skipAutoDetectionConfirmation=1" \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" --data @/tmp/kairos_vercel.json >/dev/null
  echo "$u" > "$DIR/.tunnel_synced"; echo "vercel now proxies to $u"
}

install() {
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$CF</string><string>tunnel</string><string>--no-autoupdate</string>
    <string>--url</string><string>http://127.0.0.1:8000</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$LOG</string><key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PL
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  for _ in $(seq 1 30); do launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break; sleep 1; done
  : > "$LOG"
  launchctl bootstrap "$DOMAIN" "$PLIST"
  for _ in $(seq 1 30); do [ -n "$(url)" ] && break; sleep 1; done
  echo "tunnel: $(url)"
}

case "${1:-url}" in
  install) install ;;
  uninstall) launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true; rm -f "$PLIST"; echo "removed $LABEL" ;;
  url) url ;;
  sync) sync ;;
  *) echo "usage: $0 install|uninstall|url|sync"; exit 2 ;;
esac
