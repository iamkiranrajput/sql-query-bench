"""
Trace Models

Data classes for recording execution traces — the "thinking" steps
that show how the iterative query engine resolved a query.
Similar to how Copilot shows its reasoning process.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TraceStep:
    """A single step in the query resolution trace."""

    strategy: str  # "fast_pipeline", "live_schema_fix", "alternative_joins", etc.
    action: str  # "search_tables", "introspect_schema", "execute_sql", etc.
    input_summary: str  # Brief description of what was sent
    output_summary: str  # Brief description of what came back
    success: bool
    duration_ms: float = 0.0
    error: Optional[str] = None
    details: Optional[Dict[str, Any]] = None  # Extra data for UI rendering

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "action": self.action,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "success": self.success,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
            "details": self.details,
        }


@dataclass
class QueryTrace:
    """
    Full execution trace for a query resolution.
    Records every step the engine tried, including failures.
    """

    steps: List[TraceStep] = field(default_factory=list)
    strategies_used: List[str] = field(default_factory=list)
    resolution_method: str = "direct"  # "direct" | "corrected" | "alternative_join" | "pattern_match" | "full_agent"
    total_duration_ms: float = 0.0
    _start_time: Optional[float] = field(default=None, repr=False)

    def start(self) -> None:
        """Mark trace start time."""
        self._start_time = time.perf_counter()

    def finish(self) -> None:
        """Mark trace end and compute total duration."""
        if self._start_time:
            self.total_duration_ms = (time.perf_counter() - self._start_time) * 1000

    def add_step(
        self,
        strategy: str,
        action: str,
        input_summary: str,
        output_summary: str,
        success: bool,
        duration_ms: float = 0.0,
        error: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> TraceStep:
        """Add a step to the trace."""
        step = TraceStep(
            strategy=strategy,
            action=action,
            input_summary=input_summary,
            output_summary=output_summary,
            success=success,
            duration_ms=duration_ms,
            error=error,
            details=details,
        )
        self.steps.append(step)
        if strategy not in self.strategies_used:
            self.strategies_used.append(strategy)
        return step

    def to_dict(self) -> Dict[str, Any]:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "strategies_used": self.strategies_used,
            "resolution_method": self.resolution_method,
            "total_duration_ms": round(self.total_duration_ms, 1),
            "step_count": len(self.steps),
        }

    @property
    def failed_steps(self) -> List[TraceStep]:
        return [s for s in self.steps if not s.success]

    @property
    def succeeded_steps(self) -> List[TraceStep]:
        return [s for s in self.steps if s.success]
