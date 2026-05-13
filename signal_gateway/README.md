# signal-gateway

Bridges Signal ↔ HERMES inside the `oscar-brain` pod.

- **Inbound:** long-polls `signal-cli-rest-api` (already a pod sidecar), looks the sender number up in `gateway_identities`, POSTs to HERMES `/converse` with the resolved `uid` and `endpoint="signal:<num>"`, sends HERMES's reply back via signal-cli.
- **Outbound:** exposes `POST /send` (bearer-auth) so other OSCAR components can push DMs — used by the alarm/timer fire path (#34) and the upcoming skill-reviewer (#41) for post-merge notifications.

Two purposes in one service: one is the obvious chat-with-OSCAR-on-Signal use case; the other is **a hardware-free test path**. As long as Signal pairing works, you can drive the whole stack without Voice PE.

## Container

Built from this directory's `Dockerfile`. Runs as a sidecar in the `oscar-brain` pod; reaches the in-pod `signal-cli-rest-api` over localhost.

## Configuration

| Env | Purpose | Default |
|---|---|---|
| `SIGNAL_REST_URL` | URL of `signal-cli-rest-api` (the daemon container) | `http://127.0.0.1:8080` |
| `SIGNAL_ACCOUNT` | Linked Signal account (E.164 `+49…`) — must be paired (see `oscar-brain/README.md` → "Signal pairing (Phase 1)") | required |
| `HERMES_URL` | HERMES base URL | `http://127.0.0.1:8000` |
| `HERMES_TOKEN` | Bearer for HERMES | empty |
| `POSTGRES_DSN` | Connection to oscar-brain DB (for `gateway_identities` lookup) | required |
| `LISTEN_HOST` | aiohttp bind host | `0.0.0.0` |
| `LISTEN_PORT` | aiohttp bind port for `POST /send` | `8090` |
| `SIGNAL_TOKEN` | Bearer required on `POST /send` (empty disables auth — fine pod-internal) | empty |
| `POLL_INTERVAL_S` | Inbound poll cadence | `2` |

## Endpoints

### `POST /send`

```json
{ "to": "+4915112345678", "text": "Pizza ist fertig" }
```

Returns `{"ok": true}` on success, 4xx on bad request, 5xx if signal-cli rejected.

### `GET /health`

Liveness probe — also reports `signal-cli` reachability + paired-account status.

## How a message flows

```
Phone → Signal → signal-cli-rest-api → (poll) → signal-gateway
                                                  ↓
                                       gateway_identities lookup
                                                  ↓
                                       POST HERMES /converse
                                                  ↓
                                       reply text
                                                  ↓
                          signal-cli-rest-api ← (POST /v2/send) ← signal-gateway
                                  ↓
                              Signal → Phone
```

Unknown numbers get a fixed "Unbekannte Nummer — bitte erst mit `oscar-identity-link` verknüpfen." reply rather than reaching HERMES.
