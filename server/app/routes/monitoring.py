"""
Monitoring Routes - Enterprise performance monitoring API endpoints
Provides metrics, logs, and system health data
"""

from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

from app.services.performance_service import performance_monitor
from app.services.cache_service import cache_manager
from app.services.logger_service import setup_logger
try:
    from app.services.circuit_breaker import llm_circuit_breaker
except ImportError:
    llm_circuit_breaker = None
from app.services.query_log_service import query_log_service

logger = setup_logger(__name__)

# FEATURE #6: Plan cache metrics - import from module-level instance
try:
    from app.services.plan_cache.plan_cache_service import plan_cache
except Exception as e:
    logger.debug(f"Plan cache not available: {e}")
    plan_cache = None
router = APIRouter(prefix="/monitoring", tags=["Monitoring"])


class MetricsResponse(BaseModel):
    """Response model for metrics endpoint"""
    success: bool
    data: Dict[str, Any]


class LogsResponse(BaseModel):
    """Response model for logs endpoint"""
    success: bool
    logs: List[Dict[str, Any]]
    total: int


class CacheStatsResponse(BaseModel):
    """Response model for cache statistics"""
    success: bool
    cache_stats: Dict[str, Any]


class SystemHealthResponse(BaseModel):
    """Response model for system health check"""
    success: bool
    status: str
    uptime: str
    system: Dict[str, Any]
    cache: Dict[str, Any]
    operations: Dict[str, Any]


@router.get("/metrics", response_model=MetricsResponse)
async def get_metrics():
    """
    Get comprehensive performance metrics
    
    Returns summary statistics for all tracked operations including:
    - Total operations count
    - Average, min, max, P95, P99 response times
    - Cache hit rates
    - Error rates
    - System resource usage
    """
    try:
        metrics = performance_monitor.get_metrics_summary()
        return MetricsResponse(success=True, data=metrics)
    except Exception as e:
        logger.error(f"Error getting metrics: {e}")
        return MetricsResponse(success=False, data={"error": str(e)})


@router.get("/logs", response_model=LogsResponse)
async def get_logs(
    limit: int = Query(default=50, ge=1, le=500, description="Number of logs to return"),
    operation: Optional[str] = Query(default=None, description="Filter by operation type")
):
    """
    Get recent execution logs
    
    Returns detailed logs for each operation including:
    - Operation type
    - Execution time
    - Cache hit/miss status
    - Success/error status
    - Timestamps
    """
    try:
        logs = performance_monitor.get_recent_logs(limit=limit, operation_filter=operation)
        return LogsResponse(success=True, logs=logs, total=len(logs))
    except Exception as e:
        logger.error(f"Error getting logs: {e}")
        return LogsResponse(success=False, logs=[], total=0)


@router.get("/slow-operations")
async def get_slow_operations(
    threshold_ms: float = Query(default=3000, description="Threshold in milliseconds"),
    limit: int = Query(default=20, ge=1, le=100)
):
    """
    Get operations that exceeded the time threshold
    
    Useful for identifying performance bottlenecks
    """
    try:
        slow_ops = performance_monitor.get_slow_operations(threshold_ms=threshold_ms, limit=limit)
        return {
            "success": True,
            "threshold_ms": threshold_ms,
            "operations": slow_ops,
            "total": len(slow_ops)
        }
    except Exception as e:
        logger.error(f"Error getting slow operations: {e}")
        return {"success": False, "error": str(e)}


@router.get("/errors")
async def get_error_logs(
    limit: int = Query(default=20, ge=1, le=100)
):
    """
    Get recent error logs
    
    Returns operations that failed with error messages
    """
    try:
        errors = performance_monitor.get_error_logs(limit=limit)
        return {
            "success": True,
            "errors": errors,
            "total": len(errors)
        }
    except Exception as e:
        logger.error(f"Error getting error logs: {e}")
        return {"success": False, "error": str(e)}


@router.get("/system-metrics")
async def get_system_metrics():
    """
    Get system metrics history for charts
    
    Returns time-series data for:
    - CPU usage
    - Memory usage
    - Active operations
    """
    try:
        history = performance_monitor.get_system_metrics_history()
        return {
            "success": True,
            "metrics": history,
            "count": len(history)
        }
    except Exception as e:
        logger.error(f"Error getting system metrics: {e}")
        return {"success": False, "error": str(e)}


@router.get("/cache-stats", response_model=CacheStatsResponse)
async def get_cache_stats():
    """
    Get cache statistics
    
    Returns stats for all cache types:
    - Token cache
    - Query response cache
    - Schema cache
    - General cache
    """
    try:
        stats = cache_manager.get_all_stats()
        
        # FEATURE #6: Add plan cache statistics
        if plan_cache:
            try:
                plan_cache_stats = plan_cache.get_stats()
                stats["plan_cache"] = plan_cache_stats
            except Exception as e:
                logger.debug(f"Failed to get plan cache stats: {e}")
                stats["plan_cache"] = {"error": str(e)}
        else:
            stats["plan_cache"] = {"error": "Plan cache not available"}
        
        return CacheStatsResponse(success=True, cache_stats=stats)
    except Exception as e:
        logger.error(f"Error getting cache stats: {e}")
        return CacheStatsResponse(success=False, cache_stats={"error": str(e)})


@router.get("/health", response_model=SystemHealthResponse)
async def get_system_health():
    """
    Get comprehensive system health status
    
    Single endpoint for health overview
    """
    try:
        metrics = performance_monitor.get_metrics_summary()
        cache_stats = cache_manager.get_all_stats()
        
        # Determine overall health status
        error_rate = metrics.get("summary", {}).get("error_rate", 0)
        cache_hit_rate = metrics.get("summary", {}).get("cache_hit_rate", 0)
        
        if error_rate > 10:
            status = "degraded"
        elif error_rate > 5:
            status = "warning"
        else:
            status = "healthy"
        
        return SystemHealthResponse(
            success=True,
            status=status,
            uptime=metrics.get("summary", {}).get("uptime_formatted", "0s"),
            system=metrics.get("system", {}),
            cache={
                "overall_hit_rate": cache_hit_rate,
                "details": cache_stats
            },
            operations=metrics.get("summary", {})
        )
    except Exception as e:
        logger.error(f"Error getting system health: {e}")
        return SystemHealthResponse(
            success=False,
            status="error",
            uptime="unknown",
            system={},
            cache={},
            operations={}
        )


@router.post("/reset")
async def reset_metrics():
    """
    Reset all performance metrics
    
    Use with caution - clears all collected data
    """
    try:
        performance_monitor.reset_metrics()
        return {"success": True, "message": "Metrics reset successfully"}
    except Exception as e:
        logger.error(f"Error resetting metrics: {e}")
        return {"success": False, "error": str(e)}


@router.post("/reset-circuit-breaker")
async def reset_circuit_breaker():
    """
    Reset the LLM circuit breaker to CLOSED state.
    
    Use when the circuit breaker is stuck open after transient LLM failures.
    """
    try:
        old_state = llm_circuit_breaker.get_stats()
        llm_circuit_breaker.reset()
        new_state = llm_circuit_breaker.get_stats()
        return {
            "success": True,
            "message": "Circuit breaker reset to CLOSED",
            "previous_state": old_state["state"],
            "current_state": new_state["state"],
        }
    except Exception as e:
        logger.error(f"Error resetting circuit breaker: {e}")
        return {"success": False, "error": str(e)}


@router.get("/circuit-breaker")
async def get_circuit_breaker_status():
    """Get current circuit breaker state and statistics."""
    try:
        stats = llm_circuit_breaker.get_stats()
        return {"success": True, "data": stats}
    except Exception as e:
        logger.error(f"Error getting circuit breaker status: {e}")
        return {"success": False, "error": str(e)}


@router.post("/clear-cache")
async def clear_all_caches():
    """
    Clear all caches
    
    Use when stale data needs to be purged
    """
    try:
        cache_manager.clear_all()
        return {"success": True, "message": "All caches cleared successfully"}
    except Exception as e:
        logger.error(f"Error clearing caches: {e}")
        return {"success": False, "error": str(e)}


@router.post("/cache/query/toggle")
async def toggle_query_cache(enabled: bool = True):
    """
    Enable or disable query response caching
    
    Useful for debugging or when fresh responses are needed
    """
    try:
        if enabled:
            cache_manager.query.enable()
        else:
            cache_manager.query.disable()
        
        return {
            "success": True,
            "message": f"Query cache {'enabled' if enabled else 'disabled'}",
            "enabled": enabled
        }
    except Exception as e:
        logger.error(f"Error toggling query cache: {e}")
        return {"success": False, "error": str(e)}


@router.get("/query-logs")
async def get_query_logs(
    limit: int = Query(default=50, ge=1, le=200, description="Number of query logs to return"),
    refresh: bool = Query(default=False, description="Force refresh the cache")
):
    """
    Get detailed query logs with full timing breakdown
    Results are cached for 30 seconds to reduce load on frequent refreshes
    
    Returns for each query:
    - Original user query (natural language)
    - Generated SQL query
    - Total execution time
    - Operation-by-operation time breakdown
    - Cache hit status
    - Result count
    """
    try:
        cache_key = f"query_logs:{limit}"
        
        # Check cache unless refresh is requested
        if not refresh:
            cached_logs = cache_manager.general.get(cache_key)
            if cached_logs:
                logger.debug(f"Query logs cache hit (limit={limit})")
                return {
                    "success": True,
                    "query_logs": cached_logs,
                    "total": len(cached_logs),
                    "cached": True
                }
        
        # Fetch fresh logs
        logs = performance_monitor.get_query_logs(limit=limit)
        
        # Cache for 60 seconds (increased from 30s to reduce database load)
        # Health checks and monitoring endpoints often get polled frequently
        cache_manager.general.set(cache_key, logs, ttl=60)
        
        return {
            "success": True,
            "query_logs": logs,
            "total": len(logs),
            "cached": False
        }
    except Exception as e:
        logger.error(f"Error getting query logs: {e}")
        return {"success": False, "query_logs": [], "total": 0}


@router.get("/slow-queries")
async def get_slow_queries(
    threshold_ms: float = Query(default=5000, description="Time threshold in milliseconds"),
    limit: int = Query(default=20, ge=1, le=100)
):
    """
    Get queries that exceeded the time threshold
    
    Useful for identifying slow queries that need optimization
    """
    try:
        slow = performance_monitor.get_slow_queries(threshold_ms=threshold_ms, limit=limit)
        return {
            "success": True,
            "threshold_ms": threshold_ms,
            "slow_queries": slow,
            "total": len(slow)
        }
    except Exception as e:
        logger.error(f"Error getting slow queries: {e}")
        return {"success": False, "slow_queries": [], "total": 0}


