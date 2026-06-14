"""
MCP over HTTP -- Streamable HTTP transport for the sql-query-tool MCP server.

Exposes the same MCP server defined in mcp_stdio_server.py to external MCP
clients (MCP Inspector, Claude Desktop, Cursor, VS Code Copilot, ...) over
an HTTP/SSE endpoint.

Design notes
------------
- We deliberately REUSE the ``app`` (low-level mcp.server.Server) instance
  and all its registered handlers from ``mcp_stdio_server`` so behaviour
  stays identical across stdio and HTTP transports. Importing that module
  triggers its ``@app.list_tools()`` / ``@app.call_tool()`` / ... decorators
  which register the handlers exactly once on the shared Server object.
- We do NOT call ``_initialize_services()`` from there -- that's a stdio-only
  bootstrap. By the time this module is wired in by main.py, the FastAPI
  lifespan has already initialised the singleton MCP server, schema index,
  and context manager. We only need to:
    1. Set ``mcp_stdio_server._internal_mcp`` to the shared singleton.
    2. Optionally auto-connect to a preset DB so ``execute_sql`` works without
       the caller having to pass a session_id.
- Authentication: gated by ``MCP_HTTP_AUTH_MODE``. The hackathon release
  supports ``static`` (shared bearer token in ``MCP_HTTP_BEARER_TOKEN``)
  and ``none`` (DEV ONLY, refused outside DEBUG=True).

Mount path: defaults to ``/mcp`` (configurable via ``MCP_HTTP_PATH``).
Transport:  Streamable HTTP -- supports both SSE streaming (default) and
            simple JSON responses (``MCP_HTTP_JSON_RESPONSE=true``).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Optional

from fastapi import FastAPI
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.routing import Route

from app.config.settings import settings
from app.services.logger_service import setup_logger

logger = setup_logger(__name__)

# Module-level state — created once during FastAPI startup, torn down at shutdown
_session_manager: Optional[StreamableHTTPSessionManager] = None
_lifespan_cm: Optional[AsyncIterator[None]] = None


def _build_session_manager() -> StreamableHTTPSessionManager:
    """
    Import the shared MCP `Server` from mcp_stdio_server (triggers handler
    registration) and wrap it in a Streamable-HTTP session manager.
    """
    # Importing for side-effects: registers @app.list_tools / @app.call_tool
    # / @app.list_resources / @app.read_resource / @app.list_prompts /
    # @app.get_prompt on the module-level `app` Server instance.
    import mcp_stdio_server as stdio_module

    # Wire the singleton MCP server (already initialised by FastAPI lifespan
    # in main.py's _init_mcp_server). The stdio module's `call_tool` handler
    # reads from this global to dispatch tool execution.
    from app.mcp_server import get_mcp_server
    stdio_module._internal_mcp = get_mcp_server()

    # Auto-connect to the preset DB if configured, so tool calls that need a
    # session_id work without forcing every client to call connect_database.
    _maybe_autoconnect_preset_db(stdio_module)

    manager = StreamableHTTPSessionManager(
        app=stdio_module.app,
        event_store=None,
        json_response=bool(settings.mcp_http_json_response),
        stateless=False,
    )
    logger.info(
        "[MCP_HTTP] Session manager built (json_response=%s)",
        settings.mcp_http_json_response,
    )
    return manager


def _maybe_autoconnect_preset_db(stdio_module: Any) -> None:
    """Auto-connect to settings.preset_db_* and seed _db_session_id."""
    host = (settings.preset_db_host or "").strip()
    database = (settings.preset_db_database or "").strip()
    if not host or not database:
        logger.info(
            "[MCP_HTTP] No PRESET_DB_* configured — execute_sql will require "
            "an explicit connect_database call from the client."
        )
        return
    try:
        from app.services.database_service import database_service
        session_id, msg = database_service.create_connection(
            hostname=host,
            port=int(settings.preset_db_port or 5432),
            database=database,
            username=settings.preset_db_username or "",
            password=settings.preset_db_password or "",
            db_type=settings.preset_db_type or "postgresql",
        )
        stdio_module._db_session_id = session_id
        stdio_module._db_info = {
            "connected": True,
            "database": database,
            "db_type": settings.preset_db_type,
            "host": host,
            "session_id": session_id,
        }
        logger.info(
            "[MCP_HTTP] Auto-connected to preset DB %s/%s (session=%s)",
            host, database, session_id,
        )
    except Exception as e:  # noqa: BLE001 — log and continue; clients can still call connect_database
        logger.warning("[MCP_HTTP] Preset DB auto-connect failed: %s", e)


def _make_auth_wrapper(handle_request: Callable) -> Callable:
    """
    Wrap the MCP ASGI handler with:
    - Permissive CORS for MCP clients (Inspector, Claude Desktop, browser hosts).
      MCP transport is unauthenticated at the CORS layer; auth happens via
      bearer token below. We echo back the request Origin so credentials work.
    - Bearer-token validation controlled by `MCP_HTTP_AUTH_MODE`:
        * "none"   — open (DEV ONLY). Will refuse to run if MCP_HTTP_ENABLED
                     is true AND debug is false (guard in mount_mcp_http).
        * "static" — compare bearer to MCP_HTTP_BEARER_TOKEN.
    """
    auth_mode = (settings.mcp_http_auth_mode or "none").strip().lower()
    static_token = (settings.mcp_http_bearer_token or "").strip()

    if auth_mode == "static" and not static_token:
        raise RuntimeError(
            "MCP_HTTP_AUTH_MODE=static requires MCP_HTTP_BEARER_TOKEN to be set."
        )
    if auth_mode not in {"none", "static"}:
        raise RuntimeError(
            f"Invalid MCP_HTTP_AUTH_MODE={auth_mode!r}. "
            "Expected one of: none, static."
        )

    def _cors_headers(origin: bytes) -> list[tuple[bytes, bytes]]:
        # Echo origin (instead of '*') so allow_credentials can be true and
        # MCP-specific headers/methods are accepted by browser-based clients.
        allow_origin = origin if origin else b"*"
        return [
            (b"access-control-allow-origin", allow_origin),
            (b"access-control-allow-credentials", b"true"),
            (b"access-control-allow-methods", b"GET, POST, DELETE, OPTIONS"),
            (b"access-control-allow-headers",
             b"authorization, content-type, accept, mcp-session-id, "
             b"mcp-protocol-version, last-event-id, x-request-id"),
            (b"access-control-expose-headers",
             b"mcp-session-id, mcp-protocol-version"),
            (b"access-control-max-age", b"86400"),
            (b"vary", b"Origin"),
        ]

    async def _authenticate(
        headers: dict, send: Callable, origin: bytes
    ) -> tuple[bool, Optional[Any]]:
        """
        Returns (ok, identity_or_None). On failure, has already sent a
        response and the caller must return without further processing.
        """
        if auth_mode == "none":
            return True, None

        auth_header = headers.get(b"authorization", b"").decode("latin-1", "ignore")
        if not auth_header.lower().startswith("bearer "):
            await _send_json(send, 401, {"error": "missing_bearer_token"},
                             extra_headers=_cors_headers(origin) +
                             [(b"www-authenticate", b'Bearer realm="mcp"')])
            return False, None
        token = auth_header[7:].strip()

        if auth_mode == "static":
            import hmac as _hmac
            if not _hmac.compare_digest(token, static_token):
                await _send_json(send, 401, {"error": "invalid_bearer_token"},
                                 extra_headers=_cors_headers(origin))
                return False, None
            return True, None

        # Should be unreachable -- auth_mode is validated above.
        await _send_json(send, 500, {"error": "unknown_auth_mode"},
                         extra_headers=_cors_headers(origin))
        return False, None

    async def asgi_app(scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await handle_request(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        origin = headers.get(b"origin", b"")
        method = scope.get("method", "").upper()

        # CORS preflight short-circuit (browsers send OPTIONS before POST)
        if method == "OPTIONS":
            await send({
                "type": "http.response.start",
                "status": 204,
                "headers": _cors_headers(origin) + [(b"content-length", b"0")],
            })
            await send({"type": "http.response.body", "body": b""})
            return

        # Browser-friendly landing page: GET /mcp with no bearer + Accept: text/html
        # → serve a small HTML page explaining what this endpoint is.
        # MCP Streamable-HTTP clients use GET /mcp WITH a bearer token to open
        # the SSE notification stream, so we only intercept the unauthenticated
        # browser case.
        if method == "GET":
            accept = headers.get(b"accept", b"").decode("latin-1", "ignore").lower()
            has_bearer = headers.get(b"authorization", b"").lower().startswith(b"bearer ")
            if not has_bearer and "text/html" in accept:
                await _send_landing_html(send, _cors_headers(origin))
                return

        ok, identity = await _authenticate(headers, send, origin)
        if not ok:
            return

        # Attach identity to the ASGI scope so MCP tools / logging can read it
        # without re-validating. We use scope["state"] which is preserved by
        # Starlette and accessible from request handlers.
        if identity is not None:
            scope.setdefault("state", {})
            scope["state"]["mcp_caller_identity"] = identity

        # Inject CORS headers into the upstream response by wrapping `send`.
        cors = _cors_headers(origin)

        async def send_with_cors(message: dict) -> None:
            if message.get("type") == "http.response.start":
                existing = list(message.get("headers", []))
                existing_keys = {k for k, _ in existing}
                for k, v in cors:
                    if k not in existing_keys:
                        existing.append((k, v))
                message = {**message, "headers": existing}
            await send(message)

        await handle_request(scope, receive, send_with_cors)

    return asgi_app


async def _send_json(
    send: Callable,
    status: int,
    payload: dict,
    extra_headers: Optional[list] = None,
) -> None:
    body = json.dumps(payload).encode("utf-8")
    hdrs = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if extra_headers:
        hdrs.extend(extra_headers)
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": hdrs,
    })
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# Discovery / browser-friendly endpoints
# ---------------------------------------------------------------------------

_LANDING_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>SQL Query Tool — MCP Server</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;
      max-width:760px;margin:40px auto;padding:0 20px;color:#222;background:#fafafa}
 h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin-top:28px}
 .pill{display:inline-block;padding:2px 8px;border-radius:10px;background:#eef;color:#225;font-size:12px}
 code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
 pre{background:#0e1117;color:#e6edf3;padding:12px 14px;border-radius:6px;overflow:auto}
 a{color:#0a58ca}
 .muted{color:#666}
</style></head><body>
<h1>SQL Query Tool &mdash; MCP Server</h1>
<p class="muted">Streamable HTTP transport &middot; <span class="pill">MCP 2024-11-05</span></p>

<p>This URL is a <a href="https://modelcontextprotocol.io">Model Context Protocol</a>
endpoint, not a webpage. It is meant to be consumed by MCP clients such as
Claude Desktop, VS Code Copilot, Cursor, or the MCP Inspector.</p>

<h2>Authentication</h2>
<p>When <code>MCP_HTTP_AUTH_MODE=static</code> (the default), every request must include
the shared bearer token configured in <code>MCP_HTTP_BEARER_TOKEN</code>:</p>
<pre>Authorization: Bearer &lt;your-token&gt;</pre>
<p>When <code>MCP_HTTP_AUTH_MODE=none</code> (DEV ONLY -- refused outside
<code>DEBUG=True</code>), no authentication is required.</p>

<h2>Quick test</h2>
<pre>curl -X POST "$(this URL)" \\
  -H "Authorization: Bearer $TOKEN" \\
  -H "Accept: application/json, text/event-stream" \\
  -H "Content-Type: application/json" \\
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2024-11-05",
                 "capabilities":{},
                 "clientInfo":{"name":"curl","version":"1"}}}'</pre>

<h2>Discovery</h2>
<p><a href="info">/mcp/info</a> &mdash; JSON metadata about this server
(protocol version, tool count, auth mode).</p>

<h2>Client setup</h2>
<p>See <code>docs/PHASE_4_CLIENT_INTEGRATION.md</code> in the repository
for ready-made config snippets for VS Code, Claude Desktop, Cursor, and
the MCP Inspector.</p>
</body></html>"""


async def _send_landing_html(send: Callable, cors: list) -> None:
    body = _LANDING_HTML.encode("utf-8")
    hdrs = [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"public, max-age=300"),
        (b"x-content-type-options", b"nosniff"),
    ] + cors
    await send({"type": "http.response.start", "status": 200, "headers": hdrs})
    await send({"type": "http.response.body", "body": body})


def _build_info_payload() -> dict:
    """Public, auth-free server metadata for discovery."""
    auth_mode = (settings.mcp_http_auth_mode or "none").strip().lower()
    info: dict[str, Any] = {
        "name": "sql-query-tool",
        "description": (
            "Generic SQL assistant exposed as a Model Context Protocol "
            "server. Natural-language to SQL over PostgreSQL/MySQL/MSSQL/"
            "Oracle with schema-aware tool calling."
        ),
        "transport": "streamable-http",
        "protocol_versions": ["2024-11-05"],
        "endpoint": settings.mcp_http_path or "/mcp",
        "auth": {
            "mode": auth_mode,
            "scheme": "Bearer" if auth_mode != "none" else None,
        },
        "tool_count": None,
        "docs": {
            "modelcontextprotocol": "https://modelcontextprotocol.io",
        },
    }
    try:
        from app.mcp_server import get_mcp_server
        srv = get_mcp_server()
        # MCP low-level Server stores tools on _tools dict (handler-registry).
        tools = getattr(srv, "_tools", None)
        if tools is not None:
            info["tool_count"] = len(tools)
    except Exception:  # noqa: BLE001 — discovery should never fail hard
        pass
    return info


class _MCPInfoEndpoint:
    """ASGI endpoint that serves GET /mcp/info — public discovery metadata."""

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            return
        method = scope.get("method", "").upper()
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        origin = headers.get(b"origin", b"")
        cors = [
            (b"access-control-allow-origin", origin if origin else b"*"),
            (b"access-control-allow-methods", b"GET, OPTIONS"),
            (b"access-control-allow-headers", b"content-type, accept"),
            (b"vary", b"Origin"),
        ]
        if method == "OPTIONS":
            await send({
                "type": "http.response.start",
                "status": 204,
                "headers": cors + [(b"content-length", b"0")],
            })
            await send({"type": "http.response.body", "body": b""})
            return
        if method != "GET":
            await _send_json(send, 405, {"error": "method_not_allowed"}, extra_headers=cors)
            return
        await _send_json(send, 200, _build_info_payload(), extra_headers=cors)


def mount_mcp_http(app: FastAPI) -> None:
    """
    Register the MCP Streamable-HTTP endpoint on the FastAPI app at
    settings.mcp_http_path (default '/mcp').

    Uses a Starlette Route (not Mount) so the bare path '/mcp' is served
    directly without a 307 redirect to '/mcp/'. MCP Streamable HTTP only
    uses a single URL, so sub-path routing is not needed.

    Call this AFTER all FastAPI routers are added.
    """
    if not settings.mcp_http_enabled:
        logger.info("[MCP_HTTP] Disabled (set MCP_HTTP_ENABLED=true to enable)")
        return

    auth_mode = (settings.mcp_http_auth_mode or "none").strip().lower()
    # Refuse to expose an unauthenticated MCP endpoint when not in DEBUG mode.
    # This is the cheapest possible guard against accidentally shipping the
    # dev-mode configuration to a public deployment.
    if auth_mode == "none" and not settings.debug:
        raise RuntimeError(
            "MCP_HTTP_AUTH_MODE=none is not allowed when DEBUG=false. "
            "Set MCP_HTTP_AUTH_MODE=static."
        )

    global _session_manager
    if _session_manager is None:
        _session_manager = _build_session_manager()

    mount_path = settings.mcp_http_path.rstrip("/") or "/mcp"
    asgi_app = _make_auth_wrapper(_session_manager.handle_request)
    # Wrap in a class instance with __call__ so Starlette's Route treats it
    # as a raw ASGI app instead of wrapping it in request_response().
    wrapped = _ASGIEndpoint(asgi_app)
    route = Route(
        mount_path,
        endpoint=wrapped,
        methods=["GET", "POST", "DELETE", "OPTIONS"],
    )
    app.router.routes.append(route)

    # Public discovery endpoint at <mount>/info — no auth, JSON metadata.
    info_route = Route(
        f"{mount_path}/info",
        endpoint=_MCPInfoEndpoint(),
        methods=["GET", "OPTIONS"],
    )
    app.router.routes.append(info_route)

    if auth_mode == "static":
        auth_note = "static bearer-token required"
    else:
        auth_note = "NO AUTH (dev only)"
    logger.info("[OK] MCP HTTP mounted at %s (%s)", mount_path, auth_note)


class _ASGIEndpoint:
    """Thin wrapper so a raw ASGI callable can be used as a Starlette Route endpoint."""

    def __init__(self, asgi_app: Callable) -> None:
        self._asgi_app = asgi_app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        await self._asgi_app(scope, receive, send)


@asynccontextmanager
async def mcp_http_lifespan() -> AsyncIterator[None]:
    """
    Async context manager for the session manager's background task group.
    Must be entered before the FastAPI app starts serving and exited on
    shutdown. main.py's lifespan wraps its `yield` in this context.
    """
    if not settings.mcp_http_enabled:
        yield
        return

    global _session_manager
    if _session_manager is None:
        _session_manager = _build_session_manager()

    logger.info("[MCP_HTTP] Starting session manager task group")
    async with _session_manager.run():
        try:
            yield
        finally:
            logger.info("[MCP_HTTP] Session manager task group exiting")
