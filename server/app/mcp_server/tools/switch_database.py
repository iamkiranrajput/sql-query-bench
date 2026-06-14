"""
Switch Database Tool

Allows the Copilot agent to connect to any of the pre-configured database
connections so it can query across multiple databases.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Will be injected by the MCP server
_db_service = None

# Registered connections from the frontend (set by copilot_service)
_available_connections: List[Dict[str, str]] = []


def set_database_service(db_service) -> None:
    """Set the database service instance."""
    global _db_service
    _db_service = db_service


def set_available_connections(connections: List[Dict[str, str]]) -> None:
    """Update the list of available database connections."""
    global _available_connections
    _available_connections = connections
    logger.info(f"Switch database tool: {len(connections)} connections available")


def get_available_connections() -> List[Dict[str, str]]:
    """Return current available connections."""
    return _available_connections


TOOL_METADATA = {
    "name": "switch_database",
    "description": (
        "Switch the active database connection. Use this when the current database "
        "doesn't have the tables you need. Call list_available_databases first to see "
        "which databases are available, then call this with the connection name. "
        "Returns a new session_id to use with subsequent queries. "
        "If you already have an active session_id, pass it as previous_session_id so "
        "the old connection can be released."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "connection_name": {
                "type": "string",
                "description": "The name of the saved connection to switch to (e.g. 'analytics-db', 'reporting-db')",
            },
            "previous_session_id": {
                "type": "string",
                "description": "Optional. The current session_id being replaced; will be disconnected.",
            },
        },
        "required": ["connection_name"],
    },
}

LIST_DATABASES_METADATA = {
    "name": "list_available_databases",
    "description": (
        "List all available database connections that you can switch to. "
        "Shows connection name, hostname, port, database name, and whether it's currently active. "
        "Use this to find the right database before switching."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


def list_available_databases() -> Dict[str, Any]:
    """List all available database connections."""
    if not _available_connections:
        return {
            "success": True,
            "databases": [],
            "message": "No saved database connections available. Ask the user to add connections in Settings.",
        }

    db_list = []
    for conn in _available_connections:
        db_list.append({
            "name": conn.get("name", ""),
            "hostname": conn.get("hostname", ""),
            "port": conn.get("port", ""),
            "database": conn.get("database", ""),
            "db_type": conn.get("dbType", conn.get("db_type", "postgresql")),
        })

    return {
        "success": True,
        "databases": db_list,
        "count": len(db_list),
    }


def switch_database(
    connection_name: str,
    previous_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Switch to a different database connection by name.
    Creates a new database session and returns the session_id.

    If ``previous_session_id`` is supplied, that session is disconnected after
    a new connection is successfully established (B5: prevent engine/pool
    leaks when the agent hops between databases mid-conversation).
    """
    if not _db_service:
        return {"success": False, "error": "Database service not initialized"}

    if not _available_connections:
        return {"success": False, "error": "No saved connections available"}

    # Find the matching connection (case-insensitive)
    conn = None
    for c in _available_connections:
        if c.get("name", "").lower() == connection_name.lower():
            conn = c
            break

    # Try partial match if exact match fails
    if not conn:
        for c in _available_connections:
            if connection_name.lower() in c.get("name", "").lower():
                conn = c
                break

    # Try matching by database name
    if not conn:
        for c in _available_connections:
            if c.get("database", "").lower() == connection_name.lower():
                conn = c
                break

    if not conn:
        names = [c.get("name", "") for c in _available_connections]
        return {
            "success": False,
            "error": f"Connection '{connection_name}' not found. Available: {', '.join(names)}",
        }

    try:
        password = conn.get("password", "")
        # Decode base64 if it looks encoded (frontend stores btoa)
        if password and not any(c in password for c in " !@#$%^&*"):
            try:
                import base64
                password = base64.b64decode(password).decode("utf-8")
            except Exception:
                pass  # Use as-is

        session_id, message = _db_service.create_connection(
            hostname=conn.get("hostname", ""),
            port=int(conn.get("port", 5432)),
            database=conn.get("database", ""),
            username=conn.get("username", ""),
            password=password,
            db_type=conn.get("dbType", conn.get("db_type", "postgresql")),
        )

        # B5: Dispose the previous session (if any) only after the new one
        # is established so we never end up with zero usable connections.
        if previous_session_id and previous_session_id != session_id:
            try:
                _db_service.disconnect(previous_session_id)
                logger.info(
                    f"switch_database: released previous session {previous_session_id[:8]}…"
                )
            except Exception as disconnect_err:
                # Non-fatal: TTL cleanup will reclaim it eventually.
                logger.warning(
                    f"switch_database: failed to disconnect previous session: {disconnect_err}"
                )

        return {
            "success": True,
            "session_id": session_id,
            "database": conn.get("database", ""),
            "hostname": conn.get("hostname", ""),
            "port": conn.get("port", "5432"),
            "connection_name": conn.get("name", ""),
            "message": f"Connected to {conn.get('database', '')} on {conn.get('hostname', '')}:{conn.get('port', '')}",
        }

    except Exception as e:
        logger.error(f"switch_database failed: {e}")
        return {
            "success": False,
            "error": f"Connection failed: {str(e)[:300]}",
        }
