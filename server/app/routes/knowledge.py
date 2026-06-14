"""
Governed Knowledge admin routes (Bring-Your-Own-Governance).

Lets the UI list, add/edit, and remove the governed business definitions that
Foundry IQ grounds the SQL agent in -- per ``domain`` so one knowledge index can
serve many databases. Backed by ``FoundryKnowledgeAdmin`` (Azure AI Search).

These routes live under ``/api`` and are therefore covered by the existing API
key middleware when ``API_KEY`` is set in the environment.
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.foundry_admin import get_foundry_admin
from app.services.logger_service import setup_logger

logger = setup_logger(__name__)
router = APIRouter()


# ── Request / response models ─────────────────────────────────────────
class GovernedDocRequest(BaseModel):
    id: Optional[str] = Field(None, description="Existing id to update; omit to create")
    title: str = Field(..., max_length=200, description="Short governed term / heading")
    content: str = Field(..., max_length=8000, description="The governed definition text")
    source: str = Field("", max_length=300, description="Provenance / standard")
    category: str = Field("", max_length=60, description="glossary | metric | spatial-convention | ...")
    domain: str = Field("", max_length=60, description="Dataset this governs (retail | network | ...)")


class GovernedDocResponse(BaseModel):
    id: str
    title: str
    content: str
    source: str = ""
    category: str = ""
    domain: str = ""


class KnowledgeListResponse(BaseModel):
    configured: bool
    documents: List[GovernedDocResponse] = []
    error: Optional[str] = None


class KnowledgeWriteResponse(BaseModel):
    success: bool
    configured: bool
    document: Optional[GovernedDocResponse] = None
    error: Optional[str] = None


# ── Routes ────────────────────────────────────────────────────────────
@router.get("/knowledge", response_model=KnowledgeListResponse)
async def list_knowledge(domain: Optional[str] = None):
    """List governed definitions, optionally filtered to a single ``domain``."""
    out = get_foundry_admin().list_documents(domain=domain)
    return KnowledgeListResponse(
        configured=out.get("configured", False),
        documents=out.get("documents", []),
        error=out.get("error"),
    )


@router.post("/knowledge", response_model=KnowledgeWriteResponse)
async def upsert_knowledge(doc: GovernedDocRequest):
    """Create or update a governed definition; groundable immediately."""
    if not doc.title.strip() or not doc.content.strip():
        raise HTTPException(status_code=422, detail="title and content are required.")
    out = get_foundry_admin().upsert_document(
        title=doc.title,
        content=doc.content,
        source=doc.source,
        category=doc.category,
        domain=doc.domain,
        doc_id=doc.id,
    )
    return KnowledgeWriteResponse(
        success=out.get("success", False),
        configured=out.get("configured", False),
        document=out.get("document"),
        error=out.get("error"),
    )


@router.delete("/knowledge/{doc_id}", response_model=KnowledgeWriteResponse)
async def delete_knowledge(doc_id: str):
    """Remove a governed definition by id."""
    out = get_foundry_admin().delete_document(doc_id)
    return KnowledgeWriteResponse(
        success=out.get("success", False),
        configured=out.get("configured", False),
        error=out.get("error"),
    )
