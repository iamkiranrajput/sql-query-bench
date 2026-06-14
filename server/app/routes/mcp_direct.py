"""
MCP Direct Tool Invocation Routes.

Exposes MCP server tools as plain REST endpoints for the UI's MCP Agent
panel. No LLM is involved on this path -- tools are called directly via
``MCPServer.call_tool()``. The Copilot agent loop lives in
``app.routes.copilot_routes`` and ``app.services.copilot``.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from app.mcp_server import get_mcp_server
from app.models.mcp_direct import (
    McpChainRequest,
    McpChainResponse,
    McpToolCallRequest,
    McpToolCallResponse,
    McpToolDefinition,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp")


# Tool category mapping used by the UI to group tools in the side-panel.
TOOL_CATEGORIES = {
    "search_tables": "Schema Discovery",
    "search_columns": "Schema Discovery",
    "check_relationships": "Schema Discovery",
    "introspect_schema": "Schema Discovery",
    "discover_join_paths": "Schema Discovery",
    "preview_data": "Data Access",
    "sample_column_values": "Data Access",
    "generate_sql": "SQL Generation",
    "validate_sql": "SQL Generation",
    "execute_sql": "SQL Execution",
    "explain_sql": "SQL Assistance",
    "fix_sql": "SQL Assistance",
    "connect_database": "Connection",
    "switch_database": "Connection",
    "list_available_databases": "Connection",
    "get_conversation_context": "Context",
    "get_database_context": "Context",
    "get_schema_domains": "Context",
}


@router.get("/tools", response_model=List[McpToolDefinition])
async def list_tools() -> List[McpToolDefinition]:
    """List all available MCP tools with their parameter schemas."""

    mcp = get_mcp_server()
    tools = mcp.list_tools()
    return [
        McpToolDefinition(
            name=t["name"],
            description=t["description"],
            parameters=t["inputSchema"],
            category=TOOL_CATEGORIES.get(t["name"], "general"),
        )
        for t in tools
    ]


@router.post("/call", response_model=McpToolCallResponse)
async def call_tool(request: McpToolCallRequest) -> McpToolCallResponse:
    """Invoke a single MCP tool directly (no LLM in the loop)."""

    mcp = get_mcp_server()

    tool = mcp.get_tool(request.tool_name)
    if not tool:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown tool: {request.tool_name}. "
                "Use GET /api/mcp/tools to list available tools."
            ),
        )

    start = time.perf_counter()
    result = mcp.call_tool(request.tool_name, request.arguments)
    elapsed_ms = (time.perf_counter() - start) * 1000

    logger.info(
        "MCP direct call: %s -> %s (%.0fms)",
        request.tool_name,
        "OK" if result.success else "FAIL",
        elapsed_ms,
    )

    return McpToolCallResponse(
        success=result.success,
        tool_name=request.tool_name,
        result=result.result,
        error=result.error,
        execution_time_ms=round(elapsed_ms, 1),
    )


# ---------------------------------------------------------------------------
# Tool chain support (``/api/mcp/chain``) -- run a sequence of tools, each
# step can reference outputs from previous steps via ``$prev.<path>`` /
# ``$step[n].<path>`` placeholders.
# ---------------------------------------------------------------------------

_PREV_PATTERN = re.compile(r"\$prev\.(.+)")
_STEP_PATTERN = re.compile(r"\$step\[(\d+)\]\.(.+)")


def _resolve_ref(ref_path: str, data: Any) -> Any:
    """Walk a dot-separated path into a nested dict/list."""

    current = data
    for part in ref_path.split("."):
        idx_match = re.match(r"^(\w+)\[(\d+)\]$", part)
        if idx_match:
            key, idx = idx_match.group(1), int(idx_match.group(2))
            current = current[key][idx]
        elif isinstance(current, dict):
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            raise KeyError(f"Cannot resolve '{part}' in {type(current)}")
    return current


def _step_as_dict(step: McpToolCallResponse) -> dict:
    return {
        "success": step.success,
        "tool_name": step.tool_name,
        "result": step.result,
        "error": step.error,
        "execution_time_ms": step.execution_time_ms,
    }


def _resolve_templates(
    arguments: Dict[str, Any],
    step_results: List[McpToolCallResponse],
) -> Dict[str, Any]:
    """Substitute ``$prev.x`` / ``$step[n].x`` placeholders with prior outputs."""

    resolved: Dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str):
            m = _PREV_PATTERN.fullmatch(value)
            if m and step_results:
                try:
                    resolved[key] = _resolve_ref(
                        m.group(1), _step_as_dict(step_results[-1])
                    )
                    continue
                except (KeyError, IndexError, TypeError) as exc:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Cannot resolve $prev.{m.group(1)}: {exc}",
                    )

            m2 = _STEP_PATTERN.fullmatch(value)
            if m2:
                idx = int(m2.group(1))
                if idx >= len(step_results):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"$step[{idx}] referenced but only "
                            f"{len(step_results)} steps completed."
                        ),
                    )
                try:
                    resolved[key] = _resolve_ref(
                        m2.group(2), _step_as_dict(step_results[idx])
                    )
                    continue
                except (KeyError, IndexError, TypeError) as exc:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Cannot resolve $step[{idx}].{m2.group(2)}: {exc}",
                    )

        resolved[key] = value
    return resolved


@router.post("/chain", response_model=McpChainResponse)
async def call_chain(request: McpChainRequest) -> McpChainResponse:
    """Execute a chain of MCP tool calls sequentially. Stops on first failure."""

    mcp = get_mcp_server()
    results: List[McpToolCallResponse] = []
    chain_start = time.perf_counter()

    for i, step in enumerate(request.steps):
        tool = mcp.get_tool(step.tool_name)
        if not tool:
            results.append(
                McpToolCallResponse(
                    success=False,
                    tool_name=step.tool_name,
                    error=f"Unknown tool: {step.tool_name}",
                )
            )
            break

        try:
            resolved_args = _resolve_templates(step.arguments, results)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            results.append(
                McpToolCallResponse(
                    success=False,
                    tool_name=step.tool_name,
                    error=f"Template resolution failed: {exc}",
                )
            )
            break

        start = time.perf_counter()
        result = mcp.call_tool(step.tool_name, resolved_args)
        elapsed_ms = (time.perf_counter() - start) * 1000

        step_response = McpToolCallResponse(
            success=result.success,
            tool_name=step.tool_name,
            result=result.result,
            error=result.error,
            execution_time_ms=round(elapsed_ms, 1),
        )
        results.append(step_response)

        logger.info(
            "MCP chain step %d/%d: %s -> %s (%.0fms)",
            i + 1,
            len(request.steps),
            step.tool_name,
            "OK" if result.success else "FAIL",
            elapsed_ms,
        )

        if not result.success:
            break

    total_ms = (time.perf_counter() - chain_start) * 1000
    return McpChainResponse(results=results, total_time_ms=round(total_ms, 1))
