#!/usr/bin/env bash
set -euo pipefail

echo "Disabling incompatible lossless-claw plugin if present"
openclaw plugins disable lossless-claw || true
openclaw plugins uninstall lossless-claw || true

echo "Restarting gateway"
sudo systemctl restart openclaw-gateway
sleep 3

echo "GATEWAY_ACTIVE=$(systemctl is-active openclaw-gateway || true)"
echo "MODEL_PRIMARY=$(openclaw config get agents.defaults.model.primary || true)"
echo "BROWSER_ENABLED=$(openclaw config get browser.enabled || true)"
echo "TELEGRAM_ENABLED=$(openclaw config get channels.telegram.enabled || true)"

echo "CRON_ENTRIES_START"
crontab -l || true
echo "CRON_ENTRIES_END"

python3 - <<'PY'
from pathlib import Path
p = Path('/home/ubuntu/.openclaw/workspace/SOUL.md')
print('SOUL_EXISTS', p.exists())
if p.exists():
    txt = p.read_text()
    for key in [
        'FIRST-MINUTE MOMENTUM FILTER',
        'COPY-TRADING FILTER',
        'BOT FARMING DEFENSE',
    ]:
        print(key, 'YES' if key in txt else 'NO')
PY

echo "OPENCLAW_HEALTH_START"
openclaw health || true
echo "OPENCLAW_HEALTH_END"
