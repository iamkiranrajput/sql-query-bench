"""
Services package -- shared singletons for the FastAPI app.

Only the services that survive the hackathon cleanup are re-exported here.
The Azure-based ``mcp_client_service`` and the learning services have been
removed.
"""

from .cache_service import CacheManager, cache_manager
from .database_service import DatabaseService, database_service
from .logger_service import setup_logger
from .performance_service import PerformanceMonitor, performance_monitor
from .query_log_service import QueryLogService, query_log_service

__all__ = [
    "setup_logger",
    "performance_monitor",
    "PerformanceMonitor",
    "cache_manager",
    "CacheManager",
    "database_service",
    "DatabaseService",
    "query_log_service",
    "QueryLogService",
]
