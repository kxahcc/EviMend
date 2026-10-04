"""Frozen retrieval ranks complete approved guidance, never its audit metadata.

Fixtures are host declarations. Ranking is not an applicability decision and
the synthetic history here does not authenticate an application repair.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dfx_agent import knowledge, knowledge_retrieval
from dfx_agent.knowledge import HostMaterialRegistry, KnowledgeError, KnowledgeStore
from dfx_agent.knowledge_contracts import (
    ExperienceContent,
    ExperienceDraft,
    ExperienceEntry,
    HistoricalSource,
    HostMaterialManifest,
    KnowledgeSnapshot,
    LimitedValidationScope,
    MaterialGrant,
    PermissionScope,
    RetrieverSpec,
    SolverProjection,
    content_hash,
    encode,
)
from dfx_agent.knowledge_retrieval import KnowledgeRetriever
from dfx_agent.retrieval_contracts import IndexDescriptor, MemoryQuery, RetrievalRecord


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def guidance(kind: str) -> ExperienceContent:
    values = {
        "width": {
            "tags": [" ByteWidth ", "安全"],
            "trigger": "Uint16 construction has insufficient 容量.",
            "statement": "Element width governs the intended allocation.",
            "question": "Which caller chooses the encoding?",
            "instruction": "Read the consumer before resizing storage.",
            "observation": "Required count remains uncertain.",
            "action": "Ask for the actual count.",
            "preserve": "Preserve payload format.",
            "counterexample": "Do not apply this when alignment alone is wrong.",
            "exit": "Stop when byte semantics are contradicted.",
        },
        "offset": {
            "tags": ["bytewidth", "DataView"],
            "trigger": "DataView access uses a suspect OFFSET.",
            "statement": "Confirm intended slice boundary.",
            "question": "What event establishes available 容量?",
            "instruction": "Compare offset and requested span.",
            "observation": "The target write is unobserved.",
            "action": "Trace the use point.",
            "preserve": "Preserve independent fields.",
            "counterexample": "More storage does not prove correct access.",
            "exit": "Stop if current event differs.",
        },
        "lifecycle": {
            "tags": [],
            "trigger": "Startup race for 生命周期.",
            "statement": "Object generation must match the operation.",
            "question": "Was initialization observed before use?",
            "instruction": "Check the object state at the operation.",
            "observation": "State remains unknown.",
            "action": "Request a current sample.",
            "preserve": "Preserve sequence behavior.",
            "counterexample": "Retries may hide semantic loss.",
            "exit": "Stop on conflicting generation.",
        },
    }[kind]
    return ExperienceContent.model_validate_json(encode({
        "tags": values["tags"], "trigger": values["trigger"],
        "premises": [{"premise_id": "idonlypremise", "statement": values["statement"],
                      "unknown_question": values["question"]}],
        "procedure": [{"step_id": "idonlystep", "instruction": values["instruction"],
                       "branches": [{"observation": values["observation"],
                                     "next_action": values["action"]}]}],
        "preserves": [values["preserve"]], "counterexamples": [values["counterexample"]],
        "exit_conditions": [values["exit"]],
    }))


def audit_history() -> HistoricalSource:
    return HistoricalSource(
        task_id="taskonlyneedle", run_id="runonlyneedle", lineage_group_id="lineageonlyneedle",
        source_snapshot_sha256="a" * 64, report_sha256="b" * 64, artifact_sha256="c" * 64,
        validation=LimitedValidationScope(level="synthetic_fixture", result="failed",
                                          scope_names=("scopeonlyneedle",)),
        oracle_descriptor="auditoronlyneedle", environment_descriptor="environmentonlyneedle",
    )


def admit(store: KnowledgeStore, source: Path, kind: str, *, with_history: bool = False,
          content: ExperienceContent | None = None) -> ExperienceEntry:
    source.mkdir(parents=True)
    history = audit_history() if with_history else None
    projection = SolverProjection(
        guidance=content or guidance(kind), derivation_authority="model_inference" if with_history else "host_curated",
        validation_scopes=() if history is None else (history.validation,),
    )
    materials: list[tuple[str, str, bytes]] = [
        ("projection", "inference_projection" if with_history else "curated_projection",
         encode(projection).encode("utf-8")),
    ]
    if history is not None:
        materials.append(("history", "synthetic_history", encode(history).encode("utf-8")))
    grants = []
    for material_id, role, raw in materials:
        relative = material_id + ".json"
        (source / relative).write_bytes(raw)
        grants.append(MaterialGrant.model_validate({
            "material_id": material_id, "path": relative, "sha256": digest(raw), "bytes": len(raw),
            "role": role, "origin_split": "development", "allowed_uses": ("learn", "solve"),
            "visibility": "audit_only" if role == "synthetic_history" else "solver_approved",
            "allowed_splits": ("development", "candidate_validation", "confirmation"),
            "derivation_call_id": "callonlyneedle" if with_history and material_id == "projection" else None,
            "known_tokens": 7891 if with_history and material_id == "projection" else None,
        }))
    registry = HostMaterialRegistry(source, HostMaterialManifest(registry_id="fixture-registry", grants=tuple(grants)))
    return store.admit_entry(ExperienceDraft(projection_material_id="projection",
                                            history_material_ids=("history",) if with_history else ()), registry)


@dataclass(frozen=True)
class Corpus:
    root: Path
    store: KnowledgeStore
    retriever: KnowledgeRetriever
    entries: dict[str, ExperienceEntry]
    snapshot: KnowledgeSnapshot
    scope: PermissionScope

    def query(self, **changes: Any) -> MemoryQuery:
        return MemoryQuery(snapshot_sha256=self.snapshot.snapshot_sha256, scope=self.scope, **changes)


def corpus(root: Path, *, spec: RetrieverSpec | None = None, indexed: bool = True) -> Corpus:
    store_root = root / "store"
    store_root.mkdir(parents=True)
    store = KnowledgeStore(store_root)
    entries = {kind: admit(store, root / "source" / kind, kind, with_history=kind == "width")
               for kind in ("width", "offset", "lifecycle")}
    scope = PermissionScope(split="confirmation")
    snapshot = store.freeze_snapshot(tuple(item.entry_id for item in entries.values()), scope,
                                     retriever=spec or RetrieverSpec())
    retriever = KnowledgeRetriever(store)
    if indexed:
        retriever.build_index(snapshot.snapshot_sha256, scope)
    return Corpus(store_root, store, retriever, entries, snapshot, scope)


def inventory(root: Path) -> dict[str, tuple[bytes, int]]:
    return {path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


def payload(record: RetrievalRecord) -> dict[str, Any]:
    return record.contents()


def test_constructor_missing_query_and_missing_record_never_create_paths(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    retriever = KnowledgeRetriever(KnowledgeStore(root))
    assert not root.exists()
    with pytest.raises(KnowledgeError):
        retriever.query(MemoryQuery(snapshot_sha256="a" * 64, scope=PermissionScope(split="confirmation")))
    with pytest.raises(KnowledgeError):
        retriever.read_record("retrieval-" + "a" * 64)
    assert not root.exists()


def test_query_requires_explicit_index_and_does_not_build_or_repair_it(tmp_path: Path) -> None:
    value = corpus(tmp_path, indexed=False)
    before = inventory(value.root)
    with pytest.raises(KnowledgeError):
        value.retriever.query(value.query(text="width"))
    assert inventory(value.root) == before and not (value.root / "indexes").exists()


def test_scoring_normalization_unicode_and_complete_projection(tmp_path: Path) -> None:
    value = corpus(tmp_path, spec=RetrieverSpec(tag_weight=3, text_weight=2))
    before = inventory(value.root)
    record = value.retriever.query(value.query(tags=(" BYTEWIDTH ",), text="容量 WIDTH width OFFSET"))
    result = payload(record)
    expected = {
        value.entries["width"].entry_id: (7, ["bytewidth"], ["width", "容量"]),
        value.entries["offset"].entry_id: (9, ["bytewidth"], ["offset", "width", "容量"]),
        value.entries["lifecycle"].entry_id: (0, [], []),
    }
    assert result["candidate_count"] == 3 and result["positive_count"] == 2
    assert result["ranking_status"] == "relevance_not_applicability"
    assert [hit["entry_id"] for hit in result["hits"]] == sorted(expected, key=lambda item: (-expected[item][0], item))
    for hit in result["hits"]:
        score, tags, terms = expected[hit["entry_id"]]
        assert hit["score"] == score and hit["matched_tags"] == tags and hit["matched_terms"] == terms
        entry = next(item for item in value.entries.values() if item.entry_id == hit["entry_id"])
        projection = value.store.read_projection(value.snapshot.snapshot_sha256, entry.entry_id)
        assert hit["projection"] == projection.model_dump(mode="json")
        assert hit["content_sha256"] == entry.content_sha256
        assert hit["projection_sha256"] == content_hash(projection)
        assert hit["projection"]["guidance"]["counterexamples"]
        assert hit["projection"]["guidance"]["premises"][0]["unknown_question"]
        assert "history" not in hit["projection"]
    assert inventory(value.root) == before


def test_unknown_tags_and_zero_scores_keep_every_snapshot_entry(tmp_path: Path) -> None:
    value = corpus(tmp_path)
    record = value.retriever.query(value.query(tags=("unseen-topic",), text="unseenterm"))
    result = payload(record)
    assert result["candidate_count"] == 3 and result["positive_count"] == 0
    assert [hit["entry_id"] for hit in result["hits"]] == sorted(item.entry_id for item in value.entries.values())
    assert all(hit["score"] == 0 and hit["matched_tags"] == [] and hit["matched_terms"] == [] for hit in result["hits"])


@pytest.mark.parametrize("needle", ["idonlypremise", "idonlystep", "model_inference", "host_curated",
                                    "taskonlyneedle", "runonlyneedle", "lineageonlyneedle", "scopeonlyneedle",
                                    "auditoronlyneedle", "environmentonlyneedle", "callonlyneedle", "7891",
                                    "historical_guidance_not_current_evidence", "schema_version", "premise_id"])
def test_search_document_excludes_schema_authority_cost_and_raw_history(tmp_path: Path, needle: str) -> None:
    value = corpus(tmp_path)
    result = payload(value.retriever.query(value.query(text=needle)))
    assert result["candidate_count"] == 3 and result["positive_count"] == 0
    assert all(hit["score"] == 0 and hit["matched_terms"] == [] for hit in result["hits"])


@pytest.mark.parametrize("needle,kind", [("Uint16", "width"), ("encoding", "width"), ("allocation", "width"),
                                       ("resizing", "width"), ("uncertain", "width"), ("actual", "width"),
                                       ("payload", "width"), ("alignment", "width"), ("contradicted", "width"),
                                       ("生命周期", "lifecycle"), ("dataview", "offset")])
def test_each_visible_guidance_value_is_searchable(tmp_path: Path, needle: str, kind: str) -> None:
    value = corpus(tmp_path)
    result = payload(value.retriever.query(value.query(text=needle)))
    assert result["hits"][0]["entry_id"] == value.entries[kind].entry_id
    assert result["hits"][0]["matched_terms"] == [needle.casefold()]
    assert result["hits"][0]["score"] > 0


def test_empty_browse_and_pagination_have_frozen_stable_ties(tmp_path: Path) -> None:
    value = corpus(tmp_path)
    before = inventory(value.root)
    complete = payload(value.retriever.query(value.query()))
    seen = []
    for offset in range(3):
        result = payload(value.retriever.query(value.query(offset=offset, limit=1)))
        assert result["candidate_count"] == 3 and result["positive_count"] == 0
        seen.extend(hit["entry_id"] for hit in result["hits"])
        assert result["next_offset"] == (offset + 1 if offset < 2 else None)
    assert seen == [hit["entry_id"] for hit in complete["hits"]]
    final = payload(value.retriever.query(value.query(offset=3, limit=1)))
    assert final["hits"] == [] and final["next_offset"] is None
    assert inventory(value.root) == before


def test_record_publish_is_explicit_idempotent_and_read_projection_requires_a_hit(tmp_path: Path) -> None:
    value = corpus(tmp_path)
    query = value.query(text="alignment", limit=1)
    before = inventory(value.root)
    prospective = value.retriever.query(query)
    assert inventory(value.root) == before and not (value.root / "retrievals").exists()
    published = value.retriever.record(query)
    assert published == prospective
    after = inventory(value.root)
    assert value.retriever.record(query) == published and inventory(value.root) == after
    reopened = KnowledgeRetriever(KnowledgeStore(value.root))
    assert reopened.read_record(published.record_id) == published
    hit_id = payload(published)["hits"][0]["entry_id"]
    assert reopened.read_projection(published.record_id, hit_id) == value.store.read_projection(
        value.snapshot.snapshot_sha256, hit_id)
    other = next(entry.entry_id for entry in value.entries.values() if entry.entry_id != hit_id)
    with pytest.raises(KnowledgeError):
        reopened.read_projection(published.record_id, other)
    assert inventory(value.root) == after


def test_new_entries_and_new_index_leave_old_snapshot_query_and_record_unchanged(tmp_path: Path) -> None:
    value = corpus(tmp_path)
    query = value.query(text="generation")
    old = value.retriever.record(query)
    old_files = inventory(value.root)
    extra = admit(value.store, tmp_path / "source" / "extra", "lifecycle")
    # The same complete content is intentionally deduplicated, then a changed
    # host source makes a genuinely new materialized entry for the new snapshot.
    assert extra.entry_id == value.entries["lifecycle"].entry_id
    extra_source = tmp_path / "source" / "extra"
    raw = (extra_source / "projection.json").read_bytes().replace(b"Startup", b"Changed")
    (extra_source / "projection.json").write_bytes(raw)
    grant = MaterialGrant(material_id="projection", path="projection.json", sha256=digest(raw), bytes=len(raw),
                          role="curated_projection", origin_split="development", visibility="solver_approved",
                          allowed_uses=("learn", "solve"), allowed_splits=("development", "candidate_validation", "confirmation"))
    registry = HostMaterialRegistry(extra_source, HostMaterialManifest(registry_id="new-registry", grants=(grant,)))
    changed = value.store.admit_entry(ExperienceDraft(projection_material_id="projection"), registry)
    fresh = value.store.freeze_snapshot(tuple(item.entry_id for item in value.entries.values()) + (changed.entry_id,), value.scope)
    value.retriever.build_index(fresh.snapshot_sha256, value.scope)
    assert value.retriever.query(query) == old and value.retriever.read_record(old.record_id) == old
    assert payload(value.retriever.query(MemoryQuery(snapshot_sha256=fresh.snapshot_sha256, scope=value.scope)))["candidate_count"] == 4
    now = inventory(value.root)
    assert all(now[path] == contents for path, contents in old_files.items())


@pytest.mark.parametrize("tags", [("width", " WIDTH "), ("Straße", " STRASSE "), (" ",)])
def test_normalized_duplicate_or_blank_query_tags_fail_before_source_io(tmp_path: Path, tags: tuple[str, ...]) -> None:
    root = tmp_path / "not-created"
    with pytest.raises(ValueError):
        MemoryQuery(snapshot_sha256="a" * 64, scope=PermissionScope(split="confirmation"), tags=tags)
    assert not root.exists()


@pytest.mark.parametrize("changes", [{"scope": PermissionScope(split="development")},
                                     {"scope": PermissionScope(split="confirmation", excluded_lineage_groups=("new-exclusion",))},
                                     {"limit": 3}, {"text": "x" * 25}, {"tags": (" " * 21 + "x",), "text": "abc"}])
def test_scope_and_frozen_query_limits_fail_before_index_io(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]) -> None:
    value = corpus(tmp_path, spec=RetrieverSpec(max_results=2, max_query_chars=24))
    before = inventory(value.root)
    original_read = knowledge._read

    def reject_index_read(root: Path, path: Path, **kwargs: Any) -> bytes:
        if path.parent.name == "indexes":
            pytest.fail("scope and frozen query limits must fail before reading the index")
        return original_read(root, path, **kwargs)

    parameters = {"snapshot_sha256": value.snapshot.snapshot_sha256, "scope": value.scope, **changes}
    with monkeypatch.context() as patched:
        patched.setattr(knowledge, "_read", reject_index_read)
        with pytest.raises(KnowledgeError):
            value.retriever.query(MemoryQuery.model_validate(parameters))
    assert inventory(value.root) == before


def test_scope_requires_the_full_frozen_value_and_build_does_not_widen_it(tmp_path: Path) -> None:
    value = corpus(tmp_path, indexed=False)
    before = inventory(value.root)
    with pytest.raises(KnowledgeError):
        value.retriever.build_index(value.snapshot.snapshot_sha256, PermissionScope(split="development"))
    assert inventory(value.root) == before and not (value.root / "indexes").exists()


def test_literal_sql_metacharacters_do_not_change_selection_or_schema(tmp_path: Path) -> None:
    value = corpus(tmp_path)
    before = inventory(value.root)
    result = payload(value.retriever.query(value.query(tags=("' OR 1=1; DROP TABLE entries; --",))))
    assert result["candidate_count"] == 3 and result["positive_count"] == 0
    assert all(hit["score"] == 0 for hit in result["hits"])
    assert inventory(value.root) == before
    assert payload(value.retriever.query(value.query(text="alignment")))["positive_count"] == 1


def test_index_publication_is_idempotent_and_descriptor_binds_frozen_retriever(tmp_path: Path) -> None:
    value = corpus(tmp_path)
    before = inventory(value.root)
    descriptor = value.retriever.build_index(value.snapshot.snapshot_sha256, value.scope)
    assert isinstance(descriptor, IndexDescriptor)
    assert descriptor.indexed_entries == 3 and descriptor.snapshot_sha256 == value.snapshot.snapshot_sha256
    assert descriptor.retriever_sha256 == content_hash(RetrieverSpec())
    database = value.root / "indexes" / (value.snapshot.snapshot_sha256 + ".sqlite")
    assert digest(database.read_bytes()) == descriptor.sqlite_sha256
    assert database.stat().st_size == descriptor.sqlite_bytes
    assert inventory(value.root) == before


def forge_record(value: Corpus, changed: dict[str, Any]) -> RetrievalRecord:
    encoded = encode(changed)
    content = content_hash(changed)
    forged = RetrievalRecord(record_id="retrieval-" + content, content_sha256=content, payload_json=encoded)
    directory = value.root / "retrievals"
    directory.mkdir(exist_ok=True)
    (directory / (forged.record_id + ".json")).write_bytes(encode(forged).encode("utf-8"))
    return forged


@pytest.mark.parametrize("change", ["index_identity", "positive_count", "byte_count", "projection", "false_match"])
def test_canonical_rehashed_record_is_recomputed_against_current_frozen_closure(tmp_path: Path, change: str) -> None:
    value = corpus(tmp_path)
    original = value.retriever.query(value.query(text="alignment phantomneedle", limit=1))
    changed = copy.deepcopy(payload(original))
    if change == "index_identity":
        changed["index_sha256"] = "f" * 64
    elif change == "positive_count":
        changed["positive_count"] = 3
    elif change == "byte_count":
        changed["source_closure_bytes"] += 1
    elif change == "projection":
        changed["hits"][0]["projection"]["guidance"]["trigger"] += " forged prose"
        changed["hits"][0]["projection_sha256"] = content_hash(changed["hits"][0]["projection"])
    else:
        changed["hits"][0]["matched_terms"] = ["alignment", "phantomneedle"]
        changed["hits"][0]["score"] = 2
    forged = forge_record(value, changed)
    before = inventory(value.root)
    with pytest.raises(KnowledgeError):
        value.retriever.read_record(forged.record_id)
    with pytest.raises(KnowledgeError):
        value.retriever.read_projection(forged.record_id, changed["hits"][0]["entry_id"])
    assert inventory(value.root) == before


def index_paths(value: Corpus) -> tuple[Path, Path]:
    stem = value.root / "indexes" / value.snapshot.snapshot_sha256
    return stem.with_suffix(".sqlite"), stem.with_suffix(".json")


def rewrite_index(value: Corpus, mutation: str) -> None:
    """Rehash every storage declaration so runtime must compare actual rows.

    This is an adversarial host fixture, not an index publication API.
    """
    database, manifest = index_paths(value)
    descriptor = json.loads(manifest.read_bytes())
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(database.read_bytes())
        first = value.entries["width"].entry_id
        if mutation == "search_text":
            connection.execute("UPDATE entries SET search_text=? WHERE entry_id=?", ("forged searchable answer", first))
        elif mutation == "projection_hash":
            connection.execute("UPDATE entries SET projection_sha256=? WHERE entry_id=?", ("f" * 64, first))
        elif mutation == "drop_entry":
            connection.execute("DELETE FROM entries WHERE entry_id=?", (first,))
        elif mutation == "extra_entry":
            connection.execute("INSERT INTO entries VALUES(?,?,?)", ("experience-" + "f" * 64, "e" * 64, "forged"))
        elif mutation == "extra_tag":
            connection.execute("INSERT INTO tags VALUES(?,?)", (first, "forgedtag"))
        elif mutation == "drop_tag":
            connection.execute("DELETE FROM tags WHERE entry_id=?", (first,))
        elif mutation == "meta_snapshot":
            connection.execute("UPDATE meta SET snapshot_sha256=?", ("f" * 64,))
        elif mutation == "meta_retriever":
            connection.execute("UPDATE meta SET retriever_sha256=?", ("f" * 64,))
        elif mutation == "schema_table":
            connection.execute("CREATE TABLE unwanted(secret TEXT)")
        else:
            connection.execute("CREATE TRIGGER unwanted AFTER INSERT ON entries BEGIN DELETE FROM tags; END")
        rows = {"entries": connection.execute("SELECT entry_id,projection_sha256,search_text FROM entries ORDER BY entry_id").fetchall(),
                "tags": connection.execute("SELECT entry_id,tag FROM tags ORDER BY entry_id,tag").fetchall()}
        row_hash = content_hash(rows)
        count = len(rows["entries"])
        connection.execute("UPDATE meta SET rows_sha256=?,indexed_entries=?", (row_hash, count))
        connection.commit()
        raw = connection.serialize()
    finally:
        connection.close()
    database.write_bytes(raw)
    descriptor.update(sqlite_sha256=digest(raw), sqlite_bytes=len(raw), rows_sha256=row_hash, indexed_entries=count)
    validated = IndexDescriptor.model_validate(descriptor)
    manifest.write_bytes(encode(validated).encode("utf-8"))


@pytest.mark.parametrize("mutation", ["search_text", "projection_hash", "drop_entry", "extra_entry", "extra_tag",
                                       "drop_tag", "meta_snapshot", "meta_retriever", "schema_table", "schema_trigger"])
def test_rehashed_database_and_descriptor_still_require_exact_snapshot_rows(tmp_path: Path, mutation: str) -> None:
    value = corpus(tmp_path)
    record = value.retriever.record(value.query(text="width"))
    rewrite_index(value, mutation)
    before = inventory(value.root)
    for operation in (lambda: value.retriever.query(value.query(text="width")),
                      lambda: value.retriever.read_record(record.record_id),
                      lambda: value.retriever.build_index(value.snapshot.snapshot_sha256, value.scope)):
        with pytest.raises(KnowledgeError):
            operation()
    assert inventory(value.root) == before


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_any_sqlite_sidecar_blocks_read_and_publication_without_cleanup(tmp_path: Path, suffix: str) -> None:
    value = corpus(tmp_path)
    database, _ = index_paths(value)
    sidecar = Path(str(database) + suffix)
    sidecar.write_bytes(b"host-owned-sidecar")
    before = inventory(value.root)
    with pytest.raises(KnowledgeError):
        value.retriever.query(value.query())
    with pytest.raises(KnowledgeError):
        value.retriever.build_index(value.snapshot.snapshot_sha256, value.scope)
    assert inventory(value.root) == before and sidecar.read_bytes() == b"host-owned-sidecar"


@pytest.mark.parametrize("location", ["database", "descriptor", "snapshot", "entry", "material"])
@pytest.mark.parametrize("change", ["missing", "same_length_restore_mtime"])
def test_read_requires_every_original_closure_member(tmp_path: Path, location: str, change: str) -> None:
    value = corpus(tmp_path)
    record = value.retriever.record(value.query(text="width"))
    database, descriptor = index_paths(value)
    entry = value.entries["width"]
    material_hash = entry.contents()["source_grants"][0]["sha256"]
    target = {"database": database, "descriptor": descriptor,
              "snapshot": value.root / "snapshots" / (value.snapshot.snapshot_sha256 + ".json"),
              "entry": value.root / "entries" / (entry.entry_id + ".json"),
              "material": value.root / "materials" / (material_hash + ".blob")}[location]
    if change == "missing":
        target.unlink()
    else:
        raw = target.read_bytes()
        before = target.stat()
        target.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert target.stat().st_size == before.st_size and target.stat().st_mtime_ns == before.st_mtime_ns
    before_inventory = inventory(value.root)
    with pytest.raises(KnowledgeError):
        value.retriever.query(value.query())
    with pytest.raises(KnowledgeError):
        value.retriever.read_record(record.record_id)
    assert inventory(value.root) == before_inventory


def test_empty_frozen_snapshot_has_an_explicit_empty_index_and_no_latest_fallback(tmp_path: Path) -> None:
    value = corpus(tmp_path)
    empty = value.store.freeze_snapshot((), value.scope)
    descriptor = value.retriever.build_index(empty.snapshot_sha256, value.scope)
    assert descriptor.indexed_entries == 0
    result = payload(value.retriever.query(MemoryQuery(snapshot_sha256=empty.snapshot_sha256, scope=value.scope)))
    assert result["candidate_count"] == 0 and result["positive_count"] == 0
    assert result["hits"] == [] and result["next_offset"] is None
    before = inventory(value.root)
    with pytest.raises(KnowledgeError):
        value.retriever.query(MemoryQuery(snapshot_sha256="f" * 64, scope=value.scope))
    assert inventory(value.root) == before


def test_query_preserves_original_tag_spelling_and_frozen_configuration(tmp_path: Path) -> None:
    value = corpus(tmp_path, spec=RetrieverSpec(tag_weight=4, text_weight=0, max_results=1))
    record = value.retriever.query(value.query(tags=(" 安全 ",)))
    result = payload(record)
    assert result["query"]["tags"] == [" 安全 "]
    assert result["retriever"]["tag_weight"] == 4 and result["retriever"]["text_weight"] == 0
    assert len(result["hits"]) == 1 and result["hits"][0]["entry_id"] == value.entries["width"].entry_id
    assert result["hits"][0]["score"] == 4 and result["hits"][0]["matched_tags"] == ["安全"]


def test_same_normalized_entry_tag_is_one_match_and_never_duplicates_score(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    store = KnowledgeStore(root)
    modified = guidance("width").model_dump(mode="json")
    modified["tags"] = ["ByteWidth", " bytewidth ", "BYTEWIDTH"]
    content = ExperienceContent.model_validate_json(encode(modified))
    entry = admit(store, tmp_path / "source", "width", content=content)
    scope = PermissionScope(split="confirmation")
    snapshot = store.freeze_snapshot((entry.entry_id,), scope)
    retriever = KnowledgeRetriever(store)
    retriever.build_index(snapshot.snapshot_sha256, scope)
    result = payload(retriever.query(MemoryQuery(snapshot_sha256=snapshot.snapshot_sha256, scope=scope,
                                               tags=("bytewidth",))))
    assert result["hits"][0]["score"] == 2 and result["hits"][0]["matched_tags"] == ["bytewidth"]


@pytest.mark.parametrize("gate", ["bytes", "calls"])
def test_cumulative_read_budget_stops_before_the_first_original_byte_read(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str) -> None:
    value = corpus(tmp_path)
    before = inventory(value.root)

    def unexpected_read(*_args: Any, **_kwargs: Any) -> bytes:
        pytest.fail("an exhausted cumulative read gate must reject before material byte I/O")

    with monkeypatch.context() as patched:
        patched.setattr(knowledge_retrieval, "MAX_INDEX_READ_BYTES" if gate == "bytes" else "MAX_INDEX_READ_CALLS", 0)
        patched.setattr(knowledge, "_read", unexpected_read)
        with pytest.raises(KnowledgeError):
            value.retriever.query(value.query())
    assert inventory(value.root) == before


def test_sqlite_step_budget_is_enforced_during_real_index_queries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = corpus(tmp_path)
    before = inventory(value.root)
    with monkeypatch.context() as patched:
        patched.setattr(knowledge_retrieval, "MAX_SQL_STEPS", 1)
        with pytest.raises(KnowledgeError):
            value.retriever.query(value.query(tags=("bytewidth",), text="width 容量"))
    assert inventory(value.root) == before


def test_final_query_evidence_envelope_counts_in_the_whole_closure_limit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = corpus(tmp_path)
    query = value.query(text="alignment", limit=1)
    reference = value.retriever.query(query)
    result = payload(reference)
    envelope_bytes = len(encode(reference).encode("utf-8"))
    expected_limit = result["source_closure_bytes"] + result["index_bytes"] + envelope_bytes
    assert expected_limit == sum(path.stat().st_size for path in value.root.rglob("*") if path.is_file()) + envelope_bytes
    before = inventory(value.root)
    with monkeypatch.context() as patched:
        patched.setattr(knowledge, "MAX_CLOSURE_BYTES", expected_limit - 1)
        with pytest.raises(KnowledgeError):
            value.retriever.query(query)
        with pytest.raises(KnowledgeError):
            value.retriever.record(query)
        assert inventory(value.root) == before and not (value.root / "retrievals").exists()
        patched.setattr(knowledge, "MAX_CLOSURE_BYTES", expected_limit)
        assert value.retriever.query(query) == reference
        published = value.retriever.record(query)
        assert published == reference and value.retriever.read_record(published.record_id) == reference


def test_new_index_capacity_is_checked_before_creating_publication_paths(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = corpus(tmp_path, indexed=False)
    reference = corpus(tmp_path / "reference")
    database, manifest = index_paths(reference)
    source_size = sum(path.stat().st_size for path in value.root.rglob("*") if path.is_file())
    expected_limit = source_size + database.stat().st_size + manifest.stat().st_size
    before = inventory(value.root)
    with monkeypatch.context() as patched:
        patched.setattr(knowledge, "MAX_CLOSURE_BYTES", expected_limit - 1)
        with pytest.raises(KnowledgeError):
            value.retriever.build_index(value.snapshot.snapshot_sha256, value.scope)
        assert inventory(value.root) == before and not (value.root / "indexes").exists()
        patched.setattr(knowledge, "MAX_CLOSURE_BYTES", expected_limit)
        assert value.retriever.build_index(value.snapshot.snapshot_sha256, value.scope).indexed_entries == 3


@pytest.mark.parametrize("spec,parameters", [
    (RetrieverSpec(tag_weight=2**63, text_weight=0), {}),
    (RetrieverSpec(tag_weight=0, text_weight=2**63), {"offset": 4}),
    (RetrieverSpec(tag_weight=2**62, text_weight=0), {"tags": ("bytewidth", "unseen-topic")}),
    (RetrieverSpec(tag_weight=2**63 - 1, text_weight=1), {"tags": ("bytewidth",), "text": "alignment"}),
    (RetrieverSpec(tag_weight=0, text_weight=2**62), {"text": "alignment phantomneedle", "offset": 4}),
])
def test_unrepresentable_score_capability_fails_before_index_even_for_empty_page(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spec: RetrieverSpec, parameters: dict[str, Any]) -> None:
    value = corpus(tmp_path, spec=spec)
    before = inventory(value.root)

    def forbidden_index(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("SQLite integer capability must be checked before reading an index")

    with monkeypatch.context() as patched:
        patched.setattr(KnowledgeRetriever, "_index", forbidden_index)
        with pytest.raises(KnowledgeError):
            value.retriever.query(value.query(**parameters))
    assert inventory(value.root) == before


@pytest.mark.parametrize("location", ["indexes", "root", "ancestor"])
@pytest.mark.parametrize("operation", ["query", "build"])
def test_reparse_parent_is_rejected_before_any_sidecar_leaf_probe(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str, operation: str) -> None:
    value = corpus(tmp_path)
    before = inventory(value.root)
    targeted = {"indexes": value.root / "indexes", "root": value.root, "ancestor": tmp_path}[location]
    original_stat = Path.lstat

    def guarded_stat(path: Path):
        if path.name.endswith(("-wal", "-journal", "-shm")):
            pytest.fail("sidecar leaves must not be probed through a reparse ancestor")
        info = original_stat(path)
        if path == targeted:
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino, st_mode=info.st_mode,
                                   st_nlink=info.st_nlink, st_size=info.st_size,
                                   st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return info

    with monkeypatch.context() as patched:
        patched.setattr(Path, "lstat", guarded_stat)
        with pytest.raises(KnowledgeError):
            if operation == "query":
                value.retriever.query(value.query())
            else:
                value.retriever.build_index(value.snapshot.snapshot_sha256, value.scope)
    assert inventory(value.root) == before


def test_fresh_build_runs_inside_sqlite_step_budget_and_publishes_nothing_on_exhaustion(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = corpus(tmp_path, indexed=False)
    before = inventory(value.root)
    with monkeypatch.context() as patched:
        patched.setattr(knowledge_retrieval, "MAX_SQL_STEPS", 1)
        with pytest.raises(KnowledgeError):
            value.retriever.build_index(value.snapshot.snapshot_sha256, value.scope)
    assert inventory(value.root) == before and not (value.root / "indexes").exists()


@pytest.mark.parametrize("header", ["declared_page_count", "invalid_page_size", "format"])
def test_rehashed_invalid_database_page_header_fails_before_sqlite_deserialization(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header: str) -> None:
    value = corpus(tmp_path)
    database, manifest = index_paths(value)
    raw = bytearray(database.read_bytes())
    if header == "declared_page_count":
        raw[28:32] = (2**32 - 1).to_bytes(4, "big")
    elif header == "invalid_page_size":
        raw[16:18] = (1023).to_bytes(2, "big")
    else:
        raw[0] ^= 1
    descriptor = json.loads(manifest.read_bytes())
    descriptor["sqlite_sha256"] = digest(bytes(raw))
    database.write_bytes(raw)
    manifest.write_bytes(encode(IndexDescriptor.model_validate(descriptor)).encode("utf-8"))
    before = inventory(value.root)

    def forbidden_connection(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("invalid declared page bytes must fail before SQLite allocation or deserialization")

    with monkeypatch.context() as patched:
        patched.setattr(sqlite3, "connect", forbidden_connection)
        with pytest.raises(KnowledgeError):
            value.retriever.query(value.query())
    assert inventory(value.root) == before


@pytest.mark.parametrize("gate", ["bytes", "calls"])
def test_nonzero_read_budget_is_charged_for_repeated_original_reads_and_stops_before_next_io(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str) -> None:
    value = corpus(tmp_path)
    snapshot_path = value.root / "snapshots" / (value.snapshot.snapshot_sha256 + ".json")
    before = inventory(value.root)
    seen: list[Path] = []
    original_read = knowledge._read
    allowed = 1 if gate == "bytes" else 2

    def observed_read(root: Path, path: Path, **kwargs: Any) -> bytes:
        if len(seen) >= allowed:
            pytest.fail("the next over-budget original read must not execute")
        seen.append(path)
        return original_read(root, path, **kwargs)

    with monkeypatch.context() as patched:
        if gate == "bytes":
            patched.setattr(knowledge_retrieval, "MAX_INDEX_READ_BYTES", snapshot_path.stat().st_size)
        else:
            patched.setattr(knowledge_retrieval, "MAX_INDEX_READ_CALLS", allowed)
        patched.setattr(knowledge, "_read", observed_read)
        with pytest.raises(KnowledgeError):
            value.retriever.query(value.query())
    assert len(seen) == allowed and seen == [snapshot_path] * allowed
    assert inventory(value.root) == before
