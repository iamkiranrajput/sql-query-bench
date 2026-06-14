"""
Retrieve Business Context Tool (Microsoft Foundry IQ)

Grounds the SQL agent in a Foundry IQ knowledge base before it writes SQL.
Foundry IQ (https://aka.ms/iq-series) is Microsoft's managed, permission-aware
knowledge layer over enterprise data (Azure, SharePoint, OneLake, the web).

Where ``search_tables`` / ``search_columns`` answer *"which table/column"* from
the structured schema (local FAISS), this tool answers *"what does this business
term actually mean"* from governed, unstructured knowledge — data-dictionary
entries, metric definitions, glossary terms, and PostGIS/spatial conventions —
and returns **citations** so the generated SQL is explainable and trustworthy.

The tool degrades gracefully: when Foundry IQ is not configured it returns a
clear, non-fatal message and the agent continues with the schema tools alone.
"""

import logging
from typing import Any, Dict

from app.services.foundry_iq import get_foundry_iq_client

logger = logging.getLogger(__name__)


def retrieve_business_context(
    query: str, top_k: int = 5, domain: str = None, **_ignored
) -> Dict[str, Any]:
    """
    Retrieve governed business context for a natural-language term or question.

    Use this BEFORE generating SQL whenever the request contains a business term,
    metric, or domain concept whose definition is not obvious from column names
    (e.g. "active customer", "net revenue", "stores near downtown", "stale
    device"). Ground the SQL in the returned definitions and cite the sources.

    Args:
        query: The business term or question to ground
               (e.g. "definition of active customer").
        top_k: Maximum number of grounded passages to return (default: 5).
        domain: Optional dataset tag (e.g. "retail", "network") to restrict
                grounding to that database's governed definitions. When omitted,
                all governed knowledge is searched.

    Returns:
        {
            "success": bool,
            "configured": bool,        # False when Foundry IQ is not set up
            "answer": str,             # synthesized grounded context (may be "")
            "citations": [             # governed sources backing the answer
                {"title": str, "source": str, "snippet": str}, ...
            ],
            "retrieval_source": str | None,  # which Foundry IQ path answered
            "query": str,
            "error": str | None
        }
    """
    try:
        client = get_foundry_iq_client()
        result = client.retrieve(query, top_k=top_k, domain=domain)

        configured = bool(result.get("configured"))
        citations = result.get("citations") or []
        answer = result.get("answer") or ""
        err = result.get("error")

        # "Not configured" is a soft, expected state — report it as a successful
        # call with configured=False so the agent knows to proceed schema-only
        # instead of treating it as a hard failure and retrying.
        if not configured:
            logger.debug("retrieve_business_context: Foundry IQ not configured")
            return {
                "success": True,
                "configured": False,
                "answer": "",
                "citations": [],
                "retrieval_source": None,
                "query": query,
                "error": err,
            }

        success = err is None or bool(citations) or bool(answer)
        logger.debug(
            "retrieve_business_context '%s' -> %d citations (source=%s)",
            query,
            len(citations),
            result.get("source"),
        )
        return {
            "success": success,
            "configured": True,
            "answer": answer,
            "citations": citations,
            "retrieval_source": result.get("source"),
            "query": query,
            "error": err,
        }

    except Exception as e:
        logger.error("retrieve_business_context failed: %s", e)
        return {
            "success": False,
            "configured": False,
            "answer": "",
            "citations": [],
            "retrieval_source": None,
            "query": query,
            "error": str(e),
        }


# Tool metadata for MCP registration
TOOL_METADATA = {
    "name": "retrieve_business_context",
    "description": (
        "Ground business terms in Microsoft Foundry IQ — a governed, "
        "permission-aware knowledge base — BEFORE generating SQL. Use this when a "
        "request mentions a business metric or domain concept whose meaning is not "
        "obvious from column names (e.g. 'active customer', 'net revenue', 'stores "
        "within 5km of downtown', 'stale device'). Returns governed definitions "
        "plus citations so the SQL is explainable. If it returns configured=false, "
        "Foundry IQ is not set up — proceed using the schema tools instead. "
        "Never block on this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The business term, metric, or question to ground "
                    "(e.g. 'definition of active customer')."
                ),
            },
            "top_k": {
                "type": "integer",
                "description": "Maximum number of grounded passages to return",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}
