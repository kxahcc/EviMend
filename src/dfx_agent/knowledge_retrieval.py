"""Bounded, reproducible retrieval of host-approved historical guidance.

SQLite indexes are derived artifacts, never permission or evidence authorities.
Every operation authenticates the original snapshot and exact index rows. No
model, Agent tool, applicability judgment or automatic learning is exposed.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any

from .contracts import require_hash
from .knowledge import (
    KnowledgeError,
    KnowledgeStore,
    _copy,
    _exists,
    _object,
    _path,
    _ReadSet,
)
from .knowledge_contracts import (
    PermissionScope,
    SnapshotPayload,
    SolverProjection,
    content_hash,
    encode,
)
from .retrieval_contracts import (
    MAX_INDEX_READ_BYTES,
    MAX_INDEX_READ_CALLS,
    MAX_SQL_BYTES,
    MAX_SQL_STEPS,
    IndexDescriptor,
    MemoryQuery,
    RetrievalHit,
    RetrievalPayload,
    RetrievalRecord,
    query_char_count,
    query_terms,
)
from .task import digest

_INVALID = (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError,
            sqlite3.Error, OverflowError)
_DDL = (
    "CREATE TABLE meta(snapshot_sha256 TEXT, retriever_sha256 TEXT, rows_sha256 TEXT, indexed_entries INTEGER)",
    "CREATE TABLE entries(entry_id TEXT PRIMARY KEY, projection_sha256 TEXT NOT NULL, search_text TEXT NOT NULL) WITHOUT ROWID",
    "CREATE TABLE tags(entry_id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY(entry_id,tag)) WITHOUT ROWID",
)


def _document(projection: SolverProjection) -> tuple[tuple[str, ...], str]:
    content = projection.guidance
    tags = tuple(sorted({tag.strip().casefold() for tag in content.tags}))
    values = [*content.tags, content.trigger]
    for premise in content.premises:
        values.extend((premise.statement, premise.unknown_question))
    for step in content.procedure:
        values.append(step.instruction)
        for branch in step.branches:
            values.extend((branch.observation, branch.next_action))
    values.extend((*content.preserves, *content.counterexamples, *content.exit_conditions))
    return tags, "\n".join(values).casefold()


def _rows(contents: SnapshotPayload, projections: dict[str, SolverProjection]) -> tuple[list[tuple], list[tuple]]:
    entries, tags = [], []
    for ref in contents.entries:
        entry_tags, text = _document(projections[ref.entry_id])
        entries.append((ref.entry_id, ref.projection_sha256, text))
        tags.extend((ref.entry_id, tag) for tag in entry_tags)
    return entries, tags


def _row_hash(entries: list[tuple], tags: list[tuple]) -> str:
    return content_hash({"entries": entries, "tags": tags})


def _check_scope(actual: PermissionScope, expected: PermissionScope) -> None:
    if actual != expected:
        raise KnowledgeError("knowledge_permission_denied")


def _check_query(query: MemoryQuery, contents: SnapshotPayload) -> None:
    _check_scope(contents.scope, query.scope)
    if (query_char_count(query) > contents.retriever.max_query_chars or
            (query.limit is not None and query.limit > contents.retriever.max_results)):
        raise ValueError("query exceeds its frozen retrieval policy")
    weight_tag, weight_text = contents.retriever.tag_weight, contents.retriever.text_weight
    maximum = weight_tag * len(query.tags) + weight_text * len(query_terms(query.text))
    if max(weight_tag, weight_text, maximum) > 2**63 - 1:
        raise ValueError("query scoring exceeds the integer capability of tags_text_v1")


def _sidecars(root, path) -> None:
    _path(root, root, directory=True)
    if _exists(path.parent):
        _path(root, path.parent, directory=True)
    for suffix in ("-wal", "-journal", "-shm"):
        if _exists(path.with_name(path.name + suffix)):
            raise ValueError("knowledge index has a sidecar")


def _limit_steps(connection: sqlite3.Connection) -> None:
    steps = 0

    def progress() -> int:
        nonlocal steps
        steps += 1
        return int(steps > MAX_SQL_STEPS)

    connection.set_progress_handler(progress, 1)


@contextmanager
def _connection(raw: bytes):
    if len(raw) < 100 or len(raw) > MAX_SQL_BYTES or raw[:16] != b"SQLite format 3\x00":
        raise ValueError("knowledge index header or byte limit invalid")
    page_size = int.from_bytes(raw[16:18], "big")
    page_size = 65_536 if page_size == 1 else page_size
    page_count = int.from_bytes(raw[28:32], "big")
    if (page_size < 512 or page_size > 65_536 or page_size & (page_size - 1) or
            page_count <= 0 or page_size * page_count != len(raw)):
        raise ValueError("knowledge index page bound invalid")
    connection = sqlite3.connect(":memory:")
    try:
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_SQL_BYTES)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 65_536)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 16)
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 512)
        connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 64)
        connection.deserialize(raw)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        _limit_steps(connection)
        if (connection.execute("PRAGMA page_size").fetchone() != (page_size,) or
                connection.execute("PRAGMA page_count").fetchone() != (page_count,)):
            raise ValueError("knowledge index declared pages differ")
        schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name").fetchall()
        expected = sorted(("table", name, name, ddl) for name, ddl in zip(("meta", "entries", "tags"), _DDL, strict=True))
        if schema != expected:
            raise ValueError("knowledge index schema differs")
        yield connection
    finally:
        connection.close()


def _values(name: str, values: tuple[str, ...]) -> str:
    return name + " AS (" + ("VALUES " + ",".join("(?)" for _ in values) if values else "SELECT NULL WHERE 0") + ")"


def _rank(connection: sqlite3.Connection, query: MemoryQuery, contents: SnapshotPayload) -> tuple[list[tuple], int]:
    tags = tuple(sorted(tag.strip().casefold() for tag in query.tags))
    terms = query_terms(query.text)
    prefix = "WITH " + _values("query_tags(tag)", tags) + "," + _values("query_terms(term)", terms)
    prefix += (",ranked AS (SELECT e.entry_id, "
               "? * (SELECT COUNT(*) FROM tags t JOIN query_tags q ON t.tag=q.tag WHERE t.entry_id=e.entry_id) + "
               "? * (SELECT COUNT(*) FROM query_terms q WHERE instr(e.search_text,q.term)>0) AS score FROM entries e) ")
    parameters: tuple[Any, ...] = (*tags, *terms, contents.retriever.tag_weight, contents.retriever.text_weight)
    positive = connection.execute(prefix + "SELECT COUNT(*) FROM ranked WHERE score>0", parameters).fetchone()[0]
    limit = contents.retriever.max_results if query.limit is None else query.limit
    page = connection.execute(prefix + "SELECT entry_id,score FROM ranked ORDER BY score DESC,entry_id ASC LIMIT ? OFFSET ?",
                              (*parameters, limit, query.offset)).fetchall()
    return page, positive


class KnowledgeRetriever:
    """Explicit index/record publication, and side-effect-free query/read APIs.

    Host-supplied scopes must equal the snapshot's frozen scope. Binding the
    scope and chosen snapshot to a real task/run belongs to later SDK admission.
    The managed host serializes writes; this is not an OS concurrency sandbox.
    """

    def __init__(self, store: KnowledgeStore):
        self.store = store

    def _reads(self) -> _ReadSet:
        return _ReadSet(self.store.root, max_read_bytes=MAX_INDEX_READ_BYTES, max_read_calls=MAX_INDEX_READ_CALLS)

    def _bundle(self, snapshot_sha256: str, scope: PermissionScope, reads: _ReadSet,
                query: MemoryQuery | None = None) -> tuple[SnapshotPayload, dict[str, SolverProjection]]:
        _, contents = self.store._snapshot_manifest(snapshot_sha256, reads)
        _check_scope(contents.scope, scope)
        if query is not None:
            _check_query(query, contents)
        _, projections = self.store._snapshot(snapshot_sha256, reads=reads)
        return contents, projections

    def _index(self, contents: SnapshotPayload, projections: dict[str, SolverProjection],
               snapshot_sha256: str, reads: _ReadSet) -> tuple[IndexDescriptor, bytes]:
        root = self.store.root / "indexes"
        path = root / (snapshot_sha256 + ".sqlite")
        _sidecars(self.store.root, path)
        descriptor_raw = reads.read(root / (snapshot_sha256 + ".json"))
        descriptor = IndexDescriptor.model_validate_json(encode(_object(descriptor_raw)))
        if (encode(descriptor).encode("utf-8") != descriptor_raw or descriptor.snapshot_sha256 != snapshot_sha256 or
                descriptor.retriever_sha256 != content_hash(contents.retriever) or
                descriptor.indexed_entries != len(contents.entries) or descriptor.sqlite_bytes > MAX_SQL_BYTES):
            raise ValueError("knowledge index descriptor binding differs")
        raw = reads.read(path, expected_hash=descriptor.sqlite_sha256, expected_bytes=descriptor.sqlite_bytes)
        entries, tags = _rows(contents, projections)
        row_hash = _row_hash(entries, tags)
        if descriptor.rows_sha256 != row_hash:
            raise ValueError("knowledge index row identity differs")
        with _connection(raw) as connection:
            meta = connection.execute("SELECT snapshot_sha256,retriever_sha256,rows_sha256,indexed_entries FROM meta").fetchall()
            expected = [(snapshot_sha256, content_hash(contents.retriever), row_hash, len(entries))]
            if (meta != expected or len(meta) != 1 or type(meta[0][3]) is not int or
                    connection.execute("SELECT COUNT(*) FROM entries").fetchone() != (len(entries),) or
                    connection.execute("SELECT COUNT(*) FROM tags").fetchone() != (len(tags),)):
                raise ValueError("knowledge index metadata or row count differs")
            if (connection.execute("SELECT entry_id,projection_sha256,search_text FROM entries ORDER BY entry_id").fetchall() != entries or
                    connection.execute("SELECT entry_id,tag FROM tags ORDER BY entry_id,tag").fetchall() != tags):
                raise ValueError("knowledge index derived rows differ from approved guidance")
        _sidecars(self.store.root, path)
        return descriptor, raw

    def build_index(self, snapshot_sha256: str, scope: PermissionScope) -> IndexDescriptor:
        try:
            scope = _copy(scope)
            reads = self._reads()
            contents, projections = self._bundle(snapshot_sha256, scope, reads)
            path = self.store.root / "indexes" / (snapshot_sha256 + ".sqlite")
            _sidecars(self.store.root, path)
            descriptor_path = path.with_suffix(".json")
            if _exists(path) or _exists(descriptor_path):
                descriptor, _ = self._index(contents, projections, snapshot_sha256, reads)
                reads.finish()
                return descriptor
            entries, tags = _rows(contents, projections)
            row_hash = _row_hash(entries, tags)
            connection = sqlite3.connect(":memory:")
            try:
                _limit_steps(connection)
                for ddl in _DDL:
                    connection.execute(ddl)
                connection.execute("INSERT INTO meta VALUES (?,?,?,?)",
                                   (snapshot_sha256, content_hash(contents.retriever), row_hash, len(entries)))
                connection.executemany("INSERT INTO entries VALUES (?,?,?)", entries)
                connection.executemany("INSERT INTO tags VALUES (?,?)", tags)
                connection.commit()
                raw = connection.serialize()
            finally:
                connection.close()
            descriptor = IndexDescriptor(snapshot_sha256=snapshot_sha256, sqlite_sha256=digest(raw),
                                         sqlite_bytes=len(raw), indexed_entries=len(entries), rows_sha256=row_hash,
                                         retriever_sha256=content_hash(contents.retriever))
            descriptor_raw = encode(descriptor).encode("utf-8")
            reads.reserve(path, len(raw))
            reads.reserve(descriptor_path, len(descriptor_raw))
            reads.finish()
            self.store._publish("indexes", path.name, raw)
            self.store._publish("indexes", descriptor_path.name, descriptor_raw)
            result = self._index(contents, projections, snapshot_sha256, reads)[0]
            reads.finish()
            return result
        except _INVALID as exc:
            raise KnowledgeError("knowledge_index_invalid") from exc

    def _retrieve(self, query: MemoryQuery, reads: _ReadSet, *, record_bytes: int = 0) -> RetrievalRecord:
        query = _copy(query)
        contents, projections = self._bundle(query.snapshot_sha256, query.scope, reads, query)
        source_bytes = reads.byte_count - record_bytes
        descriptor, raw = self._index(contents, projections, query.snapshot_sha256, reads)
        with _connection(raw) as connection:
            page, positive = _rank(connection, query, contents)
        refs = {ref.entry_id: ref for ref in contents.entries}
        query_tags = {tag.strip().casefold() for tag in query.tags}
        terms = query_terms(query.text)
        hits = []
        for entry_id, score in page:
            projection = projections[entry_id]
            tags, text = _document(projection)
            matched_tags = tuple(sorted(query_tags.intersection(tags)))
            matched_terms = tuple(term for term in terms if term in text)
            expected_score = contents.retriever.tag_weight * len(matched_tags) + contents.retriever.text_weight * len(matched_terms)
            if type(score) is not int or score != expected_score:
                raise ValueError("knowledge index score differs from guidance")
            ref = refs[entry_id]
            hits.append(RetrievalHit(entry_id=entry_id, content_sha256=ref.content_sha256,
                                    projection_sha256=ref.projection_sha256, projection=projection,
                                    score=score, matched_tags=matched_tags, matched_terms=matched_terms))
        next_offset = query.offset + len(hits)
        payload = RetrievalPayload(query=query, index_sha256=content_hash(descriptor), retriever=contents.retriever,
                                   candidate_count=len(refs), positive_count=positive, hits=tuple(hits),
                                   next_offset=next_offset if hits and next_offset < len(refs) else None,
                                   source_closure_bytes=source_bytes,
                                   index_bytes=descriptor.sqlite_bytes + len(encode(descriptor).encode("utf-8")))
        key = content_hash(payload)
        record = RetrievalRecord(record_id="retrieval-" + key, content_sha256=key, payload_json=encode(payload))
        # Include the final evidence envelope in this operation's capacity,
        # including pure queries which do not actually publish it.
        reads.reserve(self.store.root / "retrievals" / (record.record_id + ".json"), len(encode(record).encode("utf-8")))
        return record

    def query(self, query: MemoryQuery) -> RetrievalRecord:
        try:
            reads = self._reads()
            record = self._retrieve(query, reads)
            reads.finish()
            _sidecars(self.store.root, self.store.root / "indexes" / (query.snapshot_sha256 + ".sqlite"))
            return record
        except _INVALID as exc:
            raise KnowledgeError("knowledge_retrieval_invalid") from exc

    def record(self, query: MemoryQuery) -> RetrievalRecord:
        result = self.query(query)
        try:
            self.store._publish("retrievals", result.record_id + ".json", encode(result).encode("utf-8"))
            return self.read_record(result.record_id)
        except _INVALID as exc:
            raise KnowledgeError("knowledge_retrieval_invalid") from exc

    def read_record(self, record_id: str) -> RetrievalRecord:
        try:
            if not isinstance(record_id, str) or not record_id.startswith("retrieval-"):
                raise ValueError("knowledge retrieval identity invalid")
            require_hash(record_id.removeprefix("retrieval-"))
            reads = self._reads()
            raw = reads.read(self.store.root / "retrievals" / (record_id + ".json"))
            saved = RetrievalRecord.model_validate_json(encode(_object(raw)))
            if saved.record_id != record_id or encode(saved).encode("utf-8") != raw:
                raise ValueError("knowledge retrieval canonical identity differs")
            query = RetrievalPayload.model_validate_json(saved.payload_json).query
            # The record's own bytes do not count as source snapshot bytes.
            expected = self._retrieve(query, reads, record_bytes=len(raw))
            if expected != saved:
                raise ValueError("knowledge retrieval differs from recomputed query or capacity")
            reads.finish()
            _sidecars(self.store.root, self.store.root / "indexes" / (query.snapshot_sha256 + ".sqlite"))
            return saved
        except _INVALID as exc:
            raise KnowledgeError("knowledge_retrieval_invalid") from exc

    def read_projection(self, record_id: str, entry_id: str) -> SolverProjection:
        record = self.read_record(record_id)
        for hit in RetrievalPayload.model_validate_json(record.payload_json).hits:
            if hit.entry_id == entry_id:
                return _copy(hit.projection)
        raise KnowledgeError("knowledge_permission_denied")
