"""
Explain SQL Tool

Generate plain English explanations of SQL queries. Uses a deterministic
rule-based parser by default; if an LLM client has been injected via
:func:`set_llm_client`, it can also produce a richer prose explanation.
The hackathon build does NOT wire an LLM client in.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Will be injected by the MCP server
_llm_client = None


def set_llm_client(client) -> None:
    """Set the LLM client instance."""
    global _llm_client
    _llm_client = client


def explain_sql(
    sql: str,
    include_components: bool = True,
    target_audience: str = "business",
) -> Dict[str, Any]:
    """
    Explain a SQL query in plain English.
    
    Uses LLM to generate a human-readable explanation of what
    the SQL query does, including tables accessed, filters applied,
    and data returned.
    
    Args:
        sql: SQL query to explain
        include_components: Include breakdown of query components
        target_audience: "business" for non-technical, "technical" for developers
    
    Returns:
        {
            "success": bool,
            "explanation": str,  # Plain English explanation
            "summary": str,  # One-line summary
            "components": {
                "tables": [...],
                "columns": [...],
                "filters": [...],
                "aggregations": [...],
                "joins": [...],
                "ordering": str,
                "limit": int | None
            },
            "complexity": str,  # "simple", "moderate", "complex"
            "error": str | None
        }
    """
    try:
        if not sql or not sql.strip():
            return {
                "success": False,
                "explanation": "",
                "summary": "",
                "components": {},
                "complexity": "unknown",
                "error": "SQL query is required"
            }
        
        # Parse SQL for components
        components = _parse_sql_components(sql)
        
        # Determine complexity
        complexity = _assess_complexity(components)
        
        # If LLM is available, use it for explanation
        if _llm_client:
            explanation, summary = _explain_with_llm(sql, target_audience)
        else:
            # Fallback to rule-based explanation
            explanation, summary = _explain_rule_based(sql, components)
        
        return {
            "success": True,
            "explanation": explanation,
            "summary": summary,
            "components": components if include_components else {},
            "complexity": complexity,
            "error": None
        }
        
    except Exception as e:
        logger.error(f"explain_sql failed: {e}")
        return {
            "success": False,
            "explanation": "",
            "summary": "",
            "components": {},
            "complexity": "unknown",
            "error": str(e)
        }


def _parse_sql_components(sql: str) -> Dict[str, Any]:
    """Parse SQL into its components."""
    import re
    
    components = {
        "tables": [],
        "columns": [],
        "filters": [],
        "aggregations": [],
        "joins": [],
        "ordering": "",
        "limit": None,
        "group_by": [],
    }
    
    sql_upper = sql.upper()
    
    # Extract tables (FROM and JOIN)
    from_match = re.search(r'\bFROM\s+([^\s,()]+)', sql, re.IGNORECASE)
    if from_match:
        components["tables"].append(from_match.group(1).strip('"\'`'))
    
    join_matches = re.findall(r'\bJOIN\s+([^\s,()]+)', sql, re.IGNORECASE)
    components["joins"] = join_matches
    components["tables"].extend([t.strip('"\'`') for t in join_matches])
    
    # Extract SELECT columns
    select_match = re.search(r'\bSELECT\s+(.+?)\s+FROM\b', sql, re.IGNORECASE | re.DOTALL)
    if select_match:
        select_clause = select_match.group(1)
        # Split by comma, handling functions
        cols = re.split(r',(?![^()]*\))', select_clause)
        components["columns"] = [c.strip() for c in cols]
    
    # Check for aggregations
    agg_funcs = ["COUNT", "SUM", "AVG", "MAX", "MIN", "DISTINCT"]
    for func in agg_funcs:
        if func in sql_upper:
            matches = re.findall(rf'{func}\s*\([^)]+\)', sql, re.IGNORECASE)
            components["aggregations"].extend(matches)
    
    # Extract WHERE filters
    where_match = re.search(r'\bWHERE\s+(.+?)(?:\bGROUP\s+BY|\bORDER\s+BY|\bLIMIT|\bHAVING|$)', 
                           sql, re.IGNORECASE | re.DOTALL)
    if where_match:
        components["filters"] = [where_match.group(1).strip()]
    
    # Extract GROUP BY
    group_match = re.search(r'\bGROUP\s+BY\s+([^)]+?)(?:\bORDER\s+BY|\bHAVING|\bLIMIT|$)',
                           sql, re.IGNORECASE)
    if group_match:
        components["group_by"] = [g.strip() for g in group_match.group(1).split(',')]
    
    # Extract ORDER BY
    order_match = re.search(r'\bORDER\s+BY\s+(.+?)(?:\bLIMIT|$)', sql, re.IGNORECASE)
    if order_match:
        components["ordering"] = order_match.group(1).strip()
    
    # Extract LIMIT
    limit_match = re.search(r'\bLIMIT\s+(\d+)', sql, re.IGNORECASE)
    if limit_match:
        components["limit"] = int(limit_match.group(1))
    
    # Extract JOINs with conditions
    join_full = re.findall(r'((?:LEFT|RIGHT|INNER|OUTER|CROSS)?\s*JOIN\s+\w+\s+ON\s+[^)]+?)(?=\s+(?:LEFT|RIGHT|INNER|WHERE|GROUP|ORDER|LIMIT|$))',
                          sql, re.IGNORECASE)
    if join_full:
        components["joins"] = [j.strip() for j in join_full]
    
    return components


def _assess_complexity(components: Dict[str, Any]) -> str:
    """Assess query complexity based on components."""
    score = 0
    
    # Tables
    score += len(components.get("tables", [])) * 2
    
    # Joins
    score += len(components.get("joins", [])) * 3
    
    # Aggregations
    score += len(components.get("aggregations", [])) * 2
    
    # Group by
    score += len(components.get("group_by", [])) * 2
    
    # Filters (approximate complexity)
    filters = components.get("filters", [])
    if filters:
        filter_text = " ".join(filters).upper()
        score += filter_text.count(" AND ") * 1
        score += filter_text.count(" OR ") * 2
        score += filter_text.count("(") * 1
    
    if score <= 3:
        return "simple"
    elif score <= 8:
        return "moderate"
    else:
        return "complex"


def _explain_with_llm(sql: str, target_audience: str) -> tuple:
    """Generate explanation using LLM."""
    try:
        audience_note = (
            "Explain in simple terms for business users."
            if target_audience == "business"
            else "Explain with technical details for developers."
        )
        
        prompt = f"""Explain this SQL query in plain English.
{audience_note}

SQL:
{sql}

Provide:
1. A one-line summary of what this query does
2. A detailed explanation (2-4 sentences) describing:
   - What data is being retrieved
   - Any filters or conditions
   - How results are organized

Format your response as:
SUMMARY: [one line]
EXPLANATION: [detailed explanation]"""

        response = _llm_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
        )
        
        content = response.choices[0].message.content
        
        # Parse response
        summary = ""
        explanation = ""
        
        if "SUMMARY:" in content:
            parts = content.split("EXPLANATION:", 1)
            summary = parts[0].replace("SUMMARY:", "").strip()
            if len(parts) > 1:
                explanation = parts[1].strip()
        else:
            explanation = content
            summary = content.split(".")[0] + "." if "." in content else content[:100]
        
        return explanation, summary
        
    except Exception as e:
        logger.warning(f"LLM explanation failed, using rule-based: {e}")
        return _explain_rule_based(sql, _parse_sql_components(sql))


def _explain_rule_based(sql: str, components: Dict[str, Any]) -> tuple:
    """Generate rule-based explanation without LLM."""
    tables = components.get("tables", ["a table"])
    columns = components.get("columns", ["*"])
    filters = components.get("filters", [])
    aggregations = components.get("aggregations", [])
    ordering = components.get("ordering", "")
    limit = components.get("limit")
    group_by = components.get("group_by", [])
    
    # Build summary
    if aggregations:
        summary = f"Aggregates data from {', '.join(tables)}"
    elif limit and limit <= 10:
        summary = f"Gets top {limit} records from {', '.join(tables)}"
    else:
        summary = f"Retrieves data from {', '.join(tables)}"
    
    # Build explanation
    parts = []
    
    # What data
    if "*" in str(columns):
        parts.append(f"This query retrieves all columns from {', '.join(tables)}.")
    else:
        col_str = ", ".join(str(c) for c in columns[:5])
        if len(columns) > 5:
            col_str += f" and {len(columns) - 5} more columns"
        parts.append(f"This query retrieves {col_str} from {', '.join(tables)}.")
    
    # Filters
    if filters:
        parts.append(f"It filters results based on: {filters[0][:100]}.")
    
    # Grouping
    if group_by:
        parts.append(f"Results are grouped by {', '.join(group_by)}.")
    
    # Ordering
    if ordering:
        parts.append(f"Results are sorted by {ordering}.")
    
    # Limit
    if limit:
        parts.append(f"Only the first {limit} rows are returned.")
    
    explanation = " ".join(parts)
    
    return explanation, summary


# Tool metadata for MCP registration
TOOL_METADATA = {
    "name": "explain_sql",
    "description": (
        "Explain a SQL query in plain English. "
        "Provides a human-readable summary of what the query does, "
        "including tables accessed, filters applied, and data returned. "
        "Useful for helping users understand generated SQL."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "SQL query to explain"
            },
            "include_components": {
                "type": "boolean",
                "description": "Include breakdown of query components",
                "default": True
            },
            "target_audience": {
                "type": "string",
                "description": "Explanation style",
                "enum": ["business", "technical"],
                "default": "business"
            }
        },
        "required": ["sql"]
    }
}
