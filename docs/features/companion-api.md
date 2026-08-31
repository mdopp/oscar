# Solaris Companion API — the contract the `solaris-android` app talks to

This is the authoritative reference for the **companion-facing surface** of the
Solaris engine (`solaris-chat`): the `/napi/*` native API, device-token auth, and
the **live event stream (SSE)** the app holds open for notifications. The Android
companion app (repo **`mdopp/solaris-android`**) targets only this surface — it
never talks to ServiceBay or Home Assistant directly; Solaris aggregates those
behind `/napi`.

> **Architecture (binding):** Client = the app, Server = Solaris, **nothing in
> between** — no Google/FCM, no third-party distributor (ntfy/UnifiedPush), no
> proxy service. The native app receives everything (including background
> notifications) over **its own direct SSE connection to `/napi/portal/events`**
> (§3), kept alive by the app's own foreground service. **Web Push / VAPID (§4)
> is the browser-PWA path only — the native app does NOT use it.**

> **Source of truth is the code, not this file.** This doc is generated from
> `solaris-chat/src/solaris_chat/{server.py,engine/sb_companion.py,engine/sb_events.py,engine/notify.py,push_store.py,device_token_store.py,config.py,static/sw.js}`
> as of **v0.26.0**. If a detail here disagrees with the running engine, the code
> wins — file an issue. Paths are stable; response shapes may gain fields.

## Base URL, transport, auth surfaces

- **Host:** the Solaris engine (prod: `https://chat.dopp.cloud`; LAN IP works too). HTTPS required.
- **Two auth surfaces:**
  - **`/napi/*` — native, device-token only, proxy-bypassed (Authelia is skipped).**
    Every route is **fail-closed**: a missing/invalid `sol_device_` bearer ⇒ **401**.
    It never falls back to `default_uid` and never trusts the `Remote-User` header.
  - **`/api/*` — browser/PWA, Authelia-gated** (forward-auth `Remote-User`). The native
    app uses only `/pair-device` here (pairing needs an interactive login); everything
    else it needs is under `/napi`. The `/api/push/*` Web-Push routes are **browser-only** (§4).
- **No API version in the path.** Capability is signalled in responses (e.g.
  `vapid_public_key` in `whoami` ⇒ push available).

## 1. Device-token auth (how the app authenticates)

- **Token format:** `sol_device_<urlsafe-32-bytes>` — plaintext shown **once** at pairing.
  Stored server-side only as a SHA-256 digest; compared constant-time; per-resident (`owner_uid`).
- **Pairing (mint a token):** `GET/POST /pair-device` — **Authelia-gated** (interactive login).
  `POST /pair-device` (form field `label`) mints a token and **302-redirects to
  `<android_package>://pair#token=<plaintext>&id=<id>`** — the token rides the URL
  **fragment** so it never hits server logs. The app captures it from the deep link.
- **Use:** send `Authorization: Bearer sol_device_...` on every `/napi/*` call.
- **Manage devices (device-token authed):**
  - `GET /napi/device-tokens` → `{ok, tokens:[{id,label,created,last_used}]}` (metadata only)
  - `DELETE /napi/device-tokens/{id}` → `{ok}` (owner-checked; **404** if not yours)

## 2. `/napi/*` endpoints

All require `Authorization: Bearer sol_device_...`. Common errors: **401** (no/bad token),
**502** (upstream HA/SB unavailable), **503** (upstream unconfigured).

### Home / portal (Home-Assistant-backed)
| Method | Path | Returns |
|---|---|---|
| GET | `/napi/whoami` | `{ok,uid,is_admin,version,logout_url,context_window,household_session_id,wartung_session_id,vapid_public_key}` |
| GET | `/napi/portal/start` | `{ok,personal:[…],household:[…],ha}` — pinned favorites enriched with live HA state |
| GET | `/napi/portal/start/addable` | `{ok,rooms:[{room,cards:[…]}],automations:[…]}` — picker of addable actuators |
| GET | `/napi/portal/active` | `{ok,active:[{entity_id,name,room,domain,state}]}` — currently on/open |
| GET | `/napi/portal/cameras` | `{ok,cameras:[{entity_id,name,room}]}` |
| GET | `/napi/portal/state?entity_id=<id>` | `{ok,card:{…}}` — one entity's card spec (**400** on bad id) |
| GET | `/napi/portal/energy` | `{ok,energy:{…}}` |
| GET | `/napi/portal/entity-history?entity_id=<id>&range=<24h\|48h\|7d>` | `{ok,history:[…]}` (**400** on bad id/range) |
| GET | `/napi/portal/camera/{entity_id}/snapshot` | raw image bytes (**400** if not a camera) |
| POST | `/napi/portal/watch` | body `{entity_ids:[…]}` → `{ok}` — set this device's widget watch-set (**503** if unavailable) |

### HA actions
| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/napi/ha/call` | `{entity_id,service,data?,confirmed?}` | domains `light\|switch\|cover\|climate\|lock` (#1212). Sensitive targets — any `lock`, and garage/door/gate cover opens — need `confirmed:true` (**403** `sensitive_action` otherwise). **400** bad domain. A notification action may name a **narrower** set (§3): never `lock`, and it carries `confirm` saying whether to ask first. |

### ServiceBay BFF (Solaris aggregates ServiceBay — ADR 0010 / #811)
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/napi/servicebay/{key}` | — | SB JSON verbatim. `key ∈ {home,approvals,services,upgrades}` (**404** unknown key) |
| POST | `/napi/servicebay/services/{name}/operate` | `{action:"start"\|"stop"\|"restart"}` | `{ok,name,action}` (**400** bad action) |

> Upgrade-apply is intentionally **not** on `/napi` (it needs a mutate-scoped SB token =
> standing broad credential, which Solaris does not hold). Show "upgrade available"
> from `/napi/servicebay/upgrades`; the admin applies it in the ServiceBay web UI.

### Uploads (camera / documents)
| Method | Path | Content-Type | Limits | Returns |
|---|---|---|---|---|
| POST | `/napi/upload` | `multipart/form-data` (`file` repeatable, opt. `filename`,`kind`) | JPEG/PDF only (**415** else), ≤25 MB (**413**), ≤20 files (**400**) | one: `{ok,id,url}` · many: `{ok,files:[{ok,id,url}]}` |

Stored in the notes vault (`users/<uid>/uploads/`, household → shared `uploads/`) as a
companion note with an Obsidian embed — so uploads are also searchable via notes_search.

## 3. Live events & notifications — the SSE stream (`/napi/portal/events`) — **the native app's only push channel**

This is how the native companion receives everything, foreground **and** background.
The app's own **foreground service** (Android) holds this one connection open directly
to Solaris — no Google, no third-party distributor. On Android, an app can only be
woken while backgrounded by (1) FCM [forbidden], (2) a foreign distributor [forbidden],
or (3) its own running service — so **(3) is the design-conformant answer**, and its
unavoidable price is a discreet persistent notification + a battery exception. There is
no "process fully dead + push + no third-party" option on Android; this is it.

- `GET /napi/portal/events` — device-token authed, `Content-Type: text/event-stream`,
  long-lived (socket-connect timeout 20s, **no** read timeout; reconnect on drop).
- **Keepalive (#1093)** — while idle the server writes an SSE comment `: ping` every
  **90s** (a comment, not an event: parsers drop it). It exists to keep the proxy and
  any carrier NAT from expiring the binding, and it is the client's only liveness
  signal — a NAT drop kills the stream *silently*, so a client that waits on a socket
  with no read timeout waits forever. A client that wants to notice should treat
  **>3 min of total silence** (two missed pings) as a dead stream and reconnect.
- One multiplexed stream per resident (`uid`), in-process pub/sub (`EventBus`), per-resident privacy.
- **Event kinds** (`event: <kind>` + `data: <json>`):
  - `card_state` — an HA entity changed (fan-out from the HA watcher) → update the card / **wake the device widget**.
  - `chat` — a backgrounded/finished chat turn or a server-injected card.
  - `servicebay` — a ServiceBay **approval** event, republished from SB's SSE:
    `data:{id,kind,summary}` → show an **approval notification**. (Verdict flow below.)
  - `ha` — a **notification** for this resident (#1276, #1280; app side
    mdopp/solaris-android#116):
    `data:{kind:"ha", target, title, body, urgency, actions, category}`
    → show a notification on the channel `category` names. `urgency ∈ {low,normal,high}`
    is **presentation only**. `actions` is `[{entity_id,service,title,confirm}]` (≤3), meant
    to be mapped onto the app's existing `WidgetActionActivity` path — confirmation dialog +
    the server-side `sensitive_action` 403 gate — not a second action route.

    **`actions` (#1283, contract change — the shape is new)** — an action is a service
    call written out in **separate fields**, never one `domain.suffix` string:

    | key | what it is | example |
    |---|---|---|
    | `entity_id` | the entity the call targets (`domain.object_id`) | `cover.kueche_fenster` |
    | `service` | the service, **dotted**, its domain equal to the entity's | `cover.close_cover` |
    | `title` | the button label the resident reads | `Schließen` |
    | `confirm` | **boolean** — must the receiver ask before running it? | `false` |

    `entity_id`, `service` and `title` are required of the sender; `confirm` is computed by
    the server and is **always present on the wire**. The pair is exactly the `/napi/ha/call`
    body: the app posts `{entity_id, service}` **verbatim** (plus `confirmed:true` after its
    dialog when the server 403s `sensitive_action`) and derives nothing from the shape of a
    string.

    ```json
    {"kind":"ha","target":"anna","title":"Fenster offen","body":"Küche",
     "urgency":"normal","category":"house",
     "actions":[{"entity_id":"cover.kueche_fenster","service":"cover.close_cover",
                 "title":"Schließen","confirm":false}]}
    ```

    ```json
    {"kind":"ha","target":"anna","title":"Garagentor offen","body":"seit 20 Minuten",
     "urgency":"normal","category":"house",
     "actions":[{"entity_id":"cover.garagentor","service":"cover.close_cover",
                 "title":"Schließen","confirm":true}]}
    ```

    **`confirm` — the receiver is told, never left to guess.** In HA, `cover` is two
    unrelated things wearing one domain: shutters, blinds, awnings and curtains (harmless)
    and garage doors, gates, entrance doors and window openers (not harmless). The domain
    alone cannot tell them apart and the entity's *name* is not evidence, so the server
    resolves the entity's HA `device_class` and states the answer in the payload:
    `garage`/`door`/`gate`/`window` → `true`, an ordinary shutter or blind → `false`,
    `light`/`switch`/`climate` → `false`.

    It **fails closed**: a cover whose `device_class` cannot be resolved — HA unreachable,
    entity unknown, no class set — is sent as `true`. A sender may include `confirm:true`
    to raise it (`400 {reason:"invalid_actions"}` if it is not a boolean); a sender can
    never *lower* it — `confirm:false` is ignored wherever the server derived `true`.

    > **Receiver rule: a missing `confirm` MUST be read as `true`.** Do not default it to
    > `false` and do not infer it from the entity id. An older server that predates this
    > field would otherwise silently produce a one-tap garage door.

    *Why two fields.* Until #1283 an action was a single string, and `lock.front_door`
    (an entity) and `cover.close` (a service) are syntactically identical — the engine's
    own tests carried one of each. A receiver had to guess, and guessing wrong turns
    "show me the front door" into "switch the front door" from a notification sitting on
    the lock screen. The old one-string form is now **refused** (`400
    {reason:"invalid_actions"}`), not reinterpreted; the notice itself still delivers only
    if the whole payload is valid, so an automation still posting the old shape gets a
    400 rather than a silent guess.

    **Actionable domains — closed set:** `light`, `switch`, `cover`, `climate`. Anything
    else is refused with **400** `{reason:"forbidden_action_domain"}`.
    **`lock` and `alarm_control_panel` are never actionable from a notification**, and are
    not to be added. `/napi/ha/call` does accept `lock` — a lock *card* in the app can
    unlock, because the confirm gate is in that conversation — but a notification is
    reachable from the lock screen, outside it. So a notice cannot name a lock at all:
    an unlock is **unrepresentable** in this payload, the same structural choice as the
    deliberately missing `alarm` category below. A `cover` on a garage/door/gate still
    hits the server-side `sensitive_action` 403 and needs the app's confirmation — and
    now says so up front in `confirm`, so the app can show its dialog without waiting for
    the 403. A receiver may of course render **fewer** actions than the contract allows.

    **`category` (#1280, additive — new since v0.46.0)** — `"house" | "timer" | "reminder"`:

    | value | producer | what it is |
    |---|---|---|
    | `house` | `POST /api/ha/notify` | a household notice an HA automation posted |
    | `timer` | the timer scheduler | a fired timer **or** a fired Wecker (`kind:"alarm"`) |
    | `reminder` | the timer scheduler | a fired reminder |

    Give each category its **own** notification channel: they must stay separately
    mutable — someone who mutes the house's notices must not thereby lose their timers.
    There is deliberately **no** `alarm` category; a Wecker arrives as `timer`, because a
    category by that name on a best-effort stream reads like a promise this stream does
    not make.

    `category` is **optional and additive**: a v0.46.0-shaped event or POST body without
    it is still valid and means `house`, so a client written against v0.46.0 keeps
    working. Treat an unknown future value as `house` rather than dropping the notice.
    > **Not an alarm channel — for every category, timers and reminders included.** This
    > rides the same best-effort stream as everything else here: immediate while
    > connected, and otherwise only as good as the catch-up below — recoverable for a
    > few hours if the client asks, gone after that. It is for "Waschmaschine fertig" / "Post da"
    > / "Fenster offen" — never smoke, intrusion, or anything that has to wake somebody.
    > No `urgency` value and no `category` changes that. A Solaris timer or Wecker is
    > rung by the **speaker announcement** on the Voice PE satellite, which stays its
    > primary path; the `ha` event is the copy for a phone out of earshot.

### Catching up on what was missed — `GET /napi/notifications` (#1284)

The stream has **no backlog**: an `ha` event exists only for whoever is subscribed at
the instant it is published. A screen-off wake that listens for a few seconds therefore
used to catch a notice only by luck — everything else was **lost**, not delayed. This
endpoint is the other half: ask it once on wake, the way the fetch pass already asks for
approvals and updates.

| Method | Path | Query | Returns |
|---|---|---|---|
| GET | `/napi/notifications` | `since=<ts>` (optional) | `{ok, notifications:[…], now, retention_hours, delivery}` · **400** `{reason:"invalid_since"}` · **401** without a device token |

- **Each item is the `ha` event exactly as it went out on the stream** — same
  `{kind,target,title,body,urgency,actions,category}` — plus `id` (opaque, monotonic)
  and `ts` (`2026-08-30T01:02:03.123Z`, UTC). Feed it to the same notification code the
  SSE `ha` event feeds; nothing new to parse.
- **`since`** is the `ts` of the last notice the client handled; strictly newer ones come
  back, **oldest first**. Omit it to get the whole window. An unparsable value is a
  **400** rather than a silent full replay.
- **`now`** is the server clock in the same form, and it is **the next `since`** — always,
  not only when the list comes back empty. A client that advances the cursor with its own
  clock instead drifts against the server's and re-opens the very gap this endpoint closes.
- **Scope** is the same two streams the SSE serves — the caller's own uid and the shared
  household one. A device token authenticates; it never sees another resident's notices.
- **Retention: `retention_hours` = 6 hours, and the field is authoritative** — read it,
  don't hard-code it. The backlog is pruned on every write, by age *and* by a per-stream
  row cap, so it cannot grow without bound. These payloads name residents and describe
  what is happening in their home: this is a catch-up window, deliberately not a message
  archive, and there is no way to ask for more history.

> **This still promises nothing.** A notice is missed if the client never asks, asks
> later than the window, or the burst blew past the cap. The channel stays best effort —
> what changed is only that a screen-off gap of a few hours is now *survivable*, where
> before it was fatal. Nothing here retries, escalates or rings.

### Where an `ha` notice comes from (#1276, #1280)

Two producers, one event kind:

- **`POST /api/ha/notify`** — an HA automation (`category:"house"`). Automations no
  longer name a phone (`notify.mobile_app_<device>`); they name a **person**, and
  Solaris resolves that to their paired devices — so pairing a new phone changes no
  automation.
- **the timer scheduler** — a fired timer, Wecker or reminder (`category:"timer"` /
  `"reminder"`), published straight onto the owner's stream. Server-side only: there is
  no endpoint to post one, and no HTTP body to write.

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/ha/notify` | `{target, title, body?, urgency?, actions?, category?}` (closed key set; `actions` is `[{entity_id,service,title,confirm?}]` — `confirm` is server-computed and echoed on the wire) | **202** `{ok,target,residents,delivery}` · **400** bad payload, incl. `{reason:"invalid_category"}`, `{reason:"invalid_actions"}`, `{reason:"forbidden_action_domain"}` · **403** not a loopback neighbour · **404** `{reason:"unknown_target"}` |

- **Auth = reachability**, the model lease's pattern (#1260): peer-bound to the host
  loopback, and refused when an `X-Forwarded-For`/`X-Real-IP` header shows the call came
  through the proxy. HA is a hostNetwork pod on the same box, so it just posts to
  `http://127.0.0.1:<CHAT_PORT>/api/ha/notify`. Deliberately **not** under `/napi/`,
  which is Authelia-bypassed and device-token-only/fail-closed.
- **`target`** is a resident **uid** (as in `/napi/whoami`) or the household group
  (`DEFAULT_UID`, normally `household`). A resident is addressable once they have a
  paired device — a device token or a Web Push subscription. Matching is exact,
  case-insensitive, and nothing else: **an unknown or misspelled target delivers to
  nobody** (404) rather than falling back to the whole house.
- **Fail-open for HA:** the response is written before any push leaves the box, so an
  unreachable phone can neither fail nor delay the automation.

#### `notify.solaris` — what an automation actually calls (#1314)

**Nobody edits `configuration.yaml` for this.** The Solaris post-deploy writes the
`rest` notify platform entry itself on every deploy, so after an install (or a rebuild
of the box) `notify.solaris` is simply there in HA's action picker:

```yaml
action: notify.solaris
data:
  target: martin          # a resident uid, or `household`
  title: Waschmaschine
  message: Der Waschgang ist fertig.
  data:
    category: reminder    # house (default) | timer | reminder
    urgency: low          # low | normal (default) | high
```

- `message` → the endpoint's `body`, `title` → `title`, `target` → `target`. The
  endpoint's field names and the platform's are the same three, so neither side needed
  changing.
- `category` and `urgency` ride the platform's `data_template`, not a second
  `rest_command`: HA's legacy notify always hands the platform the caller's `data:`, and
  the rest platform merges each rendered `data_template` value **flat** into the JSON
  body — the shape the endpoint's closed key set requires. Omit them and the endpoint's
  own defaults (`house` / `normal`) are sent.
- **`target` is required.** The `rest` platform only sends it when the call names one,
  and the endpoint refuses a notice it cannot address (400 `invalid_target`) rather than
  broadcasting it to the whole house — the same fail-closed rule as an unknown target.
- `actions` are **not** reachable through `notify.solaris`; the platform has no place to
  put a list of them. An automation that needs action buttons posts to the endpoint
  directly with a `rest_command` (below).
- The post-deploy **merges, never clobbers**: an existing `notify:` list gets our entry
  appended inside it, and a `notify:` it cannot read as a block sequence (an `!include`,
  a mapping) is left completely alone — the feature then does not install, which is the
  recoverable failure. The write is idempotent, is validated through HA's
  `/api/config/core/check_config` before anything restarts, and is reverted if HA does
  not confirm it. A first write restarts HA, because a YAML notify platform only comes
  into being at setup; a converged box writes nothing and restarts nothing.

The direct form, for a payload the notify platform cannot express (`actions`):

```yaml
# configuration.yaml
rest_command:
  solaris_notify:
    url: "http://127.0.0.1:8787/api/ha/notify"   # CHAT_PORT
    method: POST
    content_type: "application/json"
    payload: '{"target": "{{ target }}", "title": "{{ title }}", "body": "{{ body }}"}'
```

**Approval verdict** is *not* on `/napi`: the verdict needs the Authelia session
(servicebay#2249), which a device token cannot supply. So the app hands off to the web.

**Deep-link target (#1085)** — open this in a Custom Tab (it carries the `dopp.cloud`
Authelia cookies):

```
https://<chat-host>/#/p/servicebay/approvals/<id>
```

The page shows what is being requested and offers **Genehmigen / Ablehnen**. Approving
asks for confirmation first; rejecting does not. It reads

| Method | Path | Returns |
|---|---|---|
| GET | `/api/servicebay/approvals/{id}` | `{ok,approval:{id,service,title,description,node,created_at}}` · **403** non-admin · **404** `{reason:"gone"}` already decided / unknown · **502** SB unreachable · **503** SB unconfigured |
| POST | `/api/servicebay/approvals/{id}/{approve\|reject}` | `{ok,approval_id,detail}` — mints an ephemeral delegated-admin assertion from the live session |

Both are Authelia-gated **and** admin-gated. The GET deliberately omits SB's
`payload`/`on_approve` (token-request ids, tool args) — the page renders none of it.

> Note the hash form. The server's bookmarkable `/p/{type}` route matches a **single**
> path segment, so `/p/servicebay/approvals/<id>` would 404; `#/p/…` is handled by the
> client router, which takes the whole rest of the path. Same for `#/p/device/<entity>`.

## 4. Web Push (VAPID) — **browser PWA only, NOT the native app**

> **The native Android companion does NOT use Web Push.** Web Push on Android means
> FCM or a third-party distributor (UnifiedPush/ntfy), both of which the architecture
> forbids. The native app gets background notifications via its own foreground service
> on the §3 SSE stream. This section is only for the **browser PWA** (installed web app).

- **VAPID public key:** `vapid_public_key` in `/api/whoami` (also present in `/napi/whoami`).
  The browser uses it as `applicationServerKey` when subscribing.
- **Subscribe / unsubscribe** (browser, Authelia-gated): `POST /api/push/subscribe`
  body `{endpoint,keys:{p256dh,auth}}` → `{ok}`; `POST /api/push/unsubscribe` `{endpoint}` → `{ok}`.
- **Selective push:** the server sends a Web Push only when no SSE subscriber is open
  for that uid (backgrounded). Payload `{title, body, data:{kind:"chat"|"reminder"|"card_state"|"servicebay"|"ha", …}}`;
  service worker `/sw.js` shows it and deep-links on click.
- **Unchanged by #1280.** Carrying timers and reminders on the `ha` event kind was
  **additive**: the browser PWA still gets its fired timer as the same Web Push it always
  did (`data:{kind:"timer"|"alarm"|"reminder", timer_id}`), and nothing here is removed
  while anyone still has the PWA installed.

> Note: device-token `/napi/push/subscribe` twins were briefly added in v0.26.0 for a
> UnifiedPush experiment that the architecture has since rejected (native app uses SSE,
> not Web Push). They were **not** part of the native contract and have since been
> **removed** — there is no `/napi/push/*`.

## 5. Android app-domain binding (TWA)

- `GET /api/.well-known/assetlinks.json` serves Digital Asset Links for the package
  `ANDROID_PACKAGE` (default `cloud.dopp.solaris`) using `ANDROID_CERT_FINGERPRINTS`
  (set at signing). This removes the URL bar in a Trusted Web Activity and binds app↔domain.

## What the companion does **not** touch

- **ServiceBay / Home Assistant directly** — always via `/napi`.
- **The durable SB read token / `SB_READ_TOKEN`** — server-internal only (powers the
  approval SSE bridge + update poller); the companion is unaffected and only ever uses
  its `sol_device_` token. (See the engine's `reference_event_driven_read_token`.)
