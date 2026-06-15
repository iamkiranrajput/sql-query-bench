"""Small helpers for cleaning SQL strings supplied to MCP tools."""

import re


_FENCED_BLOCK_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_LINE_START_RE = re.compile(r"(?im)^\s*(?:[-*]\s*)?(SELECT|WITH)\b")
_AFTER_LABEL_RE = re.compile(r"(?is)(?:^|[:\n\r])\s*(SELECT|WITH)\b")
_LEADING_COMMENT_RE = re.compile(r"(?is)^\s*(?:--[^\n]*(?:\n|$)|/\*.*?\*/\s*)+")


def normalize_readonly_sql(sql: str) -> str:
    """Return the read-only SQL statement when harmless prose precedes it.

    LLMs sometimes send tool arguments like ``"Method 1: ...\nSELECT ..."``.
    The execution tool should still enforce SELECT/WITH-only safety, but it can
    recover from that kind of label by trimming everything before the query.
    """
    if not sql:
        return sql

    cleaned = str(sql).strip()
    if not cleaned:
        return cleaned

    fenced_candidate = _first_sql_candidate_from_fence(cleaned)
    if fenced_candidate:
        cleaned = fenced_candidate

    cleaned = _strip_leading_comments(cleaned)
    if _starts_readonly(cleaned):
        return cleaned

    line_match = _LINE_START_RE.search(cleaned)
    if line_match:
        return _strip_leading_comments(cleaned[line_match.start(1):].strip())

    label_match = _AFTER_LABEL_RE.search(cleaned)
    if label_match:
        return _strip_leading_comments(cleaned[label_match.start(1):].strip())

    return cleaned


def _first_sql_candidate_from_fence(sql: str) -> str:
    for match in _FENCED_BLOCK_RE.finditer(sql):
        candidate = _strip_leading_comments(match.group(1).strip())
        if _starts_readonly(candidate) or _LINE_START_RE.search(candidate):
            return candidate
    return ""


def _strip_leading_comments(sql: str) -> str:
    previous = None
    cleaned = sql
    while previous != cleaned:
        previous = cleaned
        cleaned = _LEADING_COMMENT_RE.sub("", cleaned).strip()
    return cleaned


def _starts_readonly(sql: str) -> bool:
    upper = sql.lstrip().upper()
    return upper.startswith("SELECT") or upper.startswith("WITH")
