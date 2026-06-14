"""
Pydantic models for request and response schemas (DB connection + health).
Query / Chat / Feedback models were removed in the hackathon cleanup.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ConnectRequest(BaseModel):
    """Database connection request."""

    hostname: str = Field(..., description="Database hostname or IP address")
    port: int = Field(default=5432, description="Database port", ge=1, le=65535)
    database: str = Field(..., description="Database name")
    username: str = Field(..., description="Database username")
    password: str = Field(..., description="Database password")
    db_type: str = Field(
        default="postgresql",
        description="Database type: postgresql, mysql, mssql, oracle",
    )


class DisconnectRequest(BaseModel):
    """Database disconnection request."""

    session_id: str = Field(..., description="Session ID to disconnect")


class ConnectResponse(BaseModel):
    """Database connection response."""

    success: bool = Field(..., description="Connection success status")
    session_id: Optional[str] = Field(None, description="Unique session identifier")
    db_identity: Optional[str] = Field(
        None, description="Database identity key for scoping logs"
    )
    message: str = Field(..., description="Connection status message")
    error: Optional[str] = Field(None, description="Error message if failed")


class DisconnectResponse(BaseModel):
    """Database disconnection response."""

    success: bool = Field(..., description="Disconnection success status")
    message: str = Field(..., description="Disconnection status message")
    error: Optional[str] = Field(None, description="Error message if failed")


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = Field(..., description="API status")
    version: str = Field(..., description="API version")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow, description="Current timestamp"
    )
    active_sessions: int = Field(
        default=0, description="Number of active database sessions"
    )
    llm_reachable: Optional[bool] = Field(
        default=None,
        description="Whether the configured LLM endpoint is reachable",
    )
    llm_model: Optional[str] = Field(
        default=None, description="Configured LLM model deployment"
    )


class SessionInfo(BaseModel):
    """Internal session information."""

    session_id: str
    hostname: str
    database: str
    username: str
    created_at: datetime
    last_accessed: datetime
