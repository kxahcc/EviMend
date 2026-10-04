"""Bounded records for deterministic retrieval from a frozen knowledge snapshot.

Shape, pagination, score arithmetic and content hashes are checked here. Source
permissions, complete index rows and actual text matches must be independently
recomputed by the source-reading runtime. Relevance never certifies applicability.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from .contracts import require_hash, require_identifiers
from .knowledge_contracts import (
    MAX_CLOSURE_BYTES,
    MAX_PAYLOAD_BYTES,
    MAX_SNAPSHOT_ENTRIES,
    PermissionScope,
    RetrieverSpec,
    SolverProjection,
    Tag,
    _Frozen,
    _load,
    _unique,
    content_hash,
    encode,
)

MAX_QUERY_TERMS = 256
MAX_INDEX_READ_BYTES = 4 * MAX_CLOSURE_BYTES
MAX_INDEX_READ_CALLS = 16_384
MAX_SQL_STEPS = 1_000_000
MAX_SQL_BYTES = MAX_PAYLOAD_BYTES
_MAX_QUERY_CHARS = 65_536
Term = Annotated[str, Field(min_length=1, max_length=_MAX_QUERY_CHARS)]


def query_terms(text: str) -> tuple[str, ...]:
    """The frozen tags_text_v1 tokenization, without a vocabulary filter."""
    return tuple(sorted(set(re.findall(r"\w+", text.casefold()))))


def query_char_count(query: MemoryQuery) -> int:
    return len(query.text) + sum(len(item) for item in query.tags)


def query_tags(query: MemoryQuery) -> tuple[str, ...]:
    """Normalize matching spelling while retaining original query tags in records."""
    return tuple(item.strip().casefold() for item in query.tags)


class MemoryQuery(_Frozen):
    snapshot_sha256: str
    scope: PermissionScope
    tags: tuple[Tag, ...] = Field(default=(), max_length=128)
    text: str = Field(default="", max_length=_MAX_QUERY_CHARS)
    offset: int = Field(default=0, ge=0, le=MAX_SNAPSHOT_ENTRIES)
    limit: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def valid_query(self) -> MemoryQuery:
        require_hash(self.snapshot_sha256)
        normalized = query_tags(self)
        if any(not item or len(item) > 128 for item in normalized):
            raise ValueError("query tags require bounded nonblank normalized spelling")
        _unique(normalized)
        if query_char_count(self) > _MAX_QUERY_CHARS:
            raise ValueError("query exceeds its combined character limit")
        if len(query_terms(self.text)) > MAX_QUERY_TERMS:
            raise ValueError("query exceeds its distinct term limit")
        return self


class IndexDescriptor(_Frozen):
    snapshot_sha256: str
    sqlite_sha256: str
    sqlite_bytes: int = Field(ge=1, le=MAX_SQL_BYTES)
    indexed_entries: int = Field(ge=0, le=MAX_SNAPSHOT_ENTRIES)
    rows_sha256: str
    retriever_sha256: str

    @model_validator(mode="after")
    def valid_descriptor(self) -> IndexDescriptor:
        for digest in (self.snapshot_sha256, self.sqlite_sha256, self.rows_sha256, self.retriever_sha256):
            require_hash(digest)
        return self


class RetrievalHit(_Frozen):
    entry_id: str
    content_sha256: str
    projection_sha256: str
    projection: SolverProjection
    score: int = Field(ge=0)
    matched_tags: tuple[Tag, ...] = Field(default=(), max_length=128)
    matched_terms: tuple[Term, ...] = Field(default=(), max_length=MAX_QUERY_TERMS)

    @model_validator(mode="after")
    def valid_hit(self) -> RetrievalHit:
        require_hash(self.content_sha256)
        require_hash(self.projection_sha256)
        require_identifiers([self.entry_id])
        if self.entry_id != "experience-" + self.content_sha256:
            raise ValueError("retrieval hit identity disagrees with its content hash")
        if content_hash(self.projection) != self.projection_sha256:
            raise ValueError("retrieval hit projection changed")
        for values in (self.matched_tags, self.matched_terms):
            _unique(values)
            if tuple(sorted(values)) != values:
                raise ValueError("matched tags and terms require ascending order")
        if any(item != item.strip().casefold() for item in self.matched_tags):
            raise ValueError("matched tags must use normalized query spelling")
        if any(item != item.casefold() or re.fullmatch(r"\w+", item) is None for item in self.matched_terms):
            raise ValueError("matched terms must use frozen query tokenization")
        return self


class RetrievalPayload(_Frozen):
    query: MemoryQuery
    index_sha256: str
    retriever: RetrieverSpec
    candidate_count: int = Field(ge=0, le=MAX_SNAPSHOT_ENTRIES)
    positive_count: int = Field(ge=0, le=MAX_SNAPSHOT_ENTRIES)
    hits: tuple[RetrievalHit, ...] = Field(max_length=20)
    next_offset: int | None = Field(default=None, ge=0, le=MAX_SNAPSHOT_ENTRIES)
    source_closure_bytes: int = Field(ge=0, le=MAX_CLOSURE_BYTES)
    index_bytes: int = Field(ge=0, le=MAX_CLOSURE_BYTES)
    ranking_status: Literal["relevance_not_applicability"] = "relevance_not_applicability"

    @model_validator(mode="after")
    def valid_payload(self) -> RetrievalPayload:
        require_hash(self.index_sha256)
        if query_char_count(self.query) > self.retriever.max_query_chars:
            raise ValueError("query exceeds the frozen retriever character limit")
        limit = self.retriever.max_results if self.query.limit is None else self.query.limit
        if limit > self.retriever.max_results:
            raise ValueError("query limit exceeds frozen retriever maximum")
        remaining = max(0, self.candidate_count - self.query.offset)
        if len(self.hits) != min(limit, remaining):
            raise ValueError("retrieval page does not cover its declared candidate range")
        following = self.query.offset + len(self.hits)
        expected_next = following if self.hits and following < self.candidate_count else None
        if self.next_offset != expected_next:
            raise ValueError("retrieval next offset disagrees with its page")
        if self.positive_count > self.candidate_count:
            raise ValueError("positive candidates exceed all candidates")
        if sum(item.score > 0 for item in self.hits) > self.positive_count:
            raise ValueError("returned positive hits exceed declared positive candidates")
        _unique(tuple(item.entry_id for item in self.hits))
        ordering = tuple((-item.score, item.entry_id) for item in self.hits)
        if ordering != tuple(sorted(ordering)):
            raise ValueError("retrieval hits require score descending and identity ascending order")
        tags = set(query_tags(self.query))
        terms = set(query_terms(self.query.text))
        for hit in self.hits:
            score = self.retriever.tag_weight * len(hit.matched_tags)
            score += self.retriever.text_weight * len(hit.matched_terms)
            if hit.score != score:
                raise ValueError("retrieval score disagrees with matched tags and terms")
            if not set(hit.matched_tags) <= tags or not set(hit.matched_terms) <= terms:
                raise ValueError("retrieval matches are absent from the normalized query")
        return self


class RetrievalRecord(_Frozen):
    """Canonical immutable retrieval evidence; contents() returns a fresh copy."""

    record_id: str
    content_sha256: str
    payload_json: str = Field(min_length=1, max_length=MAX_PAYLOAD_BYTES)

    @model_validator(mode="after")
    def valid_record(self) -> RetrievalRecord:
        require_hash(self.content_sha256)
        require_identifiers([self.record_id])
        _load(self.payload_json)
        payload = RetrievalPayload.model_validate_json(self.payload_json)
        if encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256:
            raise ValueError("retrieval canonical content or hash changed")
        if self.record_id != "retrieval-" + self.content_sha256:
            raise ValueError("retrieval identity must bind all of its content")
        return self

    def contents(self) -> dict[str, Any]:
        return _load(self.payload_json)
