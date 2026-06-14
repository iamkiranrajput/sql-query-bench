"""
Validate Server Compatibility Tool

Check whether generated SQL is compatible with the target database server.
Parses SQL to detect features used (window functions, CTEs, LATERAL, JSON, etc.),
compares against server capabilities, optionally runs EXPLAIN for planner
validation, and suggests alternatives when incompatible.
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional

import sqlparse

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Injected by MCP server during initialisation
_db_service = None


def set_database_service(db_service) -> None:
    """Set the database service instance."""
    global _db_service
    _db_service = db_service


# ── SQL feature detection patterns ──────────────────────────────────

_FEATURE_PATTERNS: List[Dict[str, Any]] = [
    {
        "name": "CTE",
        "pattern": re.compile(r"\bWITH\s+\w+\s+AS\s*\(", re.IGNORECASE),
        "capability": "CTEs (WITH)",
        "fallback": "Rewrite the CTE as a subquery in the FROM clause.",
    },
    {
        "name": "window_function",
        "pattern": re.compile(
            r"\b(ROW_NUMBER|RANK|DENSE_RANK|NTILE|LAG|LEAD|FIRST_VALUE|LAST_VALUE|NTH_VALUE|PERCENT_RANK|CUME_DIST)\s*\(",
            re.IGNORECASE,
        ),
        "capability": "window_functions",
        "fallback": "Replace window functions with correlated subqueries or self-joins.",
    },
    {
        "name": "OVER_clause",
        "pattern": re.compile(r"\bOVER\s*\(", re.IGNORECASE),
        "capability": "window_functions",
        "fallback": "Replace the OVER() clause with a correlated subquery.",
    },
    {
        "name": "LATERAL_join",
        "pattern": re.compile(r"\bLATERAL\s+", re.IGNORECASE),
        "capability": "LATERAL_joins",
        "fallback": "Rewrite the LATERAL join as a correlated subquery in the SELECT list.",
    },
    {
        "name": "JSONB_operator",
        "pattern": re.compile(r"(->>'?|->>|#>>?|@>|<@|\?\||\?&)", re.IGNORECASE),
        "capability": "JSONB",
        "fallback": "Use CAST or text functions instead of JSONB operators.",
    },
    {
        "name": "JSON_path",
        "pattern": re.compile(r"\bjsonb?_path_(query|exists|match)\b", re.IGNORECASE),
        "capability": "JSON_path_queries",
        "fallback": "Use nested -> / ->> operators instead of JSON path functions.",
    },
    {
        "name": "ARRAY_AGG",
        "pattern": re.compile(r"\bARRAY_AGG\s*\(", re.IGNORECASE),
        "capability": "ARRAY_AGG_ORDER_BY",
        "fallback": "Use STRING_AGG or multiple rows instead of ARRAY_AGG.",
    },
    {
        "name": "FILTER_clause",
        "pattern": re.compile(r"\bFILTER\s*\(\s*WHERE\b", re.IGNORECASE),
        "capability": "aggregate_FILTER",
        "fallback": "Use CASE WHEN inside the aggregate instead of FILTER (WHERE ...).",
    },
    {
        "name": "GROUPING_SETS",
        "pattern": re.compile(r"\b(GROUPING\s+SETS|ROLLUP|CUBE)\s*\(", re.IGNORECASE),
        "capability": "GROUPING SETS",
        "fallback": "Use multiple queries with UNION ALL instead of GROUPING SETS/ROLLUP/CUBE.",
    },
    {
        "name": "MERGE_statement",
        "pattern": re.compile(r"\bMERGE\s+INTO\b", re.IGNORECASE),
        "capability": "MERGE_statement",
        "fallback": "Use INSERT ... ON CONFLICT (UPSERT) or separate INSERT/UPDATE statements.",
    },
    {
        "name": "TABLESAMPLE",
        "pattern": re.compile(r"\bTABLESAMPLE\b", re.IGNORECASE),
        "capability": "TABLESAMPLE",
        "fallback": "Use ORDER BY RANDOM() LIMIT N instead of TABLESAMPLE.",
    },
    {
        "name": "REGEXP_operator",
        "pattern": re.compile(r"~\*?|!~\*?|\bREGEXP_REPLACE\b|\bREGEXP_MATCHES\b", re.IGNORECASE),
        "capability": "SELECT",  # basic — PG always has regex
        "fallback": None,
    },
    {
        "name": "INTERSECT",
        "pattern": re.compile(r"\bINTERSECT\b", re.IGNORECASE),
        "capability": "INTERSECT_EXCEPT",
        "fallback": "Use an INNER JOIN on matching columns instead of INTERSECT.",
    },
    {
        "name": "EXCEPT",
        "pattern": re.compile(r"\bEXCEPT\b", re.IGNORECASE),
        "capability": "INTERSECT_EXCEPT",
        "fallback": "Use a LEFT JOIN with IS NULL instead of EXCEPT.",
    },
]


def _detect_features(sql: str) -> List[Dict[str, str]]:
    """Detect SQL features used in a query."""
    features = []
    seen = set()
    for fp in _FEATURE_PATTERNS:
        if fp["name"] in seen:
            continue
        if fp["pattern"].search(sql):
            features.append({
                "name": fp["name"],
                "capability": fp["capability"],
                "fallback": fp["fallback"],
            })
            seen.add(fp["name"])
    return features


def _get_server_capabilities(session) -> List[str]:
    """Get capabilities for the session's server (re-uses get_connection_profile logic)."""
    try:
        from .get_connection_profile import get_connection_profile, _profile_cache
        sid = session.session_id if hasattr(session, "session_id") else ""
        if sid and sid in _profile_cache:
            return _profile_cache[sid].get("capabilities", [])
        # Fetch fresh profile
        profile = get_connection_profile(sid, include_capabilities=True)
        return profile.get("capabilities", [])
    except Exception as e:
        logger.debug(f"Could not get server capabilities: {e}")
        return []


def validate_server_compatibility(
    session_id: str,
    sql_query: str,
    fallback_options: bool = True,
) -> Dict[str, Any]:
    """
    Check if generated SQL is compatible with the target database server.

    Args:
        session_id: Database session ID from connection.
        sql_query: SQL query to validate.
        fallback_options: Generate alternative suggestions if incompatible.

    Returns:
        {
            "success": bool,
            "compatible": bool,
            "score": int (0-100),
            "features_detected": [{name, capability, fallback}],
            "issues": [str],
            "suggestions": [str],
            "explain_plan": str | null,
            "explain_error": str | null,
            "error": str | null
        }
    """
    if not _db_service:
        return {"success": False, "error": "Database service not initialized"}
    if not session_id:
        return {"success": False, "error": "session_id is required"}
    if not sql_query or not sql_query.strip():
        return {"success": False, "error": "sql_query is required"}

    # Security: only SELECT / WITH allowed
    sql_upper = sql_query.strip().upper()
    if not (sql_upper.startswith("SELECT") or sql_upper.startswith("WITH")):
        return {"success": False, "error": "Only SELECT queries can be validated"}

    # Reject multiple statements
    if len(sqlparse.split(sql_query.strip())) > 1:
        return {"success": False, "error": "Multiple SQL statements are not allowed"}

    session = _db_service.get_session(session_id)
    if not session:
        return {"success": False, "error": "Invalid or expired database session"}

    result: Dict[str, Any] = {
        "success": True,
        "compatible": True,
        "score": 100,
        "features_detected": [],
        "issues": [],
        "suggestions": [],
        "explain_plan": None,
        "explain_error": None,
        "error": None,
    }

    # ── Detect SQL features ──────────────────────────────────────
    features = _detect_features(sql_query)
    result["features_detected"] = features

    # ── Compare against server capabilities ──────────────────────
    capabilities = _get_server_capabilities(session)
    cap_set = set(capabilities)

    for feat in features:
        required_cap = feat["capability"]
        if required_cap and required_cap not in cap_set:
            result["compatible"] = False
            result["score"] -= 20
            result["issues"].append(
                f"Feature '{feat['name']}' requires capability '{required_cap}' "
                "which is not available on this server."
            )
            if fallback_options and feat.get("fallback"):
                result["suggestions"].append(feat["fallback"])

    # Clamp score
    result["score"] = max(0, result["score"])

    # ── EXPLAIN plan (planner validation without execution) ──────
    db_type = getattr(session, "db_type", "postgresql")
    if db_type == "postgresql":
        try:
            with session.engine.connect() as conn:
                # Set a short statement timeout for the EXPLAIN
                try:
                    conn.execute(text("SET statement_timeout = 5000"))
                except Exception:
                    pass

                explain_sql = f"EXPLAIN (FORMAT TEXT) {sql_query}"
                rows = conn.execute(text(explain_sql)).fetchall()
                plan_lines = [str(r[0]) for r in rows]
                result["explain_plan"] = "\n".join(plan_lines)

                # Extract cost estimate from first line
                cost_match = re.search(
                    r"cost=[\d.]+\.\.([\d.]+)\s+rows=(\d+)",
                    plan_lines[0] if plan_lines else "",
                )
                if cost_match:
                    total_cost = float(cost_match.group(1))
                    est_rows = int(cost_match.group(2))
                    if total_cost > 100000:
                        result["issues"].append(
                            f"High estimated cost ({total_cost:.0f}). "
                            "Consider adding filters or indexes."
                        )
                        result["score"] = max(0, result["score"] - 10)
                    if est_rows > 1000000:
                        result["issues"].append(
                            f"Estimated {est_rows} rows. Add LIMIT or narrow filters."
                        )
                        result["score"] = max(0, result["score"] - 10)

                # Detect sequential scans on large tables
                for line in plan_lines:
                    if "Seq Scan" in line:
                        tbl_match = re.search(r"Seq Scan on (\w+)", line)
                        tbl_name = tbl_match.group(1) if tbl_match else "a table"
                        cost_m = re.search(r"rows=(\d+)", line)
                        if cost_m and int(cost_m.group(1)) > 50000:
                            result["suggestions"].append(
                                f"Sequential scan on `{tbl_name}` with many rows. "
                                "Add a WHERE filter to use an index."
                            )

        except Exception as e:
            err_str = str(e)
            result["explain_error"] = err_str
            # If EXPLAIN fails the SQL itself is likely invalid
            if "syntax error" in err_str.lower() or "does not exist" in err_str.lower():
                result["compatible"] = False
                result["score"] = max(0, result["score"] - 30)
                result["issues"].append(f"EXPLAIN failed: {err_str[:300]}")
    elif db_type == "mysql":
        try:
            with session.engine.connect() as conn:
                rows = conn.execute(text(f"EXPLAIN {sql_query}")).fetchall()
                cols = ["id", "select_type", "table", "type", "possible_keys",
                        "key", "key_len", "ref", "rows", "Extra"]
                plan_lines = []
                for r in rows:
                    parts = [f"{cols[i]}={r[i]}" for i in range(min(len(cols), len(r)))]
                    plan_lines.append(", ".join(parts))
                result["explain_plan"] = "\n".join(plan_lines)
        except Exception as e:
            result["explain_error"] = str(e)

    return result


# ── Tool metadata for MCP registration ─────────────────────────────

TOOL_METADATA = {
    "name": "validate_server_compatibility",
    "description": (
        "Check if a SQL query is compatible with the connected database server. "
        "Detects unsupported SQL features and runs EXPLAIN for planner validation. "
        "Only call this when a query fails with a syntax or feature error, "
        "or when the user explicitly asks to check compatibility. "
        "Do NOT call for normal queries."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Database session ID from connection",
            },
            "sql_query": {
                "type": "string",
                "description": "The SQL query to validate for compatibility",
            },
            "fallback_options": {
                "type": "boolean",
                "description": "Generate alternative SQL suggestions if incompatible (default: true)",
                "default": True,
            },
        },
        "required": ["session_id", "sql_query"],
    },
}
