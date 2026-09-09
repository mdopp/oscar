"""`pi-web-project` — one delegated read-only ServiceBay token per project (#1395).

Everything asserted here fails silently in production if it is wrong:

* **The request shaping.** A delegate call with the wrong body or the wrong
  Authorization header comes back 4xx, and the only symptom is a project whose
  agent "cannot see the box" — indistinguishable from a network problem.
* **The ownership rule.** `/workspace` is full of hand-cloned checkouts. A
  `remove` that guessed would revoke somebody else's credential, or report a
  clean take-back having revoked nothing.
* **That remove deletes no files.** The one thing a wrong answer here destroys
  is uncommitted work, and no test after the fact can bring it back.

The HTTP layer is a fake throughout: these are the decisions, not the wire.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
ROOT = TEMPLATES.parent
HELPER = ROOT / "pi-web" / "pi_web_project.py"

PARENT = "sb_00000000_PARENTSECRET"
CHILD = "sb_abcd1234_CHILDSECRET"
OTHER = "sb_99999999_OTHERSECRET"


@pytest.fixture(scope="module")
def helper():
    spec = importlib.util.spec_from_file_location("pi_web_project", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["pi_web_project"] = module
    spec.loader.exec_module(module)
    return module


class FakeHttp:
    """Records every call and answers from a scripted queue per method."""

    def __init__(self, **answers):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.calls: list[dict] = []

    def __call__(self, url, method="GET", token="", payload=None, timeout=15.0):
        self.calls.append(
            {"url": url, "method": method, "token": token, "payload": payload}
        )
        queue = self.answers.get(method)
        if not queue:
            raise AssertionError(f"unexpected {method} {url}")
        return queue.pop(0)


@pytest.fixture
def box(tmp_path):
    """A workspace with one project folder, and a parent token on disk."""
    workspace = tmp_path / "workspace"
    (workspace / "solarisbay").mkdir(parents=True)
    entries = tmp_path / "data" / "servicebay" / "projects"
    entries.mkdir(parents=True)
    parent = tmp_path / "data" / "servicebay" / "parent-token"
    parent.write_text(PARENT + "\n", encoding="utf-8")
    return {
        "workspace": str(workspace),
        "entry_dir": str(entries),
        "parent_token_file": str(parent),
        "api": "http://host.containers.internal:5888",
    }


def entry_file(box, name="solarisbay"):
    return pathlib.Path(box["entry_dir"]) / f"{name}.json"


# ── request and response shaping ────────────────────────────────────────────


def test_add_delegates_a_read_only_child_named_for_the_project(helper, box):
    http = FakeHttp(POST=[(200, {"secret": CHILD, "token": {"id": "abcd1234"}})])
    result = helper.add_project(box, "solarisbay", http)

    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == (
        "http://host.containers.internal:5888/api/system/api-tokens/delegate"
    )
    # The parent token is the credential — the route has no session to fall back
    # on and mints under whoever presented it.
    assert call["token"] == PARENT
    assert call["payload"] == {"name": "pi-web project solarisbay", "scopes": ["read"]}
    assert result["token_id"] == "abcd1234"


def test_a_wider_scope_is_never_asked_for(helper, box):
    """`read` is the whole standing grant; anything more is a human decision."""
    http = FakeHttp(POST=[(200, {"secret": CHILD})])
    helper.add_project(box, "solarisbay", http)
    assert http.calls[0]["payload"]["scopes"] == ["read"]


def test_a_refused_delegation_writes_no_entry(helper, box):
    """An entry naming a token that was never minted is the one state the
    ownership rule cannot survive."""
    http = FakeHttp(POST=[(403, {"error": "parent token unknown"})])
    with pytest.raises(helper.Refused) as refusal:
        helper.add_project(box, "solarisbay", http)
    assert "parent token unknown" in refusal.value.detail
    assert not entry_file(box).exists()


def test_a_non_sb_secret_is_refused_rather_than_recorded(helper, box):
    """A 200 carrying junk would otherwise be stored and fail every later read
    with a 401 that points nowhere."""
    http = FakeHttp(POST=[(200, {"secret": "not-a-token"})])
    with pytest.raises(helper.Refused):
        helper.add_project(box, "solarisbay", http)
    assert not entry_file(box).exists()


def test_the_entry_is_the_record_and_is_written_private(helper, box):
    http = FakeHttp(POST=[(200, {"secret": CHILD})])
    helper.add_project(box, "solarisbay", http)
    path = entry_file(box)
    assert path.stat().st_mode & 0o777 == 0o600
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["token"] == CHILD
    assert entry["project"] == "solarisbay"
    assert entry["url"] == box["api"]


def test_re_adding_revokes_the_previous_token_first(helper, box):
    """Re-add is where an orphan would appear: the old child is live, and after
    the overwrite nothing would name it any more."""
    entry_file(box).write_text(json.dumps({"token": OTHER}), encoding="utf-8")
    http = FakeHttp(DELETE=[(200, {"ok": True})], POST=[(200, {"secret": CHILD})])
    helper.add_project(box, "solarisbay", http)
    assert [c["method"] for c in http.calls] == ["DELETE", "POST"]
    assert http.calls[0]["url"].endswith("delegate?id=99999999")
    assert json.loads(entry_file(box).read_text(encoding="utf-8"))["token"] == CHILD


def test_a_project_folder_that_is_not_there_is_refused(helper, box):
    http = FakeHttp()
    with pytest.raises(helper.Refused):
        helper.add_project(box, "never-cloned", http)
    assert http.calls == []


def test_nothing_outside_the_workspace_can_be_named(helper, box):
    """A traversal would put an entry — and a token — against a path PI WEB
    never shows."""
    for target in ("../etc", "/etc/passwd", "nested/deeper"):
        with pytest.raises(helper.Refused):
            helper.resolve_project(box["workspace"], target)


def test_no_container_token_at_all_is_said_plainly(helper, box, tmp_path):
    box = {**box, "parent_token_file": str(tmp_path / "absent")}
    with pytest.raises(helper.Refused) as refusal:
        helper.add_project(box, "solarisbay", FakeHttp())
    assert "PI_WEB_SB_TOKEN" in str(refusal.value)


# ── the ownership rule ──────────────────────────────────────────────────────


def test_a_hand_cloned_project_is_left_alone(helper, box):
    """The whole point: `/workspace` is full of checkouts this tool never
    touched, and it revokes nothing it did not issue."""
    http = FakeHttp()
    with pytest.raises(helper.Refused) as refusal:
        helper.remove_project(box, "solarisbay", http)
    assert refusal.value.code == 2
    assert http.calls == []
    assert pathlib.Path(box["workspace"], "solarisbay").is_dir()


def test_an_unreadable_entry_is_not_read_as_unmanaged(helper, box):
    """ "We could not look" is not "not ours" — reporting a clean removal there
    would leave a live credential nobody knows about."""
    entry_file(box).write_text("{not json", encoding="utf-8")
    with pytest.raises(helper.Refused):
        helper.remove_project(box, "solarisbay", FakeHttp())
    assert entry_file(box).exists()


def test_list_marks_which_projects_are_ours(helper, box):
    (pathlib.Path(box["workspace"]) / "hand-cloned").mkdir()
    entry_file(box).write_text(json.dumps({"token": CHILD}), encoding="utf-8")
    rows = {row["project"]: row["token_id"] for row in helper.list_projects(box)}
    assert rows == {"solarisbay": "abcd1234", "hand-cloned": ""}


# ── remove semantics ────────────────────────────────────────────────────────


def test_remove_revokes_exactly_this_projects_token_and_drops_the_entry(helper, box):
    entry_file(box).write_text(json.dumps({"token": CHILD}), encoding="utf-8")
    http = FakeHttp(DELETE=[(200, {"ok": True, "revoked": 1})])
    result = helper.remove_project(box, "solarisbay", http)

    call = http.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"].endswith("/api/system/api-tokens/delegate?id=abcd1234")
    assert call["token"] == PARENT
    assert result["token"] == "revoked"
    assert not entry_file(box).exists()


def test_remove_deletes_no_files(helper, box):
    """A one-click `rm -rf` of a working tree with uncommitted work in it is
    not a thing to add on inference — so it is asserted, twice over."""
    project = pathlib.Path(box["workspace"]) / "solarisbay"
    (project / "unpushed.txt").write_text("work", encoding="utf-8")
    entry_file(box).write_text(json.dumps({"token": CHILD}), encoding="utf-8")

    result = helper.remove_project(box, "solarisbay", FakeHttp(DELETE=[(200, {})]))
    assert result["checkout_deleted"] is False
    assert (project / "unpushed.txt").read_text(encoding="utf-8") == "work"

    source = HELPER.read_text(encoding="utf-8")
    for primitive in ("shutil", "rmtree", "os.rmdir", "unlink"):
        assert primitive not in source, primitive


def test_an_already_revoked_token_still_completes_the_removal(helper, box):
    """A half-finished removal has to be retryable to completion; a 404 is the
    answer "that one is gone", not a failure."""
    entry_file(box).write_text(json.dumps({"token": CHILD}), encoding="utf-8")
    result = helper.remove_project(box, "solarisbay", FakeHttp(DELETE=[(404, {})]))
    assert result["token"] == "already-gone"
    assert not entry_file(box).exists()


def test_a_refused_revoke_keeps_the_entry(helper, box):
    """Revoke first, drop second: the other order strands a live credential
    that nothing records."""
    entry_file(box).write_text(json.dumps({"token": CHILD}), encoding="utf-8")
    with pytest.raises(helper.Refused):
        helper.remove_project(box, "solarisbay", FakeHttp(DELETE=[(500, {})]))
    assert entry_file(box).exists()


# ── reading the box with the project's own token ────────────────────────────


def added(box, name="solarisbay", token=CHILD):
    """The pair `add` writes: the JSON record and the bare token file."""
    entry_file(box, name).write_text(
        json.dumps({"token": token, "url": box["api"]}), encoding="utf-8"
    )
    (pathlib.Path(box["entry_dir"]) / f"{name}.token").write_text(
        token + "\n", encoding="utf-8"
    )


class FakeExec:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, file, argv, env):
        self.calls.append((file, argv, env))


def test_get_runs_the_servicebay_cli_as_this_project(helper, box):
    """#1398: the routes are ServiceBay's CLI's business now. What stays here is
    which token the call runs with — named, never carried."""
    added(box)
    run = FakeExec()
    helper.read_box(box, "solarisbay", ["services", "--json"], run)
    file, argv, env = run.calls[0]
    assert file == "servicebay"
    assert argv == ["servicebay", "services", "--json"]
    assert env["PI_WEB_PROJECT"] == "solarisbay"


def test_a_project_without_an_entry_cannot_read_the_box(helper, box):
    with pytest.raises(helper.Refused) as refusal:
        helper.read_box(box, "solarisbay", ["services"], FakeExec())
    assert refusal.value.code == 2


def test_a_project_whose_token_file_is_missing_is_refused_not_run(helper, box):
    """Without the file the CLI reads, `servicebay` would fall back to the pod's
    own token — a read that silently stopped being this project's."""
    entry_file(box).write_text(json.dumps({"token": CHILD}), encoding="utf-8")
    run = FakeExec()
    with pytest.raises(helper.Refused):
        helper.read_box(box, "solarisbay", ["services"], run)
    assert run.calls == []


def test_the_parent_token_is_never_used_for_a_read(helper, box):
    """The delegated child is what a session holds; a read that fell back to
    the parent would make per-project revocation meaningless."""
    added(box)
    run = FakeExec()
    helper.read_box(box, "solarisbay", ["services"], run)
    _, argv, env = run.calls[0]
    assert PARENT not in " ".join(argv)
    assert PARENT not in "".join(env.values())
    assert "SERVICEBAY_MCP_TOKEN" not in env


def test_the_token_file_is_written_and_taken_away_with_the_entry(helper, box):
    """ServiceBay's CLI takes a token *file* and has no --token flag, so `add`
    writes one beside the entry — and `remove` must not leave it behind."""
    http = FakeHttp(POST=[(200, {"secret": CHILD})], DELETE=[(200, {})])
    helper.add_project(box, "solarisbay", http)
    token_file = pathlib.Path(box["entry_dir"]) / "solarisbay.token"
    assert token_file.read_text(encoding="utf-8").strip() == CHILD
    assert token_file.stat().st_mode & 0o777 == 0o600

    helper.remove_project(box, "solarisbay", http)
    assert not token_file.exists()


# ── the per-project context file ────────────────────────────────────────────


def test_add_leaves_a_pointer_to_the_global_agents_md(helper, box):
    http = FakeHttp(POST=[(200, {"secret": CHILD})])
    result = helper.add_project(box, "solarisbay", http)
    written = pathlib.Path(result["agents_md"])
    assert written == pathlib.Path(box["workspace"]) / "solarisbay" / "AGENTS.md"
    assert "servicebay" in written.read_text(encoding="utf-8")


def test_a_project_that_brought_its_own_context_file_keeps_it(helper, box):
    """Pi takes the first context filename it finds in a directory, so writing
    ours would shadow the project's own conventions."""
    for existing in ("AGENTS.md", "CLAUDE.md"):
        project = pathlib.Path(box["workspace"]) / "solarisbay"
        for stale in project.glob("*.md"):
            stale.unlink()
        (project / existing).write_text("# theirs\n", encoding="utf-8")
        http = FakeHttp(POST=[(200, {"secret": CHILD})], DELETE=[(200, {})])
        result = helper.add_project(box, "solarisbay", http)
        assert result["agents_md"] == ""
        assert (project / existing).read_text(encoding="utf-8") == "# theirs\n"


# ── the token never leaves ──────────────────────────────────────────────────


def test_no_verb_prints_a_secret(helper):
    """Only the 8-hex id is ever shown; `sb_<id>_<secret>` must not reach a
    terminal, a log or a `ps` line."""
    source = HELPER.read_text(encoding="utf-8")
    for banned in ("print(secret", "print(parent", '"token"]}', 'f"{secret'):
        assert banned not in source, banned
    assert "token_id(secret)" in source


def test_nothing_is_adopted_by_being_there(helper):
    """No reconcile, no opt-in marker: `add` is typed by a person. A token that
    appeared because a directory did is one nobody decided to issue."""
    source = HELPER.read_text(encoding="utf-8")
    for gone in ("reconcile", "autostart", "marker"):
        assert gone not in source.lower().split("── the three verbs")[-1], gone
    verbs = helper.build_parser().parse_args(["list"])
    assert verbs.verb == "list"
    with pytest.raises(SystemExit):
        helper.build_parser().parse_args(["reconcile"])
