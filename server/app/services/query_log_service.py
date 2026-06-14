"""
Query Log Service

Persists query execution logs for analytics and debugging.
Uses SQLite for persistence across server restarts.
"""

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .logger_service import setup_logger

logger = setup_logger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "query_logs.db")

# Phase 3 â€” strip single-quoted literals from stored SQL/user_query when
# `QUERY_LOG_REDACT_LITERALS=true`. Escaped-single-quote pairs (`''`) inside
# a literal are accepted. Sole purpose: keep sensitive literals (emails, IDs,
# IP addresses) out of the audit DB while still letting operators see query shape.
_SQL_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")


def _maybe_redact_literals(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    try:
        from app.config.settings import settings as _settings
        if not bool(getattr(_settings, "query_log_redact_literals", False)):
            return text
    except Exception:
        return text
    return _SQL_LITERAL_RE.sub("'***'", text)


@dataclass
class CopilotLogEntry:
    """Single copilot/MCP Agent query log entry â€” keyed by github_username."""
    timestamp: str
    github_username: str
    session_id: str
    user_query: str
    generated_sql: str
    total_time_ms: float
    phase_timings: List[Dict[str, Any]]
    success: bool
    row_count: int
    model: str
    tables_used: List[str]
    error_message: Optional[str] = None
    token_usage: Dict[str, Any] = field(default_factory=dict)


class QueryLogService:
    """
    Logs query executions for analytics.
    
    Stores in SQLite for persistence across server restarts.
    Also keeps recent entries in memory for fast access.
    """
    
    MAX_MEMORY_ENTRIES = 10000
    MAX_DB_ENTRIES = 100000
    
    def __init__(self):
        self._copilot_entries: List[CopilotLogEntry] = []
        self._lock = threading.Lock()
        self._db_path = os.path.abspath(DB_PATH)
        self._init_db()
        self._load_copilot_from_db()
        # Phase 3 â€” prune once at startup, then on a background timer.
        self._retention_stop = threading.Event()
        self._retention_thread: Optional[threading.Thread] = None
        try:
            self._apply_retention()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"Initial retention sweep failed: {e}")
        self._start_retention_scheduler()

    # ---- Phase 3 retention helpers --------------------------------
    def _retention_days(self) -> int:
        try:
            from app.config.settings import settings as _settings
            days = int(getattr(_settings, "query_log_retention_days", 30) or 0)
            return max(0, days)
        except Exception:
            return 30

    def _apply_retention(self) -> None:
        """Delete rows older than QUERY_LOG_RETENTION_DAYS from every log table.
        A value of 0 disables time-based pruning (size cap still applies)."""
        days = self._retention_days()
        if days <= 0:
            return
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        total_deleted = 0
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            for table in ("copilot_query_logs",):
                try:
                    cursor.execute(
                        f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,)
                    )
                    total_deleted += cursor.rowcount or 0
                except sqlite3.OperationalError:
                    # Table doesn't exist yet â€” ignore.
                    pass
            conn.commit()
            conn.close()
            if total_deleted:
                logger.info(
                    f"Retention sweep removed {total_deleted} query-log rows older than {days} days"
                )
        except Exception as e:
            logger.error(f"Retention sweep failed: {e}")

    def _start_retention_scheduler(self) -> None:
        if self._retention_thread is not None:
            return

        def _loop() -> None:
            # Run hourly. The Event lets shutdown wake us early.
            while not self._retention_stop.wait(3600):
                try:
                    self._apply_retention()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(f"Retention loop iteration failed: {exc}")

        self._retention_thread = threading.Thread(
            target=_loop, name="query-log-retention", daemon=True
        )
        self._retention_thread.start()

    def shutdown(self) -> None:
        """Stop the background retention thread (called on app shutdown)."""
        self._retention_stop.set()
    
    def _init_db(self) -> None:
        """Initialize SQLite database for query logs."""
        try:
            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            
            # MCP Agent (GitHub Copilot) logs table, keyed by github_username
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS copilot_query_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    github_username TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    user_query TEXT NOT NULL,
                    generated_sql TEXT,
                    total_time_ms REAL,
                    phase_timings TEXT,
                    success INTEGER,
                    row_count INTEGER,
                    model TEXT,
                    tables_used TEXT,
                    error_message TEXT,
                    token_usage TEXT
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_copilot_username ON copilot_query_logs(github_username)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_copilot_timestamp ON copilot_query_logs(timestamp)")

            conn.commit()
            conn.close()
            logger.info(f"Query log database initialized at {self._db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize query log database: {e}")
    
    # -- Cost Calculation --------------------------------------------

    @staticmethod
    def _calculate_estimated_cost(token_usage: Dict[str, Any]) -> float:
        """Calculate estimated cost from token usage. Mirrors frontend getEstimatedCost()."""
        if not token_usage:
            return 0.0
        prompt = token_usage.get("prompt_tokens", 0) or 0
        completion = token_usage.get("completion_tokens", 0) or 0
        if prompt + completion == 0:
            return 0.0

        # Pricing per 1M tokens: [input, output]
        # ORDER MATTERS: more-specific keys must appear before shorter prefixes
        pricing = {
            # OpenAI â€“ specific first
            "gpt-5.5":              [5.00,  30.00],
            "gpt-5.4-mini":         [0.75,  4.50],
            "gpt-5.4-nano":         [0.20,  1.25],
            "gpt-5.4":              [2.50,  15.00],
            "gpt-5-nano":           [0.20,  1.25],
            "gpt-4.1-mini":         [0.40,  1.60],
            "gpt-4.1-nano":         [0.10,  0.40],
            "gpt-4.1":              [2.00,  8.00],
            "gpt-4o-mini":          [0.15,  0.60],
            "gpt-4o":               [2.50,  10.00],
            "gpt-4-turbo":          [10.00, 30.00],
            "gpt-4":                [30.00, 60.00],
            "gpt-3.5-turbo":        [0.50,  1.50],
            "o4-mini":              [1.10,  4.40],
            "o3-mini":              [1.10,  4.40],
            "o1-mini":              [3.00,  12.00],
            "o1":                   [15.00, 60.00],
            # Google Gemini
            "gemini-2.5-flash":     [0.30,  2.50],
            "gemini-2.5-pro":       [1.25,  10.00],
            "gemini-3.1-flash-lite":[0.25,  1.50],
            # Anthropic Claude 4.x â€“ version-specific first
            "claude-opus-4.7":      [5.00,  25.00],
            "claude-opus-4-7":      [5.00,  25.00],
            "claude-opus-4":        [15.00, 75.00],   # 4.5, 4.6
            "claude-sonnet-4":      [3.00,  15.00],
            "claude-haiku-4":       [1.00,  5.00],
            # Anthropic Claude 3.x legacy
            "claude-3-opus":        [15.00, 75.00],
            "claude-3-sonnet":      [3.00,  15.00],
            "claude-3-haiku":       [0.25,  1.25],
        }
        model = (token_usage.get("model") or "").lower()
        rates = pricing.get("gpt-4o-mini")  # default
        for key, val in pricing.items():
            if key in model:
                rates = val
                break
        input_cost = (prompt / 1_000_000) * rates[0]
        output_cost = (completion / 1_000_000) * rates[1]
        return round(input_cost + output_cost, 6)

    def recalculate_all_costs(self) -> Dict[str, Any]:
        """Recalculate estimated_cost for all stored records using current pricing."""
        updated = 0
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()

            for table in ("copilot_query_logs",):
                cursor.execute(f"SELECT id, token_usage FROM {table} WHERE token_usage IS NOT NULL")
                rows = cursor.fetchall()
                for row_id, tu_json in rows:
                    if not tu_json:
                        continue
                    try:
                        tu = json.loads(tu_json)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not tu.get("prompt_tokens") and not tu.get("completion_tokens"):
                        continue
                    new_cost = self._calculate_estimated_cost(tu)
                    if abs(new_cost - (tu.get("estimated_cost") or 0)) < 1e-8:
                        continue
                    tu["estimated_cost"] = new_cost
                    cursor.execute(
                        f"UPDATE {table} SET token_usage = ? WHERE id = ?",
                        (json.dumps(tu), row_id),
                    )
                    updated += 1

            conn.commit()
            conn.close()

            # Also update in-memory copilot entries
            for entry in self._copilot_entries:
                if entry.token_usage and (entry.token_usage.get("prompt_tokens") or entry.token_usage.get("completion_tokens")):
                    entry.token_usage["estimated_cost"] = self._calculate_estimated_cost(entry.token_usage)

            logger.info(f"Recalculated costs for {updated} records")
        except Exception as e:
            logger.error(f"Failed to recalculate costs: {e}")
            raise
        return {"updated": updated}

    # â”€â”€ Copilot / MCP Agent Log Methods â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def log_copilot_query(
        self,
        github_username: str,
        session_id: str,
        user_query: str,
        generated_sql: str,
        total_time_ms: float,
        phase_timings: List[Dict[str, Any]],
        success: bool,
        row_count: int,
        model: str = "",
        tables_used: List[str] = None,
        error_message: Optional[str] = None,
        token_usage: Dict[str, Any] = None,
    ) -> None:
        """Log a copilot/MCP Agent query (keyed by github_username)."""
        entry = CopilotLogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            github_username=github_username or "unknown",
            session_id=session_id,
            user_query=user_query,
            generated_sql=generated_sql,
            total_time_ms=total_time_ms,
            phase_timings=phase_timings or [],
            success=success,
            row_count=row_count,
            model=model,
            tables_used=tables_used or [],
            error_message=error_message,
            token_usage=token_usage or {},
        )
        status = "âœ”" if entry.success else "âœ˜"
        logger.info(
            f"[COPILOT_LOG] {status} @{github_username} â”‚ "
            f"rows={entry.row_count} â”‚ {entry.total_time_ms:.0f}ms â”‚ "
            f"model={model} â”‚ \"{entry.user_query[:80]}\""
        )
        with self._lock:
            self._copilot_entries.append(entry)
            if len(self._copilot_entries) > self.MAX_MEMORY_ENTRIES:
                self._copilot_entries = self._copilot_entries[-self.MAX_MEMORY_ENTRIES:]
        self._save_copilot_to_db(entry)

    def _save_copilot_to_db(self, entry: CopilotLogEntry) -> None:
        """Save a copilot log entry to SQLite."""
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO copilot_query_logs
                (timestamp, github_username, session_id, user_query, generated_sql,
                 total_time_ms, phase_timings, success, row_count, model,
                 tables_used, error_message, token_usage)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.timestamp,
                entry.github_username,
                entry.session_id,
                _maybe_redact_literals(entry.user_query),
                _maybe_redact_literals(entry.generated_sql),
                entry.total_time_ms,
                json.dumps(entry.phase_timings),
                1 if entry.success else 0,
                entry.row_count,
                entry.model,
                json.dumps(entry.tables_used),
                entry.error_message,
                json.dumps(entry.token_usage) if entry.token_usage else None,
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to save copilot log to database: {e}")

    def _load_copilot_from_db(self) -> None:
        """Load copilot log entries from SQLite on startup."""
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT timestamp, github_username, session_id, user_query, generated_sql,
                       total_time_ms, phase_timings, success, row_count, model,
                       tables_used, error_message, token_usage
                FROM copilot_query_logs
                ORDER BY timestamp DESC
                LIMIT ?
            """, (self.MAX_MEMORY_ENTRIES,))
            rows = cursor.fetchall()
            conn.close()

            for row in reversed(rows):
                entry = CopilotLogEntry(
                    timestamp=row[0],
                    github_username=row[1] or "",
                    session_id=row[2] or "",
                    user_query=row[3] or "",
                    generated_sql=row[4] or "",
                    total_time_ms=row[5] or 0.0,
                    phase_timings=json.loads(row[6]) if row[6] else [],
                    success=bool(row[7]),
                    row_count=row[8] or 0,
                    model=row[9] or "",
                    tables_used=json.loads(row[10]) if row[10] else [],
                    error_message=row[11],
                    token_usage=json.loads(row[12]) if row[12] else {},
                )
                self._copilot_entries.append(entry)

            count = len(self._copilot_entries)
            (logger.info if count else logger.debug)(
                f"Loaded {count} copilot log entries from database"
            )
        except Exception as e:
            logger.error(f"Failed to load copilot logs from database: {e}")

    def get_copilot_logs(
        self,
        limit: int = 100,
        github_username: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get copilot/MCP Agent query logs, optionally filtered by github_username."""
        with self._lock:
            entries = list(self._copilot_entries)

        if github_username:
            entries = [e for e in entries if e.github_username == github_username]

        entries = entries[-limit:]
        entries.reverse()

        def _format_ms(ms: float) -> str:
            if ms >= 1000:
                return f"{ms / 1000:.1f}s"
            return f"{ms:.0f}ms"

        return [
            {
                "timestamp": e.timestamp,
                "github_username": e.github_username,
                "session_id": e.session_id,
                "user_query": e.user_query,
                "generated_sql": e.generated_sql[:500] if e.generated_sql else "",
                "total_time_ms": round(e.total_time_ms, 2),
                "total_time_formatted": _format_ms(e.total_time_ms),
                "phase_timings": [
                    {
                        "phase": p.get("phase", p.get("name", "unknown")),
                        "duration_ms": round(p.get("duration_ms", p.get("time_ms", 0)), 2),
                        "duration_formatted": _format_ms(p.get("duration_ms", p.get("time_ms", 0))),
                        "metadata": p.get("metadata", {}),
                    }
                    for p in (e.phase_timings or [])
                ],
                "success": e.success,
                "row_count": e.row_count,
                "intent": "COPILOT_QUERY",
                "model": e.model,
                "error": e.error_message,
                "tables_used": e.tables_used,
                "token_usage": {
                    **(e.token_usage or {}),
                    "estimated_cost": (e.token_usage or {}).get("estimated_cost")
                        or self._calculate_estimated_cost(e.token_usage or {}),
                },
            }
            for e in entries
        ]

    def get_copilot_stats(self, github_username: Optional[str] = None) -> Dict[str, Any]:
        """Get aggregate stats for copilot logs."""
        with self._lock:
            entries = list(self._copilot_entries)
        if github_username:
            entries = [e for e in entries if e.github_username == github_username]
        if not entries:
            return {"total_queries": 0, "success_rate": 0.0, "avg_time_ms": 0.0, "total_tokens": 0, "total_cost": 0.0}
        successful = sum(1 for e in entries if e.success)
        total_time = sum(e.total_time_ms for e in entries)
        total_tokens = sum(e.token_usage.get("total_tokens", 0) for e in entries)
        total_cost = sum(e.token_usage.get("estimated_cost", 0) for e in entries)
        return {
            "total_queries": len(entries),
            "success_rate": successful / len(entries) * 100,
            "avg_time_ms": total_time / len(entries),
            "total_tokens": total_tokens,
            "total_cost": total_cost,
        }

# Singleton instance
query_log_service = QueryLogService()
