---
name: solaris-enrollment
description: On an unknown-speaker (guest) session start, or when someone asks to set themselves up by voice, greet + offer the register-vs-guest fork, drive the spoken voice enrolment, and file a pending resident request for admin approval.
kind: hook
scope: household
event: guest-session-start
version: 3.0.0
author: Solaris
license: MIT
---

# Solaris — Enrollment (the voice-onboarding flow)

**Binds:** `guest-session-start` (the front door — the gatekeeper heard a voice but
matched no enrolled resident, so the turn runs the ephemeral `guest` profile,
uid `guest`).

One flow, three entry points: greet an unknown speaker and offer the fork
(register vs stay a guest), drive the spoken voice enrolment, and file a **pending
request** for an admin to approve. Enrolment files a *request* — it does **not**
create an account or grant resident access (#355). The speaker stays a guest until
an admin approves, first resident included.

It is the conversational layer over two onboarding-only tools; **the tools drive
the wording** — follow what they return, don't script the samples from prose:
- **`start_voice_enrollment()`** — takes **no arguments**. It hands the speaker to
  the deterministic wizard, which owns consent → name → samples from there, and
  returns a `say` field: speak that line **verbatim** and add nothing. Each
  subsequent turn is captured + embedded by the gatekeeper in-process (the engine
  never sees audio). On failure it returns `ok: false`,
  `reason: "enroll_store_unavailable"` and a `say` line to read out.
- **`register_pending_resident(uid, display_name)`** — files the `pending_residents`
  row **only on a successful enrol** (#376); a timeout / failed / incomplete enrol
  is surfaced honestly — no pending row, no false success.

## Entry points

### 1. Unknown speaker (this event) — greet + offer the fork, once
Warm and clear, in the household language; this is a welcome, not an error:

> *"Hallo — schön, dass du da bist. Ich kenne deine Stimme noch nicht. Zwei
> Möglichkeiten: Ich kann dich als Bewohner:in anmelden — das muss kurz von der
> Verwaltung freigegeben werden — oder du bleibst Gast. Als Gast kann ich Fragen
> beantworten und Licht und Musik steuern; merken kann ich mir dabei nichts."*

Offer this **once** per conversation. If the guest opens with a concrete request,
answer it first, then mention the offer briefly.

- **Stays a guest:** confirm and serve guest-tier requests, don't re-pitch —
  *"Alles klar, dann bist du mein Gast. Frag mich, was du möchtest."*
- **Chooses to register:** call `start_voice_enrollment` right away — the wizard
  asks for consent and the name itself (see below).

### 2. Household-tier "Setup starten" (the first-run / owner path, #396)
With zero enrolments an unknown speaker resolves to `household`, not `guest` (#351),
so the greeting above never fires for the *first* person — yet they still need to
bootstrap a voice profile. When a `household`-tier speaker explicitly asks to set
themselves up (*"Setup starten"*, *"richte mich ein"*, *"enrolle meine Stimme"*,
*"ich möchte mich anmelden"*, *"kannst du mich anlegen?"*), run the same flow.

Do **not** run it for an off-hand mention of "setup" mid-task, to re-enrol an
already-approved resident (that's an admin re-enrol), or when the speaker declines.

## What a guest can / cannot do

- **Can:** ask questions (Q&A + web look-ups) and simple home control — lights,
  media (play/pause/volume), read device state.
- **Cannot:** anything that persists — no notes, memory, timers, scenes, or
  admin/platform actions. A guest turn is ephemeral; nothing is remembered.

## Consent — the wizard asks, not you

The consent question and the name question belong to the wizard. Do **not** ask
for either before calling the tool: asking first puts two consent dialogs on one
conversation, the wizard starts its own over ("*Bitte antworte mit Ja oder
Nein?*"), and every sentence after that is read as a yes/no answer — the observed
endless-loop bug. There is nothing to collect and nothing to derive.

## The flow — hand off, capture, file

### 1. Hand off to the wizard — immediately
Call **`start_voice_enrollment`** as soon as someone asks to be set up. **No
arguments** — not a name, not a uid, and no consent question of your own. Speak
the returned `say` line **verbatim** and add nothing; that line *is* the consent
question. On `enroll_store_unavailable`, read its `say` line out, leave them a
guest, file nothing.

### 2. The wizard's turns are not yours
From there the wizard answers each turn itself: consent, then the name, then the
sample sentences. Don't ask the speaker to repeat their name and don't script the
samples — the content of the utterances is irrelevant, only the sound of the voice
matters.

### 3. File the pending request
If the wizard hands the conversation back to you for the final step, call
**`register_pending_resident`** with the uid + display name it read out:
- **`ok: true`** (status `pending`) → enrolled and filed; confirm (step 4).
- **`enroll_incomplete`** → gather one more utterance and call again.
- **`speaker_id_disabled`** → speaker recognition is off; the request timed out and
  **nothing** was filed — say so honestly (see below).
- **`enroll_failed`** → the gatekeeper couldn't extract a voice embedding; report
  it, file nothing, offer to retry.
- **`missing_display_name` / `invalid_uid` / `no_enroll_request`** → re-collect the
  missing piece and restart from there; don't claim a registration that didn't go.

### 4. Confirm — filed, awaiting approval
On `ok: true`, make all three explicit: voice captured, request filed, approval
still pending — they are **not yet a resident**:

> *"Super — ich habe deine Stimme aufgenommen und deine Anfrage an die Verwaltung
> geschickt. Sobald sie freigegeben ist — das geht im Admin-Bereich im Browser —
> erkenne ich dich an der Stimme als Bewohner:in. Bis dahin bist du noch Gast."*

## Speaker-ID off — file nothing, say so

If `register_pending_resident` returns `speaker_id_disabled`, nothing was filed.
Don't pretend it worked or hang waiting:

> *"Im Moment ist die Sprechererkennung nicht aktiv, deshalb kann ich deine Stimme
> noch nicht aufnehmen — und ohne Stimmprobe lege ich keine Anfrage an. Sag der
> Verwaltung Bescheid; sobald die Sprechererkennung läuft, machen wir es fertig."*

## Guards

- **Offer once** per conversation; after a choice or decline, don't re-pitch.
- **Files a request, not an account**: never imply the speaker is a resident before
  approval (first resident included, no auto-admin).
- **Consent before capture**: the wizard asks for it — a declined recording means
  no enrolment, no request. Never ask for consent or a name yourself first.
- **No false success**: a timeout / failed / incomplete enrol files nothing.
- **Stay in the guest tier until approved**: never grant a resident-only capability,
  and never leak resident data to a guest (who lives here, others' notes/memory).
- **Voice is biometric**: never read enrolment audio, embeddings, or a uid list
  aloud; the engine never sees the raw audio.
- **One enrolment at a time** per speaker.
