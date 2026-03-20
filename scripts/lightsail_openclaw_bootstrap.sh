#!/usr/bin/env bash
set -euo pipefail

# Bootstrap script for the target Lightsail OpenClaw host.
# Run this ON the provisioned instance, not on local macOS.

log() { printf "\n[%s] %s\n" "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This script must run on the Linux Lightsail instance."
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env.tradingbot"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Copy .env.tradingbot.example and fill values."
  exit 1
fi

# shellcheck disable=SC1090
source "${ENV_FILE}"

need_cmd openclaw
need_cmd clawhub
need_cmd sudo
need_cmd curl

log "Running OpenClaw health doctor"
openclaw doctor

log "Pair gateway if not already paired"
openclaw gateway pair || true

log "Configure providers"
openclaw onboard --auth-choice openrouter || true
openclaw onboard --auth-choice ollama || true

log "Install core plugins and skills"
openclaw plugins install nvidia-nemoclaw || true
clawhub install lossless-claw || true
clawhub install guardian-executor || true
clawhub install research-agent || true
clawhub install lightpanda-browser || true
clawhub install webull-browser || true
clawhub install kalshi-browser || true
clawhub install news-sentinel || true
clawhub install agent-watcher || true
clawhub install claude-watchdog || true

log "Set browser runtime to Lightpanda cloud"
openclaw config set agents.defaults.browser.engine "lightpanda-cloud"
openclaw config set agents.defaults.browser.wssEndpoint "${LIGHTPANDA_WSS_URL}"
openclaw config set agents.defaults.browser.fallback "chrome"

log "Set model routing with fallback"
openclaw config set agents.defaults.model.primary "${PRIMARY_MODEL}"
openclaw config set agents.research.model "${RESEARCH_MODEL}"
openclaw config set agents.guardian.model "${GUARDIAN_MODEL}"
openclaw config set runtime.sandbox "openshell"
openclaw config set agents.defaults.model.fallback "openrouter/openrouter/auto"

log "Set Guardian threshold and safety controls"
openclaw config set agents.guardian.killThreshold "0.25"
openclaw config set audit.immutableLog "true"
openclaw config set changes.requireHumanApproval "true"

log "Enable Telegram channel capabilities"
openclaw config set channels.telegram.enabled "true"
openclaw config set channels.telegram.configWrites "true"
openclaw config set channels.telegram.capabilities.inlineButtons "all"
openclaw config set execApprovals.enabled "true"

log "Install Tailscale (if missing)"
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
sudo tailscale up --ssh || true

log "Apply host-level firewall hardening"
sudo apt-get update -y
sudo apt-get install -y ufw fail2ban
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 100.64.0.0/10 to any port 22 proto tcp
sudo ufw --force enable
sudo systemctl enable --now fail2ban

log "Restart OpenClaw gateway"
sudo systemctl restart openclaw-gateway
openclaw status || true

log "Bootstrap complete"
