"""
Generate SQL Tool

Compiles SQL from structured query context (tables, columns, filters, joins).
Deterministic generation based on schema metadata.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from ..schema_index import get_schema_index

logger = logging.getLogger(__name__)


def _build_filter_condition(qualified_col: str, op: str, val) -> str:
    """Build a single SQL filter condition string."""
    op_upper = op.upper()
    if op_upper == "IN" and isinstance(val, list):
        val_str = ", ".join(f"'{v}'" if isinstance(v, str) else str(v) for v in val)
        return f"{qualified_col} IN ({val_str})"
    elif op_upper == "IS NULL":
        return f"{qualified_col} IS NULL"
    elif op_upper == "IS NOT NULL":
        return f"{qualified_col} IS NOT NULL"
    elif op_upper in ("LIKE", "ILIKE"):
        return f"{qualified_col} {op} '{val}'"
    elif op_upper == "BETWEEN":
        if isinstance(val, list) and len(val) == 2:
            return f"{qualified_col} BETWEEN '{val[0]}' AND '{val[1]}'"
        return ""
    elif isinstance(val, str):
        return f"{qualified_col} {op} '{val}'"
    elif val is None:
        return f"{qualified_col} IS NULL"
    else:
        return f"{qualified_col} {op} {val}"


def generate_sql(
    intent: str,
    tables: List[str],
    columns: Optional[List[str]] = None,
    filters: Optional[List[Dict[str, Any]]] = None,
    joins: Optional[List[Dict[str, Any]]] = None,
    aggregations: Optional[List[Dict[str, Any]]] = None,
    group_by: Optional[List[str]] = None,
    order_by: Optional[List[Dict[str, Any]]] = None,
    limit: Optional[int] = None,
    db_type: str = "postgresql",
    **_ignored,  # Tolerate stray kwargs (e.g. an LLM-supplied session_id)
) -> Dict[str, Any]:
    """
    Generate SQL from a structured query plan.
    
    Compiles SQL based on intent and schema context.
    Uses schema metadata to ensure correct column types and names.
    
    Args:
        intent: Query intent (SELECT, AGGREGATION, TIME_SERIES, RANKING, etc.)
        tables: List of table names to query
        columns: Columns to SELECT (None = all columns from first table)
        filters: List of filter conditions:
            [{"column": "status", "operator": "=", "value": "active", "table": "orders"}]
        joins: List of join specifications:
            [{"table": "customers", "on": "orders.customer_id = customers.id", "type": "LEFT"}]
        aggregations: List of aggregation functions:
            [{"function": "COUNT", "column": "*", "alias": "total"}]
        group_by: Columns to GROUP BY
        order_by: Ordering: [{"column": "created_at", "direction": "DESC"}]
        limit: Maximum rows to return
        db_type: Database type for syntax variations (postgresql, mysql, mssql)
    
    Returns:
        {
            "success": bool,
            "sql": str,
            "components": {
                "select": "...",
                "from": "...",
                "joins": "...",
                "where": "...",
                "group_by": "...",
                "order_by": "...",
                "limit": "..."
            },
            "tables_used": [...],
            "columns_used": [...],
            "error": str | None
        }
    """
    try:
        if not tables:
            return {
                "success": False,
                "sql": "",
                "components": {},
                "tables_used": [],
                "columns_used": [],
                "error": "At least one table is required"
            }
        
        index = get_schema_index()
        primary_table = tables[0]
        components = {}
        columns_used = []
        
        # ============================================================
        # SELECT clause
        # ============================================================
        select_parts = []
        
        # Handle aggregations
        if aggregations:
            for agg in aggregations:
                func = agg.get("function", "COUNT").upper()
                col = agg.get("column", "*")
                alias = agg.get("alias", f"{func.lower()}_{col}")
                
                if col != "*":
                    # Qualify with table name if possible
                    table_name = agg.get("table", primary_table)
                    # Strip any alias the LLM may have embedded (e.g. "customers c" → "customers")
                    table_name = table_name.split()[0] if table_name else primary_table
                    # Strip table prefix from column if LLM already qualified it
                    col = col.split(".")[-1]
                    col = f"{table_name}.{col}"
                    columns_used.append(col)
                
                select_parts.append(f"{func}({col}) AS {alias}")
        
        # Handle regular columns
        if columns:
            for col in columns:
                if "." in col:
                    select_parts.append(col)
                    columns_used.append(col)
                else:
                    # Qualify with primary table
                    qualified = f"{primary_table}.{col}"
                    select_parts.append(qualified)
                    columns_used.append(qualified)
        elif not aggregations:
            # Default to all columns from primary table
            select_parts.append(f"{primary_table}.*")
        
        # Add group by columns to SELECT if grouping
        if group_by:
            for gb_col in group_by:
                if gb_col not in columns_used and f"{primary_table}.{gb_col}" not in select_parts:
                    qualified = gb_col if "." in gb_col else f"{primary_table}.{gb_col}"
                    if qualified not in select_parts:
                        select_parts.insert(0, qualified)
                        columns_used.append(qualified)
        
        components["select"] = ", ".join(select_parts) if select_parts else "*"
        
        # ============================================================
        # FROM clause
        # ============================================================
        components["from"] = primary_table
        
        # ============================================================
        # JOIN clauses
        # ============================================================
        join_clauses = []
        if joins:
            for join in joins:
                join_table = join.get("table", "")
                join_on = join.get("on", "")
                join_type = join.get("type", "INNER").upper().strip()
                
                # Strip trailing "JOIN" if LLM included it (e.g. "INNER JOIN" → "INNER")
                if join_type.endswith(" JOIN"):
                    join_type = join_type[:-5].strip()
                # If LLM sent just "JOIN", treat as INNER
                if join_type == "JOIN" or not join_type:
                    join_type = "INNER"
                
                if join_table and join_on:
                    join_clauses.append(f"{join_type} JOIN {join_table} ON {join_on}")
        
        # Auto-detect joins from relationships if multiple tables but no explicit joins
        if len(tables) > 1 and not joins:
            rels = index.get_relationships_for_tables(tables) if index.is_initialized else []
            for rel in rels:
                from_t = rel.get("from_table", "")
                to_t = rel.get("to_table", "")
                from_c = rel.get("from_column", "")
                to_c = rel.get("to_column", "")
                
                if from_t in tables and to_t in tables and from_c and to_c:
                    join_on = f"{from_t}.{from_c} = {to_t}.{to_c}"
                    # Add as LEFT JOIN to preserve primary table rows
                    join_table = to_t if from_t == primary_table else from_t
                    if join_table != primary_table:
                        join_clauses.append(f"LEFT JOIN {join_table} ON {join_on}")
        
        components["joins"] = " ".join(join_clauses)
        
        # ============================================================
        # WHERE / HAVING clause
        # Build a set of aggregation aliases so we can route filters
        # that reference computed aliases to HAVING instead of WHERE.
        # ============================================================
        agg_aliases = set()
        agg_expr_by_alias = {}  # alias -> full expression, e.g. "order_count" -> "COUNT(orders.id)"
        if aggregations:
            for agg in aggregations:
                alias = agg.get("alias", "")
                if alias:
                    agg_aliases.add(alias.lower())
                    func = agg.get("function", "COUNT").upper()
                    col = agg.get("column", "*")
                    if col != "*":
                        tbl = (agg.get("table", primary_table) or primary_table).split()[0]
                        col = col.split(".")[-1]
                        agg_expr_by_alias[alias.lower()] = f"{func}({tbl}.{col})"
                    else:
                        agg_expr_by_alias[alias.lower()] = f"{func}(*)"
        
        where_parts = []
        having_parts = []
        if filters:
            for f in filters:
                col = f.get("column", "")
                op = f.get("operator", "=")
                val = f.get("value")
                table = f.get("table", primary_table)
                
                if not col:
                    continue
                
                # Check if this filter targets an aggregation alias
                col_bare = col.split(".")[-1].lower()
                if col_bare in agg_aliases:
                    # Route to HAVING with the full aggregate expression
                    agg_expr = agg_expr_by_alias.get(col_bare, col_bare)
                    condition = _build_filter_condition(agg_expr, op, val)
                    if condition:
                        having_parts.append(condition)
                    continue
                
                qualified_col = f"{table}.{col}" if "." not in col else col
                columns_used.append(qualified_col)
                
                condition = _build_filter_condition(qualified_col, op, val)
                if condition:
                    where_parts.append(condition)
        
        components["where"] = " AND ".join(where_parts) if where_parts else ""
        components["having"] = " AND ".join(having_parts) if having_parts else ""
        
        # ============================================================
        # GROUP BY clause
        # ============================================================
        if group_by:
            gb_parts = []
            for gb in group_by:
                qualified = gb if "." in gb else f"{primary_table}.{gb}"
                gb_parts.append(qualified)
            components["group_by"] = ", ".join(gb_parts)
        else:
            components["group_by"] = ""
        
        # ============================================================
        # ORDER BY clause
        # ============================================================
        if order_by:
            ob_parts = []
            for ob in order_by:
                col = ob.get("column", "")
                direction = ob.get("direction", "ASC").upper()
                if col:
                    qualified = col if "." in col else f"{primary_table}.{col}"
                    ob_parts.append(f"{qualified} {direction}")
            components["order_by"] = ", ".join(ob_parts)
        else:
            components["order_by"] = ""
        
        # ============================================================
        # LIMIT clause
        # ============================================================
        if limit:
            if db_type == "mssql":
                # SQL Server uses TOP instead of LIMIT
                components["limit"] = ""
                components["select"] = f"TOP {limit} " + components["select"]
            else:
                components["limit"] = str(limit)
        else:
            components["limit"] = ""
        
        # ============================================================
        # Assemble final SQL
        # ============================================================
        sql = f"SELECT {components['select']}\nFROM {components['from']}"
        
        if components["joins"]:
            sql += f"\n{components['joins']}"
        
        if components["where"]:
            sql += f"\nWHERE {components['where']}"
        
        if components["group_by"]:
            sql += f"\nGROUP BY {components['group_by']}"
        
        if components.get("having"):
            sql += f"\nHAVING {components['having']}"
        
        if components["order_by"]:
            sql += f"\nORDER BY {components['order_by']}"
        
        if components["limit"] and db_type != "mssql":
            sql += f"\nLIMIT {components['limit']}"
        
        # ============================================================
        # Post-assembly SQL sanitization
        # Fix common LLM-induced syntax issues:
        # ============================================================
        # 1. Double JOIN keyword: "JOIN JOIN table" → "JOIN table"
        sql = re.sub(r'\bJOIN\s+JOIN\b', 'JOIN', sql, flags=re.IGNORECASE)
        # 2. Aggregate with table-space-alias: "COUNT(table alias.col)" → "COUNT(alias.col)"
        sql = re.sub(
            r'(COUNT|SUM|AVG|MIN|MAX)\(\s*(\w+)\s+(\w+)\.(\w+)\)',
            r'\1(\3.\4)',
            sql,
            flags=re.IGNORECASE,
        )
        # 3. "FROM table alias" already works, but "JOIN table alias ON" is fine too
        
        logger.debug(f"generate_sql: {intent} -> {len(sql)} chars")
        
        return {
            "success": True,
            "sql": sql,
            "components": components,
            "tables_used": tables,
            "columns_used": list(set(columns_used)),
            "error": None
        }
        
    except Exception as e:
        logger.error(f"generate_sql failed: {e}")
        return {
            "success": False,
            "sql": "",
            "components": {},
            "tables_used": tables if tables else [],
            "columns_used": [],
            "error": str(e)
        }


# Tool metadata for MCP registration
TOOL_METADATA = {
    "name": "generate_sql",
    "description": (
        "Generate SQL from a structured query plan. "
        "Provide tables, columns, filters, joins, and aggregations "
        "to compile a complete SQL query. "
        "Use search_tables and search_columns first to find the correct schema elements, "
        "then use this to generate the final SQL."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": "Query intent: SELECT, AGGREGATION, TIME_SERIES, RANKING, JOIN, FILTER",
                "enum": ["SELECT", "AGGREGATION", "TIME_SERIES", "RANKING", "JOIN", "FILTER", "COMPARISON"]
            },
            "tables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tables to query (first is primary)"
            },
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Columns to SELECT (omit for all)"
            },
            "filters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "column": {"type": "string"},
                        "operator": {"type": "string"},
                        "value": {},
                        "table": {"type": "string"}
                    }
                },
                "description": "WHERE conditions"
            },
            "joins": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "table": {"type": "string"},
                        "on": {"type": "string"},
                        "type": {"type": "string"}
                    }
                },
                "description": "JOIN specifications"
            },
            "aggregations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "function": {"type": "string"},
                        "column": {"type": "string"},
                        "alias": {"type": "string"}
                    }
                },
                "description": "Aggregation functions (COUNT, SUM, AVG, MAX, MIN)"
            },
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "GROUP BY columns"
            },
            "order_by": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "column": {"type": "string"},
                        "direction": {"type": "string", "enum": ["ASC", "DESC"]}
                    }
                },
                "description": "ORDER BY specifications"
            },
            "limit": {
                "type": "integer",
                "description": "Maximum rows to return"
            },
            "db_type": {
                "type": "string",
                "description": "Database type for syntax",
                "enum": ["postgresql", "mysql", "mssql", "oracle"],
                "default": "postgresql"
            }
        },
        "required": ["intent", "tables"]
    }
}
