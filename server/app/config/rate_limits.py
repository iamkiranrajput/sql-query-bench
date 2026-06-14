"""
Centralized rate-limiting configuration.

The limiter is keyed by the *real* client IP when the request reached us
through one of the trusted proxies configured via ``TRUSTED_PROXIES``, and
falls back to the immediate peer otherwise so that a shared upstream proxy
cannot become a single rate-limit bucket for all public traffic.
"""

from __future__ import annotations

import ipaddress
import logging
from functools import lru_cache

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.config.settings import settings

logger = logging.getLogger(__name__)


def _parse_trusted_proxies() -> frozenset[str]:
    """Normalise TRUSTED_PROXIES into a frozenset of canonical IP strings."""

    raw = (getattr(settings, "trusted_proxies", "") or "").strip()
    out: set[str] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.add(str(ipaddress.ip_address(token)))
        except ValueError:
            logger.warning(
                "TRUSTED_PROXIES contained non-IP value %r -- ignoring", token
            )
    return frozenset(out)


_TRUSTED_PROXIES = _parse_trusted_proxies()


def _first_valid_ip(value: str) -> str | None:
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            return str(ipaddress.ip_address(token))
        except ValueError:
            continue
    return None


def get_rate_limit_key(request: Request) -> str:
    """Build the rate-limit key for the request."""

    peer = request.client.host if request.client else None
    if peer and peer in _TRUSTED_PROXIES:
        for header in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
            raw = request.headers.get(header)
            if not raw:
                continue
            ip = _first_valid_ip(raw)
            if ip:
                return ip
    if peer:
        return peer
    return get_remote_address(request)


@lru_cache()
def get_limiter() -> Limiter:
    return Limiter(
        key_func=get_rate_limit_key,
        default_limits=["200/minute"],
        storage_uri="memory://",
        strategy="fixed-window",
    )


class RateLimits:
    """Per-endpoint rate-limit strings consumed by ``@limiter.limit(...)``."""

    # SQL & data access
    SQL_EXECUTE = "60/minute"
    SQL_EXPORT = "10/minute"

    # Database session management
    DB_CONNECT = "10/minute"
    DB_DISCONNECT = "30/minute"
    DB_LIST = "20/minute"
    DB_SCHEMA = "30/minute"

    # Health & monitoring
    HEALTH = "120/minute"
    MONITORING = "60/minute"

    # Catch-all
    STANDARD = "60/minute"


limiter = get_limiter()
