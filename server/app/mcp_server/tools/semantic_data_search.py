"""
Semantic Data Search Tool (pgvector)

Runs an in-database **semantic similarity search** over an embedding column using
the PostgreSQL ``pgvector`` extension. This answers "find rows similar to ..."
questions that plain SQL ``WHERE``/``ILIKE`` filters cannot — e.g. "products
like this description", "tickets similar to this complaint".

The query text is embedded with the SAME local ``all-MiniLM-L6-v2`` model used by
the FAISS schema index (384 dims), so no extra model or API call is needed. The
embedding is bound as a parameter (never string-concatenated), and all table /
column identifiers are strictly validated to prevent SQL injection.

Requires the ``vector`` (pgvector) extension — call ``detect_extensions`` first.
"""

import logging
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Injected by the MCP server during initialisation.
_db_service = None

# Fallback embedding model, loaded lazily only if the schema index model is not
# already in memory (avoids a second ~90MB model load when possible).
_local_model = None

# Strict identifier validation: a bare identifier or a schema-qualified one.
# Identifiers cannot be parameterised, so they MUST be validated, not trusted.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# pgvector distance operators (whitelist — never interpolate arbitrary text).
_METRIC_OPERATORS = {
    "cosine": "<=>",
    "l2": "<->",
    "inner_product": "<#>",
}

_MAX_LIMIT = 100


def set_database_service(db_service) -> None:
    """Set the database service instance."""
    global _db_service
    _db_service = db_service


def _valid_identifier(name: str) -> bool:
    """True if ``name`` is a safe (optionally schema-qualified) SQL identifier."""
    if not name or not isinstance(name, str):
        return False
    parts = name.split(".")
    if len(parts) > 2:
        return False
    return all(_IDENT_RE.match(p) for p in parts)


def _embed(query_text: str) -> List[float]:
    """Embed ``query_text`` into a 384-d vector with all-MiniLM-L6-v2."""
    # Use the locally-cached model offline. This avoids corporate-proxy SSL
    # failures when contacting Hugging Face — the model is shared with the
    # FAISS schema index and lives in the default HF cache. Set
    # QUERYBENCH_ALLOW_HF_DOWNLOAD=1 to permit an online fetch instead.
    if os.getenv("QUERYBENCH_ALLOW_HF_DOWNLOAD") != "1":
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    model = None
    # Prefer the already-loaded schema-index model.
    try:
        from ..schema_index import get_schema_index

        model = getattr(get_schema_index(), "_model", None)
    except Exception:
        model = None

    if model is None:
        global _local_model
        if _local_model is None:
            from sentence_transformers import SentenceTransformer  # type: ignore

            # No cache_folder override: use the default Hugging Face cache
            # (~/.cache/huggingface/hub) where the model is already downloaded,
            # so offline loading succeeds.
            _local_model = SentenceTransformer(
                "sentence-transformers/all-MiniLM-L6-v2"
            )
        model = _local_model

    vec = model.encode([query_text], show_progress_bar=False, convert_to_numpy=True)[0]
    return [float(x) for x in vec]


def _json_safe_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce row values to JSON-serializable types.

    PostgreSQL NUMERIC columns (e.g. ``price``) come back as ``Decimal``, and
    date/time columns as ``datetime`` — neither is JSON-serializable, so the
    MCP layer would raise ``Object of type Decimal is not JSON serializable``.
    """
    safe: Dict[str, Any] = {}
    for key, value in row.items():
        if value is None or isinstance(value, (str, int, float, bool, list, dict)):
            safe[key] = value
        elif isinstance(value, Decimal):
            safe[key] = float(value)
        elif hasattr(value, "isoformat"):  # date / datetime
            safe[key] = value.isoformat()
        elif isinstance(value, bytes):
            safe[key] = value.hex()
        else:
            safe[key] = str(value)
    return safe


def semantic_data_search(
    session_id: str,
    table: str,
    embedding_column: str,
    query_text: str,
    select_columns: Optional[List[str]] = None,
    limit: int = 10,
    metric: str = "cosine",
) -> Dict[str, Any]:
    """
    Find rows whose embedding column is most similar to ``query_text``.

    Args:
        session_id: Database session ID (auto-injected).
        table: Table to search (optionally schema-qualified, e.g. "public.products").
        embedding_column: The ``vector`` column to compare against.
        query_text: Natural-language text to embed and match (e.g. a description).
        select_columns: Columns to return (default: all). Each is validated.
        limit: Max rows to return (default 10, capped at 100).
        metric: Distance metric — "cosine" (default), "l2", or "inner_product".

    Returns:
        {
            "success": bool,
            "rows": [ {col: value, ..., "_distance": float}, ... ],
            "row_count": int,
            "sql": str,        # the generated SQL (embedding param redacted)
            "metric": str,
            "error": str | None
        }
    """
    if not _db_service:
        return {"success": False, "error": "Database service not initialized"}
    if not session_id:
        return {"success": False, "error": "session_id is required"}

    # -- validate identifiers (anti-injection) --------------------------------
    if not _valid_identifier(table):
        return {"success": False, "error": f"Invalid table identifier: {table!r}"}
    if not _valid_identifier(embedding_column):
        return {
            "success": False,
            "error": f"Invalid embedding_column identifier: {embedding_column!r}",
        }
    if metric not in _METRIC_OPERATORS:
        return {
            "success": False,
            "error": f"Invalid metric {metric!r}. Use one of: {', '.join(_METRIC_OPERATORS)}",
        }

    if select_columns:
        for col in select_columns:
            if col != "*" and not _valid_identifier(col):
                return {"success": False, "error": f"Invalid column identifier: {col!r}"}
        select_part = ", ".join(select_columns)
    else:
        select_part = "*"

    try:
        limit = max(1, min(int(limit), _MAX_LIMIT))
    except (TypeError, ValueError):
        limit = 10

    session = _db_service.get_session(session_id)
    if not session:
        return {"success": False, "error": "Invalid or expired database session"}

    operator = _METRIC_OPERATORS[metric]

    try:
        vector = _embed(query_text)
    except ImportError:
        return {
            "success": False,
            "error": (
                "sentence-transformers is not installed; cannot embed the query "
                "for semantic search."
            ),
        }
    except Exception as e:
        return {"success": False, "error": f"Failed to embed query: {e}"}

    # pgvector literal form, e.g. "[0.12,0.34,...]". Bound as a parameter and
    # cast to vector — never string-concatenated into the SQL body.
    vector_literal = "[" + ",".join(repr(x) for x in vector) + "]"

    distance_expr = f"{embedding_column} {operator} CAST(:qvec AS vector)"
    sql = (
        f"SELECT {select_part}, ({distance_expr}) AS _distance "
        f"FROM {table} "
        f"ORDER BY {distance_expr} "
        f"LIMIT :lim"
    )

    try:
        with session.engine.connect() as conn:
            result = conn.execute(text(sql), {"qvec": vector_literal, "lim": limit})
            rows = [_json_safe_row(dict(m)) for m in result.mappings().all()]
    except Exception as e:
        msg = str(e)
        hint = ""
        if "vector" in msg.lower() and "type" in msg.lower():
            hint = (
                " — the pgvector extension may not be installed or "
                f"'{embedding_column}' may not be a vector column. "
                "Call detect_extensions to confirm."
            )
        logger.error("semantic_data_search failed: %s", e)
        return {
            "success": False,
            "rows": [],
            "row_count": 0,
            "sql": sql.replace(":qvec", "<embedding>"),
            "metric": metric,
            "error": f"{msg}{hint}",
        }

    logger.debug("semantic_data_search '%s' on %s.%s -> %d rows",
                 query_text, table, embedding_column, len(rows))
    return {
        "success": True,
        "rows": rows,
        "row_count": len(rows),
        "sql": sql.replace(":qvec", "<embedding>"),
        "metric": metric,
        "error": None,
    }


# Tool metadata for MCP registration
TOOL_METADATA = {
    "name": "semantic_data_search",
    "description": (
        "Semantic similarity search over a pgvector embedding column — finds rows "
        "most similar in MEANING to a piece of text (e.g. 'products like this "
        "description', 'tickets similar to this complaint'). Use this instead of "
        "ILIKE/keyword filters when the user wants conceptually-similar rows. "
        "Requires the pgvector extension (check with detect_extensions first)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Database session ID (auto-injected)",
            },
            "table": {
                "type": "string",
                "description": "Table to search (optionally schema-qualified)",
            },
            "embedding_column": {
                "type": "string",
                "description": "The pgvector column to compare against",
            },
            "query_text": {
                "type": "string",
                "description": "Natural-language text to embed and match",
            },
            "select_columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Columns to return (default: all)",
            },
            "limit": {
                "type": "integer",
                "description": "Max rows to return (default 10, max 100)",
                "default": 10,
            },
            "metric": {
                "type": "string",
                "enum": ["cosine", "l2", "inner_product"],
                "description": "Distance metric (default: cosine)",
                "default": "cosine",
            },
        },
        "required": ["session_id", "table", "embedding_column", "query_text"],
    },
}
