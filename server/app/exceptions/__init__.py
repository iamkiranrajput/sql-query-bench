"""
Custom Exceptions Package
Centralized exception handling for the SQL Query Tool application
"""

from app.exceptions.base import (
    AppException,
    ErrorCode,
    ErrorSeverity,
)

from app.exceptions.database import (
    DatabaseException,
    ConnectionException,
    QueryExecutionException,
    SessionNotFoundException,
    InvalidCredentialsException,
    DatabaseTimeoutException,
    ConnectionPoolExhaustedException,
)

from app.exceptions.ai import (
    AIException,
    OpenAIException,
    TokenLimitExceededException,
    InvalidPromptException,
    AIServiceUnavailableException,
    QueryGenerationException,
)

from app.exceptions.validation import (
    ValidationException,
    InvalidInputException,
    MissingRequiredFieldException,
    InvalidFormatException,
)

from app.exceptions.auth import (
    AuthException,
    UnauthorizedException,
    ForbiddenException,
    TokenExpiredException,
    InvalidTokenException,
)

from app.exceptions.handlers import (
    setup_exception_handlers,
    create_error_response,
)

from app.exceptions.query_failure import (
    QueryFailure,
)

__all__ = [
    # Base
    "AppException",
    "ErrorCode",
    "ErrorSeverity",
    # Database
    "DatabaseException",
    "ConnectionException",
    "QueryExecutionException",
    "SessionNotFoundException",
    "InvalidCredentialsException",
    "DatabaseTimeoutException",
    "ConnectionPoolExhaustedException",
    # AI
    "AIException",
    "OpenAIException",
    "TokenLimitExceededException",
    "InvalidPromptException",
    "AIServiceUnavailableException",
    "QueryGenerationException",
    # Validation
    "ValidationException",
    "InvalidInputException",
    "MissingRequiredFieldException",
    "InvalidFormatException",
    # Auth
    "AuthException",
    "UnauthorizedException",
    "ForbiddenException",
    "TokenExpiredException",
    "InvalidTokenException",
    # Handlers
    "setup_exception_handlers",
    "create_error_response",
    # Query Failure
    "QueryFailure",
]
