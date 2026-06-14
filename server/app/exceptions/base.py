"""
Base Exception Classes
Foundation for all custom exceptions in the application
"""

from enum import Enum
from typing import Optional, Dict, Any
from datetime import datetime


class ErrorSeverity(str, Enum):
    """Severity levels for errors"""
    LOW = "low"           # Minor issues, can be ignored
    MEDIUM = "medium"     # Notable issues, should be addressed
    HIGH = "high"         # Serious issues, needs attention
    CRITICAL = "critical" # Critical failures, immediate action required


class ErrorCode(str, Enum):
    """Standardized error codes for the application"""
    
    # General errors (1000-1099)
    UNKNOWN_ERROR = "E1000"
    INTERNAL_ERROR = "E1001"
    SERVICE_UNAVAILABLE = "E1002"
    TIMEOUT = "E1003"
    
    # Database errors (2000-2099)
    DATABASE_ERROR = "E2000"
    CONNECTION_FAILED = "E2001"
    QUERY_FAILED = "E2002"
    SESSION_NOT_FOUND = "E2003"
    INVALID_CREDENTIALS = "E2004"
    DATABASE_TIMEOUT = "E2005"
    CONNECTION_POOL_EXHAUSTED = "E2006"
    INVALID_SQL = "E2007"
    PERMISSION_DENIED = "E2008"
    
    # AI/LLM errors (3000-3099)
    AI_ERROR = "E3000"
    OPENAI_ERROR = "E3001"
    TOKEN_LIMIT_EXCEEDED = "E3002"
    INVALID_PROMPT = "E3003"
    AI_SERVICE_UNAVAILABLE = "E3004"
    QUERY_GENERATION_FAILED = "E3005"
    MODEL_NOT_FOUND = "E3006"
    
    # Validation errors (4000-4099)
    VALIDATION_ERROR = "E4000"
    INVALID_INPUT = "E4001"
    MISSING_REQUIRED_FIELD = "E4002"
    INVALID_FORMAT = "E4003"
    VALUE_OUT_OF_RANGE = "E4004"
    
    # Authentication errors (5000-5099)
    AUTH_ERROR = "E5000"
    UNAUTHORIZED = "E5001"
    FORBIDDEN = "E5002"
    TOKEN_EXPIRED = "E5003"
    INVALID_TOKEN = "E5004"
    
    # Rate limiting errors (6000-6099)
    RATE_LIMIT_ERROR = "E6000"
    TOO_MANY_REQUESTS = "E6001"


class AppException(Exception):
    """
    Base exception class for all application exceptions.
    
    Provides:
    - Standardized error codes
    - Severity levels
    - HTTP status codes
    - Detailed error information
    - Context/metadata support
    - Timestamp tracking
    """
    
    def __init__(
        self,
        message: str,
        error_code: ErrorCode = ErrorCode.UNKNOWN_ERROR,
        status_code: int = 500,
        severity: ErrorSeverity = ErrorSeverity.MEDIUM,
        details: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        cause: Optional[Exception] = None,
    ):
        """
        Initialize the exception.
        
        Args:
            message: Human-readable error message
            error_code: Standardized error code
            status_code: HTTP status code to return
            severity: Error severity level
            details: Additional details about the error
            context: Additional context/metadata
            cause: The original exception that caused this one
        """
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.severity = severity
        self.details = details
        self.context = context or {}
        self.cause = cause
        self.timestamp = datetime.utcnow().isoformat()
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert exception to dictionary for JSON response.
        
        Response structure is designed to be compatible with the Angular UI.
        Provides both simple 'error' string and detailed 'error_details' object.
        """
        result = {
            "success": False,
            # Top-level 'error' as string for UI compatibility
            "error": self.message,
            # Detailed error object for new UI features
            "error_details": {
                "code": self.error_code.value,
                "message": self.message,
                "severity": self.severity.value,
                "timestamp": self.timestamp,
            }
        }
        
        if self.details:
            result["error_details"]["details"] = self.details
            result["detail"] = self.details  # Also at top level for compatibility
        
        if self.context:
            result["error_details"]["context"] = self.context
        
        return result
    
    def __str__(self) -> str:
        return f"[{self.error_code.value}] {self.message}"
    
    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"message='{self.message}', "
            f"error_code={self.error_code}, "
            f"status_code={self.status_code}, "
            f"severity={self.severity})"
        )
