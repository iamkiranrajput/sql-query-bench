"""
MCP Server Package

Model Context Protocol server exposing granular SQL tools for 
LLM-based natural language to SQL generation.

Tools:
- search_tables: FAISS semantic search over schema
- search_columns: Column lookup with types and metadata
- check_relationships: FK/inferred relationship discovery
- preview_data: Safe read-only data preview
- generate_sql: SQL compilation from structured context
- validate_sql: Syntax and security validation
- execute_sql: Query execution with results
- explain_sql: Plain English SQL explanation
- fix_sql: Auto-fix failed queries
- get_conversation_context: Session state retrieval
"""

from .server import mcp_server, get_mcp_server
from .context import ConversationContext, context_manager
from .schema_index import SchemaIndex, get_schema_index

__all__ = [
    "mcp_server",
    "get_mcp_server",
    "ConversationContext",
    "context_manager",
    "SchemaIndex",
    "get_schema_index",
]
