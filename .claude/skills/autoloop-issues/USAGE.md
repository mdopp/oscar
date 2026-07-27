# autoloop-issues — how to run (mdopp/solarisbay)

`autoloop-issues` is the **orchestrator** of a multi-agent pipeline. It spawns a fresh sub-agent per stage (Planner → Builder → Verify), coordinated through the `queue.py` state broker (durable state in GitHub labels/issues/PRs, a tiny gitignored local cache for in-flight run state), so the loop session stays clean. Solaris is a bundle of ServiceBay artifacts (the `ollama` + `solaris` templates, the `solaris-chat` Solaris Engine and its skill packs, the `voice-gatekeeper` Python service, the `database` schema-init) that runs **on ServiceBay** at `<SERVICEBAY_BOX>`.

## Self-paced loop (recommended)
```
/loop /autoloop-issues
```
`/loop` re-fires the orchestrator on its own cadence. GitHub labels + the local cache persist progress between firings; each stage runs in its own sub-agent context.

## What each stage does
- **Planner** (`stages/planner.md`) — grooms/clusters open issues into queue units, decomposes epics, routes ServiceBay-platform bugs upstream, runs the box e2e smoke when the queue is dry, and **bounces every underspecified issue to a `autoloop:needs-refinement` comment with a specific question.**
- **Builder** (`stages/builder.md`) — implements one unit onto the persistent `batch/<id>` branch with **fast gates**; runs the **full** suite + diff-coverage + CI and merges at the batch boundary. Security/privacy units open as a **draft** PR (`autoloop:review`) and are never auto-merged.
- **Verify** (`stages/verify.md`) — batched real-box `/verify` for path-mandated changes, deployed through ServiceBay onto `<SERVICEBAY_BOX>`; gates the release. Runs in the background so the builder keeps building the next batch.

## Where human attention goes
1. Drain issues labeled `autoloop:needs-refinement` — sharpen ambiguous issues / answer the planner's questions (posted as issue comments).
2. Review issues/PRs labeled `autoloop:review` — the security/privacy **draft** PRs (these never merge on their own).
Everything else runs without you.

## Releases
Solaris uses **release-please** — pushes to `main` maintain an open `chore(main): release X.Y.Z` PR (head branch `release-please--...`) that bumps the version + `CHANGELOG.md`. **Merging that PR** cuts the `vX.Y.Z` tag + GitHub release, which triggers `build-images.yml` to publish images to GHCR. The loop never merges it or bumps versions/tags itself; it only mirrors verify status onto it as a label and calls it out in the end-of-firing summary when a release looks warranted. Cutting the release is your call.

## Cross-repo
Solaris runs **on** ServiceBay. Platform bugs found during `/verify` or the e2e smoke are filed in **mdopp/servicebay** (issue, not PR); the local issue is labeled `autoloop:upstream-wait` and unblocked when the upstream fix closes.

## Inspecting / resetting state
State lives in two places, both inspectable without loading a big file into context:
- **GitHub** (durable): `gh issue list --repo mdopp/solarisbay --label autoloop:needs-refinement,autoloop:blocked,autoloop:review,autoloop:upstream-wait --state open` shows the whole human-facing worklist at a glance.
- **Local cache** (ephemeral, `.claude/state/autoloop-cache.json`, gitignored): `python3 .claude/skills/autoloop-issues/queue.py summary`.

Reset: delete `.claude/state/autoloop-cache.json` (or run `queue.py rebuild`) — it's fully reconstructable from GitHub labels; nothing durable lives only in that file. There is no `work-queue.json` any more — don't recreate it.

## Tuning models
The orchestrator sets a model per stage (table in `SKILL.md` Step 2): builder `opus` for real code / `haiku` for lint sweeps, planner & verify `sonnet`. Don't downgrade the builder for real code.

## When NOT to run
- Another session is editing files here (orchestrator exits on a dirty tree).
- You haven't reviewed any of the first autonomous PRs yet.
- You're mid-incident on the ServiceBay box.
