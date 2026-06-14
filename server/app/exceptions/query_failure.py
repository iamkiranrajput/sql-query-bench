"""
Query Failure Exception
Raised when SQL compilation fails due to schema validation or compiler errors
"""

from typing import Optional
from app.exceptions.base import AppException, ErrorCode, ErrorSeverity


class QueryFailure(AppException):
    """
    Exception raised when SQL query compilation fails.
    
    Used by SQL compiler to signal schema validation failures,
    missing columns, invalid joins, etc.
    """
    
    def __init__(
        self,
        stage: str,
        reason: str,
        retryable: bool = False,
        error_code: Optional[str] = None,
        details: Optional[str] = None,
        context: Optional[dict] = None,
    ):
        """
        Initialize QueryFailure exception.
        
        Args:
            stage: Stage where failure occurred (e.g., "SQL_COMPILATION", "sql_join", "time_filter")
            reason: Human-readable reason for failure
            retryable: Whether the failure is retryable (transient vs permanent)
            error_code: Optional error code for programmatic handling
            details: Optional additional details
            context: Optional context dictionary
        """
        message = f"Query compilation failed at {stage}: {reason}"
        
        # Map to appropriate ErrorCode
        if error_code == "INVALID_COLUMN" or "column" in reason.lower():
            mapped_error_code = ErrorCode.VALIDATION_ERROR
        elif error_code == "NO_TIMESTAMP_COLUMN" or "timestamp" in reason.lower():
            mapped_error_code = ErrorCode.VALIDATION_ERROR
        elif "join" in reason.lower():
            mapped_error_code = ErrorCode.VALIDATION_ERROR
        else:
            mapped_error_code = ErrorCode.QUERY_GENERATION_FAILED
        
        # Build context
        failure_context = {
            "stage": stage,
            "retryable": retryable,
            "error_code": error_code,
        }
        if context:
            failure_context.update(context)
        
        super().__init__(
            message=message,
            error_code=mapped_error_code,
            status_code=422 if not retryable else 500,
            severity=ErrorSeverity.MEDIUM if retryable else ErrorSeverity.HIGH,
            details=details or reason,
            context=failure_context,
        )
        
        self.stage = stage
        self.reason = reason
        self.retryable = retryable
        # CRITICAL FIX: Store as `failure_code` to avoid overwriting parent's
        # `self.error_code` (an ErrorCode enum). AppException.__str__() calls
        # `self.error_code.value` — overwriting with a plain string like
        # "INVALID_COLUMN" causes AttributeError: 'str' has no attribute 'value'.
        self.failure_code = error_code
