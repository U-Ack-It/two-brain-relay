#!/usr/bin/env bash
# install.sh — run the relay as a background systemd timer (every 10 minutes).
#   bash install.sh
set -euo pipefail
R="$(cd "$(dirname "$0")" && pwd)"
U="$HOME/.config/systemd/user"; mkdir -p "$U"
PY="$(command -v python3)"
[ -f "$R/relay.json" ] || cp "$R/relay.example.json" "$R/relay.json"

cat > "$U/relay.service" <<UNIT
[Unit]
Description=Two-brain relay - one work slice
After=network-online.target
[Service]
Type=oneshot
WorkingDirectory=$R
ExecStart=$PY $R/relay.py tick
TimeoutStartSec=15min
Nice=10
UNIT

cat > "$U/relay.timer" <<UNIT
[Unit]
Description=Run the two-brain relay every 10 minutes
[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
Persistent=true
[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now relay.timer >/dev/null
echo "Timer enabled: $(systemctl --user is-active relay.timer)"
echo "Add an API key to your OS keyring:  secret-tool store --label='API key: anthropic' service apikeys name anthropic"
