"""Every persistent paperless volume is classified in the backup contract (#1133).

The vault is household paperwork that exists nowhere else, and ServiceBay's own
backup contract cannot reach this template: manifests are code in the platform
and its coverage gate only scans templates shipped from the servicebay repo
(mdopp/servicebay#2849). So the classification lives in the template's
`solaris.backup-contract` annotation, and this test is what keeps it honest — a
volume added to the pod without a line in the contract fails here instead of
quietly falling out of the backup.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

TEMPLATE = pathlib.Path(__file__).resolve().parents[1] / "paperless" / "template.yml"

CLASSES = ("include", "dump", "keep", "exclude")


@pytest.fixture(scope="module")
def raw() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def contract(raw: str) -> dict:
    # The template as a whole is not valid YAML ({{VAR}} placeholders), but the
    # annotation's block scalar is.
    body = raw.split("    solaris.backup-contract: |\n", 1)[1]
    lines = []
    for line in body.splitlines():
        if line.strip() and not line.startswith("      "):
            break
        lines.append(line[6:])
    return yaml.safe_load("\n".join(lines))


@pytest.fixture(scope="module")
def volume_names(raw: str) -> list[str]:
    volumes = raw.split("\n  volumes:\n", 1)[1]
    return re.findall(r"^  - name: (\S+)$", volumes, re.MULTILINE)


def test_every_volume_is_classified(contract, volume_names):
    classified = [name for cls in CLASSES for name in contract.get(cls, {})]
    assert sorted(classified) == sorted(volume_names)
    assert len(classified) == len(set(classified))


def test_every_classification_carries_a_reason(contract):
    for cls in CLASSES:
        for name, reason in contract.get(cls, {}).items():
            assert isinstance(reason, str) and len(reason) > 20, name


def test_originals_and_app_data_are_backed_up(contract):
    assert set(contract["include"]) == {"paperless-media", "paperless-data"}


def test_the_database_is_captured_as_a_dump_not_a_file_copy(contract):
    assert set(contract["dump"]) == {"paperless-pgdata"}
    assert "pg_dump" in contract["dump"]["paperless-pgdata"]
