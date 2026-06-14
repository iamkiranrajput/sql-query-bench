"""
Check Relationships Tool

Find foreign key relationships between database tables.
Essential for building JOIN queries correctly. Uses schema_index (FAISS)
when a curated schema-hints file is loaded; otherwise returns an empty
list so callers fall back to live introspection.
"""

import logging
from typing import Any, Dict, List

from ..schema_index import get_schema_index

logger = logging.getLogger(__name__)


def _get_relationships_from_hints(tables: List[str]) -> List[Dict[str, Any]]:
    """Placeholder fallback -- the per-database hints service was removed.

    The check_relationships tool now relies solely on the FAISS-backed
    schema_index. When no schema-hints file is loaded, callers should
    invoke ``introspect_schema`` to discover relationships live from the
    connected database.
    """
    return []


def check_relationships(
    tables: List[str],
    include_indirect: bool = True,
) -> Dict[str, Any]:
    """
    Find relationships between specified tables.
    
    Returns foreign key relationships and inferred connections
    (based on naming conventions and UUID patterns) from schema_hints.
    
    Args:
        tables: List of table names to check relationships for
        include_indirect: Include relationships where only one table matches
                         (useful for discovering bridge tables)
    
    Returns:
        {
            "success": bool,
            "relationships": [
                {
                    "from_table": "orders",
                    "from_column": "customer_id",
                    "to_table": "customers",
                    "to_column": "id",
                    "type": "foreign_key",  # or "inferred", "naming_convention"
                    "join_hint": "orders.customer_id = customers.id"
                },
                ...
            ],
            "direct_relationships": [...],  # Both tables in the input list
            "indirect_relationships": [...],  # One table in input, one discovered
            "suggested_join_path": str,  # SQL-ready JOIN clause suggestion
            "error": str | None
        }
    """
    try:
        index = get_schema_index()
        
        # Get relationships from FAISS schema index
        all_rels = []
        if index.is_initialized:
            all_rels = index.get_relationships_for_tables(tables)
        
        # If schema index has no relationships for these tables,
        # fall back to per-database schema_hints_service
        if not all_rels:
            all_rels = _get_relationships_from_hints(tables)
            if all_rels:
                logger.info(f"Using schema_hints relationships for {tables} ({len(all_rels)} found)")
        
        if not all_rels:
            return {
                "success": True,
                "relationships": [],
                "direct_relationships": [],
                "indirect_relationships": [],
                "suggested_join_path": "",
                "error": None
            }
        
        table_set = set(tables)
        direct = []
        indirect = []
        
        for rel in all_rels:
            from_tbl = rel.get("from_table", "")
            to_tbl = rel.get("to_table", "")
            from_col = rel.get("from_column", "")
            to_col = rel.get("to_column", "")
            rel_type = rel.get("type", "foreign_key")
            
            # Skip invalid relationships
            if not from_tbl or not to_tbl or not from_col or not to_col:
                continue
            
            rel_dict = {
                "from_table": from_tbl,
                "from_column": from_col,
                "to_table": to_tbl,
                "to_column": to_col,
                "type": rel_type,
                "join_hint": f"{from_tbl}.{from_col} = {to_tbl}.{to_col}"
            }
            
            # Check if direct (both tables in input) or indirect
            if from_tbl in table_set and to_tbl in table_set:
                direct.append(rel_dict)
            elif include_indirect:
                indirect.append(rel_dict)
        
        # Build suggested join path for direct relationships
        join_path = ""
        if direct and len(tables) >= 2:
            join_clauses = []
            for rel in direct:
                join_clauses.append(rel["join_hint"])
            join_path = " AND ".join(join_clauses)
        
        all_relationships = direct + indirect
        
        logger.debug(
            f"check_relationships for {tables}: "
            f"{len(direct)} direct, {len(indirect)} indirect"
        )
        
        return {
            "success": True,
            "relationships": all_relationships,
            "direct_relationships": direct,
            "indirect_relationships": indirect,
            "suggested_join_path": join_path,
            "error": None
        }
        
    except Exception as e:
        logger.error(f"check_relationships failed: {e}")
        return {
            "success": False,
            "relationships": [],
            "direct_relationships": [],
            "indirect_relationships": [],
            "suggested_join_path": "",
            "error": str(e)
        }


# Tool metadata for MCP registration
TOOL_METADATA = {
    "name": "check_relationships",
    "description": (
        "Find foreign key and inferred relationships between database tables. "
        "Use this before generating JOIN queries to understand how tables connect. "
        "Returns relationship columns and suggested JOIN conditions. "
        "Essential for multi-table queries."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of table names to check relationships between"
            },
            "include_indirect": {
                "type": "boolean",
                "description": "Include relationships where only one table is in the list",
                "default": True
            }
        },
        "required": ["tables"]
    }
}
