"""
In-app admin for Foundry IQ governed knowledge (Bring-Your-Own-Governance).

Lets a user add / edit / remove governed business definitions from the running
app instead of editing a JSON file and re-running the ingest script. Documents
are written straight into the Azure AI Search index that backs Foundry IQ, so a
new definition becomes groundable immediately by ``retrieve_business_context``.

Each document carries a ``domain`` tag (e.g. "retail", "network", "finance") so
ONE index can serve MANY databases: the agent grounds only on the domain bound
to the active connection. This is what makes the governance layer
bring-your-own rather than hard-coded to a single dataset.

Defensive, like ``FoundryIQClient``: if Azure is not configured or the SDK is
not installed, methods return a clear ``configured: False`` payload instead of
raising. Secrets come from settings/env, never hard-coded.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Azure Search document keys may contain only letters, digits, _ , - , =
_ID_SAFE = re.compile(r"[^A-Za-z0-9_\-=]")
# Domain / category tags: lower-case slug.
_TAG_SAFE = re.compile(r"[^a-z0-9_\-]")

_MAX_TITLE = 200
_MAX_CONTENT = 8000
_MAX_SOURCE = 300
_MAX_TAG = 60
_MAX_ID = 512


def sanitize_tag(value: Optional[str], *, default: str = "") -> str:
    """Normalise a domain/category tag to a safe lower-case slug."""
    if not value:
        return default
    slug = _TAG_SAFE.sub("-", value.strip().lower()).strip("-")
    return (slug or default)[:_MAX_TAG]


def odata_quote(value: str) -> str:
    """Escape a string literal for an OData filter ('' escapes a single quote)."""
    return value.replace("'", "''")


def _slug_id(title: str, domain: str) -> str:
    base = _ID_SAFE.sub("-", (title or "").strip().lower()).strip("-") or "term"
    dom = sanitize_tag(domain, default="general")
    return f"{dom}-{base}"[:_MAX_ID]


def _sdk_missing(exc: Exception) -> str:
    return (
        "The 'azure-search-documents' package is not installed. "
        f"Install it (see server/requirements.txt). Details: {exc}"
    )


class FoundryKnowledgeAdmin:
    """CRUD over the Foundry IQ governed-knowledge index (Azure AI Search)."""

    def __init__(self, *, endpoint: str, api_key: str = "", index_name: str = "") -> None:
        self._endpoint = (endpoint or "").strip().rstrip("/")
        self._api_key = api_key or ""
        self._index_name = (index_name or "").strip()
        self._lock = threading.Lock()
        self._search_client = None
        self._index_ensured = False

    @property
    def is_configured(self) -> bool:
        return bool(self._endpoint and self._index_name)

    # -- internals -------------------------------------------------------
    def _credential(self):
        if self._api_key:
            from azure.core.credentials import AzureKeyCredential

            return AzureKeyCredential(self._api_key)
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential()

    def _client(self):
        from azure.search.documents import SearchClient

        with self._lock:
            if self._search_client is None:
                self._search_client = SearchClient(
                    endpoint=self._endpoint,
                    index_name=self._index_name,
                    credential=self._credential(),
                )
            return self._search_client

    def _ensure_index(self) -> None:
        """Create or update the index so the schema (incl. ``domain``) exists.

        Adding fields to an existing Azure AI Search index is additive and
        non-destructive, so this is safe to call before an upsert.
        """
        if self._index_ensured:
            return
        from azure.search.documents.indexes import SearchIndexClient

        from app.services.foundry_index import build_knowledge_index

        index_client = SearchIndexClient(
            endpoint=self._endpoint, credential=self._credential()
        )
        index_client.create_or_update_index(build_knowledge_index(self._index_name))
        self._index_ensured = True

    def _not_configured(self) -> Dict[str, Any]:
        return {
            "success": True,
            "configured": False,
            "documents": [],
            "error": (
                "Foundry IQ knowledge admin is not configured. Set "
                "AZURE_SEARCH_ENDPOINT and FOUNDRY_SEARCH_INDEX to manage "
                "governed knowledge."
            ),
        }

    # -- public API ------------------------------------------------------
    def list_documents(
        self, domain: Optional[str] = None, *, top: int = 200
    ) -> Dict[str, Any]:
        """List governed docs, optionally filtered to a single ``domain``.

        Ensures the index schema (incl. ``domain``) first so existing indexes
        created before this feature are upgraded additively. If the schema
        can't be updated (e.g. a query-only key), it degrades to a domain-less
        listing so the call still succeeds.
        """
        if not self.is_configured:
            return self._not_configured()

        has_domain = True
        try:
            self._ensure_index()
        except ImportError as exc:
            return {
                "success": False,
                "configured": True,
                "documents": [],
                "error": _sdk_missing(exc),
            }
        except Exception as exc:  # pragma: no cover - schema update not permitted
            logger.info("list_documents: could not ensure index schema: %s", exc)
            has_domain = False

        try:
            select = ["id", "title", "content", "source", "category"]
            if has_domain:
                select.append("domain")
            flt = None
            dom = sanitize_tag(domain) if domain else ""
            if dom and has_domain:
                flt = f"domain eq '{odata_quote(dom)}'"
            results = self._client().search(
                search_text="*",
                filter=flt,
                top=max(1, min(int(top), 1000)),
                select=select,
            )
            docs = [
                {
                    "id": d.get("id") or "",
                    "title": d.get("title") or "",
                    "content": d.get("content") or "",
                    "source": d.get("source") or "",
                    "category": d.get("category") or "",
                    "domain": d.get("domain") or "",
                }
                for d in (dict(x) for x in results)
            ]
            return {
                "success": True,
                "configured": True,
                "documents": docs,
                "error": None,
            }
        except ImportError as exc:
            return {
                "success": False,
                "configured": True,
                "documents": [],
                "error": _sdk_missing(exc),
            }
        except Exception as exc:  # pragma: no cover - network/runtime
            logger.warning("list_documents failed: %s", exc)
            return {
                "success": False,
                "configured": True,
                "documents": [],
                "error": str(exc),
            }

    def upsert_document(
        self,
        *,
        title: str,
        content: str,
        source: str = "",
        category: str = "",
        domain: str = "",
        doc_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert or update a single governed definition."""
        if not self.is_configured:
            return self._not_configured()
        title = (title or "").strip()
        content = (content or "").strip()
        if not title or not content:
            return {
                "success": False,
                "configured": True,
                "error": "title and content are required.",
            }
        doc = {
            "id": (
                _ID_SAFE.sub("-", doc_id.strip())[:_MAX_ID]
                if doc_id and doc_id.strip()
                else _slug_id(title, domain)
            ),
            "title": title[:_MAX_TITLE],
            "content": content[:_MAX_CONTENT],
            "source": (source or "").strip()[:_MAX_SOURCE],
            "category": sanitize_tag(category, default="business-glossary"),
            "domain": sanitize_tag(domain, default="general"),
        }
        try:
            self._ensure_index()
            result = self._client().merge_or_upload_documents(documents=[doc])
            ok = all(r.succeeded for r in result)
            return {
                "success": ok,
                "configured": True,
                "document": doc,
                "error": None if ok else "the search service reported an upload failure.",
            }
        except ImportError as exc:
            return {"success": False, "configured": True, "error": _sdk_missing(exc)}
        except Exception as exc:  # pragma: no cover - network/runtime
            logger.warning("upsert_document failed: %s", exc)
            return {"success": False, "configured": True, "error": str(exc)}

    def delete_document(self, doc_id: str) -> Dict[str, Any]:
        """Remove a governed definition by id."""
        if not self.is_configured:
            return self._not_configured()
        clean = _ID_SAFE.sub("-", (doc_id or "").strip())[:_MAX_ID]
        if not clean:
            return {
                "success": False,
                "configured": True,
                "error": "a document id is required.",
            }
        try:
            self._client().delete_documents(documents=[{"id": clean}])
            return {"success": True, "configured": True, "id": clean, "error": None}
        except ImportError as exc:
            return {"success": False, "configured": True, "error": _sdk_missing(exc)}
        except Exception as exc:  # pragma: no cover - network/runtime
            logger.warning("delete_document failed: %s", exc)
            return {"success": False, "configured": True, "error": str(exc)}


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------
_admin: Optional[FoundryKnowledgeAdmin] = None
_admin_lock = threading.Lock()


def get_foundry_admin() -> FoundryKnowledgeAdmin:
    """Return the process-wide knowledge-admin client, built from settings."""
    global _admin
    if _admin is None:
        with _admin_lock:
            if _admin is None:
                from app.config.settings import settings

                _admin = FoundryKnowledgeAdmin(
                    endpoint=settings.azure_search_endpoint,
                    api_key=settings.azure_search_api_key,
                    index_name=settings.foundry_search_index,
                )
    return _admin
