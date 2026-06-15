"""
GitHub Copilot Integration Service

Supports TWO auth methods (same as VS Code):

A) OAuth Device Flow (recommended):
   1. App requests a device code from GitHub
   2. User opens github.com/login/device and enters the code
   3. App polls GitHub until user approves → gets OAuth access token
   4. Exchange access token for Copilot session token

B) Personal Access Token (PAT):
   1. User pastes a classic PAT
   2. Exchange PAT for Copilot session token

In both cases the final step is:
   GET https://api.github.com/copilot_internal/v2/token
   → short-lived Copilot JWT → api.githubcopilot.com/chat/completions
"""

import asyncio
import json
import logging
import os
import re
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.mcp_server import get_mcp_server
from app.mcp_server.tools.sql_normalizer import normalize_readonly_sql

logger = logging.getLogger(__name__)

# C9: cap how many rows we ship over SSE/JSON to the UI. The Angular grid
# never renders more than this, and the full result set is always available
# by re-executing the SQL via /api/copilot/sql/execute.
_UI_ROW_LIMIT = 500

# C8: short conversational user messages ("hi", "thanks", "ok cool") should
# never trigger an auto-continue prompt — there is nothing to continue and
# nudging the model only wastes tokens / produces hallucinated work.
_CONVERSATIONAL_PATTERN = re.compile(
    r"^(?:hi|hey|hello|yo|sup|thanks|thank\s*you|thx|ty|"
    r"ok(?:ay)?|cool|nice|great|good|got\s*it|"
    r"bye|goodbye|cya|see\s*ya)[\s!?.,]*$",
    re.IGNORECASE,
)


def _looks_conversational(msg: str) -> bool:
    """Return True if ``msg`` is a short greeting / acknowledgement."""
    if not msg:
        return True
    stripped = msg.strip()
    if len(stripped) <= 4:
        return True
    if len(stripped) <= 30 and _CONVERSATIONAL_PATTERN.match(stripped):
        return True
    return False

# ── SSL Context ────────────────────────────────────────────────────
# Try proper certificate verification first (what VS Code does).
# Falls back to unverified ONLY if corporate proxy has custom CA
# that isn't in the system trust store.
def _build_ssl_context() -> ssl.SSLContext:
    """
    Build SSL context with proper cert verification.
    Priority: 1) Custom CA bundle (REQUESTS_CA_BUNDLE / SSL_CERT_FILE env)
              2) System default trust store (certifi)
              3) Unverified (last resort for corporate proxies)
    """
    # Check for custom CA bundle (set by IT for corporate proxies)
    ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca_bundle and os.path.isfile(ca_bundle):
        ctx = ssl.create_default_context(cafile=ca_bundle)
        logger.info(f"SSL: Using custom CA bundle from {ca_bundle}")
        return ctx

    # Try system default (certifi included with httpx/requests)
    try:
        ctx = ssl.create_default_context()
        # Verify default certs are loaded (77+ root certs expected)
        stats = ctx.cert_store_stats()
        if stats.get("x509_ca", 0) > 0:
            logger.info(f"SSL: Using system trust store ({stats['x509_ca']} CA certs)")
            return ctx
    except Exception:
        pass

    # Try certifi explicitly
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        logger.info("SSL: Using certifi CA bundle")
        return ctx
    except Exception:
        pass

    # Last resort: unverified (log a warning so it's visible)
    logger.warning(
        "SSL: ⚠️ Certificate verification DISABLED — no valid CA bundle found. "
        "Set REQUESTS_CA_BUNDLE or SSL_CERT_FILE env var to your corporate CA bundle path."
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

_SSL_CTX = _build_ssl_context()

# ── Endpoints ──────────────────────────────────────────────────────
GITHUB_API_URL = "https://api.github.com"
GITHUB_SITE_URL = "https://github.com"
COPILOT_API_URL = "https://api.githubcopilot.com"

# ── OAuth Device Flow ──────────────────────────────────────────────
# VS Code uses client_id 01ab8ac9400c4e429b23 for GitHub auth.
# The Copilot CLI uses Iv1.b507a08c87ecfe98.
# We use VS Code's client_id since the user already has Copilot via VS Code.
GITHUB_CLIENT_ID = "01ab8ac9400c4e429b23"

# ── Headers that VS Code sends (required by Copilot API) ──────────
COPILOT_HEADERS = {
    "Editor-Version": "vscode/1.100.0",
    "Editor-Plugin-Version": "copilot-chat/0.25.2024",
    "Copilot-Integration-Id": "vscode-chat",
    "Openai-Organization": "github-copilot",
    "Openai-Intent": "conversation-panel",
}

# ── Fallback model list ───────────────────────────────────────────
KNOWN_MODELS = [
    {"id": "gpt-4o", "name": "GPT-4o", "vendor": "OpenAI", "context_window": 128000},
    {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "vendor": "OpenAI", "context_window": 128000},
    {"id": "gpt-4.1", "name": "GPT-4.1", "vendor": "OpenAI", "context_window": 1047576},
    {"id": "claude-sonnet-4", "name": "Claude Sonnet 4", "vendor": "Anthropic", "context_window": 200000},
    {"id": "claude-opus-4", "name": "Claude Opus 4", "vendor": "Anthropic", "context_window": 200000},
    {"id": "o4-mini", "name": "o4-mini", "vendor": "OpenAI", "context_window": 200000},
    {"id": "o3-mini", "name": "o3-mini", "vendor": "OpenAI", "context_window": 200000},
]

# ── System prompt ─────────────────────────────────────────────────
# The generic SQL-assistant guidance and agent behavioural rules live in
# `app.mcp_server.system_prompt` so the GitHub-Copilot path (web UI)
# and the VSCode-stdio MCP path share a single source of truth. The
# response-format block is gated by `COPILOT_STRUCTURED_RESPONSE` so VSCode-
# style free-form answers are the default.
from app.mcp_server.system_prompt import (
    GENERIC_SQL_PROMPT,
    COPILOT_BEHAVIORAL_RULES,
    COPILOT_STRUCTURED_RESPONSE_FORMAT,
)


def _compose_system_prompt_base() -> str:
    """Compose the static portion of the system prompt at call time so the
    `COPILOT_STRUCTURED_RESPONSE` env flag can be flipped without restart."""
    parts = [GENERIC_SQL_PROMPT, COPILOT_BEHAVIORAL_RULES]
    try:
        from app.config.settings import settings as _settings
        if getattr(_settings, "copilot_structured_response", False):
            parts.append(COPILOT_STRUCTURED_RESPONSE_FORMAT)
    except Exception:
        # Settings unavailable — fall back to free-form.
        pass
    return "".join(parts)


# Back-compat alias: legacy code paths read `SYSTEM_PROMPT_BASE` directly.
# `_build_system_prompt` calls `_compose_system_prompt_base()` instead so the
# structured-response flag is re-evaluated on every request.
SYSTEM_PROMPT_BASE = _compose_system_prompt_base()


# ---------------------------------------------------------------------------
# Cross-pod / SSH cluster tooling was removed in the hackathon cleanup.
# The agent is now strictly scoped to the database the user is currently
# connected to. The helpers below are kept as no-ops so call sites that
# still reference them (e.g. ``_build_system_prompt``) keep compiling.
# ---------------------------------------------------------------------------
CROSSPOD_HINT_ENABLED = ""
CROSSPOD_HINT_DISABLED = ""
CROSSPOD_HINT_SSH_CREDS_PROVIDED = ""

_SSH_CRED_TOOLS: set[str] = set()
_SSH_CRED_FIELDS: tuple[str, ...] = ()
_SSH_SECRET_FIELDS: set[str] = set()


def _inject_ssh_credentials(
    tool_name: str,
    tool_args: Dict[str, Any],
    ssh_credentials: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """No-op -- SSH-based cluster tools were removed in the cleanup."""

    return tool_args


def _redact_ssh_args_for_log(tool_args: Dict[str, Any]) -> Dict[str, Any]:
    """No-op -- SSH-based cluster tools were removed in the cleanup."""

    return tool_args


@dataclass
class CopilotToolCall:
    """A tool call executed during the agent loop."""
    tool_name: str
    arguments: Dict[str, Any]
    result: Any = None
    success: bool = True
    error: Optional[str] = None
    execution_time_ms: float = 0.0
    reasoning: Optional[str] = None
    database: Optional[str] = None


@dataclass
class CopilotResponse:
    """Response from the Copilot agent loop."""
    success: bool
    message: str = ""
    sql: Optional[str] = None
    records: List[Dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    columns: List[str] = field(default_factory=list)
    tool_calls: List[CopilotToolCall] = field(default_factory=list)
    total_time_ms: float = 0.0
    model: str = ""
    error: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    active_database: str = ""
    # ── Verifiable Trust Layer (Phase 1) ──────────────────────────────
    # Earned trust signals computed from the tool trace (see _compute_trust).
    trust_score: int = 0
    trust_label: str = ""  # "verified" | "caution" | "unverified" | "" (n/a)
    trust_checks: List[Dict[str, Any]] = field(default_factory=list)
    verification: Optional[Dict[str, Any]] = None
    grounded_sources: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Verifiable Trust Layer (Phase 1)
#
# Every text-to-SQL agent can produce a clean-looking result that is silently
# wrong (a fan-out JOIN that double-counts, the wrong filter, a hallucinated
# column, or an ungoverned definition). These helpers turn the agent's own
# tool trace into four *earned* trust signals so the answer can be trusted:
#   1. schema_validated -- SQL was checked against the live schema
#   2. grounded         -- the answer cites >=1 governed Foundry IQ definition
#   3. cross_checked    -- a 2nd independent query confirms the headline metric
#   4. result_sane      -- the headline query returned non-empty, non-null data
# Nothing here calls the model or the database; it only inspects results that
# were already produced this turn.
# ---------------------------------------------------------------------------
def _trust_to_number(value: Any) -> Optional[float]:
    """Best-effort numeric coercion for cross-checks (int/float/Decimal/str)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from decimal import Decimal
        if isinstance(value, Decimal):
            return float(value)
    except Exception:
        pass
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _trust_scalar_from_execute(result: Any) -> Optional[float]:
    """Extract a single headline scalar from an execute_sql result.

    Only single-cell results (1 row x 1 column -- e.g. COUNT/SUM/AVG) are
    treated as verifiable headline metrics. This deliberately ignores
    multi-row results so two unrelated queries that both hit a ``LIMIT 100``
    cannot be mistaken for an "agreement".
    """
    if not isinstance(result, dict):
        return None
    records = result.get("records") or []
    if len(records) == 1 and isinstance(records[0], dict) and len(records[0]) == 1:
        return _trust_to_number(next(iter(records[0].values())))
    return None


def _trust_numeric_pair_from_execute(result: Any) -> Optional[Dict[str, Any]]:
    """Extract an in-query dual-method check from a single-row result.

    Some generated SQL verifies a metric in one statement, returning columns
    such as ``method_a`` and ``method_b`` in the same row. Treat exactly two
    numeric cells in a one-row result as a legitimate cross-check candidate.
    """
    if not isinstance(result, dict):
        return None
    records = result.get("records") or []
    if len(records) != 1 or not isinstance(records[0], dict):
        return None

    numeric_cells = []
    for key, value in records[0].items():
        number = _trust_to_number(value)
        if number is not None:
            numeric_cells.append((str(key), number))

    if len(numeric_cells) != 2:
        return None

    (primary_label, primary_value), (check_label, check_value) = numeric_cells
    return {
        "primary_label": primary_label.replace("_", " "),
        "primary_value": primary_value,
        "check_label": check_label.replace("_", " "),
        "check_value": check_value,
    }


def _trust_method_label(sql: Optional[str]) -> str:
    """Human-readable method label inferred from SQL so the cross-check trace
    is legible to a reviewer (e.g. "Haversine" vs "PostGIS geocoded")."""
    s = (sql or "").lower()
    if "st_dwithin" in s or "st_distance" in s or "::geography" in s or "st_within" in s:
        return "PostGIS geocoded"
    if "acos(" in s and "radians(" in s:
        return "Haversine formula"
    if "ilike" in s or "lower(city)" in s or "city =" in s or "city in" in s:
        return "city-label filter"
    if "join" in s and "count(" in s:
        return "JOIN-based count"
    if "count(" in s:
        return "count query"
    return "SQL query"


def _compute_trust(
    tool_calls: List[CopilotToolCall],
    sql: Optional[str] = None,
    records: Optional[List[Dict[str, Any]]] = None,
    row_count: int = 0,
    columns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Derive the Verifiable Trust Layer signals from the agent's tool trace.

    Returns a dict with: trust_score (0-100), trust_label, trust_checks[],
    verification (or None), grounded_sources[].
    """
    checks: List[Dict[str, Any]] = []

    # ── 1. Schema validation ──────────────────────────────────────────
    schema_tools = {
        "validate_sql", "search_tables", "search_columns",
        "introspect_schema", "discover_join_paths", "preview_data",
        "check_relationships",
    }
    schema_validated = False
    schema_detail = "Schema verification was not run; we should verify that."
    for tc in tool_calls:
        if not tc.success:
            continue
        if tc.tool_name == "validate_sql" and isinstance(tc.result, dict) and tc.result.get("valid"):
            schema_validated = True
            schema_detail = "Passed static + structural validation (validate_sql)"
            break
        if tc.tool_name in schema_tools:
            schema_validated = True
            schema_detail = f"Schema introspected via {tc.tool_name} before generating SQL"
    if not schema_validated:
        for tc in tool_calls:
            if tc.tool_name == "execute_sql" and tc.success:
                schema_validated = True
                schema_detail = "Executed successfully against the live database (runtime schema check)"
                break
    checks.append({"name": "Schema validated", "passed": schema_validated, "detail": schema_detail})

    # ── 2. Governed grounding (Foundry IQ) ────────────────────────────
    grounded_sources: List[Dict[str, Any]] = []
    for tc in tool_calls:
        if tc.tool_name == "retrieve_business_context" and tc.success and isinstance(tc.result, dict):
            for c in (tc.result.get("citations") or []):
                if isinstance(c, dict):
                    grounded_sources.append({
                        "title": c.get("title") or c.get("source") or "governed definition",
                        "source": c.get("source") or "",
                    })
    grounded = len(grounded_sources) > 0
    grounded_detail = (
        f"Grounded in {len(grounded_sources)} governed Foundry IQ source(s)"
        if grounded else "No governed business definition was applied"
    )
    checks.append({"name": "Governed grounding", "passed": grounded, "detail": grounded_detail})

    # ── 3. Dual-path cross-check ──────────────────────────────────────
    scalar_runs: List[tuple] = []  # (scalar_value, sql_text)
    in_query_pair: Optional[Dict[str, Any]] = None
    for tc in tool_calls:
        if tc.tool_name == "execute_sql" and tc.success and isinstance(tc.result, dict):
            scalar = _trust_scalar_from_execute(tc.result)
            if scalar is not None:
                scalar_runs.append((scalar, tc.result.get("sql")))
            if in_query_pair is None:
                in_query_pair = _trust_numeric_pair_from_execute(tc.result)
    verification: Optional[Dict[str, Any]] = None
    cross_checked = False
    if len(scalar_runs) >= 2:
        primary_value, primary_sql = scalar_runs[-2]
        check_value, check_sql = scalar_runs[-1]
        delta = abs(primary_value - check_value)
        denom = max(abs(primary_value), abs(check_value), 1.0)
        agreed = (delta / denom) <= 0.05
        cross_checked = agreed
        method_primary = _trust_method_label(primary_sql)
        method_check = _trust_method_label(check_sql)
        verification = {
            "primary_value": primary_value,
            "check_value": check_value,
            "delta": round(delta, 4),
            "agreed": agreed,
            "method_primary": method_primary,
            "method_check": method_check,
        }
        if agreed:
            cross_detail = (
                f"Independently confirmed: {method_primary} vs {method_check} "
                f"agree (\u0394 {round(delta, 2):g})"
            )
        else:
            cross_detail = (
                f"Discrepancy flagged: {method_primary}={primary_value:g} vs "
                f"{method_check}={check_value:g} (\u0394 {round(delta, 2):g})"
            )
    elif in_query_pair is not None:
        primary_value = in_query_pair["primary_value"]
        check_value = in_query_pair["check_value"]
        delta = abs(primary_value - check_value)
        denom = max(abs(primary_value), abs(check_value), 1.0)
        agreed = (delta / denom) <= 0.05
        cross_checked = agreed
        method_primary = in_query_pair["primary_label"]
        method_check = in_query_pair["check_label"]
        verification = {
            "primary_value": primary_value,
            "check_value": check_value,
            "delta": round(delta, 4),
            "agreed": agreed,
            "method_primary": method_primary,
            "method_check": method_check,
        }
        if agreed:
            cross_detail = (
                f"Confirmed inside one verification query: {method_primary} vs "
                f"{method_check} agree (\u0394 {round(delta, 2):g})"
            )
        else:
            cross_detail = (
                f"Discrepancy flagged: {method_primary}={primary_value:g} vs "
                f"{method_check}={check_value:g} (\u0394 {round(delta, 2):g})"
            )
    else:
        cross_detail = "Independent cross-check was not run; we should verify that."
    checks.append({"name": "Cross-checked", "passed": cross_checked, "detail": cross_detail})

    # ── 4. Result sanity ──────────────────────────────────────────────
    result_sane = False
    if row_count and row_count > 0:
        result_sane = True
        sane_detail = f"Query returned {row_count} row(s)"
        if records and columns:
            first_col = columns[0]
            non_null = sum(
                1 for r in records
                if isinstance(r, dict) and r.get(first_col) is not None
            )
            if non_null == 0:
                result_sane = False
                sane_detail = f"Key column '{first_col}' is null in every returned row"
    else:
        sane_detail = "Query returned no rows"
    checks.append({"name": "Result sanity", "passed": result_sane, "detail": sane_detail})

    # ── Score & label ─────────────────────────────────────────────────
    weights = {
        "Schema validated": 25,
        "Governed grounding": 25,
        "Cross-checked": 30,
        "Result sanity": 20,
    }
    score = sum(weights.get(c["name"], 0) for c in checks if c["passed"])
    if verification is not None and not verification["agreed"]:
        # An independent check ran and disagreed -- flag for review regardless
        # of the other signals. This is the live "self-catch" state.
        label = "caution"
    elif score >= 75:
        label = "verified"
    elif score >= 40:
        label = "caution"
    else:
        label = "unverified"

    return {
        "trust_score": int(score),
        "trust_label": label,
        "trust_checks": checks,
        "verification": verification,
        "grounded_sources": grounded_sources,
    }


class CopilotService:
    """
    GitHub Copilot API client with OAuth Device Flow and PAT support.
    """

    # Persist token to survive server restarts
    _TOKEN_FILE = Path(__file__).resolve().parent.parent.parent / "data" / ".copilot_token.json"
    # Persist most-recent successful live model list so a server restart doesn't
    # drop us back to the 7-item KNOWN_MODELS fallback while the API warms up.
    _MODELS_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / ".copilot_models_cache.json"

    def __init__(self):
        self._github_token: Optional[str] = None
        self._default_model: str = "claude-opus-4"
        # Copilot session token (short-lived, ~30 min)
        self._copilot_token: Optional[str] = None
        self._copilot_token_expires: int = 0
        # OAuth device flow state
        self._device_code: Optional[str] = None
        self._cached_user_code: Optional[str] = None
        self._device_code_expires: int = 0
        self._device_poll_interval: int = 5
        # Per-session conversation history
        self._sessions: Dict[str, List[Dict[str, Any]]] = {}
        # Last error from list_models (surface in UI)
        self._last_models_fetch_error: Optional[str] = None
        # In-memory copy of last successful live model fetch
        self._cached_live_models: List[Dict[str, Any]] = []
        # Concurrency guards (Phase A fixes):
        # - _token_lock: serialize Copilot token refresh to prevent thundering herd
        # - _session_locks: one asyncio.Lock per chat session_id so concurrent
        #   requests with the same session_id do not corrupt history.
        # - _session_locks_guard: protects _session_locks dict creation.
        self._token_lock: asyncio.Lock = asyncio.Lock()
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._session_locks_guard: asyncio.Lock = asyncio.Lock()
        # Try to restore saved token
        self._load_token()
        # Try to restore cached live model list (survives server restarts)
        self._load_models_cache()

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Return (creating if needed) the asyncio.Lock for this chat session."""
        async with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
                # Soft cap: evict oldest if we ever accumulate too many sessions
                if len(self._session_locks) > 1000:
                    # Drop the first inserted key (insertion order preserved)
                    oldest = next(iter(self._session_locks))
                    if oldest != session_id:
                        self._session_locks.pop(oldest, None)
            return lock

    # ── DPAPI encryption (Windows) for token-at-rest protection ──────
    @staticmethod
    def _dpapi_encrypt(data: bytes) -> bytes:
        """Encrypt bytes using Windows DPAPI (current-user scope)."""
        import ctypes, ctypes.wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        blob_in = DATA_BLOB(len(data), ctypes.create_string_buffer(data, len(data)))
        blob_out = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            raise OSError("CryptProtectData failed")
        encrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return encrypted

    @staticmethod
    def _dpapi_decrypt(data: bytes) -> bytes:
        """Decrypt DPAPI-encrypted bytes."""
        import ctypes, ctypes.wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        blob_in = DATA_BLOB(len(data), ctypes.create_string_buffer(data, len(data)))
        blob_out = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            raise OSError("CryptUnprotectData failed")
        decrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return decrypted

    @staticmethod
    def _is_windows() -> bool:
        return os.name == "nt"

    # ── AES-GCM at-rest encryption for non-Windows hosts (Phase 3) ───
    # The legacy code path wrote the GitHub OAuth token in plaintext to
    # `data/.copilot_token.json` whenever DPAPI wasn't available (i.e. any
    # Linux/macOS deployment). That token has the same blast radius as the
    # user's GitHub session — if the file leaks (backup, container snapshot,
    # accidental commit), the attacker can call Copilot on their behalf. We
    # now require an explicit 32-byte AES-256 key in the env var
    # `COPILOT_TOKEN_ENC_KEY` (base64-encoded). When the key is missing the
    # token is held in memory only and the user must re-authenticate after
    # every restart — a deliberate trade so we never silently write
    # bearer tokens to disk in the clear.
    _ENC_HEADER = b"COPILOTv1"  # marks AES-GCM-encrypted token files

    @staticmethod
    def _load_token_enc_key() -> Optional[bytes]:
        try:
            from app.config.settings import settings as _settings
            raw = (getattr(_settings, "copilot_token_enc_key", "") or "").strip()
        except Exception:
            raw = ""
        if not raw:
            return None
        try:
            import base64
            key = base64.b64decode(raw, validate=True)
        except Exception:
            logger.warning(
                "COPILOT_TOKEN_ENC_KEY is set but is not valid base64 — "
                "token will be held in memory only and not persisted."
            )
            return None
        if len(key) != 32:
            logger.warning(
                "COPILOT_TOKEN_ENC_KEY must decode to exactly 32 bytes (got %d) — "
                "token will be held in memory only and not persisted.",
                len(key),
            )
            return None
        return key

    @classmethod
    def _aesgcm_encrypt(cls, payload: bytes) -> Optional[bytes]:
        key = cls._load_token_enc_key()
        if key is None:
            return None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            aes = AESGCM(key)
            nonce = os.urandom(12)
            ct = aes.encrypt(nonce, payload, associated_data=cls._ENC_HEADER)
            return cls._ENC_HEADER + nonce + ct
        except Exception as e:
            logger.warning(f"AES-GCM encryption failed: {e}")
            return None

    @classmethod
    def _aesgcm_decrypt(cls, blob: bytes) -> Optional[bytes]:
        if not blob.startswith(cls._ENC_HEADER):
            return None
        key = cls._load_token_enc_key()
        if key is None:
            return None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            aes = AESGCM(key)
            nonce = blob[len(cls._ENC_HEADER):len(cls._ENC_HEADER) + 12]
            ct = blob[len(cls._ENC_HEADER) + 12:]
            return aes.decrypt(nonce, ct, associated_data=cls._ENC_HEADER)
        except Exception as e:
            logger.warning(f"AES-GCM decryption failed: {e}")
            return None

    def _load_token(self):
        """Load persisted GitHub OAuth token (DPAPI-encrypted on Windows)."""
        try:
            if not self._TOKEN_FILE.exists():
                return
            raw = self._TOKEN_FILE.read_bytes()

            # Try DPAPI decryption first (binary file)
            if self._is_windows():
                try:
                    decrypted = self._dpapi_decrypt(raw)
                    data = json.loads(decrypted.decode("utf-8"))
                    token = data.get("github_token", "")
                    model = data.get("default_model", "claude-opus-4")
                    if token:
                        self._github_token = token
                        self._default_model = model
                        logger.info("Restored DPAPI-encrypted GitHub OAuth token")
                        return
                except Exception:
                    pass  # Fall through to AES-GCM / plaintext migration paths

            # AES-GCM-encrypted (non-Windows preferred at-rest format).
            decrypted = self._aesgcm_decrypt(raw)
            if decrypted is not None:
                try:
                    data = json.loads(decrypted.decode("utf-8"))
                    token = data.get("github_token", "")
                    model = data.get("default_model", "claude-opus-4")
                    if token:
                        self._github_token = token
                        self._default_model = model
                        logger.info("Restored AES-GCM-encrypted GitHub OAuth token")
                        return
                except Exception as e:
                    logger.warning(f"AES-GCM token blob present but JSON parse failed: {e}")
                    return

            # Legacy plaintext file (pre-Phase 3). Read once, then — if we
            # have a way to encrypt it — migrate; otherwise wipe the
            # plaintext file so it doesn't sit on disk forever.
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                logger.warning("Token file is not DPAPI, AES-GCM, or plaintext JSON — ignoring.")
                return
            token = data.get("github_token", "")
            model = data.get("default_model", "claude-opus-4")
            if not token:
                return
            self._github_token = token
            self._default_model = model
            if self._is_windows():
                logger.info("Restored GitHub OAuth token from disk (plaintext) — migrating to DPAPI")
                self._save_token()
            elif self._load_token_enc_key() is not None:
                logger.info("Restored GitHub OAuth token from disk (plaintext) — migrating to AES-GCM")
                self._save_token()
            else:
                logger.warning(
                    "Found legacy plaintext token at %s. COPILOT_TOKEN_ENC_KEY is not set; "
                    "deleting the plaintext file. Re-authenticate via the UI to persist a new "
                    "AES-GCM-encrypted token, or set the key to keep the token across restarts.",
                    self._TOKEN_FILE,
                )
                try:
                    self._TOKEN_FILE.unlink()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Could not load saved token: {e}")

    def _save_token(self):
        """Persist GitHub OAuth token (DPAPI on Windows, AES-GCM elsewhere).
        Refuses to write plaintext: if no encryption is available the token
        stays in memory only and the user must re-authenticate after restart."""
        try:
            self._TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({
                "github_token": self._github_token or "",
                "default_model": self._default_model,
            }).encode("utf-8")

            if self._is_windows():
                encrypted = self._dpapi_encrypt(payload)
                self._TOKEN_FILE.write_bytes(encrypted)
                logger.info("Saved DPAPI-encrypted GitHub OAuth token to disk")
                return

            enc = self._aesgcm_encrypt(payload)
            if enc is None:
                logger.warning(
                    "COPILOT_TOKEN_ENC_KEY is not set — GitHub OAuth token will NOT be "
                    "written to disk. Re-authenticate after every restart, or generate "
                    "a key with: python -c \"import base64,os; print(base64.b64encode(os.urandom(32)).decode())\" "
                    "and put it in .env as COPILOT_TOKEN_ENC_KEY."
                )
                # If a legacy plaintext file is lingering, remove it now —
                # don't leave a stale bearer token sitting on disk.
                try:
                    if self._TOKEN_FILE.exists():
                        self._TOKEN_FILE.unlink()
                except Exception:
                    pass
                return

            self._TOKEN_FILE.write_bytes(enc)
            try:
                os.chmod(self._TOKEN_FILE, 0o600)
            except Exception:
                pass
            logger.info("Saved AES-GCM-encrypted GitHub OAuth token to disk (mode 600)")
        except Exception as e:
            logger.warning(f"Could not save token: {e}")

    def _load_models_cache(self) -> None:
        """Restore the last successful live model fetch from disk so we
        don't fall back to the 7-item KNOWN_MODELS list on a cold start.
        Best-effort: failures are silent."""
        try:
            if not self._MODELS_CACHE_FILE.exists():
                return
            data = json.loads(self._MODELS_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                self._cached_live_models = [m for m in data if isinstance(m, dict) and m.get("id")]
                logger.info(f"Restored {len(self._cached_live_models)} cached live models")
        except Exception as e:
            logger.warning(f"Could not load models cache: {e}")

    def _save_models_cache(self, models: List[Dict[str, Any]]) -> None:
        """Persist the latest live model fetch (best-effort, non-fatal)."""
        try:
            self._MODELS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._MODELS_CACHE_FILE.write_text(
                json.dumps(models, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Could not save models cache: {e}")

    @property
    def is_configured(self) -> bool:
        return bool(self._github_token)

    def configure(self, github_token: str, default_model: str = "claude-opus-4"):
        """Set the GitHub token (PAT or OAuth) and default model."""
        self._github_token = github_token
        if default_model:
            self._default_model = default_model
        # Invalidate cached Copilot token
        self._copilot_token = None
        self._copilot_token_expires = 0
        self._save_token()
        logger.info(f"Copilot service configured with model={self._default_model}")

    def disconnect(self):
        """Clear all tokens and remove persisted token file."""
        self._github_token = None
        self._copilot_token = None
        self._copilot_token_expires = 0
        try:
            if self._TOKEN_FILE.exists():
                self._TOKEN_FILE.unlink()
                logger.info("Deleted persisted token file")
        except Exception as e:
            logger.warning(f"Could not delete token file: {e}")

    def get_config(self) -> Dict[str, Any]:
        return {
            "configured": self.is_configured,
            "default_model": self._default_model,
            "has_token": bool(self._github_token),
        }

    # ── Cross-database connections ─────────────────────────────────

    _saved_connections: List[Dict[str, str]] = []

    def register_connections(self, connections: List[Dict[str, str]]) -> None:
        """Register saved database connections so the agent can switch between them."""
        self._saved_connections = connections
        # Also push to the switch_database tool
        from app.mcp_server.tools.switch_database import set_available_connections
        set_available_connections(connections)
        logger.info(f"Registered {len(connections)} database connections for cross-DB lookup")

    # ── OAuth Device Flow ──────────────────────────────────────────

    async def start_device_flow(self) -> Dict[str, Any]:
        """
        Step 1 of OAuth Device Flow.
        POST https://github.com/login/device/code
        Returns: { user_code, verification_uri, device_code, interval, expires_in }

        The user opens verification_uri in their browser and enters user_code.
        """
        # If a device flow is already active and not expired, return the existing code
        if self._device_code and int(time.time()) < self._device_code_expires:
            print(f"[DEVICE_FLOW] Reusing existing device flow (expires in {self._device_code_expires - int(time.time())}s)")
            return {
                "user_code": self._cached_user_code or "",
                "verification_uri": "https://github.com/login/device",
                "expires_in": self._device_code_expires - int(time.time()),
                "interval": self._device_poll_interval,
            }

        async with httpx.AsyncClient(timeout=5.0, verify=_SSL_CTX) as client:
            resp = await client.post(
                f"{GITHUB_SITE_URL}/login/device/code",
                headers={
                    "Accept": "application/json",
                },
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "scope": "user:email",
                },
            )
            print(f"[DEVICE_FLOW] /login/device/code: {resp.status_code} | {resp.text[:500]}")

            if resp.status_code != 200:
                raise Exception(f"Failed to start device flow: {resp.status_code} {resp.text[:300]}")

            data = resp.json()
            self._device_code = data.get("device_code", "")
            self._cached_user_code = data.get("user_code", "")
            self._device_code_expires = int(time.time()) + data.get("expires_in", 900)
            self._device_poll_interval = data.get("interval", 5)

            return {
                "user_code": data.get("user_code", ""),
                "verification_uri": data.get("verification_uri", "https://github.com/login/device"),
                "expires_in": data.get("expires_in", 900),
                "interval": self._device_poll_interval,
            }

    async def poll_device_flow(self) -> Dict[str, Any]:
        """
        Step 2 of OAuth Device Flow.
        POST https://github.com/login/oauth/access_token
        Polls until the user has approved or the code expires.

        Returns: { status: 'pending' | 'complete' | 'expired', ... }
        """
        if not self._device_code:
            raise Exception("No device flow in progress. Call start_device_flow first.")

        if int(time.time()) > self._device_code_expires:
            self._device_code = None
            return {"status": "expired"}

        async with httpx.AsyncClient(timeout=5.0, verify=_SSL_CTX) as client:
            try:
                resp = await client.post(
                    f"{GITHUB_SITE_URL}/login/oauth/access_token",
                    headers={
                        "Accept": "application/json",
                    },
                    data={
                        "client_id": GITHUB_CLIENT_ID,
                        "device_code": self._device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                )
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                # Network flaky — treat as "pending" so UI keeps polling
                logger.debug(f"[DEVICE_FLOW] Poll network error ({type(e).__name__}), treating as pending")
                return {"status": "pending", "network_error": True}
            print(f"[DEVICE_FLOW] Poll response: {resp.status_code} | {resp.text[:500]}")

            if resp.status_code != 200:
                raise Exception(f"Poll failed: {resp.status_code} {resp.text[:300]}")

            data = resp.json()
            error = data.get("error", "")

            if error == "authorization_pending":
                return {"status": "pending"}
            elif error == "slow_down":
                # Use the interval GitHub tells us, not our own cumulative value
                new_interval = data.get("interval", self._device_poll_interval + 5)
                self._device_poll_interval = new_interval
                return {"status": "pending", "interval": new_interval}
            elif error == "expired_token":
                self._device_code = None
                return {"status": "expired"}
            elif error == "access_denied":
                self._device_code = None
                return {"status": "denied"}
            elif error:
                return {"status": "error", "error": data.get("error_description", error)}

            # Success! We got an access token
            access_token = data.get("access_token", "")
            if access_token:
                self._device_code = None
                self._cached_user_code = None
                # Store the OAuth token as our GitHub token
                self.configure(access_token, self._default_model)
                print(f"[DEVICE_FLOW] ✅ OAuth complete — access_token acquired (len={len(access_token)})")

                # Verify it works by doing the Copilot token exchange
                try:
                    await self._get_copilot_token()
                    return {"status": "complete"}
                except Exception as e:
                    logger.error(f"Copilot token exchange after OAuth failed: {e}")
                    return {
                        "status": "complete",
                        "warning": str(e),
                    }

            return {"status": "error", "error": "No access token in response"}

    # ── Copilot Token Exchange ─────────────────────────────────────

    async def _get_copilot_token(self) -> str:
        """
        Exchange GitHub token for a short-lived Copilot session token.
        GET https://api.github.com/copilot_internal/v2/token
        Authorization: token <github_token>
        """
        now = int(time.time())

        # Fast-path: cached token still valid (60s buffer) — no lock needed
        if self._copilot_token and self._copilot_token_expires > (now + 60):
            return self._copilot_token

        if not self._github_token:
            raise Exception("GitHub token not configured")

        # Serialize refresh so N concurrent callers don't all hit GitHub.
        async with self._token_lock:
            # Re-check inside the lock: another coroutine may have just refreshed.
            now = int(time.time())
            if self._copilot_token and self._copilot_token_expires > (now + 60):
                return self._copilot_token
            return await self._do_token_exchange(now)

    async def _do_token_exchange(self, now: int) -> str:
        """Actual HTTP token exchange. Caller must hold _token_lock."""
        async with httpx.AsyncClient(timeout=5.0, verify=_SSL_CTX) as client:
            logger.info(f"Token exchange: GET {GITHUB_API_URL}/copilot_internal/v2/token")
            try:
                resp = await client.get(
                    f"{GITHUB_API_URL}/copilot_internal/v2/token",
                    headers={
                        "Authorization": f"token {self._github_token}",
                        "Accept": "application/json",
                        "User-Agent": "GitHubCopilotChat/0.25.2024",
                    },
                )
            except httpx.ConnectError as e:
                err = str(e)
                if "getaddrinfo" in err or "Name or service not known" in err:
                    raise Exception(
                        "Cannot resolve api.github.com (DNS failure). "
                        "Check VPN/proxy — GitHub APIs must be reachable from this network."
                    ) from e
                raise Exception(f"Cannot connect to api.github.com: {err}") from e
            except httpx.TimeoutException:
                raise Exception(
                    "Connection to api.github.com timed out. Check VPN/network connectivity."
                )
            logger.info(
                f"Token exchange response: {resp.status_code} | "
                f"body={resp.text[:500]}"
            )

            if resp.status_code == 200:
                data = resp.json()
                self._copilot_token = data.get("token", "")
                self._copilot_token_expires = data.get("expires_at", 0)
                logger.info(
                    f"Copilot token acquired, expires in "
                    f"{self._copilot_token_expires - now}s"
                )
                return self._copilot_token

            elif resp.status_code == 401:
                body = resp.text[:500]
                logger.error(f"Token exchange 401: {body}")
                raise Exception(
                    f"GitHub token rejected (401): {body}. "
                    "Try signing in again via the GitHub OAuth flow."
                )
            elif resp.status_code == 403:
                raise Exception(
                    "Your GitHub account doesn't have an active Copilot subscription. "
                    "Check your subscription at github.com/settings/copilot"
                )
            else:
                body = resp.text[:500]
                raise Exception(
                    f"Copilot token exchange failed ({resp.status_code}): {body}"
                )

    # ── Model listing ──────────────────────────────────────────────

    async def list_models(self) -> List[Dict[str, Any]]:
        """Fetch available models from the Copilot API.

        Resilience strategy:
          1. If no GitHub token is configured, return last cached live list
             (if any), else KNOWN_MODELS.
          2. Try GET /models with a 15s timeout. On 401/403, force a fresh
             Copilot session token and retry once.
          3. On any success, persist the live list to disk so the next cold
             start serves it immediately instead of the 7-item fallback.
          4. On any failure, return last cached live list (if any), else
             KNOWN_MODELS, and remember the error for the UI to display.
        """
        # No token \u2192 fall back without hitting the API
        if not self._github_token:
            self._last_models_fetch_error = "GitHub token not configured"
            return list(self._cached_live_models) if self._cached_live_models else list(KNOWN_MODELS)

        last_err: Optional[str] = None
        for attempt in (1, 2):
            try:
                if attempt == 2:
                    # Force a fresh Copilot session token on retry
                    self._copilot_token = None
                    self._copilot_token_expires = 0
                copilot_token = await self._get_copilot_token()
                async with httpx.AsyncClient(timeout=15.0, verify=_SSL_CTX) as client:
                    resp = await client.get(
                        f"{COPILOT_API_URL}/models",
                        headers={
                            "Authorization": f"Bearer {copilot_token}",
                            **COPILOT_HEADERS,
                        },
                    )

                if resp.status_code in (401, 403) and attempt == 1:
                    last_err = f"HTTP {resp.status_code} (auth) \u2014 retrying with fresh token"
                    logger.info(f"Copilot /models {resp.status_code}; refreshing session token and retrying")
                    continue

                if resp.status_code != 200:
                    last_err = f"Copilot /models returned HTTP {resp.status_code}"
                    logger.warning(last_err)
                    break

                data = resp.json()
                model_list = data.get("data", data) if isinstance(data, dict) else data
                models: List[Dict[str, Any]] = []
                if isinstance(model_list, list):
                    for m in model_list:
                        if not isinstance(m, dict):
                            continue
                        mid = m.get("id", m.get("name", ""))
                        mid = self._clean_model_id(mid)
                        if not mid:
                            continue
                        name = m.get("name", m.get("friendly_name", mid))
                        if isinstance(name, str) and name.startswith("azureml://"):
                            name = mid
                        if not isinstance(name, str):
                            name = mid
                        # Skip non-chat models
                        name_check = (mid + " " + name).lower()
                        if any(kw in name_check for kw in (
                            "embed", "embedding", "whisper", "tts",
                            "dall-e", "jais", "safety", "moderation",
                            "codex", "code-",
                        )):
                            continue
                        # Check model capabilities - skip if not chat-compatible
                        caps = m.get("capabilities", {})
                        if isinstance(caps, dict):
                            cap_type = caps.get("type", "")
                            if cap_type and cap_type != "chat":
                                continue
                        vendor = m.get("publisher") or m.get("owned_by") or ""
                        if not isinstance(vendor, str):
                            vendor = ""
                        if not vendor or vendor.lower() in ("unknown", "azureml", "azure"):
                            vendor = self._infer_vendor(mid, name)
                        # NOTE: do NOT overwrite the display name with
                        # caps["family"] — that collapses every version of a
                        # family (gpt-4o, gpt-4o-mini, dated snapshots) to one
                        # label and hides the individual models. Keep the
                        # API's distinct friendly name instead.
                        ctx = 0
                        limits = m.get("model_limits", {})
                        if isinstance(limits, dict):
                            ctx = limits.get("max_context_window", 0) or 0
                        models.append({
                            "id": mid,
                            "name": name,
                            "vendor": vendor,
                            "context_window": ctx,
                        })

                if models:
                    # Deduplicate by model id ONLY. The API can return literal
                    # duplicate ids, but every distinct id is a distinct model
                    # we want to show (all versions/variants of a family).
                    # When two distinct ids share the same friendly name, the
                    # display label is disambiguated with the id so the picker
                    # still lists every model.
                    _seen_ids: set = set()
                    _used_names: set = set()
                    _deduped: List[Dict[str, Any]] = []
                    for _m in models:
                        _id_key = (_m.get("id") or "").lower()
                        if not _id_key or _id_key in _seen_ids:
                            continue
                        _seen_ids.add(_id_key)
                        _nm = _m.get("name") or _m.get("id")
                        if _nm in _used_names:
                            _m["name"] = f"{_nm} ({_m.get('id')})"
                        _used_names.add(_nm)
                        _deduped.append(_m)
                    models = _deduped
                    # Stable sort: vendor, then by name
                    models.sort(key=lambda x: (x.get("vendor", ""), x.get("name", "")))
                    self._cached_live_models = models
                    self._last_models_fetch_error = None
                    self._save_models_cache(models)
                    logger.info(f"Loaded {len(models)} models from Copilot API")
                    return models

                last_err = "Copilot API returned 200 but produced 0 chat models after filtering"
                logger.warning(last_err)
                break

            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                logger.warning(f"Failed to fetch Copilot models (attempt {attempt}): {last_err}")
                if attempt == 1:
                    continue
                break

        # Live fetch failed \u2014 fall back to last good cache, else KNOWN_MODELS
        self._last_models_fetch_error = last_err
        if self._cached_live_models:
            logger.info(f"Serving {len(self._cached_live_models)} cached live models (live fetch failed)")
            return list(self._cached_live_models)
        return list(KNOWN_MODELS)

    @staticmethod
    def _clean_model_id(raw_id: str) -> str:
        """Extract clean model ID from Azure ML URIs."""
        if not raw_id:
            return ""
        m = re.search(r'/models/([^/]+)', raw_id)
        if m:
            return m.group(1)
        return raw_id

    @staticmethod
    def _infer_vendor(model_id: str, name: str = "") -> str:
        """Infer the vendor from the model id/name when the Copilot API
        doesn't return a `publisher` or `owned_by` field. Falls back to
        ``"Other"`` for unrecognised models so the UI doesn't dump
        everything into a generic "Unknown" group.
        """
        s = f"{model_id} {name}".lower()
        # Order matters: check more specific tokens before generic ones.
        if "claude" in s or "anthropic" in s:
            return "Anthropic"
        if "gemini" in s or "bison" in s or "palm" in s or s.startswith("google"):
            return "Google"
        if "grok" in s:
            return "xAI"
        if "llama" in s or "meta-" in s:
            return "Meta"
        if "mistral" in s or "mixtral" in s or "codestral" in s:
            return "Mistral"
        if "phi" in s or "deepseek" in s.split() or s.startswith("deepseek"):
            return "DeepSeek" if "deepseek" in s else "Microsoft"
        if "command" in s or "cohere" in s:
            return "Cohere"
        if (
            s.startswith("gpt")
            or "gpt-" in s
            or s.startswith("o1")
            or s.startswith("o3")
            or s.startswith("o4")
            or "openai" in s
        ):
            return "OpenAI"
        return "Other"

    # ── MCP tools -> OpenAI function definitions ───────────────────

    # Tools the model should NOT see (we handle connection and conversation
    # context internally — those are stateful infrastructure, not LLM-callable
    # actions). `check_db_integrity` USED to be excluded as a long-running
    # admin probe, but the schema-driven gating in the tool itself plus the
    # bumped agent wall-clock budget make it safe to expose for parity with
    # VSCode Copilot Chat.
    _EXCLUDED_TOOLS = {
        "connect_database",
        "get_conversation_context",
    }

    def _get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Convert MCP tools into OpenAI function-calling format."""
        mcp = get_mcp_server()
        tools = mcp.list_tools()
        definitions = []
        for t in tools:
            # Skip tools the model shouldn't call directly
            if t["name"] in self._EXCLUDED_TOOLS:
                continue
            schema = t.get("inputSchema", {})
            props = schema.get("properties", {})
            required = [r for r in schema.get("required", []) if r != "session_id"]
            cleaned_props = {}
            for k, v in props.items():
                # Hide session_id — we inject it automatically
                if k == "session_id":
                    continue
                cleaned_props[k] = {
                    "type": v.get("type", "string"),
                    "description": v.get("description", ""),
                }
                if "enum" in v:
                    cleaned_props[k]["enum"] = v["enum"]
                if "default" in v:
                    cleaned_props[k]["default"] = v["default"]
                # OpenAI requires 'items' for array types
                if v.get("type") == "array":
                    cleaned_props[k]["items"] = v.get("items", {"type": "string"})
            definitions.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": {
                        "type": "object",
                        "properties": cleaned_props,
                        "required": required,
                    },
                },
            })
        return definitions

    def _build_system_prompt(
        self,
        current_db_session_id: str = "",
        cross_pod_enabled: bool = False,
        ssh_credentials_present: bool = False,
    ) -> str:
        """Build system prompt with available database connections + cross-pod policy."""
        prompt = _compose_system_prompt_base()
        if self._saved_connections:
            db_lines = []
            for c in self._saved_connections:
                db_lines.append(
                    f"  - **{c.get('name', '')}**: {c.get('database', '')} "
                    f"on {c.get('hostname', '')}:{c.get('port', '')}"
                )
            prompt += (
                "\n\n## Available database connections:\n"
                "You can switch between these databases using `switch_database` tool.\n"
                + "\n".join(db_lines)
                + "\n\nIf a table is not found, try switching to a different database."
            )
        if not current_db_session_id:
            prompt += (
                "\n\n## ⚠ NO DATABASE CONNECTED\n"
                "There is no active database session. You MUST call `list_available_databases` "
                "first, then `switch_database` to connect before running any queries. "
                "Do NOT tell the user to go to Settings — connect automatically."
            )
        # Cross-pod federation policy (opt-in, see CROSSPOD_HINT_* docs).
        prompt += CROSSPOD_HINT_ENABLED if cross_pod_enabled else CROSSPOD_HINT_DISABLED
        if cross_pod_enabled and ssh_credentials_present:
            prompt += CROSSPOD_HINT_SSH_CREDS_PROVIDED
        return prompt

    # ── Agent loop ─────────────────────────────────────────────────

    async def chat(
        self,
        session_id: str,
        message: str,
        db_session_id: str = "",
        model: Optional[str] = None,
        cross_pod_enabled: bool = False,
        ssh_credentials: Optional[Dict[str, Any]] = None,
    ) -> CopilotResponse:
        """Public entry point. Serializes calls per chat session_id (Phase A3)
        so concurrent requests on the same session can't corrupt history.

        ``cross_pod_enabled`` opts the agent into fan-out queries via
        ``query_across_databases`` (see CROSSPOD_HINT_*).

        ``ssh_credentials`` (optional) is a dict with keys ssh_host,
        ssh_port, ssh_username, ssh_password, sudo_password, kubeconfig_path,
        use_sudo. When supplied, the agent can call the *_via_ssh MCP tools
        without ever seeing the password — these fields are auto-injected at
        dispatch time. Never persisted; lives only for this chat() call.
        """
        if not self._github_token:
            return CopilotResponse(success=False, error="Not signed in. Click 'Sign in with GitHub' first.")

        session_lock = await self._get_session_lock(session_id)
        if session_lock.locked():
            return CopilotResponse(
                success=False,
                error="Another request is already running for this chat session. Please wait for it to finish.",
            )
        async with session_lock:
            return await self._chat_impl(
                session_id=session_id,
                message=message,
                db_session_id=db_session_id,
                model=model,
                cross_pod_enabled=cross_pod_enabled,
                ssh_credentials=ssh_credentials,
            )

    async def _chat_impl(
        self,
        session_id: str,
        message: str,
        db_session_id: str = "",
        model: Optional[str] = None,
        cross_pod_enabled: bool = False,
        ssh_credentials: Optional[Dict[str, Any]] = None,
    ) -> CopilotResponse:
        """
        Run the Copilot agent loop:
        1. Exchange GitHub token -> Copilot session token
        2. Send user message + MCP tools to api.githubcopilot.com
        3. If model calls a tool, execute it and feed result back
        4. Repeat until model produces a final text response
        """
        if not self._github_token:
            return CopilotResponse(success=False, error="Not signed in. Click 'Sign in with GitHub' first.")

        model_id = model or self._default_model
        start = time.perf_counter()
        tool_calls_made: List[CopilotToolCall] = []
        total_usage: Dict[str, int] = {}
        active_db_name: str = ""  # Track which database is active (e.g. "analytics@db-host")

        print(f"\n{'='*80}")
        print(f"[COPILOT] Starting Copilot Agent Loop")
        print(f"[COPILOT] Model: {model_id}")
        print(f"[COPILOT] Session: {session_id}")
        print(f"[COPILOT] DB Session: {db_session_id or 'None'}")
        print(f"[COPILOT] User: '{message[:120]}'")
        print(f"{'='*80}")

        # Get Copilot session token (auto-refreshes if expired)
        try:
            copilot_token = await self._get_copilot_token()
            print(f"[COPILOT] ✔ Token acquired (expires: {self._copilot_token_expires})")
        except Exception as e:
            print(f"[COPILOT] ✖ Token acquisition FAILED: {e}")
            return CopilotResponse(success=False, error=str(e))

        # Get or create session history. Always refresh the system prompt so
        # any per-request policy changes take effect on the next user turn
        # (the rest of the system prompt is identical and reuses prior cache).
        ssh_creds_present = bool(ssh_credentials and ssh_credentials.get("ssh_host"))
        if session_id not in self._sessions:
            self._sessions[session_id] = [{
                "role": "system",
                "content": self._build_system_prompt(db_session_id, cross_pod_enabled, ssh_creds_present),
            }]
        else:
            self._sessions[session_id][0] = {
                "role": "system",
                "content": self._build_system_prompt(db_session_id, cross_pod_enabled, ssh_creds_present),
            }
        history = self._sessions[session_id]
        history.append({"role": "user", "content": message})

        tool_defs = self._get_tool_definitions()
        mcp = get_mcp_server()
        # Bound the agent loop. Configurable via
        # COPILOT_AGENT_MAX_ITERATIONS / COPILOT_AGENT_WALL_CLOCK_SECONDS.
        # Generous defaults suit multi-step schema exploration where
        # Claude occasionally returns empty tool_calls and burns a few extra
        # round-trips before emitting a real call.
        from app.config.settings import settings as _agent_settings
        max_iterations = max(1, int(_agent_settings.copilot_agent_max_iterations))
        _budget_seconds = float(_agent_settings.copilot_agent_wall_clock_seconds)
        wall_clock_deadline = time.monotonic() + _budget_seconds
        print(f"[COPILOT] Available tools: {len(tool_defs)}")
        for td in tool_defs:
            print(f"[COPILOT]   - {td['function']['name']}")

        headers = {
            "Authorization": f"Bearer {copilot_token}",
            "Content-Type": "application/json",
            **COPILOT_HEADERS,
        }

        logger.info(f"Copilot chat: model={model_id}")

        try:
            async with httpx.AsyncClient(timeout=180.0, verify=_SSL_CTX) as client:
                for iteration in range(max_iterations):
                    # Hard wall-clock budget
                    if time.monotonic() > wall_clock_deadline:
                        print(f"[COPILOT] ✖ Wall-clock budget ({_budget_seconds:.0f}s) exceeded at iteration {iteration}")
                        return CopilotResponse(
                            success=False,
                            error=(
                                f"Agent loop exceeded {_budget_seconds:.0f}s wall-clock budget. "
                                "Increase COPILOT_AGENT_WALL_CLOCK_SECONDS or simplify the request."
                            ),
                            model=model_id,
                            total_time_ms=round((time.perf_counter() - start) * 1000, 1),
                        )
                    # Refresh token if expired mid-conversation
                    if self._copilot_token_expires < int(time.time()) + 30:
                        copilot_token = await self._get_copilot_token()
                        headers["Authorization"] = f"Bearer {copilot_token}"

                    payload: Dict[str, Any] = {
                        "model": model_id,
                        "messages": history,
                        "temperature": 0.1,
                        "max_tokens": 4096,
                    }
                    if tool_defs:
                        payload["tools"] = tool_defs
                        payload["tool_choice"] = "auto"

                    print(f"\n[COPILOT] ── Iteration {iteration + 1}/{max_iterations} ──")
                    print(f"[COPILOT] → Sending {len(history)} messages to LLM...")
                    llm_start = time.perf_counter()

                    resp = await client.post(
                        f"{COPILOT_API_URL}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    llm_elapsed = (time.perf_counter() - llm_start) * 1000

                    if resp.status_code != 200:
                        error_text = resp.text
                        print(f"[COPILOT] ✖ API error {resp.status_code} ({llm_elapsed:.0f}ms)")
                        print(f"[COPILOT]   {error_text[:300]}")
                        logger.error(f"Copilot API error {resp.status_code}: {error_text}")
                        return CopilotResponse(
                            success=False,
                            error=f"API error {resp.status_code}: {error_text[:500]}",
                            model=model_id,
                            tool_calls=tool_calls_made,
                        )

                    data = resp.json()
                    print(f"[COPILOT] ← LLM responded ({llm_elapsed:.0f}ms)")

                    if "usage" in data:
                        for k, v in data["usage"].items():
                            if isinstance(v, (int, float)):
                                total_usage[k] = total_usage.get(k, 0) + v
                            else:
                                total_usage[k] = v
                        print(f"[COPILOT]   Tokens: prompt={total_usage.get('prompt_tokens', '?')}, completion={total_usage.get('completion_tokens', '?')}")

                    choice = data["choices"][0]
                    assistant_msg = choice["message"]
                    finish_reason = choice.get("finish_reason", "unknown")
                    print(f"[COPILOT]   Finish reason: {finish_reason}")

                    # Capture reasoning text from the assistant message
                    reasoning_text = (assistant_msg.get("content") or "").strip() or None
                    if reasoning_text:
                        print(f"[COPILOT]   Reasoning: {reasoning_text[:150]}{'...' if len(reasoning_text or '') > 150 else ''}")

                    if assistant_msg.get("tool_calls"):
                        tc_list = assistant_msg["tool_calls"]
                        print(f"[COPILOT]   Tool calls requested: {len(tc_list)}")
                        for _i, _tc in enumerate(tc_list):
                            _fn = _tc.get("function", {})
                            print(f"[COPILOT]     [{_i+1}] {_fn.get('name', '?')}({str(_fn.get('arguments', ''))[:100]})")
                        history.append(assistant_msg)

                        # Attach reasoning only to the first tool call in this batch
                        first_in_batch = True
                        for tc in assistant_msg["tool_calls"]:
                            fn = tc["function"]
                            tool_name = fn["name"]
                            try:
                                tool_args = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
                            except json.JSONDecodeError:
                                tool_args = {}

                            # Auto-inject session_id for DB-dependent tools.
                            # generate_sql and fix_sql are pure SQL builders / LLM helpers
                            # and do NOT accept a session_id kwarg.
                            _SESSION_TOOLS = {
                                "execute_sql",
                                "preview_data", "sample_column_values",
                                "introspect_schema", "discover_join_paths",
                                "get_connection_profile",
                                "analyze_connection_performance",
                                "validate_server_compatibility",
                                "detect_extensions", "semantic_data_search",
                                "search_tables", "search_columns",
                            }
                            if tool_name in _SESSION_TOOLS:
                                if "session_id" not in tool_args and db_session_id:
                                    tool_args["session_id"] = db_session_id
                            if tool_name in {"execute_sql", "validate_sql"} and isinstance(tool_args.get("sql"), str):
                                tool_args["sql"] = normalize_readonly_sql(tool_args["sql"])
                            if tool_name == "validate_server_compatibility" and isinstance(tool_args.get("sql_query"), str):
                                tool_args["sql_query"] = normalize_readonly_sql(tool_args["sql_query"])

                            # Auto-inject SSH credentials for *_via_ssh tools so
                            # the LLM never sees the password.
                            tool_args = _inject_ssh_credentials(tool_name, tool_args, ssh_credentials)

                            tool_start = time.perf_counter()
                            print(f"[COPILOT]   ▶ Executing tool: {tool_name}")
                            # Show key args (hide session_id for brevity, redact SSH secrets)
                            _safe_args = _redact_ssh_args_for_log(tool_args)
                            _display_args = {k: (str(v)[:80] + '...' if len(str(v)) > 80 else v) for k, v in _safe_args.items() if k != 'session_id'}
                            if _display_args:
                                print(f"[COPILOT]     Args: {json.dumps(_display_args, default=str)[:200]}")
                            try:
                                # MCP tools run sync (psycopg2 + SQLAlchemy). Off-load to a
                                # worker thread so long-running tools do NOT block the event
                                # loop and starve other concurrent requests (UI streams, etc).
                                result = await asyncio.to_thread(mcp.call_tool, tool_name, tool_args)
                                tool_elapsed = (time.perf_counter() - tool_start) * 1000
                                # Log result summary
                                if result.success and result.result:
                                    _r = result.result
                                    _summary = ""
                                    if isinstance(_r, dict):
                                        if "row_count" in _r:
                                            _summary = f"rows={_r['row_count']}"
                                        elif "tables" in _r and isinstance(_r["tables"], list):
                                            _summary = f"tables={len(_r['tables'])}"
                                        elif "sql" in _r:
                                            _summary = f"sql={str(_r['sql'])[:80]}..."
                                        elif "valid" in _r:
                                            _summary = f"valid={_r['valid']}"
                                        elif "columns" in _r and isinstance(_r["columns"], list):
                                            _summary = f"columns={len(_r['columns'])}"
                                        elif "relationships" in _r:
                                            _rels = _r["relationships"]
                                            _summary = f"relationships={len(_rels) if isinstance(_rels, list) else _rels}"
                                        elif "explanation" in _r:
                                            _summary = f"explanation={str(_r['explanation'])[:80]}..."
                                        elif "values" in _r:
                                            _vals = _r["values"]
                                            _summary = f"values={len(_vals) if isinstance(_vals, list) else _vals}"
                                    print(f"[COPILOT]   ✔ {tool_name} -> OK ({tool_elapsed:.0f}ms) {_summary}")
                                elif not result.success:
                                    print(f"[COPILOT]   ✖ {tool_name} -> FAIL ({tool_elapsed:.0f}ms) error={result.error}\")")
                                else:
                                    print(f"[COPILOT]   ✔ {tool_name} -> OK ({tool_elapsed:.0f}ms)")
                                tool_call = CopilotToolCall(
                                    tool_name=tool_name,
                                    arguments=_redact_ssh_args_for_log(tool_args),
                                    result=result.result,
                                    success=result.success,
                                    error=result.error,
                                    execution_time_ms=round(tool_elapsed, 1),
                                    reasoning=reasoning_text if first_in_batch else None,
                                    database=active_db_name or None,
                                )
                            except Exception as e:
                                tool_elapsed = (time.perf_counter() - tool_start) * 1000
                                print(f"[COPILOT]   ✖ {tool_name} -> EXCEPTION ({tool_elapsed:.0f}ms) {type(e).__name__}: {e}")
                                tool_call = CopilotToolCall(
                                    tool_name=tool_name,
                                    arguments=_redact_ssh_args_for_log(tool_args),
                                    success=False,
                                    error=str(e),
                                    execution_time_ms=round(tool_elapsed, 1),
                                    reasoning=reasoning_text if first_in_batch else None,
                                    database=active_db_name or None,
                                )
                            first_in_batch = False

                            tool_calls_made.append(tool_call)

                            # If switch_database succeeded, update db_session_id
                            # so subsequent tools use the new database
                            if (
                                tool_name == "switch_database"
                                and tool_call.success
                                and tool_call.result
                                and tool_call.result.get("session_id")
                            ):
                                db_session_id = tool_call.result["session_id"]
                                active_db_name = (
                                    f"{tool_call.result.get('database', '?')}"
                                    f"@{tool_call.result.get('hostname', '?')}"
                                )
                                # Update the database on this tool call too
                                tool_call.database = active_db_name
                                logger.info(
                                    f"Switched database to {active_db_name} "
                                    f"(new session: {db_session_id[:12]}…)"
                                )

                            result_content = (
                                json.dumps(tool_call.result)
                                if tool_call.result
                                else (tool_call.error or "No result")
                            )
                            history.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": result_content[:30000],
                            })
                            print(f"[COPILOT]     → Fed {len(result_content[:30000])} chars back to LLM")
                            logger.info(
                                f"Tool: {tool_name} -> "
                                f"{'OK' if tool_call.success else 'FAIL'} "
                                f"({tool_elapsed:.0f}ms)"
                            )

                        continue

                    # Final text response
                    final_text = assistant_msg.get("content", "")
                    history.append({"role": "assistant", "content": final_text})

                    # Auto-continue heuristic.
                    #
                    # Trust the LLM when it says it's done (finish_reason='stop'
                    # AND no tool_calls). Re-prompting a finished answer with
                    # "continue, run the query" makes the model hallucinate work
                    # for conversational inputs like "hi" or "thanks" and burns
                    # tokens.
                    #
                    # Force exactly ONE continue only when:
                    #   * the model was cut off (finish_reason='length'), OR
                    #   * the model explicitly promised an action but didn't
                    #     actually call a tool (e.g. "let me run that query").
                    final_lower = final_text.lower().strip()
                    promised_action = bool(re.search(
                        r"\b(let me (?:run|execute|fetch|query|pull|connect|check|look|try)"
                        r"|i'?ll (?:run|execute|fetch|query|pull|connect|go ahead|check|look|try)"
                        r"|now (?:i'?ll|i will|let me)"
                        r"|going to (?:run|execute|fetch|query|check)"
                        r"|connecting (?:to|and)"
                        r"|one moment|hold on|stand by)\b",
                        final_lower,
                    ))
                    is_final = (
                        finish_reason == "stop" and not promised_action
                    ) or finish_reason in ("content_filter",)

                    # C8: never re-prompt for greetings / acks; there is no
                    # query to execute and continuing wastes tokens.
                    if _looks_conversational(message):
                        is_final = True

                    if not is_final and iteration < max_iterations - 2:
                        # Sharper nudge when the model promised tool_calls but
                        # returned an empty tool_calls array (a known Claude
                        # quirk via the Copilot proxy). Without this, the
                        # loop wastes 3-4 LLM round-trips on preface text
                        # like "Let me find ..." before a real tool call.
                        if finish_reason == "tool_calls":
                            nudge = (
                                "You indicated a tool call but the tool_calls array was empty. "
                                "Call the appropriate MCP tool NOW (no preface, no commentary). "
                                "If no tool is needed, give the final answer."
                            )
                        else:
                            nudge = "Continue. Execute the query and show me the results."
                        print(f"[COPILOT]   Auto-continuing (finish={finish_reason}, promised_action={promised_action}, {len(final_text)} chars)")
                        print(f"[COPILOT]   Text: {final_text[:120]}...")
                        history.append({
                            "role": "user",
                            "content": nudge,
                        })
                        logger.info(f"Auto-continuing agent iteration {iteration} (model said: {final_text[:80]}...)")
                        continue

                    elapsed = (time.perf_counter() - start) * 1000
                    print(f"\n[COPILOT] {'='*60}")
                    print(f"[COPILOT] ✔ FINAL RESPONSE")
                    print(f"[COPILOT]   Iterations: {iteration + 1}")
                    print(f"[COPILOT]   Tools called: {len(tool_calls_made)}")
                    for _tc in tool_calls_made:
                        _status = "✔" if _tc.success else "✖"
                        print(f"[COPILOT]     {_status} {_tc.tool_name} ({_tc.execution_time_ms:.0f}ms)")
                    print(f"[COPILOT]   Response: {len(final_text)} chars")
                    print(f"[COPILOT]   Total elapsed: {elapsed:.0f}ms")
                    if total_usage:
                        print(f"[COPILOT]   Token usage: {total_usage}")
                    print(f"[COPILOT]   Preview: {final_text[:200]}{'...' if len(final_text) > 200 else ''}")
                    print(f"[COPILOT] {'='*60}\n")

                    sql = None
                    records = []
                    columns = []
                    row_count = 0
                    for tc in tool_calls_made:
                        if tc.tool_name == "generate_sql" and tc.success and tc.result:
                            sql = tc.result.get("sql", sql)
                        if tc.tool_name == "execute_sql" and tc.success and tc.result:
                            sql = tc.result.get("sql", sql)
                            records = tc.result.get("records", [])
                            columns = tc.result.get("columns", [])
                            row_count = tc.result.get("row_count", len(records))

                    # Trim history but preserve tool_call/tool response pairs
                    if len(history) > 40:
                        trimmed = [history[0]]  # keep system prompt
                        tail = history[-24:]  # take more to be safe
                        # Ensure we don't start with an orphan 'tool' message
                        start_idx = 0
                        for i, msg in enumerate(tail):
                            if msg.get("role") == "tool":
                                start_idx = i + 1  # skip orphan tool messages
                            else:
                                break
                        trimmed.extend(tail[start_idx:])
                        self._sessions[session_id] = trimmed

                    # Compute estimated_cost using query_log_service pricing
                    if total_usage and "estimated_cost" not in total_usage:
                        from app.services.query_log_service import query_log_service
                        total_usage["estimated_cost"] = query_log_service._calculate_estimated_cost(
                            {**total_usage, "model": model_id}
                        )

                    # ── Verifiable Trust Layer: earned trust signals ──
                    _has_data = bool(sql) or any(
                        tc.tool_name == "execute_sql" for tc in tool_calls_made
                    )
                    _trust = _compute_trust(
                        tool_calls_made, sql=sql, records=records,
                        row_count=row_count, columns=columns,
                    ) if _has_data else {}

                    return CopilotResponse(
                        success=True,
                        message=final_text,
                        sql=sql,
                        records=records[:200],
                        row_count=row_count,
                        columns=columns,
                        tool_calls=tool_calls_made,
                        total_time_ms=round(elapsed, 1),
                        model=model_id,
                        usage=total_usage,
                        active_database=active_db_name,
                        trust_score=_trust.get("trust_score", 0),
                        trust_label=_trust.get("trust_label", ""),
                        trust_checks=_trust.get("trust_checks", []),
                        verification=_trust.get("verification"),
                        grounded_sources=_trust.get("grounded_sources", []),
                    )

                elapsed = (time.perf_counter() - start) * 1000
                return CopilotResponse(
                    success=False,
                    error=f"Agent loop exceeded {max_iterations} iterations",
                    tool_calls=tool_calls_made,
                    total_time_ms=round(elapsed, 1),
                    model=model_id,
                )

        except httpx.TimeoutException:
            elapsed = (time.perf_counter() - start) * 1000
            return CopilotResponse(
                success=False,
                error="Request timed out",
                tool_calls=tool_calls_made,
                total_time_ms=round(elapsed, 1),
                model=model_id,
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            logger.error(f"Copilot chat error: {e}", exc_info=True)
            return CopilotResponse(
                success=False,
                error=str(e),
                tool_calls=tool_calls_made,
                total_time_ms=round(elapsed, 1),
                model=model_id,
            )

    def clear_session(self, session_id: str):
        """Clear conversation history for a session."""
        self._sessions.pop(session_id, None)

    # ── Streaming Agent Loop (SSE) ─────────────────────────────────

    async def chat_stream(
        self,
        session_id: str,
        message: str,
        db_session_id: str = "",
        model: Optional[str] = None,
        cross_pod_enabled: bool = False,
        ssh_credentials: Optional[Dict[str, Any]] = None,
    ):
        """
        Streaming version of chat(). Yields SSE events as the agent works:
        - event: thinking   → model reasoning text
        - event: tool_start → tool name + args (before execution)
        - event: tool_result→ tool result (after execution)
        - event: done       → final response with all data
        - event: error      → error message

        Concurrency: acquires a per-session asyncio.Lock so two concurrent
        requests sharing the same session_id do not interleave updates to
        ``self._sessions[session_id]`` or yield interleaved SSE chunks. The
        outer try/finally guarantees we always emit a terminal event so the
        UI's EventSource never hangs (Phase A1).
        """

        def _sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

        if not self._github_token:
            yield _sse("error", {"error": "Not signed in. Click 'Sign in with GitHub' first."})
            yield _sse("done", {"success": False, "error": "not_signed_in"})
            return

        session_lock = await self._get_session_lock(session_id)
        if session_lock.locked():
            yield _sse("error", {
                "error": "Another request is already running for this chat session. Please wait for it to finish."
            })
            yield _sse("done", {"success": False, "error": "session_busy"})
            return

        async with session_lock:
            async for chunk in self._chat_stream_impl(
                session_id=session_id,
                message=message,
                db_session_id=db_session_id,
                model=model,
                cross_pod_enabled=cross_pod_enabled,
                ssh_credentials=ssh_credentials,
                _sse=_sse,
            ):
                yield chunk

    async def _chat_stream_impl(
        self,
        session_id: str,
        message: str,
        db_session_id: str,
        model: Optional[str],
        _sse,
        cross_pod_enabled: bool = False,
        ssh_credentials: Optional[Dict[str, Any]] = None,
    ):
        """Inner implementation of chat_stream; runs under per-session lock."""

        model_id = model or self._default_model
        start = time.perf_counter()
        tool_calls_made: List[CopilotToolCall] = []
        total_usage: Dict[str, int] = {}
        active_db_name: str = ""
        done_sent = False  # Phase A1: track whether terminal event was emitted

        try:
            copilot_token = await self._get_copilot_token()
        except Exception as e:
            yield _sse("error", {"error": str(e)})
            yield _sse("done", {"success": False, "error": str(e)})
            return

        ssh_creds_present = bool(ssh_credentials and ssh_credentials.get("ssh_host"))
        if session_id not in self._sessions:
            self._sessions[session_id] = [{
                "role": "system",
                "content": self._build_system_prompt(db_session_id, cross_pod_enabled, ssh_creds_present),
            }]
        else:
            self._sessions[session_id][0] = {
                "role": "system",
                "content": self._build_system_prompt(db_session_id, cross_pod_enabled, ssh_creds_present),
            }
        history = self._sessions[session_id]
        history.append({"role": "user", "content": message})

        tool_defs = self._get_tool_definitions()
        mcp = get_mcp_server()
        # Bound the agent loop. Configurable via
        # COPILOT_AGENT_MAX_ITERATIONS / COPILOT_AGENT_WALL_CLOCK_SECONDS.
        from app.config.settings import settings as _agent_settings
        max_iterations = max(1, int(_agent_settings.copilot_agent_max_iterations))
        _budget_seconds = float(_agent_settings.copilot_agent_wall_clock_seconds)
        wall_clock_deadline = time.monotonic() + _budget_seconds

        headers = {
            "Authorization": f"Bearer {copilot_token}",
            "Content-Type": "application/json",
            **COPILOT_HEADERS,
        }

        try:
            async with httpx.AsyncClient(timeout=180.0, verify=_SSL_CTX) as client:
                for iteration in range(max_iterations):
                    if time.monotonic() > wall_clock_deadline:
                        yield _sse("error", {"error": f"Agent loop exceeded {_budget_seconds:.0f}s wall-clock budget"})
                        yield _sse("done", {"success": False, "error": "wall_clock_exceeded"})
                        done_sent = True
                        return
                    if self._copilot_token_expires < int(time.time()) + 30:
                        copilot_token = await self._get_copilot_token()
                        headers["Authorization"] = f"Bearer {copilot_token}"

                    payload = {
                        "model": model_id,
                        "messages": history,
                        "tools": tool_defs,
                        "tool_choice": "auto",
                    }

                    resp = await client.post(
                        "https://api.githubcopilot.com/chat/completions",
                        headers=headers,
                        json=payload,
                    )

                    if resp.status_code != 200:
                        yield _sse("error", {"error": f"API error {resp.status_code}"})
                        yield _sse("done", {"success": False, "error": f"api_{resp.status_code}"})
                        done_sent = True
                        return

                    data = resp.json()
                    if "usage" in data:
                        for k, v in data["usage"].items():
                            if isinstance(v, (int, float)):
                                total_usage[k] = total_usage.get(k, 0) + v
                            else:
                                total_usage[k] = v

                    choice = data["choices"][0]
                    assistant_msg = choice["message"]
                    reasoning_text = (assistant_msg.get("content") or "").strip() or None

                    if assistant_msg.get("tool_calls"):
                        history.append(assistant_msg)

                        # Emit reasoning/status for every iteration so UI shows progress
                        if reasoning_text:
                            yield _sse("thinking", {"text": reasoning_text})
                        else:
                            # Generate synthetic status when model returns content=null with tool_calls
                            tool_names = [tc["function"]["name"] for tc in assistant_msg["tool_calls"]]
                            readable_names = [t.replace("_", " ") for t in tool_names]
                            if iteration == 0:
                                status_text = f"Starting analysis... {', '.join(readable_names)}"
                            else:
                                status_text = f"Continuing analysis... {', '.join(readable_names)}"
                            yield _sse("thinking", {"text": status_text})

                        first_in_batch = True
                        for tc in assistant_msg["tool_calls"]:
                            fn = tc["function"]
                            tool_name = fn["name"]
                            try:
                                tool_args = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
                            except json.JSONDecodeError:
                                tool_args = {}

                            # Auto-inject session_id for DB-dependent tools.
                            # generate_sql and fix_sql are pure SQL builders / LLM helpers
                            # and do NOT accept a session_id kwarg.
                            _SESSION_TOOLS = {
                                "execute_sql",
                                "preview_data", "sample_column_values",
                                "introspect_schema", "discover_join_paths",
                                "get_connection_profile",
                                "analyze_connection_performance",
                                "validate_server_compatibility",
                                "detect_extensions", "semantic_data_search",
                                "search_tables", "search_columns",
                            }
                            if tool_name in _SESSION_TOOLS:
                                if "session_id" not in tool_args and db_session_id:
                                    tool_args["session_id"] = db_session_id
                            if tool_name in {"execute_sql", "validate_sql"} and isinstance(tool_args.get("sql"), str):
                                tool_args["sql"] = normalize_readonly_sql(tool_args["sql"])
                            if tool_name == "validate_server_compatibility" and isinstance(tool_args.get("sql_query"), str):
                                tool_args["sql_query"] = normalize_readonly_sql(tool_args["sql_query"])

                            # Auto-inject SSH credentials for *_via_ssh tools.
                            tool_args = _inject_ssh_credentials(tool_name, tool_args, ssh_credentials)

                            # Emit tool_start BEFORE execution (redact SSH secrets)
                            yield _sse("tool_start", {
                                "tool_name": tool_name,
                                "arguments": _redact_ssh_args_for_log(tool_args),
                                "database": active_db_name or None,
                                "index": len(tool_calls_made),
                            })

                            tool_start_t = time.perf_counter()
                            try:
                                # See chat() for rationale: off-load sync MCP tool calls
                                # (psycopg2 + SQLAlchemy are blocking) to a worker thread
                                # so we don't freeze the asyncio loop during long tools
                                # like check_db_integrity.
                                result = await asyncio.to_thread(mcp.call_tool, tool_name, tool_args)
                                tool_elapsed = (time.perf_counter() - tool_start_t) * 1000
                                tool_call = CopilotToolCall(
                                    tool_name=tool_name,
                                    arguments=_redact_ssh_args_for_log(tool_args),
                                    result=result.result,
                                    success=result.success,
                                    error=result.error,
                                    execution_time_ms=round(tool_elapsed, 1),
                                    reasoning=reasoning_text if first_in_batch else None,
                                    database=active_db_name or None,
                                )
                            except Exception as e:
                                tool_elapsed = (time.perf_counter() - tool_start_t) * 1000
                                tool_call = CopilotToolCall(
                                    tool_name=tool_name,
                                    arguments=_redact_ssh_args_for_log(tool_args),
                                    success=False,
                                    error=str(e),
                                    execution_time_ms=round(tool_elapsed, 1),
                                    reasoning=reasoning_text if first_in_batch else None,
                                    database=active_db_name or None,
                                )
                            first_in_batch = False
                            tool_calls_made.append(tool_call)

                            if (
                                tool_name == "switch_database"
                                and tool_call.success
                                and tool_call.result
                                and tool_call.result.get("session_id")
                            ):
                                db_session_id = tool_call.result["session_id"]
                                active_db_name = (
                                    f"{tool_call.result.get('database', '?')}"
                                    f"@{tool_call.result.get('hostname', '?')}"
                                )
                                tool_call.database = active_db_name

                            # Emit tool_result AFTER execution
                            yield _sse("tool_result", {
                                "tool_name": tool_name,
                                "success": tool_call.success,
                                "error": tool_call.error,
                                "execution_time_ms": tool_call.execution_time_ms,
                                "database": tool_call.database,
                                "index": len(tool_calls_made) - 1,
                            })

                            result_content = (
                                json.dumps(tool_call.result)
                                if tool_call.result
                                else (tool_call.error or "No result")
                            )
                            history.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": result_content[:30000],
                            })

                        continue

                    # Final text response
                    final_text = assistant_msg.get("content", "")
                    history.append({"role": "assistant", "content": final_text})

                    # Auto-continue heuristic (see non-streaming path for full rationale).
                    # Trust finish_reason='stop' unless the model explicitly
                    # promised an action but didn't execute one.
                    stream_finish = choice.get("finish_reason", "unknown")
                    final_lower = final_text.lower().strip()
                    promised_action = bool(re.search(
                        r"\b(let me (?:run|execute|fetch|query|pull|connect|check|look|try)"
                        r"|i'?ll (?:run|execute|fetch|query|pull|connect|go ahead|check|look|try)"
                        r"|now (?:i'?ll|i will|let me)"
                        r"|going to (?:run|execute|fetch|query|check)"
                        r"|connecting (?:to|and)"
                        r"|one moment|hold on|stand by)\b",
                        final_lower,
                    ))
                    is_final = (
                        stream_finish == "stop" and not promised_action
                    ) or stream_finish in ("content_filter",)

                    # C8: never re-prompt for greetings / acks.
                    if _looks_conversational(message):
                        is_final = True

                    if not is_final and iteration < max_iterations - 2:
                        # Emit intermediate text as thinking so it shows in UI reasoning
                        yield _sse("thinking", {"text": final_text})
                        history.append({
                            "role": "user",
                            "content": "Continue. Execute the query and show me the results."
                        })
                        logger.info(f"[STREAM] Auto-continuing iteration {iteration} (finish={stream_finish}, promised_action={promised_action}, said: {final_text[:80]}...)")
                        continue

                    elapsed = (time.perf_counter() - start) * 1000

                    sql = None
                    records = []
                    columns = []
                    row_count = 0
                    for tc_item in tool_calls_made:
                        if tc_item.tool_name == "generate_sql" and tc_item.success and tc_item.result:
                            sql = tc_item.result.get("sql", sql)
                        if tc_item.tool_name == "execute_sql" and tc_item.success and tc_item.result:
                            sql = tc_item.result.get("sql", sql)
                            records = tc_item.result.get("records", [])
                            columns = tc_item.result.get("columns", [])
                            row_count = tc_item.result.get("row_count", len(records))

                    # Trim history
                    if len(history) > 40:
                        trimmed = [history[0]]
                        tail = history[-24:]
                        si = 0
                        for i, msg in enumerate(tail):
                            if msg.get("role") == "tool":
                                si = i + 1
                            else:
                                break
                        trimmed.extend(tail[si:])
                        self._sessions[session_id] = trimmed

                    tool_steps_data = [
                        {
                            "tool_name": tc_item.tool_name,
                            "arguments": tc_item.arguments,
                            "result": tc_item.result,
                            "success": tc_item.success,
                            "error": tc_item.error,
                            "execution_time_ms": tc_item.execution_time_ms,
                            "reasoning": tc_item.reasoning,
                            "database": tc_item.database,
                        }
                        for tc_item in tool_calls_made
                    ]

                    # Compute estimated_cost using query_log_service pricing
                    if total_usage and "estimated_cost" not in total_usage:
                        from app.services.query_log_service import query_log_service
                        total_usage["estimated_cost"] = query_log_service._calculate_estimated_cost(
                            {**total_usage, "model": model_id}
                        )

                    # ── Verifiable Trust Layer: earned trust signals ──
                    _has_data = bool(sql) or any(
                        tc.tool_name == "execute_sql" for tc in tool_calls_made
                    )
                    _trust = _compute_trust(
                        tool_calls_made, sql=sql, records=records,
                        row_count=row_count, columns=columns,
                    ) if _has_data else {}

                    yield _sse("done", {
                        "success": True,
                        "message": final_text,
                        "sql": sql,
                        "trust_score": _trust.get("trust_score", 0),
                        "trust_label": _trust.get("trust_label", ""),
                        "trust_checks": _trust.get("trust_checks", []),
                        "verification": _trust.get("verification"),
                        "grounded_sources": _trust.get("grounded_sources", []),
                        # C9: align with non-streaming /api/copilot/chat
                        # (UI_ROW_LIMIT=500). UI never renders more than this
                        # and the row_count + truncated flag tell the caller
                        # to fetch the full set via /api/copilot/sql/execute.
                        "records": records[:_UI_ROW_LIMIT],
                        "row_count": row_count,
                        "truncated_for_ui": bool(records and len(records) > _UI_ROW_LIMIT),
                        "columns": columns,
                        "tool_steps": tool_steps_data,
                        "total_time_ms": round(elapsed, 1),
                        "model": model_id,
                        "session_id": session_id,
                        "usage": total_usage,
                        "active_database": active_db_name,
                    })
                    done_sent = True
                    return

                # Max iterations reached
                elapsed = (time.perf_counter() - start) * 1000
                yield _sse("error", {"error": f"Agent loop exceeded {max_iterations} iterations"})
                yield _sse("done", {"success": False, "error": "max_iterations"})
                done_sent = True

        except httpx.TimeoutException:
            yield _sse("error", {"error": "Request to GitHub Copilot API timed out. Check VPN/network connectivity to api.githubcopilot.com."})
        except httpx.ConnectError as e:
            err_str = str(e)
            logger.error(f"Copilot stream ConnectError: {err_str}", exc_info=True)
            if not err_str:
                err_str = "Cannot connect to api.githubcopilot.com (TLS/SSL handshake failed). Check VPN/network connectivity."
            yield _sse("error", {"error": err_str})
        except Exception as e:
            err_str = str(e)
            logger.error(f"Copilot stream error: {type(e).__name__}: {err_str}", exc_info=True)
            # Provide user-friendly error for common network issues
            if "getaddrinfo" in err_str or "Name or service not known" in err_str:
                err_str = ("Cannot resolve GitHub API hostname (DNS failure). "
                           "Check VPN/proxy — api.github.com and api.githubcopilot.com must be reachable.")
            elif "timed out" in err_str.lower() or "timeout" in err_str.lower():
                err_str = ("Connection to GitHub API timed out. "
                           "Check VPN/network connectivity.")
            elif not err_str:
                err_str = f"Network error ({type(e).__name__}). Check VPN/network connectivity to GitHub."
            yield _sse("error", {"error": err_str})
        finally:
            # Phase A1: guarantee a terminal SSE event so the UI EventSource
            # always knows the stream is over and can re-enable the Send button.
            if not done_sent:
                try:
                    yield _sse("done", {
                        "success": False,
                        "error": "stream_closed_unexpectedly",
                        "session_id": session_id,
                    })
                except Exception:
                    pass


# Singleton
_copilot_service: Optional[CopilotService] = None


def get_copilot_service() -> CopilotService:
    global _copilot_service
    if _copilot_service is None:
        _copilot_service = CopilotService()
    return _copilot_service
