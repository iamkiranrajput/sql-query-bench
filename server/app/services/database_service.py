"""
Database Service

Manages database connections, session pooling, and schema introspection.
Supports PostgreSQL, MySQL, MSSQL, and Oracle.
"""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .logger_service import setup_logger

logger = setup_logger(__name__)

# Background executor for non-blocking operations
_background_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="db-bg")

# GLOBAL schema cache keyed by db_identity (host:port/database)
# This prevents re-introspection for the same database across sessions
_global_schema_cache: Dict[str, Dict[str, Any]] = {}
_schema_cache_lock = threading.Lock()


@dataclass
class DatabaseSession:
    """Represents an active database connection session."""
    session_id: str
    engine: Engine
    db_type: str
    hostname: str
    database: str
    username: str
    created_at: str
    last_used: str
    db_identity: str = ""  # e.g., "host:port/database"
    schema_snapshot: Dict[str, Any] = field(default_factory=dict)
    
    def touch(self) -> None:
        """Update last_used timestamp."""
        self.last_used = datetime.now(timezone.utc).isoformat()


class DatabaseService:
    """
    Database connection and query service.
    
    Features:
    - Multi-database support (PostgreSQL, MySQL, MSSQL, Oracle)
    - Session-based connection pooling
    - Schema introspection and caching
    - Query logging
    - TTL-based session expiry
    """
    
    SESSION_TTL_SECONDS = 3600  # 1 hour
    
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: Dict[str, DatabaseSession] = {}
    
    @property
    def sessions(self) -> Dict[str, DatabaseSession]:
        """Get all active sessions."""
        return self._sessions

    def get_active_session_count(self) -> int:
        """Return the number of currently active database sessions."""
        with self._lock:
            return len(self._sessions)
    
    def create_connection(
        self,
        hostname: str,
        port: int,
        database: str,
        username: str,
        password: str,
        db_type: str = "postgresql"
    ) -> Tuple[str, str]:
        """
        Create a new database connection.
        
        Args:
            hostname: Database host
            port: Database port
            database: Database name
            username: Username
            password: Password
            db_type: Type of database (postgresql, mysql, mssql, oracle)
        
        Returns:
            Tuple of (session_id, message)
        
        Raises:
            Exception on connection failure
        """
        # Build connection URL
        connection_url = self._build_connection_url(
            hostname, port, database, username, password, db_type
        )
        
        # Create engine with connection timeout to avoid long waits on unreachable hosts
        connect_args = {}
        if db_type == "postgresql":
            connect_args = {"connect_timeout": 5}
        elif db_type == "mysql":
            connect_args = {"connect_timeout": 5}
        elif db_type == "mssql":
            connect_args = {"timeout": 5}

        try:
            engine = create_engine(
                connection_url,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=10,
                pool_timeout=30,
                echo=False,
                connect_args=connect_args
            )
            
            # Test connection
            with engine.connect() as conn:
                if db_type == "postgresql":
                    result = conn.execute(text("SELECT version()"))
                elif db_type == "mysql":
                    result = conn.execute(text("SELECT VERSION()"))
                elif db_type == "mssql":
                    result = conn.execute(text("SELECT @@VERSION"))
                else:
                    result = conn.execute(text("SELECT 1"))
                
                version = result.scalar()
                logger.info(f"Connected to {db_type}: {version[:50] if version else 'OK'}...")
            
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            raise
        
        # Create session
        session_id = str(uuid.uuid4())
        db_identity = f"{hostname}:{port}/{database}"
        now = datetime.now(timezone.utc).isoformat()
        
        session = DatabaseSession(
            session_id=session_id,
            engine=engine,
            db_type=db_type,
            hostname=hostname,
            database=database,
            username=username,
            created_at=now,
            last_used=now,
            db_identity=db_identity
        )
        
        # Store session
        with self._lock:
            self._sessions[session_id] = session
        
        logger.info(f"Created session {session_id} for {db_identity}")
        
        # Check global schema cache - try exact match first, then partial matches
        with _schema_cache_lock:
            cached_schema = None
            
            # 1. Exact match
            if db_identity in _global_schema_cache:
                cached_schema = _global_schema_cache[db_identity]
                logger.info(f"[SCHEMA] ✅ Exact cache hit for {db_identity}")
            
            # 2. Try just database name (for pre-populated cache from schema_hints.json)
            elif database in _global_schema_cache:
                cached_schema = _global_schema_cache[database]
                # Also store under the full identity for future lookups
                _global_schema_cache[db_identity] = cached_schema
                logger.info(f"[SCHEMA] ✅ Cache hit via database name '{database}'")
            
            # 3. Fuzzy match - any key ending with same database name
            else:
                for cached_id, schema in _global_schema_cache.items():
                    if cached_id.endswith(f"/{database}"):
                        cached_schema = schema
                        _global_schema_cache[db_identity] = schema
                        logger.info(f"[SCHEMA] ✅ Cache hit via fuzzy match '{cached_id}'")
                        break
            
            if cached_schema:
                session.schema_snapshot = cached_schema
                table_count = len(cached_schema.get('tables', {}))
                logger.info(f"[SCHEMA] ✅ Reusing cached schema for {db_identity} ({table_count} tables)")
            else:
                # Trigger background schema introspection and caching
                logger.info(f"[SCHEMA] No cache for {db_identity}, triggering introspection")
                self._trigger_schema_introspection(session)
        
        return session_id, f"Connected to {database} on {hostname}"
    
    def _trigger_schema_introspection(self, session: DatabaseSession) -> None:
        """
        Trigger background schema introspection to cache schema for the session.
        This ensures schema is ready for AI queries.
        """
        def _introspect():
            try:
                logger.info(f"[SCHEMA] Starting background introspection for {session.db_identity}")
                
                # Introspect and cache schema snapshot
                inspector = inspect(session.engine)
                tables = {}
                
                for table_name in inspector.get_table_names():
                    columns = {}
                    for col in inspector.get_columns(table_name):
                        columns[col['name']] = {
                            'type': str(col['type']),
                            'nullable': col.get('nullable', True),
                            'default': str(col.get('default')) if col.get('default') else None,
                            'pk': False
                        }
                    
                    # Mark primary keys
                    pk_constraint = inspector.get_pk_constraint(table_name)
                    for pk_col in pk_constraint.get('constrained_columns', []):
                        if pk_col in columns:
                            columns[pk_col]['pk'] = True
                    
                    tables[table_name] = {'columns': columns}
                
                # Get foreign keys (handle composite FKs — all column pairs)
                foreign_keys = []
                for table_name in tables:
                    for fk in inspector.get_foreign_keys(table_name):
                        constrained = fk.get('constrained_columns', [])
                        referred = fk.get('referred_columns', [])
                        referred_table = fk.get('referred_table', '')
                        if constrained and referred and referred_table:
                            for from_col, to_col in zip(constrained, referred):
                                foreign_keys.append({
                                    'from_table': table_name,
                                    'from_column': from_col,
                                    'to_table': referred_table,
                                    'to_column': to_col
                                })
                
                # Cache it in both the session and global cache
                snapshot = {
                    'tables': tables,
                    'foreign_keys': foreign_keys
                }
                session.schema_snapshot = snapshot
                
                # Store in global cache so other sessions for the same DB can reuse it
                with _schema_cache_lock:
                    _global_schema_cache[session.db_identity] = snapshot
                
                logger.info(f"[SCHEMA] ✅ Cached schema for {session.db_identity}: "
                           f"{len(tables)} tables, {len(foreign_keys)} FKs")

                # The per-database schema_hints / db_context auto-generation
                # hooks were removed in the hackathon cleanup. The cached
                # snapshot above is enough for the MCP introspect_schema /
                # check_relationships tools to operate without static hints.

            except Exception as e:
                logger.error(f"[SCHEMA] Background introspection failed: {e}")
        
        # Run in background thread
        _background_executor.submit(_introspect)
    
    def _build_connection_url(
        self,
        hostname: str,
        port: int,
        database: str,
        username: str,
        password: str,
        db_type: str
    ) -> str:
        """Build SQLAlchemy connection URL."""
        # URL-encode password to handle special characters
        encoded_password = quote_plus(password)
        
        if db_type == "postgresql":
            return f"postgresql+psycopg2://{username}:{encoded_password}@{hostname}:{port}/{database}"
        elif db_type == "mysql":
            return f"mysql+pymysql://{username}:{encoded_password}@{hostname}:{port}/{database}"
        elif db_type == "mssql":
            return f"mssql+pyodbc://{username}:{encoded_password}@{hostname}:{port}/{database}?driver=ODBC+Driver+17+for+SQL+Server"
        elif db_type == "oracle":
            return f"oracle+cx_oracle://{username}:{encoded_password}@{hostname}:{port}/{database}"
        else:
            raise ValueError(f"Unsupported database type: {db_type}")
    
    def get_session(self, session_id: str) -> Optional[DatabaseSession]:
        """Get a session by ID."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.touch()
            else:
                logger.debug(f"[DB] Session {session_id[:8]}… not found")
            return session
    
    def disconnect(self, session_id: str) -> bool:
        """Disconnect and remove a session."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
        
        if session:
            try:
                session.engine.dispose()
                logger.info(f"Disconnected session {session_id}")
                return True
            except Exception as e:
                logger.warning(f"Error disposing engine: {e}")
                return True
        
        return False
    
    def execute_query(
        self,
        session_id: str,
        sql: str,
        max_rows: int = 10000,
        timeout_seconds: int = 120,
        params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[str], int]:
        """
        Execute a SQL query.
        
        Args:
            session_id: Session ID
            sql: SQL query to execute
            max_rows: Maximum rows to return
            timeout_seconds: Query timeout
            params: Optional bound parameters dict (uses SQLAlchemy named binds,
                e.g. ``execute_query(..., sql="SELECT :x", params={"x": 1})``).
                Always prefer this over string interpolation for user-supplied
                values to prevent SQL injection.
        
        Returns:
            Tuple of (records, columns, row_count)
        
        Raises:
            Exception on query failure
        """
        session = self.get_session(session_id)
        if not session:
            raise ValueError("Invalid or expired session")
        
        _t0 = time.perf_counter()
        logger.info(f"[DB] ▶ Executing SQL on session {session_id[:8]}… ({len(sql)} chars)")
        logger.debug(f"[DB]   SQL: {sql[:200]}{'…' if len(sql) > 200 else ''}")
        
        with session.engine.connect() as conn:
            # Set timeout if supported
            if session.db_type == "postgresql":
                try:
                    conn.execute(text(f"SET statement_timeout = {timeout_seconds * 1000}"))
                except Exception:
                    pass
            
            result = conn.execute(text(sql), params or {})
            rows = result.fetchmany(max_rows)
            columns = list(result.keys())
        
        # Convert to list of dicts
        records = []
        for row in rows:
            record = dict(zip(columns, row))
            # Convert non-serializable types
            for key, value in record.items():
                if hasattr(value, 'isoformat'):
                    record[key] = value.isoformat()
                elif isinstance(value, bytes):
                    record[key] = value.hex()
                elif value is not None and not isinstance(value, (str, int, float, bool, list, dict)):
                    record[key] = str(value)
            records.append(record)
        
        _elapsed = (time.perf_counter() - _t0) * 1000
        logger.info(f"[DB] ✔ Query returned {len(records)} rows, {len(columns)} cols in {_elapsed:.0f}ms")
        
        return records, columns, len(records)
    
    def get_schema_snapshot(self, session_id: str) -> Dict[str, Any]:
        """
        Get schema snapshot for a session.
        
        Returns cached snapshot if available (from session or global cache), or introspects schema.
        """
        session = self.get_session(session_id)
        if not session:
            return {}
        
        # Return session cached snapshot if available
        if session.schema_snapshot:
            return session.schema_snapshot
        
        # Check global cache for same database identity
        with _schema_cache_lock:
            if session.db_identity in _global_schema_cache:
                session.schema_snapshot = _global_schema_cache[session.db_identity]
                logger.info(f"[SCHEMA] ✅ Reusing global cache for {session.db_identity}")
                return session.schema_snapshot
        
        # Introspect schema
        try:
            inspector = inspect(session.engine)
            tables = {}
            
            for table_name in inspector.get_table_names():
                columns = {}
                for col in inspector.get_columns(table_name):
                    columns[col['name']] = {
                        'type': str(col['type']),
                        'nullable': col.get('nullable', True),
                        'default': str(col.get('default')) if col.get('default') else None,
                        'pk': False
                    }
                
                # Mark primary keys
                for pk_col in inspector.get_pk_constraint(table_name).get('constrained_columns', []):
                    if pk_col in columns:
                        columns[pk_col]['pk'] = True
                
                tables[table_name] = {'columns': columns}
            
            # Get foreign keys
            foreign_keys = []
            for table_name in tables:
                for fk in inspector.get_foreign_keys(table_name):
                    if fk.get('constrained_columns') and fk.get('referred_columns'):
                        foreign_keys.append({
                            'from_table': table_name,
                            'from_column': fk['constrained_columns'][0],
                            'to_table': fk['referred_table'],
                            'to_column': fk['referred_columns'][0]
                        })
            
            snapshot = {
                'tables': tables,
                'foreign_keys': foreign_keys
            }
            
            # Cache it in session and global cache
            session.schema_snapshot = snapshot
            with _schema_cache_lock:
                _global_schema_cache[session.db_identity] = snapshot
            
            logger.info(f"[SCHEMA] ✅ Introspected and cached for {session.db_identity}: "
                       f"{len(tables)} tables, {len(foreign_keys)} FKs")
            return snapshot
            
        except Exception as e:
            logger.error(f"Schema introspection failed: {e}")
            return {}
    
    def get_tables(self, session_id: str) -> List[str]:
        """Get list of table names."""
        session = self.get_session(session_id)
        if not session:
            return []
        
        try:
            inspector = inspect(session.engine)
            tables = inspector.get_table_names()
            logger.debug(f"[DB] Found {len(tables)} tables for session {session_id[:8]}…")
            return tables
        except Exception as e:
            logger.error(f"Failed to get tables: {e}")
            return []
    
    def log_query(
        self,
        session_id: str,
        username: str,
        user_prompt: str,
        generated_sql: str,
        execution_status: str,
        row_count: int = 0,
        execution_time: float = 0.0,
        error_message: Optional[str] = None
    ) -> None:
        """Log a query execution (fire-and-forget)."""
        def _log():
            try:
                # This could write to a database table or file
                logger.info(
                    f"Query Log: user={username}, status={execution_status}, "
                    f"rows={row_count}, time={execution_time:.2f}ms"
                )
            except Exception as e:
                logger.debug(f"Query logging failed: {e}")
        
        _background_executor.submit(_log)
    
    def cleanup_expired_sessions(self) -> int:
        """Remove expired sessions."""
        now = datetime.now(timezone.utc)
        expired = []
        
        with self._lock:
            for session_id, session in self._sessions.items():
                try:
                    last_used = datetime.fromisoformat(
                        session.last_used.replace("Z", "+00:00")
                    )
                    if (now - last_used).total_seconds() > self.SESSION_TTL_SECONDS:
                        expired.append(session_id)
                except Exception:
                    pass
        
        for session_id in expired:
            self.disconnect(session_id)
        
        if expired:
            logger.info(f"[DB] Cleaned up {len(expired)} expired session(s)")
        
        return len(expired)
    
    def shutdown(self) -> None:
        """Shutdown all sessions."""
        with self._lock:
            session_ids = list(self._sessions.keys())
        
        logger.info(f"[DB] Shutting down — disposing {len(session_ids)} session(s)…")
        for session_id in session_ids:
            self.disconnect(session_id)
        
        _background_executor.shutdown(wait=False)
        logger.info("[DB] ✔ Database service shutdown complete")

    def list_databases(
        self,
        hostname: str,
        port: int,
        username: str,
        password: str,
        db_type: str = "postgresql"
    ) -> List[str]:
        """
        List available databases on a server.
        
        Args:
            hostname: Database host
            port: Database port
            username: Username
            password: Password
            db_type: Type of database
            
        Returns:
            List of database names
        """
        try:
            # Connect to system database to list databases
            if db_type == "postgresql":
                connection_url = self._build_connection_url(
                    hostname, port, "postgres", username, password, db_type
                )
                query = "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname"
            elif db_type == "mysql":
                connection_url = self._build_connection_url(
                    hostname, port, "mysql", username, password, db_type
                )
                query = "SHOW DATABASES"
            elif db_type == "mssql":
                connection_url = self._build_connection_url(
                    hostname, port, "master", username, password, db_type
                )
                query = "SELECT name FROM sys.databases WHERE database_id > 4 ORDER BY name"
            else:
                logger.warning(f"list_databases not supported for {db_type}")
                return []
            
            connect_args = {}
            if db_type == "postgresql":
                connect_args = {"connect_timeout": 5}
            elif db_type == "mysql":
                connect_args = {"connect_timeout": 5}
            elif db_type == "mssql":
                connect_args = {"timeout": 5}

            engine = create_engine(connection_url, pool_pre_ping=True, connect_args=connect_args)
            
            with engine.connect() as conn:
                result = conn.execute(text(query))
                databases = [row[0] for row in result.fetchall()]
            
            engine.dispose()
            logger.info(f"Found {len(databases)} databases on {hostname}:{port}")
            return databases
            
        except Exception as e:
            logger.error(f"Failed to list databases: {e}")
            return []


# Singleton instance
database_service = DatabaseService()
