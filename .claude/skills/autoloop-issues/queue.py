#!/usr/bin/env python3
"""queue.py — the autoloop's state broker (stdlib-only, no deps).

Why this exists: the autoloop's stages used to read a fat work-queue.json into
context every tick (~13k tokens) and mutate it as free JSON. That is token-heavy,
unbounded, and unsafe when several loop instances run the same repo at once.

This broker splits state by durability:

  * DURABLE / core state -> GitHub (the source of truth): issue open/closed,
    work status as `autoloop:*` labels, human questions/links as issue comments,
    completion as closed-issue + merged-PR. Cross-instance CLAIMS ride on the
    `autoloop:building` label so two instances never grab the same issue.

  * EPHEMERAL run state -> a tiny local cache (`.claude/state/autoloop-cache.json`,
    gitignored): only the in-flight batch, the current unit plan (clustering for
    THIS run), the verify state-machine, and a bounded notes ring. Rebuildable
    from GitHub via `rebuild`, so losing it (or gitignoring it) is safe.

Stages call narrow verbs and get back only the slice they need, so no stage ever
loads the whole state into context. Pruning, caps, and label projection are
enforced here in code — not in prose a model must remember.

Usage: python3 queue.py <verb> [args].  `--help` on any verb.
Requires `gh` on PATH for the GitHub-backed verbs; the cache-only verbs work
offline (and are covered by `python3 queue.py selftest`).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CACHE_DEFAULT = ".claude/state/autoloop-cache.json"
NOTES_CAP = 15  # run-scoped scratch ring; durable history lives in git + issues
LOCK_STALE_S = 600  # a lock older than this is treated as abandoned
CACHE_VERSION = 3

LABEL = "autoloop:"
L_QUEUED = LABEL + "queued"
L_BUILDING = LABEL + "building"
L_BLOCKED = LABEL + "blocked"
L_REFINE = LABEL + "needs-refinement"
L_REVIEW = LABEL + "review"
L_DEVICE = LABEL + "device-test"
L_UPSTREAM = LABEL + "upstream-wait"
L_VERIFY_PENDING = LABEL + "verify-pending"
L_VERIFY_FAILED = LABEL + "verify-failed"

PARK_LABELS = {
    "blocked": L_BLOCKED,
    "refinement": L_REFINE,
    "review": L_REVIEW,
    "device-test": L_DEVICE,
    "upstream-wait": L_UPSTREAM,
}


# --------------------------------------------------------------------------- gh
def gh(args: list[str], check: bool = True) -> str:
    """Run a gh command, return stdout. Fail-soft: on error return '' unless check."""
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        if check:
            sys.exit(f"queue.py: gh unavailable: {e}")
        return ""
    if r.returncode != 0:
        if check:
            sys.exit(f"queue.py: gh {' '.join(args)} failed:\n{r.stderr.strip()}")
        return ""
    return r.stdout


def gh_json(args: list[str], default):
    out = gh(args, check=False)
    if not out.strip():
        return default
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return default


# ------------------------------------------------------------------------ cache
class Cache:
    def __init__(self, path: str):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return self._fresh()
        try:
            d = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return self._fresh()
        return {**self._fresh(), **d}

    def save(self, d: dict) -> None:
        d = prune_state(d)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2))
        tmp.replace(self.path)  # atomic on POSIX

    @staticmethod
    def _fresh() -> dict:
        return {
            "version": CACHE_VERSION,
            "batch": None,  # {branch, count, unit_ids:[...]} | None
            "units": {},  # id -> unit dict (the in-flight plan for THIS run)
            "verify": None,  # {sha, status, since, detail} | None
            "notes": [],  # bounded run-scoped scratch ring
            "lock": None,  # {pid, since}
            "last_invocation": None,
        }


def prune_state(d: dict) -> dict:
    """Enforce the caps in code so the cache can never grow unbounded."""
    notes = d.get("notes") or []
    if len(notes) > NOTES_CAP:
        d["notes"] = notes[-NOTES_CAP:]
    # Drop built units whose work is done — GitHub (closed issues / merged PRs)
    # is the durable record; the cache keeps only in-flight plan.
    units = d.get("units") or {}
    d["units"] = {k: v for k, v in units.items() if v.get("status") != "done"}
    return d


# ------------------------------------------------------------------------ verbs
def v_summary(c: Cache, a) -> None:
    """Compact status for the orchestrator's preflight — never the whole state."""
    d = c.load()
    batch = d.get("batch")
    verify = d.get("verify")
    planned = [u for u in d["units"].values() if u.get("status") == "planned"]
    out = {
        "batch": None
        if not batch
        else {"branch": batch["branch"], "count": batch["count"]},
        "verify": None
        if not verify
        else {"sha": verify.get("sha"), "status": verify.get("status")},
        "planned_units": len(planned),
        "next_unit": (planned[0]["id"] if planned else None),
        "gh": {
            "queued": _count_label(a.repo, L_QUEUED),
            "blocked": _count_label(a.repo, L_BLOCKED),
            "needs_refinement": _count_label(a.repo, L_REFINE),
            "review": _count_label(a.repo, L_REVIEW),
            "device_test": _count_label(a.repo, L_DEVICE),
        }
        if not a.offline
        else {},
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def _count_label(repo: str, label: str) -> int:
    data = gh_json(
        _repo_args(repo)
        + [
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            label,
            "--json",
            "number",
            "--limit",
            "200",
        ],
        [],
    )
    return len(data)


def _repo_args(repo: str | None) -> list[str]:
    return ["-R", repo] if repo else []


def v_candidates(c: Cache, a) -> None:
    """Open, selectable issues in priority order — for the planner. GitHub-backed."""
    exclude = set(x.strip() for x in (a.exclude or "").split(",") if x.strip())
    order = [x.strip() for x in (a.order or "").split(",") if x.strip()]
    claimed = {
        L_QUEUED,
        L_BUILDING,
        L_BLOCKED,
        L_REFINE,
        L_REVIEW,
        L_DEVICE,
        L_UPSTREAM,
    }
    issues = gh_json(
        _repo_args(a.repo)
        + [
            "issue",
            "list",
            "--state",
            "open",
            "--json",
            "number,title,labels",
            "--limit",
            "300",
        ],
        [],
    )
    picked = []
    for it in issues:
        labels = {lb["name"] for lb in it.get("labels", [])}
        if labels & exclude or labels & claimed:
            continue
        rank = next((i for i, o in enumerate(order) if o in labels), len(order))
        picked.append((rank, it["number"], it["title"]))
    picked.sort(key=lambda t: (t[0], t[1]))
    print(
        json.dumps(
            [{"number": n, "title": t} for _, n, t in picked],
            ensure_ascii=False,
            indent=2,
        )
    )


def v_plan(c: Cache, a) -> None:
    """Add a planned unit to the cache and label its member issues `autoloop:queued`."""
    unit = json.loads(a.unit)
    unit.setdefault("status", "planned")
    unit.setdefault("pr", None)
    uid = str(unit["id"])
    d = c.load()
    d["units"][uid] = unit
    c.save(d)
    if not a.offline:
        for n in unit.get("issues", []):
            gh(
                _repo_args(a.repo) + ["issue", "edit", str(n), "--add-label", L_QUEUED],
                check=False,
            )
    print(f"planned unit {uid}: issues {unit.get('issues')}")


def v_next(c: Cache, a) -> None:
    """The next planned unit the builder should implement (or nothing)."""
    d = c.load()
    planned = [u for u in d["units"].values() if u.get("status") == "planned"]
    planned.sort(key=lambda u: str(u["id"]))
    print(json.dumps(planned[0] if planned else None, ensure_ascii=False, indent=2))


def v_claim(c: Cache, a) -> None:
    """Claim a unit for building — the `autoloop:building` label is the cross-instance lock."""
    d = c.load()
    u = d["units"].get(str(a.unit))
    if not u:
        sys.exit(f"queue.py: no planned unit {a.unit}")
    if not a.offline:
        for n in u.get("issues", []):
            gh(
                _repo_args(a.repo)
                + [
                    "issue",
                    "edit",
                    str(n),
                    "--add-label",
                    L_BUILDING,
                    "--remove-label",
                    L_QUEUED,
                ],
                check=False,
            )
    u["status"] = "building"
    c.save(d)
    print(f"claimed {a.unit} ({u.get('issues')})")


def v_built(c: Cache, a) -> None:
    """Mark a unit built onto the batch (PR attached at seal)."""
    d = c.load()
    u = d["units"].get(str(a.unit))
    if not u:
        sys.exit(f"queue.py: no unit {a.unit}")
    u["status"] = "built"
    if a.pr:
        u["pr"] = a.pr
    b = d.get("batch")
    if b and str(a.unit) not in [str(x) for x in b["unit_ids"]]:
        b["unit_ids"].append(str(a.unit))
        b["count"] = len(b["unit_ids"])
    c.save(d)
    print(f"built {a.unit}; batch count {d['batch']['count'] if d.get('batch') else 0}")


def v_batch(c: Cache, a) -> None:
    d = c.load()
    if a.action == "new":
        d["batch"] = {"branch": a.branch, "count": 0, "unit_ids": []}
    elif a.action == "reset":
        # Batch shipped: drop its units (durable record is the merged PR / closed issues).
        b = d.get("batch") or {}
        for uid in b.get("unit_ids", []):
            d["units"].pop(str(uid), None)
        d["batch"] = None
    elif a.action == "seal":
        if d.get("batch"):
            d["batch"]["sealed"] = True
    c.save(d)
    print(json.dumps(d.get("batch"), ensure_ascii=False))


def v_verify_set(c: Cache, a) -> None:
    d = c.load()
    d["verify"] = {
        "sha": a.sha,
        "status": a.status,
        "detail": a.detail or "",
        "since": int(time.time()),
    }
    c.save(d)
    if a.pr and not a.offline:  # mirror onto the release PR as a label (one-way)
        add, rm = _verify_labels(a.status)
        for lbl in rm:
            gh(
                _repo_args(a.repo) + ["pr", "edit", str(a.pr), "--remove-label", lbl],
                check=False,
            )
        for lbl in add:
            gh(
                _repo_args(a.repo) + ["pr", "edit", str(a.pr), "--add-label", lbl],
                check=False,
            )
    print(json.dumps(d["verify"], ensure_ascii=False))


def _verify_labels(status: str):
    if status in ("owed", "verifying"):
        return [L_VERIFY_PENDING], [L_VERIFY_FAILED]
    if status == "red":
        return [L_VERIFY_FAILED], [L_VERIFY_PENDING]
    return [], [L_VERIFY_PENDING, L_VERIFY_FAILED]  # green/clear


def v_verify_get(c: Cache, a) -> None:
    d = c.load()
    verify = d.get("verify")
    # A verifying entry with no progress for >20min = the agent died -> reset to owed.
    if verify and verify.get("status") == "verifying":
        if int(time.time()) - int(verify.get("since", 0)) > 1200:
            verify["status"] = "owed"
            c.save(d)
    print(json.dumps(verify, ensure_ascii=False, indent=2))


def v_note(c: Cache, a) -> None:
    d = c.load()
    d["notes"].append({"note": a.text[:280], "since": int(time.time())})
    c.save(d)  # prune_state caps the ring
    print(f"noted ({len(d['notes'])}/{NOTES_CAP})")


def v_park(c: Cache, a) -> None:
    """Park an issue durably in GitHub: a label + (optional) a comment with the reason."""
    label = PARK_LABELS[a.state]
    if not a.offline:
        gh(
            _repo_args(a.repo)
            + [
                "issue",
                "edit",
                str(a.issue),
                "--add-label",
                label,
                "--remove-label",
                L_QUEUED,
            ],
            check=False,
        )
        if a.comment:
            gh(
                _repo_args(a.repo)
                + ["issue", "comment", str(a.issue), "--body", a.comment],
                check=False,
            )
    print(f"parked #{a.issue} -> {label}")


def v_mirror(c: Cache, a) -> None:
    """Prune the cache and (re)project the verify label onto the release PR. One-way."""
    d = c.load()
    c.save(d)  # save() prunes
    if a.pr and d.get("verify") and not a.offline:
        add, rm = _verify_labels(d["verify"]["status"])
        for lbl in rm:
            gh(
                _repo_args(a.repo) + ["pr", "edit", str(a.pr), "--remove-label", lbl],
                check=False,
            )
        for lbl in add:
            gh(
                _repo_args(a.repo) + ["pr", "edit", str(a.pr), "--add-label", lbl],
                check=False,
            )
    print("mirrored + pruned")


def v_rebuild(c: Cache, a) -> None:
    """Cold-start: reconstruct ephemeral state from GitHub (makes gitignoring safe)."""
    d = Cache._fresh()
    if not a.offline and a.release_pr:
        pr = gh_json(
            _repo_args(a.repo)
            + ["pr", "view", str(a.release_pr), "--json", "labels,headRefName"],
            {},
        )
        labels = {lb["name"] for lb in pr.get("labels", [])}
        if L_VERIFY_FAILED in labels:
            d["verify"] = {
                "sha": "?",
                "status": "red",
                "since": int(time.time()),
                "detail": "from label",
            }
        elif L_VERIFY_PENDING in labels:
            d["verify"] = {
                "sha": "?",
                "status": "owed",
                "since": int(time.time()),
                "detail": "from label",
            }
    c.save(d)
    print("rebuilt cache from GitHub (units re-plan on next planner run)")


def v_lock(c: Cache, a) -> None:
    """Advisory single-writer lock for THIS checkout (mtime-stale after LOCK_STALE_S)."""
    d = c.load()
    lock = d.get("lock")
    now = int(time.time())
    if (
        lock
        and now - int(lock.get("since", 0)) < LOCK_STALE_S
        and lock.get("pid") != os.getpid()
    ):
        print("locked", file=sys.stderr)
        sys.exit(3)
    d["lock"] = {"pid": os.getpid(), "since": now}
    c.save(d)
    print("acquired")


def v_unlock(c: Cache, a) -> None:
    d = c.load()
    d["lock"] = None
    c.save(d)
    print("released")


# --------------------------------------------------------------------- selftest
def v_selftest(c: Cache, a) -> None:
    """Offline coverage of the cache-backed logic (no gh)."""
    import tempfile
    import unittest

    class T(unittest.TestCase):
        def setUp(self):
            self.d = tempfile.mkdtemp()
            self.c = Cache(os.path.join(self.d, "cache.json"))

        def test_fresh_and_roundtrip(self):
            d = self.c.load()
            self.assertEqual(d["version"], CACHE_VERSION)
            d["last_invocation"] = 42
            self.c.save(d)
            self.assertEqual(self.c.load()["last_invocation"], 42)

        def test_notes_ring_capped(self):
            d = self.c.load()
            d["notes"] = [{"note": str(i), "since": i} for i in range(NOTES_CAP + 20)]
            self.c.save(d)
            self.assertEqual(len(self.c.load()["notes"]), NOTES_CAP)

        def test_done_units_dropped(self):
            d = self.c.load()
            d["units"] = {
                "1": {"id": "1", "status": "done"},
                "2": {"id": "2", "status": "planned"},
            }
            self.c.save(d)
            self.assertEqual(list(self.c.load()["units"]), ["2"])

        def test_batch_reset_drops_units(self):
            d = self.c.load()
            d["units"] = {"u1": {"id": "u1", "status": "built"}}
            d["batch"] = {"branch": "batch/x", "count": 1, "unit_ids": ["u1"]}
            self.c.save(d)
            ns = argparse.Namespace(action="reset")
            v_batch(self.c, ns)
            back = self.c.load()
            self.assertIsNone(back["batch"])
            self.assertNotIn("u1", back["units"])

        def test_verify_labels(self):
            self.assertEqual(
                _verify_labels("owed"), ([L_VERIFY_PENDING], [L_VERIFY_FAILED])
            )
            self.assertEqual(
                _verify_labels("green"), ([], [L_VERIFY_PENDING, L_VERIFY_FAILED])
            )

    res = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(T)
    )
    sys.exit(0 if res.wasSuccessful() else 1)


# ----------------------------------------------------------------------- parser
def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--cache", default=os.environ.get("AUTOLOOP_CACHE", CACHE_DEFAULT))
    p.add_argument(
        "--repo",
        default=os.environ.get("AUTOLOOP_REPO"),
        help="owner/repo for gh (default: the checkout's origin)",
    )
    p.add_argument(
        "--offline", action="store_true", help="skip all gh calls (cache only)"
    )
    sub = p.add_subparsers(dest="verb", required=True)

    def add(name, fn, **kw):
        sp = sub.add_parser(name, help=(fn.__doc__ or "").split("\n")[0])
        sp.set_defaults(fn=fn)
        return sp

    add("summary", v_summary)
    sp = add("candidates", v_candidates)
    sp.add_argument("--exclude", default="postponed,wontfix,duplicate")
    sp.add_argument("--order", default="", help="comma-separated label priority")
    sp = add("plan", v_plan)
    sp.add_argument("unit", help="unit as JSON")
    add("next", v_next)
    sp = add("claim", v_claim)
    sp.add_argument("unit")
    sp = add("built", v_built)
    sp.add_argument("unit")
    sp.add_argument("--pr", type=int)
    sp = add("batch", v_batch)
    sp.add_argument("action", choices=["new", "seal", "reset"])
    sp.add_argument("--branch", default="")
    sp = add("verify-set", v_verify_set)
    sp.add_argument("sha")
    sp.add_argument("status", choices=["owed", "verifying", "green", "red"])
    sp.add_argument("--detail", default="")
    sp.add_argument("--pr", type=int)
    add("verify-get", v_verify_get)
    sp = add("note", v_note)
    sp.add_argument("text")
    sp = add("park", v_park)
    sp.add_argument("issue", type=int)
    sp.add_argument("state", choices=list(PARK_LABELS))
    sp.add_argument("--comment", default="")
    sp = add("mirror", v_mirror)
    sp.add_argument("--pr", type=int)
    sp = add("rebuild", v_rebuild)
    sp.add_argument("--release-pr", type=int)
    add("lock", v_lock)
    add("unlock", v_unlock)
    add("selftest", v_selftest)

    a = p.parse_args()
    a.fn(Cache(a.cache), a)


if __name__ == "__main__":
    main()
