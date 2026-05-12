# Gateway identities

> Status: draft, May 2026. Target phase: Phase 1 (first use with Signal). Home: a table in `oscar-brain.postgres`, written by a setup wizard, read by HERMES before every gateway conversation call.

## Purpose

HERMES gateways (Signal, Telegram, email, Discord, …) see incoming messages with the sender's **external identity** — e.g. `+4915112345678` for Signal, `123456789` for Telegram. For harness selection, memory namespacing, and skill permissions to work, HERMES needs to turn that into the **LLDAP uid**. This table is that mapping.

Voice recognition does **not** go through this table — voice embeddings are continuous and need their own distance-based resolution in `gatekeeper_voice_embeddings` ([oscar-architecture.md](../oscar-architecture.md)). Only exact-match channels live here.

## Data model

```sql
CREATE TABLE gateway_identities (
  gateway      TEXT NOT NULL,                  -- 'signal' | 'telegram' | 'email' | 'discord' | …
  external_id  TEXT NOT NULL,                  -- E.164 number, chat id, email address, Discord id
  uid          TEXT NOT NULL,                  -- LLDAP uid ('guest' is *not* stored here)
  display_name TEXT,                           -- optional, for logs/UI
  verified_at  TIMESTAMPTZ NOT NULL,
  created_by   TEXT NOT NULL,                  -- LLDAP uid of the admin who created the mapping
  PRIMARY KEY (gateway, external_id)
);
CREATE INDEX gateway_identities_uid_idx ON gateway_identities (uid);
```

**Multiple external ids per uid** are allowed (Michael has private + work Signal numbers): the primary key is `(gateway, external_id)`, not `uid`.

**Unknown external_id** means: no row, no match. HERMES then treats the caller as **guest harness**, denies sensitive tools by default, and answers with a verbal hint ("I don't know you yet — if you're in the family, ask Michael to register you.").

## Lookup path

```
gateway receives a message
  → HERMES looks up (gateway, external_id) → uid via gateway_identities
  → uid NULL? → uid = 'guest'
  → harness load = system.yaml ∪ ({uid}.yaml | guest.yaml)
  → conversation call with (text, uid, endpoint, …)
```

Fresh lookup on every call — no caching in gateway code. The table is small and the Postgres lookup is negligible compared to LLM inference time.

## Write path

**Phase 1:** no web UI. Entries are written via the HERMES skill `identity.link`, callable only from an admin harness (`groups: [admins]` in LLDAP):

```
Michael (Voice PE office, admin):
  "Link the Signal number +49 151 1234 5678 to user anna."
  → skill checks admin permission, writes the row, confirms verbally.
```

Verification in Phase 1 is **implicit and trust-based** — the admin speaks, the skill writes. No cross-channel confirmation (e.g. "send YES to this Signal account").

**Phase 2+:** once the gatekeeper grows a web UI for voice-embedding onboarding ([oscar-architecture.md](../oscar-architecture.md)), it gets a "Gateway links" tab — Authelia OIDC protected, editable there.

## Privacy

- Phone numbers and chat ids are PII. The table lives in `oscar-brain.postgres`, **not** in LLDAP — same reasoning as for voice embeddings ([oscar-architecture.md](../oscar-architecture.md)): biometric and contact-related data belong in an OSCAR-internal layer, not in the identity provider.
- No HMAC / hashing: lookup is exact-match, plain is fine. Postgres is encrypted at rest on Fedora-CoreOS LUKS volumes anyway.
- LLDAP uid deletion → cascade: when a uid is deleted in LLDAP, a hook (or a periodic reconciler in HERMES) must remove orphan `gateway_identities` rows. Variant 1 (hook) couples to ServiceBay; variant 2 (reconciler) is OSCAR-internal and therefore robust against ServiceBay upgrades. **Recommendation: reconciler, daily.**

## Open questions

1. **Cross-channel verification** before the Phase 2 UI: is it worth wiring a confirmation-code round-trip in Phase 1 ("I just sent you a six-digit code on Signal — say it out loud")? Upside: no trust break from accidental misregistration. Downside: complexity for a 4-person household. Recommendation: no, later.
2. **Guest accounts with their own identity:** if a known guest (e.g. a sister) regularly messages via Signal — do we want a dedicated `guest:sister` uid or do they stay `guest`? Depends on harness design; defer until Phase 2.
