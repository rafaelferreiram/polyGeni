#!/bin/bash
# 24/7 watchdog (launchd, every 2 min, OUTSIDE the app: a hung app cannot restart itself). Restarts the service when
#   1) the API stops answering 3 checks in a row, or
#   2) the bots' cycle counters stop advancing for 15 minutes while the API still answers (a hung scheduler).
# A restart is graceful (the app waits for running account cycles) and at most one per 10 minutes.
#   scripts/kairos_watchdog.sh install | uninstall | run | status
set -uo pipefail
LABEL="com.kairos.watchdog"
SERVICE="com.kairos.bots"
PORT="${SAVOIA_PORT:-1914}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/kairos-watchdog.log"
STATE="${WATCHDOG_STATE:-$HOME/.kairos/watchdog.json}"
URL="${WATCHDOG_URL:-http://127.0.0.1:$PORT/api/paper/arena}"
DOMAIN="gui/$(id -u)"
mkdir -p "$(dirname "$STATE")"

run() {
  local now body sig
  now=$(date +%s)
  body=$(curl -s -m 25 "$URL" || true)
  sig=$(printf '%s' "$body" | python3 -c "import json,sys; d=json.load(sys.stdin); print(sum(int(x.get('cycle') or 0) for x in d['leaderboard']))" 2>/dev/null || true)
  python3 - "$now" "${sig:-}" "$STATE" "${WATCHDOG_DRY:-0}" "$SERVICE" "$DOMAIN" "$LOG" <<'PY'
import json, os, subprocess, sys, time
now, sig, state_path, dry, service, domain, log = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4] == "1", sys.argv[5], sys.argv[6], sys.argv[7]
try:
    st = json.load(open(state_path))
except Exception:
    st = {}
def say(msg):
    with open(log, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
reason = None
if not sig:                                   # no answer (or garbage): count it
    st["fails"] = st.get("fails", 0) + 1
    if st["fails"] >= 3:
        reason = f"API sem resposta em {st['fails']} checagens seguidas"
else:
    st["fails"] = 0
    if str(st.get("sig")) != sig:
        st["sig"], st["changed"] = sig, now   # the cycles advanced: alive
    elif now - st.get("changed", now) >= 15 * 60:
        reason = f"ciclos parados há {(now - st['changed']) // 60} min com a API respondendo"
if reason and now - st.get("last_restart", 0) < 600:
    say(f"precisaria reiniciar ({reason}) mas já reiniciou há menos de 10 min: aguardando")
    reason = None
if reason:
    say(f"REINICIANDO o serviço: {reason}" + (" (simulação)" if dry else ""))
    if not dry:
        subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{service}"], check=False)
    st.update(last_restart=now, fails=0, changed=now, sig=None)
os.makedirs(os.path.dirname(state_path), exist_ok=True)
json.dump(st, open(state_path, "w"))
print(json.dumps({"fails": st.get("fails"), "sig": st.get("sig"), "restart": reason}))
PY
}

install() {
  chmod +x "$0"
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DIR/scripts/kairos_watchdog.sh</string><string>run</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string><key>SAVOIA_PORT</key><string>$PORT</string></dict>
  <key>RunAtLoad</key><true/><key>StartInterval</key><integer>120</integer>
  <key>StandardOutPath</key><string>$LOG</string><key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PL
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST"
  echo "watchdog instalado (a cada 2 min). Log: $LOG"
}

case "${1:-status}" in
  install) install ;;
  uninstall) launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true; rm -f "$PLIST"; echo "removed $LABEL" ;;
  run) run ;;
  status) launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|runs =|last exit" || echo "not installed"; tail -5 "$LOG" 2>/dev/null ;;
  *) echo "usage: $0 install|uninstall|run|status"; exit 2 ;;
esac
