"""Every persistent paperless volume has a place in the backup declaration (#1133).

The vault is household paperwork that exists nowhere else. Since ServiceBay
5.32.0 the template itself declares what to keep (`servicebay.backup`,
mdopp/servicebay#2858) and the platform does the backing up — so this test is
what keeps the declaration honest: a volume added to the pod without a place in
it fails here instead of quietly falling out of the backup.

Paperless is the first user of the `pg-dump` collector anywhere, so the object
form and its required fields are asserted too — a bare `collector: pg-dump` is
refused by the platform parser, which would leave the database unbacked.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

TEMPLATE = pathlib.Path(__file__).resolve().parents[1] / "paperless" / "template.yml"

ANNOTATION = "    servicebay.backup: |\n"

# This service's own data dir. Every declared path is relative to it, and the
# platform refuses anything that resolves outside (ADR 0002).
DATA_ROOT = "{{DATA_DIR}}/paperless/"

# Roots ServiceBay holds as bulk (EXCLUDED_BULK_VOLUMES): covered there, and
# never reachable from this declaration.
BULK_ROOTS = ("{{DATA_DIR}}/file-share/data/",)

# The fields the platform parser accepts (zod strictObject — a typo fails loudly).
DECLARED_FIELDS = {
    "dataSubdir",
    "volume",
    "collector",
    "include",
    "exclude",
    "data",
    "strip",
    "transform",
    "stores",
}


@pytest.fixture(scope="module")
def raw() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def backup(raw: str) -> dict:
    # The template as a whole is not valid YAML ({{VAR}} placeholders), but the
    # annotation's block scalar is — that is exactly what ServiceBay parses.
    body = raw.split(ANNOTATION, 1)[1]
    lines = []
    for line in body.splitlines():
        if line.strip() and not line.startswith("      "):
            break
        lines.append(line[6:])
    return yaml.safe_load("\n".join(lines))


@pytest.fixture(scope="module")
def volumes(raw: str) -> dict[str, str]:
    section = raw.split("\n  volumes:\n", 1)[1]
    return dict(
        re.findall(
            r"^  - name: (\S+)\n    hostPath:\n      path: (\S+)$",
            section,
            re.MULTILINE,
        )
    )


def test_declaration_uses_only_fields_the_platform_accepts(backup):
    assert set(backup) <= DECLARED_FIELDS


def test_every_volume_is_included_excluded_or_out_of_reach(backup, volumes):
    covered = set(backup["include"]) | set(backup["exclude"])
    assert volumes, "no hostPath volumes parsed out of the pod"
    for name, path in volumes.items():
        if path.startswith(DATA_ROOT):
            assert path[len(DATA_ROOT) :] in covered, name
        else:
            assert path.startswith(BULK_ROOTS), name
            assert path.rsplit("/", 1)[-1] not in covered, name


def test_declared_paths_stay_inside_the_service_data_dir(backup):
    for field in ("include", "exclude"):
        for path in backup[field]:
            assert not path.startswith(("/", "~")), path
            assert ".." not in path.split("/"), path
            assert "{{" not in path, path


def test_the_originals_and_the_app_data_are_kept(backup):
    assert set(backup["include"]) == {"media", "data"}


def test_the_cluster_dir_and_the_broker_are_dropped(backup):
    assert set(backup["exclude"]) == {"pgdata", "redis"}


def test_the_database_is_dumped_not_file_copied(backup, raw):
    collector = backup["collector"]
    # A bare `collector: pg-dump` is refused by the platform: a dump has to know
    # what to dump, and a collector that never runs ships a backup without a
    # database in it.
    assert collector == {
        "kind": "pg-dump",
        "container": "paperless-postgres",
        "user": "paperless",
        "database": "paperless",
    }
    # The collector names the pod's own postgres container: <pod>-<container>.
    assert "\n  - name: postgres\n" in raw
