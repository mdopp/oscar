#!/usr/bin/env python3
"""`pi-web-project` — one delegated read-only ServiceBay token per PI WEB project.

A PI WEB session is a shell in a project folder under `/workspace`, and the agent
in it has no way to read this box. claude-dev solves that with a per-project MCP
entry carrying its own `sb_` token (servicebay#2680); **Pi has no MCP at all** —
upstream says so in as many words ("It intentionally does not include built-in
MCP … build CLI tools with READMEs") — so there is no MCP config file here to put
a token in. This CLI is the file and the call: `add` mints the project's own
read-only child token, `get` is the read.

Three rules carry the design, so they are written down rather than inferred:

  OWNERSHIP IS THE ENTRY. A project is ours iff `<entries>/<name>.json` exists
    and names an `sb_` token. That one record is both the ownership flag and the
    credential, so the token and the record cannot drift apart — no entry can
    name a token that was never minted, and no minted token can lack an entry.
    Everything else under `/workspace` — every hand-cloned checkout — is
    untouchable: `remove` refuses it instead of guessing.

  NOTHING IS ADOPTED BY BEING THERE. There is no reconcile that mints for
    whatever directories turn up, and no opt-in marker either. `add` is typed by
    a person, once, per project. A credential that exists because a directory
    does is one nobody decided to issue.

  REMOVE DELETES NO FILES. It revokes the token and drops the entry; the
    checkout stays on disk. The bound is structural: this module calls no
    removal primitive on anything under the workspace at all.

Ordering leaves nothing orphaned: `add` revokes a previous round's token before
minting, and revokes the fresh one again if the entry cannot be written;
`remove` revokes first, so a failure there leaves a live token that is still
recorded and can be retried.

No token reaches argv or stdout. The parent is read from a file, the child goes
straight into the 0600 entry, and only the 8-hex id is ever printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_WORKSPACE = "/workspace"
DEFAULT_ENTRY_DIR = "/data/servicebay/projects"
DEFAULT_PARENT_TOKEN_FILE = "/data/servicebay/parent-token"
# The pod has its own network namespace, so ServiceBay is reached the way every
# other on-box sibling is — never 127.0.0.1, which is this pod's own loopback.
DEFAULT_API = "http://host.containers.internal:5888"

DELEGATE_PATH = "/api/system/api-tokens/delegate"

# `sb_<8-hex-id>_<secret>`. Only the id is ever pulled out of it.
SB_TOKEN = re.compile(r"^sb_([0-9a-f]{8})_")


class Refused(Exception):
    """A refusal with an exit code: 2 means "not ours", 1 means it went wrong."""

    def __init__(self, message: str, code: int = 1, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


def config_from_env(env=os.environ) -> dict:
    return {
        "workspace": env.get("PI_WEB_WORKSPACE") or DEFAULT_WORKSPACE,
        "entry_dir": env.get("PI_WEB_PROJECT_DIR") or DEFAULT_ENTRY_DIR,
        "parent_token_file": (
            env.get("PI_WEB_SB_TOKEN_FILE") or DEFAULT_PARENT_TOKEN_FILE
        ),
        "api": (env.get("SERVICEBAY_API_URL") or DEFAULT_API).rstrip("/"),
    }


# ── the HTTP layer (injected in tests) ──────────────────────────────────────


def http_json(
    url: str,
    method: str = "GET",
    token: str = "",
    payload: dict | None = None,
    timeout: float = 15.0,
):
    """`(status, decoded body)`. Status 0 means ServiceBay did not answer."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _decode(resp.read())
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except OSError:
            raw = b""
        return e.code, _decode(raw)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": str(e)}


def _decode(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _reason(status: int, body) -> str:
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    return f"HTTP {status}" if status else "ServiceBay did not answer"


# ── names, entries, tokens ──────────────────────────────────────────────────


def token_id(secret: str) -> str:
    match = SB_TOKEN.match(secret or "")
    return match.group(1) if match else ""


def validate_name(name: str) -> str:
    if not name:
        return "a project name is required"
    if len(name) > 64:
        return "a project name may be at most 64 characters"
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", name):
        return (
            f'"{name}" is not a usable project name — use letters, digits, '
            '".", "_" and "-", starting with a letter or a digit'
        )
    return ""


def resolve_project(workspace: str, target: str) -> tuple[str, str]:
    """`(name, path)` for a folder directly under the workspace.

    A name and a path are both accepted because PI WEB shows the path and a
    person types the name; anything that resolves elsewhere is refused rather
    than rewritten, so the entry always matches the folder the operator meant.
    """
    raw = (target or "").strip()
    if not raw:
        raise Refused("say which project: pi-web-project <verb> <name-or-path>")
    root = os.path.normpath(workspace)
    path = os.path.normpath(raw if os.path.isabs(raw) else os.path.join(root, raw))
    if os.path.dirname(path) != root:
        raise Refused(f"{path} is not a project folder directly under {root}")
    name = os.path.basename(path)
    error = validate_name(name)
    if error:
        raise Refused(error)
    return name, path


def entry_path(entry_dir: str, name: str) -> str:
    return os.path.join(entry_dir, f"{name}.json")


def read_entry(entry_dir: str, name: str) -> dict | None:
    """The project's record, or `None` when there is none.

    An unreadable or malformed entry RAISES: "we could not look" is not "not
    ours", and treating it as unmanaged would revoke nothing while reporting a
    clean removal.
    """
    path = entry_path(entry_dir, name)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Refused(f"could not read {path}: {e}") from e
    try:
        entry = json.loads(text)
    except ValueError as e:
        raise Refused(f"{path} is not valid JSON: {e}") from e
    if not isinstance(entry, dict):
        raise Refused(f"{path} does not hold a project entry")
    return entry


def write_entry(entry_dir: str, name: str, entry: dict) -> str:
    path = entry_path(entry_dir, name)
    os.makedirs(entry_dir, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2)
        f.write("\n")
    # O_CREAT's mode is ignored for a file that already existed, and the pod's
    # perms init runs `chmod -R a+rwX /data` on every start.
    os.chmod(path, 0o600)
    return path


def read_parent_token(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        token = ""
    if not token_id(token):
        raise Refused(
            "this container holds no ServiceBay token, so it cannot delegate one "
            "to a project — leave PI_WEB_SB_TOKEN blank in the ServiceBay wizard "
            "and it mints a read-only one at install, then redeploy pi-web"
        )
    return token


def delegate_token(api: str, parent: str, name: str, http=http_json) -> str:
    """Mint a read-only child of this container's token, named for the project.

    Read-only like the parent: a session that genuinely needs more asks the
    operator through ServiceBay, it does not get a wider standing credential.
    """
    status, body = http(
        f"{api}{DELEGATE_PATH}",
        "POST",
        parent,
        {"name": f"pi-web project {name}", "scopes": ["read"]},
    )
    secret = body.get("secret") if isinstance(body, dict) else None
    if status not in (200, 201) or not token_id(secret or ""):
        raise Refused(
            f'ServiceBay refused to delegate a token for "{name}"',
            detail=_reason(status, body),
        )
    return secret


def revoke_token(api: str, parent: str, child_id: str, http=http_json) -> str:
    """`"revoked"` or `"already-gone"`.

    A 404 is an answer, not a failure — a removal that failed halfway can be
    retried to completion. Every other refusal is raised, because "revoked
    nothing" must never read as "revoked it".
    """
    query = urllib.parse.urlencode({"id": child_id})
    status, body = http(f"{api}{DELEGATE_PATH}?{query}", "DELETE", parent, None)
    if status == 404:
        return "already-gone"
    if status not in (200, 204):
        raise Refused(
            f"ServiceBay refused to revoke the token {child_id}",
            detail=_reason(status, body),
        )
    return "revoked"


# ── the three verbs ─────────────────────────────────────────────────────────


def add_project(cfg: dict, target: str, http=http_json) -> dict:
    name, path = resolve_project(cfg["workspace"], target)
    if not os.path.isdir(path):
        raise Refused(
            f"{path} does not exist — clone or create the project folder first, "
            "then add it"
        )
    parent = read_parent_token(cfg["parent_token_file"])
    notes: list[str] = []

    # Re-adding must not leave the previous round's token live and unrecorded.
    previous = read_entry(cfg["entry_dir"], name)
    previous_id = token_id(previous.get("token", "")) if previous else ""
    if previous_id:
        state = revoke_token(cfg["api"], parent, previous_id, http)
        if state == "already-gone":
            notes.append(f"the recorded token {previous_id} was already gone")

    secret = delegate_token(cfg["api"], parent, name, http)
    try:
        written = write_entry(
            cfg["entry_dir"],
            name,
            {"project": name, "path": path, "url": cfg["api"], "token": secret},
        )
    except OSError as e:
        # The token exists but nothing records it — take it back rather than
        # leave a credential nobody can find again.
        try:
            revoke_token(cfg["api"], parent, token_id(secret), http)
        except Refused:
            pass
        raise Refused(
            f'the entry for "{name}" could not be written, so its token was '
            f"revoked again: {e}"
        ) from e
    return {
        "project": name,
        "path": path,
        "entry": written,
        "token_id": token_id(secret),
        "notes": notes,
    }


def remove_project(cfg: dict, target: str, http=http_json) -> dict:
    name, path = resolve_project(cfg["workspace"], target)
    entry = read_entry(cfg["entry_dir"], name)
    if entry is None:
        raise Refused(
            f'"{name}" was not added with `pi-web-project add` — it has no '
            "ServiceBay token of ours, so there is nothing to take back",
            code=2,
            detail="Its files are untouched either way; this command deletes none.",
        )
    child_id = token_id(entry.get("token", ""))
    if not child_id:
        raise Refused(
            f'the entry for "{name}" names no ServiceBay token — delete '
            f"{entry_path(cfg['entry_dir'], name)} by hand if it is leftover"
        )
    parent = read_parent_token(cfg["parent_token_file"])
    state = revoke_token(cfg["api"], parent, child_id, http)
    os.remove(entry_path(cfg["entry_dir"], name))
    return {
        "project": name,
        "path": path,
        "token_id": child_id,
        "token": state,
        # Said out loud so nobody reads `remove` as a delete.
        "checkout_deleted": False,
    }


def list_projects(cfg: dict) -> list[dict]:
    """Every folder in the workspace, and whether it is one of ours."""
    try:
        names = sorted(
            e.name
            for e in os.scandir(cfg["workspace"])
            if e.is_dir(follow_symlinks=False)
        )
    except OSError as e:
        raise Refused(f"could not list {cfg['workspace']}: {e}") from e
    rows = []
    for name in names:
        if validate_name(name):
            continue
        entry = read_entry(cfg["entry_dir"], name)
        rows.append(
            {
                "project": name,
                "token_id": token_id(entry.get("token", "")) if entry else "",
            }
        )
    return rows


def read_box(cfg: dict, target: str, api_path: str, http=http_json):
    """One read against ServiceBay with the project's own token. GET only."""
    name, _ = resolve_project(cfg["workspace"], target)
    entry = read_entry(cfg["entry_dir"], name)
    if entry is None:
        raise Refused(
            f'"{name}" has no ServiceBay token — run `pi-web-project add {name}`',
            code=2,
        )
    if not api_path.startswith("/"):
        raise Refused("an API path starts with a slash, e.g. /api/services")
    base = str(entry.get("url") or cfg["api"]).rstrip("/")
    return http(f"{base}{api_path}", "GET", entry.get("token", ""), None)


# ── the command line ────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-web-project",
        description=(
            "Give one PI WEB project its own read-only ServiceBay token, and "
            "read the box with it."
        ),
    )
    sub = parser.add_subparsers(dest="verb", required=True)
    add = sub.add_parser("add", help="mint this project's ServiceBay token")
    add.add_argument("project", help="project folder name or path under /workspace")
    remove = sub.add_parser("remove", help="revoke it again; deletes no files")
    remove.add_argument("project", help="project folder name or path")
    sub.add_parser("list", help="which projects have a token of ours")
    get = sub.add_parser("get", help="read the box with this project's token")
    get.add_argument("path", help="ServiceBay API path, e.g. /api/services")
    get.add_argument(
        "--project",
        default="",
        help="project to read as (default: the current folder)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_from_env()
    try:
        if args.verb == "add":
            result = add_project(cfg, args.project)
            for note in result["notes"]:
                print(f"note: {note}")
            print(
                f"{result['project']}: ServiceBay token {result['token_id']} "
                f"(read-only), recorded in {result['entry']}"
            )
        elif args.verb == "remove":
            result = remove_project(cfg, args.project)
            print(
                f"{result['project']}: token {result['token_id']} "
                f"{result['token']}, entry dropped, no file deleted"
            )
        elif args.verb == "list":
            for row in list_projects(cfg):
                mark = row["token_id"] or "— not added here"
                print(f"{row['project']}\t{mark}")
        else:
            status, body = read_box(cfg, args.project or os.getcwd(), args.path)
            if status != 200:
                print(f"ServiceBay answered {_reason(status, body)}", file=sys.stderr)
                return 1
            print(json.dumps(body, indent=2, ensure_ascii=False))
    except Refused as e:
        print(str(e), file=sys.stderr)
        if e.detail:
            print(e.detail, file=sys.stderr)
        return e.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
