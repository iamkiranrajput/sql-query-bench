"""
Ingest governed knowledge into Azure AI Search for Foundry IQ grounding.

This one-shot script creates (or updates) an Azure AI Search index and uploads
the curated governed-knowledge documents in
``server/data/foundry_knowledge/knowledge.json`` — the business glossary, metric
definitions, and PostGIS/pgvector conventions that the ``retrieve_business_context``
MCP tool grounds the SQL agent in.

It backs the **direct index** retrieval path of ``FoundryIQClient`` (stable
``SearchClient`` API). To also use Foundry IQ's *knowledge base* agentic
retrieval, create a knowledge source + knowledge base over this index in the
Foundry IQ portal and set ``FOUNDRY_KNOWLEDGE_BASE_NAME``.

Usage (from the ``server`` directory, with your .env populated):

    python scripts/ingest_knowledge.py
    python scripts/ingest_knowledge.py --index-name querybench-knowledge

Required configuration (env / .env):
    AZURE_SEARCH_ENDPOINT   - https://<service>.search.windows.net
    AZURE_SEARCH_API_KEY    - an ADMIN key (create/update index + upload)
    FOUNDRY_SEARCH_INDEX    - target index name (or pass --index-name)

Secrets are read from configuration (environment), never hard-coded here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the ``app`` package importable when run as a standalone script.
SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

DEFAULT_KNOWLEDGE_FILE = (
    SERVER_DIR / "app" / "data" / "foundry_knowledge" / "knowledge.json"
)
# The knowledge file actually lives under server/data (see repo layout); fall
# back to that location if the app/data copy is absent.
ALT_KNOWLEDGE_FILE = SERVER_DIR / "data" / "foundry_knowledge" / "knowledge.json"


def _load_settings():
    try:
        from app.config.settings import settings

        return settings
    except Exception as exc:  # pragma: no cover
        print(f"ERROR: could not load app settings: {exc}", file=sys.stderr)
        sys.exit(2)


def _resolve_knowledge_file(cli_path: str | None) -> Path:
    if cli_path:
        p = Path(cli_path)
        if not p.is_file():
            print(f"ERROR: knowledge file not found: {p}", file=sys.stderr)
            sys.exit(2)
        return p
    if ALT_KNOWLEDGE_FILE.is_file():
        return ALT_KNOWLEDGE_FILE
    if DEFAULT_KNOWLEDGE_FILE.is_file():
        return DEFAULT_KNOWLEDGE_FILE
    print(
        f"ERROR: no knowledge file found at {ALT_KNOWLEDGE_FILE} or "
        f"{DEFAULT_KNOWLEDGE_FILE}",
        file=sys.stderr,
    )
    sys.exit(2)


def _build_index(index_name: str):
    """Build a SearchIndex definition with a 'default' semantic configuration.

    Delegates to the shared schema builder so the ingest script and the in-app
    knowledge admin (``app/services/foundry_admin.py``) stay identical, incl.
    the ``domain`` field used for per-database governance.
    """
    from app.services.foundry_index import build_knowledge_index

    return build_knowledge_index(index_name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest Foundry IQ knowledge.")
    parser.add_argument(
        "--index-name",
        default=None,
        help="Target Azure AI Search index (default: FOUNDRY_SEARCH_INDEX).",
    )
    parser.add_argument(
        "--knowledge-file",
        default=None,
        help="Path to knowledge JSON (default: server/data/foundry_knowledge/knowledge.json).",
    )
    args = parser.parse_args()

    settings = _load_settings()
    endpoint = (settings.azure_search_endpoint or "").strip().rstrip("/")
    api_key = settings.azure_search_api_key or ""
    index_name = (args.index_name or settings.foundry_search_index or "").strip()

    if not endpoint:
        print("ERROR: AZURE_SEARCH_ENDPOINT is not set.", file=sys.stderr)
        return 2
    if not index_name:
        print(
            "ERROR: no index name. Set FOUNDRY_SEARCH_INDEX or pass --index-name.",
            file=sys.stderr,
        )
        return 2

    knowledge_file = _resolve_knowledge_file(args.knowledge_file)
    documents = json.loads(knowledge_file.read_text(encoding="utf-8"))
    if not isinstance(documents, list) or not documents:
        print(f"ERROR: {knowledge_file} must be a non-empty JSON array.", file=sys.stderr)
        return 2

    try:
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents import SearchClient
        from azure.search.documents.indexes import SearchIndexClient
    except ImportError as exc:
        print(
            "ERROR: azure-search-documents is not installed. Run "
            "`pip install -r requirements.txt`. Details: " + str(exc),
            file=sys.stderr,
        )
        return 2

    # Prefer an API key (admin) for index creation; fall back to AAD.
    if api_key:
        credential = AzureKeyCredential(api_key)
    else:
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()

    print(f"Endpoint : {endpoint}")
    print(f"Index    : {index_name}")
    print(f"Knowledge: {knowledge_file} ({len(documents)} documents)")

    # 1) Create or update the index.
    index_client = SearchIndexClient(endpoint=endpoint, credential=credential)
    index = _build_index(index_name)
    index_client.create_or_update_index(index)
    print(f"OK: index '{index_name}' created/updated.")

    # 2) Upload the documents.
    search_client = SearchClient(
        endpoint=endpoint, index_name=index_name, credential=credential
    )
    result = search_client.upload_documents(documents=documents)
    succeeded = sum(1 for r in result if r.succeeded)
    print(f"OK: uploaded {succeeded}/{len(documents)} documents.")

    if succeeded != len(documents):
        print("WARNING: some documents failed to upload.", file=sys.stderr)
        for r in result:
            if not r.succeeded:
                print(f"  - {r.key}: {r.error_message}", file=sys.stderr)
        return 1

    print(
        "\nDone. Set FOUNDRY_SEARCH_INDEX="
        f"{index_name} in your .env to enable grounding via the direct index path."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
