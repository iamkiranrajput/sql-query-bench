"""
Preview Data Tool

Safe, read-only data preview from database tables.
Executes SELECT with LIMIT for quick data exploration.
"""

import logging
import re
from typing import Any, Dict, List, Optional

_IDENTIFIER_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_.]*$')

logger = logging.getLogger(__name__)

# Will be injected by the MCP server
_db_service = None


def set_database_service(db_service) -> None:
    """Set the database service instance."""
    global _db_service
    _db_service = db_service


def preview_data(
    session_id: str,
    table_name: str,
    columns: Optional[List[str]] = None,
    limit: int = 5,
    **_ignored: Any,
) -> Dict[str, Any]:
    """
    Preview data from a database table (read-only, limited).

    Executes a safe SELECT query with LIMIT to quickly see sample data.
    Useful for understanding data format and values before generating complex queries.

    Args:
        session_id: Database session ID from connection
        table_name: Table to preview
        columns: Specific columns to select (default: all with SELECT *)
        limit: Maximum rows to return (default: 5, max: 100)

    Returns:
        {
            "success": bool,
            "records": [...],
            "row_count": int,
            "columns": ["col1", "col2", ...],
            "sql": str,  # The executed SQL
            "error": str | None
        }

    Note: The previous `where` parameter has been removed. Free-form WHERE
    clauses were string-interpolated into the SELECT without parameter binding
    and a keyword-only banlist was easy to bypass (`WHERE 1=(SELECT pg_sleep(60))`,
    encoded identifiers, etc.). Callers that need filtering must go through
    `generate_sql` / `execute_sql` which builds bound queries.
    """
    try:
        if not _db_service:
            return {
                "success": False,
                "records": [],
                "row_count": 0,
                "columns": [],
                "sql": "",
                "error": "Database service not initialized"
            }
        
        # Validate inputs
        if not session_id:
            return {
                "success": False,
                "records": [],
                "row_count": 0,
                "columns": [],
                "sql": "",
                "error": "session_id is required"
            }
        
        if not table_name:
            return {
                "success": False,
                "records": [],
                "row_count": 0,
                "columns": [],
                "sql": "",
                "error": "table_name is required"
            }
        
        # Sanitize limit
        limit = min(max(1, limit), 100)
        
        # Validate identifiers to prevent SQL injection
        if not _IDENTIFIER_RE.match(table_name):
            return {
                "success": False, "records": [], "row_count": 0,
                "columns": [], "sql": "",
                "error": f"Invalid table name: {table_name}"
            }
        if columns:
            for col in columns:
                if not _IDENTIFIER_RE.match(col):
                    return {
                        "success": False, "records": [], "row_count": 0,
                        "columns": [], "sql": "",
                        "error": f"Invalid column name: {col}"
                    }
        
        # Build safe SELECT query (no caller-controlled predicates)
        col_clause = ", ".join(columns) if columns else "*"
        sql = f"SELECT {col_clause} FROM {table_name} LIMIT {limit}"
        
        # Execute via database service
        session = _db_service.get_session(session_id)
        if not session:
            return {
                "success": False,
                "records": [],
                "row_count": 0,
                "columns": [],
                "sql": sql,
                "error": "Invalid or expired session"
            }
        
        # Execute query
        from sqlalchemy import text
        with session.engine.connect() as conn:
            result = conn.execute(text(sql))
            rows = result.fetchall()
            col_names = list(result.keys())
        
        # Convert to list of dicts
        records = [dict(zip(col_names, row)) for row in rows]
        
        # Convert non-serializable types
        for record in records:
            for key, value in record.items():
                if hasattr(value, 'isoformat'):  # datetime
                    record[key] = value.isoformat()
                elif isinstance(value, bytes):
                    record[key] = value.hex()
                elif value is not None and not isinstance(value, (str, int, float, bool, list, dict)):
                    record[key] = str(value)
        
        logger.debug(f"preview_data {table_name}: {len(records)} rows")
        
        return {
            "success": True,
            "records": records,
            "row_count": len(records),
            "columns": col_names,
            "sql": sql,
            "error": None
        }
        
    except Exception as e:
        logger.error(f"preview_data failed: {e}")
        return {
            "success": False,
            "records": [],
            "row_count": 0,
            "columns": [],
            "sql": sql if 'sql' in locals() else "",
            "error": str(e)
        }


# Tool metadata for MCP registration
TOOL_METADATA = {
    "name": "preview_data",
    "description": (
        "Preview sample data from a database table. "
        "Executes a safe SELECT with LIMIT to show sample rows. "
        "Use to understand data format, column values, and verify table contents. "
        "Maximum 100 rows for safety."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Database session ID from connection"
            },
            "table_name": {
                "type": "string",
                "description": "Name of the table to preview"
            },
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific columns to select (omit for all)"
            },
            "limit": {
                "type": "integer",
                "description": "Maximum rows to return (default: 5, max: 100)",
                "default": 5
            }
        },
        "required": ["session_id", "table_name"]
    }
}
