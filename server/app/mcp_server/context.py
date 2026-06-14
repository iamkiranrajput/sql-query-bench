"""
Conversation Context Manager

Manages multi-turn conversation state for the MCP server.
Each chat session has its own context with:
- Turn history (user queries, intents, SQL, results)
- Current query state (tables, columns, filters, joins)
- Last successful query for refinement context

TTL-based expiry (1 hour by default).
SQLite persistence for server restarts.
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class QueryIntent(str, Enum):
    """Types of user query intents."""
    NEW_QUERY = "NEW_QUERY"
    REMOVE_COLUMN = "REMOVE_COLUMN"
    ADD_COLUMN = "ADD_COLUMN"
    MODIFY_FILTER = "MODIFY_FILTER"
    CHANGE_TIME_RANGE = "CHANGE_TIME_RANGE"
    JOIN_TABLE = "JOIN_TABLE"
    CHANGE_ORDER = "CHANGE_ORDER"
    CHANGE_LIMIT = "CHANGE_LIMIT"
    FOLLOW_UP = "FOLLOW_UP"
    CLARIFICATION_RESPONSE = "CLARIFICATION_RESPONSE"
    CONVERSATIONAL = "CONVERSATIONAL"
    UNKNOWN = "UNKNOWN"


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class QueryTurn:
    """A single turn in the conversation."""
    turn_id: str
    timestamp: str
    user_query: str
    intent: QueryIntent
    tables: List[str] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    filters: List[Dict[str, Any]] = field(default_factory=list)
    joins: List[Dict[str, Any]] = field(default_factory=list)
    sql: str = ""
    success: bool = False
    row_count: int = 0
    execution_time_ms: float = 0.0
    error: Optional[str] = None
    assistant_response: str = ""
    modifications: List[Dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "timestamp": self.timestamp,
            "user_query": self.user_query,
            "intent": self.intent.value if isinstance(self.intent, QueryIntent) else self.intent,
            "tables": self.tables,
            "columns": self.columns,
            "filters": self.filters,
            "joins": self.joins,
            "sql": self.sql,
            "success": self.success,
            "row_count": self.row_count,
            "execution_time_ms": self.execution_time_ms,
            "error": self.error,
            "assistant_response": self.assistant_response,
            "modifications": self.modifications,
        }


@dataclass
class QueryState:
    """Current query state for refinement."""
    tables: List[str] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    filters: List[Dict[str, Any]] = field(default_factory=list)
    joins: List[Dict[str, Any]] = field(default_factory=list)
    order_by: List[Dict[str, Any]] = field(default_factory=list)
    limit: Optional[int] = None
    last_sql: str = ""
    last_intent: Optional[QueryIntent] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "tables": self.tables,
            "columns": self.columns,
            "filters": self.filters,
            "joins": self.joins,
            "order_by": self.order_by,
            "limit": self.limit,
            "last_sql": self.last_sql,
            "last_intent": self.last_intent.value if self.last_intent else None,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueryState":
        state = cls()
        state.tables = data.get("tables", [])
        state.columns = data.get("columns", [])
        state.filters = data.get("filters", [])
        state.joins = data.get("joins", [])
        state.order_by = data.get("order_by", [])
        state.limit = data.get("limit")
        state.last_sql = data.get("last_sql", "")
        intent = data.get("last_intent")
        if intent:
            state.last_intent = QueryIntent(intent) if isinstance(intent, str) else intent
        return state
    
    def clear(self) -> None:
        """Reset state to empty."""
        self.tables = []
        self.columns = []
        self.filters = []
        self.joins = []
        self.order_by = []
        self.limit = None
        self.last_sql = ""
        self.last_intent = None


@dataclass
class ConversationContext:
    """
    Full context for a chat session.
    
    Contains turn history, current query state, and metadata.
    """
    session_id: str
    db_session_id: str
    created_at: str
    last_active: str
    history: List[QueryTurn] = field(default_factory=list)
    query_state: QueryState = field(default_factory=QueryState)
    custom_name: Optional[str] = None
    schema_context: Optional[Dict[str, Any]] = None  # From ERD explorer
    pending_clarification: Optional[Dict[str, Any]] = None
    db_identity: str = ""  # e.g. "host:port/database" for cross-session scoping
    
    def add_turn(self, turn: QueryTurn) -> None:
        """Add a turn to history."""
        self.history.append(turn)
        self.last_active = datetime.now(timezone.utc).isoformat()
        
        # Update query state from query turns that have SQL
        # For successful queries: update all state fields
        # For failed queries: still store last_sql so refinements can reference it
        if turn.sql:
            if turn.success:
                self.query_state.tables = turn.tables
                self.query_state.columns = turn.columns
                self.query_state.filters = turn.filters
                self.query_state.joins = turn.joins
                self.query_state.last_sql = turn.sql
                self.query_state.last_intent = turn.intent
            else:
                # Failed query — store SQL and tables so follow-ups can reference them
                self.query_state.last_sql = turn.sql
                if turn.tables:
                    self.query_state.tables = turn.tables
                self.query_state.last_intent = turn.intent
    
    def get_last_turn(self) -> Optional[QueryTurn]:
        """Get the most recent turn."""
        return self.history[-1] if self.history else None
    
    def get_last_successful_turn(self) -> Optional[QueryTurn]:
        """Get the most recent successful turn with SQL."""
        for turn in reversed(self.history):
            if turn.success and turn.sql:
                return turn
        return None
    
    def get_recent_context(self, max_turns: int = 5) -> List[Dict[str, Any]]:
        """Get recent turns for LLM context."""
        recent = self.history[-max_turns:] if len(self.history) > max_turns else self.history
        return [turn.to_dict() for turn in recent]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "db_session_id": self.db_session_id,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "turn_count": len(self.history),
            "query_state": self.query_state.to_dict(),
            "custom_name": self.custom_name,
            "has_schema_context": self.schema_context is not None,
            "pending_clarification": self.pending_clarification,
            "db_identity": self.db_identity,
        }
    
    def to_summary(self) -> Dict[str, Any]:
        """Get summary for session listing."""
        last_turn = self.get_last_turn()
        first_turn = self.history[0] if self.history else None
        
        return {
            "session_id": self.session_id,
            "db_session_id": self.db_session_id,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "turn_count": len(self.history),
            "first_query": first_turn.user_query if first_turn else None,
            "last_query": last_turn.user_query if last_turn else None,
            "last_sql": last_turn.sql if last_turn else None,
            "last_tables": self.query_state.tables,
            "custom_name": self.custom_name,
            "db_identity": self.db_identity,
        }


# ============================================================================
# Context Manager
# ============================================================================

class ConversationContextManager:
    """
    Manages conversation contexts for all chat sessions.
    
    Features:
    - SQLite persistence for server restarts
    - In-memory storage with thread-safe access
    - TTL-based automatic expiry
    - Session CRUD operations
    - Query state persistence
    """
    
    DEFAULT_TTL_SECONDS = 3600 * 24 * 7  # 7 days for persistence
    CLEANUP_INTERVAL_SECONDS = 300  # 5 minutes
    DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "sessions.db")
    
    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._sessions: Dict[str, ConversationContext] = {}
        self._lock = threading.Lock()
        self._ttl_seconds = ttl_seconds
        self._cleanup_thread: Optional[threading.Thread] = None
        self._running = False
        self._db_path = os.path.abspath(self.DB_PATH)
        self._init_db()
        self._load_from_db()
    
    def _init_db(self) -> None:
        """Initialize SQLite database."""
        try:
            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            
            # Sessions table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    db_session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_active TEXT NOT NULL,
                    custom_name TEXT,
                    query_state TEXT,
                    schema_context TEXT,
                    pending_clarification TEXT,
                    db_identity TEXT DEFAULT ''
                )
            """)
            
            # Auto-migrate: add db_identity column if missing
            cursor.execute("PRAGMA table_info(sessions)")
            columns = [col[1] for col in cursor.fetchall()]
            if "db_identity" not in columns:
                cursor.execute("ALTER TABLE sessions ADD COLUMN db_identity TEXT DEFAULT ''")
                logger.info("Migrated sessions: added db_identity column")
            
            # Turns table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    user_query TEXT NOT NULL,
                    intent TEXT,
                    tables TEXT,
                    columns TEXT,
                    filters TEXT,
                    joins TEXT,
                    sql TEXT,
                    success INTEGER,
                    row_count INTEGER,
                    execution_time_ms REAL,
                    error TEXT,
                    assistant_response TEXT,
                    modifications TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                )
            """)
            
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id)")
            
            conn.commit()
            conn.close()
            logger.info(f"Session database initialized at {self._db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize session database: {e}")
    
    def _load_from_db(self) -> None:
        """Load sessions from SQLite on startup."""
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            
            # Load sessions
            cursor.execute("SELECT * FROM sessions")
            rows = cursor.fetchall()
            
            for row in rows:
                # Handle both old (8-col) and new (9-col) schema
                session_id = row[0]
                db_session_id = row[1]
                created_at = row[2]
                last_active = row[3]
                custom_name = row[4]
                query_state = row[5]
                schema_context = row[6]
                pending_clarification = row[7]
                db_identity = row[8] if len(row) > 8 else ""
                
                ctx = ConversationContext(
                    session_id=session_id,
                    db_session_id=db_session_id,
                    created_at=created_at,
                    last_active=last_active,
                    custom_name=custom_name,
                    db_identity=db_identity or "",
                )
                
                if query_state:
                    ctx.query_state = QueryState.from_dict(json.loads(query_state))
                if schema_context:
                    ctx.schema_context = json.loads(schema_context)
                if pending_clarification:
                    ctx.pending_clarification = json.loads(pending_clarification)
                
                # Load turns for this session
                cursor.execute("SELECT * FROM turns WHERE session_id = ? ORDER BY timestamp", (session_id,))
                turn_rows = cursor.fetchall()
                
                for turn_row in turn_rows:
                    turn = QueryTurn(
                        turn_id=turn_row[0],
                        timestamp=turn_row[2],
                        user_query=turn_row[3],
                        intent=QueryIntent(turn_row[4]) if turn_row[4] else QueryIntent.UNKNOWN,
                        tables=json.loads(turn_row[5]) if turn_row[5] else [],
                        columns=json.loads(turn_row[6]) if turn_row[6] else [],
                        filters=json.loads(turn_row[7]) if turn_row[7] else [],
                        joins=json.loads(turn_row[8]) if turn_row[8] else [],
                        sql=turn_row[9] or "",
                        success=bool(turn_row[10]),
                        row_count=turn_row[11] or 0,
                        execution_time_ms=turn_row[12] or 0.0,
                        error=turn_row[13],
                        assistant_response=turn_row[14] or "",
                        modifications=json.loads(turn_row[15]) if turn_row[15] else [],
                    )
                    ctx.history.append(turn)
                
                self._sessions[session_id] = ctx
            
            conn.close()
            print(f"[CONTEXT_MGR] Loaded {len(self._sessions)} sessions from database")
            logger.info(f"Loaded {len(self._sessions)} sessions from database")
        except Exception as e:
            print(f"[CONTEXT_MGR] ERROR loading sessions: {e}")
            logger.error(f"Failed to load sessions from database: {e}")
    
    def _save_session(self, ctx: ConversationContext) -> None:
        """Save a session to SQLite."""
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT OR REPLACE INTO sessions 
                (session_id, db_session_id, created_at, last_active, custom_name, query_state, schema_context, pending_clarification, db_identity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ctx.session_id,
                ctx.db_session_id,
                ctx.created_at,
                ctx.last_active,
                ctx.custom_name,
                json.dumps(ctx.query_state.to_dict()) if ctx.query_state else None,
                json.dumps(ctx.schema_context) if ctx.schema_context else None,
                json.dumps(ctx.pending_clarification) if ctx.pending_clarification else None,
                ctx.db_identity or "",
            ))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to save session {ctx.session_id}: {e}")
    
    def _save_turn(self, session_id: str, turn: QueryTurn) -> None:
        """Save a turn to SQLite."""
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT OR REPLACE INTO turns 
                (turn_id, session_id, timestamp, user_query, intent, tables, columns, filters, joins, sql, success, row_count, execution_time_ms, error, assistant_response, modifications)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                turn.turn_id,
                session_id,
                turn.timestamp,
                turn.user_query,
                turn.intent.value if isinstance(turn.intent, QueryIntent) else turn.intent,
                json.dumps(turn.tables),
                json.dumps(turn.columns),
                json.dumps(turn.filters),
                json.dumps(turn.joins),
                turn.sql,
                1 if turn.success else 0,
                turn.row_count,
                turn.execution_time_ms,
                turn.error,
                turn.assistant_response,
                json.dumps(turn.modifications),
            ))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to save turn {turn.turn_id}: {e}")
    
    def _delete_session_from_db(self, session_id: str) -> None:
        """Delete a session from SQLite."""
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM turns WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to delete session {session_id} from database: {e}")
    
    def start(self) -> None:
        """Start the background cleanup thread."""
        if self._running:
            return
        
        self._running = True
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            daemon=True,
            name="context-cleanup"
        )
        self._cleanup_thread.start()
        logger.debug("Context manager cleanup thread started")
    
    def stop(self) -> None:
        """Stop the background cleanup thread."""
        self._running = False
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=2.0)
            self._cleanup_thread = None
        logger.debug("Context manager cleanup thread stopped")
    
    def _cleanup_loop(self) -> None:
        """Background loop to clean up expired sessions."""
        while self._running:
            try:
                self._cleanup_expired()
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
            time.sleep(self.CLEANUP_INTERVAL_SECONDS)
    
    def _cleanup_expired(self) -> None:
        """Remove expired sessions."""
        now = datetime.now(timezone.utc)
        expired = []
        
        with self._lock:
            for session_id, ctx in self._sessions.items():
                try:
                    last_active = datetime.fromisoformat(
                        ctx.last_active.replace("Z", "+00:00")
                    )
                    if (now - last_active).total_seconds() > self._ttl_seconds:
                        expired.append(session_id)
                except Exception:
                    pass
            
            for session_id in expired:
                del self._sessions[session_id]
                self._delete_session_from_db(session_id)
        
        if expired:
            logger.debug(f"Cleaned up {len(expired)} expired sessions")
    
    def create_session(self, db_session_id: str, db_identity: str = "") -> ConversationContext:
        """Create a new conversation session."""
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        
        ctx = ConversationContext(
            session_id=session_id,
            db_session_id=db_session_id,
            created_at=now,
            last_active=now,
            db_identity=db_identity,
        )
        
        with self._lock:
            self._sessions[session_id] = ctx
        
        # Persist to SQLite
        self._save_session(ctx)
        
        logger.debug(f"Created session: {session_id} (db_identity={db_identity})")
        return ctx
    
    def get_session(self, session_id: str) -> Optional[ConversationContext]:
        """Get a session by ID."""
        with self._lock:
            ctx = self._sessions.get(session_id)
            if ctx:
                # Touch to keep alive
                ctx.last_active = datetime.now(timezone.utc).isoformat()
                # Persist update
                self._save_session(ctx)
                print(f"[CONTEXT_MGR] get_session {session_id[:8]}... found, history={len(ctx.history)} turns")
            else:
                print(f"[CONTEXT_MGR] get_session {session_id[:8]}... NOT FOUND")
            return ctx
    
    def delete_session(self, session_id: str) -> bool:
        """Delete a session."""
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                self._delete_session_from_db(session_id)
                logger.debug(f"Deleted session: {session_id}")
                return True
            return False
    
    def list_sessions(
        self, 
        db_session_id: Optional[str] = None,
        limit: int = 200,
        db_identity: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List all sessions, optionally filtered by db_session_id or db_identity."""
        with self._lock:
            sessions = list(self._sessions.values())
        
        logger.debug(f"list_sessions: total={len(sessions)}, db_session_id={db_session_id}, db_identity={db_identity}")
        
        if db_session_id:
            sessions = [s for s in sessions if s.db_session_id == db_session_id]
        
        if db_identity:
            sessions = [s for s in sessions if s.db_identity == db_identity]
        
        logger.debug(f"list_sessions: after filtering={len(sessions)}")
        
        # Sort by last_active descending
        sessions.sort(key=lambda s: s.last_active, reverse=True)
        
        return [s.to_summary() for s in sessions[:limit]]
    
    def clear_all_sessions(self) -> int:
        """Delete all sessions."""
        with self._lock:
            count = len(self._sessions)
            # Delete from DB
            try:
                conn = sqlite3.connect(self._db_path)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM turns")
                cursor.execute("DELETE FROM sessions")
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"Failed to clear database: {e}")
            self._sessions.clear()
        logger.info(f"Cleared all {count} sessions")
        return count
    
    def rename_session(self, session_id: str, new_name: str) -> bool:
        """Rename a session."""
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].custom_name = new_name
                self._save_session(self._sessions[session_id])
                return True
            return False
    
    def add_turn(
        self,
        session_id: str,
        user_query: str,
        intent: QueryIntent,
        tables: List[str] = None,
        columns: List[str] = None,
        filters: List[Dict[str, Any]] = None,
        joins: List[Dict[str, Any]] = None,
        sql: str = "",
        success: bool = False,
        row_count: int = 0,
        execution_time_ms: float = 0.0,
        error: Optional[str] = None,
        assistant_response: str = "",
        modifications: List[Dict[str, Any]] = None,
    ) -> Optional[QueryTurn]:
        """Add a turn to a session."""
        print(f"[CONTEXT_MGR] add_turn called for session {session_id[:8]}...")
        ctx = self.get_session(session_id)
        if not ctx:
            print(f"[CONTEXT_MGR] Session NOT FOUND: {session_id}")
            logger.warning(f"Session not found: {session_id}")
            return None
        
        print(f"[CONTEXT_MGR] Session found, current history length: {len(ctx.history)}")
        
        turn = QueryTurn(
            turn_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            user_query=user_query,
            intent=intent,
            tables=tables or [],
            columns=columns or [],
            filters=filters or [],
            joins=joins or [],
            sql=sql,
            success=success,
            row_count=row_count,
            execution_time_ms=execution_time_ms,
            error=error,
            assistant_response=assistant_response,
            modifications=modifications or [],
        )
        
        ctx.add_turn(turn)
        # Persist turn and session update
        self._save_turn(session_id, turn)
        self._save_session(ctx)
        return turn
    
    def get_query_state(self, session_id: str) -> Optional[QueryState]:
        """Get current query state for a session."""
        ctx = self.get_session(session_id)
        return ctx.query_state if ctx else None
    
    def set_query_state(self, session_id: str, state_dict: Dict[str, Any]) -> bool:
        """Set query state from a dictionary."""
        ctx = self.get_session(session_id)
        if not ctx:
            return False
        
        ctx.query_state = QueryState.from_dict(state_dict)
        self._save_session(ctx)
        return True
    
    def clear_query_state(self, session_id: str) -> bool:
        """Clear query state (for reset operations)."""
        ctx = self.get_session(session_id)
        if not ctx:
            return False
        
        ctx.query_state.clear()
        self._save_session(ctx)
        return True
    
    def set_schema_context(
        self, 
        session_id: str, 
        schema_context: Dict[str, Any]
    ) -> bool:
        """Set schema context from ERD explorer."""
        ctx = self.get_session(session_id)
        if not ctx:
            return False
        
        ctx.schema_context = schema_context
        self._save_session(ctx)
        return True
    
    def set_pending_clarification(
        self,
        session_id: str,
        clarification: Optional[Dict[str, Any]]
    ) -> bool:
        """Set or clear pending clarification."""
        ctx = self.get_session(session_id)
        if not ctx:
            return False
        
        ctx.pending_clarification = clarification
        self._save_session(ctx)
        return True
    
    def get_history(
        self, 
        session_id: str, 
        max_turns: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Get conversation history."""
        ctx = self.get_session(session_id)
        if not ctx:
            return []
        
        history = ctx.history
        if max_turns:
            history = history[-max_turns:]
        
        return [turn.to_dict() for turn in history]
    
    def get_context_for_llm(self, session_id: str) -> Dict[str, Any]:
        """
        Get context formatted for LLM consumption.
        
        Includes recent history, current state, and schema context.
        """
        print(f"[CONTEXT_MGR] get_context_for_llm for session {session_id[:8]}...")
        ctx = self.get_session(session_id)
        if not ctx:
            print(f"[CONTEXT_MGR] Session NOT FOUND for context")
            return {}
        
        print(f"[CONTEXT_MGR] Session has {len(ctx.history)} turns")
        last_turn = ctx.get_last_successful_turn()
        
        # Build a short summary from recent history (up to 15 turns for richer context)
        summary_parts = []
        for turn in ctx.get_recent_context(max_turns=15):
            q = turn.get("user_query", "")
            sql = turn.get("sql", "")
            assistant_resp = turn.get("assistant_response", "")
            if q:
                summary_parts.append(f"Q: {q}")
            if sql:
                summary_parts.append(f"SQL: {sql[:200]}")
            if assistant_resp:
                summary_parts.append(f"A: {assistant_resp[:150]}")
        summary = "\n".join(summary_parts) if summary_parts else "No conversation history."

        return {
            "session_id": session_id,
            "has_history": len(ctx.history) > 0,
            "turn_count": len(ctx.history),
            "summary": summary,
            "recent_history": ctx.get_recent_context(max_turns=15),
            "current_state": ctx.query_state.to_dict(),
            "query_state": {
                "table": ctx.query_state.tables[0] if ctx.query_state.tables else None,
                "columns": ctx.query_state.columns,
                "filters": {f.get("column", f.get("field", "?")): f.get("value", "") for f in ctx.query_state.filters} if ctx.query_state.filters else {},
                "order_by": {"column": ctx.query_state.order_by[0].get("column", ""), "direction": ctx.query_state.order_by[0].get("direction", "ASC")} if ctx.query_state.order_by else None,
                "limit": ctx.query_state.limit,
            },
            "last_sql": last_turn.sql if last_turn else None,
            "last_tables": ctx.query_state.tables,
            "last_columns": ctx.query_state.columns,
            "last_filters": ctx.query_state.filters,
            "last_joins": ctx.query_state.joins,
            "schema_context": ctx.schema_context,
            "pending_clarification": ctx.pending_clarification,
        }


# ============================================================================
# Singleton Instance
# ============================================================================

_context_manager: Optional[ConversationContextManager] = None
_manager_lock = threading.Lock()


def get_context_manager() -> ConversationContextManager:
    """Get or create the singleton context manager."""
    global _context_manager
    
    with _manager_lock:
        if _context_manager is None:
            _context_manager = ConversationContextManager()
            _context_manager.start()
        return _context_manager


# Alias for easier imports
context_manager = property(lambda self: get_context_manager())
