"""
Authentication Exceptions
Custom exceptions for authentication and authorization errors
"""

from typing import Optional, Dict, Any
from app.exceptions.base import AppException, ErrorCode, ErrorSeverity


class AuthException(AppException):
    """Base exception for all authentication-related errors"""
    
    def __init__(
        self,
        message: str = "Authentication error",
        error_code: ErrorCode = ErrorCode.AUTH_ERROR,
        status_code: int = 401,
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


class UnauthorizedException(AuthException):
    """Exception raised when user is not authenticated"""
    
    def __init__(
        self,
        message: str = "Authentication required",
        resource: Optional[str] = None,
        details: Optional[str] = None,
    ):
        context = {}
        if resource:
            context["resource"] = resource
        
        super().__init__(
            message=message,
            error_code=ErrorCode.UNAUTHORIZED,
            status_code=401,
            details=details or "Please provide valid authentication credentials",
            context=context,
        )


class ForbiddenException(AuthException):
    """Exception raised when user lacks permission"""
    
    def __init__(
        self,
        message: str = "Access denied",
        resource: Optional[str] = None,
        required_permission: Optional[str] = None,
        details: Optional[str] = None,
    ):
        context = {}
        if resource:
            context["resource"] = resource
        if required_permission:
            context["required_permission"] = required_permission
        
        super().__init__(
            message=message,
            error_code=ErrorCode.FORBIDDEN,
            status_code=403,
            severity=ErrorSeverity.MEDIUM,
            details=details or "You do not have permission to access this resource",
            context=context,
        )


class TokenExpiredException(AuthException):
    """Exception raised when authentication token has expired"""
    
    def __init__(
        self,
        message: str = "Authentication token has expired",
        expired_at: Optional[str] = None,
        details: Optional[str] = None,
    ):
        context = {}
        if expired_at:
            context["expired_at"] = expired_at
        
        super().__init__(
            message=message,
            error_code=ErrorCode.TOKEN_EXPIRED,
            status_code=401,
            details=details or "Please re-authenticate to continue",
            context=context,
        )


class InvalidTokenException(AuthException):
    """Exception raised when authentication token is invalid"""
    
    def __init__(
        self,
        message: str = "Invalid authentication token",
        reason: Optional[str] = None,
        details: Optional[str] = None,
    ):
        context = {}
        if reason:
            context["reason"] = reason
        
        super().__init__(
            message=message,
            error_code=ErrorCode.INVALID_TOKEN,
            status_code=401,
            details=details or "The provided token is invalid or malformed",
            context=context,
        )


class AzureADException(AuthException):
    """Exception raised for Azure AD authentication errors"""
    
    def __init__(
        self,
        message: str = "Azure AD authentication failed",
        tenant_id: Optional[str] = None,
        error_description: Optional[str] = None,
        details: Optional[str] = None,
        cause: Optional[Exception] = None,
    ):
        context = {}
        if tenant_id:
            context["tenant_id"] = tenant_id
        if error_description:
            context["error_description"] = error_description
        
        super().__init__(
            message=message,
            error_code=ErrorCode.AUTH_ERROR,
            status_code=401,
            severity=ErrorSeverity.HIGH,
            details=details or "Failed to authenticate with Azure AD",
            context=context,
            cause=cause,
        )
