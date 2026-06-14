"""
Cache Service

Multi-level caching with TTL support for queries, schemas, and sessions.
"""

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """A single cache entry with TTL."""
    value: Any
    created_at: float
    ttl_seconds: float
    
    @property
    def is_expired(self) -> bool:
        return time.time() > (self.created_at + self.ttl_seconds)


class CacheLayer:
    """A single cache layer with TTL support."""
    
    def __init__(self, name: str, default_ttl: float = 300):
        self.name = name
        self.default_ttl = default_ttl
        self._cache: Dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
    
    def _make_key(self, *args, **kwargs) -> str:
        """Create a cache key from arguments."""
        key_parts = [str(a) for a in args]
        key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
        key_str = "|".join(key_parts)
        return hashlib.md5(key_str.encode()).hexdigest()
    
    def get(self, *args, **kwargs) -> Optional[Any]:
        """Get a value from cache."""
        key = self._make_key(*args, **kwargs)
        
        with self._lock:
            entry = self._cache.get(key)
            
            if entry is None:
                return None
            
            if entry.is_expired:
                del self._cache[key]
                return None
            
            return entry.value
    
    def set(
        self, 
        *args, 
        response: Any = None,
        ttl: Optional[float] = None,
        **kwargs
    ) -> None:
        """Set a value in cache."""
        # Extract response from kwargs if provided there
        if response is None and "response" in kwargs:
            response = kwargs.pop("response")
        
        key = self._make_key(*args, **kwargs)
        
        with self._lock:
            self._cache[key] = CacheEntry(
                value=response,
                created_at=time.time(),
                ttl_seconds=ttl or self.default_ttl
            )
    
    def delete(self, *args, **kwargs) -> bool:
        """Delete a value from cache."""
        key = self._make_key(*args, **kwargs)
        
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False
    
    def clear(self) -> int:
        """Clear all cache entries."""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count
    
    def cleanup_expired(self) -> int:
        """Remove expired entries."""
        with self._lock:
            expired = [k for k, v in self._cache.items() if v.is_expired]
            for key in expired:
                del self._cache[key]
            return len(expired)
    
    @property
    def size(self) -> int:
        """Number of entries in cache."""
        with self._lock:
            return len(self._cache)

    def invalidate_session(self, session_id: str) -> int:
        """Invalidate all cache entries whose key contains the given session_id."""
        with self._lock:
            keys_to_delete = [k for k in self._cache if session_id in k]
            for key in keys_to_delete:
                del self._cache[key]
            if keys_to_delete:
                logger.debug(f"Invalidated {len(keys_to_delete)} cache entries for session {session_id}")
            return len(keys_to_delete)


class CacheManager:
    """
    Multi-level cache manager.
    
    Provides separate cache layers for different data types:
    - query: Query results (5 minute TTL)
    - schema: Schema information (30 minute TTL)
    - session: Session data (1 hour TTL)
    """
    
    def __init__(self):
        self._layers: Dict[str, CacheLayer] = {
            "query": CacheLayer("query", default_ttl=300),      # 5 min
            "schema": CacheLayer("schema", default_ttl=1800),   # 30 min
            "session": CacheLayer("session", default_ttl=3600), # 1 hour
            "static": CacheLayer("static", default_ttl=86400),  # 24 hour (static data)
            "general": CacheLayer("general", default_ttl=10),   # 10 sec (frequently changing)
        }
        self._running = False
        self._cleanup_thread: Optional[threading.Thread] = None
    
    @property
    def query(self) -> CacheLayer:
        """Query result cache."""
        return self._layers["query"]
    
    @property
    def schema(self) -> CacheLayer:
        """Schema cache."""
        return self._layers["schema"]
    
    @property
    def session(self) -> CacheLayer:
        """Session cache."""
        return self._layers["session"]
    
    @property
    def static(self) -> CacheLayer:
        """Static data cache (schema hints, etc)."""
        return self._layers["static"]
    
    @property
    def general(self) -> CacheLayer:
        """General purpose cache (short TTL for frequently changing data)."""
        return self._layers["general"]
    
    def get_layer(self, name: str) -> Optional[CacheLayer]:
        """Get a cache layer by name."""
        return self._layers.get(name)
    
    def start_cleanup_thread(self, interval: int = 60) -> None:
        """Start background cleanup thread."""
        if self._running:
            return
        
        self._running = True
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            args=(interval,),
            daemon=True,
            name="cache-cleanup"
        )
        self._cleanup_thread.start()
        logger.debug("Cache cleanup thread started")
    
    def _cleanup_loop(self, interval: int) -> None:
        """Background cleanup loop."""
        while self._running:
            try:
                total_cleaned = 0
                for layer in self._layers.values():
                    total_cleaned += layer.cleanup_expired()
                
                if total_cleaned > 0:
                    logger.debug(f"Cache cleanup: removed {total_cleaned} expired entries")
                    
            except Exception as e:
                logger.error(f"Cache cleanup error: {e}")
            
            time.sleep(interval)
    
    def shutdown(self) -> None:
        """Stop cleanup thread."""
        self._running = False
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=2.0)
            self._cleanup_thread = None
        logger.debug("Cache cleanup thread stopped")
    
    def clear_all(self) -> Dict[str, int]:
        """Clear all cache layers."""
        result = {}
        for name, layer in self._layers.items():
            result[name] = layer.clear()
        # Only log at INFO when something was actually cleared. On startup the
        # caches are empty by construction, so the unconditional INFO line was
        # just boot-time noise.
        total = sum(result.values())
        (logger.info if total else logger.debug)(f"Cleared all caches: {result}")
        return result
    
    def get_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get cache statistics."""
        return {
            name: {
                "size": layer.size,
                "default_ttl": layer.default_ttl
            }
            for name, layer in self._layers.items()
        }


# Singleton instance
cache_manager = CacheManager()
