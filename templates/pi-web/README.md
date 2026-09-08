# pi-web — the coding-agent web UI on the box

[PI WEB](https://pi-web.dev/) (`@jmfederico/pi-web`) is a browser surface for the
[Pi Coding Agent](https://github.com/earendil-works/pi/tree/main/packages/coding-agent):
agent sessions that keep running in real repositories on this box after the
browser disconnects. This template deploys it at `pi.<publicDomain>` behind
Authelia and wires it to the box's own model server.

## What it is made of

| | |
|---|---|
| Image | `ghcr.io/mdopp/solaris-pi-web:latest`, built from `pi-web/Dockerfile` in this repo |
| Containers | `sessiond` (owns the sessions, terminals and the model runtime) + `web` (HTTP/WebSocket) |
| Network | isolated netns, `hostPort` 8504 |
| Route | `pi.<publicDomain>`, internal exposure, Authelia forward-auth `one_factor` |
| Model | llama-server on this box, via the Pi agent's `models.json` |
| Volumes | `{{DATA_DIR}}/pi-web/data` → `/data`, `{{DATA_DIR}}/pi-web/workspace` → `/workspace` |

### Why the image is ours

Upstream publishes an **npm package only**. Its `docker/` directory is a
Dockerfile you are expected to build yourself, and no OCI image is pushed to
GHCR or Docker Hub. So this repo builds one, pinned to a specific
`@jmfederico/pi-web` version, in the same `build-images.yml` matrix as the
engine and the gatekeeper. Bumping PI WEB is a `PI_WEB_VERSION` change in
`pi-web/Dockerfile`, in its own commit — never a silent `latest` drift under a
running session.

### Why it is not on host networking

ADR 0007 Decision 2's carve-out list is **closed**; a new service does not join
it by arguing its case, and Decision 3 says explicitly that needing to reach a
loopback-bound sibling is not a reason either. So the pod runs in its own
network namespace, publishes 8504 as a `hostPort`, and addresses llama-server
as `http://host.containers.internal:11435/v1`. That path answers because the
sibling half already landed in #1344: llama-server binds `0.0.0.0` and
`LLAMA_PORT` carries `blockLanAccess: true`, so the LAN is refused at the host
firewall while loopback — where the pasta-proxied pod path arrives — is not.

`PI_WEB_PORT` carries the same `blockLanAccess: true` flag, for the same
reason one step further out: PI WEB has no login of its own (upstream states
plainly that it assumes trusted users and is not a sandbox), so a published
port reachable from the WLAN would be a way around Authelia. nginx reaches it
over loopback; a laptop on the WLAN does not.

The service never talks to `llama.<publicDomain>`. That route exists for a
human with a browser and is Authelia-gated; a service on the same box that used
it would meet a login page.

## How the model is configured

PI WEB has no LLM settings of its own — the model runtime belongs to the Pi
Coding Agent, and a self-hosted OpenAI-compatible endpoint is declared in the
agent directory's `models.json`. The post-deploy writes that file into
`{{DATA_DIR}}/pi-web/data/pi-agent/models.json`:

```json
{
  "providers": {
    "solaris-llama": {
      "baseUrl": "http://host.containers.internal:11435/v1",
      "api": "openai-completions",
      "apiKey": "llama",
      "compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": false },
      "models": [{ "id": "qwen3.8-27b" }, { "id": "gemma-4-e4b" }]
    }
  }
}
```

Three details that are not obvious:

- **`api: "openai-completions"`, not Pi's built-in `llama.cpp` provider.** That
  built-in speaks to llama.cpp's *router* mode, which discovers models in a
  directory and loads them on demand. This box runs llama-server in
  single-model mode (`-m <weights>`), where the router endpoints do not exist
  but `/v1` does.
- **`apiKey` is a placeholder.** llama-server ships no authentication and there
  is no key to hold; Pi hides models whose provider has no auth configured at
  all, so a dummy value is what makes them appear. Upstream's own Ollama
  example does the same.
- **Both aliases are listed.** llama-server serves one model at a time: the
  coding alias answers while the lease is held, the household alias otherwise.
  A list with only one of them would name a model that is absent for half the
  day.

## How the coding lease is held

The household model stays Gemma 4 E4B (#1318/#1325). Qwen 3.8 27B is what the
**coding lease** loads, and that lease is an endpoint of the Solaris Engine —
`POST /api/model-lease {"model": "coding", "ttl_s": …, "holder": "pi-web"}`
(contract mdopp/foundry-chronicle#321, holder semantics #1347).

That endpoint carries no token: it is loopback-only and being able to reach it
*is* the authorisation, so the engine binds `127.0.0.1` alone. An isolated pod
cannot call it — `host.containers.internal` maps to the host's LAN address,
where nothing listens. Widening the engine's bind to fix that would expose far
more than a lease, so **the lease is held on the host instead**: the post-deploy
copies itself to `{{DATA_DIR}}/pi-web/pi-web-lease.py` and installs a systemd
user unit

```ini
[Unit]
After=pi-web.service
BindsTo=pi-web.service
[Service]
Environment=DATA_DIR=…
ExecStart=… pi-web-lease.py hold
ExecStopPost=… pi-web-lease.py release
[Install]
WantedBy=pi-web.service
```

so systemd starts it with PI WEB and stops it with PI WEB. The `hold` loop
follows the contract's state machine:

| answer | what the unit does |
|---|---|
| `200 ready` | logs the alias, sleeps `renew_after`, POSTs again to renew |
| `202 preparing` | polls `GET` every `retry_after` until `ready` (15 min ceiling) |
| `409 held` | logs who holds it and carries on with whatever model is loaded; asks again in 5 min |
| unreachable | logs it, carries on, retries in a minute |

`ExecStopPost` sends `DELETE {"holder": "pi-web"}`, which releases only this
service's own window — a `409` there means somebody else's lease and is left
alone. The TTL (4 h by default, the engine's maximum) is the safety net for a
box that loses the unit, not the schedule.

## Why it does not start on its own

PI WEB is started by the operator when they want a coding session, and by
nobody else. That is not the default it would otherwise get: ServiceBay's
kube-write path emits `[Install] WantedBy=default.target` into every `.kube`
unit it renders and takes no annotation to say otherwise, so Quadlet links
`pi-web.service` into `default.target.wants` and the box brings it up after
every reboot. With it comes `pi-web-model-lease.service`, which takes the
coding lease — Qwen loaded, voice on the CPU, the household assistant slow for
up to four hours, for a session nobody started (#1373).

So the post-deploy strips that `[Install]` section back out of
`~/.config/containers/systemd/pi-web.kube` and reloads the generator. Setting
`PI_WEB_START_ON_BOOT` to `true` leaves the platform's section alone for an
operator who does want it up unattended.

The deploy itself has the same problem one step earlier: ServiceBay starts (or
restarts) the service before it runs the post-deploy, so a deploy would take
the lease on a box where PI WEB had been off for days. The pre-deploy run state
cannot be read at that point — the start has already happened — so the lease
unit's two hooks, which are exactly a PI WEB start and a PI WEB stop, append
`<epoch> running|stopped` to `{{DATA_DIR}}/pi-web/run-state.log`. The
post-deploy restores the newest entry written *before* the `.kube` file's
mtime, i.e. the last transition the operator caused; no entry at all means
nobody has had PI WEB up since this landed, and it is stopped. Two consequences
worth knowing:

- a fresh install ends **stopped** — start it from the ServiceBay UI or with
  `systemctl --user start pi-web`;
- because the on-disk `.kube` no longer matches what ServiceBay renders, its
  `specChanged` check is true on every deploy, so a running PI WEB is always
  restarted by a redeploy.

## Not in the `solarisbay` stack

The stack is the household assistant — the model server plus the Solaris
service. PI WEB is a developer tool that happens to live on the same box, like
`paperless`, so it installs on its own.

## Verifying on the box

- `https://pi.<publicDomain>/` unauthenticated → **302** to Authelia; after
  login the UI loads over WebSocket.
- The model picker lists the alias llama-server currently reports
  (`curl http://127.0.0.1:11435/v1/models`).
- `journalctl --user -u pi-web-model-lease` shows `coding lease held` with the
  alias, and `coding lease released` after `systemctl --user stop pi-web`.
- After a deploy, `systemctl --user is-enabled pi-web` is **disabled** and
  `grep Install ~/.config/containers/systemd/pi-web.kube` finds nothing — the
  reboot no longer takes the coding lease.
- From another LAN device, `curl -m 3 http://<box>:8504/` and
  `http://<box>:11435/v1/models` must both fail — the `blockLanAccess` rules.
