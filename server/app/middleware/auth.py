"""
API Key Authentication Middleware

When API_KEY is set in .env, all API requests must include:
  Authorization: Bearer <API_KEY>

Exempt paths (no auth required):
  - /api/docs, /api/redoc, /api/openapi.json (Swagger)
  - /api/monitoring/health (health checks)
  - OPTIONS requests (CORS preflight)
  - Static file serving (/)
"""

import secrets
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.services.logger_service import setup_logger

logger = setup_logger(__name__)

# Paths that never require authentication
_EXEMPT_PATHS = frozenset({
    "/api/docs",
    "/api/redoc",
    "/api/openapi.json",
    "/api/monitoring/health",
})

# Path prefixes that are exempt
#
# IMPORTANT: do NOT add `/api/llm/` or any LLM configuration route here.
# An unauthenticated `POST /api/llm/configure` would let an attacker redirect
# the system to an attacker-controlled LLM endpoint that logs every prompt,
# leaking schema and business logic. Routes that the UI needs before login
# (Copilot OAuth device-flow + config lookup) are the only legitimate
# exemptions.
_EXEMPT_PREFIXES = (
    "/docs",
    "/redoc",
    "/openapi",
    "/api/copilot/auth/",      # OAuth device flow (setup before auth is possible)
    "/api/copilot/config",     # Config check (UI needs this to show connect dialog)
    "/api/copilot/connections", # Connection status
    "/api/copilot/models",     # Model listing
)


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """
    Validates Authorization: Bearer <key> on all /api/ routes.
    Disabled when api_key setting is empty.
    """

    def __init__(self, app, api_key: str):
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next):
        # Skip auth for OPTIONS (CORS preflight)
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path

        # Skip auth for exempt paths
        if path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)

        # Skip auth for non-API paths (static files, root)
        if not path.startswith("/api/"):
            return await call_next(request)

        # Validate Authorization header
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header. Use: Bearer <API_KEY>"},
            )

        token = auth_header[7:]  # Strip "Bearer "
        if not secrets.compare_digest(token, self._api_key):
            logger.warning(f"Invalid API key attempt from {request.client.host}")
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid API key"},
            )

        return await call_next(request)
