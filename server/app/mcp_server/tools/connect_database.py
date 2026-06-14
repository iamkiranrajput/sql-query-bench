"""
Connect Database Tool

Connect to a database by providing connection parameters.
Returns a session_id that is used by other tools (execute_sql, preview_data, introspect_schema, etc.).
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Will be injected by the MCP server
_db_service = None


def set_database_service(db_service) -> None:
    """Set the database service instance."""
    global _db_service
    _db_service = db_service


def connect_database(
    hostname: str,
    port: int,
    database: str,
    username: str,
    password: str,
    db_type: str = "postgresql",
) -> Dict[str, Any]:
    """
    Connect to a database and return a session_id for subsequent queries.

    Supports PostgreSQL, MySQL, MSSQL, and Oracle databases.

    Args:
        hostname: Database host address (IP or hostname)
        port: Database port number
        database: Database name to connect to
        username: Database username
        password: Database password
        db_type: Database type - postgresql, mysql, mssql, or oracle (default: postgresql)

    Returns:
        {
            "success": bool,
            "session_id": str | None,
            "message": str,
            "error": str | None
        }
    """
    try:
        if not _db_service:
            return {
                "success": False,
                "session_id": None,
                "message": "",
                "error": "Database service not initialized",
            }

        if not hostname or not database or not username:
            return {
                "success": False,
                "session_id": None,
                "message": "",
                "error": "hostname, database, and username are required",
            }

        session_id, message = _db_service.create_connection(
            hostname=hostname,
            port=port,
            database=database,
            username=username,
            password=password,
            db_type=db_type,
        )

        return {
            "success": True,
            "session_id": session_id,
            "message": message,
            "error": None,
        }

    except Exception as e:
        logger.error(f"connect_database failed: {e}")
        return {
            "success": False,
            "session_id": None,
            "message": "",
            "error": str(e),
        }


TOOL_METADATA = {
    "name": "connect_database",
    "description": (
        "Connect to a database (PostgreSQL, MySQL, MSSQL, or Oracle). "
        "Returns a session_id used by execute_sql, preview_data, introspect_schema, "
        "and other tools that need a database connection. "
        "Use this tool when no session_id is available or when connecting to a new database."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "hostname": {
                "type": "string",
                "description": "Database host address (IP or hostname)",
            },
            "port": {
                "type": "integer",
                "description": "Database port number (e.g., 5432 for PostgreSQL, 3306 for MySQL)",
            },
            "database": {
                "type": "string",
                "description": "Database name to connect to",
            },
            "username": {
                "type": "string",
                "description": "Database username",
            },
            "password": {
                "type": "string",
                "description": "Database password",
            },
            "db_type": {
                "type": "string",
                "description": "Database type: postgresql, mysql, mssql, or oracle (default: postgresql)",
                "enum": ["postgresql", "mysql", "mssql", "oracle"],
                "default": "postgresql",
            },
        },
        "required": ["hostname", "port", "database", "username", "password"],
    },
}
