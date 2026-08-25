# Solaris — house rules

Solaris is a household AI assistant packaged as ServiceBay artifacts: ServiceBay
Pod-YAML templates, the `solaris-chat` Solaris Engine (agent core + chat surface,
the code-heavy part), its skill packs (markdown prompt content), the
`voice-gatekeeper` Wyoming bridge, the `database` alembic schema-init sidecar,
and the bundled `solarisbay` stack. It runs **on ServiceBay**. See `README.md` for
the full layout.

These rules apply to every session, human or agent.

## Finish the work — this rule outranks every other rule here

**Work everything through to production. Do not stop and hand back a list.**

A task is done when it is merged, released, and verified running on the box —
not when it is analysed, planned, drafted, or "ready for review". Reporting
status is not delivering work. If you find yourself writing a summary of what
someone else now has to do, you have stopped too early.

**When something genuinely needs the operator, ASK — don't halt.**

- Ask with **concrete answer options**, not open prose.
- Write the question for someone who has **not looked at this project for 2–4
  weeks**: name the thing, say what it changes, say what happens with each
  option. No unexplained issue numbers, no jargon, no assumed context.
- Ask **as early as you know** you'll need the answer, not after everything else
  is finished.
- Then **carry on to done** with the answer. One question is a checkpoint, not
  an exit.

**Never end a turn in a state where the loop is stopped and work remains.**
"Awaiting review", "the queue is empty but three drafts are open", "hard-exit
condition reached" are not endings — they are questions you have not asked yet.
If a rule in this repo, in a skill, or in a stage doc tells you to halt and hand
work back, that rule is wrong: fix the rule, then keep going.

## Design (resident-facing UI)

- Every change to a resident-facing surface (the chat, cards, `.tool`s, pages)
  must meet the **"could my mother use it?"** bar — see
  [`docs/design-guidelines.md`](docs/design-guidelines.md): self-explaining,
  mobile-first, plain language, obvious/safe actions, immediate feedback, no dead
  ends, one card/pattern SSOT. Walk that checklist before shipping UI; note in the
  PR any rule a change can't meet.

## Commits

- **Conventional Commits**: `type(scope): subject` — `feat`/`fix`/`refactor`/
  `chore`/`docs`/`test`. Scope mirrors the path: `fix(gatekeeper):`,
  `feat(skill):`, `fix(template):`, `feat(solarisbay):`, `chore(db):`,
  `docs:`.
- **No parentheses in the subject** beyond the conventional `(scope)`. A stray
  `(...)` token can make release tooling run green but cut no release — keep
  subjects paren-free.

## Scope discipline

- Smallest change that solves the task. A bug fix doesn't need surrounding
  cleanup; a one-shot doesn't need a helper.
- Three similar lines beat a premature abstraction. No speculative
  error-handling, fallbacks, or feature flags for cases that can't happen.

## Comments

- Default to none. Add one only for a non-obvious *why* (a hidden constraint, a
  workaround, a surprising invariant). Don't narrate *what* the code does.

## Verify in the real environment

- Type-check + tests prove code correctness, not **feature** correctness.
- Template (`templates/**`), skill (`**/skills/*/SKILL.md`), `solaris-chat`,
  `voice-gatekeeper`, `database`/migration and `stacks/**` changes are verified
  by **deploying the changed artifact through ServiceBay onto the box** and
  checking the Solaris runtime — not by CI alone (CI only builds images). If
  you can't verify on the box, say so explicitly.

## Releases

- Releases are automated via **release-please**. It maintains a release PR that
  bumps the version + `CHANGELOG.md` from the conventional commits on `main`.
  **Merging that release PR** cuts the `vX.Y.Z` tag + GitHub release, which
  triggers `build-images.yml` to publish `solaris-gatekeeper`,
  `solaris-gatekeeper-ml`, `solaris-chat`, and `schema-init` to GHCR.
- Conventional Commits + paren-free subjects (above) are load-bearing:
  release-please derives the version bump and CHANGELOG from them.
- **Don't** hand-bump versions in `pyproject.toml` or create/push tags by hand —
  let release-please own that.
- **Cutting the release is part of finishing the work.** Ask the operator once
  per session whether to cut it (with options: cut now / hold / cut after the
  next batch), then merge the release PR yourself and confirm the tag, the GHCR
  images, and the box. Don't leave a green, verified release PR sitting open as
  someone else's homework — see *Finish the work* above.

## Never

- `--no-verify` / skip hooks — fix the underlying failure instead.
- Loosen the lint baseline or a CI check to make it pass.

## Local gates

- Install hooks once: `pip install pre-commit && pre-commit install`.
- Lint: `ruff check . && ruff format --check .`
- Tests: `cd voice-gatekeeper && pip install -e '.[test]' && pytest -q`
- CI (`ci.yml`) runs lint + pytest on Python changes; image builds run in
  `build-images.yml`.

## Issues

- Capture **symptom + repro + starting-point files** — not a fix-plan or
  acceptance bullets. The fix is decided in the PR. Symptom-style issues age
  well; fix-plan-heavy bodies rot. See `.github/ISSUE_TEMPLATE/bug_report.md`.

## Autoloop state

- `.claude/state/work-queue.json` is **retired** — never `cat`/read/write it,
  and never recreate it. It used to be re-read into context in full on every
  autoloop tick (~82KB), which burns tokens for no reason and doesn't scale to
  concurrent loop instances.
- All autoloop state now goes through
  `.claude/skills/autoloop-issues/queue.py` (`summary`/`candidates`/`plan`/
  `next`/`claim`/`built`/`batch`/`verify-set`/`verify-get`/`park`/`note`/
  `mirror`/`rebuild`) — durable state lives in GitHub (`autoloop:*` labels +
  issue comments), a tiny gitignored `.claude/state/autoloop-cache.json` holds
  only in-flight run state. See `.claude/skills/autoloop-issues/SKILL.md`.
