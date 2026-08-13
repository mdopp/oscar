"""ServiceBay MCP tools for the admin profile — official `mcp` SDK client.

The admin persona's operator powers come from the `servicebay_admin` MCP
endpoint (token scopes read+lifecycle+mutate, no destroy/exec — unchanged
from the Hermes era).

Token source (#794): NOT a standing minting credential in the pod. The SB-MCP
Bearer is minted on demand from the ACTING admin's *live, verified* Authelia
session — the engine forwards the admin's forward-auth identity
(Remote-User / Remote-Groups, pinned per turn in `current_admin_identity`) to
ServiceBay's `POST /api/auth/token-from-authelia-session`, which returns a
short-lived (≤1h) read+lifecycle+mutate token. Authority flows from the human
signed in behind NPM, so the pod holds no long-lived token-minting secret. The
deploy-time token file (`sb_mcp_token_path`) remains a fallback for the boot/
code path (onboarding via `call_sb_tool`); the admin chat prefers the
session-exchanged token and re-exchanges it on a 401.

Connections are per-call (connect → initialize → act → close): admin turns
are rare and the MCP server is loopback, so holding a long-lived session
buys nothing and costs reconnect handling. Fail-open everywhere — an
unreachable MCP server leaves the admin chat tool-less, never broken.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp

from solaris_chat.engine.client import current_admin_identity
from solaris_chat.engine.tools import Toolbox
from solaris_chat.logging import log

_TTL_S = 300.0
# SB-MCP's scope-refusal text for a call the ambient token can't do (server.ts):
# "Token scope 'destroy' required for delete_service; this token has [read,...]".
# The Wartung ambient token is read+lifecycle+mutate, so ONLY destroy/exec refuse
# — that refusal is what P2c (#789) routes into a one-shot approval instead of
# surfacing raw to the model.
_SCOPE_REFUSAL_RE = re.compile(r"Token scope '(destroy|exec)' required for (\w+)")

# A one-shot op's target service must be a single safe path segment — SB derives
# the same anchor from `args.name ?? args.service` (coerceApprovalService) and
# rejects a bound token whose call targets a different service, so we bind to the
# same value or leave it unbound.
_SERVICE_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")

# (op, request_id, approval_id) → inject the Wartung approval card (#789). Kept
# out of this toolbox so it holds no notify/store deps; server.py wires it.
EscalationSink = Callable[[dict[str, Any], str, str | None], Awaitable[None]]
_EXCHANGE_TIMEOUT = aiohttp.ClientTimeout(total=15)
# The Authelia-session → SB-MCP token exchange (servicebay#2246). Mints a
# short-lived read+lifecycle+mutate token from the caller's forward-auth
# identity; refuses without Remote-User (401) or a client Bearer (403).
_EXCHANGE_PATH = "/api/auth/token-from-authelia-session"


def read_token(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_sb_token(read_path: str, fallback_path: str) -> str:
    """The unattended pollers' Bearer: the non-expiring read-only token
    (servicebay#2302), falling back to the rotating deploy-time SB-MCP token
    when its file is absent (pre-deploy / an older box). Read fresh each call so
    a 401 recovery is just "call again" — no cached staleness."""
    return read_token(read_path) or read_token(fallback_path)


def _scope_refusal(text: str) -> tuple[str, str] | None:
    """`(required_scope, tool_name)` when `text` is SB-MCP's destroy/exec scope
    refusal, else None. The Wartung ambient token holds read+lifecycle+mutate,
    so only a destroy/exec op refuses — that is what P2c escalates (#789)."""
    m = _SCOPE_REFUSAL_RE.search(text)
    return (m.group(1), m.group(2)) if m else None


def _op_service(arguments: dict[str, Any]) -> str | None:
    """The op's target service, matching SB's `args.name ?? args.service` anchor
    (coerceApprovalService). Only a single safe path segment binds the one-shot
    token to a service; anything else leaves it unbound (tool-only)."""
    for key in ("name", "service"):
        val = arguments.get(key)
        if isinstance(val, str) and _SERVICE_RE.match(val) and val not in (".", ".."):
            return val
    return None


def _parse_tool_json(text: str) -> dict[str, Any] | None:
    """SB-MCP tool results are text-JSON; parse to a dict when possible."""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def exchange_sb_token(sb_api_url: str) -> str:
    """Mint a short-lived scoped SB token from the ACTING admin's live Authelia
    session (#794), for a code-path REST call that needs the admin's authority
    (the approval-verdict callbacks, #790) rather than the deploy-time token.

    Forwards the turn's pinned Remote-User / Remote-Groups to
    `token-from-authelia-session`, which returns a read+lifecycle+mutate token.
    NO standing minting credential and NO client Bearer is sent (the endpoint
    403s a token caller). Best-effort: no SB API base or no admin identity ⇒ ""
    (the caller decides what to do without a session token); any failure ⇒ ""."""
    base = sb_api_url.rstrip("/")
    if not base:
        return ""
    user, groups = current_admin_identity.get()
    if not user:
        return ""
    try:
        async with aiohttp.ClientSession(timeout=_EXCHANGE_TIMEOUT) as client:
            async with client.post(
                f"{base}{_EXCHANGE_PATH}",
                headers={"Remote-User": user, "Remote-Groups": groups},
            ) as resp:
                if resp.status != 200:
                    log.warn("engine.sb.exchange_failed", status=resp.status)
                    return ""
                body = await resp.json()
    except (aiohttp.ClientError, ValueError) as e:
        log.warn("engine.sb.exchange_failed", error=str(e))
        return ""
    token = body.get("token") if isinstance(body, dict) else None
    return token if isinstance(token, str) and token else ""


def _is_401(exc: BaseException) -> bool:
    """True when `exc` is (or, for a task-group ExceptionGroup, wraps) an HTTP
    401. The MCP streamable-http client runs in an anyio task group, so a stale
    token surfaces as an ExceptionGroup around an httpx HTTPStatusError; we
    duck-type on `.response.status_code` to avoid a direct httpx import."""
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_401(sub) for sub in exc.exceptions)
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 401


class McpToolbox(Toolbox):
    def __init__(
        self,
        url: str,
        token_path: str,
        sb_api_url: str = "",
        on_escalation: EscalationSink | None = None,
    ):
        super().__init__([])
        self._url = url
        self._token_path = token_path
        # Wartung P2c (#789): when a destroy/exec call is refused for lack of
        # scope, route it into a one-shot approval instead of returning the raw
        # refusal to the model. None ⇒ no escalation (the refusal surfaces).
        self._on_escalation = on_escalation
        # SB control-plane base for the Authelia-session token exchange (#794).
        # Empty ⇒ no session exchange (the deploy-time token file is all we
        # have); a rotation then needs a redeploy.
        self._sb_api_url = sb_api_url.rstrip("/")
        # The short-lived token minted from the acting admin's session, cached
        # in-memory between the exchange and the connection that uses it. Never
        # persisted — it dies with the process (and expires ≤1h anyway).
        self._session_token = ""
        self._defs: list[dict[str, Any]] = []
        self._names: list[str] = []
        self._fetched_at = 0.0

    @property
    def url(self) -> str:
        return self._url

    async def prepare(self) -> None:
        if not self._url:
            return
        if self._defs and (time.time() - self._fetched_at) < _TTL_S:
            return
        try:
            tools = await self._list_tools()
        except Exception as e:  # noqa: BLE001 — fail-open: stale beats broken
            if _is_401(e) and await self._exchange_token():
                try:
                    tools = await self._list_tools()
                except Exception as e2:  # noqa: BLE001
                    log.warn("engine.mcp.list_failed", url=self._url, error=str(e2))
                    return
            else:
                log.warn("engine.mcp.list_failed", url=self._url, error=str(e))
                return
        self._defs = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description") or "",
                    "parameters": t.get("inputSchema")
                    or {"type": "object", "properties": {}},
                },
            }
            for t in tools
        ]
        self._names = [t["name"] for t in tools]
        self._fetched_at = time.time()
        log.info("engine.mcp.tools", url=self._url, n=len(self._names))

    def definitions(self) -> list[dict[str, Any]]:
        return list(self._defs)

    def names(self) -> list[str]:
        return list(self._names)

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self._names:
            return f'{{"error": "unknown tool: {name}"}}'
        try:
            out = await self._call_tool(name, arguments)
        except Exception as e:  # noqa: BLE001 — a tool error is model feedback
            if _is_401(e) and await self._exchange_token():
                try:
                    out = await self._call_tool(name, arguments)
                except Exception as e2:  # noqa: BLE001
                    return f'{{"error": "{type(e2).__name__}: {str(e2)[:200]}"}}'
            else:
                return f'{{"error": "{type(e).__name__}: {str(e)[:200]}"}}'
        refusal = _scope_refusal(out)
        if refusal is not None and self._on_escalation is not None:
            return await self._escalate(refusal[0], refusal[1], arguments)
        return out

    async def _escalate(
        self, scope: str, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        """Route a refused destroy/exec call into a ONE-SHOT owner approval
        (#789): ask SB-MCP for a one-shot token BOUND to exactly this op (parks
        an approval card, mints nothing yet), then inject the [Approve]/[Deny]
        card into the Wartung chat. The ambient token gains no scope — it only
        holds `read`, which `request_token` itself requires. Fail-open: if the
        request can't be filed, surface a short note (never the raw refusal, and
        never a silent run)."""
        service = _op_service(arguments)
        op: dict[str, Any] = {"tool_name": tool_name}
        if service:
            op["service"] = service
        req = {
            "scopes": [scope],
            "reason": f"Wartung chat needs to run {tool_name}"
            + (f" on {service}" if service else ""),
            "ttl_seconds": 600,
            "one_shot_op": op,
        }
        try:
            raw = await self._call_tool("request_token", req)
        except Exception as e:  # noqa: BLE001 — never break the turn on this path
            log.warn("engine.mcp.escalate_failed", tool=tool_name, error=str(e))
            return f'{{"error": "escalation failed: {str(e)[:160]}"}}'
        body = _parse_tool_json(raw)
        request_id = str(body.get("id")) if body and body.get("id") else ""
        if not request_id:
            log.warn("engine.mcp.escalate_norequest", tool=tool_name, raw=raw[:200])
            return f'{{"error": "escalation failed: {raw[:160]}"}}'
        approval_id = body.get("approvalId") if isinstance(body, dict) else None
        await self._on_escalation(
            {"tool_name": tool_name, "service": service, "arguments": arguments},
            request_id,
            str(approval_id) if approval_id else None,
        )
        log.info("engine.mcp.escalated", tool=tool_name, request_id=request_id)
        return json.dumps(
            {
                "status": "pending_approval",
                "request_id": request_id,
                "detail": f"{tool_name} needs owner approval; an approval card was "
                "posted to the Wartung chat. It runs only on Approve.",
            }
        )

    async def run_one_shot(
        self, tool_name: str, arguments: dict[str, Any], request_id: str
    ) -> tuple[bool, str]:
        """Collect the owner-approved one-shot token and run the bound op ONCE
        (#789 [Approve] handler). Polls `poll_token_request` for the single-use
        token, then calls `tool_name` over a FRESH connection carrying ONLY that
        token — never the ambient `_session_token`, so the toolbox gains no
        standing destroy/exec. Returns `(ok, detail)`: `ok` False when the token
        isn't ready (still pending / denied / already collected)."""
        try:
            polled = await self._call_tool("poll_token_request", {"id": request_id})
        except Exception as e:  # noqa: BLE001
            return False, f"poll failed: {str(e)[:200]}"
        body = _parse_tool_json(polled)
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            status = body.get("status") if isinstance(body, dict) else "unknown"
            return False, f"no one-shot token (status={status})"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            result = await self._call_tool_with(tool_name, arguments, headers)
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {str(e)[:200]}"
        return True, result

    # -- MCP wire ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # Prefer the token exchanged from the acting admin's Authelia session
        # (#794); fall back to the deploy-time file for the boot/code path.
        token = self._session_token or read_token(self._token_path)
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _exchange_token(self) -> bool:
        """Mint a fresh SB-MCP token from the ACTING admin's live Authelia
        session (#794): forward the turn's Remote-User / Remote-Groups
        forward-auth headers to `token-from-authelia-session`, which returns a
        short-lived read+lifecycle+mutate token. NO standing minting credential
        and NO client Bearer is sent (the endpoint 403s a token caller). Only an
        admin turn carries an identity, so a non-admin turn exchanges nothing.
        Best-effort: no SB API base or no admin identity ⇒ False; any failure ⇒
        False (the caller stays fail-open). True only when a token was cached."""
        if not self._sb_api_url:
            return False
        user, groups = current_admin_identity.get()
        if not user:
            return False
        try:
            async with aiohttp.ClientSession(timeout=_EXCHANGE_TIMEOUT) as client:
                async with client.post(
                    f"{self._sb_api_url}{_EXCHANGE_PATH}",
                    headers={"Remote-User": user, "Remote-Groups": groups},
                ) as resp:
                    if resp.status != 200:
                        log.warn("engine.mcp.exchange_failed", status=resp.status)
                        return False
                    body = await resp.json()
        except (aiohttp.ClientError, ValueError) as e:
            log.warn("engine.mcp.exchange_failed", error=str(e))
            return False
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            log.warn("engine.mcp.exchange_failed", reason="no token in response")
            return False
        self._session_token = token
        log.info("engine.mcp.exchanged", url=self._url, user=user)
        return True

    async def _list_tools(self) -> list[dict[str, Any]]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        async with create_mcp_http_client(self._headers()) as http:
            async with streamable_http_client(self._url, http_client=http) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            }
            for t in listed.tools
        ]

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        return await self._call_tool_with(name, arguments, self._headers())

    async def _call_tool_with(
        self, name: str, arguments: dict[str, Any], headers: dict[str, str]
    ) -> str:
        """One connect→initialize→act→close call with explicit headers. The
        one-shot flow (#789) passes the owner-approved token here so it never
        touches the ambient `_session_token`; the normal path passes the
        ambient headers."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        async with create_mcp_http_client(headers) as http:
            async with streamable_http_client(self._url, http_client=http) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
        parts: list[str] = []
        for item in result.content:
            text = getattr(item, "text", None)
            if text:
                parts.append(str(text))
        out = "\n".join(parts) or json.dumps({"ok": not result.is_error})
        return out[:16000]


class CombinedToolbox(Toolbox):
    """Two toolboxes presented as one — the admin profile needs the remote
    SB-MCP tools *and* a few local onboarding tools (#355) in one toolset.
    A name in the first wins on dispatch (none currently collide)."""

    def __init__(self, *boxes: Toolbox):
        super().__init__([])
        self._boxes = list(boxes)

    async def prepare(self) -> None:
        for box in self._boxes:
            await box.prepare()

    def definitions(self) -> list[dict[str, Any]]:
        defs: list[dict[str, Any]] = []
        for box in self._boxes:
            defs += box.definitions()
        return defs

    def names(self) -> list[str]:
        names: list[str] = []
        for box in self._boxes:
            names += box.names()
        return names

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        for box in self._boxes:
            if name in box.names():
                return await box.dispatch(name, arguments)
        return f'{{"error": "unknown tool: {name}"}}'


async def call_sb_tool(
    url: str, token_path: str, name: str, arguments: dict[str, Any]
) -> str:
    """Invoke one SB-MCP tool over the same connect→initialize→act→close path
    the admin toolbox uses, but callable from Python (the #355 onboarding flow
    needs to file/poll an access request as a code side-effect, not only expose
    the tool to the model)."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    token = read_token(token_path)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with create_mcp_http_client(headers) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
    parts: list[str] = []
    for item in result.content:
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
    out = "\n".join(parts) or json.dumps({"ok": not result.is_error})
    return out[:16000]
