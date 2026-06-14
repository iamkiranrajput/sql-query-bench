"""
GitHub Copilot API Routes

Uses the same auth flow as VS Code Copilot:
GitHub PAT → token exchange at api.github.com/copilot_internal/v2/token →
short-lived Copilot session token → api.githubcopilot.com/chat/completions
"""

import logging
import time
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Any, Dict, List

from app.services.copilot import get_copilot_service
from app.services.query_log_service import query_log_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/copilot")


# ── Pydantic Models ───────────────────────────────────────────────

class CopilotConfigRequest(BaseModel):
    github_token: str = Field(..., description="GitHub Personal Access Token")
    default_model: str = Field("claude-opus-4", description="Default model ID")


class CopilotConfigResponse(BaseModel):
    configured: bool
    default_model: str
    has_token: bool


class CopilotModelInfo(BaseModel):
    id: str
    name: str
    vendor: str = "Unknown"
    context_window: int = 0


class CopilotChatRequest(BaseModel):
    message: str = Field(..., description="User's natural language question")
    db_session_id: str = Field("", description="Database session ID (optional — agent can auto-connect via switch_database)")
    session_id: Optional[str] = Field(None, description="Chat session ID for multi-turn")
    model: Optional[str] = Field(None, description="Model ID to use (overrides default)")


class CopilotToolStepResponse(BaseModel):
    tool_name: str
    arguments: Dict[str, Any] = {}
    result: Any = None
    success: bool = True
    error: Optional[str] = None
    execution_time_ms: float = 0.0
    reasoning: Optional[str] = None
    database: Optional[str] = None


class CopilotChatResponse(BaseModel):
    success: bool
    message: str = ""
    sql: Optional[str] = None
    records: List[Dict[str, Any]] = []
    row_count: int = 0
    columns: List[str] = []
    tool_steps: List[CopilotToolStepResponse] = []
    total_time_ms: float = 0.0
    model: str = ""
    session_id: str = ""
    error: Optional[str] = None
    usage: Dict[str, Any] = {}
    active_database: str = ""
    # ── Verifiable Trust Layer (Phase 1) ──
    trust_score: int = 0
    trust_label: str = ""
    trust_checks: List[Dict[str, Any]] = []
    verification: Optional[Dict[str, Any]] = None
    grounded_sources: List[Dict[str, Any]] = []


# ── Routes ────────────────────────────────────────────────────────

@router.post("/configure", response_model=CopilotConfigResponse)
async def configure_copilot(request: CopilotConfigRequest):
    """
    Configure the GitHub PAT for Copilot.
    Verifies the token by exchanging it for a Copilot session token first.
    """
    svc = get_copilot_service()
    # Store the token
    svc.configure(github_token=request.github_token, default_model=request.default_model)
    # Verify it by attempting the token exchange
    try:
        await svc._get_copilot_token()
        logger.info("Copilot token verified successfully via token exchange")
    except Exception as e:
        # Reset on failure
        svc._github_token = None
        svc._copilot_token = None
        raise HTTPException(status_code=401, detail=str(e))
    cfg = svc.get_config()
    return CopilotConfigResponse(**cfg)


@router.get("/config", response_model=CopilotConfigResponse)
async def get_copilot_config():
    """Get current Copilot configuration status."""
    svc = get_copilot_service()
    cfg = svc.get_config()
    return CopilotConfigResponse(**cfg)


@router.get("/models", response_model=List[CopilotModelInfo])
async def list_copilot_models():
    """List available models from GitHub Models API."""
    svc = get_copilot_service()
    models = await svc.list_models()
    return [CopilotModelInfo(**m) for m in models]


@router.post("/chat", response_model=CopilotChatResponse)
async def copilot_chat(request: CopilotChatRequest):
    """
    Chat with a GitHub Copilot model using MCP tools.

    The model receives the user's question along with MCP tool definitions.
    It can call tools (search_tables, generate_sql, execute_sql, etc.)
    and the full tool-call trace is returned for UI visualization.
    """
    svc = get_copilot_service()

    if not svc.is_configured:
        return CopilotChatResponse(
            success=False,
            error="GitHub token not configured. Go to MCP Agent tab → click the settings icon → enter your GitHub PAT.",
        )

    session_id = request.session_id or f"copilot-{int(time.time())}"

    result = await svc.chat(
        session_id=session_id,
        message=request.message,
        db_session_id=request.db_session_id,
        model=request.model,
    )

    tool_steps = [
        CopilotToolStepResponse(
            tool_name=tc.tool_name,
            arguments=tc.arguments,
            result=tc.result,
            success=tc.success,
            error=tc.error,
            execution_time_ms=tc.execution_time_ms,
            reasoning=tc.reasoning,
            database=tc.database,
        )
        for tc in result.tool_calls
    ]

    # ── Cap records sent to UI to prevent browser lag ──
    UI_ROW_LIMIT = 500
    capped_records = result.records[:UI_ROW_LIMIT] if result.records and len(result.records) > UI_ROW_LIMIT else result.records
    truncated_for_ui = result.records and len(result.records) > UI_ROW_LIMIT
    capped_message = result.message
    if truncated_for_ui:
        capped_message += f" (Showing first {UI_ROW_LIMIT} of {result.row_count} rows)"

    response = CopilotChatResponse(
        success=result.success,
        message=capped_message,
        sql=result.sql,
        records=capped_records or [],
        row_count=result.row_count,
        columns=result.columns,
        tool_steps=tool_steps,
        total_time_ms=result.total_time_ms,
        model=result.model,
        session_id=session_id,
        error=result.error,
        usage=result.usage,
        active_database=result.active_database,
        trust_score=result.trust_score,
        trust_label=result.trust_label,
        trust_checks=result.trust_checks,
        verification=result.verification,
        grounded_sources=result.grounded_sources,
    )

    # Log copilot query to query_log_service
    try:
        tables_used = []
        for tc in result.tool_calls:
            if tc.tool_name in ("execute_sql", "generate_sql", "search_tables") and tc.success:
                if isinstance(tc.result, dict):
                    tables_used.extend(tc.result.get("tables_used", []))

        token_usage = {
            **(result.usage or {}),
            "model": result.model or "",
            "source": "copilot",
        }

        def _fmt_ms(ms: float) -> str:
            if ms >= 1000:
                return f"{ms / 1000:.1f}s"
            return f"{ms:.0f}ms"

        query_log_service.log_copilot_query(
            github_username="",
            session_id=session_id,
            user_query=request.message,
            generated_sql=result.sql or "",
            total_time_ms=result.total_time_ms,
            phase_timings=[
                {
                    "phase": tc.tool_name,
                    "duration_ms": tc.execution_time_ms,
                    "duration_formatted": _fmt_ms(tc.execution_time_ms),
                    "metadata": {"success": tc.success, "reasoning": getattr(tc, 'reasoning', None)},
                }
                for tc in result.tool_calls
            ],
            success=result.success,
            row_count=result.row_count,
            tables_used=list(set(tables_used)),
            token_usage=token_usage,
            error_message=result.error,
            model=result.model or "",
        )
    except Exception as e:
        logger.warning(f"Failed to log copilot query: {e}")

    return response


@router.post("/chat/stream")
async def copilot_chat_stream(request: CopilotChatRequest):
    """
    Streaming version of copilot chat using Server-Sent Events (SSE).
    Sends real-time progress as the agent calls tools:
    - event: thinking    → model reasoning text
    - event: tool_start  → tool name + args (before execution)
    - event: tool_result → tool result (after execution)
    - event: done        → final response with all data
    - event: error       → error message
    """
    svc = get_copilot_service()

    if not svc.is_configured:
        async def err_gen():
            import json
            yield f"event: error\ndata: {json.dumps({'error': 'GitHub token not configured'})}\n\n"
        return StreamingResponse(err_gen(), media_type="text/event-stream")

    session_id = request.session_id or f"copilot-{int(time.time())}"

    async def event_generator():
        async for chunk in svc.chat_stream(
            session_id=session_id,
            message=request.message,
            db_session_id=request.db_session_id,
            model=request.model,
        ):
            yield chunk

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Query Logging (for streaming) ─────────────────────────────────

class CopilotLogQueryRequest(BaseModel):
    session_id: str = ""
    user_query: str = ""
    generated_sql: str = ""
    total_time_ms: float = 0.0
    success: bool = True
    row_count: int = 0
    model: str = ""
    tables_used: List[str] = []
    db_identity: str = ""
    github_username: str = ""
    error_message: Optional[str] = None
    tool_steps: List[Dict[str, Any]] = []
    usage: Dict[str, Any] = {}


@router.post("/log-query")
async def log_copilot_query(request: CopilotLogQueryRequest):
    """
    Log a copilot query after SSE streaming completes.
    Stored in a separate copilot_query_logs table, keyed by github_username.
    """
    try:
        token_usage = {
            **(request.usage or {}),
            "model": request.model or "",
            "source": "copilot",
        }

        def _fmt_ms(ms: float) -> str:
            if ms >= 1000:
                return f"{ms / 1000:.1f}s"
            return f"{ms:.0f}ms"

        phase_timings = []
        for ts in request.tool_steps:
            dur = ts.get("execution_time_ms", 0)
            phase_timings.append({
                "phase": ts.get("tool_name", "unknown"),
                "duration_ms": dur,
                "duration_formatted": _fmt_ms(dur),
                "metadata": {
                    "success": ts.get("success", True),
                    "reasoning": ts.get("reasoning"),
                },
            })

        query_log_service.log_copilot_query(
            github_username=request.github_username,
            session_id=request.session_id,
            user_query=request.user_query,
            generated_sql=request.generated_sql,
            total_time_ms=request.total_time_ms,
            phase_timings=phase_timings,
            success=request.success,
            row_count=request.row_count,
            model=request.model,
            tables_used=request.tables_used,
            error_message=request.error_message,
            token_usage=token_usage,
        )
        return {"success": True}
    except Exception as e:
        logger.warning(f"Failed to log copilot query: {e}")
        return {"success": False, "error": str(e)}


@router.get("/logs")
async def get_copilot_logs(
    limit: int = Query(default=100, ge=1, le=10000, description="Max logs to return"),
    github_username: str = Query(default='', description="Filter by GitHub username"),
):
    """Get copilot/MCP Agent query logs from the separate copilot table."""
    try:
        logs = query_log_service.get_copilot_logs(limit=limit, github_username=github_username or None)
        return {"success": True, "logs": logs, "total": len(logs)}
    except Exception as e:
        logger.error(f"Error getting copilot logs: {e}")
        return {"success": False, "logs": [], "total": 0}


@router.get("/auth/user")
async def get_github_user():
    """
    Fetch the authenticated GitHub user's login from the stored token.
    GET https://api.github.com/user
    """
    svc = get_copilot_service()
    if not svc.is_configured or not svc._github_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"token {svc._github_token}",
                    "Accept": "application/json",
                    "User-Agent": "QueryBench/1.0",
                },
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail="GitHub API error")
            data = resp.json()
            return {"username": data.get("login", ""), "name": data.get("name", ""), "avatar_url": data.get("avatar_url", "")}
    except httpx.HTTPError as e:
        err_str = str(e)
        if "getaddrinfo" in err_str or "Name or service not known" in err_str:
            err_str = "Cannot reach api.github.com (DNS failure). Check VPN/network connectivity."
        elif not err_str:
            err_str = f"Network error ({type(e).__name__}). Check VPN/network."
        raise HTTPException(status_code=502, detail=err_str)


# ── OAuth Device Flow ─────────────────────────────────────────────

@router.post("/auth/device-code")
async def start_device_flow():
    """
    Start GitHub OAuth Device Flow.
    Returns a user_code and verification_uri.
    The user opens the URI in their browser and enters the code.
    """
    svc = get_copilot_service()
    try:
        result = await svc.start_device_flow()
        return result
    except Exception as e:
        logger.error(f"Device flow start failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/auth/poll")
async def poll_device_flow():
    """
    Poll for OAuth Device Flow completion.
    Returns { status: 'pending' | 'complete' | 'expired' | 'denied' | 'error' }
    Call this repeatedly (every `interval` seconds) until status != 'pending'.
    """
    svc = get_copilot_service()
    try:
        result = await svc.poll_device_flow()
        if result.get("status") == "complete":
            # Also return config so the UI knows we're configured
            cfg = svc.get_config()
            result.update(cfg)
        return result
    except Exception as e:
        err_str = str(e) or f"{type(e).__name__}: network/timeout error"
        logger.error(f"Device flow poll failed: {err_str}")
        raise HTTPException(status_code=500, detail=err_str)


@router.post("/auth/disconnect")
async def disconnect_github():
    """Sign out of GitHub OAuth — clears stored tokens."""
    svc = get_copilot_service()
    svc.disconnect()
    return {"status": "disconnected"}


@router.delete("/session/{session_id}")
async def clear_copilot_session(session_id: str):
    """Clear conversation history for a Copilot session."""
    svc = get_copilot_service()
    svc.clear_session(session_id)
    return {"cleared": True, "session_id": session_id}


@router.post("/recalculate-costs")
async def recalculate_costs():
    """Recalculate estimated_cost for all stored log records using current pricing."""
    try:
        result = query_log_service.recalculate_all_costs()
        return {"success": True, **result}
    except Exception as e:
        logger.error(f"Failed to recalculate costs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Cross-Database Connections ────────────────────────────────────

class SavedConnectionInfo(BaseModel):
    name: str
    dbType: str = "postgresql"
    hostname: str
    port: int = 5432
    database: str
    username: str
    password: str = ""


class RegisterConnectionsRequest(BaseModel):
    connections: List[SavedConnectionInfo]


@router.post("/connections")
async def register_connections(request: RegisterConnectionsRequest):
    """
    Register saved database connections so the Copilot agent can switch
    between databases automatically when a table is not found.
    """
    svc = get_copilot_service()
    conn_dicts = [c.model_dump() for c in request.connections]
    svc.register_connections(conn_dicts)
    return {
        "registered": len(conn_dicts),
        "databases": [c.database for c in request.connections],
    }
