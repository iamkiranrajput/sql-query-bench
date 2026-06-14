"""
Populate products.embedding in the demo database with pgvector embeddings.

Computes a sentence-transformers (all-MiniLM-L6-v2, 384-dim) embedding of each
product description and stores it in the ``products.embedding`` vector column,
so the ``semantic_data_search`` MCP tool can rank products by meaning.

Run this once after the demo database is up (from the server virtualenv so the
dependencies are available):

    python demo/seed_embeddings.py

Connection settings come from the environment (no credentials are hard-coded):
    DEMO_DB_HOST      (default: localhost)
    DEMO_DB_PORT      (default: 5432)
    DEMO_DB_NAME      (default: querybench_demo)
    DEMO_DB_USER      (default: postgres)
    DEMO_DB_PASSWORD  (required)
"""

from __future__ import annotations

import os
import sys

# Use the locally-cached embedding model instead of contacting Hugging Face.
# This avoids corporate-proxy SSL failures and works fully offline as long as
# all-MiniLM-L6-v2 has been downloaded once (it is shared with the FAISS
# schema index). Set QUERYBENCH_ALLOW_HF_DOWNLOAD=1 to force an online fetch.
if os.getenv("QUERYBENCH_ALLOW_HF_DOWNLOAD") != "1":
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


def main() -> int:
    host = os.getenv("DEMO_DB_HOST", "localhost")
    port = os.getenv("DEMO_DB_PORT", "5432")
    name = os.getenv("DEMO_DB_NAME", "querybench_demo")
    user = os.getenv("DEMO_DB_USER", "postgres")
    password = os.getenv("DEMO_DB_PASSWORD")

    if not password:
        print(
            "ERROR: DEMO_DB_PASSWORD is not set. Export it before running "
            "(it must match the POSTGRES_PASSWORD used for docker compose).",
            file=sys.stderr,
        )
        return 2

    try:
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import URL
    except ImportError as exc:
        print(f"ERROR: SQLAlchemy not installed: {exc}", file=sys.stderr)
        return 2

    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError as exc:
        print(f"ERROR: sentence-transformers not installed: {exc}", file=sys.stderr)
        return 2

    # Supabase / most hosted Postgres require TLS. Add sslmode=require for
    # non-local hosts unless the caller already specified an sslmode.
    sslmode = os.getenv("DEMO_DB_SSLMODE")
    if not sslmode and host not in ("localhost", "127.0.0.1", "::1"):
        sslmode = "require"

    # Build the URL via URL.create() so special characters in the password
    # (e.g. '@', ':', '/') are escaped correctly instead of corrupting the
    # host — a plain f-string URL breaks when the password contains '@'.
    url = URL.create(
        "postgresql+psycopg2",
        username=user,
        password=password,
        host=host,
        port=int(port),
        database=name,
        query={"sslmode": sslmode} if sslmode else {},
    )
    engine = create_engine(url)

    print("Loading embedding model (all-MiniLM-L6-v2, offline cache)...")
    try:
        model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    except Exception as exc:
        print(
            "ERROR: could not load the cached embedding model: "
            f"{exc}\nThe model must be downloaded once while online. "
            "To force an online download (needs network + valid TLS), re-run "
            "with QUERYBENCH_ALLOW_HF_DOWNLOAD=1.",
            file=sys.stderr,
        )
        return 2

    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT product_id, description FROM products WHERE embedding IS NULL")
        ).fetchall()

        if not rows:
            print("All products already have embeddings. Nothing to do.")
            return 0

        print(f"Embedding {len(rows)} product descriptions...")
        for product_id, description in rows:
            vector = model.encode([description], show_progress_bar=False)[0]
            literal = "[" + ",".join(repr(float(x)) for x in vector) + "]"
            conn.execute(
                text(
                    "UPDATE products SET embedding = CAST(:vec AS vector) "
                    "WHERE product_id = :pid"
                ),
                {"vec": literal, "pid": product_id},
            )

    print("Done. products.embedding populated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
