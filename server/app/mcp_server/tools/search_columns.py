"""
Search Columns Tool

Search and retrieve column metadata for specified tables.
Returns column types, semantic types, filterability, and sample values.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from ..schema_index import get_schema_index, SchemaChunk, ColumnInfo

logger = logging.getLogger(__name__)

# Injected by the MCP server so search_columns can fall back to live database
# introspection when no curated FAISS schema_hints index is loaded.
_db_service = None


def set_database_service(db_service) -> None:
    """Set the database service instance (enables live-introspection fallback)."""
    global _db_service
    _db_service = db_service


def search_columns(
    table_name: Optional[str] = None,
    query: Optional[str] = None,
    tables: Optional[List[str]] = None,
    top_k: int = 30,
    min_score: float = 0.25,
    session_id: str = "",
) -> Dict[str, Any]:
    """
    Search for columns in the database schema.
    
    Can either:
    1. Get all columns for a specific table (table_name)
    2. Search columns by query across all tables or filtered tables
    3. Get all columns for multiple tables (tables list)
    
    Args:
        table_name: Specific table to get columns from
        query: Natural language search query for columns (e.g., "timestamp", "user id")
        tables: List of table names to search within
        top_k: Maximum number of results when searching
        min_score: Minimum similarity score for search
    
    Returns:
        {
            "success": bool,
            "columns": [
                {
                    "name": "created_at",
                    "table": "orders",
                    "business_name": "Creation Time",
                    "description": "...",
                    "data_type": "timestamp",
                    "semantic_type": "timestamp",
                    "is_nullable": true,
                    "is_filterable": true,
                    "is_aggregatable": false,
                    "is_join_key": false,
                    "sample_values": ["2024-01-01", "2024-01-02"],
                    "match_score": 0.85 (only when searching)
                },
                ...
            ],
            "total_found": int,
            "error": str | None
        }
    """
    try:
        index = get_schema_index()
        
        if not index.is_initialized:
            # No curated FAISS schema_hints index. Fall back to live
            # introspection of the connected database's information_schema.
            if _db_service and session_id:
                return _live_columns_fallback(
                    table_name, query, tables, top_k, session_id
                )
            return {
                "success": False,
                "columns": [],
                "total_found": 0,
                "error": (
                    "Schema index not initialized and no active database "
                    "session. Connect to a database first."
                ),
            }
        
        results = []
        
        # Mode 1: Get all columns for a specific table
        if table_name:
            columns = index.get_table_columns(table_name)
            for col in columns:
                results.append(_column_to_dict(col))
        
        # Mode 2: Get all columns for multiple tables
        elif tables and not query:
            for tbl in tables:
                columns = index.get_table_columns(tbl)
                for col in columns:
                    results.append(_column_to_dict(col))
        
        # Mode 3: Search columns by query
        elif query:
            table_filter = tables if tables else None
            chunks: List[SchemaChunk] = index.search_columns(
                query=query,
                table_filter=table_filter,
                top_k=top_k,
                min_score=min_score
            )
            
            for chunk in chunks:
                col_info = index.get_column(chunk.table_name, chunk.name)
                if col_info:
                    col_dict = _column_to_dict(col_info)
                    col_dict["match_score"] = round(chunk.score, 3)
                    results.append(col_dict)
        
        else:
            return {
                "success": False,
                "columns": [],
                "total_found": 0,
                "error": "Must provide table_name, tables list, or query"
            }
        
        logger.debug(f"search_columns found {len(results)} columns")
        
        return {
            "success": True,
            "columns": results,
            "total_found": len(results),
            "error": None
        }
        
    except Exception as e:
        logger.error(f"search_columns failed: {e}")
        return {
            "success": False,
            "columns": [],
            "total_found": 0,
            "error": str(e)
        }


def _column_to_dict(col: ColumnInfo) -> Dict[str, Any]:
    """Convert ColumnInfo to dictionary."""
    return {
        "name": col.name,
        "table": col.table_name,
        "business_name": col.business_name,
        "description": col.description[:300] if col.description else "",
        "data_type": col.data_type,
        "semantic_type": col.semantic_type,
        "is_nullable": col.is_nullable,
        "is_filterable": col.is_filterable,
        "is_aggregatable": col.is_aggregatable,
        "is_join_key": col.is_join_key,
        "synonyms": col.synonyms[:5],
        "sample_values": col.sample_values[:5],
        "allowed_values": col.allowed_values[:10] if col.allowed_values else [],
    }


def _live_columns_fallback(
    table_name: Optional[str],
    query: Optional[str],
    tables: Optional[List[str]],
    top_k: int,
    session_id: str,
) -> Dict[str, Any]:
    """Return column metadata from information_schema when no FAISS index exists.

    All user-supplied table/column values are passed as BOUND parameters (never
    string-interpolated), so there is no SQL-injection surface. Mirrors the
    three modes of ``search_columns`` (single table, table list, or keyword
    search) and tags the result with ``source='live_introspection'``.
    """
    session = _db_service.get_session(session_id)
    if not session:
        return {
            "success": False,
            "columns": [],
            "total_found": 0,
            "error": f"Invalid or expired database session: {session_id}",
        }

    conditions = ["table_schema = 'public'"]
    params: Dict[str, Any] = {}

    # Restrict to the requested table(s) when provided.
    target_tables = [t for t in ([table_name] if table_name else (tables or [])) if t]
    if target_tables:
        placeholders = []
        for i, t in enumerate(target_tables):
            key = f"t{i}"
            params[key] = t
            placeholders.append(f":{key}")
        conditions.append(f"table_name IN ({', '.join(placeholders)})")

    where = " AND ".join(conditions)
    sql = (
        "SELECT table_name, column_name, data_type, is_nullable, ordinal_position "
        "FROM information_schema.columns "
        f"WHERE {where} "
        "ORDER BY table_name, ordinal_position "
        "LIMIT 2000"
    )

    try:
        records, _cols, _n = _db_service.execute_query(session_id, sql, params=params)
    except Exception as e:
        logger.error("search_columns live fallback failed: %s", e)
        return {
            "success": False,
            "columns": [],
            "total_found": 0,
            "error": f"Live column introspection failed: {e}",
        }

    # Optional keyword filter when the caller passed a free-text query.
    query_tokens = re.findall(r"[a-z0-9]+", query.lower()) if query else []

    rows = []
    for r in records:
        col_name = r.get("column_name", "") or ""
        if query_tokens:
            hay = col_name.lower()
            if not any(
                tok in hay or (tok.endswith("s") and tok[:-1] in hay)
                for tok in query_tokens
            ):
                continue
        dtype = r.get("data_type", "") or ""
        rows.append({
            "name": col_name,
            "table": r.get("table_name", ""),
            "business_name": col_name.replace("_", " ").title(),
            "description": "",
            "data_type": dtype,
            "semantic_type": dtype,
            "is_nullable": (r.get("is_nullable") == "YES"),
            "is_filterable": True,
            "is_aggregatable": dtype in (
                "integer", "bigint", "numeric", "double precision", "real", "smallint"
            ),
            "is_join_key": col_name.endswith("_id") or col_name == "id",
            "synonyms": [],
            "sample_values": [],
            "allowed_values": [],
        })
        if len(rows) >= max(top_k, 1) and query_tokens:
            break

    return {
        "success": True,
        "columns": rows,
        "total_found": len(rows),
        "source": "live_introspection",
        "error": None,
    }


# Tool metadata for MCP registration
TOOL_METADATA = {
    "name": "search_columns",
    "description": (
        "Search for columns in database tables. "
        "Use to find column names, types, and metadata. "
        "Can get all columns for a table, or search across tables. "
        "Returns data types, semantic types (primary_key, foreign_key, timestamp, etc.), "
        "and whether columns are filterable or aggregatable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "table_name": {
                "type": "string",
                "description": "Get all columns from this specific table"
            },
            "query": {
                "type": "string",
                "description": "Natural language search for columns (e.g., 'timestamp', 'customer id')"
            },
            "tables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of tables to search within"
            },
            "top_k": {
                "type": "integer",
                "description": "Maximum results when searching",
                "default": 30
            },
            "session_id": {
                "type": "string",
                "description": "Database session ID (auto-injected; enables live-introspection fallback when no schema index is loaded)"
            }
        },
        "required": []
    }
}
