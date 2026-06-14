"""
Get Connection Profile Tool

Retrieve detailed server connection metadata including database engine type,
version, capabilities, connection pool status, and server settings.
Enables the LLM agent to generate engine-specific, optimised SQL.
"""

import logging
import time
from typing import Any, Dict, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Injected by MCP server during initialisation
_db_service = None

# Cache per session_id — capabilities don't change mid-session
_profile_cache: Dict[str, Dict[str, Any]] = {}


def set_database_service(db_service) -> None:
    """Set the database service instance."""
    global _db_service
    _db_service = db_service


def _parse_pg_version(version_str: str) -> Dict[str, Any]:
    """Extract major/minor version numbers from PostgreSQL version() output."""
    import re
    m = re.search(r"PostgreSQL\s+(\d+)\.(\d+)", version_str, re.IGNORECASE)
    if m:
        return {"major": int(m.group(1)), "minor": int(m.group(2))}
    m = re.search(r"PostgreSQL\s+(\d+)", version_str, re.IGNORECASE)
    if m:
        return {"major": int(m.group(1)), "minor": 0}
    return {"major": 0, "minor": 0}


def _pg_capabilities(major: int, minor: int) -> list:
    """Determine PostgreSQL feature capabilities from version numbers."""
    caps = [
        "SELECT", "JOIN", "GROUP BY", "ORDER BY", "HAVING",
        "subqueries", "UNION", "INTERSECT", "EXCEPT",
        "CASE expressions", "COALESCE", "NULLIF",
        "aggregate_functions",
    ]
    # PG 8.4+
    if major >= 9 or (major == 8 and minor >= 4):
        caps.extend(["window_functions", "CTEs (WITH)"])
    # PG 9.1+
    if major >= 10 or (major == 9 and minor >= 1):
        caps.append("writable_CTEs")
    # PG 9.3+
    if major >= 10 or (major == 9 and minor >= 3):
        caps.extend(["LATERAL_joins", "materialized_views"])
    # PG 9.4+
    if major >= 10 or (major == 9 and minor >= 4):
        caps.extend(["JSONB", "ARRAY_AGG_ORDER_BY", "aggregate_FILTER"])
    # PG 9.5+
    if major >= 10 or (major == 9 and minor >= 5):
        caps.extend(["UPSERT (ON CONFLICT)", "TABLESAMPLE", "GROUPING SETS", "ROLLUP", "CUBE"])
    # PG 10+
    if major >= 10:
        caps.extend(["identity_columns", "declarative_partitioning"])
    # PG 12+
    if major >= 12:
        caps.extend(["generated_columns", "JSON_path_queries"])
    # PG 13+
    if major >= 13:
        caps.append("incremental_sorting")
    # PG 14+
    if major >= 14:
        caps.extend(["multirange_types", "JSON_subscripting"])
    # PG 15+
    if major >= 15:
        caps.append("MERGE_statement")
    # PG 16+
    if major >= 16:
        caps.extend(["SQL_standard_JSON_functions", "parallel_FULL_OUTER_JOIN"])
    return caps


def _mysql_capabilities(major: int, minor: int) -> list:
    """Determine MySQL feature capabilities from version numbers."""
    caps = [
        "SELECT", "JOIN", "GROUP BY", "ORDER BY", "HAVING",
        "subqueries", "UNION", "CASE expressions", "aggregate_functions",
    ]
    if major >= 8:
        caps.extend(["window_functions", "CTEs (WITH)", "JSON_functions", "LATERAL_derived_tables"])
    if major >= 8 and minor >= 1:
        caps.append("INTERSECT_EXCEPT")
    return caps


def get_connection_profile(
    session_id: str,
    include_performance_metrics: bool = False,
    include_capabilities: bool = True,
) -> Dict[str, Any]:
    """
    Retrieve detailed server connection metadata.

    Args:
        session_id: Database session ID from connection.
        include_performance_metrics: Include pool status and latency info.
        include_capabilities: Include detected SQL feature capabilities.

    Returns:
        {
            "success": bool,
            "db_engine": str,
            "version": str,
            "version_major": int,
            "version_minor": int,
            "hostname": str,
            "database": str,
            "db_type": str,
            "capabilities": [str],
            "server_settings": {str: str},
            "pool_status": {str: Any} | None,
            "latency_ms": float | None,
            "error": str | None
        }
    """
    if not _db_service:
        return {"success": False, "error": "Database service not initialized"}
    if not session_id:
        return {"success": False, "error": "session_id is required"}

    # Return cached profile if available
    if session_id in _profile_cache:
        cached = _profile_cache[session_id]
        # Re-fetch performance metrics even if profile is cached
        if include_performance_metrics:
            cached = dict(cached)
            session = _db_service.get_session(session_id)
            if session:
                cached["pool_status"] = _get_pool_status(session)
                cached["latency_ms"] = _measure_latency(session)
        return cached

    session = _db_service.get_session(session_id)
    if not session:
        return {"success": False, "error": "Invalid or expired database session"}

    db_type = getattr(session, "db_type", "postgresql")
    hostname = getattr(session, "hostname", "unknown")
    database = getattr(session, "database", "unknown")

    result: Dict[str, Any] = {
        "success": True,
        "db_engine": db_type,
        "version": "",
        "version_major": 0,
        "version_minor": 0,
        "hostname": hostname,
        "database": database,
        "db_type": db_type,
        "capabilities": [],
        "server_settings": {},
        "pool_status": None,
        "latency_ms": None,
        "error": None,
    }

    try:
        with session.engine.connect() as conn:
            # ── Version detection ────────────────────────────────
            if db_type == "postgresql":
                row = conn.execute(text("SELECT version()")).scalar()
                result["version"] = str(row) if row else ""
                parsed = _parse_pg_version(result["version"])
                result["version_major"] = parsed["major"]
                result["version_minor"] = parsed["minor"]
            elif db_type == "mysql":
                row = conn.execute(text("SELECT VERSION()")).scalar()
                result["version"] = str(row) if row else ""
                import re
                m = re.search(r"(\d+)\.(\d+)", result["version"])
                if m:
                    result["version_major"] = int(m.group(1))
                    result["version_minor"] = int(m.group(2))
            elif db_type == "mssql":
                row = conn.execute(text("SELECT @@VERSION")).scalar()
                result["version"] = str(row) if row else ""
            else:
                try:
                    row = conn.execute(text("SELECT version()")).scalar()
                    result["version"] = str(row) if row else ""
                except Exception:
                    result["version"] = "unknown"

            # ── Server settings (PostgreSQL) ─────────────────────
            if db_type == "postgresql":
                _pg_settings = [
                    "max_connections", "shared_buffers", "work_mem",
                    "effective_cache_size", "maintenance_work_mem",
                    "default_statistics_target", "random_page_cost",
                    "seq_page_cost", "max_parallel_workers_per_gather",
                ]
                for setting in _pg_settings:
                    try:
                        val = conn.execute(
                            text("SELECT current_setting(:s)"),
                            {"s": setting},
                        ).scalar()
                        result["server_settings"][setting] = str(val)
                    except Exception:
                        pass

            # ── Capabilities ─────────────────────────────────────
            if include_capabilities:
                major = result["version_major"]
                minor = result["version_minor"]
                if db_type == "postgresql":
                    result["capabilities"] = _pg_capabilities(major, minor)
                elif db_type == "mysql":
                    result["capabilities"] = _mysql_capabilities(major, minor)
                else:
                    result["capabilities"] = [
                        "SELECT", "JOIN", "GROUP BY", "ORDER BY",
                        "subqueries", "aggregate_functions",
                    ]

            # ── Performance metrics (optional) ───────────────────
            if include_performance_metrics:
                result["pool_status"] = _get_pool_status(session)
                result["latency_ms"] = _measure_latency(session)

    except Exception as e:
        logger.error(f"get_connection_profile failed: {e}")
        result["error"] = str(e)

    # Cache (without volatile performance metrics)
    cache_entry = dict(result)
    cache_entry.pop("pool_status", None)
    cache_entry.pop("latency_ms", None)
    _profile_cache[session_id] = cache_entry

    return result


def _get_pool_status(session) -> Dict[str, Any]:
    """Get connection pool status from the engine."""
    try:
        pool = session.engine.pool
        return {
            "pool_size": pool.size(),
            "checked_in": pool.checkedin(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow(),
        }
    except Exception:
        return {}


def _measure_latency(session) -> Optional[float]:
    """Measure round-trip latency with a trivial query."""
    try:
        with session.engine.connect() as conn:
            t0 = time.perf_counter()
            conn.execute(text("SELECT 1"))
            return round((time.perf_counter() - t0) * 1000, 2)
    except Exception:
        return None


# ── Tool metadata for MCP registration ─────────────────────────────

TOOL_METADATA = {
    "name": "get_connection_profile",
    "description": (
        "Retrieve server connection metadata: database engine type/version, "
        "SQL feature capabilities, and server settings. "
        "Only call this when the user explicitly asks about the server version, "
        "database capabilities, or connection details. Do NOT call for normal queries."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Database session ID from connection",
            },
            "include_performance_metrics": {
                "type": "boolean",
                "description": "Include pool status and round-trip latency (default: false)",
                "default": False,
            },
            "include_capabilities": {
                "type": "boolean",
                "description": "Include detected SQL feature capabilities (default: true)",
                "default": True,
            },
        },
        "required": ["session_id"],
    },
}
