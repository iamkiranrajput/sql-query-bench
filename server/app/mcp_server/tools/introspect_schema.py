"""
Introspect Schema Tool

Live database schema introspection via information_schema.
Provides real-time column names, data types, and constraints
instead of relying on static schema_hints.json.
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


def introspect_schema(
    session_id: str,
    table_name: Optional[str] = None,
    column_name: Optional[str] = None,
    search_pattern: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Live schema introspection from information_schema.

    Queries the actual database catalog for real-time column names,
    data types, nullable constraints, and key information.

    Args:
        session_id: Database session ID from connection.
        table_name: Specific table to introspect (optional).
        column_name: Filter to a specific column (optional).
        search_pattern: ILIKE pattern to search column names (optional).

    Returns:
        {
            "success": bool,
            "tables": [{"table_name": str, "columns": [...]}],
            "total_columns": int,
            "error": str | None
        }
    """
    try:
        if not _db_service:
            return {
                "success": False,
                "tables": [],
                "total_columns": 0,
                "error": "Database service not initialized",
            }

        if not session_id:
            return {
                "success": False,
                "tables": [],
                "total_columns": 0,
                "error": "session_id is required",
            }

        # B7: Build query against information_schema using SQLAlchemy bound
        # parameters instead of string interpolation. This eliminates the SQL
        # injection surface entirely for user-supplied table/column/pattern
        # values regardless of upstream caller sanitisation.
        conditions: List[str] = ["table_schema = 'public'"]
        params: Dict[str, Any] = {}

        if table_name:
            # Still apply a strict identifier whitelist as defense-in-depth so a
            # malformed identifier surfaces no rows instead of a DB error.
            safe_table = re.sub(r'[^a-zA-Z0-9_]', '', table_name)
            conditions.append("table_name = :p_table_name")
            params["p_table_name"] = safe_table

        if column_name:
            safe_col = re.sub(r'[^a-zA-Z0-9_]', '', column_name)
            conditions.append("column_name = :p_column_name")
            params["p_column_name"] = safe_col

        if search_pattern:
            # Bound parameter handles any quoting; reject only control chars
            # (newlines etc.) that could disrupt logging/auditing of the value.
            cleaned_pattern = "".join(
                ch for ch in search_pattern if ch == "%" or ch == "_" or ch.isalnum()
            )
            if cleaned_pattern:
                conditions.append("column_name ILIKE :p_search_pattern")
                params["p_search_pattern"] = cleaned_pattern

        where_clause = " AND ".join(conditions)

        sql = f"""
            SELECT
                table_name,
                column_name,
                data_type,
                character_maximum_length,
                is_nullable,
                column_default,
                ordinal_position
            FROM information_schema.columns
            WHERE {where_clause}
            ORDER BY table_name, ordinal_position
            LIMIT 2000
        """

        # Execute via database service
        session = _db_service.get_session(session_id)
        if not session:
            return {
                "success": False,
                "tables": [],
                "total_columns": 0,
                "error": f"Invalid session: {session_id}",
            }

        records, columns, row_count = _db_service.execute_query(
            session_id, sql, params=params
        )

        # Group results by table
        tables_map: Dict[str, List[Dict]] = {}
        for row in records:
            tname = row.get("table_name", "")
            if tname not in tables_map:
                tables_map[tname] = []
            tables_map[tname].append({
                "column_name": row.get("column_name"),
                "data_type": row.get("data_type"),
                "max_length": row.get("character_maximum_length"),
                "is_nullable": row.get("is_nullable") == "YES",
                "column_default": row.get("column_default"),
                "ordinal_position": row.get("ordinal_position"),
            })

        tables_list = [
            {"table_name": tname, "columns": cols, "column_count": len(cols)}
            for tname, cols in sorted(tables_map.items())
        ]

        total_cols = sum(len(t["columns"]) for t in tables_list)

        logger.debug(
            f"introspect_schema: {len(tables_list)} tables, {total_cols} columns"
        )

        return {
            "success": True,
            "tables": tables_list,
            "total_columns": total_cols,
            "error": None,
        }

    except Exception as e:
        logger.error(f"introspect_schema failed: {e}")
        return {
            "success": False,
            "tables": [],
            "total_columns": 0,
            "error": str(e),
        }


# Tool metadata for MCP registration
TOOL_METADATA = {
    "name": "introspect_schema",
    "description": (
        "Live database schema introspection. Queries information_schema for "
        "real-time column names, data types, and constraints. Use this when "
        "you need accurate column names for a table, especially if a query "
        "fails with 'column does not exist' errors. More accurate than "
        "search_columns for column name validation."
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
                "description": "Specific table to introspect (optional, omit to search all)",
            },
            "column_name": {
                "type": "string",
                "description": "Filter to a specific column name (optional)",
            },
            "search_pattern": {
                "type": "string",
                "description": "ILIKE pattern to search column names, e.g. '%%speed%%' (optional)",
            },
        },
        "required": ["session_id"],
    },
}
