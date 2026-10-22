#!/bin/bash
# Public HTTPS address for the dashboard (Cloudflare quick tunnel -> 127.0.0.1:8000), 24/7 via launchd.
# The app itself asks for login and is read-only from outside (src/api/auth.py).
# The quick-tunnel address changes when cloudflared restarts (reboot): `url` prints the current one, and
# `sync` points the Vercel site at it. `install` also adds a launchd job running `sync` every 2 minutes, so after a
# reboot the site follows the new address by itself. Needs VERCEL_TOKEN in .env (git-ignored — never commit it).
#   scripts/kairos_tunnel.sh install | uninstall | url | sync
set -euo pipefail
LABEL="com.kairos.tunnel"
SYNC_LABEL="com.kairos.tunnel-sync"
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
  if [ "$u" = "$last" ]; then
    # the site can be replaced behind our back (a git deployment of the repo): trust it only while it answers
    code="$(curl -s -m 15 -o /dev/null -w '%{http_code}' "https://$VERCEL_PROJECT.vercel.app/login" || true)"
    [ "$code" = "200" ] && { echo "vercel already points to $u"; return; }
    echo "vercel answered $code: redeploying the proxy"
  fi
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
  local hdr; hdr="$(mktemp)"; chmod 600 "$hdr"
  printf 'Authorization: Bearer %s\n' "$token" > "$hdr"          # header from a file: the token never shows in `ps`
  curl -fsS -X POST "https://api.vercel.com/v13/deployments?skipAutoDetectionConfirmation=1" \
    -H @"$hdr" -H "Content-Type: application/json" --data @/tmp/kairos_vercel.json >/dev/null || { rm -f "$hdr"; echo "vercel deploy failed"; exit 1; }
  rm -f "$hdr"
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
  cat > "$HOME/Library/LaunchAgents/$SYNC_LABEL.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$SYNC_LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DIR/scripts/kairos_tunnel.sh</string><string>sync</string></array>
  <key>RunAtLoad</key><true/><key>StartInterval</key><integer>120</integer>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/kairos-tunnel-sync.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/kairos-tunnel-sync.log</string>
</dict></plist>
PL
  launchctl bootout "$DOMAIN/$SYNC_LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$HOME/Library/LaunchAgents/$SYNC_LABEL.plist"
  echo "auto-sync to Vercel every 2 min (log: ~/Library/Logs/kairos-tunnel-sync.log)"
}

case "${1:-url}" in
  install) install ;;
  uninstall) for l in "$LABEL" "$SYNC_LABEL"; do launchctl bootout "$DOMAIN/$l" 2>/dev/null || true; rm -f "$HOME/Library/LaunchAgents/$l.plist"; done
             echo "removed $LABEL and $SYNC_LABEL" ;;
  url) url ;;
  sync) sync ;;
  *) echo "usage: $0 install|uninstall|url|sync"; exit 2 ;;
esac
