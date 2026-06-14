"""
Pydantic models for the MCP Direct Tool Invocation API.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class McpToolCallRequest(BaseModel):
    """Request to call a single MCP tool."""

    tool_name: str = Field(..., description="Name of the MCP tool to invoke")
    arguments: Dict[str, Any] = Field(
        default_factory=dict, description="Tool arguments"
    )


class McpToolCallResponse(BaseModel):
    """Response from a single MCP tool call."""

    success: bool
    tool_name: str
    result: Any = None
    error: Optional[str] = None
    execution_time_ms: float = 0.0


class McpChainStepRequest(BaseModel):
    """A single step in a tool chain."""

    tool_name: str = Field(..., description="Name of the MCP tool to invoke")
    arguments: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Tool arguments (may contain $prev or $step[n] references)."
        ),
    )


class McpChainRequest(BaseModel):
    """Request to execute a chain of MCP tool calls sequentially."""

    steps: List[McpChainStepRequest] = Field(
        ..., description="Ordered list of tool calls", min_length=1
    )


class McpChainResponse(BaseModel):
    """Response from a chain of MCP tool calls."""

    results: List[McpToolCallResponse]
    total_time_ms: float = 0.0


class McpToolDefinition(BaseModel):
    """Definition of an available MCP tool."""

    name: str
    description: str
    parameters: Dict[str, Any]
    category: str = "general"
