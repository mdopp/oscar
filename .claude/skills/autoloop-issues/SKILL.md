---
name: autoloop-issues
description: Orchestrates an autonomous issue-resolution pipeline for mdopp/solarisbay — Planner → Builder → Verify — coordinated through the queue.py state broker, spawning each stage as a fresh sub-agent so the loop session stays clean. Verify runs in the BACKGROUND (writes its own result file) so the builder keeps build-ahead-ing the next batch while a prior batch is verified on the real ServiceBay box; only the seal→release critical section serializes. Fast per-issue gates, expensive pipeline (CI + real-box /verify) once per batch. Security/privacy-sensitive issues open as a DRAFT PR and wait for human review (pre-merge gate). Durable state lives in GitHub (labels/issues/PRs); a tiny gitignored cache holds only in-flight run state. Use when the user asks to "burn down the backlog", "work the solarisbay issues autonomously", or invokes /loop with this skill.
---

# Autoloop orchestrator — mdopp/solarisbay

You are the **coordinator** of an autonomous issue-resolution pipeline. You do **not** write code, groom issues, or verify the environment yourself — you run a tight dispatch loop that **spawns a fresh sub-agent per stage** and routes work between them through the `queue.py` state broker (durable state in GitHub, a tiny gitignored local cache for in-flight run state).

Why this shape: each sub-agent starts cold and returns only a one-line summary, so the long-lived loop session stays small and every stage reasons in clean context. The pipeline is built so **human attention goes to one place: refining issues** (the planner's `needs_refinement` questions, mirrored as `autoloop:needs-refinement`). Everything downstream — grouping, building, verifying — runs without you.

```
            ┌──────────────── you (orchestrator, this session) ────────────────┐
            │ preflight → queue.py summary → dispatch ONE stage agent → re-check → cadence │
            └──────────────────────────────────────────────────────────────────┘
 PLANNER ──plans──▶ queue.py cache ──┬─▶ BUILDER ──merges, verify-set owed──┐
 groom/cluster/                      │   fast gates, batch seal,            │
 decompose/refine                    │   push→CI→merge                      ▼
                                     └─▶ BUILDER build-aheads          VERIFY (BACKGROUND) ──gates──▶ release
                                         next batch concurrently       deploy through ServiceBay,
                                         (no main, no release)          /verify on the box, restore
                                                                        writes verify-result.json

  VERIFY runs in the BACKGROUND (Agent run_in_background) and writes its verdict to its OWN file
  (.claude/state/verify-result.json); the orchestrator folds it into the cache via `queue.py verify-set`
  at preflight (single writer). While it runs, the builder keeps BUILDING the next batch. Only the
  seal→release critical section serializes; building is concurrent with it.
```

Any project `CLAUDE.md` or user memory overrides this skill on conflict. Read them before the first iteration of a fresh `/loop` run.

**What Solaris is, and why verification is unusual.** Solaris is *not* a standalone app. It is a bundle of ServiceBay artifacts: `templates/{ollama,solaris}/` (Pod-YAML templates), `templates/solaris/skills/*/SKILL.md` (skill packs the Solaris Engine folds into its prompts, delivered to the node by ServiceBay's asset-transport), `solaris-chat/` (the Solaris Engine — agent core + chat surface, the code-heavy part, package `solaris-chat/src/solaris_chat/`, built as image `solaris-chat`), `voice-gatekeeper/` (a Python Wyoming bridge, package `voice-gatekeeper/src/gatekeeper/`, pytest in `voice-gatekeeper/tests/`, built as image `solaris-gatekeeper`), `database/` (alembic schema-init sidecar, image `schema-init`), and `stacks/solarisbay/stack.yml`. Solaris runs **on ServiceBay**, on the same box ServiceBay uses (`<SERVICEBAY_BOX>`). So real-environment `/verify` means *deploying the changed Solaris artifact through ServiceBay onto that box and checking the Solaris runtime* — see `stages/verify.md`.

## State — GitHub is the source of truth; `queue.py` brokers a tiny local cache

State lives in **two tiers, split by durability**, and **no stage ever reads a big JSON blob into context** — every stage calls `queue.py` verbs and gets back only the slice it needs. The old fat `work-queue.json` (re-read in full each tick, ~82KB, unbounded, unsafe under concurrent instances) is **retired** — do not create it, read it, or write it. If it (or its template) exists on disk, delete it; it is dead.

**DURABLE / core state → GitHub (the source of truth).** Issue open/closed, work status as `autoloop:*` labels, human questions/reasons as issue comments, completion as closed-issue + merged-PR. This survives across firings, machines, and **concurrent loop instances**: the `autoloop:building` label is the **cross-instance claim**, so two instances never grab the same issue. Labels: `autoloop:queued` (planned) · `autoloop:building` (claimed) · `autoloop:blocked` · `autoloop:needs-refinement` · `autoloop:review` (security/privacy unit — see below, this is Solaris's **pre-merge draft** gate, not a post-deploy glance) · `autoloop:device-test` · `autoloop:upstream-wait` (blocked on mdopp/servicebay) · `autoloop:verify-pending`/`autoloop:verify-failed` (on the open release-please PR). These labels already exist in the repo (`gh label create` once if a new one is ever needed).

**EPHEMERAL run state → a tiny local cache** (`.claude/state/autoloop-cache.json`, **gitignored**, touched only through `queue.py`): the in-flight `batch` (branch/count/unit_ids), the current unit **plan** (this run's clustering — `{id, kind, issues[], theme, region, scope, acceptance, gate, security, status, pr}`), the `verify` state-machine (`owed→verifying→green|red`, sha, since), and a bounded `notes` ring (15 entries, run-scoped scratch). It holds only what GitHub can't cheaply model; it's a few KB, never committed, and **rebuildable from GitHub** (`queue.py rebuild`) — so losing it is safe and it is never the source of truth. `security: true` on a unit is carried by the `autoloop:review` label on its issue; a unit built this way rides its **own** branch, never the shared batch (see `stages/builder.md`).

**No `awaiting_user`/`review`/`upstream-wait` free text lives in the cache** — if a planner needs to record *why* something is parked, it posts that as an issue comment (`--comment` on `queue.py park`) so the reason is durable and visible to the human on GitHub, not buried in a local file only the loop reads.

### `queue.py` verbs — the only way stages touch state

Run `python3 .claude/skills/autoloop-issues/queue.py <verb>` (add `--offline` to skip gh in tests; `--repo mdopp/solarisbay` to override the origin if needed). Covered by `queue.py selftest`.

| Verb | Who | Does |
|---|---|---|
| `summary` | orchestrator | compact status (batch, verify, gh label counts) — the preflight peek |
| `candidates [--order L,…] [--exclude L,…]` | planner | open, unclaimed issues in priority order |
| `plan '<unit-json>'` | planner | record a planned unit + label its issues `autoloop:queued` |
| `next` | builder | the next planned unit to build |
| `claim <id>` | builder | **cross-instance claim** (`autoloop:building`) before building |
| `built <id> [--pr N]` | builder | mark the unit built onto the batch |
| `batch new\|seal\|reset [--branch …]` | builder/orch | batch lifecycle (`reset` drops the shipped units) |
| `verify-set <sha> <status> [--pr N]` | builder/orch | set verify state + mirror the release-PR label |
| `verify-get` | orchestrator | read verify state (auto-resets a dead `verifying`) |
| `park <issue> <blocked\|refinement\|review\|device-test\|upstream-wait> [--comment …]` | planner | durably park to GitHub (label + comment) |
| `note "<one line>"` | any | append to the bounded run-scoped ring |
| `mirror [--pr N]` | orchestrator | prune the cache + re-project labels (one-way) |
| `rebuild [--release-pr N]` | orchestrator | cold-start: reconstruct the cache from GitHub |
| `lock` / `unlock` | orchestrator | advisory single-writer lock for this checkout |

`park <issue> blocked --comment "..."` also covers **awaiting-user** and **epic/decomposition** parking — there's no separate label for those; phrase the comment/`blocked_by` reason accordingly (e.g. "awaiting-user: external comment, not the loop's to answer" or "epic: umbrella for #a #b #c").

## Batch economy — the prime directive (ENFORCED)

The expensive pipeline — full gates, CI, real-box `/verify` — runs **once per batch (up to 8 closed issues), never once per issue.** All fixes accumulate on ONE long-lived branch `batch/<id>`; it is pushed / PR'd / CI'd / merged / verified **only when it holds 8 closed issues OR the queue of planned units is empty.** Shipping one issue as its own PR while planned units remain is a **failure of this pipeline**.

The builder enforces the per-issue side (fast gates only, commit to the batch branch, no push). You enforce the batch side: **never dispatch a seal step while `batch.count < 8` AND planned units remain.**

**Build-ahead is allowed; seal-ahead is not.** Verify runs in the background (it touches only the box env via ServiceBay and its own result file). The builder may keep **building** the next batch onto a fresh `batch/<id>` branch while a prior batch is being verified — building writes neither `main` nor the box, so it overlaps safely. What must **not** overlap is the singleton critical section: there is one `main`, one box, one verify state, so **a new batch may not be *sealed* while verify status is `owed`/`verifying`/`red`** (a prior batch is still in merge/verify). Build up to 8 then *wait* for the verify to clear before sealing.

## Step 0 — Preflight (every firing)

1. **Working tree clean?** `git status --porcelain`. Dirty → exit (another session owns this tree). Don't stash/switch.
2. **On `main`, current?** `git fetch origin && git checkout main && git pull --ff-only`. FF fails → exit + report.
3. **Lock check.** `.claude/state/autoloop.lock` mtime < 10 min ⇒ another firing is running → exit. Else touch it.
4. **Read status:** `queue.py summary` (compact — batch, verify, gh label counts). On a cold start (no cache), `queue.py rebuild` reconstructs it from GitHub labels. `queue.py mirror` prunes the cache + re-projects labels.
5. **Fold in any background Verify result.** If `.claude/state/verify-result.json` exists, the background agent finished: fold it in with `queue.py verify-set <sha> <status> --detail "<…>"`, then **delete the result file**. `queue.py verify-get` auto-resets a `verifying` entry stuck >20 min (the agent died → relaunches next dispatch).
6. **Release gate.** Releases are managed by **release-please**: `gh pr list --repo mdopp/solarisbay --state open --json number,headRefName,labels` and pick the one whose `headRefName` starts with `release-please--`. On each push to `main` it maintains that `chore(main): release X.Y.Z` PR (bumps the version + `CHANGELOG.md` from conventional commits); **merging that PR** cuts the `vX.Y.Z` tag + GitHub release (triggers `build-images.yml`, publishing `solaris-gatekeeper` + `schema-init` to GHCR). You **never merge the release PR yourself** and never create/push tags or bump versions in `pyproject.toml` — cutting a release is a human/explicit-ask decision. Mirror verify status onto that PR as a label (`queue.py verify-set ... --pr <n>` or `queue.py mirror --pr <n>` does this): `owed`/`verifying` → `autoloop:verify-pending`; `red` → `autoloop:verify-failed`; `green`/`null` → remove both. The gate you enforce here is the verify state: a merged batch whose path-mandated changes are `owed`/`verifying`/`red` is **not** clear, and you must not seal the next batch until it goes `green`. If a release is warranted after a green verify, say so in your end-of-firing summary — don't tag, don't merge the release PR (its mere existence on GitHub is the durable record; nothing to track locally).

## Step 1 — Dispatch (the loop body)

**First, a non-blocking side-action (does NOT consume the tick):** if verify status is `"owed"`, launch Verify **in the background** (Step 2, `run_in_background: true`), set it to `"verifying"` (`queue.py verify-set <sha> verifying`) and **fall through** to pick a foreground stage below. If already `"verifying"`, an agent is already in flight — don't relaunch; fall through. The background verify clears the release gate on its own time; you don't wait on it here.

Then pick **exactly one** foreground stage this tick, by the first matching rule, and spawn it (Step 2). Then re-check status and loop.

1. **Builder — seal** — if a `batch` exists and (`batch.count >= 8` **or** `queue.py next` returns nothing) and it isn't merged yet **and** verify status is clear (`green`/`null` — *not* `owed`/`verifying`/`red`). Builder runs full gates + CI (where CI applies), merges, sets verify to `owed` if any merged file is path-mandated. **Seal-ahead is forbidden:** if verify is `owed`/`verifying`/`red`, a prior batch is still in merge/verify — do **not** seal; build-ahead instead (rule 2), or idle-wait (Step 3).
2. **Builder — build** — if `queue.py next` returns a planned unit and `batch.count < 8`. Builder implements the next unit onto the batch branch with fast gates only. **This is the build-ahead path** — eligible even while a background Verify runs, because building touches neither `main` nor the box.
3. **Planner** — if there's no actionable unit. Planner refills: groom + cluster open issues, decompose epics, park refinement/awaiting-user/upstream-waits (security issues become `security:true` units that route to the draft gate, not parked), or (queue dry) enqueue lint-sweep units, run a codebase eval, or run **end-to-end validation on the box** + route failures cross-repo to mdopp/servicebay.

Never jump to seal while mid-batch (`count < 8` and planned units remain) — that's the prime-directive violation. Keep building. If the only thing left is to wait on a background Verify (batch built out to 8, nothing to plan), don't dispatch a foreground stage — go to Step 3 and schedule a short wakeup.

## Step 2 — Spawning a stage agent

Use the **Agent** tool, `subagent_type: "general-purpose"` (needs Bash, gh, SSH/MCP env tools, Edit/Write).

**Planner and Builder run foreground (blocking)** — they share `main`, the batch branch, and the `queue.py` cache, so one foreground stage per tick keeps that file single-writer. **Verify runs in the background** (`run_in_background: true`) — it touches only the box (via ServiceBay) and its own result file, so it overlaps with the builder safely.

Foreground (Planner / Builder) prompt — they touch state only via `queue.py` verbs:
```
Read .claude/skills/autoloop-issues/stages/<planner|builder>.md and follow it exactly.
Context for this run: <unit id / batch state to act on>.
Touch state ONLY via `python3 .claude/skills/autoloop-issues/queue.py <verb>` (candidates/plan/next/
claim/built/batch/verify-set/park/note — see SKILL.md § queue.py verbs); never create, read, or write
.claude/state/work-queue.json (retired) or hand-edit the cache JSON. Return ONE line: what you did +
the mutations you made. Do not narrate.
```

Background (Verify) prompt — it does **NOT** touch the queue.py cache (avoids a write-race with the concurrent builder); it writes its verdict to its own file:
```
Read .claude/skills/autoloop-issues/stages/verify.md and follow it exactly.
Context for this run: verify SHA <sha>, path-mandated paths: <detail>.
Box: <SERVICEBAY_BOX> (supply the real address from CLAUDE.md / memory).
Do NOT touch the queue.py cache. Write your verdict to .claude/state/verify-result.json as
{sha, status:"green"|"red"|"owed", detail, verified_at}. The orchestrator folds it in via
`queue.py verify-set`.
Return ONE line: the verdict + any revert PR or upstream issue you opened. Do not narrate.
```

Builder mode (`build` vs `seal`) and the unit `gate`/`security` go in the context line. After a **foreground** agent returns: **re-run `queue.py summary`** (GitHub + the cache are authoritative, not the agent's summary line), append the one-liner to your tally, go back to Step 1. The **background** Verify does not block — proceed immediately; its result is folded in at the next preflight (Step 0.5), and the harness re-invokes the loop when it completes.

### Model per stage — match the model to the cost of being wrong

Set `model` on each Agent call. A weak model on real code *costs* time (rework); don't downgrade where being wrong is expensive — do downgrade mechanical work.

| Stage / unit | Model |
|---|---|
| Builder — real code (`cluster`/`issue`) | `opus` |
| Builder — `lint-sweep` unit | `haiku` |
| Planner | `sonnet` |
| Verify | `sonnet` |

The orchestrator itself is pure dispatch and runs at the session model — a light model is fine for it.

## Step 3 — Cadence (`/loop` dynamic mode)

**Never sleep while there is eligible work** — go straight to the next dispatch. A **background Verify in flight is not a reason to sleep** if there's still a unit to build: leave it running and keep building the next batch. `ScheduleWakeup` only when:
- **Mid-pipeline waiting on an external gate** (CI on an image-path PR, or a ServiceBay install/deploy on the box) → `delaySeconds ≤ 480`, prefer ~60s if imminent.
- **Build-ahead exhausted, only a background Verify outstanding** (batch built out to 8, nothing left to plan, can't seal until verify clears) → `≤ 480`. The harness also re-invokes you when the background agent completes, so this is a fallback heartbeat.
- **Queue empty and planner found nothing** (and an e2e/eval ran recently) → idle heartbeat `≤ 480`.

Pass the same `/loop /autoloop-issues` input back. Don't nap between dispatches when work remains.

## Comment hygiene

Every comment any stage posts is attributable as agent-authored (an AI marker if posted as a human account), and stays short and sharp. **No stage ever replies to an external human commenter** — those issues get `queue.py park <issue> blocked --comment "awaiting-user: ..."` for a human-confirmed reply, never a loop reply.

## State hygiene (enforced in code by `queue.py`)

You don't hand-prune anything — `queue.py` enforces it on every write: the `notes` ring is capped (15, run-scoped scratch), `done`/shipped units are dropped (`batch reset` clears a released batch), and the cache never carries a schema or long prose. The durable record of shipped work is GitHub (closed issues + merged PRs) + git history, so there is nothing to accumulate locally. Call `queue.py mirror` at preflight to prune + re-project labels one-way. This is the whole reason state is split: the local cache stays a few KB and cheap to touch, while everything durable — and everything that must be consistent across concurrent instances — lives in GitHub.

## End-of-firing summary

```
Autoloop (solarisbay) firing complete.
  Built this firing: <unit ids> → batch/<id> (count N/8)
  Merged batches:    PR #<n> (closes #a #b …)
  Verify:            green @ <sha> on the box | verifying (background) | owed | red (<detail>)
  Review pre-merge:  #<issue> (draft #<pr>) — security/privacy, NOT merged   ← review these
  Needs refinement:  #<issue> — "<question>"   ← your worklist
  Upstream waits:    #<issue> → servicebay#<N>
  Awaiting user:     #<issue> (external comment)
Next: <building #x | sealing batch | verifying | planner refill | e2e | idle heartbeat>.
```

The **Needs refinement** line is the point of the pipeline.

## Hard exit conditions (stop; do not reschedule)

1. A stage reports CI red twice on the same SHA with no change between.
2. `autoloop:review` shows >3 security/privacy draft PRs accumulated without human review.
3. Working tree dirty at preflight on two consecutive firings.
4. A `/verify` failed twice on the same SHA with no change between, or the box was left in a staging state the Verify stage couldn't restore (env must not be left in the test state).
5. Planner's issue queue and lint set both empty AND a codebase eval ran within the last ~5 firings AND an e2e ran since the last merge.
6. Every open issue is blocked on an unmerged **mdopp/servicebay** upstream fix (`autoloop:upstream-wait`) — nothing in Solaris is actionable until ServiceBay ships it. Report the upstream links and wait.

## Things this orchestrator does NOT do
- Write code / groom / `/verify` itself — only dispatches stage agents.
- Read, write, or recreate `.claude/state/work-queue.json` — it is retired; state is `queue.py` verbs + GitHub only.
- Bump versions in `pyproject.toml`, create/push `v*` tags, or merge release-please's release PR — releases are the user's call.
- `gh pr merge --auto` (no branch protection → silent no-op); reply to external commenters.
- Dispatch a seal step while mid-batch (prime directive).
- **Seal** a new batch while a prior batch's verify status is `owed`/`verifying`/`red` (seal-ahead forbidden — one batch in the merge/verify critical section at a time). It *may* build-ahead.
- Block the loop on Verify — that runs in the background; the builder keeps building while it does.
- Ship/merge a path-mandated change without a green real-box `/verify`.
- Auto-merge a `security:true` change — those open as draft and wait for human review.

## Reference
- Stages: `stages/planner.md`, `stages/builder.md`, `stages/verify.md` (this dir; Verify runs in the background and writes `.claude/state/verify-result.json`). State broker: `queue.py` (verbs + `selftest`). How to run: `USAGE.md`.
- Repo: `mdopp/solarisbay`. Upstream platform: `mdopp/servicebay` (cross-repo routing — see `stages/{planner,verify}.md`).
- Real-box access: `<SERVICEBAY_BOX>` — the **same** box and access paths ServiceBay uses (SSH / HTTP API / MCP; host-key-change, stale-MCP-token, and `Origin`-header gotchas all apply). The `mdopp/solarisbay` registry must be enabled in ServiceBay on that box so changed templates resolve. `<SERVICEBAY_BOX>` is a placeholder — supply the real SSH/HTTP/MCP address from local config (project `CLAUDE.md` or memory), **never** commit it to this public repo.
- CI: `.github/workflows/ci.yml` (ruff + pytest + semgrep + pip-audit + diff-coverage, path-filtered to Python/config paths) and `build-images.yml` (builds the two images on PR for image paths, publishes on `main`/tags). **Template-only / skill-only / docs-only PRs trigger no CI** — for those the gate is local validation + real-box `/verify`.
- Worked example: `mdopp/servicebay` (`.claude/skills/autoloop-issues/`) — its Verify stage is `box-verify.md` (a `:dev`/`:latest` channel flip); Solaris's Verify deploys the changed artifact through ServiceBay instead.
