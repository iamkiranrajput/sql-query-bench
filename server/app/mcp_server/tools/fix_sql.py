"""
Fix SQL Tool

Auto-fix failed SQL queries using error messages and schema context.
Applies rule-based fixes (typo correction, case folding, ...) and, when
an LLM client has been injected via :func:`set_llm_client`, falls back to
an LLM round-trip for harder fixes. The hackathon build does NOT wire an
LLM client in, so only the deterministic rule-based path is active.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from ..schema_index import get_schema_index

logger = logging.getLogger(__name__)

# Will be injected by the MCP server
_llm_client = None
_llm_model = "gpt-4o-mini"  # default, overridden by set_llm_model()


def set_llm_client(client) -> None:
    """Set the LLM client instance."""
    global _llm_client
    _llm_client = client


def set_llm_model(model: str) -> None:
    """Set the LLM model name for fix_sql calls."""
    global _llm_model
    if model:
        _llm_model = model


def fix_sql(
    sql: str,
    error_message: str,
    tables_context: Optional[List[str]] = None,
    attempt_count: int = 1,
    **_ignored,  # Tolerate stray kwargs (e.g. an LLM-supplied session_id)
) -> Dict[str, Any]:
    """
    Attempt to fix a failed SQL query based on the error message.
    
    Uses the error message and schema context to identify the issue
    and generate a corrected SQL query.
    
    Args:
        sql: The failed SQL query
        error_message: Error message from the database
        tables_context: List of tables involved (for schema lookup)
        attempt_count: Which fix attempt this is (for progressive fixes)
    
    Returns:
        {
            "success": bool,
            "fixed_sql": str,
            "changes": [...],  # List of changes made
            "explanation": str,  # What was wrong and how it was fixed
            "confidence": str,  # "high", "medium", "low"
            "error": str | None
        }
    """
    try:
        if not sql or not sql.strip():
            return {
                "success": False,
                "fixed_sql": "",
                "changes": [],
                "explanation": "No SQL provided",
                "confidence": "low",
                "error": "SQL query is required"
            }
        
        if not error_message:
            return {
                "success": False,
                "fixed_sql": sql,
                "changes": [],
                "explanation": "No error message provided",
                "confidence": "low",
                "error": "Error message is required to fix SQL"
            }
        
        changes = []
        fixed_sql = sql
        confidence = "medium"
        
        # Get schema context
        schema_context = _get_schema_context(tables_context)
        
        # Try rule-based fixes first (fast, deterministic)
        rule_fix, rule_changes = _apply_rule_fixes(sql, error_message)
        if rule_changes:
            fixed_sql = rule_fix
            changes.extend(rule_changes)
            confidence = "high"
        
        # If LLM available and rule fixes weren't enough, use LLM
        if _llm_client and (not changes or attempt_count > 1):
            llm_fix, llm_changes, llm_explanation = _fix_with_llm(
                sql, error_message, schema_context
            )
            if llm_fix and llm_fix != sql:
                fixed_sql = llm_fix
                changes.extend(llm_changes)
                explanation = llm_explanation
            else:
                explanation = _generate_explanation(error_message, changes)
        else:
            explanation = _generate_explanation(error_message, changes)
        
        # Validate the fix doesn't introduce new obvious issues
        if fixed_sql == sql:
            return {
                "success": False,
                "fixed_sql": sql,
                "changes": [],
                "explanation": f"Could not automatically fix: {error_message}",
                "confidence": "low",
                "error": "No fix found"
            }
        
        logger.debug(f"fix_sql: {len(changes)} changes, confidence={confidence}")
        
        return {
            "success": True,
            "fixed_sql": fixed_sql,
            "changes": changes,
            "explanation": explanation,
            "confidence": confidence,
            "error": None
        }
        
    except Exception as e:
        logger.error(f"fix_sql failed: {e}")
        return {
            "success": False,
            "fixed_sql": sql,
            "changes": [],
            "explanation": "",
            "confidence": "low",
            "error": str(e)
        }


def _get_schema_context(tables: Optional[List[str]]) -> str:
    """Get schema context for the specified tables."""
    if not tables:
        return ""
    
    index = get_schema_index()
    if not index.is_initialized:
        return ""
    
    context_parts = []
    for table_name in tables:
        table_info = index.get_table(table_name)
        if table_info:
            cols = [f"  - {col}: {info.get('data_type', 'unknown')}" 
                   for col, info in list(table_info.columns.items())[:20]]
            context_parts.append(f"Table {table_name}:\n" + "\n".join(cols))
    
    return "\n\n".join(context_parts)


def _apply_rule_fixes(sql: str, error_message: str) -> tuple:
    """Apply rule-based fixes for common errors."""
    error_lower = error_message.lower()
    changes = []
    fixed = sql
    
    # Column not found - try removing quotes or fixing case
    if "column" in error_lower and ("not found" in error_lower or "does not exist" in error_lower):
        # Extract column name from error
        col_match = re.search(r'column\s+["\']?(\w+)["\']?', error_message, re.IGNORECASE)
        if col_match:
            bad_col = col_match.group(1)
            # Try lowercase
            if bad_col != bad_col.lower():
                fixed = re.sub(rf'\b{bad_col}\b', bad_col.lower(), fixed)
                changes.append(f"Changed column '{bad_col}' to lowercase")
    
    # Table not found
    if "table" in error_lower and ("not found" in error_lower or "does not exist" in error_lower):
        table_match = re.search(r'table\s+["\']?(\w+)["\']?', error_message, re.IGNORECASE)
        if table_match:
            bad_table = table_match.group(1)
            # Try lowercase
            if bad_table != bad_table.lower():
                fixed = re.sub(rf'\b{bad_table}\b', bad_table.lower(), fixed)
                changes.append(f"Changed table '{bad_table}' to lowercase")
    
    # Ambiguous column reference - add table prefix
    if "ambiguous" in error_lower:
        # This typically requires schema context to fix properly
        pass
    
    # Syntax error near specific keyword
    syntax_match = re.search(r'syntax error\s+(?:at or )?near\s+["\']?(\w+)["\']?', 
                            error_message, re.IGNORECASE)
    if syntax_match:
        problem_word = syntax_match.group(1)
        # Common typos
        typo_fixes = {
            "FORM": "FROM",
            "SLECT": "SELECT",
            "WEHRE": "WHERE",
            "ODER": "ORDER",
            "GRUOP": "GROUP",
            "LIMT": "LIMIT",
        }
        if problem_word.upper() in typo_fixes:
            fixed = re.sub(rf'\b{problem_word}\b', typo_fixes[problem_word.upper()], 
                          fixed, flags=re.IGNORECASE)
            changes.append(f"Fixed typo '{problem_word}' -> '{typo_fixes[problem_word.upper()]}'")
    
    # Missing comma between columns
    if "syntax error" in error_lower and "," in sql:
        # Check for missing commas in SELECT clause
        pass
    
    # PostgreSQL specific: operator does not exist
    if "operator does not exist" in error_lower:
        # Try casting
        if "character varying" in error_lower and "integer" in error_lower:
            # Add CAST
            changes.append("Consider adding explicit type cast")
    
    # MySQL specific: Unknown column
    if "unknown column" in error_lower:
        col_match = re.search(r"Unknown column '([^']+)'", error_message, re.IGNORECASE)
        if col_match:
            bad_col = col_match.group(1)
            changes.append(f"Column '{bad_col}' not found in table")
    
    return fixed, changes


def _fix_with_llm(
    sql: str, 
    error_message: str, 
    schema_context: str
) -> tuple:
    """Use LLM to fix the SQL."""
    try:
        prompt = f"""Fix this SQL query based on the error message.

FAILED SQL:
{sql}

ERROR MESSAGE:
{error_message}

{f"SCHEMA CONTEXT:{chr(10)}{schema_context}" if schema_context else ""}

Provide only:
1. The fixed SQL query
2. A brief list of changes made
3. An explanation of what was wrong

Format:
FIXED_SQL:
[corrected SQL here]

CHANGES:
- [change 1]
- [change 2]

EXPLANATION:
[what was wrong and how you fixed it]"""

        response = _llm_client.chat.completions.create(
            model=_llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=1000,
        )
        
        content = response.choices[0].message.content
        
        # Parse response
        fixed_sql = sql
        changes = []
        explanation = ""
        
        # Extract fixed SQL
        sql_match = re.search(r'FIXED_SQL:\s*\n(.+?)(?=\nCHANGES:|\nEXPLANATION:|$)', 
                             content, re.DOTALL | re.IGNORECASE)
        if sql_match:
            fixed_sql = sql_match.group(1).strip()
            # Clean up markdown code blocks
            fixed_sql = re.sub(r'^```\w*\n?', '', fixed_sql)
            fixed_sql = re.sub(r'\n?```$', '', fixed_sql)
        
        # Extract changes
        changes_match = re.search(r'CHANGES:\s*\n(.+?)(?=\nEXPLANATION:|$)', 
                                 content, re.DOTALL | re.IGNORECASE)
        if changes_match:
            changes_text = changes_match.group(1)
            changes = [c.strip().lstrip('- ') for c in changes_text.split('\n') if c.strip()]
        
        # Extract explanation
        expl_match = re.search(r'EXPLANATION:\s*\n(.+)$', content, re.DOTALL | re.IGNORECASE)
        if expl_match:
            explanation = expl_match.group(1).strip()
        
        return fixed_sql, changes, explanation
        
    except Exception as e:
        logger.warning(f"LLM fix failed: {e}")
        return sql, [], ""


def _generate_explanation(error_message: str, changes: List[str]) -> str:
    """Generate explanation from error and changes."""
    if changes:
        return f"Fixed the following issues: {', '.join(changes)}."
    return f"The query failed with: {error_message[:200]}"


# Tool metadata for MCP registration
TOOL_METADATA = {
    "name": "fix_sql",
    "description": (
        "Attempt to fix a failed SQL query based on the error message. "
        "Analyzes the error and schema context to identify issues "
        "and generate a corrected query. "
        "Use when execute_sql returns an error."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "The failed SQL query"
            },
            "error_message": {
                "type": "string",
                "description": "Error message from the database"
            },
            "tables_context": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tables involved for schema lookup"
            },
            "attempt_count": {
                "type": "integer",
                "description": "Which fix attempt this is",
                "default": 1
            }
        },
        "required": ["sql", "error_message"]
    }
}
