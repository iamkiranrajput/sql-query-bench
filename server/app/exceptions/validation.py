"""
Validation Exceptions
Custom exceptions for input validation errors
"""

from typing import Optional, Dict, Any, List
from app.exceptions.base import AppException, ErrorCode, ErrorSeverity


class ValidationException(AppException):
    """Base exception for all validation-related errors"""
    
    def __init__(
        self,
        message: str = "Validation error",
        error_code: ErrorCode = ErrorCode.VALIDATION_ERROR,
        status_code: int = 422,
        severity: ErrorSeverity = ErrorSeverity.LOW,
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


class InvalidInputException(ValidationException):
    """Exception raised for invalid input values"""
    
    def __init__(
        self,
        message: str = "Invalid input provided",
        field_name: Optional[str] = None,
        provided_value: Optional[Any] = None,
        expected_type: Optional[str] = None,
        details: Optional[str] = None,
    ):
        context = {}
        if field_name:
            context["field"] = field_name
        if provided_value is not None:
            # Truncate long values
            str_value = str(provided_value)
            context["provided_value"] = str_value[:100] + "..." if len(str_value) > 100 else str_value
        if expected_type:
            context["expected_type"] = expected_type
        
        super().__init__(
            message=message,
            error_code=ErrorCode.INVALID_INPUT,
            details=details,
            context=context,
        )


class MissingRequiredFieldException(ValidationException):
    """Exception raised when required field is missing"""
    
    def __init__(
        self,
        field_name: str,
        message: Optional[str] = None,
        details: Optional[str] = None,
    ):
        context = {"field": field_name}
        
        super().__init__(
            message=message or f"Required field '{field_name}' is missing",
            error_code=ErrorCode.MISSING_REQUIRED_FIELD,
            status_code=400,
            details=details or f"Please provide a value for '{field_name}'",
            context=context,
        )


class InvalidFormatException(ValidationException):
    """Exception raised for invalid format (email, date, etc.)"""
    
    def __init__(
        self,
        message: str = "Invalid format",
        field_name: Optional[str] = None,
        expected_format: Optional[str] = None,
        provided_value: Optional[str] = None,
        details: Optional[str] = None,
    ):
        context = {}
        if field_name:
            context["field"] = field_name
        if expected_format:
            context["expected_format"] = expected_format
        if provided_value:
            context["provided_value"] = provided_value[:50] + "..." if len(provided_value) > 50 else provided_value
        
        super().__init__(
            message=message,
            error_code=ErrorCode.INVALID_FORMAT,
            details=details or f"Expected format: {expected_format}" if expected_format else None,
            context=context,
        )


class ValueOutOfRangeException(ValidationException):
    """Exception raised when value is out of acceptable range"""
    
    def __init__(
        self,
        field_name: str,
        provided_value: Any,
        min_value: Optional[Any] = None,
        max_value: Optional[Any] = None,
        message: Optional[str] = None,
        details: Optional[str] = None,
    ):
        context = {
            "field": field_name,
            "provided_value": provided_value,
        }
        if min_value is not None:
            context["min_value"] = min_value
        if max_value is not None:
            context["max_value"] = max_value
        
        # Build message if not provided
        if not message:
            if min_value is not None and max_value is not None:
                message = f"Value for '{field_name}' must be between {min_value} and {max_value}"
            elif min_value is not None:
                message = f"Value for '{field_name}' must be at least {min_value}"
            elif max_value is not None:
                message = f"Value for '{field_name}' must be at most {max_value}"
            else:
                message = f"Value for '{field_name}' is out of range"
        
        super().__init__(
            message=message,
            error_code=ErrorCode.VALUE_OUT_OF_RANGE,
            details=details,
            context=context,
        )


class MultipleValidationException(ValidationException):
    """Exception raised when multiple validation errors occur"""
    
    def __init__(
        self,
        errors: List[Dict[str, Any]],
        message: str = "Multiple validation errors occurred",
        details: Optional[str] = None,
    ):
        context = {
            "errors": errors,
            "error_count": len(errors),
        }
        
        super().__init__(
            message=message,
            error_code=ErrorCode.VALIDATION_ERROR,
            details=details or "Please fix the listed errors and try again",
            context=context,
        )
