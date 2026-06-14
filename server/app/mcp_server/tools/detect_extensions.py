"""
Detect Database Extensions Tool

Queries the connected server for installed extensions so the agent KNOWS which
advanced capabilities it can safely use — instead of guessing and emitting SQL
the server cannot run.

For PostgreSQL this reads ``pg_extension`` and maps well-known extensions
(PostGIS, pgvector, pg_trgm, ...) to plain-language capabilities. This is what
lets the agent generate *correct* spatial (``ST_DWithin``) or vector (``<=>``)
SQL only when the server actually supports it, and fall back to standard SQL
otherwise.

Read-only and safe: it only inspects metadata.
"""

import logging
from typing import Any, Dict, List

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Injected by the MCP server during initialisation.
_db_service = None

# Cache per session_id — installed extensions don't change mid-session.
_ext_cache: Dict[str, Dict[str, Any]] = {}


def set_database_service(db_service) -> None:
    """Set the database service instance."""
    global _db_service
    _db_service = db_service


# Well-known PostgreSQL extensions → (capability label, agent guidance).
_EXTENSION_CAPABILITIES: Dict[str, str] = {
    "postgis": (
        "Geospatial queries: geometry/geography types and ST_* functions "
        "(ST_DWithin, ST_Distance, ST_Within, ST_AsGeoJSON)."
    ),
    "postgis_topology": "PostGIS topology support.",
    "postgis_raster": "PostGIS raster support.",
    "vector": (
        "pgvector semantic similarity search over embedding columns using the "
        "<=> (cosine), <-> (L2) and <#> (inner product) operators."
    ),
    "pg_trgm": (
        "Trigram fuzzy text matching: similarity()/word_similarity() and "
        "accelerated ILIKE for 'did you mean' style lookups."
    ),
    "hstore": "Key/value pair storage and querying.",
    "uuid-ossp": "UUID generation functions.",
    "citext": "Case-insensitive text columns.",
    "pgcrypto": "Cryptographic hashing/encryption functions.",
    "timescaledb": "Time-series: hypertables and time_bucket() aggregation.",
    "age": "Graph queries via Apache AGE (openCypher).",
    "unaccent": "Accent-insensitive text search.",
    "pg_stat_statements": "Query execution statistics.",
    "postgres_fdw": "Foreign data wrappers to remote PostgreSQL servers.",
    "ltree": "Hierarchical tree-like label paths.",
}


def detect_extensions(session_id: str) -> Dict[str, Any]:
    """
    List installed database extensions and the capabilities they unlock.

    Use this early (e.g. right after connecting) when a request hints at
    advanced features — maps, distances, "near", "similar to", fuzzy matching —
    so you only generate SQL the server can actually run.

    Args:
        session_id: Database session ID (auto-injected).

    Returns:
        {
            "success": bool,
            "db_type": str,
            "extensions": [{"name": str, "version": str, "capability": str}],
            "has_postgis": bool,
            "has_pgvector": bool,
            "has_pg_trgm": bool,
            "capability_summary": str,
            "error": str | None
        }
    """
    if not _db_service:
        return {"success": False, "error": "Database service not initialized"}
    if not session_id:
        return {"success": False, "error": "session_id is required"}

    if session_id in _ext_cache:
        return _ext_cache[session_id]

    session = _db_service.get_session(session_id)
    if not session:
        return {"success": False, "error": "Invalid or expired database session"}

    db_type = getattr(session, "db_type", "postgresql")

    result: Dict[str, Any] = {
        "success": True,
        "db_type": db_type,
        "extensions": [],
        "has_postgis": False,
        "has_pgvector": False,
        "has_pg_trgm": False,
        "capability_summary": "",
        "error": None,
    }

    # Extensions in this form are a PostgreSQL concept. Other engines report an
    # empty list with a clear note rather than failing.
    if db_type != "postgresql":
        result["capability_summary"] = (
            f"Extension introspection is only supported for PostgreSQL; the "
            f"connected server is '{db_type}'."
        )
        _ext_cache[session_id] = result
        return result

    try:
        with session.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT extname, extversion FROM pg_extension ORDER BY extname")
            ).fetchall()

        extensions: List[Dict[str, str]] = []
        for row in rows:
            name = str(row[0])
            version = str(row[1]) if row[1] is not None else ""
            capability = _EXTENSION_CAPABILITIES.get(
                name, "Installed extension (no special agent guidance)."
            )
            extensions.append(
                {"name": name, "version": version, "capability": capability}
            )

        result["extensions"] = extensions
        names = {e["name"] for e in extensions}
        result["has_postgis"] = "postgis" in names
        result["has_pgvector"] = "vector" in names
        result["has_pg_trgm"] = "pg_trgm" in names

        notable = [
            e["name"]
            for e in extensions
            if e["name"] in _EXTENSION_CAPABILITIES
        ]
        if notable:
            result["capability_summary"] = (
                "Advanced capabilities available: " + ", ".join(notable) + "."
            )
        else:
            result["capability_summary"] = (
                "No advanced extensions detected — use standard ANSI SQL."
            )

        logger.debug(
            "detect_extensions: %d extensions (postgis=%s, pgvector=%s)",
            len(extensions),
            result["has_postgis"],
            result["has_pgvector"],
        )

    except Exception as e:
        logger.error(f"detect_extensions failed: {e}")
        return {"success": False, "db_type": db_type, "error": str(e)}

    _ext_cache[session_id] = result
    return result


# Tool metadata for MCP registration
TOOL_METADATA = {
    "name": "detect_extensions",
    "description": (
        "List the database extensions installed on the connected server and the "
        "capabilities they unlock (e.g. PostGIS spatial, pgvector similarity, "
        "pg_trgm fuzzy matching). Call this before generating advanced SQL — "
        "spatial distance/'near' queries, vector 'similar to' searches, or fuzzy "
        "matching — so you only emit SQL the server can actually run. "
        "PostgreSQL only; other engines return an empty list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Database session ID (auto-injected)",
            }
        },
        "required": ["session_id"],
    },
}
