"""
Analyze Connection Performance Tool

Assess server capabilities and recommend query optimisation strategies
based on live database statistics: table sizes, buffer hit ratio,
memory settings, and connection latency.
"""

import logging
import time
from typing import Any, Dict, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Injected by MCP server during initialisation
_db_service = None


def set_database_service(db_service) -> None:
    """Set the database service instance."""
    global _db_service
    _db_service = db_service


def analyze_connection_performance(
    session_id: str,
    query_complexity: str = "complex",
    expected_data_volume: str = "medium",
) -> Dict[str, Any]:
    """
    Assess server capabilities and return query-optimisation recommendations.

    Args:
        session_id: Database session ID from connection.
        query_complexity: One of 'simple', 'complex', 'analytical'.
        expected_data_volume: One of 'small' (<1K rows), 'medium' (1K-100K), 'large' (>100K).

    Returns:
        {
            "success": bool,
            "latency_ms": float,
            "buffer_hit_ratio": float | null,
            "work_mem": str | null,
            "effective_cache_size": str | null,
            "table_stats": [{...}],
            "recommendations": {
                "recommended_limit": int,
                "join_strategy": str,
                "aggregation_approach": str,
                "use_ctes": bool,
                "parallel_safe": bool,
                "tips": [str],
            },
            "error": str | null
        }
    """
    if not _db_service:
        return {"success": False, "error": "Database service not initialized"}
    if not session_id:
        return {"success": False, "error": "session_id is required"}

    session = _db_service.get_session(session_id)
    if not session:
        return {"success": False, "error": "Invalid or expired database session"}

    db_type = getattr(session, "db_type", "postgresql")

    result: Dict[str, Any] = {
        "success": True,
        "latency_ms": None,
        "buffer_hit_ratio": None,
        "work_mem": None,
        "effective_cache_size": None,
        "table_stats": [],
        "recommendations": {},
        "error": None,
    }

    # ── Measure latency ──────────────────────────────────────────
    try:
        with session.engine.connect() as conn:
            t0 = time.perf_counter()
            conn.execute(text("SELECT 1"))
            result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    except Exception:
        pass

    # ── PostgreSQL-specific stats ────────────────────────────────
    if db_type == "postgresql":
        try:
            with session.engine.connect() as conn:
                # Memory settings
                for setting in ("work_mem", "effective_cache_size"):
                    try:
                        val = conn.execute(
                            text("SELECT current_setting(:s)"),
                            {"s": setting},
                        ).scalar()
                        result[setting] = str(val)
                    except Exception:
                        pass

                # Buffer hit ratio from pg_stat_bgwriter
                try:
                    row = conn.execute(text(
                        "SELECT "
                        "  CASE WHEN (buffers_checkpoint + buffers_clean + buffers_backend) > 0 "
                        "       THEN ROUND(buffers_checkpoint::numeric / "
                        "            (buffers_checkpoint + buffers_clean + buffers_backend) * 100, 2) "
                        "       ELSE NULL END AS hit_ratio "
                        "FROM pg_stat_bgwriter"
                    )).fetchone()
                    if row and row[0] is not None:
                        result["buffer_hit_ratio"] = float(row[0])
                except Exception:
                    # Try the simpler blks_hit / blks_read ratio from pg_stat_database
                    try:
                        db_name = getattr(session, "database", "")
                        row = conn.execute(text(
                            "SELECT CASE WHEN blks_hit + blks_read > 0 "
                            "       THEN ROUND(blks_hit::numeric / (blks_hit + blks_read) * 100, 2) "
                            "       ELSE NULL END "
                            "FROM pg_stat_database WHERE datname = current_database()"
                        )).fetchone()
                        if row and row[0] is not None:
                            result["buffer_hit_ratio"] = float(row[0])
                    except Exception:
                        pass

                # Top tables by size — useful for JOIN strategy advice
                try:
                    rows = conn.execute(text(
                        "SELECT "
                        "  relname AS table_name, "
                        "  n_live_tup AS live_rows, "
                        "  n_dead_tup AS dead_rows, "
                        "  seq_scan, "
                        "  idx_scan, "
                        "  pg_relation_size(relid) AS size_bytes "
                        "FROM pg_stat_user_tables "
                        "ORDER BY n_live_tup DESC "
                        "LIMIT 20"
                    )).fetchall()
                    for r in rows:
                        result["table_stats"].append({
                            "table_name": r[0],
                            "live_rows": r[1],
                            "dead_rows": r[2],
                            "seq_scan": r[3],
                            "idx_scan": r[4],
                            "size_bytes": r[5],
                        })
                except Exception as e:
                    logger.debug(f"pg_stat_user_tables query failed: {e}")

                # Index usage stats
                try:
                    idx_row = conn.execute(text(
                        "SELECT "
                        "  SUM(idx_scan) AS total_idx_scan, "
                        "  SUM(seq_scan) AS total_seq_scan "
                        "FROM pg_stat_user_tables"
                    )).fetchone()
                    if idx_row:
                        result["index_usage"] = {
                            "total_index_scans": int(idx_row[0] or 0),
                            "total_seq_scans": int(idx_row[1] or 0),
                        }
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Performance stats collection failed: {e}")

    # ── Build recommendations ────────────────────────────────────
    result["recommendations"] = _build_recommendations(
        db_type=db_type,
        query_complexity=query_complexity,
        expected_data_volume=expected_data_volume,
        work_mem=result.get("work_mem"),
        buffer_hit_ratio=result.get("buffer_hit_ratio"),
        latency_ms=result.get("latency_ms"),
        table_stats=result.get("table_stats", []),
    )

    return result


def _parse_mem_to_mb(val: Optional[str]) -> Optional[float]:
    """Parse PostgreSQL memory setting string to MB."""
    if not val:
        return None
    val = val.strip().lower()
    try:
        if val.endswith("gb"):
            return float(val[:-2]) * 1024
        if val.endswith("mb"):
            return float(val[:-2])
        if val.endswith("kb"):
            return float(val[:-2]) / 1024
        # Plain number = kB in PostgreSQL
        return float(val) / 1024
    except (ValueError, TypeError):
        return None


def _build_recommendations(
    db_type: str,
    query_complexity: str,
    expected_data_volume: str,
    work_mem: Optional[str],
    buffer_hit_ratio: Optional[float],
    latency_ms: Optional[float],
    table_stats: list,
) -> Dict[str, Any]:
    """Generate optimisation recommendations from collected stats."""

    tips: list = []

    # ── Recommended LIMIT ────────────────────────────────────────
    limit_map = {
        ("simple", "small"): 1000,
        ("simple", "medium"): 500,
        ("simple", "large"): 200,
        ("complex", "small"): 500,
        ("complex", "medium"): 200,
        ("complex", "large"): 100,
        ("analytical", "small"): 200,
        ("analytical", "medium"): 100,
        ("analytical", "large"): 50,
    }
    recommended_limit = limit_map.get(
        (query_complexity, expected_data_volume), 100
    )
    # Reduce further if high latency
    if latency_ms and latency_ms > 50:
        recommended_limit = max(10, recommended_limit // 2)
        tips.append(
            f"High latency detected ({latency_ms:.0f} ms). "
            "Consider reducing result set size."
        )

    # ── JOIN strategy ────────────────────────────────────────────
    work_mem_mb = _parse_mem_to_mb(work_mem)
    if work_mem_mb and work_mem_mb >= 256:
        join_strategy = "hash_join"
        tips.append(
            f"work_mem is {work_mem} — hash joins are efficient. "
            "Use them freely for multi-table queries."
        )
    elif work_mem_mb and work_mem_mb < 64:
        join_strategy = "nested_loop_preferred"
        tips.append(
            f"work_mem is low ({work_mem}). Prefer indexed nested-loop joins "
            "and avoid large hash joins or sorts."
        )
    else:
        join_strategy = "auto"

    # ── CTE vs subquery ──────────────────────────────────────────
    # PG < 12 materialises CTEs by default (can be slow); PG 12+ inlines them
    use_ctes = True
    if db_type == "postgresql":
        # For complex queries CTEs improve readability at minimal cost on PG 12+
        tips.append(
            "PostgreSQL 12+ inlines CTEs when possible. "
            "CTEs are recommended for readability."
        )

    # ── Aggregation approach ─────────────────────────────────────
    if expected_data_volume == "large":
        aggregation_approach = "server_side"
        tips.append(
            "Large data volume — use server-side aggregation (GROUP BY, window "
            "functions) rather than fetching raw rows."
        )
    else:
        aggregation_approach = "standard"

    # ── Parallel query ───────────────────────────────────────────
    parallel_safe = db_type == "postgresql"

    # ── Buffer hit ratio warning ─────────────────────────────────
    if buffer_hit_ratio is not None and buffer_hit_ratio < 90:
        tips.append(
            f"Buffer hit ratio is {buffer_hit_ratio}% (< 90%). "
            "Disk I/O may be a bottleneck. Minimise full table scans."
        )

    # ── Table-specific tips ──────────────────────────────────────
    for ts in table_stats[:5]:
        seq = ts.get("seq_scan", 0) or 0
        idx = ts.get("idx_scan", 0) or 0
        if seq > 0 and idx == 0 and (ts.get("live_rows", 0) or 0) > 10000:
            tips.append(
                f"Table `{ts['table_name']}` has {ts['live_rows']} rows "
                f"but only sequential scans (no index usage). "
                "Add WHERE filters to leverage indexes."
            )

    return {
        "recommended_limit": recommended_limit,
        "join_strategy": join_strategy,
        "aggregation_approach": aggregation_approach,
        "use_ctes": use_ctes,
        "parallel_safe": parallel_safe,
        "tips": tips,
    }


# ── Tool metadata for MCP registration ─────────────────────────────

TOOL_METADATA = {
    "name": "analyze_connection_performance",
    "description": (
        "Assess database server performance: latency, buffer hit ratio, table sizes, "
        "and index usage. Returns optimisation recommendations. "
        "Only call this when the user explicitly asks about performance tuning, "
        "slow queries, or server diagnostics. Do NOT call for normal queries."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Database session ID from connection",
            },
            "query_complexity": {
                "type": "string",
                "description": "Expected query complexity: 'simple', 'complex', or 'analytical'",
                "enum": ["simple", "complex", "analytical"],
                "default": "complex",
            },
            "expected_data_volume": {
                "type": "string",
                "description": "Expected result size: 'small' (<1K rows), 'medium' (1K-100K), 'large' (>100K)",
                "enum": ["small", "medium", "large"],
                "default": "medium",
            },
        },
        "required": ["session_id"],
    },
}
