"""
Debug Logs API - View Full LLM Prompts & Transparency Data
Allows viewing exactly what was sent to the LLM for each query
"""

import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from typing import Optional
from app.config.settings import settings
from app.services.database_service import database_service
from app.services.cache_service import cache_manager
from app.services.logger_service import setup_logger
from sqlalchemy import text

router = APIRouter(prefix="/api/debug", tags=["Debug Logs"])
logger = setup_logger(__name__)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _require_safe_debug_access(request: Request) -> None:
    """
    /api/debug/* exposes raw LLM prompts, schema context, and generated SQL
    for the last N queries -- effectively an inventory of how the system
    thinks about every business question. That is a goldmine for an attacker
    mapping the application, so we refuse access unless one of the following
    holds:

    1. The global API_KEY auth middleware is active. When ``settings.api_key``
       is set, ApiKeyAuthMiddleware has already required a valid bearer token
       on every /api/* route, including this one -- we do not double-gate.
    2. The request is coming from the loopback interface. This keeps the
       "open in local dev, no .env touched" workflow alive without exposing
       the route through any reverse proxy.

    Anything else — e.g. a remote request that arrived via a misconfigured
    proxy or LAN-exposed deployment when the operator forgot to set
    API_KEY — is denied.
    """
    if (settings.api_key or "").strip():
        return  # middleware already enforced bearer auth

    peer = request.client.host if request.client else ""
    if peer in _LOOPBACK_HOSTS:
        return
    try:
        if ipaddress.ip_address(peer).is_loopback:
            return
    except ValueError:
        pass

    logger.warning(
        "[DEBUG_LOGS] Refusing %s %s from %s -- API_KEY unset and peer is not loopback",
        request.method, request.url.path, peer or "<unknown>",
    )
    raise HTTPException(
        status_code=403,
        detail=(
            "debug logs are disabled when API_KEY is unset and the request "
            "is not from localhost. Set API_KEY in .env to enable remote access."
        ),
    )


@router.get("/logs")
async def get_debug_logs(
    request: Request,
    session_id: Optional[str] = Query(None, description="Filter by session ID"),
    limit: int = Query(20, description="Number of logs to return", le=100),
    refresh: bool = Query(False, description="Force refresh cache (bypasses 60s cache)"),
    _gate: None = Depends(_require_safe_debug_access),
):
    """
    Get query logs with full transparency data.
    
    NOTE: Results are cached for 60 seconds to reduce database load.
    Use refresh=true to force fresh data.
    
    Returns:
        - User query
        - Generated SQL
        - Full LLM prompt
        - Schema context sent
        - Hints used
        - Filtered tables
    """
    try:
        # Check cache first (unless refresh requested)
        cache_key = f"debug_logs:{session_id or 'all'}:{limit}"
        if not refresh:
            cached_logs = cache_manager.general.get(cache_key)
            if cached_logs:
                logger.debug(f"Debug logs cache HIT: {cache_key}")
                return {
                    "count": len(cached_logs),
                    "logs": cached_logs,
                    "cached": True
                }
        
        # Get any active session to query the logs database
        sessions = list(database_service.sessions.values())
        if not sessions:
            raise HTTPException(status_code=400, detail="No active database connection")
        
        engine = sessions[0].engine
        
        # Build query
        if session_id:
            query = text("""
                SELECT 
                    id, username, user_prompt, generated_sql, 
                    execution_status, row_count, execution_time, error_message,
                    created_at, session_id,
                    full_prompt, schema_context, hints_used, filtered_tables
                FROM ai_chatbot_query_logs
                WHERE session_id = :session_id
                ORDER BY created_at DESC
                LIMIT :limit
            """)
            params = {"session_id": session_id, "limit": limit}
        else:
            query = text("""
                SELECT 
                    id, username, user_prompt, generated_sql, 
                    execution_status, row_count, execution_time, error_message,
                    created_at, session_id,
                    full_prompt, schema_context, hints_used, filtered_tables
                FROM ai_chatbot_query_logs
                ORDER BY created_at DESC
                LIMIT :limit
            """)
            params = {"limit": limit}
        
        with engine.connect() as conn:
            result = conn.execute(query, params)
            rows = result.fetchall()
        
        # Format results
        logs = []
        for row in rows:
            logs.append({
                "id": row[0],
                "username": row[1],
                "user_prompt": row[2],
                "generated_sql": row[3],
                "execution_status": row[4],
                "row_count": row[5],
                "execution_time": float(row[6]) if row[6] else None,
                "error_message": row[7],
                "created_at": str(row[8]),
                "session_id": row[9],
                "debug": {
                    "full_prompt": row[10],
                    "schema_context": row[11],
                    "hints_used": row[12],
                    "filtered_tables": row[13]
                }
            })
        
        # Cache for 60 seconds to reduce database load
        cache_manager.general.set(cache_key, logs, ttl=60)
        
        return {
            "count": len(logs),
            "logs": logs,
            "cached": False
        }
    
    except Exception as e:
        logger.error(f"Failed to fetch debug logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/logs/{log_id}")
async def get_single_log(
    log_id: int,
    _gate: None = Depends(_require_safe_debug_access),
):
    """
    Get full details of a single query log including complete LLM prompt.
    
    Perfect for debugging why a query succeeded or failed.
    """
    try:
        sessions = list(database_service.sessions.values())
        if not sessions:
            raise HTTPException(status_code=400, detail="No active database connection")
        
        engine = sessions[0].engine
        
        query = text("""
            SELECT 
                id, username, user_prompt, generated_sql, 
                execution_status, row_count, execution_time, error_message,
                created_at, session_id,
                full_prompt, schema_context, hints_used, filtered_tables
            FROM ai_chatbot_query_logs
            WHERE id = :log_id
        """)
        
        with engine.connect() as conn:
            result = conn.execute(query, {"log_id": log_id})
            row = result.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail=f"Log ID {log_id} not found")
        
        return {
            "id": row[0],
            "username": row[1],
            "user_prompt": row[2],
            "generated_sql": row[3],
            "execution_status": row[4],
            "row_count": row[5],
            "execution_time": float(row[6]) if row[6] else None,
            "error_message": row[7],
            "created_at": str(row[8]),
            "session_id": row[9],
            "transparency": {
                "full_llm_prompt": row[10],
                "schema_context_sent": row[11],
                "hints_used": row[12],
                "filtered_tables": row[13]
            }
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch log {log_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/logs/{log_id}/prompt")
async def get_full_prompt(
    log_id: int,
    _gate: None = Depends(_require_safe_debug_access),
):
    """
    Get ONLY the full LLM prompt for a query (for copy-paste testing).
    """
    try:
        sessions = list(database_service.sessions.values())
        if not sessions:
            raise HTTPException(status_code=400, detail="No active database connection")
        
        engine = sessions[0].engine
        
        query = text("""
            SELECT full_prompt
            FROM ai_chatbot_query_logs
            WHERE id = :log_id
        """)
        
        with engine.connect() as conn:
            result = conn.execute(query, {"log_id": log_id})
            row = result.fetchone()
        
        if not row or not row[0]:
            raise HTTPException(status_code=404, detail=f"Prompt not found for log ID {log_id}")
        
        return {
            "log_id": log_id,
            "full_prompt": row[0]
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch prompt for log {log_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
