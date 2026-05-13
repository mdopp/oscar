#!/usr/bin/env bash
#
# OSCAR installer — top-level orchestrator.
#
# Sequences three things in order:
#   1. Install Hermes Agent on this host (via Nous Research's official
#      installer). Hermes is OSCAR's agent core; skill management, cron,
#      messaging gateways, and the self-improvement loop all live there.
#   2. Deploy the OSCAR data-plane templates (oscar-brain, oscar-voice,
#      oscar-connectors) to your ServiceBay node via the ServiceBay-MCP.
#   3. Link OSCAR's skills into Hermes' skills dir so Hermes loads them.
#
# Idempotent: re-running skips steps that are already done.
#
# Required env:
#   SB_URL             ServiceBay-MCP URL (e.g. http://192.168.178.100:5888/mcp)
#   SB_TOKEN           ServiceBay-MCP bearer token
#
# Optional env:
#   HERMES_HOME        Where Hermes installs (default: ~/.hermes)
#   OSCAR_REPO_DIR     Where this OSCAR checkout lives (default: detected)
#   SKIP_HERMES        Skip step 1 (you've already installed Hermes)
#   SKIP_TEMPLATES     Skip step 2 (templates already deployed)
#   SKIP_SKILL_LINK    Skip step 3
#
# Usage:
#   scripts/install.sh
#   SKIP_HERMES=1 scripts/install.sh        # only deploy templates + link skills
#

set -euo pipefail

: "${SB_URL:?SB_URL not set — e.g. http://192.168.178.100:5888/mcp}"
: "${SB_TOKEN:?SB_TOKEN not set}"

OSCAR_REPO_DIR="${OSCAR_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- Step 1: Hermes Agent -------------------------------------------------

install_hermes() {
  if [[ -n "${SKIP_HERMES:-}" ]]; then
    log "Skipping Hermes install (SKIP_HERMES set)"
    return
  fi
  if command -v hermes >/dev/null 2>&1; then
    log "Hermes already installed at $(command -v hermes); skipping install"
    return
  fi
  log "Installing Hermes Agent via Nous Research's official installer"
  log "  → curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash"
  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
  # Hermes installer modifies ~/.bashrc; make hermes available in this shell.
  if [[ -d "$HERMES_HOME/bin" ]]; then
    export PATH="$HERMES_HOME/bin:$PATH"
  fi
  command -v hermes >/dev/null 2>&1 || warn "hermes binary not in PATH yet — open a new shell after the install completes"
}

# ---- Step 2: ServiceBay-MCP template deploys -----------------------------

sb_call() {
  # sb_call <tool-name> <json-args>
  local name="$1" args="${2:-{\}}"
  curl -sS -m 60 -X POST "$SB_URL" \
    -H "Authorization: Bearer $SB_TOKEN" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d "$(printf '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"%s","arguments":%s}}' \
          "$name" "$args")" \
    | sed 's/^data: //' | sed '/^event:/d' | sed '/^$/d'
}

service_exists() {
  sb_call list_services '{}' \
    | python3 -c "import json,sys; print(any(s.get('name')=='$1' for s in json.loads(json.load(sys.stdin)['result']['content'][0]['text'])))" \
    | grep -q True
}

render_and_deploy() {
  local template="$1"
  if service_exists "$template"; then
    log "Service $template already deployed; skipping (use ServiceBay UI to update)"
    return
  fi
  log "Rendering + deploying $template via ServiceBay-MCP"
  local rendered
  rendered="$(SB_TOKEN="$SB_TOKEN" python3 "$OSCAR_REPO_DIR/scripts/render-template.py" "$template")"
  python3 - <<PYEOF | sb_call deploy_service "$(cat -)"
import json, sys
payload = {
  "name": "$template",
  "kubeContent": """[Kube]
Yaml=$template.yml
AutoUpdate=registry

[Install]
WantedBy=default.target

[Service]
TimeoutStartSec=600
Restart=on-failure
RestartSec=5
RestartSteps=4
RestartMaxDelaySec=300

[Unit]
StartLimitIntervalSec=0
""",
  "yamlContent": $(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" <<< "$rendered"),
  "yamlFileName": "$template.yml"
}
print(json.dumps(payload))
PYEOF
}

deploy_templates() {
  if [[ -n "${SKIP_TEMPLATES:-}" ]]; then
    log "Skipping ServiceBay template deploys (SKIP_TEMPLATES set)"
    return
  fi
  render_and_deploy oscar-brain
  # oscar-voice and oscar-connectors are deployed independently — they
  # don't depend on oscar-brain being up first. Uncomment when their
  # variable contracts are ready:
  # render_and_deploy oscar-voice
  # render_and_deploy oscar-connectors
}

# ---- Step 3: Symlink skills into Hermes ----------------------------------

link_skills() {
  if [[ -n "${SKIP_SKILL_LINK:-}" ]]; then
    log "Skipping skill linking (SKIP_SKILL_LINK set)"
    return
  fi
  if [[ ! -d "$HERMES_HOME/skills" ]]; then
    warn "$HERMES_HOME/skills does not exist — Hermes might not be installed yet"
    return
  fi
  local target="$HERMES_HOME/skills/oscar"
  if [[ -L "$target" ]]; then
    log "$target already linked → $(readlink "$target")"
    return
  fi
  if [[ -e "$target" ]]; then
    warn "$target exists and is not a symlink — leaving alone"
    return
  fi
  log "Symlinking $OSCAR_REPO_DIR/skills → $target"
  ln -s "$OSCAR_REPO_DIR/skills" "$target"
}

# ---- Run -----------------------------------------------------------------

main() {
  log "OSCAR installer starting"
  log "  OSCAR_REPO_DIR = $OSCAR_REPO_DIR"
  log "  HERMES_HOME    = $HERMES_HOME"
  log "  SB_URL         = $SB_URL"
  install_hermes
  deploy_templates
  link_skills
  log "Done. Next:"
  log "  - hermes setup                     # configure model + messaging gateway"
  log "  - hermes mcp add <ha-mcp-url>      # connect HA-MCP"
  log "  - hermes mcp add <servicebay-mcp>  # connect ServiceBay-MCP"
  log "  - hermes                            # start chatting"
}

main "$@"
