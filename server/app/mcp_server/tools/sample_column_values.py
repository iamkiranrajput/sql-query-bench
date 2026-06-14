"""
Sample Column Values Tool

Get DISTINCT values for specific columns with optional pattern matching.
Enables targeted value discovery for smarter SQL filter generation.
"""

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Will be injected by the MCP server
_db_service = None


def set_database_service(db_service) -> None:
    """Set the database service instance."""
    global _db_service
    _db_service = db_service


def sample_column_values(
    session_id: str,
    table_name: str,
    column_name: str,
    pattern: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """
    Get distinct values for a specific column, optionally filtered by pattern.

    Useful for understanding what values exist in a column before building
    WHERE clauses. Can also match user-provided keywords against real data.

    Args:
        session_id: Database session ID from connection.
        table_name: Table to sample from.
        column_name: Column to get distinct values for.
        pattern: Optional ILIKE pattern to filter values (e.g. '%%XR%%').
        limit: Maximum distinct values to return (default: 50, max: 200).

    Returns:
        {
            "success": bool,
            "values": [str | number],
            "total_distinct": int,
            "matched_pattern": str | None,
            "column_name": str,
            "table_name": str,
            "error": str | None
        }
    """
    try:
        if not _db_service:
            return {
                "success": False,
                "values": [],
                "total_distinct": 0,
                "matched_pattern": None,
                "column_name": column_name,
                "table_name": table_name,
                "error": "Database service not initialized",
            }

        if not session_id:
            return {
                "success": False,
                "values": [],
                "total_distinct": 0,
                "matched_pattern": None,
                "column_name": column_name,
                "table_name": table_name,
                "error": "session_id is required",
            }

        # Sanitize inputs
        safe_table = re.sub(r'[^a-zA-Z0-9_]', '', table_name)
        safe_column = re.sub(r'[^a-zA-Z0-9_]', '', column_name)
        safe_limit = min(max(1, limit), 200)

        if not safe_table or not safe_column:
            return {
                "success": False,
                "values": [],
                "total_distinct": 0,
                "matched_pattern": None,
                "column_name": column_name,
                "table_name": table_name,
                "error": "Invalid table_name or column_name",
            }

        session = _db_service.get_session(session_id)
        if not session:
            return {
                "success": False,
                "values": [],
                "total_distinct": 0,
                "matched_pattern": None,
                "column_name": column_name,
                "table_name": table_name,
                "error": f"Invalid session: {session_id}",
            }

        # Build query
        where_parts = [f"{safe_column} IS NOT NULL"]
        safe_pattern_val = None

        if pattern:
            # Sanitize pattern – block injection characters
            clean_pattern = pattern.replace("'", "''")
            if not any(c in clean_pattern for c in [';', '--', '/*']):
                where_parts.append(f"CAST({safe_column} AS TEXT) ILIKE '{clean_pattern}'")
                safe_pattern_val = clean_pattern

        where_clause = " AND ".join(where_parts)

        # Get distinct values
        values_sql = (
            f"SELECT DISTINCT {safe_column} AS val "
            f"FROM {safe_table} "
            f"WHERE {where_clause} "
            f"ORDER BY {safe_column} "
            f"LIMIT {safe_limit}"
        )

        result_records, result_cols, result_count = _db_service.execute_query(session_id, values_sql)

        values = [r.get("val") for r in result_records if r.get("val") is not None]

        # Get total distinct count
        count_sql = (
            f"SELECT COUNT(DISTINCT {safe_column}) AS cnt "
            f"FROM {safe_table} "
            f"WHERE {safe_column} IS NOT NULL"
        )
        count_records, _, _ = _db_service.execute_query(session_id, count_sql)
        total_distinct = 0
        if count_records:
            total_distinct = count_records[0].get("cnt", 0)

        logger.debug(
            f"sample_column_values: {safe_table}.{safe_column} → "
            f"{len(values)} values (total distinct: {total_distinct})"
        )

        return {
            "success": True,
            "values": values,
            "total_distinct": total_distinct,
            "matched_pattern": safe_pattern_val,
            "column_name": safe_column,
            "table_name": safe_table,
            "error": None,
        }

    except Exception as e:
        logger.error(f"sample_column_values failed: {e}")
        return {
            "success": False,
            "values": [],
            "total_distinct": 0,
            "matched_pattern": None,
            "column_name": column_name,
            "table_name": table_name,
            "error": str(e),
        }


# Tool metadata for MCP registration
TOOL_METADATA = {
    "name": "sample_column_values",
    "description": (
        "Get distinct values for a specific column in a table. "
        "Optionally filter by ILIKE pattern. Use this to discover what "
        "actual values exist in a column before building WHERE clauses. "
        "Helps avoid generating queries with wrong filter values."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Database session ID from connection",
            },
            "table_name": {
                "type": "string",
                "description": "Table to sample values from",
            },
            "column_name": {
                "type": "string",
                "description": "Column to get distinct values for",
            },
            "pattern": {
                "type": "string",
                "description": "Optional ILIKE pattern to filter values, e.g. '%%XR%%'",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum distinct values to return (default: 50, max: 200)",
                "default": 50,
            },
        },
        "required": ["session_id", "table_name", "column_name"],
    },
}
