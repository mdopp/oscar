"""The Pi autoloop in the pi-web pod (#1398 slice B).

Everything asserted here has a wrong answer that looks like a working install:

* **The claim.** Two loops both building the same ticket produces two branches
  and two pull requests, and nothing in either run says anything is wrong. The
  only thing that prevents it is treating a non-201 from `POST /git/refs` — a
  rival claim *or* a transport error — as "not mine".
* **The label boundary.** The set of repositories and the label are the whole
  authorisation model. A loop that polled without them would work tickets
  nobody cut, in repositories nobody offered.
* **A gate that cannot run.** The image carries git and node but no `ruff` and
  no `pytest`. Counting an absent tool as a pass would ship red work as green,
  and the protocol would say "grün" about a command that never executed.
* **The token.** It must reach git and the REST calls but neither this
  process's argv nor Pi's environment.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest
import yaml

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
PI_WEB = TEMPLATES / "pi-web"
ROOT = TEMPLATES.parent
LOOP = ROOT / "pi-web" / "pi_autoloop.py"


@pytest.fixture(scope="module")
def loop():
    spec = importlib.util.spec_from_file_location("pi_autoloop", LOOP)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["pi_autoloop"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pod() -> dict:
    text = (PI_WEB / "template.yml").read_text(encoding="utf-8")
    variables = json.loads((PI_WEB / "variables.json").read_text(encoding="utf-8"))
    for name, spec in variables.items():
        text = text.replace("{{%s}}" % name, str(spec.get("default", "x")))
    text = text.replace("{{PUBLIC_DOMAIN}}", "example.test")
    text = text.replace("{{DATA_DIR}}", "/mnt/data/stacks")
    return yaml.safe_load(text)


@pytest.fixture(scope="module")
def variables() -> dict:
    return json.loads((PI_WEB / "variables.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def autoloop_container(pod) -> dict:
    found = [c for c in pod["spec"]["containers"] if c["name"] == "autoloop"]
    assert found, "the pod runs no autoloop container"
    return found[0]


class FakeApi:
    """Scripted GitHub. Records every call so argv/URL leaks are visible."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls: list[tuple] = []

    def __call__(self, url, method="GET", token="", payload=None, timeout=30.0):
        self.calls.append((method, url, payload, token))
        for (m, fragment), answer in self.answers.items():
            if m == method and fragment in url:
                return answer
        return 200, {}


# ── the claim: the one thing that stops two loops on one ticket ─────────────

SHA = "a" * 40


def test_the_claim_ref_is_the_same_name_the_claude_side_uses(loop):
    assert loop.claim_ref(1398) == "refs/autoloop/claim/1398"


def test_a_created_ref_grants_the_claim(loop):
    api = FakeApi({("POST", "/git/refs"): (201, {"ref": "refs/autoloop/claim/7"})})
    assert loop.acquire_claim("https://api", "o/r", 7, SHA, "t", api) is True
    method, url, payload, _ = api.calls[0]
    assert (method, url) == ("POST", "https://api/repos/o/r/git/refs")
    assert payload == {"ref": "refs/autoloop/claim/7", "sha": SHA}


def test_a_ref_that_already_exists_loses(loop):
    api = FakeApi(
        {("POST", "/git/refs"): (422, {"message": "Reference already exists"})}
    )
    assert loop.acquire_claim("https://api", "o/r", 7, SHA, "t", api) is False


def test_a_transport_error_also_loses(loop):
    """Fail closed: a claim you cannot prove you hold is not yours."""
    api = FakeApi({("POST", "/git/refs"): (0, {"message": "connection refused"})})
    assert loop.acquire_claim("https://api", "o/r", 7, SHA, "t", api) is False


def test_no_claim_is_attempted_without_a_remote_known_target(loop):
    """servicebay#2646: a ref pointed at an object the remote never saw fails
    422 'Object does not exist' — every claim silently absent."""
    api = FakeApi()
    assert loop.acquire_claim("https://api", "o/r", 7, "", "t", api) is False
    assert loop.acquire_claim("https://api", "o/r", 7, "HEAD", "t", api) is False
    assert api.calls == []


def test_the_claim_is_released_by_deleting_that_same_ref(loop):
    api = FakeApi()
    loop.release_claim("https://api", "o/r", 7, "t", api)
    assert api.calls[0][:2] == (
        "DELETE",
        "https://api/repos/o/r/git/refs/autoloop/claim/7",
    )


def test_a_losing_claim_never_reaches_the_work(loop, monkeypatch):
    """The whole point: a 422 must stop the build, not merely be logged."""
    api = FakeApi(
        {
            ("GET", "/repos/o/r"): (200, {"default_branch": "main"}),
            ("GET", "/git/ref/heads/main"): (200, {"object": {"sha": SHA}}),
            ("GET", "/issues?"): (200, [{"number": 7, "title": "t", "body": ""}]),
            ("GET", "/git/matching-refs/heads/pi/7-"): (200, []),
            ("POST", "/git/refs"): (422, {"message": "Reference already exists"}),
        }
    )
    worked = []
    monkeypatch.setattr(loop, "work_ticket", lambda *a, **k: worked.append(a))
    cfg = loop.config_from_env({"PI_AUTOLOOP_REPOS": "o/r"})
    assert loop.tick(cfg, "t", api) == 0
    assert worked == []


# ── the boundary: only labelled tickets, only listed repositories ───────────


def test_only_the_configured_label_is_polled(loop):
    api = FakeApi({("GET", "/issues?"): (200, [])})
    loop.open_tickets("https://api", "o/r", "pi:ready", "t", api)
    assert "labels=pi%3Aready" in api.calls[0][1]
    assert "state=open" in api.calls[0][1]


def test_pull_requests_are_not_tickets(loop):
    """GitHub's issue list contains pull requests; working one would mean the
    loop rewriting its own output."""
    api = FakeApi(
        {
            ("GET", "/issues?"): (
                200,
                [{"number": 2, "pull_request": {}}, {"number": 1}],
            )
        }
    )
    assert [
        i["number"] for i in loop.open_tickets("https://api", "o/r", "l", "t", api)
    ] == [1]


def test_the_repository_list_is_the_boundary(loop):
    cfg = loop.config_from_env({"PI_AUTOLOOP_REPOS": " a/b , c/d "})
    assert cfg["repos"] == ["a/b", "c/d"]
    assert loop.config_from_env({})["repos"] == ["mdopp/solarisbay"]


def test_a_ticket_that_already_has_a_branch_is_left_alone(loop):
    """The claim is released at the end of a run, so it cannot be what stops
    the next pass — the pushed branch is."""
    api = FakeApi({("GET", "/matching-refs/"): (200, [{"ref": "refs/heads/pi/7-x"}])})
    assert loop.already_worked("https://api", "o/r", 7, "t", api) is True
    assert "matching-refs/heads/pi/7-" in api.calls[0][1]

    empty = FakeApi({("GET", "/matching-refs/"): (200, [])})
    assert loop.already_worked("https://api", "o/r", 7, "t", empty) is False


# ── the limits live in the service ──────────────────────────────────────────


def test_the_loop_is_off_until_the_operator_turns_it_on(loop, variables):
    assert loop.config_from_env({})["enabled"] is False
    assert variables["PI_AUTOLOOP_ENABLED"]["default"] == "false"
    assert loop.config_from_env({"PI_AUTOLOOP_ENABLED": "true"})["enabled"] is True
    assert loop.config_from_env({"PI_AUTOLOOP_ENABLED": "no"})["enabled"] is False


def test_the_defaults_are_the_agreed_ones(loop):
    cfg = loop.config_from_env({})
    assert cfg["label"] == "pi:ready"
    assert cfg["interval_s"] == 300
    assert cfg["time_cap_s"] == 3600


def test_a_nonsense_interval_falls_back_rather_than_crashing_the_loop(loop):
    assert loop.config_from_env({"PI_AUTOLOOP_INTERVAL_S": "bald"})["interval_s"] == 300


# ── gates ───────────────────────────────────────────────────────────────────


def test_a_python_repo_gets_lint_before_tests(loop, tmp_path):
    (tmp_path / "ruff.toml").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    assert loop.detect_gates(tmp_path) == [
        ["ruff", "check", "."],
        ["ruff", "format", "--check", "."],
        ["pytest", "-q"],
    ]


def test_a_node_repo_gets_the_scripts_it_actually_declares(loop, tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "node --test"}}), encoding="utf-8"
    )
    assert loop.detect_gates(tmp_path) == [["npm", "test"]]


def test_a_repo_with_no_gates_produces_none(loop, tmp_path):
    assert loop.detect_gates(tmp_path) == []


def test_a_broken_package_json_does_not_take_the_loop_down(loop, tmp_path):
    (tmp_path / "package.json").write_text("{ not json", encoding="utf-8")
    assert loop.detect_gates(tmp_path) == []


def test_a_missing_tool_is_skipped_and_never_counted_as_green(
    loop, tmp_path, monkeypatch
):
    (tmp_path / "ruff.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(loop.shutil, "which", lambda _name: None)
    results = loop.run_gates(tmp_path)
    assert [r["status"] for r in results] == ["übersprungen", "übersprungen"]
    assert loop.gates_are_red(results) is False


def test_a_red_gate_is_a_red_gate(loop):
    assert loop.gates_are_red([{"command": "x", "status": "rot"}]) is True
    assert loop.gates_are_red([{"command": "x", "status": "grün"}]) is False


# ── the branch, the subject, the protocol ───────────────────────────────────


def test_the_branch_carries_the_issue_number_and_a_readable_slug(loop):
    assert loop.branch_name(1398, "Pi-Autoloop für zugeschnittene Tickets!") == (
        "pi/1398-pi-autoloop-f-r-zugeschnittene-tickets"
    )
    assert loop.branch_name(7, "!!!") == "pi/7-ticket"
    assert len(loop.slug("x" * 200)) <= 40


def test_the_branch_and_its_marker_agree(loop):
    assert loop.branch_name(7, "Title").startswith(
        loop.branch_marker(7).removeprefix("heads/")
    )


def test_the_commit_subject_carries_no_stray_parens(loop):
    """A `(...)` token makes release-please in the target repo run green and cut
    nothing — and the loop cannot know that repo's scope, so it claims none."""
    subject = loop.commit_subject({"number": 7, "title": "fix the thing (again)"})
    assert "(" not in subject and ")" not in subject
    assert subject.startswith("pi: #7 ")


def test_the_protocol_names_model_duration_and_every_gate(loop):
    text = loop.format_protocol(
        "o/r",
        7,
        "qwen3.8-27b",
        412.7,
        [{"command": "ruff check .", "status": "grün"}],
        {"events": 40, "tools": {"bash": 3}, "tool_errors": 1, "last": "agent_end"},
        "pi/7-x",
        "https://github.com/o/r/pull/9",
    )
    assert "qwen3.8-27b" in text
    assert "412 s" in text
    assert "grün" in text and "ruff check ." in text
    assert "bash×3" in text
    assert "führt nichts zusammen" in text


def test_the_protocol_lands_where_the_readme_says(loop):
    assert loop.protocol_path("/data/pi-web/autoloop", "mdopp/solarisbay", 1398) == (
        pathlib.Path("/data/pi-web/autoloop/mdopp-solarisbay-1398.log")
    )


def test_the_event_summary_reads_the_documented_shapes(loop):
    summary = loop.summarise_events(
        [
            json.dumps({"type": "session", "version": 3}),
            json.dumps(
                {"type": "tool_execution_end", "toolName": "bash", "isError": False}
            ),
            json.dumps(
                {"type": "tool_execution_end", "toolName": "bash", "isError": True}
            ),
            "not json at all",
            json.dumps({"type": "agent_end"}),
        ]
    )
    assert summary["events"] == 4
    assert summary["tools"] == {"bash": 2}
    assert summary["tool_errors"] == 1
    assert summary["unparsed"] == 1
    assert summary["last"] == "agent_end"


# ── the token ───────────────────────────────────────────────────────────────


def test_the_token_comes_out_of_the_credential_store_git_already_uses(loop):
    store = (
        "https://oauth2:wrong-host@gitlab.com\n"
        "https://x-access-token:github_pat_abc%2Fdef@github.com\n"
    )
    assert loop.token_from_credentials(store, "github.com") == "github_pat_abc/def"
    assert loop.token_from_credentials("", "github.com") == ""


def test_the_autoloop_container_never_carries_the_token(autoloop_container):
    names = {e["name"] for e in autoloop_container.get("env", [])}
    assert "PI_WEB_GIT_TOKEN" not in names
    assert "PI_WEB_SB_TOKEN" not in names


def test_the_pi_subprocess_gets_the_ticket_and_not_the_token(
    loop, monkeypatch, tmp_path
):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        raise OSError("not started in the test")

    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    loop.run_pi("do the ticket", "qwen3.8-27b", tmp_path, 60)
    assert seen["cmd"][:3] == ["pi", "--mode", "json"]
    assert "--model" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == "solaris-llama/qwen3.8-27b"
    assert seen["kwargs"]["timeout"] == 60
    assert not any(
        "github_pat" in str(a) or "token" in str(a).lower() for a in seen["cmd"]
    )


# ── the pod ─────────────────────────────────────────────────────────────────


def test_the_autoloop_runs_as_its_own_container_on_the_shared_volumes(
    autoloop_container,
):
    assert autoloop_container["args"] == ["pi-web-autoloop"]
    assert "command" not in autoloop_container  # tini stays the entrypoint
    mounts = {m["mountPath"] for m in autoloop_container["volumeMounts"]}
    assert mounts == {"/data", "/workspace"}


def test_the_autoloop_takes_no_gpu_lease(loop):
    """#1392: the coding lease belongs to the Solaris model tile. The loop reads
    which alias is loaded and proceeds — it never asks for a swap."""
    source = LOOP.read_text(encoding="utf-8")
    assert "model-lease" not in source
    cfg = loop.config_from_env({"LLAMA_PORT": "11435"})
    assert cfg["models_url"] == "http://host.containers.internal:11435/v1/models"


def test_the_loop_addresses_llama_the_way_the_isolated_pod_has_to(loop):
    """ADR 0007: this pod has its own netns, so 127.0.0.1 would be itself."""
    url = loop.config_from_env({})["models_url"]
    assert "host.containers.internal" in url
    assert "127.0.0.1" not in url and "localhost" not in url


def test_the_autoloop_knobs_are_all_wired_into_the_container(
    autoloop_container, variables
):
    declared = {e["name"] for e in autoloop_container["env"]}
    assert {
        "PI_AUTOLOOP_ENABLED",
        "PI_AUTOLOOP_REPOS",
        "PI_AUTOLOOP_LABEL",
        "PI_AUTOLOOP_INTERVAL_S",
        "PI_AUTOLOOP_TIME_CAP_S",
    } <= declared
    for name in declared:
        if name.startswith("PI_AUTOLOOP_"):
            assert name in variables, f"{name} has no variables.json entry"
            assert variables[name].get("description")


def test_the_token_scopes_are_written_down_where_the_operator_types_it(variables):
    """Issues read + Pull requests write + Contents write. A token short of any
    of them produces an empty queue or an unopened PR and no other symptom."""
    text = variables["PI_WEB_GIT_TOKEN"]["description"]
    assert "Issues: Read" in text
    assert "Pull requests: Read and write" in text
    assert "Contents: Read and write" in text


def test_the_image_installs_the_loop_as_a_command(loop):
    dockerfile = (ROOT / "pi-web" / "Dockerfile").read_text(encoding="utf-8")
    assert "pi_autoloop.py /usr/local/bin/pi-web-autoloop" in dockerfile


def test_the_readme_tells_the_operator_how_to_hand_pi_a_ticket():
    text = (PI_WEB / "README.md").read_text(encoding="utf-8")
    assert "## Pi-Autoloop" in text
    assert "pi:ready" in text
    assert "422" in text
    assert "/data/pi-web/autoloop/" in text
    assert "Zusammengeführt wird nie etwas" in text
