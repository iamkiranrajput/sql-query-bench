"""
MCP stdio server -- generic SQL toolbox over the Model Context Protocol.

Exposes the SQL tools registered in ``app.mcp_server`` over MCP stdio so
that VSCode Copilot Chat, Cursor, Claude Desktop and other MCP-compatible
agents can query whatever database the operator has connected to.

The server:
  - Auto-connects to the preset database from .env (if configured).
  - Loads a curated schema-hints file when present (optional).
  - Provides two generic MCP Resources (``queryBench://database-context`` and
    ``queryBench://schema-domains``) so clients can self-describe.
  - Provides a single guided ``query-database`` prompt template.
  - Exposes the same toolbox as the HTTP backend (``search_tables``,
    ``execute_sql``, ``fix_sql``, ...).

Usage:
    python mcp_stdio_server.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

_SERVER_DIR = str(Path(__file__).parent.resolve())
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)
os.chdir(_SERVER_DIR)

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import io as _io
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name)
    if hasattr(_stream, "buffer") and _stream.encoding and _stream.encoding.lower() not in (
        "utf-8",
        "utf8",
    ):
        setattr(
            sys,
            _stream_name,
            _io.TextIOWrapper(
                _stream.buffer,
                encoding="utf-8",
                errors="replace",
                line_buffering=True,
            ),
        )

# Offline hints for sentence-transformers / huggingface so the server does
# not try to download embedding models at runtime in restricted networks.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    TextContent,
    TextResourceContents,
    Tool,
)

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

_SESSION_TOOLS = {
    "execute_sql",
    "preview_data",
    "introspect_schema",
    "discover_join_paths",
    "sample_column_values",
    "get_connection_profile",
    "analyze_connection_performance",
    "validate_server_compatibility",
    "check_db_integrity",
}


# ============================================================================
# Server Instructions -- the LLM reads this on every conversation.
# Shared with the UI Copilot path via ``app.mcp_server.system_prompt``.
# ============================================================================

from app.mcp_server.system_prompt import GENERIC_SQL_PROMPT as _SERVER_INSTRUCTIONS


# ============================================================================
# Initialise internal MCP server, attach database service, auto-connect.
# ============================================================================

def _initialize_services():
    """Initialise the in-process MCP server and (optionally) auto-connect."""

    from app.config.settings import settings
    from app.mcp_server import get_mcp_server
    from app.services.database_service import DatabaseService

    mcp = get_mcp_server()
    db_service = DatabaseService()

    schema_path = str(Path(__file__).parent / "data" / "schema_hints.json")

    mcp.initialize(
        db_service=db_service,
        llm_client=None,  # No LLM is required by the stdio path itself.
        schema_hints_path=schema_path if Path(schema_path).exists() else None,
    )
    logger.info("MCP server initialised with %d tools", len(mcp.list_tools()))

    # Load curated schema intelligence if a hints file is present.
    schema_meta = {}
    if Path(schema_path).exists():
        from app.mcp_server.schema_index import (
            get_schema_index,
            initialize_schema_index,
        )

        try:
            ok = initialize_schema_index(schema_path)
            if ok:
                idx = get_schema_index()
                logger.info(
                    "Schema index loaded: %d tables, %d columns, %d relationships",
                    len(idx.tables),
                    len(idx.columns),
                    len(idx.relationships),
                )
                schema_meta = {
                    "tables": len(idx.tables),
                    "columns": len(idx.columns),
                    "relationships": len(idx.relationships),
                }
            else:
                logger.warning("Schema index initialisation returned False")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Schema index initialisation failed: %s", exc)
    else:
        logger.info(
            "No schema_hints.json found at %s -- search_tables will use "
            "live database introspection instead.",
            schema_path,
        )

    try:
        from app.mcp_server.context import get_context_manager

        get_context_manager()
        logger.info("Context manager initialised")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Context manager initialisation failed: %s", exc)

    # Auto-connect: prefer PRESET_DB_* env vars; fall back to plain
    # HOST/PORT/DATABASE/USERNAME/PASSWORD (the names mcp.json typically
    # uses when it spawns this script).
    _db_host = settings.preset_db_host or os.environ.get("HOST", "")
    if settings.preset_db_host:
        try:
            _db_port = int(settings.preset_db_port)
        except (TypeError, ValueError):
            _db_port = 5432
    else:
        try:
            _db_port = int(os.environ.get("PORT", "5432"))
        except ValueError:
            _db_port = 5432
    _db_database = settings.preset_db_database or os.environ.get("DATABASE", "")
    _db_username = settings.preset_db_username or os.environ.get("USERNAME", "")
    _db_password = settings.preset_db_password or os.environ.get("PASSWORD", "")
    _db_type = settings.preset_db_type or "postgresql"

    session_id = None
    db_info = {
        "connected": False,
        "database": None,
        "db_type": None,
        "host": None,
    }
    if _db_host and _db_database:
        try:
            session_id, msg = db_service.create_connection(
                hostname=_db_host,
                port=_db_port,
                database=_db_database,
                username=_db_username,
                password=_db_password,
                db_type=_db_type,
            )
            db_info = {
                "connected": True,
                "database": _db_database,
                "db_type": _db_type,
                "host": _db_host,
                "session_id": session_id,
            }
            db_info.update(schema_meta)
            logger.info(
                "Auto-connected to database: %s (session=%s)", msg, session_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Auto-connect failed: %s", exc)
    else:
        logger.warning(
            "No preset database credentials -- execute_sql / preview_data "
            "will be unavailable until the client calls connect_database."
        )

    return mcp, session_id, db_info


# ============================================================================
# MCP Protocol Server -- with Resources, Prompts, and Instructions
# ============================================================================

app = Server("sql-query-tool", instructions=_SERVER_INSTRUCTIONS)

_internal_mcp = None
_db_session_id = None
_db_info: dict = {}


# ----- Tools -----

@app.list_tools()
async def list_tools() -> list[Tool]:
    """List all available SQL tools."""

    tools: list[Tool] = []
    for tool_def in _internal_mcp.list_tools():
        schema = tool_def["inputSchema"]

        # Strip ``session_id`` from tools where the server auto-injects it.
        # This prevents the LLM from hallucinating a value and avoids MCP
        # protocol validation failures when the LLM omits it.
        if tool_def["name"] in _SESSION_TOOLS:
            schema = dict(schema)
            props = dict(schema.get("properties", {}))
            props.pop("session_id", None)
            schema["properties"] = props
            req = [r for r in schema.get("required", []) if r != "session_id"]
            if req:
                schema["required"] = req
            else:
                schema.pop("required", None)

        tools.append(
            Tool(
                name=tool_def["name"],
                description=tool_def["description"],
                inputSchema=schema,
            )
        )
    return tools


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Execute a SQL tool and return the result."""

    global _db_session_id
    logger.info("Tool call: %s(%s)", name, list(arguments.keys()))

    if name in _SESSION_TOOLS:
        if _db_session_id:
            # Always override -- ignore any session_id the LLM may hallucinate.
            arguments["session_id"] = _db_session_id
        elif "session_id" not in arguments:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "success": False,
                            "error": (
                                "No database connection. Use the connect_database "
                                "tool first, or set HOST / DATABASE / USERNAME / "
                                "PASSWORD in the MCP env config."
                            ),
                        }
                    ),
                )
            ]

    result = _internal_mcp.call_tool(name, arguments)

    if result.success:
        if isinstance(result.result, dict):
            if (
                name in ("connect_database", "switch_database")
                and result.result.get("success")
                and result.result.get("session_id")
            ):
                _db_session_id = result.result["session_id"]
                logger.info("Updated active session to %s", _db_session_id)
            text = json.dumps(result.result, indent=2, default=str)
        else:
            text = str(result.result)
    else:
        text = json.dumps({"success": False, "error": result.error}, default=str)

    return [TextContent(type="text", text=text)]


# ----- Resources -----

@app.list_resources()
async def list_resources() -> list[Resource]:
    """Expose two generic context resources."""

    return [
        Resource(
            uri="queryBench://database-context",
            name="Database Context",
            description=(
                "Current database connection status, schema statistics, "
                "and the recommended tool workflow. Read this first to "
                "understand what database you are connected to."
            ),
            mimeType="application/json",
        ),
        Resource(
            uri="queryBench://schema-domains",
            name="Schema Domains & Key Tables",
            description=(
                "Breakdown of all table domains (extracted from the "
                "curated schema-hints file when one is loaded). Returns "
                "an empty structure when no hints file is present -- use "
                "search_tables / introspect_schema in that case."
            ),
            mimeType="application/json",
        ),
    ]


@app.read_resource()
async def read_resource(uri) -> list[TextResourceContents]:
    """Return the content of a resource."""

    uri_str = str(uri)

    if uri_str == "queryBench://database-context":
        return [
            TextResourceContents(
                uri=uri_str,
                mimeType="application/json",
                text=json.dumps(_build_database_context(), indent=2, default=str),
            )
        ]

    if uri_str == "queryBench://schema-domains":
        return [
            TextResourceContents(
                uri=uri_str,
                mimeType="application/json",
                text=json.dumps(_build_schema_domains(), indent=2, default=str),
            )
        ]

    return [
        TextResourceContents(
            uri=uri_str,
            mimeType="text/plain",
            text=f"Unknown resource: {uri_str}",
        )
    ]


# ----- Prompts -----

@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    """Expose a single guided workflow prompt."""

    return [
        Prompt(
            name="query-database",
            description=(
                "Step-by-step workflow for querying the connected database. "
                "Discovers tables, inspects columns, checks relationships, "
                "generates SQL, validates it, and executes the result."
            ),
            arguments=[
                PromptArgument(
                    name="question",
                    description=(
                        "Natural language question about the data "
                        "(e.g. 'list the 10 most recent orders')."
                    ),
                    required=True,
                ),
            ],
        ),
    ]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict | None) -> GetPromptResult:
    """Return a prompt template with the user's question embedded."""

    if name == "query-database":
        question = (arguments or {}).get("question", "show me the most recent rows")
        return GetPromptResult(
            description=f"Query the connected database: {question}",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=(
                            f"I need to query the connected database to answer "
                            f"this question:\n\n**\"{question}\"**\n\n"
                            "Follow this workflow step by step:\n"
                            "1. Use `search_tables` to find the relevant tables.\n"
                            "2. Use `search_columns` on the top matches to find the right columns.\n"
                            "3. Use `check_relationships` to discover correct JOIN paths.\n"
                            "4. Use `generate_sql` to compile the structured plan into SQL.\n"
                            "5. Use `validate_sql` to verify syntax and SELECT-only safety.\n"
                            "6. Use `execute_sql` to run the validated query.\n"
                            "7. If execution fails, use `fix_sql` with the error message and retry.\n\n"
                            "Reminders:\n"
                            "- Only SELECT queries are allowed.\n"
                            "- The database connection is pre-established -- "
                            "`session_id` is auto-injected.\n"
                            "- Always add a `LIMIT` (100 is a sensible default).\n"
                            "- Never invent table or column names -- discover them with tools.\n"
                        ),
                    ),
                ),
            ],
        )

    return GetPromptResult(description="Unknown prompt", messages=[])


# ============================================================================
# Resource builders -- thin wrappers around ``app.mcp_server.resources``.
# The shared module is the single source of truth so the stdio MCP path
# (``queryBench://*``) and the get_* tool path return byte-identical content.
# ============================================================================

from app.mcp_server.resources import (
    build_database_context as _shared_build_database_context,
    build_schema_domains as _build_schema_domains,
)


def _build_database_context() -> dict:
    """Stdio variant injects the live connection metadata."""

    return _shared_build_database_context(_db_info)


# ============================================================================
# Main
# ============================================================================

async def main():
    """Run the MCP stdio server."""

    global _internal_mcp, _db_session_id, _db_info

    logger.info("Starting MCP stdio server ...")
    _internal_mcp, _db_session_id, _db_info = _initialize_services()

    async with stdio_server() as (read_stream, write_stream):
        logger.info("MCP stdio server ready -- waiting for requests")
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
