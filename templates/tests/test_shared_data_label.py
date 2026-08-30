"""The shared solaris-data volume must keep a SHARED SELinux label (#1271).

Four containers of the solaris pod and the wakeword-trainer Quadlet mount
`<data_dir>/solarisbay`. A private relabel (`:Z`) stamps one container's MCS
categories on the whole tree and locks every other pair out of solaris.db —
the 2026-08-30 outage, where chat 500ed on `/napi/*` with correct owner and
mode. The post-deploy asserts the label before the pod is restarted onto a
fresh pair.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
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
    return _load("solaris_pd_selinux", TEMPLATES / "solaris" / "post-deploy.py")


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "solarisbay").mkdir()
    return str(tmp_path)


def _chcon_spy(pd, monkeypatch, returncode=0, stderr=""):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    monkeypatch.setattr(pd.subprocess, "run", fake_run)
    return calls


def test_shared_label_is_left_alone(pd, monkeypatch, data_dir):
    monkeypatch.setattr(
        pd, "read_selinux_label", lambda p: "system_u:object_r:container_file_t:s0"
    )
    calls = _chcon_spy(pd, monkeypatch)
    assert pd.assert_shared_data_label(data_dir) is True
    assert calls == []


def test_a_host_without_selinux_is_not_a_finding(pd, monkeypatch, data_dir):
    monkeypatch.setattr(pd, "read_selinux_label", lambda p: "")
    calls = _chcon_spy(pd, monkeypatch)
    assert pd.assert_shared_data_label(data_dir) is True
    assert calls == []


def test_private_categories_are_reported_and_cleared(pd, monkeypatch, data_dir):
    monkeypatch.setattr(
        pd,
        "read_selinux_label",
        lambda p: "system_u:object_r:container_file_t:s0:c1022,c1023",
    )
    calls = _chcon_spy(pd, monkeypatch)
    assert pd.assert_shared_data_label(data_dir) is False
    assert calls == [["chcon", "-R", "-l", "s0", f"{data_dir}/solarisbay"]]


def test_a_failed_repair_is_not_reported_as_healthy(pd, monkeypatch, data_dir):
    monkeypatch.setattr(
        pd,
        "read_selinux_label",
        lambda p: "system_u:object_r:container_file_t:s0:c1,c2",
    )
    _chcon_spy(pd, monkeypatch, returncode=1, stderr="Operation not permitted")
    assert pd.assert_shared_data_label(data_dir) is False


def test_the_label_is_read_from_the_real_xattr(pd, tmp_path):
    """A real context comes back as `user:role:type:level` with no trailing NUL;
    a host without SELinux (or a path that isn't there) yields "" rather than a
    crash — the caller must never act on a label it could not read."""
    target = tmp_path / "solarisbay"
    target.mkdir()
    label = pd.read_selinux_label(str(target))
    assert label == "" or (label.count(":") >= 3 and "\x00" not in label)
    assert pd.read_selinux_label(str(tmp_path / "missing")) == ""
