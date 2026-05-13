#!/usr/bin/env python3
"""Render an OSCAR ServiceBay template (Mustache → YAML) to stdout.

Only the variables OSCAR templates actually use are handled. Conditional
sections (`{{#FLAG}}…{{/FLAG}}`) are kept if the env var is truthy and
dropped otherwise.

Required env (for `oscar-brain`):
  POSTGRES_PASSWORD (generated if absent)
  TZ                (default: Europe/Berlin)

Optional env:
  OLLAMA_ENABLED    (default: yes — cpu-local mode)
  GPU_PASSTHROUGH   (default: empty)
  POSTGRES_PORT     (default: 5432)
  OLLAMA_PORT       (default: 11434)
  QDRANT_PORT       (default: 6333)
  DATA_DIR          (default: /mnt/data/stacks)

Usage:
  scripts/render-template.py oscar-brain > /tmp/oscar-brain.yml
"""

from __future__ import annotations

import os
import pathlib
import re
import secrets
import string
import sys


def _gen_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _vars_for(template: str) -> dict[str, str]:
    if template == "oscar-brain":
        return {
            "POSTGRES_PASSWORD": os.environ.get("POSTGRES_PASSWORD") or _gen_password(),
            "POSTGRES_PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "OLLAMA_PORT": os.environ.get("OLLAMA_PORT", "11434"),
            "QDRANT_PORT": os.environ.get("QDRANT_PORT", "6333"),
            "TZ": os.environ.get("TZ", "Europe/Berlin"),
            "DATA_DIR": os.environ.get("DATA_DIR", "/mnt/data/stacks"),
        }
    raise ValueError(f"unknown template {template!r}")


def _flags_for(template: str) -> dict[str, bool]:
    if template == "oscar-brain":
        return {
            "OLLAMA_ENABLED": _truthy(os.environ.get("OLLAMA_ENABLED", "yes")),
            "GPU_PASSTHROUGH": _truthy(os.environ.get("GPU_PASSTHROUGH", "")),
        }
    raise ValueError(f"unknown template {template!r}")


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("yes", "true", "1", "y")


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write(f"Usage: {sys.argv[0]} <template-name>\n")
        return 2
    template = sys.argv[1]
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    src = repo_root / "templates" / template / "template.yml"
    if not src.is_file():
        sys.stderr.write(f"No template at {src}\n")
        return 2
    text = src.read_text(encoding="utf-8")
    for name, keep in _flags_for(template).items():
        pattern = re.compile(
            r"\{\{#" + name + r"\}\}(.*?)\{\{/" + name + r"\}\}", re.DOTALL
        )
        text = pattern.sub((lambda m, k=keep: m.group(1) if k else ""), text)
    for k, v in _vars_for(template).items():
        text = text.replace("{{" + k + "}}", v)
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if leftover:
        sys.stderr.write(
            f"Warning: unrendered placeholders remain: {sorted(set(leftover))}\n"
        )
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
