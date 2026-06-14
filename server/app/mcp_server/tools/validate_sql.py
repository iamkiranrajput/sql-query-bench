"""
Validate SQL Tool

SQL syntax validation and security checks.
Blocks destructive statements (DELETE, UPDATE, INSERT, DROP, etc.)
"""

import logging
import re
from typing import Any, Dict, List, Set

import sqlparse

logger = logging.getLogger(__name__)

# Forbidden SQL operations (security)
FORBIDDEN_KEYWORDS: Set[str] = {
    "DELETE", "UPDATE", "INSERT", "DROP", "TRUNCATE", "ALTER",
    "CREATE", "GRANT", "REVOKE", "EXEC", "EXECUTE", "CALL",
    "MERGE", "REPLACE", "RENAME"
}

# Patterns that indicate SQL injection attempts
INJECTION_PATTERNS = [
    r";\s*--",  # Statement terminator followed by comment
    r";\s*DROP",  # Statement terminator followed by DROP
    r";\s*DELETE",  # Statement terminator followed by DELETE
    r"'\s*OR\s+'1'\s*=\s*'1",  # Classic injection
    r"'\s*OR\s+1\s*=\s*1",  # Classic injection variant
    r"UNION\s+SELECT",  # UNION injection
    r"INTO\s+OUTFILE",  # File write attempt
    r"LOAD_FILE",  # File read attempt
    r"xp_cmdshell",  # SQL Server command execution
    r"sp_executesql",  # SQL Server dynamic SQL
]


def validate_sql(
    sql: str,
    check_syntax: bool = True,
    check_security: bool = True,
    allowed_operations: List[str] = None,
    session_id: str = None,  # Accepted and ignored (see note below)
    **_ignored,             # Tolerate any other auto-injected kwargs
) -> Dict[str, Any]:
    """
    Validate SQL for syntax errors and security issues.
    
    Performs two types of validation:
    1. Syntax validation using sqlparse
    2. Security validation to block destructive operations
    
    Args:
        sql: SQL query to validate
        check_syntax: Whether to check SQL syntax (default: True)
        check_security: Whether to check for forbidden operations (default: True)
        allowed_operations: Override list of allowed SQL operations
                           (default: only SELECT)
        session_id: Ignored. validate_sql is a pure static analyzer and needs no
                    DB session, but the LLM (and historical auto-injection) often
                    passes one because sibling tools require it. Accepting and
                    ignoring it keeps the call from failing with a TypeError.
    
    Returns:
        {
            "valid": bool,
            "errors": [...],  # Critical issues that block execution
            "warnings": [...],  # Non-critical issues
            "statement_type": str,  # SELECT, INSERT, etc.
            "tables_referenced": [...],
            "formatted_sql": str,  # Pretty-printed SQL
            "error": str | None
        }
    """
    try:
        errors = []
        warnings = []
        tables_referenced = []
        statement_type = "UNKNOWN"
        formatted_sql = sql
        
        if not sql or not sql.strip():
            return {
                "valid": False,
                "errors": ["Empty SQL statement"],
                "warnings": [],
                "statement_type": "UNKNOWN",
                "tables_referenced": [],
                "formatted_sql": "",
                "error": None
            }
        
        # Clean up SQL
        sql_clean = sql.strip()
        sql_upper = sql_clean.upper()
        
        # ================================================================
        # Security Validation
        # ================================================================
        if check_security:
            # Check for forbidden keywords
            allowed = set(op.upper() for op in (allowed_operations or ["SELECT"]))
            
            # Parse to get statement type
            parsed = sqlparse.parse(sql_clean)
            if parsed:
                stmt = parsed[0]
                stmt_type = stmt.get_type()
                if stmt_type:
                    statement_type = stmt_type.upper()
                    
                    # Check if statement type is allowed
                    if statement_type not in allowed:
                        errors.append(
                            f"Statement type '{statement_type}' is not allowed. "
                            f"Only {', '.join(allowed)} operations are permitted."
                        )
            
            # Check for forbidden keywords in the SQL text
            for keyword in FORBIDDEN_KEYWORDS:
                # Use word boundary to avoid false positives
                pattern = rf'\b{keyword}\b'
                if re.search(pattern, sql_upper):
                    if keyword not in allowed:
                        errors.append(f"Forbidden keyword '{keyword}' detected")
            
            # Check for SQL injection patterns
            for pattern in INJECTION_PATTERNS:
                if re.search(pattern, sql_clean, re.IGNORECASE):
                    errors.append(f"Potential SQL injection pattern detected")
                    break
            
            # Check for multiple statements (potential injection)
            statements = sqlparse.split(sql_clean)
            if len(statements) > 1:
                warnings.append(
                    "Multiple SQL statements detected. "
                    "Only the first statement will be considered."
                )
        
        # ================================================================
        # Syntax Validation
        # ================================================================
        if check_syntax:
            try:
                # Parse SQL
                parsed = sqlparse.parse(sql_clean)
                
                if not parsed or not parsed[0].tokens:
                    errors.append("Failed to parse SQL - invalid syntax")
                else:
                    # Format SQL
                    formatted_sql = sqlparse.format(
                        sql_clean,
                        reindent=True,
                        keyword_case='upper'
                    )
                    
                    # Extract tables (basic heuristic)
                    tables_referenced = _extract_tables(sql_clean)
                    
                    # Check for common syntax issues
                    if "SELECT" in sql_upper and "FROM" not in sql_upper:
                        if "*" in sql_clean or "," in sql_clean:
                            warnings.append("SELECT without FROM clause")
                    
                    # Check for unbalanced parentheses
                    if sql_clean.count("(") != sql_clean.count(")"):
                        errors.append("Unbalanced parentheses")
                    
                    # Check for unbalanced quotes
                    single_quotes = sql_clean.count("'")
                    if single_quotes % 2 != 0:
                        errors.append("Unbalanced single quotes")
                    
            except Exception as e:
                errors.append(f"SQL parse error: {str(e)}")
        
        is_valid = len(errors) == 0
        
        logger.debug(
            f"validate_sql: valid={is_valid}, "
            f"errors={len(errors)}, warnings={len(warnings)}"
        )
        
        return {
            "valid": is_valid,
            "errors": errors,
            "warnings": warnings,
            "statement_type": statement_type,
            "tables_referenced": tables_referenced,
            "formatted_sql": formatted_sql,
            "error": None
        }
        
    except Exception as e:
        logger.error(f"validate_sql failed: {e}")
        return {
            "valid": False,
            "errors": [str(e)],
            "warnings": [],
            "statement_type": "UNKNOWN",
            "tables_referenced": [],
            "formatted_sql": sql,
            "error": str(e)
        }


def _extract_tables(sql: str) -> List[str]:
    """Extract table names from SQL (basic heuristic)."""
    tables = []
    sql_upper = sql.upper()
    
    # FROM clause
    from_match = re.search(r'\bFROM\s+([^\s,()]+)', sql, re.IGNORECASE)
    if from_match:
        tables.append(from_match.group(1).strip('"\'`[]'))
    
    # JOIN clauses
    join_matches = re.findall(r'\bJOIN\s+([^\s,()]+)', sql, re.IGNORECASE)
    tables.extend([t.strip('"\'`[]') for t in join_matches])
    
    # Remove duplicates and SQL keywords
    sql_keywords = {"INNER", "LEFT", "RIGHT", "OUTER", "CROSS", "NATURAL", "ON", "AND", "OR"}
    tables = [t for t in set(tables) if t.upper() not in sql_keywords]
    
    return tables


# Tool metadata for MCP registration
TOOL_METADATA = {
    "name": "validate_sql",
    "description": (
        "Validate SQL for syntax errors and security issues. "
        "Checks for forbidden operations (DELETE, UPDATE, DROP, etc.) "
        "and potential SQL injection patterns. "
        "Always validate SQL before executing it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "SQL query to validate"
            },
            "check_syntax": {
                "type": "boolean",
                "description": "Check SQL syntax",
                "default": True
            },
            "check_security": {
                "type": "boolean",
                "description": "Check for forbidden operations",
                "default": True
            },
            "allowed_operations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Override allowed SQL operations (default: SELECT only)"
            }
        },
        "required": ["sql"]
    }
}
