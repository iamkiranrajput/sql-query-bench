"""
Search Tables Tool

FAISS semantic search over database tables using schema_hints metadata.
Returns top-k matching tables with descriptions, domains, and relevance scores.
"""

import logging
import re
from typing import Any, Dict, List

from ..schema_index import get_schema_index, SchemaChunk

logger = logging.getLogger(__name__)

# Injected by the MCP server so search_tables can fall back to live database
# introspection when no curated FAISS schema_hints index is loaded.
_db_service = None


def set_database_service(db_service) -> None:
    """Set the database service instance (enables live-introspection fallback)."""
    global _db_service
    _db_service = db_service


# Tokens ignored when ranking tables by keyword overlap with the query.
_STOPWORDS = {
    "the", "and", "for", "with", "all", "show", "list", "find", "get", "give",
    "are", "any", "that", "this", "from", "into", "our", "their", "want",
    "need", "data", "table", "tables", "record", "records", "row", "rows",
}


def search_tables(
    query: str,
    top_k: int = 10,
    min_score: float = 0.3,
    include_empty: bool = True,  # Default True - schema hints row counts may be stale
    session_id: str = "",
) -> Dict[str, Any]:
    """
    Search for database tables matching a natural language query.
    
    Uses FAISS vector search over table descriptions, business names,
    and synonyms from schema_hints.json.
    
    Args:
        query: Natural language search query (e.g., "customer orders", "recent transactions")
        top_k: Maximum number of results to return (default: 10)
        min_score: Minimum similarity score threshold 0-1 (default: 0.3)
        include_empty: Whether to include tables with 0 rows (default: True)
    
    Returns:
        {
            "success": bool,
            "tables": [
                {
                    "name": "orders",
                    "business_name": "Orders",
                    "description": "...",
                    "domain": "sales",
                    "row_count": 1500,
                    "columns_count": 12,
                    "relevance_score": 8,
                    "match_score": 0.87,
                    "is_fact_table": true,
                    "time_columns": ["created_at", "updated_at"],
                    "primary_key": "id"
                },
                ...
            ],
            "total_found": int,
            "query": str,
            "error": str | None
        }
    """
    try:
        index = get_schema_index()
        
        if not index.is_initialized:
            # No curated FAISS schema_hints index is loaded (the common case
            # for ad-hoc databases). Fall back to LIVE introspection of the
            # connected database's information_schema so discovery still works.
            if _db_service and session_id:
                return _live_introspection_fallback(
                    query, top_k, include_empty, session_id
                )
            return {
                "success": False,
                "tables": [],
                "total_found": 0,
                "query": query,
                "error": (
                    "Schema index not initialized and no active database session. "
                    "Connect to a database first (list_available_databases then "
                    "switch_database)."
                ),
            }
        
        # Search tables
        chunks: List[SchemaChunk] = index.search_tables(
            query=query,
            top_k=top_k * 2 if not include_empty else top_k,  # Get more if filtering
            min_score=min_score
        )
        
        results = []
        for chunk in chunks:
            table_info = index.get_table(chunk.name)
            if not table_info:
                continue
            
            # Filter empty tables if requested
            if not include_empty and table_info.is_empty:
                continue
            
            results.append({
                "name": table_info.name,
                "business_name": table_info.business_name,
                "description": table_info.description[:500] if table_info.description else "",
                "domain": table_info.domain,
                "row_count": table_info.row_count,
                "columns_count": len(table_info.columns),
                "relevance_score": table_info.relevance_score,
                "match_score": round(chunk.score, 3),
                "is_fact_table": table_info.is_fact_table,
                "time_columns": table_info.time_columns,
                "primary_key": table_info.primary_key,
                "synonyms": table_info.synonyms[:5],  # Limit synonyms
            })
            
            if len(results) >= top_k:
                break
        
        logger.debug(f"search_tables '{query}' found {len(results)} tables")
        
        return {
            "success": True,
            "tables": results,
            "total_found": len(results),
            "query": query,
            "error": None
        }
        
    except Exception as e:
        logger.error(f"search_tables failed: {e}")
        return {
            "success": False,
            "tables": [],
            "total_found": 0,
            "query": query,
            "error": str(e)
        }


def _tokenize(text: str) -> List[str]:
    """Lowercase alphanumeric tokens, stopwords removed, length >= 2."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if len(t) >= 2 and t not in _STOPWORDS]


def _live_introspection_fallback(
    query: str, top_k: int, include_empty: bool, session_id: str
) -> Dict[str, Any]:
    """Rank tables by keyword overlap with ``query`` using information_schema.

    Used when no curated FAISS schema_hints index is available. The SQL is
    read-only and contains NO user-supplied values (only the constant 'public'
    schema), so there is no injection surface. Returns the same result shape as
    the FAISS path plus ``source='live_introspection'`` so callers can tell the
    two modes apart.
    """
    session = _db_service.get_session(session_id)
    if not session:
        return {
            "success": False,
            "tables": [],
            "total_found": 0,
            "query": query,
            "error": f"Invalid or expired database session: {session_id}",
        }

    # One round-trip: every base table in the public schema with its column
    # list. No user input is interpolated into the SQL.
    sql = (
        "SELECT t.table_name, "
        "       COUNT(c.column_name) AS column_count, "
        "       COALESCE(string_agg(c.column_name, ',' ORDER BY c.ordinal_position), '') AS columns "
        "FROM information_schema.tables t "
        "LEFT JOIN information_schema.columns c "
        "  ON c.table_schema = t.table_schema AND c.table_name = t.table_name "
        "WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
        "GROUP BY t.table_name "
        "ORDER BY t.table_name "
        "LIMIT 500"
    )

    try:
        records, _cols, _n = _db_service.execute_query(session_id, sql, params={})
    except Exception as e:
        logger.error("search_tables live fallback failed: %s", e)
        return {
            "success": False,
            "tables": [],
            "total_found": 0,
            "query": query,
            "error": f"Live schema introspection failed: {e}",
        }

    query_tokens = _tokenize(query)

    scored = []
    for row in records:
        name = row.get("table_name", "") or ""
        columns_csv = row.get("columns", "") or ""
        column_names = [c for c in columns_csv.split(",") if c]
        name_l = name.lower()
        cols_l = columns_csv.lower()

        score = 0
        for tok in query_tokens:
            # Crude singular/plural fold so 'customers' matches 'customer'.
            singular = tok[:-1] if tok.endswith("s") and len(tok) > 3 else tok
            if tok in name_l or singular in name_l:
                score += 2  # a table-name hit is the strongest signal
            elif tok in cols_l or singular in cols_l:
                score += 1  # a column hit is a weaker signal
        scored.append((score, name, column_names))

    # Best matches first; stable alphabetical tiebreak.
    scored.sort(key=lambda x: (-x[0], x[1]))

    max_possible = max(1, 2 * len(query_tokens))
    any_match = bool(scored) and scored[0][0] > 0

    results = []
    for score, name, column_names in scored:
        # When there are real matches, surface only those; otherwise fall
        # through and list the schema so the agent can still pick a table.
        if any_match and score == 0:
            break
        results.append({
            "name": name,
            "business_name": name.replace("_", " ").title(),
            "description": "",
            "domain": "",
            "row_count": None,  # unknown without an extra COUNT(*) per table
            "columns_count": len(column_names),
            "relevance_score": score,
            "match_score": round(min(1.0, score / max_possible), 3),
            "is_fact_table": False,
            "time_columns": [
                c for c in column_names
                if any(k in c.lower() for k in ("date", "time", "_at", "timestamp"))
            ],
            "primary_key": None,
            "synonyms": [],
        })
        if len(results) >= top_k:
            break

    note = (
        "Matched live database tables by keyword (no curated schema index "
        "loaded). Row counts are not computed in this mode."
        if any_match else
        "No keyword match; returning available tables from live introspection "
        "so you can pick one. Use introspect_schema for full column details."
    )

    return {
        "success": True,
        "tables": results,
        "total_found": len(results),
        "query": query,
        "source": "live_introspection",
        "note": note,
        "error": None,
    }


# Tool metadata for MCP registration
TOOL_METADATA = {
    "name": "search_tables",
    "description": (
        "Search for database tables matching a natural language query. "
        "Use this to find relevant tables before generating SQL. "
        "Returns table names, descriptions, row counts, and relevance scores. "
        "Examples: 'customer orders', 'recent transactions', 'user activity logs'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language search query describing the data you're looking for"
            },
            "top_k": {
                "type": "integer",
                "description": "Maximum number of results to return",
                "default": 10
            },
            "min_score": {
                "type": "number",
                "description": "Minimum similarity score (0-1)",
                "default": 0.3
            },
            "include_empty": {
                "type": "boolean",
                "description": "Include tables with 0 rows",
                "default": True
            },
            "session_id": {
                "type": "string",
                "description": "Database session ID (auto-injected; enables live-introspection fallback when no schema index is loaded)"
            }
        },
        "required": ["query"]
    }
}
