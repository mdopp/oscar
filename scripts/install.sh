#!/usr/bin/env bash
#
# OSCAR installer — deploys everything via ServiceBay-MCP.
#
# Sequence:
#   1. Ensure the OSCAR repo is cloned to a stable host path so the
#      ServiceBay templates can hostPath-mount skills/, shared/, etc.
#   2. Deploy oscar-brain   (Postgres + Qdrant + Ollama + db-migrate + pg-backup)
#   3. Deploy oscar-hermes  (Nous' official hermes-agent container — agent runtime)
#   4. Print next-step checklist for the interactive `hermes setup` wizard.
#
# Idempotent: services already deployed are skipped.
#
# Required env:
#   SB_URL    ServiceBay-MCP URL (e.g. http://192.168.178.100:5888/mcp)
#   SB_TOKEN  ServiceBay-MCP bearer token
#
# Optional env:
#   OSCAR_REGISTRY_DIR  Where the OSCAR repo is cloned on the host
#                       (default: /var/mnt/data/registries/oscar)
#   SKIP_BRAIN          Skip oscar-brain deploy
#   SKIP_HERMES         Skip oscar-hermes deploy
#

set -euo pipefail

: "${SB_URL:?SB_URL not set — e.g. http://192.168.178.100:5888/mcp}"
: "${SB_TOKEN:?SB_TOKEN not set}"

OSCAR_REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OSCAR_REGISTRY_DIR="${OSCAR_REGISTRY_DIR:-/var/mnt/data/registries/oscar}"

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- ServiceBay-MCP helpers ----------------------------------------------

sb_call() {
  # sb_call <tool-name> <json-args>
  curl -sS -m 60 -X POST "$SB_URL" \
    -H "Authorization: Bearer $SB_TOKEN" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d "$(printf '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"%s","arguments":%s}}' "$1" "${2:-{\}}")" \
    | sed 's/^data: //' | sed '/^event:/d' | sed '/^$/d'
}

service_exists() {
  sb_call list_services '{}' | python3 -c "
import json, sys
d = json.load(sys.stdin)
items = json.loads(d['result']['content'][0]['text'])
print(any(s.get('name') == '$1' for s in items))
" | grep -q True
}

# ---- Step 1: clone the OSCAR repo onto the host ---------------------------

ensure_registry_clone() {
  log "Ensuring OSCAR repo is cloned at $OSCAR_REGISTRY_DIR"
  local out
  out=$(sb_call exec_command "$(python3 -c "
import json; print(json.dumps({'command': '''
set -e
mkdir -p \$(dirname '$OSCAR_REGISTRY_DIR')
if [ ! -d '$OSCAR_REGISTRY_DIR/.git' ]; then
  git clone --depth 1 https://github.com/mdopp/oscar.git '$OSCAR_REGISTRY_DIR'
else
  git -C '$OSCAR_REGISTRY_DIR' fetch --depth 1 origin main && \\
  git -C '$OSCAR_REGISTRY_DIR' reset --hard origin/main
fi
echo \"ready: \$(git -C '$OSCAR_REGISTRY_DIR' rev-parse --short HEAD)\"
'''})")")
  log "  $(echo "$out" | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['content'][0]['text'])" | tail -1)"
}

# ---- Render + deploy helper -----------------------------------------------

render_and_deploy() {
  local template="$1"
  if [[ -n "${SKIP_BRAIN:-}" && "$template" == "oscar-brain" ]]; then
    log "Skipping oscar-brain (SKIP_BRAIN set)"; return
  fi
  if [[ -n "${SKIP_HERMES:-}" && "$template" == "oscar-hermes" ]]; then
    log "Skipping oscar-hermes (SKIP_HERMES set)"; return
  fi
  if service_exists "$template"; then
    log "$template already deployed; skipping (use ServiceBay UI/MCP to update)"
    return
  fi

  log "Rendering $template …"
  local rendered
  rendered=$(OSCAR_REGISTRY_DIR="$OSCAR_REGISTRY_DIR" python3 "$OSCAR_REPO_DIR/scripts/render-template.py" "$template")

  log "Deploying $template via ServiceBay-MCP …"
  python3 - <<PYEOF | sb_call deploy_service "$(cat -)" >/dev/null
import json
kube = """[Kube]
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
"""
payload = {
  "name": "$template",
  "kubeContent": kube,
  "yamlContent": $(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" <<<"$rendered"),
  "yamlFileName": "$template.yml",
}
print(json.dumps(payload))
PYEOF
  log "  $template deployed"
}

# ---- Run ------------------------------------------------------------------

main() {
  log "OSCAR installer starting"
  log "  OSCAR_REPO_DIR     = $OSCAR_REPO_DIR  (this checkout)"
  log "  OSCAR_REGISTRY_DIR = $OSCAR_REGISTRY_DIR  (host-side clone for hostPath mounts)"
  log "  SB_URL             = $SB_URL"

  ensure_registry_clone
  render_and_deploy oscar-brain
  render_and_deploy oscar-hermes

  log ""
  log "============================================================"
  log "Deploy phase done. Next steps (one-time, interactive):"
  log "============================================================"
  log ""
  log "1. Run Hermes' setup wizard (LLM provider + API keys):"
  log "   ssh <oscar-host>"
  log "   podman exec -it oscar-hermes-hermes hermes setup"
  log ""
  log "2. Connect MCP servers Hermes will use:"
  log "   podman exec -it oscar-hermes-hermes hermes mcp add http://localhost:8123/mcp_server/sse --token <ha-token>"
  log "   podman exec -it oscar-hermes-hermes hermes mcp add http://localhost:5888/mcp           --token <sb-mcp-token>"
  log ""
  log "3. Pair a messaging gateway (Signal / Telegram / Discord / …):"
  log "   podman exec -it oscar-hermes-hermes hermes gateway setup signal"
  log ""
  log "4. Restart the pod so gateway run picks up the new config:"
  log "   systemctl --user restart oscar-hermes.service"
  log ""
  log "5. Smoke test:"
  log "   podman exec -it oscar-hermes-hermes hermes"
  log "   > /skills           # should list both Hermes' Skills Hub + oscar-* entries"
  log ""
}

main "$@"
