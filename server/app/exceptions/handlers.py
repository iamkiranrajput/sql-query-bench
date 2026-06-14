"""
Exception Handlers
Centralized exception handlers for FastAPI application
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from slowapi.errors import RateLimitExceeded
from typing import Dict, Any

from app.exceptions.base import AppException, ErrorCode, ErrorSeverity
from app.services.logger_service import setup_logger

logger = setup_logger(__name__)


def create_error_response(
    status_code: int,
    error_code: str,
    message: str,
    severity: str = "medium",
    details: str = None,
    context: Dict[str, Any] = None,
) -> JSONResponse:
    """
    Create a standardized error response.
    
    Response structure is designed to be compatible with the Angular UI.
    The UI expects either:
    - error.error (string) for simple errors
    - error.error.message for detailed errors
    
    We provide both for maximum compatibility.
    
    Args:
        status_code: HTTP status code
        error_code: Application error code
        message: Human-readable error message
        severity: Error severity level
        details: Additional details
        context: Additional context data
    
    Returns:
        JSONResponse with error information
    """
    content = {
        "success": False,
        # Top-level 'error' as string for backward compatibility with UI
        "error": message,
        # Detailed error object for new UI features
        "error_details": {
            "code": error_code,
            "message": message,
            "severity": severity,
        }
    }
    
    if details:
        content["error_details"]["details"] = details
        content["detail"] = details  # Also add at top level for compatibility
    
    if context:
        content["error_details"]["context"] = context
    
    return JSONResponse(status_code=status_code, content=content)


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    """
    Handle custom application exceptions.
    """
    # Log based on severity
    log_message = f"[{exc.error_code.value}] {exc.message}"
    if exc.context:
        log_message += f" | Context: {exc.context}"
    
    if exc.severity == ErrorSeverity.CRITICAL:
        logger.critical(log_message, exc_info=exc.cause)
    elif exc.severity == ErrorSeverity.HIGH:
        logger.error(log_message, exc_info=exc.cause)
    elif exc.severity == ErrorSeverity.MEDIUM:
        logger.warning(log_message)
    else:
        logger.info(log_message)
    
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict()
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    Handle Pydantic validation errors with user-friendly messages.
    """
    errors = exc.errors()
    
    # Build user-friendly error messages
    error_messages = []
    error_details = []
    
    for error in errors:
        field = " -> ".join(str(loc) for loc in error.get("loc", [])[1:])  # Skip 'body' prefix
        msg = error.get("msg", "Invalid value")
        error_type = error.get("type", "unknown")
        
        if field:
            error_messages.append(f"{field}: {msg}")
            error_details.append({
                "field": field,
                "message": msg,
                "type": error_type
            })
        else:
            error_messages.append(msg)
            error_details.append({
                "message": msg,
                "type": error_type
            })
    
    friendly_message = "; ".join(error_messages) if error_messages else "Invalid request data"
    
    logger.warning(f"Validation error: {friendly_message}")
    
    return create_error_response(
        status_code=422,
        error_code=ErrorCode.VALIDATION_ERROR.value,
        message=friendly_message,
        severity=ErrorSeverity.LOW.value,
        details="Please check your request parameters and try again.",
        context={"validation_errors": error_details}
    )


async def rate_limit_exception_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """
    Handle rate limit exceeded errors with user-friendly message.
    """
    client_ip = request.client.host if request.client else "unknown"
    path = request.url.path
    
    logger.warning(f"Rate limit exceeded for IP: {client_ip} on {path}")
    
    # Determine the type of operation for a more helpful message
    if "/query" in path:
        friendly_message = "Too Many Query Requests: You're sending queries too quickly. Please wait a moment before trying again."
    elif "/execute-sql" in path:
        friendly_message = "Too Many SQL Executions: You're executing SQL queries too quickly. Please wait a moment."
    elif "/explain" in path or "/fix" in path or "/describe" in path:
        friendly_message = "Too Many AI Requests: The AI service is rate limited. Please wait a moment before trying again."
    elif "/connect" in path:
        friendly_message = "Too Many Connection Attempts: Please wait before trying to connect again."
    else:
        friendly_message = "Too Many Requests: You're making requests too quickly. Please slow down and try again in a moment."
    
    return create_error_response(
        status_code=429,
        error_code=ErrorCode.TOO_MANY_REQUESTS.value,
        message=friendly_message,
        severity=ErrorSeverity.MEDIUM.value,
        details="Rate limit exceeded. Please wait before making more requests.",
        context={
            "path": path,
            "retry_after": "Wait a few seconds before retrying"
        }
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Handle all uncaught exceptions with user-friendly messages.
    """
    error_str = str(exc)
    error_type = type(exc).__name__
    
    logger.error(f"Unhandled exception: {error_str}", exc_info=True)
    
    # Provide more specific messages based on error type
    if "connection" in error_str.lower() or "connect" in error_str.lower():
        user_message = "Connection Error: Unable to connect to the service. Please check your connection and try again."
    elif "timeout" in error_str.lower():
        user_message = "Request Timeout: The request took too long to complete. Please try again."
    elif "permission" in error_str.lower() or "denied" in error_str.lower():
        user_message = "Permission Denied: You don't have permission to perform this action."
    elif "not found" in error_str.lower() or "404" in error_str:
        user_message = "Not Found: The requested resource could not be found."
    elif "invalid" in error_str.lower():
        user_message = "Invalid Request: The request contains invalid data. Please check your input."
    else:
        user_message = "An Unexpected Error Occurred: Something went wrong on our end. Please try again or contact support if the issue persists."
    
    return create_error_response(
        status_code=500,
        error_code=ErrorCode.INTERNAL_ERROR.value,
        message=user_message,
        severity=ErrorSeverity.HIGH.value,
        details="The server encountered an unexpected error. If this persists, please contact support.",
        context={
            "error_type": error_type,
            "request_path": request.url.path
        }
    )


def setup_exception_handlers(app: FastAPI) -> None:
    """
    Register all exception handlers with the FastAPI application.
    
    Args:
        app: FastAPI application instance
    """
    # Custom application exceptions
    app.add_exception_handler(AppException, app_exception_handler)
    
    # Validation errors
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    
    # Rate limiting errors
    app.add_exception_handler(RateLimitExceeded, rate_limit_exception_handler)
    
    # Generic catch-all handler (should be last)
    app.add_exception_handler(Exception, generic_exception_handler)
    
    logger.debug("Exception handlers configured")
