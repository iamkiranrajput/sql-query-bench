"""
Execute SQL Tool

Execute validated SQL queries against the connected database.
Returns results, row count, and execution metrics.
"""

import csv
import io
import logging
import time
from typing import Any, Dict, List, Optional

from .sql_normalizer import normalize_readonly_sql

logger = logging.getLogger(__name__)

# Will be injected by the MCP server
_db_service = None


def set_database_service(db_service) -> None:
    """Set the database service instance."""
    global _db_service
    _db_service = db_service


def execute_sql(
    session_id: str,
    sql: str,
    max_rows: int = 10000,
    timeout_seconds: int = 120,
    include_csv: bool = True,
) -> Dict[str, Any]:
    """
    Execute a SQL query against the connected database.
    
    The SQL should be validated before calling this tool.
    Only SELECT queries are allowed for safety.
    
    Args:
        session_id: Database session ID from connection
        sql: SQL query to execute (must be SELECT)
        max_rows: Maximum rows to return (default: 10000)
        timeout_seconds: Query timeout (default: 120)
        include_csv: Include CSV export of results (default: True)
    
    Returns:
        {
            "success": bool,
            "records": [{...}, ...],
            "row_count": int,
            "columns": ["col1", "col2", ...],
            "column_types": {"col1": "INTEGER", ...},
            "execution_time_ms": float,
            "csv_data": str | None,
            "truncated": bool,  # True if max_rows limit was hit
            "sql": str,
            "error": str | None
        }
    """
    start_time = time.perf_counter()
    
    try:
        if not _db_service:
            return {
                "success": False,
                "records": [],
                "row_count": 0,
                "columns": [],
                "column_types": {},
                "execution_time_ms": 0,
                "csv_data": None,
                "truncated": False,
                "sql": sql,
                "error": "Database service not initialized"
            }
        
        if not session_id:
            return {
                "success": False,
                "records": [],
                "row_count": 0,
                "columns": [],
                "column_types": {},
                "execution_time_ms": 0,
                "csv_data": None,
                "truncated": False,
                "sql": sql,
                "error": "session_id is required"
            }
        
        if not sql or not sql.strip():
            return {
                "success": False,
                "records": [],
                "row_count": 0,
                "columns": [],
                "column_types": {},
                "execution_time_ms": 0,
                "csv_data": None,
                "truncated": False,
                "sql": sql,
                "error": "SQL query is required"
            }
        
        # Cap max_rows to prevent memory exhaustion
        max_rows = min(max(1, max_rows), 50000)
        timeout_seconds = min(max(1, timeout_seconds), 300)
        
        # Basic security check (validate_sql should be called first). Normalize
        # harmless leading labels like "Method 1:" before enforcing SELECT-only.
        sql_clean = normalize_readonly_sql(sql)
        sql_upper = sql_clean.upper()
        if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
            return {
                "success": False,
                "records": [],
                "row_count": 0,
                "columns": [],
                "column_types": {},
                "execution_time_ms": 0,
                "csv_data": None,
                "truncated": False,
                "sql": sql,
                "error": "Only SELECT queries are allowed"
            }
        
        # Reject multiple statements
        import sqlparse as _sp
        if len(_sp.split(sql_clean)) > 1:
            return {
                "success": False,
                "records": [],
                "row_count": 0,
                "columns": [],
                "column_types": {},
                "execution_time_ms": 0,
                "csv_data": None,
                "truncated": False,
                "sql": sql,
                "error": "Multiple SQL statements are not allowed"
            }
        
        # Get database session
        session = _db_service.get_session(session_id)
        if not session:
            return {
                "success": False,
                "records": [],
                "row_count": 0,
                "columns": [],
                "column_types": {},
                "execution_time_ms": 0,
                "csv_data": None,
                "truncated": False,
                "sql": sql,
                "error": "Invalid or expired database session"
            }
        
        # Execute query
        from sqlalchemy import text
        from sqlalchemy.engine import Result
        
        query_start = time.perf_counter()
        
        with session.engine.connect() as conn:
            # Set statement timeout if supported
            if session.db_type in ("postgresql", "mysql"):
                try:
                    if session.db_type == "postgresql":
                        conn.execute(text(f"SET statement_timeout = {timeout_seconds * 1000}"))
                    elif session.db_type == "mysql":
                        conn.execute(text(f"SET max_execution_time = {timeout_seconds * 1000}"))
                except Exception:
                    pass  # Ignore if not supported
            
            result: Result = conn.execute(text(sql_clean))
            rows = result.fetchmany(max_rows + 1)  # Fetch one extra to detect truncation
            col_names = list(result.keys())
            
            # Try to get column types
            col_types = {}
            try:
                for i, col in enumerate(result.cursor.description or []):
                    col_types[col_names[i]] = str(col[1].__name__) if hasattr(col[1], '__name__') else str(col[1])
            except Exception:
                pass
        
        query_time = (time.perf_counter() - query_start) * 1000
        
        # Check if truncated
        truncated = len(rows) > max_rows
        if truncated:
            rows = rows[:max_rows]
        
        # Convert to list of dicts
        records = []
        for row in rows:
            record = dict(zip(col_names, row))
            # Convert non-serializable types
            for key, value in record.items():
                if hasattr(value, 'isoformat'):  # datetime
                    record[key] = value.isoformat()
                elif isinstance(value, bytes):
                    record[key] = value.hex()
                elif value is not None and not isinstance(value, (str, int, float, bool, list, dict)):
                    record[key] = str(value)
            records.append(record)
        
        # Generate CSV if requested
        csv_data = None
        if include_csv and records:
            csv_data = _generate_csv(records, col_names)
        
        execution_time_ms = (time.perf_counter() - start_time) * 1000
        
        logger.debug(
            f"execute_sql: {len(records)} rows in {query_time:.2f}ms "
            f"(total {execution_time_ms:.2f}ms)"
        )
        
        return {
            "success": True,
            "records": records,
            "row_count": len(records),
            "columns": col_names,
            "column_types": col_types,
            "execution_time_ms": round(execution_time_ms, 2),
            "csv_data": csv_data,
            "truncated": truncated,
            "sql": sql_clean,
            "error": None
        }
        
    except Exception as e:
        execution_time_ms = (time.perf_counter() - start_time) * 1000
        error_msg = str(e)
        
        # Clean up error message
        if "psycopg2" in error_msg or "sqlalchemy" in error_msg.lower():
            # Extract just the database error
            if "DETAIL:" in error_msg:
                error_msg = error_msg.split("DETAIL:")[0].strip()
            if ") " in error_msg:
                error_msg = error_msg.split(") ", 1)[-1]
        
        logger.error(f"execute_sql failed: {error_msg}")
        
        return {
            "success": False,
            "records": [],
            "row_count": 0,
            "columns": [],
            "column_types": {},
            "execution_time_ms": round(execution_time_ms, 2),
            "csv_data": None,
            "truncated": False,
            "sql": sql,
            "error": error_msg
        }


def _generate_csv(records: List[Dict[str, Any]], columns: List[str]) -> str:
    """Generate CSV string from records."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(records)
    return output.getvalue()


# Tool metadata for MCP registration
TOOL_METADATA = {
    "name": "execute_sql",
    "description": (
        "Execute a SQL query against the connected database. "
        "Returns query results as records with row count and execution time. "
        "Only SELECT queries are allowed. "
        "Always use validate_sql first to check the query."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Database session ID from connection"
            },
            "sql": {
                "type": "string",
                "description": "SQL query to execute (SELECT only)"
            },
            "max_rows": {
                "type": "integer",
                "description": "Maximum rows to return",
                "default": 10000
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Query timeout in seconds",
                "default": 120
            },
            "include_csv": {
                "type": "boolean",
                "description": "Include CSV export",
                "default": True
            }
        },
        "required": ["session_id", "sql"]
    }
}
