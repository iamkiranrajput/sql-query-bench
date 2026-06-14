"""
Microsoft Foundry IQ knowledge-grounding client.

Foundry IQ is Microsoft's managed, permission-aware knowledge layer
(https://aka.ms/iq-series). It connects structured and unstructured enterprise
data through Azure AI Search "knowledge sources" / "knowledge bases" so an agent
can retrieve *grounded, cited* business context — going beyond traditional RAG.

This module wraps the Azure AI Search SDK and exposes a single, defensive
``retrieve(query)`` call used by the ``retrieve_business_context`` MCP tool. It is
deliberately resilient:

* **Graceful degradation** — if Azure credentials are not configured, or the
  ``azure-search-documents`` package is not installed, ``retrieve`` returns a
  clear "not configured" payload instead of raising. The SQL agent then simply
  proceeds with the local FAISS schema index, so the app runs identically with
  or without Foundry IQ.
* **API-drift tolerant** — it first attempts the Foundry IQ *knowledge base*
  agentic-retrieval API (``KnowledgeBaseRetrievalClient``, matching the
  microsoft/iq-series cookbooks). If that surface is unavailable in the
  installed SDK build, it falls back to a direct semantic/hybrid query against
  the backing Azure AI Search index via the stable ``SearchClient`` API. Both
  paths return the same ``{answer, citations}`` shape.

Secrets (the Azure AI Search admin/query key) are read from configuration which
itself is sourced from environment variables — never hard-coded here.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Common field-name candidates for content / title / source across both the
# IQ-series sample index (NASA "Earth at Night") and our own ingested index.
_CONTENT_FIELDS = ("content", "chunk", "text", "body", "page_content")
_TITLE_FIELDS = ("title", "name", "heading", "id")
_SOURCE_FIELDS = ("source", "url", "filepath", "metadata_storage_path", "category")


def _first_field(doc: Dict[str, Any], candidates) -> str:
    """Return the first non-empty candidate field from a search document."""
    for key in candidates:
        val = doc.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


class FoundryIQClient:
    """Thin, defensive wrapper over Foundry IQ knowledge retrieval."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str = "",
        knowledge_base_name: str = "",
        search_index: str = "",
        default_top_k: int = 5,
    ) -> None:
        self._endpoint = (endpoint or "").strip().rstrip("/")
        self._api_key = api_key or ""
        self._knowledge_base_name = (knowledge_base_name or "").strip()
        self._search_index = (search_index or "").strip()
        self._default_top_k = max(1, int(default_top_k or 5))

        self._lock = threading.Lock()
        self._kb_client = None
        self._search_client = None
        self._kb_unavailable = False  # set True once the agentic API is ruled out

    # -- configuration ---------------------------------------------------
    @property
    def is_configured(self) -> bool:
        """True when at least an endpoint plus one retrieval target exists."""
        return bool(self._endpoint and (self._knowledge_base_name or self._search_index))

    def _build_credential(self):
        """Build an Azure credential.

        Prefers an API key (simplest for a hackathon / single-tenant demo).
        Falls back to ``DefaultAzureCredential`` (Microsoft Entra ID / managed
        identity) when no key is supplied — the more production-grade,
        permission-aware path Foundry IQ is designed around.
        """
        if self._api_key:
            from azure.core.credentials import AzureKeyCredential

            return AzureKeyCredential(self._api_key)
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential()

    # -- public API ------------------------------------------------------
    def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        domain: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Retrieve grounded business context for ``query``.

        ``domain`` (optional) restricts grounding to governed definitions tagged
        with that dataset (e.g. "retail", "network") so one knowledge index can
        serve many databases. When omitted, all governed knowledge is searched
        (the original behaviour). Only the direct-index path filters by domain.

        Returns a dict with:
            ``configured`` (bool), ``answer`` (str), ``citations`` (list of
            {title, source, snippet}), ``source`` (which retrieval path ran),
            and ``error`` (str | None).
        """
        k = max(1, int(top_k or self._default_top_k))

        if not self.is_configured:
            return {
                "configured": False,
                "answer": "",
                "citations": [],
                "source": None,
                "error": (
                    "Foundry IQ is not configured. Set AZURE_SEARCH_ENDPOINT and "
                    "either FOUNDRY_KNOWLEDGE_BASE_NAME or FOUNDRY_SEARCH_INDEX to "
                    "enable governed knowledge grounding."
                ),
            }

        # 1) Preferred: Foundry IQ knowledge-base agentic retrieval.
        if self._knowledge_base_name and not self._kb_unavailable:
            try:
                return self._retrieve_via_knowledge_base(query, k)
            except ImportError:
                # SDK build lacks the knowledge-base surface — stop trying it.
                self._kb_unavailable = True
                logger.info(
                    "Foundry IQ knowledge-base API unavailable in this SDK build; "
                    "falling back to direct Azure AI Search index query."
                )
            except Exception as exc:  # pragma: no cover - network/runtime
                logger.warning("Foundry IQ knowledge-base retrieval failed: %s", exc)
                # fall through to the index path below

        # 2) Fallback: direct semantic/keyword query on the backing index.
        if self._search_index:
            try:
                return self._retrieve_via_search_index(query, k, domain)
            except ImportError as exc:
                return {
                    "configured": True,
                    "answer": "",
                    "citations": [],
                    "source": None,
                    "error": (
                        "The 'azure-search-documents' package is not installed. "
                        "Install it (see server/requirements.txt) to enable "
                        f"Foundry IQ grounding. Details: {exc}"
                    ),
                }
            except Exception as exc:  # pragma: no cover - network/runtime
                logger.warning("Foundry IQ index retrieval failed: %s", exc)
                return {
                    "configured": True,
                    "answer": "",
                    "citations": [],
                    "source": None,
                    "error": f"Foundry IQ retrieval failed: {exc}",
                }

        return {
            "configured": True,
            "answer": "",
            "citations": [],
            "source": None,
            "error": (
                "Foundry IQ knowledge-base retrieval was unavailable and no "
                "FOUNDRY_SEARCH_INDEX fallback is configured."
            ),
        }

    # -- retrieval paths -------------------------------------------------
    def _retrieve_via_knowledge_base(self, query: str, k: int) -> Dict[str, Any]:
        """Agentic retrieval against a Foundry IQ knowledge base.

        Mirrors the microsoft/iq-series Episode 3 cookbook. Parsing is defensive
        because the knowledge-base models are still in beta and field names have
        shifted across SDK builds.
        """
        # Imports are local so the package stays optional.
        from azure.search.documents.knowledgebases import (  # type: ignore
            KnowledgeBaseRetrievalClient,
        )
        from azure.search.documents.knowledgebases.models import (  # type: ignore
            KnowledgeBaseRetrievalRequest,
            KnowledgeBaseMessage,
            KnowledgeBaseMessageTextContent,
        )

        with self._lock:
            if self._kb_client is None:
                self._kb_client = KnowledgeBaseRetrievalClient(
                    endpoint=self._endpoint,
                    knowledge_base_name=self._knowledge_base_name,
                    credential=self._build_credential(),
                )
            client = self._kb_client

        request = KnowledgeBaseRetrievalRequest(
            messages=[
                KnowledgeBaseMessage(
                    role="user",
                    content=[KnowledgeBaseMessageTextContent(text=query)],
                )
            ]
        )
        result = client.retrieve(request)

        answer = self._extract_kb_answer(result)
        citations = self._extract_kb_citations(result, k)
        return {
            "configured": True,
            "answer": answer,
            "citations": citations,
            "source": "foundry_iq_knowledge_base",
            "error": None,
        }

    def _retrieve_via_search_index(
        self, query: str, k: int, domain: Optional[str] = None
    ) -> Dict[str, Any]:
        """Direct semantic/keyword query against the backing Azure AI Search index."""
        from azure.search.documents import SearchClient  # type: ignore

        with self._lock:
            if self._search_client is None:
                self._search_client = SearchClient(
                    endpoint=self._endpoint,
                    index_name=self._search_index,
                    credential=self._build_credential(),
                )
            client = self._search_client

        # Optional domain filter so one index can ground many databases.
        flt: Optional[str] = None
        if domain:
            from app.services.foundry_admin import sanitize_tag, odata_quote

            dom = sanitize_tag(domain)
            if dom:
                flt = f"domain eq '{odata_quote(dom)}'"

        # Try semantic ranking first; fall back to plain keyword search if the
        # index has no semantic configuration.
        try:
            results = client.search(
                search_text=query,
                top=k,
                filter=flt,
                query_type="semantic",
                semantic_configuration_name="default",
            )
            docs = list(results)
        except Exception:
            docs = list(client.search(search_text=query, top=k, filter=flt))

        citations: List[Dict[str, str]] = []
        snippets: List[str] = []
        for doc in docs:
            d = dict(doc)
            content = _first_field(d, _CONTENT_FIELDS)
            title = _first_field(d, _TITLE_FIELDS) or "knowledge passage"
            source = _first_field(d, _SOURCE_FIELDS)
            if not content:
                continue
            snippet = content[:600]
            citations.append({"title": title, "source": source, "snippet": snippet})
            snippets.append(f"- {snippet}")

        answer = "\n".join(snippets[:k])
        return {
            "configured": True,
            "answer": answer,
            "citations": citations,
            "source": "azure_ai_search_index",
            "error": None,
        }

    # -- defensive response parsing -------------------------------------
    @staticmethod
    def _extract_kb_answer(result: Any) -> str:
        """Pull synthesized answer text out of a knowledge-base response."""
        # Common shapes: result.response (list of messages with .content[].text),
        # or a plain string. Parse defensively.
        response = getattr(result, "response", None)
        if isinstance(response, str):
            return response
        texts: List[str] = []
        if response:
            for message in response:
                content = getattr(message, "content", None) or []
                for part in content:
                    text = getattr(part, "text", None)
                    if isinstance(text, str) and text.strip():
                        texts.append(text.strip())
        if texts:
            return "\n".join(texts)
        # Some builds expose a direct ``content`` string.
        content = getattr(result, "content", None)
        return content if isinstance(content, str) else ""

    @staticmethod
    def _extract_kb_citations(result: Any, k: int) -> List[Dict[str, str]]:
        """Pull reference passages out of a knowledge-base response."""
        citations: List[Dict[str, str]] = []
        references = getattr(result, "references", None) or getattr(
            result, "activity", None
        ) or []
        for ref in references:
            if isinstance(ref, dict):
                d = ref
            else:
                d = {
                    "title": getattr(ref, "title", "") or getattr(ref, "doc_key", ""),
                    "source": getattr(ref, "source", "") or getattr(ref, "url", ""),
                    "content": getattr(ref, "content", "") or getattr(ref, "text", ""),
                }
            title = _first_field(d, _TITLE_FIELDS) or "knowledge passage"
            source = _first_field(d, _SOURCE_FIELDS)
            snippet = _first_field(d, _CONTENT_FIELDS)[:600]
            if snippet:
                citations.append({"title": title, "source": source, "snippet": snippet})
            if len(citations) >= k:
                break
        return citations


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------
_client: Optional[FoundryIQClient] = None
_client_lock = threading.Lock()


def get_foundry_iq_client() -> FoundryIQClient:
    """Return the process-wide Foundry IQ client, built from settings."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from app.config.settings import settings

                _client = FoundryIQClient(
                    endpoint=settings.azure_search_endpoint,
                    api_key=settings.azure_search_api_key,
                    knowledge_base_name=settings.foundry_knowledge_base_name,
                    search_index=settings.foundry_search_index,
                    default_top_k=settings.foundry_retrieval_top_k,
                )
    return _client
