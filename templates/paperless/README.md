# Paperless (Document Store)

[paperless-ngx](https://docs.paperless-ngx.com/) is a document management
system: a Web-UI, a REST API, a consume-folder watcher, and Postgres-backed
full-text search. ServiceBay's `paperless` template wraps the upstream image
(`ghcr.io/paperless-ngx/paperless-ngx:beta`) plus its `redis` broker and
`postgres` store as one hostNetwork pod bound to `127.0.0.1`, fronted by NPM +
Authelia forward-auth.

## What this template is (and is NOT)

Paperless here is a document **store + Web-UI + full-text search ONLY**. Its own
Tesseract OCR is **disabled** (`PAPERLESS_OCR_MODE=skip`): the #929 PoC proved
paperless Tesseract garbles rotated German scans even with `deu+eng` + deskew +
300 dpi (root cause = page rotation). Solaris's `gemma4:12b` vision extractor
stays the fact + text source.

The text handoff — ingest a doc OCR-skipped, then
`PATCH /api/documents/{id}/ {"content": <gemma vision text>}` so paperless
re-indexes clean text into full-text search — belongs to the **downstream #931
PaperlessIngest adapter**, not this template. This template only **exposes** what
#931 needs:

- the REST API on the host loopback at `http://127.0.0.1:{{PAPERLESS_PORT}}`
  (same netns as the solaris pod), and
- a `consume/` drop path under the file-share share
  (`{{DATA_DIR}}/file-share/data/paperless-consume`).

#931 authenticates to the API with a paperless API token (mint one under the
SSO-provisioned user in the Web-UI), reaching the API on the loopback so it
bypasses forward-auth.

## Pod

Three containers in one hostNetwork pod:

- `webserver` — paperless-ngx (Web-UI + REST API + consume watcher + search).
- `redis` — Celery task broker, loopback-only.
- `postgres` — metadata + the full-text search index, loopback-only.

Resource budget observed in the PoC: ~0.8 GB RAM idle + ~2.5 GB image disk.

## SSO

NPM proxies `paperless.<publicDomain>` behind Authelia forward-auth (the same
pattern as `ollama` and the chat surface). Authelia forwards the authenticated
identity as `Remote-User`; paperless trusts it via
`PAPERLESS_ENABLE_HTTP_REMOTE_USER=true` /
`PAPERLESS_HTTP_REMOTE_USER_HEADER_NAME=HTTP_REMOTE_USER` and auto-provisions +
logs the user in, so there is no second login. Never expose paperless directly
on the LAN — the webserver binds `127.0.0.1` so every request goes through
forward-auth.

## Variables

- `PAPERLESS_PORT` — host loopback port for the Web-UI + REST API. Default
  `8000`. NPM proxies the public subdomain down to it; #931 reaches the API on
  it. NB: the `servicebay.healthcheck` annotation uses the literal `8000` —
  change it there too if you change this.
- `PAPERLESS_REDIS_PORT` — loopback port for the bundled redis broker. Default
  `6389`.
- `PAPERLESS_DB_PORT` — loopback port for the bundled postgres. Default `5442`
  (offset from stock 5432 to avoid colliding with another postgres on the box).
- `PAPERLESS_DB_PASSWORD` — postgres `paperless` user password, shared between
  the postgres + webserver containers. Loopback-only, so an intra-pod boundary;
  change the default on a shared box.
- `PAPERLESS_OCR_LANGUAGE` — Tesseract lang hint, default `deu+eng`. With OCR
  **skipped**, this only affects date parsing + the UI locale, not OCR.
- `PAPERLESS_UID` / `PAPERLESS_GID` — host uid/gid paperless runs + owns its
  files as. Default `1000/1000`, matching the file-share Syncthing share owner
  so scans dropped into the shared consume dir are readable.
- `PAPERLESS_SUBDOMAIN` — subdomain for the Web-UI + API. Default `paperless`.
  Internal exposure: cert + LAN-only access list + Authelia forward-auth.

## Volumes

Under `{{DATA_DIR}}`:

- `paperless/redis`, `paperless/pgdata` — broker + database state.
- `paperless/media` — archived document blobs (originals + thumbnails).
- `paperless/data` — search index, classifier model, app data.
- `file-share/data/paperless-consume` — the watched drop dir (auto-ingest); the
  path #931 writes OCR-skipped docs into.

## Verify

Deployed through ServiceBay onto the box:

- pod healthy (all three containers up; `servicebay.healthcheck` green on
  `/api/`);
- `https://paperless.<publicDomain>` loads the Web-UI behind Authelia (single
  login, auto-provisioned user);
- the REST API answers on the host loopback
  (`curl http://127.0.0.1:8000/api/` → 200/JSON with a token);
- a file copied into the consume dir is auto-ingested;
- `PAPERLESS_OCR_MODE=skip` is in the running webserver env (no Tesseract
  re-OCR).
