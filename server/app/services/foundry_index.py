"""
Shared Azure AI Search index definition for Foundry IQ governed knowledge.

Single source of truth for the index schema so the ingest script
(``scripts/ingest_knowledge.py``) and the in-app knowledge admin
(``app/services/foundry_admin.py``) build an identical index.

Fields:
  id       (key)          - stable document id
  title    (searchable)   - short term / heading
  content  (searchable)   - the governed definition text
  source   (filterable)   - provenance / standard the definition comes from
  category (filter+facet) - glossary | metric | spatial-convention | ...
  domain   (filter+facet) - which dataset this governs (retail | network | ...)
                            Lets ONE Foundry IQ index serve MANY databases: the
                            agent grounds only on the active connection's domain,
                            so governance is bring-your-own, not hard-coded to a
                            single dataset.
"""

from __future__ import annotations


def build_knowledge_index(index_name: str):
    """Return a ``SearchIndex`` definition with a 'default' semantic config.

    The ``azure-search-documents`` import is local so this module stays
    importable even when the optional Azure SDK is not installed.
    """
    from azure.search.documents.indexes.models import (
        SearchIndex,
        SimpleField,
        SearchableField,
        SearchFieldDataType,
        SemanticConfiguration,
        SemanticField,
        SemanticPrioritizedFields,
        SemanticSearch,
    )

    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SearchableField(name="title", type=SearchFieldDataType.String),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SimpleField(
            name="source",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SimpleField(
            name="category",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True,
        ),
        # New in the Bring-Your-Own-Governance work: tag each definition with
        # the dataset/domain it governs so one index can serve many databases.
        SimpleField(
            name="domain",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True,
        ),
    ]

    semantic_config = SemanticConfiguration(
        name="default",
        prioritized_fields=SemanticPrioritizedFields(
            title_field=SemanticField(field_name="title"),
            content_fields=[SemanticField(field_name="content")],
        ),
    )
    semantic_search = SemanticSearch(configurations=[semantic_config])

    return SearchIndex(
        name=index_name,
        fields=fields,
        semantic_search=semantic_search,
    )
