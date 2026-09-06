#!/usr/bin/env bash
# Solaris box regression — run ON the box after every deploy.
#
# Covers the integration paths unit tests cannot see (born from the
# 2026-06-12 breakages, each check names the incident it would have
# caught). Read-only towards Home Assistant; never switches a device.
#
#   ssh box 'bash -s' < scripts/box-regression.sh
#
set -u
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ✅ $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  ❌ $1"; }

CHAT=http://127.0.0.1:8787
HA=http://127.0.0.1:8123
TOKEN=$(cat /mnt/data/stacks/home-assistant/homeassistant/.solaris-long-lived-token 2>/dev/null)
KEY=$(podman exec solaris-chat printenv SOLARIS_API_KEY 2>/dev/null)

echo "== solaris box regression $(date -u +%FT%TZ) =="

# 1. health
curl -sf -m 5 $CHAT/health >/dev/null && ok "chat /health" || bad "chat /health"

# 2. panel SSE stream — full consume, must end with `event: done` and no
#    error frame (incident: contextvar ValueError → 'Network error').
BODY=$(curl -s -m 90 -X POST -H "Remote-User: regression" -H "Content-Type: application/json" \
  -d '{"input": "Sag nur das Wort Test."}' $CHAT/api/chat/stream)
if echo "$BODY" | grep -q "event: done" && ! echo "$BODY" | grep -q '"reason": "engine_unavailable"'; then
  ok "panel SSE turn completes"
else
  bad "panel SSE turn (incident: Network error)"
fi

# 3. facade non-stream turn (gatekeeper/HA path) — the Ollama-PROTOCOL
#    surface on the engine, which outlived the ollama service (#1332).
R=$(curl -s -m 90 -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"solaris","stream":false,"messages":[{"role":"user","content":"Sag nur Hallo."}]}' \
  $CHAT/ollama/api/chat | python3 -c "import json,sys; print(json.load(sys.stdin)['message']['content'])" 2>/dev/null)
[ -n "$R" ] && ok "facade turn: $R" || bad "facade turn"

# 4. conversation.solaris tool truthfulness — the reported light states must
#    match HA's real states (incident: narration without tool calls).
SAID=$(curl -s -m 90 -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "Welche Lichter sind an?", "agent_id": "conversation.solaris", "language": "de"}' \
  $HA/api/conversation/process | python3 -c "import json,sys; print(json.load(sys.stdin)['response']['speech']['plain']['speech'])" 2>/dev/null)
REAL=$(curl -s -m 10 -H "Authorization: Bearer $TOKEN" $HA/api/states | python3 -c "
import json,sys
on=[ (s.get('attributes',{}).get('friendly_name') or s['entity_id'])
     for s in json.load(sys.stdin)
     if s['entity_id'].startswith('light.') and s['state']=='on']
print('|'.join(sorted(on)))")
if [ -z "$REAL" ]; then
  if echo "$SAID" | grep -qiE "kein|nicht"; then
    ok "state truthfulness (none on): $SAID"
  else
    bad "state truthfulness — HA: none on, Solaris: $SAID"
  fi
else
  MISS=0
  IFS='|'; for L in $REAL; do echo "$SAID" | grep -qiF "$L" || MISS=1; done; unset IFS
  if [ $MISS = 0 ]; then
    ok "state truthfulness: $SAID"
  else
    bad "state truthfulness — HA on: [$REAL] vs Solaris: $SAID"
  fi
fi

# 5. pipeline wiring — engine, voice and language must be a known-good combo
#    (incident: tts_voice 'kokoro' → every playback failed silently).
PIPE=$(python3 - <<'PYEOF'
import base64, json, secrets, socket, struct

class WS:
    def __init__(self, token, host="127.0.0.1", port=8123):
        self.s = socket.create_connection((host, port), timeout=15)
        self.s.settimeout(15)
        self.b = b""
        self.i = 1
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.s.sendall((
            "GET /api/websocket HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (host, key)).encode())
        r = b""
        while b"\r\n\r\n" not in r:
            r += self.s.recv(4096)
        self.b = r.split(b"\r\n\r\n", 1)[1]
        assert self.rj()["type"] == "auth_required"
        self.sj({"type": "auth", "access_token": token})
        assert self.rj()["type"] == "auth_ok"

    def sj(self, o):
        p = json.dumps(o).encode()
        m = secrets.token_bytes(4)
        L = len(p)
        h = struct.pack("!BB", 0x81, 0x80 | L) if L < 126 else struct.pack("!BBH", 0x81, 0xFE, L)
        self.s.sendall(h + m + bytes(b ^ m[i % 4] for i, b in enumerate(p)))

    def rx(self, n):
        while len(self.b) < n:
            self.b += self.s.recv(65536)
        o, self.b = self.b[:n], self.b[n:]
        return o

    def rj(self):
        msg = b""
        while True:
            b1, b2 = self.rx(2)
            op = b1 & 0x0F
            L = b2 & 0x7F
            if L == 126:
                (L,) = struct.unpack("!H", self.rx(2))
            elif L == 127:
                (L,) = struct.unpack("!Q", self.rx(8))
            pl = self.rx(L)
            if op == 0x9:
                self.s.sendall(struct.pack("!BB", 0x8A, 0x80) + b"\x00" * 4)
                continue
            if op == 0x8:
                raise ConnectionError("closed")
            msg += pl
            if b1 & 0x80:
                return json.loads(msg)

    def cmd(self, o):
        i = self.i
        self.i += 1
        self.sj({"id": i, **o})
        while True:
            m = self.rj()
            if m.get("id") == i and m.get("type") == "result":
                if not m.get("success"):
                    raise RuntimeError(m.get("error"))
                return m.get("result") or {}

token = open("/mnt/data/stacks/home-assistant/homeassistant/.solaris-long-lived-token").read().strip()
ws = WS(token)
solaris = [p for p in ws.cmd({"type": "assist_pipeline/pipeline/list"})["pipelines"] if p["name"] == "Solaris"][0]
print(solaris["tts_engine"], solaris["tts_language"], solaris["tts_voice"])
PYEOF
)
case "$PIPE" in
  "tts.openai_streaming de martin"|"tts.piper de_DE None") ok "pipeline tts: $PIPE" ;;
  *) bad "pipeline tts combo unknown-bad: $PIPE" ;;
esac

# 6. TTS synthesis + bridge health (incident: silent 'voice not supported').
if podman logs --since 2m voice-tts-bridge 2>&1 | grep -q "not supported"; then
  bad "bridge logged 'voice not supported'"
else
  ok "bridge log clean"
fi
curl -sf -m 30 -o /tmp/.regr-tts.wav -X POST http://127.0.0.1:8881/v1/audio/speech \
  -H "Content-Type: application/json" -d '{"input": "Regressionstest.", "voice": "martin"}' \
  && ok "martin synthesis" || bad "martin synthesis"

# 7. model servers up — the household model with its drafter, and the
#    embeddings instance (#1332; incident: a stopped embeddings server leaves
#    vault search silently on keyword hits).
curl -sf -m 10 http://127.0.0.1:11435/health >/dev/null \
  && ok "llama-server :11435" || bad "llama-server :11435"
if curl -sf -m 30 -X POST http://127.0.0.1:11436/v1/embeddings \
     -H "Content-Type: application/json" -d '{"input": "Regressionstest."}' \
     | grep -q '"embedding"'; then
  ok "embeddings :11436"
else
  bad "embeddings :11436 (semantic vault search is degraded)"
fi

# 8. STT wyoming reachable
timeout 3 bash -c "</dev/tcp/127.0.0.1/10300" 2>/dev/null && ok "whisper :10300" || bad "whisper :10300"

echo "== $PASS passed, $FAIL failed =="
[ $FAIL = 0 ]
