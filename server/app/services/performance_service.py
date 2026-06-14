"""
Performance Service

Tracks operation timing and provides performance monitoring.
"""

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class OperationMetrics:
    """Metrics for a single operation."""
    operation: str
    start_time: float
    end_time: Optional[float] = None
    duration_ms: float = 0.0
    success: bool = True
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    cache_hit: bool = False


@dataclass
class QueryLog:
    """Log entry for a query execution."""
    timestamp: str
    user_query: str
    generated_sql: str
    total_time_ms: float
    operation_times: Dict[str, float]
    success: bool
    cache_hit: bool
    result_count: int
    error_message: Optional[str]


class OperationTracker:
    """Context manager for tracking operation timing."""
    
    def __init__(self, operation: str, monitor: 'PerformanceMonitor'):
        self.operation = operation
        self.monitor = monitor
        self.metrics = OperationMetrics(
            operation=operation,
            start_time=time.perf_counter()
        )
    
    def __enter__(self) -> 'OperationTracker':
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.metrics.end_time = time.perf_counter()
        self.metrics.duration_ms = (self.metrics.end_time - self.metrics.start_time) * 1000
        
        if exc_type:
            self.metrics.success = False
            self.metrics.error = str(exc_val)
        
        self.monitor._record_operation(self.metrics)
        return False  # Don't suppress exceptions
    
    def add_metadata(self, key: str, value: Any) -> None:
        """Add metadata to the operation."""
        self.metrics.metadata[key] = value
    
    def set_error(self, error: str) -> None:
        """Mark operation as failed with error."""
        self.metrics.success = False
        self.metrics.error = error
    
    def set_cache_hit(self, hit: bool) -> None:
        """Mark whether this was a cache hit."""
        self.metrics.cache_hit = hit


class PerformanceMonitor:
    """
    Performance monitoring service.
    
    Tracks operation timing, query logs, and provides statistics.
    """
    
    MAX_RECENT_OPERATIONS = 1000
    MAX_QUERY_LOGS = 500
    
    def __init__(self):
        self._lock = threading.Lock()
        self._recent_operations: List[OperationMetrics] = []
        self._query_logs: List[QueryLog] = []
        self._operation_stats: Dict[str, Dict[str, float]] = {}
    
    @contextmanager
    def track_operation(self, operation: str) -> Generator[OperationTracker, None, None]:
        """
        Track an operation's timing.
        
        Usage:
            with performance_monitor.track_operation("query_generation") as tracker:
                result = generate_query()
                tracker.add_metadata("sql_length", len(result))
        """
        tracker = OperationTracker(operation, self)
        yield tracker
    
    def _record_operation(self, metrics: OperationMetrics) -> None:
        """Record operation metrics."""
        with self._lock:
            self._recent_operations.append(metrics)
            
            # Trim if too many
            if len(self._recent_operations) > self.MAX_RECENT_OPERATIONS:
                self._recent_operations = self._recent_operations[-self.MAX_RECENT_OPERATIONS:]
            
            # Update stats
            op = metrics.operation
            if op not in self._operation_stats:
                self._operation_stats[op] = {
                    "count": 0,
                    "total_ms": 0,
                    "min_ms": float('inf'),
                    "max_ms": 0,
                    "errors": 0
                }
            
            stats = self._operation_stats[op]
            stats["count"] += 1
            stats["total_ms"] += metrics.duration_ms
            stats["min_ms"] = min(stats["min_ms"], metrics.duration_ms)
            stats["max_ms"] = max(stats["max_ms"], metrics.duration_ms)
            if not metrics.success:
                stats["errors"] += 1
    
    def log_query(
        self,
        user_query: str,
        generated_sql: str,
        total_time_ms: float,
        operation_times: Dict[str, float],
        success: bool,
        cache_hit: bool = False,
        result_count: int = 0,
        error_message: Optional[str] = None
    ) -> None:
        """Log a query execution."""
        log_entry = QueryLog(
            timestamp=datetime.now(timezone.utc).isoformat(),
            user_query=user_query,
            generated_sql=generated_sql,
            total_time_ms=total_time_ms,
            operation_times=operation_times,
            success=success,
            cache_hit=cache_hit,
            result_count=result_count,
            error_message=error_message
        )
        
        with self._lock:
            self._query_logs.append(log_entry)
            
            if len(self._query_logs) > self.MAX_QUERY_LOGS:
                self._query_logs = self._query_logs[-self.MAX_QUERY_LOGS:]
    
    def get_stats(self) -> Dict[str, Any]:
        """Get performance statistics."""
        with self._lock:
            return self._get_stats_unlocked()

    def _get_stats_unlocked(self) -> Dict[str, Any]:
        """Get performance statistics (caller must hold self._lock)."""
        stats = {}
        for op, op_stats in self._operation_stats.items():
            count = op_stats["count"]
            stats[op] = {
                "count": count,
                "avg_ms": op_stats["total_ms"] / count if count > 0 else 0,
                "min_ms": op_stats["min_ms"] if op_stats["min_ms"] != float('inf') else 0,
                "max_ms": op_stats["max_ms"],
                "error_rate": op_stats["errors"] / count if count > 0 else 0
            }
        return stats
    
    def get_recent_operations(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent operations."""
        with self._lock:
            operations = self._recent_operations[-limit:]
            return [
                {
                    "operation": op.operation,
                    "duration_ms": round(op.duration_ms, 2),
                    "success": op.success,
                    "cache_hit": op.cache_hit,
                    "error": op.error,
                    "metadata": op.metadata
                }
                for op in operations
            ]
    
    def get_query_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent query logs."""
        with self._lock:
            logs = self._query_logs[-limit:]
            return [
                {
                    "timestamp": log.timestamp,
                    "user_query": log.user_query[:100],
                    "total_time_ms": round(log.total_time_ms, 2),
                    "success": log.success,
                    "cache_hit": log.cache_hit,
                    "result_count": log.result_count,
                    "error": log.error_message
                }
                for log in reversed(logs)
            ]
    
    def clear(self) -> None:
        """Clear all metrics."""
        with self._lock:
            self._recent_operations.clear()
            self._query_logs.clear()
            self._operation_stats.clear()

    def record_operation(
        self,
        operation: str,
        duration_ms: float,
        success: bool = True,
        error_message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Record an operation directly (without context manager).
        
        Used by middleware for request tracking.
        """
        metrics = OperationMetrics(
            operation=operation,
            start_time=time.perf_counter() - (duration_ms / 1000),
            end_time=time.perf_counter(),
            duration_ms=duration_ms,
            success=success,
            error=error_message,
            metadata=metadata or {}
        )
        self._record_operation(metrics)

    def get_metrics_summary(self) -> Dict[str, Any]:
        """Get a summary of all metrics for shutdown logging."""
        with self._lock:
            total_ops = len(self._recent_operations)
            total_queries = len(self._query_logs)
            
            successful_ops = sum(1 for op in self._recent_operations if op.success)
            failed_ops = total_ops - successful_ops
            
            successful_queries = sum(1 for q in self._query_logs if q.success)
            cache_hits = sum(1 for q in self._query_logs if q.cache_hit)
            
            avg_query_time = 0.0
            if self._query_logs:
                avg_query_time = sum(q.total_time_ms for q in self._query_logs) / len(self._query_logs)
            
            return {
                "total_operations": total_ops,
                "successful_operations": successful_ops,
                "failed_operations": failed_ops,
                "total_queries": total_queries,
                "successful_queries": successful_queries,
                "cache_hits": cache_hits,
                "cache_hit_rate": cache_hits / total_queries if total_queries > 0 else 0.0,
                "avg_query_time_ms": round(avg_query_time, 2),
                "operation_stats": self._get_stats_unlocked()
            }

# Singleton instance
performance_monitor = PerformanceMonitor()
