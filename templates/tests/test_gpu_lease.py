"""The GPU lease (#1320) and its two profiles (#1319, #1325): what `acquire`
stops, what it swaps, and what `release` gives back.

The unit list is the load-bearing part — a missing `llama.service` leaves
Solaris' own 3.9 GB server loaded and the Qwen run then OOMs on a card measured
full at 15.0 of 16.4 GB. The ordering matters just as much: the lease file is
written before the stop and removed after the model answers again, so there is
no moment when the card is gone and nothing knows it.

The two named profiles are the softer shapes: llama-server is reloaded on the
leased model instead of stopped, so the house keeps an assistant. A coding
lease moves the voice units to the CPU; a foundry lease stops nothing at all,
because foundry transcribes through `solaris-whisper-batch` while it runs.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("llama_pd_lease", TEMPLATES / "llama" / "post-deploy.py")


@pytest.fixture(autouse=True)
def no_box(pd, monkeypatch):
    """Nothing here may reach the box: `systemd-run`, `daemon-reload` and the
    timer stop all go through subprocess. Returns the recorded argv list."""
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        pd.subprocess, "run", lambda argv, **kw: calls.append(list(argv)) or _Done()
    )
    return calls


@pytest.fixture
def systemctl_calls(pd, monkeypatch):
    """Record `(verb, units)` instead of touching the box's units."""
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        pd, "systemctl", lambda verb, units: bool(calls.append((verb, units))) or True
    )
    return calls


def _lease(tmp_path, pd) -> pathlib.Path:
    return tmp_path / "solarisbay" / pd.LEASE_FILE


def test_leased_units_cover_voice_embeddings_and_solaris_own_server(pd):
    assert set(pd.LEASED_UNITS) == {
        "llama-embed.service",
        "solaris-whisper.service",
        "solaris-whisper-batch.service",
        "solaris-tts.service",
        "solaris-wakeword-trainer.service",
        "llama.service",
    }


def test_acquire_stops_exactly_the_leased_units(pd, tmp_path, systemctl_calls):
    assert pd.lease_acquire(str(tmp_path), "coder") == 0
    assert systemctl_calls == [("stop", pd.LEASED_UNITS)]
    written = json.loads(_lease(tmp_path, pd).read_text())
    assert written["holder"] == "coder"
    assert written["since"] > 0


def test_acquire_claims_before_it_stops(pd, tmp_path, monkeypatch):
    held_when_stopping: list[bool] = []
    monkeypatch.setattr(
        pd,
        "systemctl",
        lambda verb, units: (
            bool(held_when_stopping.append(_lease(tmp_path, pd).exists())) or True
        ),
    )
    assert pd.lease_acquire(str(tmp_path), "foundry") == 0
    assert held_when_stopping == [True]


def test_acquire_refuses_a_card_someone_else_holds(pd, tmp_path, systemctl_calls):
    assert pd.lease_acquire(str(tmp_path), "foundry") == 0
    systemctl_calls.clear()
    assert pd.lease_acquire(str(tmp_path), "coder") == 1
    assert systemctl_calls == []
    assert pd.read_lease(str(tmp_path))["holder"] == "foundry"


def test_acquire_is_idempotent_for_the_same_holder(pd, tmp_path, systemctl_calls):
    assert pd.lease_acquire(str(tmp_path), "foundry") == 0
    assert pd.lease_acquire(str(tmp_path), "foundry") == 0


def test_release_starts_everything_and_clears_only_once_warm(
    pd, tmp_path, monkeypatch, systemctl_calls
):
    pd.lease_acquire(str(tmp_path), "foundry")
    systemctl_calls.clear()
    held_while_loading: list[bool] = []
    monkeypatch.setattr(
        pd,
        "wait_for_ready",
        lambda url, deadline_sec: (
            bool(held_while_loading.append(_lease(tmp_path, pd).exists())) or True
        ),
    )
    monkeypatch.setattr(pd, "speculative_active", lambda url: True)

    assert pd.lease_release(str(tmp_path), "11435") == 0
    assert systemctl_calls == [("start", pd.LEASED_UNITS)]
    # The lease still stood while the model was loading — a resident asking
    # during those ~38 s gets the honest sentence, not a connection error.
    assert held_while_loading == [True]
    assert not _lease(tmp_path, pd).exists()


def test_release_clears_the_lease_even_when_the_model_never_comes_back(
    pd, tmp_path, monkeypatch, systemctl_calls
):
    """A stuck llama-server must not mute Solaris forever — staying "busy"
    after the holder has left is the worse lie."""
    pd.lease_acquire(str(tmp_path), "foundry")
    monkeypatch.setattr(pd, "wait_for_ready", lambda url, deadline_sec: False)
    assert pd.lease_release(str(tmp_path), "11435") == 1
    assert not _lease(tmp_path, pd).exists()


def test_lease_file_sits_where_the_chat_pod_mounts_it(pd):
    # templates/solaris/template.yml mounts {{DATA_DIR}}/solarisbay at
    # /var/lib/solaris, which is where solaris_chat.gpu_lease looks for it.
    assert (
        pd.lease_file("/mnt/data/stacks")
        == "/mnt/data/stacks/solarisbay/gpu_lease.json"
    )


# ── #1319: the coding lease ────────────────────────────────────────────────


@pytest.fixture
def swap_box(pd, tmp_path, monkeypatch):
    """A box where the leased weights are already there and llama-server comes
    back up: what is left to observe is which units moved and what was written."""
    systemd_dir = tmp_path / ".config" / "containers" / "systemd"
    systemd_dir.mkdir(parents=True)
    monkeypatch.setattr(
        pd.os.path, "expanduser", lambda p: p.replace("~", str(tmp_path))
    )
    monkeypatch.setattr(pd, "http_request", lambda *a, **k: (0, b""))
    monkeypatch.setattr(
        pd, "download_model", lambda repo, filename, models_dir, stall: True
    )
    monkeypatch.setattr(pd, "gpu_container_is_live_source", lambda: True)
    monkeypatch.setattr(pd, "wait_for_ready", lambda url, deadline_sec: True)
    monkeypatch.setattr(pd, "speculative_active", lambda url: True)
    return systemd_dir / "llama.container"


def test_the_coding_profile_is_the_only_cell_that_measured(pd):
    """`--parallel 1` and q8 KV are not tuning: with llama-server's stock four
    slots, or f16 KV, the drafter OOMs before it loads (#1318 cell H1)."""
    args = pd.server_args("11435", "/models", pd.CODING_PROFILE)
    assert "-m /models/Qwen3.8-27B-UD-IQ3_XXS.gguf" in " ".join(args)
    assert "--spec-draft-model /models/mtp-Qwen3.8-27B-Q4_0.gguf" in " ".join(args)
    assert args[args.index("-c") + 1] == "81920"
    assert args[args.index("-ctk") + 1] == "q8_0"
    assert args[args.index("-ctv") + 1] == "q8_0"
    assert args[args.index("--parallel") + 1] == "1"


def test_the_coding_profile_switches_thinking_off_at_the_server(pd):
    """Box-measured 2026-09-06 (#1321): with tools in the request and the flag
    missing, Qwen answers a one-line request with 222 generated tokens, 200 of
    them an invisible `reasoning_content` trace; with it, 35. goose aborts the
    run when a reply hits its output-token limit, so the trace does not just
    cost time. `solaris-chat` sends the same switch per request — the leased
    server is driven by tools that do not, so it has to default to it."""
    args = pd.server_args("11435", "/models", pd.CODING_PROFILE)
    assert args[args.index("--reasoning") + 1] == "off"


def test_the_household_profile_is_unchanged_by_the_new_knobs(pd):
    """A household unit that re-renders differently would restart llama-server
    on every deploy for nothing."""
    args = pd.server_args("11435", "/models")
    assert "-ctk" not in args and "-ctv" not in args and "--parallel" not in args
    assert "--reasoning" not in args


def test_coding_acquire_keeps_the_voice_units_running_on_the_cpu(
    pd, tmp_path, swap_box, systemctl_calls
):
    assert pd.lease_acquire(str(tmp_path), "coder", "11435", "coding", 3600) == 0
    stopped = [units for verb, units in systemctl_calls if verb == "stop"]
    assert stopped == [pd.LEASE_GPU_UNITS]
    assert "solaris-whisper.service" not in sum((list(u) for u in stopped), [])
    assert ("restart", pd.LEASE_VOICE_UNITS) in systemctl_calls
    env_file = tmp_path / "solarisbay" / pd.VOICE_DEVICE_FILE
    assert env_file.read_text() == "WHISPER_DEVICE=cpu\nKOKORO_ONNX_PROVIDER=cpu\n"


def test_coding_acquire_swaps_the_server_instead_of_stopping_it(
    pd, tmp_path, swap_box, systemctl_calls
):
    assert pd.lease_acquire(str(tmp_path), "coder", "11435", "coding", 3600) == 0
    unit = swap_box.read_text()
    assert "Qwen3.8-27B-UD-IQ3_XXS.gguf" in unit
    assert "--parallel 1" in unit
    assert ("restart", ("llama.service",)) in systemctl_calls
    assert ("stop", ("llama.service",)) not in systemctl_calls


def test_the_lease_says_it_is_still_loading_until_the_model_answers(
    pd, tmp_path, monkeypatch, swap_box, systemctl_calls
):
    """Mode B only holds once the coding model serves: during the swap there is
    no model at all, and the Engine has to keep saying so."""
    ready_when_restarting: list[object] = []
    monkeypatch.setattr(
        pd,
        "wait_for_ready",
        lambda url, deadline_sec: (
            ready_when_restarting.append(pd.read_lease(str(tmp_path)).get("ready"))
            or True
        ),
    )
    assert pd.lease_acquire(str(tmp_path), "coder", "11435", "coding", 3600) == 0
    assert ready_when_restarting == [False]
    lease = pd.read_lease(str(tmp_path))
    assert lease["ready"] is True
    assert lease["mode"] == "coding"
    assert lease["model"] == "Qwen 3.8 27B"


def test_every_lease_carries_a_deadline_and_arms_the_expiry(
    pd, tmp_path, swap_box, systemctl_calls, no_box
):
    """#1260's lesson: an end signal alone is not enough. A run that dies
    without releasing must not leave the household on the coding model."""
    before = pd.time.time()
    assert pd.lease_acquire(str(tmp_path), "coder", "11435", "coding", 3600) == 0
    lease = pd.read_lease(str(tmp_path))
    assert before + 3600 <= lease["until"] <= pd.time.time() + 3600
    armed = [c for c in no_box if c and c[0] == "systemd-run"]
    assert armed, "no expiry timer was armed"
    assert f"--unit={pd.LEASE_EXPIRY_UNIT}" in armed[0]
    # Not at the deadline but at the grace of two missed renewals (#1361).
    assert "--on-active=2400" in armed[0]
    assert armed[0][-1] == "release"


def test_the_grace_is_two_missed_renewals_and_never_past_the_deadline(pd):
    """#1361: a holder that dies without a DELETE must lose the card in
    minutes, not hours — but a window can still never outlive its own TTL."""
    assert pd.renew_after(900) == 300
    assert pd.expiry_wake(900) == 600
    assert pd.renew_after(14400) == 4800
    assert pd.expiry_wake(14400) == 9600
    # Windows too short for the third to clear the 60 s floor: the deadline
    # itself is the wake, so nothing is armed past it.
    assert pd.expiry_wake(120) == 120
    assert pd.expiry_wake(180) == 120


def test_a_holder_that_keeps_renewing_keeps_its_window(
    pd, tmp_path, swap_box, systemctl_calls, no_box
):
    """The re-arm is the heartbeat: every renewal cancels the pending release
    and arms the next grace, so a live holder is never released underneath."""
    pd.lease_acquire(str(tmp_path), "pi-web", "11435", "coding", 900)
    first = pd.read_lease(str(tmp_path))["last_renewed_at"]
    no_box.clear()
    pd.lease_acquire(str(tmp_path), "pi-web", "11435", "coding", 900)
    lease = pd.read_lease(str(tmp_path))
    assert lease["last_renewed_at"] >= first
    assert lease["renew_after"] == 300
    armed = [c for c in no_box if c and c[0] == "systemd-run"]
    assert armed and "--on-active=600" in armed[0]


def test_the_lease_records_the_heartbeat_the_engine_reports(
    pd, tmp_path, swap_box, systemctl_calls
):
    """`GET /api/model-lease` answers these two straight out of the file, so
    the holder can see how long its window survives its own silence."""
    before = pd.time.time()
    pd.lease_acquire(str(tmp_path), "pi-web", "11435", "coding", 900)
    lease = pd.read_lease(str(tmp_path))
    assert before <= lease["last_renewed_at"] <= pd.time.time()
    assert lease["renew_after"] == pd.renew_after(900)


def test_coding_release_puts_the_household_model_and_the_gpu_voice_back(
    pd, tmp_path, swap_box, systemctl_calls
):
    pd.lease_acquire(str(tmp_path), "coder", "11435", "coding", 3600)
    systemctl_calls.clear()
    assert pd.lease_release(str(tmp_path), "11435") == 0
    assert ("start", pd.LEASE_GPU_UNITS) in systemctl_calls
    assert ("restart", pd.LEASE_VOICE_UNITS) in systemctl_calls
    env_file = tmp_path / "solarisbay" / pd.VOICE_DEVICE_FILE
    assert env_file.read_text() == "WHISPER_DEVICE=cuda\nKOKORO_ONNX_PROVIDER=cuda\n"
    assert "gemma-4-E4B-it-Q4_0.gguf" in swap_box.read_text()
    assert not _lease(tmp_path, pd).exists()


def test_release_reloads_the_profile_that_was_installed_not_the_default(
    pd, tmp_path, monkeypatch, swap_box, systemctl_calls
):
    """An operator who deployed other weights gets those back, not this
    script's defaults."""
    monkeypatch.setenv("LLAMA_MODEL_FILE", "gemma-4-12B-it-Q4_0.gguf")
    pd.save_household_profile(str(tmp_path))
    monkeypatch.delenv("LLAMA_MODEL_FILE")
    pd.lease_acquire(str(tmp_path), "coder", "11435", "coding", 3600)
    pd.lease_release(str(tmp_path), "11435")
    assert "gemma-4-12B-it-Q4_0.gguf" in swap_box.read_text()


def test_missing_coding_weights_stop_nothing(pd, tmp_path, monkeypatch, swap_box):
    """A 12.6 GB download is not something to do with the house muted — the
    weights are fetched before anything is stopped, and a failure is a no-op."""
    monkeypatch.setattr(pd, "download_model", lambda *a: False)
    monkeypatch.setattr(
        pd, "systemctl", lambda verb, units: pytest.fail("stopped a unit anyway")
    )
    assert pd.lease_acquire(str(tmp_path), "coder", "11435", "coding", 3600) == 1
    assert not _lease(tmp_path, pd).exists()


def test_an_unknown_model_is_refused_rather_than_run_exclusively(
    pd, tmp_path, systemctl_calls
):
    assert pd.lease_acquire(str(tmp_path), "coder", "11435", "qwen", 3600) == 2
    assert systemctl_calls == []


def test_the_cli_reads_the_holder_the_model_and_the_duration(pd, monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        pd,
        "lease_acquire",
        lambda d, h, p, m, s: seen.update(holder=h, model=m, seconds=s) or 0,
    )
    assert (
        pd.lease_cli(["acquire", "coder", "--model", "coding", "--duration", "4h"]) == 0
    )
    assert seen == {"holder": "coder", "model": "coding", "seconds": 14400}


def test_a_lease_without_a_duration_still_gets_one(pd, monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        pd, "lease_acquire", lambda d, h, p, m, s: seen.update(seconds=s) or 0
    )
    assert pd.lease_cli(["acquire", "foundry"]) == 0
    assert seen == {"seconds": pd.LEASE_DEFAULT_DURATION_SEC}


def test_a_deploy_during_a_lease_leaves_the_leased_server_alone(
    pd, tmp_path, monkeypatch
):
    """The unit belongs to the lease for its duration: rewriting it would
    restart llama-server into a card the coding run has filled."""
    monkeypatch.setattr(
        pd,
        "install_gpu_quadlet_fallback",
        lambda *a: pytest.fail("took the card back mid-lease"),
    )
    monkeypatch.setattr(
        pd, "wait_for_ready", lambda *a, **k: pytest.fail("waited on a leased server")
    )
    monkeypatch.setattr(pd, "download_model", lambda *a: True)
    monkeypatch.setattr(pd, "install_lease_script", lambda d: "/x/gpu-lease.py")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    pd.write_lease(str(tmp_path), {"holder": "coder", "mode": "coding"})
    assert pd.main() == 0


def test_both_templates_agree_on_the_voice_env_contract(pd):
    """The lease writes this file; templates/solaris/post-deploy.py's Quadlets
    read it. Two files, one contract — so it is pinned here."""
    solaris_pd = _load("solaris_pd_voice", TEMPLATES / "solaris" / "post-deploy.py")
    assert solaris_pd.VOICE_DEVICE_FILE == pd.VOICE_DEVICE_FILE
    assert solaris_pd.VOICE_DEVICE_ENV == pd.VOICE_DEVICE_ENV
    assert solaris_pd.GPU_LEASE_FILE == pd.LEASE_FILE


def test_a_cpu_box_is_refused_rather_than_silently_left_on_the_household_model(
    pd, tmp_path, monkeypatch, swap_box, systemctl_calls
):
    """The swap rewrites llama.container; where llama.service still comes from
    the deployed .kube unit that file is inert, and the restart would bring the
    household model back up as if nothing had happened."""
    monkeypatch.setattr(pd, "gpu_container_is_live_source", lambda: False)
    assert pd.lease_acquire(str(tmp_path), "coder", "11435", "coding", 3600) == 1
    assert systemctl_calls == []
    assert not _lease(tmp_path, pd).exists()


# ── #1325: the foundry lease ───────────────────────────────────────────────


def test_the_foundry_profile_is_the_cell_that_measured(pd):
    """#1318 cell K2: the 12B Q4_0 with its MTP drafter, four slots and f16 KV
    at 32k measured 9 626 MiB — 14 144 of 16 380 beside the voice stack under
    load (4 508). q8 KV would buy 872 MiB back and cost a fifth of the model's
    throughput; it is not needed, so it is not set."""
    args = pd.server_args("11435", "/models", pd.FOUNDRY_PROFILE)
    assert "-m /models/gemma-4-12B-it-Q4_0.gguf" in " ".join(args)
    assert "--spec-draft-model /models/mtp-gemma-4-12B-it-Q8_0.gguf" in " ".join(args)
    assert args[args.index("-c") + 1] == "32768"
    assert "-ctk" not in args and "--parallel" not in args and "--mmproj" not in args


def test_foundry_acquire_stops_nothing_and_leaves_the_voice_on_the_gpu(
    pd, tmp_path, swap_box, systemctl_calls
):
    """The whole point of the profile: foundry transcribes through
    `solaris-whisper-batch` all evening, so the five units it would otherwise
    stop are exactly the ones it needs running."""
    assert pd.lease_acquire(str(tmp_path), "foundry", "11435", "foundry", 3600) == 0
    assert [verb for verb, _ in systemctl_calls] == ["restart"]
    assert systemctl_calls == [("restart", ("llama.service",))]
    assert not (tmp_path / "solarisbay" / pd.VOICE_DEVICE_FILE).exists()


def test_foundry_acquire_swaps_only_llama_service(
    pd, tmp_path, swap_box, systemctl_calls
):
    assert pd.lease_acquire(str(tmp_path), "foundry", "11435", "foundry", 3600) == 0
    unit = swap_box.read_text()
    assert "gemma-4-12B-it-Q4_0.gguf" in unit
    assert "mtp-gemma-4-12B-it-Q8_0.gguf" in unit
    lease = pd.read_lease(str(tmp_path))
    assert lease["mode"] == "foundry"
    assert lease["model"] == "Gemma 4 12B"
    assert lease["ready"] is True


def test_foundry_acquire_leaves_the_embeddings_server_alone(
    pd, tmp_path, swap_box, systemctl_calls
):
    """9 636 MiB for the 12B, 4 508 for the voice stack and 300 for the
    embeddings server is 14 444 of a 16 380 card — so the household keeps its
    semantic vault search for the whole evening (#1332)."""
    assert pd.lease_acquire(str(tmp_path), "foundry", "11435", "foundry", 3600) == 0
    assert [verb for verb, _ in systemctl_calls] == ["restart"]
    assert not any(pd.EMBED_UNIT in units for _, units in systemctl_calls)


def test_coding_acquire_stops_the_embeddings_server(
    pd, tmp_path, swap_box, systemctl_calls
):
    """The coding profile peaks at 15 700 of 16 380 MiB — the embeddings
    server's 300 MB is the difference between the drafter loading and not."""
    assert pd.lease_acquire(str(tmp_path), "coder", "11435", "coding", 3600) == 0
    assert ("stop", pd.LEASE_GPU_UNITS) in systemctl_calls
    assert pd.EMBED_UNIT in pd.LEASE_GPU_UNITS


def test_foundry_release_restores_e4b_without_touching_other_units(
    pd, tmp_path, swap_box, systemctl_calls
):
    pd.lease_acquire(str(tmp_path), "foundry", "11435", "foundry", 3600)
    systemctl_calls.clear()
    assert pd.lease_release(str(tmp_path), "11435") == 0
    assert "gemma-4-E4B-it-Q4_0.gguf" in swap_box.read_text()
    # Nothing was stopped, so nothing may be started behind the units' backs.
    assert [verb for verb, _ in systemctl_calls] == ["restart"]
    assert not (tmp_path / "solarisbay" / pd.VOICE_DEVICE_FILE).exists()
    assert not _lease(tmp_path, pd).exists()


def test_coding_release_starts_the_embeddings_server_again(
    pd, tmp_path, swap_box, systemctl_calls
):
    pd.lease_acquire(str(tmp_path), "coder", "11435", "coding", 3600)
    systemctl_calls.clear()
    assert pd.lease_release(str(tmp_path), "11435") == 0
    assert ("start", pd.LEASE_GPU_UNITS) in systemctl_calls


def test_a_foundry_lease_expires_back_to_the_household_model(
    pd, tmp_path, swap_box, systemctl_calls, no_box
):
    assert pd.lease_acquire(str(tmp_path), "foundry", "11435", "foundry", 3600) == 0
    armed = [c for c in no_box if c and c[0] == "systemd-run"]
    assert armed and "--on-active=2400" in armed[0] and armed[0][-1] == "release"


def test_the_cli_knows_the_foundry_model(pd, monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        pd,
        "lease_acquire",
        lambda d, h, p, m, s: seen.update(holder=h, model=m, seconds=s) or 0,
    )
    assert pd.lease_cli(["acquire", "foundry", "--model", "foundry"]) == 0
    assert seen["model"] == "foundry"


# ── #1333: the HTTP lease — --alias, the renewal, and the host broker ──────


def test_every_profile_names_itself_in_the_v1_responses(pd):
    """foundry reads the `model` field of the answer to record which model
    wrote a chronicle entry; without `--alias` that field is a GGUF path."""
    assert "--alias gemma-4-e4b" in " ".join(pd.server_args("11435", "/models"))
    assert "--alias qwen3.8-27b" in " ".join(
        pd.server_args("11435", "/models", pd.CODING_PROFILE)
    )
    assert "--alias gemma-4-12b" in " ".join(
        pd.server_args("11435", "/models", pd.FOUNDRY_PROFILE)
    )


def test_both_argv_sources_carry_the_alias(pd):
    """`server_args` renders the Quadlet on a GPU box, template.yml the kube
    unit everywhere else — a name that only one of them sets is a name a
    neighbour cannot rely on."""
    tmpl = (TEMPLATES / "llama" / "template.yml").read_text(encoding="utf-8")
    assert '- "--alias"' in tmpl
    assert '- "{{LLAMA_MODEL_ALIAS}}"' in tmpl
    variables = json.loads(
        (TEMPLATES / "llama" / "variables.json").read_text(encoding="utf-8")
    )
    assert variables["LLAMA_MODEL_ALIAS"]["default"] == "gemma-4-e4b"
    assert pd.env_profile()["alias"] == variables["LLAMA_MODEL_ALIAS"]["default"]


def test_the_lease_records_the_alias_the_holder_will_be_answered_by(
    pd, tmp_path, swap_box, systemctl_calls
):
    pd.lease_acquire(str(tmp_path), "foundry", "11435", "foundry", 3600)
    assert pd.read_lease(str(tmp_path))["alias"] == "gemma-4-12b"


def test_a_renewal_moves_the_deadline_without_swapping_again(
    pd, tmp_path, swap_box, systemctl_calls, no_box
):
    """The holder renews every few minutes. Reloading llama-server each time
    would cost the household a cold load per renewal — the deadline moves, the
    server does not."""
    pd.lease_acquire(str(tmp_path), "foundry", "11435", "foundry", 3600)
    first_until = pd.read_lease(str(tmp_path))["until"]
    systemctl_calls.clear()
    no_box.clear()
    assert pd.lease_acquire(str(tmp_path), "foundry", "11435", "foundry", 7200) == 0
    assert systemctl_calls == []
    assert pd.read_lease(str(tmp_path))["until"] > first_until
    armed = [c for c in no_box if c and c[0] == "systemd-run"]
    assert armed and "--on-active=4800" in armed[0]


def _request(pd, tmp_path, **fields) -> None:
    path = pathlib.Path(pd.request_file(str(tmp_path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields))


def test_the_broker_acquires_what_the_engine_asked_for(
    pd, tmp_path, swap_box, systemctl_calls
):
    _request(
        pd,
        tmp_path,
        op="acquire",
        model="foundry",
        ttl_s=900,
        holder="foundry",
        requested_at=1.5,
    )
    assert pd.broker_run(str(tmp_path), "11435") == 0
    lease = pd.read_lease(str(tmp_path))
    assert lease["mode"] == "foundry" and lease["holder"] == "foundry"
    status = json.loads(pathlib.Path(pd.status_file(str(tmp_path))).read_text())
    # The requested_at goes back unchanged — that is how the HTTP side knows
    # this request has been dealt with and is not still "preparing".
    assert status["requested_at"] == 1.5
    assert status["state"] == "ready"
    assert status["alias"] == "gemma-4-12b"
    assert status["expires_at"] == lease["until"]


def test_the_broker_files_the_window_under_the_service_that_asked(
    pd, tmp_path, swap_box, systemctl_calls
):
    """#1347: the Engine passes the caller's own name through, so the lease on
    the box says who holds it and a stranger's `release` is refused here too."""
    _request(
        pd,
        tmp_path,
        op="acquire",
        model="foundry",
        ttl_s=900,
        holder="foundry-chronicle",
        requested_at=1.75,
    )
    assert pd.broker_run(str(tmp_path), "11435") == 0
    assert pd.read_lease(str(tmp_path))["holder"] == "foundry-chronicle"
    status = json.loads(pathlib.Path(pd.status_file(str(tmp_path))).read_text())
    assert status["holder"] == "foundry-chronicle"
    # An acquire without a holder stays what it has always been: the profile.
    pathlib.Path(pd.lease_file(str(tmp_path))).unlink()
    _request(pd, tmp_path, op="acquire", model="foundry", ttl_s=900, requested_at=1.85)
    assert pd.broker_run(str(tmp_path), "11435") == 0
    assert pd.read_lease(str(tmp_path))["holder"] == "foundry"


def test_the_broker_releases_and_says_which_model_is_back(
    pd, tmp_path, swap_box, systemctl_calls
):
    pd.lease_acquire(str(tmp_path), "foundry", "11435", "foundry", 3600)
    _request(pd, tmp_path, op="release", model="", requested_at=2.5)
    assert pd.broker_run(str(tmp_path), "11435") == 0
    assert not _lease(tmp_path, pd).exists()
    status = json.loads(pathlib.Path(pd.status_file(str(tmp_path))).read_text())
    assert status["state"] == "released"
    assert status["alias"] == "gemma-4-e4b"
    assert status["requested_at"] == 2.5


def test_a_failed_acquire_is_reported_rather_than_left_pending(
    pd, tmp_path, monkeypatch, swap_box
):
    """A holder polling GET must find out; a silent failure would leave it
    waiting for a window that is never coming."""
    monkeypatch.setattr(pd, "download_model", lambda *a: False)
    _request(pd, tmp_path, op="acquire", model="foundry", ttl_s=900, requested_at=3.5)
    assert pd.broker_run(str(tmp_path), "11435") == 0
    status = json.loads(pathlib.Path(pd.status_file(str(tmp_path))).read_text())
    assert status["state"] == "error"
    assert status["expires_at"] is None


def test_an_unknown_request_never_reaches_the_units(pd, tmp_path, systemctl_calls):
    _request(pd, tmp_path, op="acquire", model="llama5", requested_at=4.5)
    assert pd.broker_run(str(tmp_path), "11435") == 0
    assert systemctl_calls == []
    assert not _lease(tmp_path, pd).exists()


def test_no_request_at_all_is_a_no_op(pd, tmp_path, systemctl_calls):
    assert pd.broker_run(str(tmp_path), "11435") == 0
    assert systemctl_calls == []
    assert not pathlib.Path(pd.status_file(str(tmp_path))).exists()


def test_the_broker_units_watch_the_file_the_engine_writes(pd, tmp_path):
    path_unit, service_unit = render = pd.render_broker_units(
        str(tmp_path), "11435", "/x/gpu-lease.py"
    )
    assert len(render) == 2
    assert f"PathChanged={pd.request_file(str(tmp_path))}" in path_unit
    assert f"Unit={pd.BROKER_UNIT}.service" in path_unit
    assert "/x/gpu-lease.py broker" in service_unit
    assert f"Environment=DATA_DIR={tmp_path}" in service_unit


def test_installing_the_broker_enables_the_watcher(pd, tmp_path, monkeypatch, no_box):
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        pd, "systemctl", lambda verb, units: bool(calls.append((verb, units))) or True
    )
    monkeypatch.setattr(
        pd.os.path, "expanduser", lambda p: p.replace("~", str(tmp_path))
    )
    pd.install_broker_units(str(tmp_path), "11435", "/x/gpu-lease.py")
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    assert (unit_dir / f"{pd.BROKER_UNIT}.path").exists()
    assert (unit_dir / f"{pd.BROKER_UNIT}.service").exists()
    assert calls == [("enable", ("--now", f"{pd.BROKER_UNIT}.path"))]
