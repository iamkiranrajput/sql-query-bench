"""
Database Exceptions
Custom exceptions for database-related errors
"""

from typing import Optional, Dict, Any
from app.exceptions.base import AppException, ErrorCode, ErrorSeverity


class DatabaseException(AppException):
    """Base exception for all database-related errors"""
    
    def __init__(
        self,
        message: str = "A database error occurred",
        error_code: ErrorCode = ErrorCode.DATABASE_ERROR,
        status_code: int = 500,
        severity: ErrorSeverity = ErrorSeverity.HIGH,
        details: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        cause: Optional[Exception] = None,
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            status_code=status_code,
            severity=severity,
            details=details,
            context=context,
            cause=cause,
        )


class ConnectionException(DatabaseException):
    """Exception raised when database connection fails"""
    
    def __init__(
        self,
        message: str = "Failed to connect to database",
        hostname: Optional[str] = None,
        port: Optional[int] = None,
        database: Optional[str] = None,
        db_type: Optional[str] = None,
        details: Optional[str] = None,
        cause: Optional[Exception] = None,
    ):
        context = {}
        if hostname:
            context["hostname"] = hostname
        if port:
            context["port"] = port
        if database:
            context["database"] = database
        if db_type:
            context["db_type"] = db_type
        
        super().__init__(
            message=message,
            error_code=ErrorCode.CONNECTION_FAILED,
            status_code=503,
            severity=ErrorSeverity.HIGH,
            details=details or "Check your connection parameters and ensure the database server is running",
            context=context,
            cause=cause,
        )


class QueryExecutionException(DatabaseException):
    """Exception raised when SQL query execution fails"""
    
    def __init__(
        self,
        message: str = "Query execution failed",
        sql_query: Optional[str] = None,
        details: Optional[str] = None,
        cause: Optional[Exception] = None,
    ):
        context = {}
        if sql_query:
            # Truncate long queries for context
            context["sql_query"] = sql_query[:500] + "..." if len(sql_query) > 500 else sql_query
        
        super().__init__(
            message=message,
            error_code=ErrorCode.QUERY_FAILED,
            status_code=400,
            severity=ErrorSeverity.MEDIUM,
            details=details,
            context=context,
            cause=cause,
        )


class SessionNotFoundException(DatabaseException):
    """Exception raised when database session is not found"""
    
    def __init__(
        self,
        session_id: Optional[str] = None,
        message: str = "Database session not found",
        details: Optional[str] = None,
    ):
        context = {}
        if session_id:
            context["session_id"] = session_id
        
        super().__init__(
            message=message,
            error_code=ErrorCode.SESSION_NOT_FOUND,
            status_code=404,
            severity=ErrorSeverity.MEDIUM,
            details=details or "Please reconnect to the database",
            context=context,
        )


class InvalidCredentialsException(DatabaseException):
    """Exception raised for invalid database credentials"""
    
    def __init__(
        self,
        message: str = "Invalid database credentials",
        username: Optional[str] = None,
        details: Optional[str] = None,
        cause: Optional[Exception] = None,
    ):
        context = {}
        if username:
            context["username"] = username
        
        super().__init__(
            message=message,
            error_code=ErrorCode.INVALID_CREDENTIALS,
            status_code=401,
            severity=ErrorSeverity.HIGH,
            details=details or "Please verify your username and password",
            context=context,
            cause=cause,
        )


class DatabaseTimeoutException(DatabaseException):
    """Exception raised when database operation times out"""
    
    def __init__(
        self,
        message: str = "Database operation timed out",
        timeout_seconds: Optional[float] = None,
        operation: Optional[str] = None,
        details: Optional[str] = None,
        cause: Optional[Exception] = None,
    ):
        context = {}
        if timeout_seconds:
            context["timeout_seconds"] = timeout_seconds
        if operation:
            context["operation"] = operation
        
        super().__init__(
            message=message,
            error_code=ErrorCode.DATABASE_TIMEOUT,
            status_code=504,
            severity=ErrorSeverity.HIGH,
            details=details or "The operation took too long. Try simplifying your query.",
            context=context,
            cause=cause,
        )


class ConnectionPoolExhaustedException(DatabaseException):
    """Exception raised when connection pool is exhausted"""
    
    def __init__(
        self,
        message: str = "Database connection pool exhausted",
        pool_size: Optional[int] = None,
        active_connections: Optional[int] = None,
        details: Optional[str] = None,
    ):
        context = {}
        if pool_size:
            context["pool_size"] = pool_size
        if active_connections:
            context["active_connections"] = active_connections
        
        super().__init__(
            message=message,
            error_code=ErrorCode.CONNECTION_POOL_EXHAUSTED,
            status_code=503,
            severity=ErrorSeverity.CRITICAL,
            details=details or "Too many concurrent connections. Please try again later.",
            context=context,
        )
