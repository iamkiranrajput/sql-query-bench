"""
Database Routes - Connection management endpoints
Enterprise-grade with performance monitoring and rate limiting
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from app.models.schemas import ConnectRequest, ConnectResponse, DisconnectRequest, DisconnectResponse
from app.services.database_service import database_service
from app.services.logger_service import setup_logger
from app.services.performance_service import performance_monitor
from app.services.cache_service import cache_manager
from app.config.rate_limits import limiter, RateLimits

logger = setup_logger(__name__)
router = APIRouter()


# User-friendly error messages for database operations
def get_db_friendly_error(error_str: str) -> str:
    """Convert database errors to user-friendly messages"""
    error_lower = error_str.lower()
    
    if "password" in error_lower or "authentication" in error_lower:
        return "Authentication Failed: Invalid username or password. Please check your credentials."
    elif "connection refused" in error_lower or "could not connect" in error_lower:
        return "Connection Refused: Unable to reach the database server. Please check the hostname and port."
    elif "timeout" in error_lower or "timed out" in error_lower:
        return "Connection Timeout: The database server took too long to respond. Please try again."
    elif "does not exist" in error_lower:
        return "Database Not Found: The specified database does not exist."
    elif "permission" in error_lower or "denied" in error_lower:
        return "Access Denied: You don't have permission to access this database."
    elif "ssl" in error_lower:
        return "SSL Error: There was a problem with the secure connection. Please check SSL settings."
    else:
        return f"Database Error: {error_str}"


class ListDatabasesRequest(BaseModel):
    """Request model for listing databases"""
    hostname: str
    port: int
    username: str
    password: str
    db_type: str = "postgresql"


class ListDatabasesResponse(BaseModel):
    """Response model for listing databases"""
    success: bool
    databases: list = []
    message: str
    error: str = None


@router.post("/connect", response_model=ConnectResponse)
@limiter.limit(RateLimits.DB_CONNECT)
def connect_database(request: Request, conn_request: ConnectRequest):
    """
    Establish connection to database
    
    - Creates new session with UUID
    - Handles special characters in credentials
    - Supports multiple database types: postgresql, mysql, mssql, oracle
    - Returns session_id for subsequent queries
    """
    with performance_monitor.track_operation("db_connect") as tracker:
        try:
            tracker.add_metadata("hostname", conn_request.hostname)
            tracker.add_metadata("database", conn_request.database)
            tracker.add_metadata("db_type", conn_request.db_type)
            
            session_id, message = database_service.create_connection(
                hostname=conn_request.hostname,
                port=conn_request.port,
                database=conn_request.database,
                username=conn_request.username,
                password=conn_request.password,
                db_type=conn_request.db_type
            )
            
            _db_identity = ''
            _session = database_service.get_session(session_id)
            if _session:
                _db_identity = _session.db_identity or ''
            
            return ConnectResponse(
                success=True,
                session_id=session_id,
                db_identity=_db_identity,
                message=message
            )
        
        except Exception as e:
            tracker.set_error(str(e))
            error_str = str(e)
            user_friendly_error = get_db_friendly_error(error_str)
            # Log sanitized error (no credentials)
            logger.error(f"Connection failed for host: {conn_request.hostname}, db: {conn_request.database}")
            return ConnectResponse(
                success=False,
                message="Connection failed",
                error=user_friendly_error
            )


@router.post("/disconnect", response_model=DisconnectResponse)
@limiter.limit(RateLimits.DB_DISCONNECT)
def disconnect_database(request: Request, disconnect_request: DisconnectRequest):
    """
    Disconnect from database and cleanup session
    
    - Closes database connection
    - Removes session from memory
    - Invalidates session cache
    """
    with performance_monitor.track_operation("db_disconnect") as tracker:
        try:
            # Invalidate caches for this session
            cache_manager.query.invalidate_session(disconnect_request.session_id)
            cache_manager.schema.invalidate_session(disconnect_request.session_id)
            
            success, message = database_service.disconnect(disconnect_request.session_id)
            
            if success:
                return DisconnectResponse(
                    success=True,
                    message=message
                )
            else:
                return DisconnectResponse(
                    success=False,
                    message=message,
                    error=message
                )
        
        except Exception as e:
            tracker.set_error(str(e))
            error_str = str(e)
            user_friendly_error = get_db_friendly_error(error_str)
            logger.error(f"Disconnect failed: {error_str}")
            return DisconnectResponse(
                success=False,
                message="Disconnect failed",
                error=user_friendly_error
            )


@router.post("/list-databases", response_model=ListDatabasesResponse)
@limiter.limit(RateLimits.DB_LIST)
def list_databases(request: Request, list_request: ListDatabasesRequest):
    """
    List all available databases on a database server
    
    - Supports PostgreSQL, MySQL, SQL Server, Oracle
    - Returns list of user-created databases
    - Excludes system databases
    """
    with performance_monitor.track_operation("list_databases") as tracker:
        try:
            databases = database_service.list_databases(
                hostname=list_request.hostname,
                port=list_request.port,
                username=list_request.username,
                password=list_request.password,
                db_type=list_request.db_type
            )
            
            tracker.add_metadata("db_count", len(databases))
            
            return ListDatabasesResponse(
                success=True,
                databases=databases,
                message=f"Found {len(databases)} database(s)"
            )
        
        except Exception as e:
            tracker.set_error(str(e))
            error_str = str(e)
            user_friendly_error = get_db_friendly_error(error_str)
            logger.error(f"List databases failed: {error_str}")
            return ListDatabasesResponse(
                success=False,
                databases=[],
                message="Failed to list databases",
                error=user_friendly_error
            )


class GetTablesRequest(BaseModel):
    """Request model for getting table names"""
    session_id: str


class GetTablesResponse(BaseModel):
    """Response model for table names"""
    success: bool
    tables: list = []
    cached: bool = False
    error: str = None


class SchemaSessionRequest(BaseModel):
    """Request model for schema explorer compatibility endpoints."""
    session_id: str


class DescribeTableRequest(BaseModel):
    """Request model for simple table descriptions."""
    session_id: str
    table_name: str


@router.post("/get-tables", response_model=GetTablesResponse)
@limiter.limit(RateLimits.DB_SCHEMA)
def get_tables(request: Request, tables_request: GetTablesRequest):
    """
    Get all table names for a session (cached for autocomplete)
    
    - Returns table names for autocomplete suggestions
    - Cached for 6 hours to avoid repeated queries
    - Cache prevents database hits for repeated requests
    """
    try:
        # Check cache first (prevents DB query)
        cached_tables = cache_manager.schema.get("tables", tables_request.session_id)
        
        if cached_tables:
            # Cache hit - return immediately without DB query
            return GetTablesResponse(
                success=True,
                tables=cached_tables,
                cached=True
            )
        
        # Get session
        session = database_service.get_session(tables_request.session_id)
        if not session:
            return GetTablesResponse(
                success=False,
                error="Session not found or expired"
            )
        
        # OPTIMIZATION: Use schema snapshot instead of querying DB directly
        # Schema snapshot is already cached (database-specific, persistent)
        # This eliminates a DB query when we already have the data
        try:
            from app.services.schema_snapshot_service import SchemaSnapshotService
            schema_snapshot_service = SchemaSnapshotService()
            port = getattr(session, 'port', 5432)
            
            # Get schema snapshot (uses cache, no DB hit if already cached)
            schema_snapshot = schema_snapshot_service.get_or_discover(
                session.engine, session.hostname, port, session.database
            )
            
            # Extract table names from snapshot (already cached, no DB query)
            tables = list(schema_snapshot.get('tables', {}).keys())
            
            # Cache the result for 6 hours (session cache for faster lookup)
            cache_manager.schema.set("tables", tables_request.session_id, response=tables, ttl=21600)
            logger.debug(f"Fetched {len(tables)} tables from schema snapshot (no DB query) for session {tables_request.session_id}")
        except Exception as e:
            # Fallback to direct DB query if schema snapshot fails
            logger.warning(f"Failed to get tables from schema snapshot, falling back to DB query: {e}")
            
            # Get tables from database (fallback)
            db_type = getattr(session, 'db_type', 'postgresql')
            
            if db_type == 'postgresql':
                query = """
                    SELECT table_name 
                    FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                """
            elif db_type == 'mysql':
                query = """
                    SELECT table_name 
                    FROM information_schema.tables 
                    WHERE table_schema = DATABASE()
                    AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                """
            else:
                query = """
                    SELECT table_name 
                    FROM information_schema.tables 
                    WHERE table_type = 'BASE TABLE'
                    ORDER BY table_name
                """
            
            from sqlalchemy import text
            with session.engine.connect() as conn:
                result = conn.execute(text(query))
                tables = [row[0] for row in result.fetchall()]
            
            # Cache the result for 6 hours
            cache_manager.schema.set("tables", tables_request.session_id, response=tables, ttl=21600)
            logger.debug(f"Fetched and cached {len(tables)} tables from DB (fallback) for session {tables_request.session_id}")
        
        return GetTablesResponse(
            success=True,
            tables=tables,
            cached=False
        )
    
    except Exception as e:
        error_str = str(e)
        logger.error(f"Get tables failed: {error_str}")
        return GetTablesResponse(
            success=False,
            error=f"Failed to get tables: {error_str}"
        )


def _schema_tables_from_snapshot(snapshot: dict) -> list:
    """Convert the cached DB snapshot into the legacy UI table shape."""
    tables = []
    for table_name, table_info in sorted((snapshot.get("tables") or {}).items()):
        columns = []
        for col_name, col_info in (table_info.get("columns") or {}).items():
            columns.append(
                {
                    "name": col_name,
                    "type": col_info.get("type", "unknown"),
                    "nullable": col_info.get("nullable", True),
                    "default": col_info.get("default"),
                    "is_pk": bool(col_info.get("pk")),
                }
            )
        tables.append(
            {
                "name": table_name,
                "columns": columns,
                "row_count": None,
                "ai_description": (
                    f"{table_name} contains {len(columns)} column"
                    f"{'' if len(columns) == 1 else 's'}."
                ),
            }
        )
    return tables


def _visual_schema_from_snapshot(snapshot: dict) -> dict:
    """Convert the cached DB snapshot into the visual schema contract."""
    foreign_keys = snapshot.get("foreign_keys") or []
    fk_by_column = {
        (fk.get("from_table"), fk.get("from_column")): fk
        for fk in foreign_keys
    }

    visual_tables = []
    total_columns = 0
    for table_name, table_info in sorted((snapshot.get("tables") or {}).items()):
        visual_columns = []
        for col_name, col_info in (table_info.get("columns") or {}).items():
            fk = fk_by_column.get((table_name, col_name))
            visual_columns.append(
                {
                    "name": col_name,
                    "data_type": col_info.get("type", "unknown"),
                    "is_nullable": bool(col_info.get("nullable", True)),
                    "is_pk": bool(col_info.get("pk")),
                    "is_fk": bool(fk),
                    "fk_target_table": fk.get("to_table") if fk else None,
                    "fk_target_column": fk.get("to_column") if fk else None,
                    "default_value": col_info.get("default"),
                }
            )
        total_columns += len(visual_columns)
        visual_tables.append(
            {
                "name": table_name,
                "columns": visual_columns,
                "row_count": None,
                "column_count": len(visual_columns),
            }
        )

    relationships = [
        {
            "from_table": fk.get("from_table"),
            "from_column": fk.get("from_column"),
            "to_table": fk.get("to_table"),
            "to_column": fk.get("to_column"),
            "relationship_type": "foreign_key",
            "confidence": 1.0,
            "method": "explicit",
        }
        for fk in foreign_keys
        if fk.get("from_table") and fk.get("from_column") and fk.get("to_table") and fk.get("to_column")
    ]

    return {
        "tables": visual_tables,
        "relationships": relationships,
        "total_tables": len(visual_tables),
        "total_columns": total_columns,
        "total_relationships": len(relationships),
    }


def _visual_schema_response(session_id: str) -> dict:
    session = database_service.get_session(session_id)
    if not session:
        return {"success": False, "error": "Session not found or expired"}

    snapshot = database_service.get_schema_snapshot(session_id)
    return {"success": True, **_visual_schema_from_snapshot(snapshot)}


@router.post("/schema-explorer")
@limiter.limit(RateLimits.DB_SCHEMA)
def get_schema_explorer(request: Request, schema_request: SchemaSessionRequest):
    """Compatibility endpoint used by the Angular Schema Explorer list view."""
    try:
        session = database_service.get_session(schema_request.session_id)
        if not session:
            return {"success": False, "error": "Session not found or expired"}

        snapshot = database_service.get_schema_snapshot(schema_request.session_id)
        tables = _schema_tables_from_snapshot(snapshot)
        return {"success": True, "tables": tables}
    except Exception as e:
        logger.error(f"Schema explorer failed: {e}")
        return {"success": False, "error": str(e)}


@router.post("/schema-visual")
@limiter.limit(RateLimits.DB_SCHEMA)
def get_schema_visual(request: Request, schema_request: SchemaSessionRequest):
    """Compatibility endpoint used by the Angular visual schema canvas."""
    try:
        return _visual_schema_response(schema_request.session_id)
    except Exception as e:
        logger.error(f"Visual schema failed: {e}")
        return {"success": False, "error": str(e)}


@router.post("/schema-visual-refresh")
@limiter.limit(RateLimits.DB_SCHEMA)
def refresh_schema_visual(request: Request, schema_request: SchemaSessionRequest):
    """Refresh-compatible visual schema endpoint.

    The current backend keeps schema in memory per database. Returning the
    latest snapshot keeps the UI contract intact without a separate FK graph
    service.
    """
    try:
        return _visual_schema_response(schema_request.session_id)
    except Exception as e:
        logger.error(f"Visual schema refresh failed: {e}")
        return {"success": False, "error": str(e)}


@router.post("/describe-table")
@limiter.limit(RateLimits.DB_SCHEMA)
def describe_table(request: Request, describe_request: DescribeTableRequest):
    """Return a concise local description for a table."""
    try:
        snapshot = database_service.get_schema_snapshot(describe_request.session_id)
        table_info = (snapshot.get("tables") or {}).get(describe_request.table_name)
        if not table_info:
            return {"success": False, "error": "Table not found"}

        columns = table_info.get("columns") or {}
        pk_columns = [name for name, info in columns.items() if info.get("pk")]
        description = (
            f"{describe_request.table_name} has {len(columns)} column"
            f"{'' if len(columns) == 1 else 's'}"
        )
        if pk_columns:
            description += f"; primary key: {', '.join(pk_columns)}"
        description += "."
        return {"success": True, "description": description}
    except Exception as e:
        logger.error(f"Describe table failed: {e}")
        return {"success": False, "error": str(e)}


@router.get("/schema-intelligence/status")
def get_schema_intelligence_status(request: Request, session_id: str):
    """Compatibility status endpoint for the schema status indicator."""
    try:
        session = database_service.get_session(session_id)
        if not session:
            return {
                "success": False,
                "status": "unavailable",
                "has_embeddings": False,
                "message": "Session not found or expired",
            }

        snapshot = database_service.get_schema_snapshot(session_id)
        tables = snapshot.get("tables") or {}
        columns_count = sum(
            len((table_info.get("columns") or {}))
            for table_info in tables.values()
        )
        return {
            "success": True,
            "status": "ready",
            "has_embeddings": False,
            "tables_count": len(tables),
            "columns_count": columns_count,
            "message": "Schema cache ready",
            "cached": bool(snapshot),
        }
    except Exception as e:
        logger.error(f"Schema intelligence status failed: {e}")
        return {
            "success": False,
            "status": "unavailable",
            "has_embeddings": False,
            "message": str(e),
        }


class PresetConnectionResponse(BaseModel):
    """Response model for preset connection"""
    success: bool
    has_preset: bool = False
    connection: dict = None
    message: str = ""


@router.get("/preset-connection", response_model=PresetConnectionResponse)
def get_preset_connection(request: Request):
    """
    Get preset database connection metadata from configuration.
    
    Returns non-secret fields only (name, type, host, port, database, username).
    Password is never sent to the client — auto-connect uses server-side injection.
    """
    from app.config.settings import settings
    
    try:
        if settings.preset_db_host and settings.preset_db_database:
            connection = {
                "name": settings.preset_db_name or settings.preset_db_database,
                "db_type": settings.preset_db_type,
                "hostname": settings.preset_db_host,
                "port": settings.preset_db_port,
                "database": settings.preset_db_database,
                "username": settings.preset_db_username,
            }
            
            return PresetConnectionResponse(
                success=True,
                has_preset=True,
                connection=connection,
                message=f"Preset connection '{connection['name']}' available"
            )
        else:
            return PresetConnectionResponse(
                success=True,
                has_preset=False,
                message="No preset connection configured. Set PRESET_DB_* variables in .env file."
            )
    
    except Exception as e:
        logger.error(f"Get preset connection failed: {str(e)}")
        return PresetConnectionResponse(
            success=False,
            has_preset=False,
            message=f"Failed to get preset connection: {str(e)}"
        )


@router.post("/preset-connect", response_model=ConnectResponse)
@limiter.limit(RateLimits.DB_CONNECT)
def connect_preset(request: Request):
    """
    Auto-connect using preset credentials (server-side only).
    
    The password never leaves the server — client calls this endpoint
    and the server injects the preset credentials internally.
    """
    from app.config.settings import settings
    
    with performance_monitor.track_operation("preset_connect") as tracker:
        try:
            if not settings.preset_db_host or not settings.preset_db_database:
                raise HTTPException(status_code=400, detail="No preset connection configured")
            
            tracker.add_metadata("hostname", settings.preset_db_host)
            tracker.add_metadata("database", settings.preset_db_database)
            
            session_id, message = database_service.create_connection(
                hostname=settings.preset_db_host,
                port=settings.preset_db_port,
                database=settings.preset_db_database,
                username=settings.preset_db_username,
                password=settings.preset_db_password,
                db_type=settings.preset_db_type
            )
            
            _db_identity = ''
            _session = database_service.get_session(session_id)
            if _session:
                _db_identity = _session.db_identity or ''
            
            return ConnectResponse(
                success=True,
                session_id=session_id,
                db_identity=_db_identity,
                message=message
            )
        
        except HTTPException:
            raise
        except Exception as e:
            tracker.set_error(str(e))
            user_friendly_error = get_db_friendly_error(str(e))
            logger.error(f"Preset connection failed for host: {settings.preset_db_host}")
            return ConnectResponse(
                success=False,
                message="Preset connection failed",
                error=user_friendly_error
            )


# Schema Generation Models
class GenerateSchemaRequest(BaseModel):
    """Request to generate database schema YAML"""
    session_id: str
    include_descriptions: bool = True
    include_samples: bool = True
    include_relationships: bool = True


class GenerateSchemaResponse(BaseModel):
    """Response with generated schema info"""
    success: bool
    message: str
    schema_path: str = None
    database_name: str = None
    tables_count: int = 0
    columns_count: int = 0
    relationships_count: int = 0
    error: str = None


@router.post("/generate-schema", response_model=GenerateSchemaResponse)
@limiter.limit(RateLimits.DB_SCHEMA)
def generate_schema(request: Request, schema_request: GenerateSchemaRequest):
    """
    Generate comprehensive schema YAML for the connected database.
    
    This will:
    1. Extract all tables, columns, types from database
    2. Generate AI descriptions for each table/column
    3. Detect relationships and foreign keys
    4. Save as YAML for AI query understanding
    5. Build RAG index for semantic search
    
    The schema is unique per database and stored in data/generated_schemas/
    """
    with performance_monitor.track_operation("generate_schema") as tracker:
        try:
            # Get session
            session = database_service.get_session(schema_request.session_id)
            if not session:
                return GenerateSchemaResponse(
                    success=False,
                    message="Invalid session",
                    error="Session not found or expired. Please reconnect."
                )
            
            tracker.add_metadata("database", session.database)
            tracker.add_metadata("db_type", session.db_type)
            
            # Import schema generator
            from app.services.ai.query_agent.schema_yaml_generator import SchemaYAMLGenerator
            from app.services.ai.query_agent import configure_pipeline
            import os
            import yaml
            import hashlib
            
            logger.info(f"Generating schema for database: {session.database}")
            
            # Generate schema with LLM descriptions enabled
            # use_llm_descriptions: When True, calls LLM to generate meaningful table/column descriptions
            # sample_rows: Number of sample rows to fetch for context (3 default)
            generator = SchemaYAMLGenerator(
                engine=session.engine,
                use_llm_descriptions=schema_request.include_descriptions,  # Enable LLM for rich descriptions
                sample_rows=3 if schema_request.include_samples else 0
            )
            schema_data = generator.generate_schema()
            
            # Count elements
            tables_count = len(schema_data.get('tables', []))
            columns_count = sum(len(t.get('columns', [])) for t in schema_data.get('tables', []))
            relationships_count = len(schema_data.get('relationships', []))
            
            # Create unique filename for this database
            db_name = session.database
            db_host = session.hostname
            schema_hash = hashlib.md5(f"{db_host}_{db_name}".encode()).hexdigest()[:8]
            
            # Save to data/generated_schemas folder
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
            schema_dir = os.path.join(base_dir, 'data', 'generated_schemas')
            os.makedirs(schema_dir, exist_ok=True)
            
            schema_file = os.path.join(schema_dir, f'{db_name}_{schema_hash}_schema.yaml')
            
            with open(schema_file, 'w', encoding='utf-8') as f:
                yaml.dump(schema_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            
            logger.info(f"Schema saved to: {schema_file}")
            
            # Configure query pipeline with this schema
            pipeline = configure_pipeline(
                engine=session.engine,
                db_type=session.db_type,
                schema_yaml_path=schema_file
            )
            
            tracker.add_metadata("tables", tables_count)
            tracker.add_metadata("columns", columns_count)
            
            return GenerateSchemaResponse(
                success=True,
                message=f"Schema generated successfully for {db_name}",
                schema_path=schema_file,
                database_name=db_name,
                tables_count=tables_count,
                columns_count=columns_count,
                relationships_count=relationships_count
            )
            
        except Exception as e:
            tracker.set_error(str(e))
            logger.error(f"Schema generation failed: {str(e)}")
            return GenerateSchemaResponse(
                success=False,
                message="Schema generation failed",
                error=str(e)
            )


@router.get("/schema-status/{session_id}")
def get_schema_status(request: Request, session_id: str):
    """
    Check if schema has been generated for this database session.
    """
    try:
        session = database_service.get_session(session_id)
        if not session:
            return {"success": False, "error": "Session not found"}
        
        import os
        import hashlib
        
        db_name = session.database
        db_host = session.hostname
        schema_hash = hashlib.md5(f"{db_host}_{db_name}".encode()).hexdigest()[:8]
        
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        schema_file = os.path.join(base_dir, 'data', 'generated_schemas', f'{db_name}_{schema_hash}_schema.yaml')
        
        if os.path.exists(schema_file):
            import yaml
            with open(schema_file, 'r', encoding='utf-8') as f:
                schema_data = yaml.safe_load(f)
            
            return {
                "success": True,
                "has_schema": True,
                "schema_path": schema_file,
                "database": db_name,
                "tables_count": len(schema_data.get('tables', [])),
                "generated_at": schema_data.get('metadata', {}).get('generated_at')
            }
        else:
            return {
                "success": True,
                "has_schema": False,
                "database": db_name,
                "message": "Schema not generated. Click 'Generate Schema' to create."
            }
            
    except Exception as e:
        logger.error(f"Schema status check failed: {str(e)}")
        return {"success": False, "error": str(e)}
