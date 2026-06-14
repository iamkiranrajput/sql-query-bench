"""
AI/LLM Exceptions
Custom exceptions for AI and OpenAI-related errors
"""

from typing import Optional, Dict, Any
from app.exceptions.base import AppException, ErrorCode, ErrorSeverity


class AIException(AppException):
    """Base exception for all AI-related errors"""
    
    def __init__(
        self,
        message: str = "An AI service error occurred",
        error_code: ErrorCode = ErrorCode.AI_ERROR,
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


class OpenAIException(AIException):
    """Exception raised for OpenAI API errors"""
    
    def __init__(
        self,
        message: str = "OpenAI API error occurred",
        openai_error_code: Optional[str] = None,
        openai_error_type: Optional[str] = None,
        details: Optional[str] = None,
        cause: Optional[Exception] = None,
    ):
        context = {}
        if openai_error_code:
            context["openai_error_code"] = openai_error_code
        if openai_error_type:
            context["openai_error_type"] = openai_error_type
        
        super().__init__(
            message=message,
            error_code=ErrorCode.OPENAI_ERROR,
            status_code=502,
            severity=ErrorSeverity.HIGH,
            details=details or "There was an issue communicating with the AI service",
            context=context,
            cause=cause,
        )


class TokenLimitExceededException(AIException):
    """Exception raised when token limit is exceeded"""
    
    def __init__(
        self,
        message: str = "Token limit exceeded",
        tokens_used: Optional[int] = None,
        token_limit: Optional[int] = None,
        details: Optional[str] = None,
    ):
        context = {}
        if tokens_used:
            context["tokens_used"] = tokens_used
        if token_limit:
            context["token_limit"] = token_limit
        
        super().__init__(
            message=message,
            error_code=ErrorCode.TOKEN_LIMIT_EXCEEDED,
            status_code=400,
            severity=ErrorSeverity.MEDIUM,
            details=details or "Your query is too long. Please try a shorter or simpler query.",
            context=context,
        )


class InvalidPromptException(AIException):
    """Exception raised for invalid or unsafe prompts"""
    
    def __init__(
        self,
        message: str = "Invalid or unsafe prompt",
        prompt_preview: Optional[str] = None,
        reason: Optional[str] = None,
        details: Optional[str] = None,
    ):
        context = {}
        if prompt_preview:
            # Only show first 100 chars for safety
            context["prompt_preview"] = prompt_preview[:100] + "..." if len(prompt_preview) > 100 else prompt_preview
        if reason:
            context["reason"] = reason
        
        super().__init__(
            message=message,
            error_code=ErrorCode.INVALID_PROMPT,
            status_code=400,
            severity=ErrorSeverity.MEDIUM,
            details=details or "Please rephrase your query and try again",
            context=context,
        )


class AIServiceUnavailableException(AIException):
    """Exception raised when AI service is unavailable"""
    
    def __init__(
        self,
        message: str = "AI service is temporarily unavailable",
        service_name: Optional[str] = None,
        retry_after: Optional[int] = None,
        details: Optional[str] = None,
        cause: Optional[Exception] = None,
    ):
        context = {}
        if service_name:
            context["service_name"] = service_name
        if retry_after:
            context["retry_after_seconds"] = retry_after
        
        super().__init__(
            message=message,
            error_code=ErrorCode.AI_SERVICE_UNAVAILABLE,
            status_code=503,
            severity=ErrorSeverity.CRITICAL,
            details=details or "Please try again in a few moments",
            context=context,
            cause=cause,
        )


class QueryGenerationException(AIException):
    """Exception raised when SQL query generation fails"""
    
    def __init__(
        self,
        message: str = "Failed to generate SQL query",
        user_prompt: Optional[str] = None,
        reason: Optional[str] = None,
        details: Optional[str] = None,
        cause: Optional[Exception] = None,
    ):
        context = {}
        if user_prompt:
            context["user_prompt"] = user_prompt[:200] + "..." if len(user_prompt) > 200 else user_prompt
        if reason:
            context["reason"] = reason
        
        super().__init__(
            message=message,
            error_code=ErrorCode.QUERY_GENERATION_FAILED,
            status_code=422,
            severity=ErrorSeverity.MEDIUM,
            details=details or "Unable to convert your request to SQL. Please try rephrasing your question.",
            context=context,
            cause=cause,
        )
