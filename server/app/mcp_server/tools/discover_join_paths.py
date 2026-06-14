"""
Discover Join Paths Tool

When FK-based JOINs return 0 rows, this tool samples both tables
and finds alternative join conditions by pattern-matching column values.
Implements Copilot-style iterative join discovery.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Will be injected by the MCP server
_db_service = None

# Known alternative join paths (loaded from alternative_joins.json)
_alternative_joins: Dict[str, Any] = {}


def set_database_service(db_service) -> None:
    """Set the database service instance."""
    global _db_service
    _db_service = db_service


def load_alternative_joins(file_path: str) -> None:
    """Load known alternative join paths from JSON config."""
    global _alternative_joins
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            _alternative_joins = json.load(f)
        logger.info(f"Loaded {len(_alternative_joins)} alternative join paths")
    except FileNotFoundError:
        logger.warning(f"Alternative joins file not found: {file_path}")
        _alternative_joins = {}
    except Exception as e:
        logger.error(f"Failed to load alternative joins: {e}")
        _alternative_joins = {}


def _sanitize_identifier(name: str) -> str:
    """Sanitize a SQL identifier to prevent injection."""
    return re.sub(r'[^a-zA-Z0-9_]', '', name)


def _execute_safe(session_id: str, sql: str) -> Dict[str, Any]:
    """Execute a safe read-only query, returning a dict with success/records/error."""
    if not _db_service:
        return {"success": False, "records": [], "error": "No DB service"}
    session = _db_service.get_session(session_id)
    if not session:
        return {"success": False, "records": [], "error": f"Invalid session: {session_id}"}
    try:
        records, columns, row_count = _db_service.execute_query(session_id, sql)
        return {"success": True, "records": records, "columns": columns, "row_count": row_count}
    except Exception as e:
        return {"success": False, "records": [], "error": str(e)}


def discover_join_paths(
    session_id: str,
    source_table: str,
    target_table: str,
    context: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Discover join paths between two tables using multiple strategies.

    Strategies tried in order:
    1. Known alternative joins registry (instant)
    2. FK-based join from schema_hints (fast)
    3. Column name overlap detection (medium)
    4. Value pattern matching via sampling (thorough)

    Args:
        session_id: Database session ID.
        source_table: Source table name.
        target_table: Target table name.
        context: User query context for smarter matching.

    Returns:
        {
            "success": bool,
            "join_paths": [
                {
                    "join_condition": str,
                    "method": "known_registry" | "fk" | "column_overlap" | "pattern_match",
                    "confidence": "high" | "medium" | "low",
                    "description": str,
                    "verified_row_count": int | None
                }
            ],
            "strategies_tried": [str],
            "error": str | None
        }
    """
    try:
        if not _db_service:
            return {
                "success": False,
                "join_paths": [],
                "strategies_tried": [],
                "error": "Database service not initialized",
            }

        if not session_id:
            return {
                "success": False,
                "join_paths": [],
                "strategies_tried": [],
                "error": "session_id is required",
            }

        src = _sanitize_identifier(source_table)
        tgt = _sanitize_identifier(target_table)
        join_paths: List[Dict[str, Any]] = []
        strategies_tried: List[str] = []

        # ── Strategy 1: Known alternative joins registry ──
        strategies_tried.append("known_registry")
        registry_key = f"{src} → {tgt}"
        reverse_key = f"{tgt} → {src}"

        known = _alternative_joins.get(registry_key) or _alternative_joins.get(reverse_key)
        if known and known.get("solution"):
            sol = known["solution"]
            join_cond = sol.get("join_condition", "")
            # Verify it produces rows
            verified_count = None
            try:
                verify_sql = (
                    f"SELECT COUNT(*) as cnt FROM {src} "
                    f"JOIN {tgt} ON {join_cond} LIMIT 1"
                )
                vr = _execute_safe(session_id, verify_sql)
                if vr.get("success") and vr.get("records"):
                    verified_count = vr["records"][0].get("cnt") or 0
            except Exception:
                pass

            join_paths.append({
                "join_condition": join_cond,
                "method": "known_registry",
                "confidence": "high" if (verified_count and verified_count > 0) else "medium",
                "description": sol.get("description", "Known alternative join"),
                "verified_row_count": verified_count,
            })

            if verified_count and verified_count > 0:
                return {
                    "success": True,
                    "join_paths": join_paths,
                    "strategies_tried": strategies_tried,
                    "error": None,
                }

        # ── Strategy 2: Standard FK join via column name convention ──
        strategies_tried.append("fk_convention")
        fk_patterns = [
            (f"{src}.{tgt}_id", f"{tgt}.{tgt}_id"),
            (f"{src}.{tgt}_id", f"{tgt}.id"),
            (f"{tgt}.{src}_id", f"{src}.{src}_id"),
            (f"{tgt}.{src}_id", f"{src}.id"),
        ]
        for src_col, tgt_col in fk_patterns:
            try:
                fk_sql = (
                    f"SELECT COUNT(*) as cnt FROM {src} "
                    f"JOIN {tgt} ON {src_col} = {tgt_col} LIMIT 1"
                )
                fk_result = _execute_safe(session_id, fk_sql)
                if fk_result.get("success") and fk_result.get("records"):
                    cnt = fk_result["records"][0].get("cnt") or 0
                    if cnt > 0:
                        join_paths.append({
                            "join_condition": f"{src_col} = {tgt_col}",
                            "method": "fk_convention",
                            "confidence": "high",
                            "description": f"FK convention join: {src_col} = {tgt_col}",
                            "verified_row_count": cnt,
                        })
                        return {
                            "success": True,
                            "join_paths": join_paths,
                            "strategies_tried": strategies_tried,
                            "error": None,
                        }
            except Exception:
                continue

        # ── Strategy 3: Column name overlap ──
        strategies_tried.append("column_overlap")
        try:
            overlap_sql = f"""
                SELECT a.column_name
                FROM information_schema.columns a
                JOIN information_schema.columns b
                  ON a.column_name = b.column_name
                WHERE a.table_name = '{src}'
                  AND b.table_name = '{tgt}'
                  AND a.table_schema = 'public'
                  AND b.table_schema = 'public'
                  AND a.column_name NOT IN ('id', 'created', 'lastchanged', 'comments')
                ORDER BY a.ordinal_position
            """
            overlap_result = _execute_safe(session_id, overlap_sql)
            if overlap_result.get("success"):
                for row in overlap_result.get("records", []):
                    col = row.get("column_name", "")
                    if not col:
                        continue
                    try:
                        test_sql = (
                            f"SELECT COUNT(*) as cnt FROM {src} "
                            f"JOIN {tgt} ON {src}.{col} = {tgt}.{col} LIMIT 1"
                        )
                        test_r = _execute_safe(session_id, test_sql)
                        if test_r.get("success") and test_r.get("records"):
                            cnt = test_r["records"][0].get("cnt") or 0
                            if cnt > 0:
                                join_paths.append({
                                    "join_condition": f"{src}.{col} = {tgt}.{col}",
                                    "method": "column_overlap",
                                    "confidence": "medium",
                                    "description": f"Shared column: {col}",
                                    "verified_row_count": cnt,
                                })
                    except Exception:
                        continue
        except Exception as e:
            logger.debug(f"Column overlap strategy failed: {e}")

        if join_paths and any((jp.get("verified_row_count") or 0) > 0 for jp in join_paths):
            return {
                "success": True,
                "join_paths": join_paths,
                "strategies_tried": strategies_tried,
                "error": None,
            }

        # ── Strategy 4: Value pattern matching (compound/composite IDs) ──
        # Some schemas encode a foreign key inside a composite string column
        # (e.g. "123_<something>"). Candidate text columns on the source table
        # are discovered dynamically from information_schema -- no column names
        # are hard-coded, so this works against any database.
        strategies_tried.append("pattern_match")
        try:
            comp_cols_sql = (
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_name = '{src}' AND table_schema = 'public' "
                "AND data_type IN ('character varying', 'varchar', 'text', "
                "'char', 'character') LIMIT 25"
            )
            comp_cols_r = _execute_safe(session_id, comp_cols_sql)
            compound_cols = [
                r.get("column_name")
                for r in comp_cols_r.get("records", [])
                if comp_cols_r.get("success") and r.get("column_name")
            ]
            for comp_col in compound_cols:
                try:
                    sample_sql = (
                        f"SELECT DISTINCT {comp_col} FROM {src} "
                        f"WHERE {comp_col} IS NOT NULL LIMIT 10"
                    )
                    sample_r = _execute_safe(session_id, sample_sql)
                    if not sample_r.get("success") or not sample_r.get("records"):
                        continue

                    sample_values = [
                        r.get(comp_col, "") for r in sample_r["records"] if r.get(comp_col)
                    ]
                    if not sample_values:
                        continue

                    # Detect compound ID patterns (e.g., "123_abc.def")
                    ip_pattern = re.compile(r'_(\d{1,3}\.\d{1,3}\.\d{1,3}\.\s*\d{1,3})$')
                    has_ip = any(ip_pattern.search(v) for v in sample_values)

                    if has_ip:
                        # Look for IP column in target table
                        ip_cols_sql = f"""
                            SELECT column_name FROM information_schema.columns
                            WHERE table_name = '{tgt}' AND table_schema = 'public'
                            AND (column_name ILIKE '%%address%%'
                                 OR column_name ILIKE '%%ip%%')
                        """
                        ip_cols_r = _execute_safe(session_id, ip_cols_sql)
                        if ip_cols_r.get("success"):
                            for ip_row in ip_cols_r.get("records", []):
                                ip_col = ip_row.get("column_name", "")
                                if not ip_col:
                                    continue
                                # Build ILIKE-based join condition
                                join_cond = (
                                    f"{src}.{comp_col} ILIKE "
                                    f"CONCAT('%%', TRIM({tgt}.{ip_col}), '%%')"
                                )
                                # Verify
                                try:
                                    verify_sql = (
                                        f"SELECT COUNT(*) as cnt FROM {src} "
                                        f"JOIN {tgt} ON {join_cond} LIMIT 1"
                                    )
                                    vr = _execute_safe(session_id, verify_sql)
                                    if vr.get("success") and vr.get("records"):
                                        cnt = vr["records"][0].get("cnt") or 0
                                        if cnt > 0:
                                            join_paths.append({
                                                "join_condition": join_cond,
                                                "method": "pattern_match",
                                                "confidence": "medium",
                                                "description": (
                                                    f"Compound ID pattern: {comp_col} "
                                                    f"contains {tgt}.{ip_col} (IP-based)"
                                                ),
                                                "verified_row_count": cnt,
                                            })
                                except Exception:
                                    pass

                    # Also check numeric ID prefix pattern (e.g., "440440_...")
                    id_pattern = re.compile(r'^(\d+)_')
                    has_id_prefix = any(id_pattern.match(v) for v in sample_values)

                    if has_id_prefix:
                        # Check if target has a matching numeric ID column
                        id_cols = [
                            f"{tgt}_id", "id",
                        ]
                        for id_col in id_cols:
                            try:
                                # Build CAST-based join
                                join_cond = (
                                    f"CAST(SPLIT_PART({src}.{comp_col}, '_', 1) AS NUMERIC) "
                                    f"= {tgt}.{id_col}"
                                )
                                verify_sql = (
                                    f"SELECT COUNT(*) as cnt FROM {src} "
                                    f"JOIN {tgt} ON {join_cond} LIMIT 1"
                                )
                                vr = _execute_safe(session_id, verify_sql)
                                if vr.get("success") and vr.get("records"):
                                    cnt = vr["records"][0].get("cnt") or 0
                                    if cnt > 0:
                                        join_paths.append({
                                            "join_condition": join_cond,
                                            "method": "pattern_match",
                                            "confidence": "medium",
                                            "description": (
                                                f"Numeric prefix: SPLIT_PART({comp_col},'_',1) "
                                                f"matches {tgt}.{id_col}"
                                            ),
                                            "verified_row_count": cnt,
                                        })
                            except Exception:
                                continue

                except Exception:
                    continue

        except Exception as e:
            logger.debug(f"Pattern match strategy failed: {e}")

        # Return best results
        if join_paths:
            # Sort by verified_row_count descending, then confidence
            conf_order = {"high": 0, "medium": 1, "low": 2}
            join_paths.sort(
                key=lambda x: (
                    -(x.get("verified_row_count") or 0) if x.get("verified_row_count") is not None else 0,
                    conf_order.get(x.get("confidence", "low"), 2),
                )
            )

        return {
            "success": len(join_paths) > 0,
            "join_paths": join_paths,
            "strategies_tried": strategies_tried,
            "error": None if join_paths else "No viable join path found between tables",
        }

    except Exception as e:
        logger.error(f"discover_join_paths failed: {e}")
        return {
            "success": False,
            "join_paths": [],
            "strategies_tried": [],
            "error": str(e),
        }


# Tool metadata for MCP registration
TOOL_METADATA = {
    "name": "discover_join_paths",
    "description": (
        "Discover how to JOIN two tables when standard FK relationships "
        "don't work (return 0 rows). Tries multiple strategies: known "
        "registry, FK convention, column overlap, and value pattern matching. "
        "Use this when a JOIN query returns 0 rows unexpectedly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Database session ID from connection",
            },
            "source_table": {
                "type": "string",
                "description": "Source table name for the JOIN",
            },
            "target_table": {
                "type": "string",
                "description": "Target table name for the JOIN",
            },
            "context": {
                "type": "string",
                "description": "User query context to help with matching (optional)",
            },
        },
        "required": ["session_id", "source_table", "target_table"],
    },
}
