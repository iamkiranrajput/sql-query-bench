"""
FAISS Schema Index

Builds and manages a FAISS vector index over schema_hints.json for
semantic search across 449 tables and 4900+ columns.

Uses sentence-transformers (all-MiniLM-L6-v2) for local embeddings,
which is 100x faster than LLM API calls and works offline.
"""

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class SchemaChunk:
    """A searchable chunk of schema metadata."""
    chunk_type: str  # "table" or "column"
    name: str  # table name or column name
    table_name: str  # always the table name
    description: str  # text used for embedding
    metadata: Dict[str, Any] = field(default_factory=dict)
    score: float = 0.0  # similarity score from search


@dataclass
class TableInfo:
    """Detailed table information from schema_hints."""
    name: str
    business_name: str
    description: str
    domain: str
    primary_key: Optional[str]
    row_count: int
    is_empty: bool
    time_columns: List[str]
    is_fact_table: bool
    relevance_score: int
    synonyms: List[str]
    columns: Dict[str, Dict[str, Any]]
    relationships: List[Dict[str, Any]]
    sample_queries: List[Dict[str, Any]]


@dataclass
class ColumnInfo:
    """Detailed column information from schema_hints."""
    name: str
    table_name: str
    business_name: str
    description: str
    data_type: str
    semantic_type: str
    is_nullable: bool
    is_filterable: bool
    is_aggregatable: bool
    is_join_key: bool
    synonyms: List[str]
    sample_values: List[str] = field(default_factory=list)
    allowed_values: List[str] = field(default_factory=list)


# ============================================================================
# Schema Index
# ============================================================================

class SchemaIndex:
    """
    FAISS-based semantic search index over database schema.
    
    Indexes tables and columns separately for targeted retrieval.
    Uses sentence-transformers for fast local embeddings.
    """
    
    def __init__(self, schema_hints_path: Optional[str] = None):
        """
        Initialize the schema index.
        
        Args:
            schema_hints_path: Path to schema_hints.json. If None, auto-detects.
        """
        self._lock = threading.Lock()
        self._initialized = False
        
        # Schema data
        self._schema_data: Dict[str, Any] = {}
        self._tables: Dict[str, TableInfo] = {}
        self._columns: Dict[str, ColumnInfo] = {}  # key: "table.column"
        self._relationships: List[Dict[str, Any]] = []
        
        # FAISS indexes
        self._table_index = None
        self._column_index = None
        self._table_chunks: List[SchemaChunk] = []
        self._column_chunks: List[SchemaChunk] = []
        
        # Embedding model (shared singleton)
        self._model = None
        self._embedding_dim = 384  # all-MiniLM-L6-v2 dimension
        
        # Path
        self._schema_path = schema_hints_path
        
    @property
    def is_initialized(self) -> bool:
        return self._initialized
    
    @property
    def tables(self) -> Dict[str, TableInfo]:
        return self._tables
    
    @property
    def columns(self) -> Dict[str, ColumnInfo]:
        return self._columns
    
    @property
    def relationships(self) -> List[Dict[str, Any]]:
        return self._relationships
    
    def initialize(self, schema_hints_path: Optional[str] = None) -> bool:
        """
        Load schema and build FAISS index.
        
        Args:
            schema_hints_path: Override path to schema_hints.json
            
        Returns:
            True if initialization successful
        """
        with self._lock:
            if self._initialized:
                logger.debug("Schema index already initialized")
                return True
            
            try:
                # Determine schema path
                path = schema_hints_path or self._schema_path
                if not path:
                    # Auto-detect based on common locations
                    base_dir = Path(__file__).resolve().parent.parent
                    candidates = [
                        base_dir / "data" / "schema_hints.json",
                        base_dir / "data" / "schema_hints_ems.json",
                    ]
                    for candidate in candidates:
                        if candidate.exists():
                            path = str(candidate)
                            break
                
                if not path or not Path(path).exists():
                    logger.error(f"Schema hints file not found: {path}")
                    return False
                
                logger.info(f"Loading schema from: {path}")
                
                # Load schema data
                with open(path, "r", encoding="utf-8") as f:
                    self._schema_data = json.load(f)
                self._schema_path_resolved = path
                
                # Parse tables and columns
                self._parse_schema()
                
                # Try to build FAISS index, but continue without it if it fails
                try:
                    self._build_index()
                    logger.info("FAISS index built successfully")
                except Exception as faiss_err:
                    logger.warning(f"FAISS index build failed (will use keyword search): {faiss_err}")
                    # Continue without FAISS - keyword search fallback will be used
                
                self._initialized = True
                logger.info(
                    f"Schema index initialized: {len(self._tables)} tables, "
                    f"{len(self._columns)} columns, {len(self._relationships)} relationships"
                )
                return True
                
            except Exception as e:
                logger.error(f"Failed to initialize schema index: {e}")
                return False
    
    def _parse_schema(self) -> None:
        """Parse schema_hints.json into structured data."""
        tables_data = self._schema_data.get("tables", {})
        
        for table_name, table_info in tables_data.items():
            # Parse table
            self._tables[table_name] = TableInfo(
                name=table_name,
                business_name=table_info.get("business_name", table_name),
                description=table_info.get("description", ""),
                domain=table_info.get("domain", "general"),
                primary_key=table_info.get("primary_key"),
                row_count=table_info.get("row_count", 0),
                is_empty=table_info.get("is_empty", True),
                time_columns=table_info.get("time_columns", []),
                is_fact_table=table_info.get("is_fact_table", False),
                relevance_score=table_info.get("relevance_score", 1),
                synonyms=table_info.get("synonyms", []),
                columns=table_info.get("columns", {}),
                relationships=table_info.get("relationships", []),
                sample_queries=table_info.get("sample_queries", []),
            )
            
            # Parse columns
            for col_name, col_info in table_info.get("columns", {}).items():
                key = f"{table_name}.{col_name}"
                self._columns[key] = ColumnInfo(
                    name=col_name,
                    table_name=table_name,
                    business_name=col_info.get("business_name", col_name),
                    description=col_info.get("description", ""),
                    data_type=col_info.get("data_type", "unknown"),
                    semantic_type=col_info.get("semantic_type", "unknown"),
                    is_nullable=col_info.get("is_nullable", True),
                    is_filterable=col_info.get("is_filterable", False),
                    is_aggregatable=col_info.get("is_aggregatable", False),
                    is_join_key=col_info.get("is_join_key", False),
                    synonyms=col_info.get("synonyms", []),
                    sample_values=col_info.get("sample_values", []),
                    allowed_values=col_info.get("allowed_values", []),
                )
            
            # Collect relationships
            for rel in table_info.get("relationships", []):
                self._relationships.append({
                    "from_table": table_name,
                    "from_column": rel.get("from_column", rel.get("column", "")),
                    "to_table": rel.get("to_table", rel.get("references_table", "")),
                    "to_column": rel.get("to_column", rel.get("references_column", "")),
                    "type": rel.get("type", "foreign_key"),
                })
        
        # Also check top-level relationships from alternative_joins.json
        alt_joins_path = Path(self._schema_path_resolved).parent / "alternative_joins.json"
        if alt_joins_path.exists():
            try:
                with open(alt_joins_path, "r", encoding="utf-8") as f:
                    alt_joins_data = json.load(f)
                for rel in alt_joins_data.get("relationships", []):
                    self._relationships.append({
                        "from_table": rel.get("from_table", ""),
                        "from_column": rel.get("from_column", ""),
                        "to_table": rel.get("to_table", ""),
                        "to_column": rel.get("to_column", ""),
                        "type": rel.get("type", "foreign_key"),
                    })
            except Exception as e:
                logger.warning(f"Failed to load relationships from alternative_joins.json: {e}")

        # Legacy: check top-level relationships in schema_hints (backward compat)
        for rel in self._schema_data.get("relationships", []):
            self._relationships.append({
                "from_table": rel.get("from_table", ""),
                "from_column": rel.get("from_column", ""),
                "to_table": rel.get("to_table", ""),
                "to_column": rel.get("to_column", ""),
                "type": rel.get("type", "foreign_key"),
            })
    
    def _load_concept_mappings(self) -> Dict[str, List[str]]:
        """
        Load concept_mappings from domain_context.json and invert them
        into a table_name → [synonym1, synonym2, ...] dictionary.
        
        E.g. {"device": "equipment", "node": "equipment"} →
             {"equipment": ["device", "node"]}
        """
        try:
            ctx_path = Path(self._schema_path_resolved).parent / "domain_context.json"
            if not ctx_path.exists():
                return {}
            with open(ctx_path, "r", encoding="utf-8") as f:
                ctx = json.load(f)
            mappings = ctx.get("concept_mappings", {})
            
            inverted: Dict[str, List[str]] = {}
            for concept, target in mappings.items():
                if concept.startswith("_"):
                    continue
                # Extract the table name from the target value
                # e.g. "orders WHERE status = 'active'" → "orders"
                table_name = target.strip().split()[0].split("(")[0].lower()
                if table_name and table_name in self._tables:
                    inverted.setdefault(table_name, []).append(concept)
            
            return inverted
        except Exception as e:
            logger.warning(f"Failed to load concept_mappings: {e}")
            return {}
    
    def _compute_schema_hash(self) -> str:
        """
        Compute a short hash of schema_hints.json + domain_context.json content.
        Used as cache key for embedding files — when either file changes,
        embeddings are re-encoded.
        """
        import hashlib
        h = hashlib.sha256()
        try:
            with open(self._schema_path_resolved, "rb") as f:
                h.update(f.read())
        except Exception:
            pass
        try:
            ctx_path = Path(self._schema_path_resolved).parent / "domain_context.json"
            if ctx_path.exists():
                with open(ctx_path, "rb") as f:
                    h.update(f.read())
        except Exception:
            pass
        return h.hexdigest()[:12]
    
    def _load_abbreviations(self) -> Dict[str, str]:
        """
        Merge abbreviations from schema_hints.json (86 entries) and
        domain_context.json (16 entries) into a single abbr→expansion dict.
        Domain context entries win on conflict (they're more descriptive).
        
        Also adds common English words that appear in table/column names
        (e.g. "device", "interface", "network") as identity mappings so
        the DP expansion can fully decompose concatenated names.
        """
        abbrs: Dict[str, str] = {}
        
        # Common English words found in table/column names (identity mappings)
        _common_words = [
            "device", "interface", "network", "element", "endpoint",
            "event", "link", "equipment", "module", "image", "status",
            "info", "data", "config", "backup", "restore", "job", "task",
            "user", "credential", "template", "location", "site",
            "physical", "logical", "virtual", "managed", "service", "resource",
            "point", "window", "current", "hourly",
            "daily", "weekly", "monitor", "performance", "field",
            "software", "all", "details",
            "view", "summary", "history", "archive", "version", "connector",
            "port", "memory", "storage", "file", "system",
            "group", "name", "type", "class", "state", "code", "value",
            "address", "route", "policy", "profile", "schedule", "trigger",
            "account", "role", "permission", "session", "log", "audit",
            "error", "errors", "counter", "rate", "count", "total",
            "utilization", "availability", "uptime",
            "spec", "sub", "with", "for", "settings",
            "inventory", "collection", "communication", "lifecycle",
            "extended", "base", "index", "entity", "owner",
            "owning", "created", "updated", "deleted", "active", "inactive",
            "primary", "secondary", "default", "custom", "global", "local",
            "input", "output", "source", "destination", "target",
            "average", "max", "min", "last", "first",
        ]
        # Additional abbreviation fragments. Database-specific abbreviations
        # are loaded at runtime from the optional schema_hints.json /
        # domain_context.json files (see below) rather than hard-coded here.
        _extra_abbrs: Dict[str, str] = {}
        for word in _common_words:
            abbrs[word] = word
        for abbr, expansion in _extra_abbrs.items():
            abbrs[abbr] = expansion
        
        # schema_hints abbreviations (86 entries)
        for abbr, expansion in self._schema_data.get("abbreviations", {}).items():
            if not abbr.startswith("_"):
                abbrs[abbr.lower()] = expansion
        # domain_context abbreviations (16 entries — override if present)
        try:
            ctx_path = Path(self._schema_path_resolved).parent / "domain_context.json"
            if ctx_path.exists():
                with open(ctx_path, "r", encoding="utf-8") as f:
                    ctx = json.load(f)
                for abbr, expansion in ctx.get("abbreviations", {}).items():
                    if not abbr.startswith("_"):
                        abbrs[abbr.lower()] = expansion
        except Exception:
            pass
        return abbrs
    
    def _expand_name(self, name: str, abbreviations: Dict[str, str]) -> str:
        """
        Expand a cryptic table/column name using abbreviation mappings.
        
        E.g. "custtxnhist"  → "customer transaction history"
             "ordln_qty"     → "order line quantity"
        
        Strategy: split on underscores, then use dynamic programming to find
        the segmentation that maximizes the total length of matched abbreviations
        (i.e. covers the most characters with known abbreviations).
        """
        import re
        segments = re.split(r'[_]', name.lower())
        expanded_parts = []
        
        for segment in segments:
            if not segment:
                continue
            expanded_parts.append(self._dp_expand_segment(segment, abbreviations))
        
        return " ".join(expanded_parts)
    
    def _dp_expand_segment(self, segment: str, abbreviations: Dict[str, str]) -> str:
        """
        Use dynamic programming to find the optimal segmentation of a
        concatenated string. Maximizes total matched-character coverage.
        
        The abbreviations dict is augmented with common English words
        that appear in table/column names (e.g. "device", "interface",
        "network") so the DP can fully decompose names without leaving
        single-character leftovers.
        """
        n = len(segment)
        # dp[i] = (max_matched_chars_from_i_to_end, list_of_expansion_tokens)
        dp: List[Optional[Tuple[int, List[str]]]] = [None] * (n + 1)
        dp[n] = (0, [])
        
        for i in range(n - 1, -1, -1):
            best_score = -1
            best_tokens: List[str] = []
            
            # Try every abbreviation/word match starting at position i
            for length in range(1, min(20, n - i) + 1):
                candidate = segment[i:i + length]
                if candidate in abbreviations and dp[i + length] is not None:
                    future_score, future_tokens = dp[i + length]
                    score = length + future_score
                    if score > best_score:
                        best_score = score
                        best_tokens = [abbreviations[candidate]] + future_tokens
            
            # Also try skipping this character (penalty: 0 matched chars)
            if dp[i + 1] is not None:
                skip_score, skip_tokens = dp[i + 1]
                if skip_score > best_score:
                    best_score = skip_score
                    best_tokens = [segment[i]] + skip_tokens
            
            if best_score >= 0:
                dp[i] = (best_score, best_tokens)
        
        if dp[0] is not None:
            return " ".join(dp[0][1])
        return segment
    
    def _build_index(self) -> None:
        """Build FAISS indexes for tables and columns, with disk-based embedding cache."""
        import os
        import hashlib
        import time as _time
        
        t0 = _time.perf_counter()
        
        # Check if we should skip FAISS (for environments without network access)
        if os.environ.get("SKIP_FAISS", "").lower() in ("1", "true", "yes"):
            logger.info("Skipping FAISS index build (SKIP_FAISS=1)")
            return
        
        try:
            import faiss
        except ImportError:
            logger.warning("FAISS not installed - using keyword search fallback")
            return
        
        try:
            # The previous implementation used a shared singleton in
            # ``app/services/embedding_model.py``. That service was removed in
            # the hackathon cleanup so we load sentence-transformers inline
            # here. The model identifier and dimension match the original
            # configuration (all-MiniLM-L6-v2 / 384 dims).
            from sentence_transformers import SentenceTransformer  # type: ignore

            self._model = SentenceTransformer(
                "sentence-transformers/all-MiniLM-L6-v2",
                cache_folder=str(
                    Path(__file__).resolve().parent.parent.parent
                    / "data"
                    / "embedding_cache"
                ),
            )
            self._embedding_dim = 384
        except Exception as e:
            logger.info(
                "Sentence-transformers model not available locally -- using "
                "keyword search: %s",
                e,
            )
            return
        
        # ── Enrich table synonyms with concept_mappings from domain_context.json ──
        concept_synonyms = self._load_concept_mappings()
        if concept_synonyms:
            enriched_count = 0
            for table_name, extra_synonyms in concept_synonyms.items():
                if table_name in self._tables:
                    existing = set(s.lower() for s in self._tables[table_name].synonyms)
                    for syn in extra_synonyms:
                        if syn.lower() not in existing:
                            self._tables[table_name].synonyms.append(syn)
                            existing.add(syn.lower())
                            enriched_count += 1
            if enriched_count:
                logger.info(f"Enriched table synonyms with {enriched_count} concept mappings")

        # ── Compute cache key from schema + domain context content ──
        cache_dir = Path(self._schema_path_resolved).parent / "embedding_cache"
        cache_hash = self._compute_schema_hash()
        table_cache_path = cache_dir / f"table_embeddings_{cache_hash}.npy"
        column_cache_path = cache_dir / f"column_embeddings_{cache_hash}.npy"
        
        # ── Load abbreviations for name expansion ──
        abbreviations = self._load_abbreviations()
        if abbreviations:
            logger.info(f"Loaded {len(abbreviations)} abbreviations for embedding name expansion")
        
        # ── Build table chunks (metadata — always needed) ──
        logger.debug(f"Building table embeddings for {len(self._tables)} tables...")
        table_texts = []
        for table_name, table_info in self._tables.items():
            text_parts = [
                table_info.business_name,
                table_info.description,
                f"domain: {table_info.domain}",
            ]
            # Expand cryptic table name: "custtxnhist" → "customer transaction history"
            if abbreviations:
                expanded = self._expand_name(table_name, abbreviations)
                if expanded != table_name.lower():
                    text_parts.append(f"expanded: {expanded}")
            if table_info.synonyms:
                text_parts.append(f"also known as: {', '.join(table_info.synonyms)}")
            
            text = " | ".join(text_parts)
            table_texts.append(text)
            
            self._table_chunks.append(SchemaChunk(
                chunk_type="table",
                name=table_name,
                table_name=table_name,
                description=text,
                metadata={
                    "business_name": table_info.business_name,
                    "domain": table_info.domain,
                    "row_count": table_info.row_count,
                    "columns_count": len(table_info.columns),
                    "relevance_score": table_info.relevance_score,
                    "is_fact_table": table_info.is_fact_table,
                },
            ))
        
        # ── Build column chunks (metadata — always needed) ──
        logger.debug(f"Building column embeddings for {len(self._columns)} columns...")
        column_texts = []
        for key, col_info in self._columns.items():
            text_parts = [
                col_info.business_name,
                col_info.description,
                f"type: {col_info.semantic_type}",
                f"in table: {col_info.table_name}",
            ]
            # Expand cryptic column name: "cpuutilization" → "cpu utilization"
            if abbreviations:
                expanded = self._expand_name(col_info.name, abbreviations)
                if expanded != col_info.name.lower():
                    text_parts.append(f"expanded: {expanded}")
            if col_info.synonyms:
                text_parts.append(f"also known as: {', '.join(col_info.synonyms)}")
            
            text = " | ".join(text_parts)
            column_texts.append(text)
            
            self._column_chunks.append(SchemaChunk(
                chunk_type="column",
                name=col_info.name,
                table_name=col_info.table_name,
                description=text,
                metadata={
                    "business_name": col_info.business_name,
                    "data_type": col_info.data_type,
                    "semantic_type": col_info.semantic_type,
                    "is_filterable": col_info.is_filterable,
                    "is_aggregatable": col_info.is_aggregatable,
                    "is_join_key": col_info.is_join_key,
                },
            ))
        
        # ── Try loading embeddings from cache ──
        cache_hit = False
        if table_cache_path.exists() and column_cache_path.exists():
            try:
                table_embeddings = np.load(str(table_cache_path))
                column_embeddings = np.load(str(column_cache_path))
                if (table_embeddings.shape[0] == len(self._table_chunks) and
                        column_embeddings.shape[0] == len(self._column_chunks)):
                    cache_hit = True
                    logger.info(
                        f"Loaded embeddings from cache "
                        f"({table_embeddings.shape[0]} tables, {column_embeddings.shape[0]} columns)"
                    )
                else:
                    logger.info("Embedding cache shape mismatch — re-encoding")
            except Exception as e:
                logger.warning(f"Failed to load embedding cache: {e}")
        
        # ── Encode if no cache hit ──
        if not cache_hit:
            if table_texts:
                table_embeddings = self._model.encode(
                    table_texts,
                    show_progress_bar=False,
                    convert_to_numpy=True
                ).astype(np.float32)
                faiss.normalize_L2(table_embeddings)
            else:
                table_embeddings = np.empty((0, self._embedding_dim), dtype=np.float32)
            
            if column_texts:
                column_embeddings = self._model.encode(
                    column_texts,
                    show_progress_bar=False,
                    convert_to_numpy=True
                ).astype(np.float32)
                faiss.normalize_L2(column_embeddings)
            else:
                column_embeddings = np.empty((0, self._embedding_dim), dtype=np.float32)
            
            # Save to cache
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                # Remove stale caches with different hashes
                for old in cache_dir.glob("*.npy"):
                    if cache_hash not in old.name:
                        old.unlink()
                np.save(str(table_cache_path), table_embeddings)
                np.save(str(column_cache_path), column_embeddings)
                logger.info(f"Saved embedding cache to {cache_dir}")
            except Exception as e:
                logger.warning(f"Failed to save embedding cache: {e}")
        
        # ── Build FAISS indexes from embeddings ──
        if table_embeddings.shape[0] > 0:
            self._table_index = faiss.IndexFlatIP(self._embedding_dim)
            # Embeddings are already normalized when encoded or when saved to cache
            if cache_hit:
                faiss.normalize_L2(table_embeddings)
            self._table_index.add(table_embeddings)
        
        if column_embeddings.shape[0] > 0:
            self._column_index = faiss.IndexFlatIP(self._embedding_dim)
            if cache_hit:
                faiss.normalize_L2(column_embeddings)
            self._column_index.add(column_embeddings)
        
        elapsed_ms = (_time.perf_counter() - t0) * 1000
        logger.info(
            f"FAISS indexes built: {len(self._table_chunks)} table chunks, "
            f"{len(self._column_chunks)} column chunks ({elapsed_ms:.0f}ms, "
            f"{'cache hit' if cache_hit else 'fresh encode'})"
        )
    
    def search_tables(
        self, 
        query: str, 
        top_k: int = 10,
        min_score: float = 0.3
    ) -> List[SchemaChunk]:
        """
        Search for tables matching the query.
        
        Args:
            query: Natural language search query
            top_k: Maximum number of results
            min_score: Minimum similarity score (0-1)
            
        Returns:
            List of matching table chunks with scores
        """
        if not self._initialized:
            logger.warning("Schema index not initialized")
            return []
        
        # Use FAISS if available, otherwise fallback to keyword search
        if self._table_index is not None and self._model is not None:
            return self._search_tables_faiss(query, top_k, min_score)
        else:
            return self._search_tables_keyword(query, top_k)
    
    def _search_tables_faiss(
        self, 
        query: str, 
        top_k: int = 10,
        min_score: float = 0.3
    ) -> List[SchemaChunk]:
        """Search using FAISS embeddings, boosted by column-level semantic matches."""
        try:
            # Embed query once — reuse for both table and column search
            query_embedding = self._model.encode(
                [query], 
                show_progress_bar=False,
                convert_to_numpy=True
            ).astype(np.float32)
            
            import faiss
            faiss.normalize_L2(query_embedding)
            
            # ── Table-level search ──
            scores, indices = self._table_index.search(query_embedding, min(top_k * 2, len(self._table_chunks)))
            
            # Collect table scores into a dict for merging with column boosts
            table_scores: Dict[str, float] = {}
            table_chunk_map: Dict[str, SchemaChunk] = {}
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0:
                    continue
                chunk = self._table_chunks[idx]
                table_scores[chunk.table_name] = float(score)
                table_chunk_map[chunk.table_name] = SchemaChunk(
                    chunk_type=chunk.chunk_type,
                    name=chunk.name,
                    table_name=chunk.table_name,
                    description=chunk.description,
                    metadata=chunk.metadata,
                    score=float(score),
                )
            
            # ── Column-level boost: search columns, boost their parent tables ──
            if self._column_index is not None:
                col_k = min(30, len(self._column_chunks))
                col_scores, col_indices = self._column_index.search(query_embedding, col_k)
                
                col_boost_weight = 0.3  # how much column match boosts parent table
                for score, idx in zip(col_scores[0], col_indices[0]):
                    if idx < 0 or float(score) < 0.35:
                        continue
                    col_chunk = self._column_chunks[idx]
                    tname = col_chunk.table_name
                    boost = float(score) * col_boost_weight
                    
                    if tname in table_scores:
                        # Boost existing table score
                        table_scores[tname] = max(table_scores[tname], table_scores[tname] + boost)
                    else:
                        # Column match found a table not in direct table results — add it
                        table_scores[tname] = boost
                        tinfo = self._tables.get(tname)
                        if tinfo:
                            table_chunk_map[tname] = SchemaChunk(
                                chunk_type="table",
                                name=tname,
                                table_name=tname,
                                description=tinfo.description,
                                metadata={
                                    "business_name": tinfo.business_name,
                                    "domain": tinfo.domain,
                                    "row_count": tinfo.row_count,
                                    "columns_count": len(tinfo.columns),
                                    "relevance_score": tinfo.relevance_score,
                                    "is_fact_table": tinfo.is_fact_table,
                                },
                                score=boost,
                            )
            
            # ── Merge and return top-k above threshold ──
            results = []
            for tname in sorted(table_scores, key=lambda t: -table_scores[t]):
                final_score = table_scores[tname]
                if final_score < min_score:
                    continue
                chunk = table_chunk_map[tname]
                chunk.score = min(final_score, 1.0)
                results.append(chunk)
                if len(results) >= top_k:
                    break
            
            return results
            
        except Exception as e:
            logger.error(f"FAISS table search failed: {e}")
            return self._search_tables_keyword(query, top_k)
    
    def _search_tables_keyword(self, query: str, top_k: int = 10) -> List[SchemaChunk]:
        """Simple keyword-based fallback search."""
        import re
        query_lower = query.lower()
        query_words = set(re.findall(r'\w+', query_lower))
        
        # Remove common stop words
        stop_words = {'show', 'all', 'the', 'get', 'me', 'from', 'data', 'list', 'find', 'display', 'a', 'an', 'in', 'with'}
        query_words = query_words - stop_words
        
        scored_tables = []
        for table_name, table_info in self._tables.items():
            score = 0.0
            table_lower = table_name.lower()
            
            # Exact table name match
            if table_lower in query_lower or query_lower in table_lower:
                score += 0.9
            
            # Word matches in table name
            table_words = set(re.findall(r'\w+', table_lower))
            common_words = query_words & table_words
            if common_words:
                score += len(common_words) * 0.3
            
            # Check business name
            business_lower = table_info.business_name.lower()
            if any(w in business_lower for w in query_words):
                score += 0.2
            
            # Check synonyms
            for syn in table_info.synonyms:
                syn_lower = syn.lower()
                if syn_lower in query_lower or any(w in syn_lower for w in query_words):
                    score += 0.4
                    break
            
            # Check description
            desc_lower = table_info.description.lower()
            if any(w in desc_lower for w in query_words):
                score += 0.1
            
            if score > 0:
                scored_tables.append((table_name, score, table_info))
        
        # Sort by score descending
        scored_tables.sort(key=lambda x: -x[1])
        
        results = []
        for table_name, score, table_info in scored_tables[:top_k]:
            chunk = SchemaChunk(
                chunk_type="table",
                name=table_name,
                table_name=table_name,
                description=table_info.description,
                metadata={
                    "business_name": table_info.business_name,
                    "domain": table_info.domain,
                    "row_count": table_info.row_count,
                    "columns_count": len(table_info.columns),
                },
                score=min(score, 1.0)  # Cap at 1.0
            )
            results.append(chunk)
        
        return results
    
    def search_columns(
        self,
        query: str,
        table_filter: Optional[List[str]] = None,
        top_k: int = 20,
        min_score: float = 0.25
    ) -> List[SchemaChunk]:
        """
        Search for columns matching the query.
        
        Args:
            query: Natural language search query
            table_filter: Optional list of table names to restrict search
            top_k: Maximum number of results
            min_score: Minimum similarity score (0-1)
            
        Returns:
            List of matching column chunks with scores
        """
        if not self._initialized:
            logger.warning("Schema index not initialized")
            return []
        
        # Use FAISS if available, otherwise fallback to keyword search
        if self._column_index is not None and self._model is not None:
            return self._search_columns_faiss(query, table_filter, top_k, min_score)
        else:
            return self._search_columns_keyword(query, table_filter, top_k)
    
    def _search_columns_faiss(
        self,
        query: str,
        table_filter: Optional[List[str]] = None,
        top_k: int = 20,
        min_score: float = 0.25
    ) -> List[SchemaChunk]:
        """Search columns using FAISS embeddings."""
        try:
            # Embed query
            query_embedding = self._model.encode(
                [query],
                show_progress_bar=False,
                convert_to_numpy=True
            ).astype(np.float32)
            
            import faiss
            faiss.normalize_L2(query_embedding)
            
            # Search (get more results if filtering)
            search_k = top_k * 3 if table_filter else top_k
            scores, indices = self._column_index.search(
                query_embedding, 
                min(search_k, len(self._column_chunks))
            )
            
            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0 or score < min_score:
                    continue
                chunk = self._column_chunks[idx]
                
                # Apply table filter
                if table_filter and chunk.table_name not in table_filter:
                    continue
                
                chunk.score = float(score)
                results.append(chunk)
                
                if len(results) >= top_k:
                    break
            
            return results
            
        except Exception as e:
            logger.error(f"FAISS column search failed: {e}")
            return self._search_columns_keyword(query, table_filter, top_k)
    
    def _search_columns_keyword(
        self,
        query: str,
        table_filter: Optional[List[str]] = None,
        top_k: int = 20
    ) -> List[SchemaChunk]:
        """Simple keyword-based fallback column search."""
        import re
        query_lower = query.lower()
        query_words = set(re.findall(r'\w+', query_lower))
        
        scored_columns = []
        for key, col_info in self._columns.items():
            # Apply table filter
            if table_filter and col_info.table_name not in table_filter:
                continue
            
            score = 0.0
            col_lower = col_info.name.lower()
            
            # Exact column name match
            if col_lower in query_lower or query_lower in col_lower:
                score += 0.9
            
            # Word matches
            col_words = set(re.findall(r'\w+', col_lower))
            common_words = query_words & col_words
            if common_words:
                score += len(common_words) * 0.3
            
            # Check business name
            business_lower = col_info.business_name.lower()
            if any(w in business_lower for w in query_words):
                score += 0.2
            
            # Check synonyms
            for syn in col_info.synonyms:
                if syn.lower() in query_lower:
                    score += 0.4
                    break
            
            if score > 0:
                scored_columns.append((key, score, col_info))
        
        # Sort by score descending
        scored_columns.sort(key=lambda x: -x[1])
        
        results = []
        for key, score, col_info in scored_columns[:top_k]:
            chunk = SchemaChunk(
                chunk_type="column",
                name=col_info.name,
                table_name=col_info.table_name,
                description=col_info.description,
                metadata={
                    "business_name": col_info.business_name,
                    "data_type": col_info.data_type,
                    "semantic_type": col_info.semantic_type,
                },
                score=min(score, 1.0)
            )
            results.append(chunk)
        
        return results
    
    def get_table(self, table_name: str) -> Optional[TableInfo]:
        """Get detailed table information."""
        return self._tables.get(table_name)
    
    def get_column(self, table_name: str, column_name: str) -> Optional[ColumnInfo]:
        """Get detailed column information."""
        return self._columns.get(f"{table_name}.{column_name}")
    
    def get_table_columns(self, table_name: str) -> List[ColumnInfo]:
        """Get all columns for a table."""
        prefix = f"{table_name}."
        return [
            col for key, col in self._columns.items()
            if key.startswith(prefix)
        ]
    
    def get_relationships_for_tables(
        self, 
        tables: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Get all relationships involving the specified tables.
        
        Args:
            tables: List of table names
            
        Returns:
            List of relationship dictionaries
        """
        table_set = set(tables)
        return [
            rel for rel in self._relationships
            if rel["from_table"] in table_set or rel["to_table"] in table_set
        ]
    
    def get_stats(self) -> Dict[str, Any]:
        """Get index statistics."""
        return {
            "initialized": self._initialized,
            "total_tables": len(self._tables),
            "total_columns": len(self._columns),
            "total_relationships": len(self._relationships),
            "table_chunks": len(self._table_chunks),
            "column_chunks": len(self._column_chunks),
            "embedding_dim": self._embedding_dim,
        }


# ============================================================================
# Singleton Instance
# ============================================================================

_schema_index: Optional[SchemaIndex] = None
_index_lock = threading.Lock()


def get_schema_index() -> SchemaIndex:
    """Get or create the singleton schema index."""
    global _schema_index
    
    with _index_lock:
        if _schema_index is None:
            _schema_index = SchemaIndex()
        return _schema_index


def initialize_schema_index(schema_hints_path: Optional[str] = None) -> bool:
    """Initialize the schema index with optional custom path."""
    index = get_schema_index()
    return index.initialize(schema_hints_path)


def rebuild_for_database(schema_hints_path: str) -> bool:
    """
    Rebuild the singleton SchemaIndex from a new schema_hints file.
    
    Clears all existing data (tables, columns, relationships, FAISS indexes)
    and re-initializes from the given path. Used when the user switches databases.
    
    The embedding model is preserved across rebuilds (no re-load needed).
    Cached embeddings on disk are keyed by content hash — switching to a
    previously-seen database is nearly instant on the second switch.
    """
    index = get_schema_index()
    with index._lock:
        # Preserve model reference — no need to reload the 90MB model
        saved_model = index._model
        
        # Reset all internal state
        index._schema_data = {}
        index._tables = {}
        index._columns = {}
        index._relationships = []
        index._table_index = None
        index._column_index = None
        index._table_chunks = []
        index._column_chunks = []
        index._initialized = False
        
        # Restore model
        index._model = saved_model

    logger.info(f"[SchemaIndex] Rebuilding for new database: {schema_hints_path}")
    return index.initialize(schema_hints_path)
