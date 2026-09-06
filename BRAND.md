# Solaris — Brand

> **Solaris** is a second brain you talk to. Put your thinking into words —
> by voice or in chat — and it holds, connects, and gives it back, alive,
> when you ask.

**Tagline:** Help yourself think.
**Voice:** "Solaris" · wake word "Solaris" (one word — no "Hey"/"Ok" prefix)
**Home:** SolarisBay, on ServiceBay

---

## The name

**Solaris** takes its name from Stanisław Lem's 1961 novel — the planet whose
entire surface is a single sentient ocean: a vast, thinking thing that listens,
remembers, and answers back in forms drawn from the minds of those who study it.
That is the picture: a mind you can talk to, made of everything you have ever
put into it.

It keeps the older idea it grew from, too. The name's root is *soliloquy* —
from St. Augustine's *Soliloquia* (c. 386 AD), a dialogue between Augustine and
his own Reason. Augustine had no word for *speaking alone with one's own soul*,
so he coined one: *solus* (alone) + *loquī* (to speak). Solaris is that
soliloquy **made to remember**: you speak with your own soul, and this time
something holds every word.

- **Sol** — soul · sun · source. Your knowledge, your way of thinking, the
  light you cast on a question.
- **The ocean** — Lem's thinking sea: not a tool that fetches, but a mind that
  holds the whole of what you've said and connects it.
- **The soliloquy** — a thought spoken is a thought released. To speak aloud
  and be heard, fully, is the thing Solaris is for.

A soliloquy on a stage is spoken alone, yet overheard. **Solaris is the
listener that was always missing:** you think aloud, and for the first time
something holds it — and connects it to everything else you know, and to what
the world knows.

## Voice & wake

- Wake word: **"Solaris"** — a single word, **no "Hey"/"Ok" prefix**. You don't summon a device; you say its name.
- **Solaris** is the mind you talk to — the corpus of everything you've said and
  the voice that answers. **SolarisBay** is the home it lives in. One name to
  call, one place it rests — the sun that lights your stored knowledge, the one
  who turns to you when you speak.

## Tagline

**Help yourself think.**

- **Display / emphasis:** `Help. Yourself. Think.` — three deliberate beats:
  the soul, the self, the thought set loose.
- **Inline / spoken:** *Help yourself think.* — one breath, the idiom intact.
- Rule of thumb: punctuated form for hero/display; smooth form for body,
  captions, and voice.

Supporting lines:
- Origin line: *A soliloquy you can keep.*
- Elevator: *Solaris is a soliloquy you can keep — put your mind into words,
  and it knows the rest.*

## What it is

A personal knowledge base and agent: your chats, documents, notes, and the
digital reflection of your thinking — captured, connected, and reachable by
voice ("Solaris") or chat. It runs **on ServiceBay**, which is its harbor:

> **ServiceBay** hosts **SolarisBay** (the home & store) · **Solaris** is the
> mind that lives there — and the voice you summon.

**SolarisBay** is the home stack — the harbor where the soul rests (your chats,
documents, knowledge) and the machinery that brings it alive runs. The `*Bay`
sibling to ServiceBay. Components *inside* SolarisBay carry bare role names
(`gatekeeper`, `schema-init`) — the namespace already says whose they
are.

## Tone of voice

Soul **and** clarity. Warm and invitational, a little mythic at the origin,
plain-spoken in the promise. Never self-help-cheesy, never cold-tech. It speaks
*to* you, as the part of you that remembers everything and has read the rest.

## Domains

- Brand: **solaris.ai** (primary) · **solaris.io**
- Home: **solaris.de**
- `solaris.com` is registered but parked (no live site) — acquisition target,
  not blocking.

## Naming convention

Inside SolarisBay, in-stack containers keep bare role names (`gatekeeper`,
`schema-init`); the brand prefix returns only on published artifacts (GHCR
images `solaris-*`, Python projects) where they must be identifiable. Two
deliberate stems: home/voice on `solaris`/`solarisbay` (stack, pod, template
dir, voice handle), brand artifacts on `solaris` (images, Python projects, env
vars `SOLARIS_*`, data paths `solaris.db` / `/var/lib/solaris`, notes
namespace, Wyoming/MCP program names). The chat pod is the one brand-prefixed
*template* (`solaris-chat`), lining up with its `solaris-chat` image and
`solaris-chat/` source dir, alongside the role-named source dirs
(`voice-gatekeeper/`, `database/`). Unchanged: `llama`; generic package
`gatekeeper`; `GATEKEEPER_*` / `DEFAULT_UID`.

---

*Brand v1.0 — origin: Lem's *Solaris* (1961) over Augustine's soliloquium (c. 386 AD).*
