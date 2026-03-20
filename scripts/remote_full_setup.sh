#!/usr/bin/env bash
set -u

OPENROUTER_KEY="${OPENROUTER_KEY:-}"
LIGHTPANDA_WSS_URL="${LIGHTPANDA_WSS_URL:-}"

run() {
  echo "[CMD] $*"
  eval "$@"
  local code=$?
  echo "[EXIT:$code]"
  return 0
}

echo "[INFO] Starting remote full setup"

run "openclaw doctor"

run "sudo apt-get update -y"
run "sudo apt-get install -y ufw fail2ban"
run "sudo ufw default deny incoming"
run "sudo ufw default allow outgoing"
run "sudo ufw allow from 100.64.0.0/10 to any port 22 proto tcp"
run "sudo ufw --force enable"
run "sudo systemctl enable --now fail2ban"

run "command -v tailscale || (curl -fsSL https://tailscale.com/install.sh | sh)"
run "timeout 20 sudo tailscale up --ssh"

if [[ -n "${OPENROUTER_KEY}" ]]; then
  run "openclaw config set env.OPENROUTER_API_KEY \"${OPENROUTER_KEY}\""
  run "openclaw config set agents.defaults.model.fallback \"openrouter/openrouter/auto\""
else
  echo "[WARN] OPENROUTER_KEY not provided; skipping key config"
fi

run "openclaw onboard --auth-choice ollama"
run "openclaw plugins install nvidia-nemoclaw"
run "openclaw config set agents.defaults.model.primary \"nemotron-4:14b\""
run "openclaw config set agents.research.model \"nemotron-4:14b\""
run "openclaw config set agents.guardian.model \"nemotron-4:14b\""
run "openclaw config set runtime.sandbox \"openshell\""
run "openclaw config set agents.guardian.killThreshold \"0.25\""
run "openclaw config set audit.immutableLog \"true\""
run "openclaw config set changes.requireHumanApproval \"true\""

run "clawhub install lossless-claw"
run "clawhub install guardian-executor"
run "clawhub install research-agent"
run "clawhub install lightpanda-browser"
run "clawhub install webull-browser"
run "clawhub install kalshi-browser"
run "clawhub install news-sentinel"
run "clawhub install agent-watcher"
run "clawhub install claude-watchdog"

if [[ -n "${LIGHTPANDA_WSS_URL}" && "${LIGHTPANDA_WSS_URL}" != *"REPLACE_ME"* ]]; then
  run "openclaw config set agents.defaults.browser.engine \"lightpanda-cloud\""
  run "openclaw config set agents.defaults.browser.wssEndpoint \"${LIGHTPANDA_WSS_URL}\""
  run "openclaw config set agents.defaults.browser.fallback \"chrome\""
else
  echo "[WARN] LIGHTPANDA_WSS_URL missing/placeholder; skipping browser cloud config"
fi

run "openclaw config set channels.telegram.enabled \"true\""
run "openclaw config set channels.telegram.configWrites \"true\""
run "openclaw config set channels.telegram.capabilities.inlineButtons \"all\""
run "openclaw config set execApprovals.enabled \"true\""

run "mkdir -p ~/.openclaw/workspace"
run "cp /tmp/finalImplemenatationSoul.md ~/.openclaw/workspace/SOUL.md"

tmp="$(mktemp)"
crontab -l 2>/dev/null | awk '
  $0 !~ /openclaw run-agent trading-daily-review/ &&
  $0 !~ /openclaw run-agent trading-scan/ &&
  $0 !~ /openclaw run-agent research-cycle/ &&
  $0 !~ /openclaw run-agent evolutionary-tune/
' > "$tmp"
cat >> "$tmp" <<'EOF'
0 9 * * * openclaw run-agent trading-daily-review
*/5 * * * * openclaw run-agent trading-scan
0 8,20 * * * openclaw run-agent research-cycle
0 3 * * 0 openclaw run-agent evolutionary-tune
EOF
crontab "$tmp"
rm -f "$tmp"

run "sudo systemctl restart openclaw-gateway"
run "openclaw health"
run "systemctl is-active openclaw-gateway"
run "crontab -l"
run "rg -n \"FIRST-MINUTE MOMENTUM FILTER|COPY-TRADING FILTER|BOT FARMING DEFENSE\" ~/.openclaw/workspace/SOUL.md"

echo "[INFO] Remote full setup complete"
