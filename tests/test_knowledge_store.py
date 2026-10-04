"""Host-reviewed guidance keeps permissions, raw provenance and frozen closure.

All materials here are synthetic. Shape validation does not certify that prose
contains no secret answer; admitting its natural language remains a host duty.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from dfx_agent import knowledge
from dfx_agent.knowledge import HostMaterialRegistry, KnowledgeError, KnowledgeStore
from dfx_agent.knowledge_contracts import (
    MAX_PAYLOAD_BYTES,
    MAX_SNAPSHOT_ENTRIES,
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
    SnapshotPayload,
    SnapshotRef,
    SolverProjection,
    content_hash,
    encode,
)


def guidance() -> ExperienceContent:
    return ExperienceContent.model_validate_json(encode({
        "tags": [], "trigger": "A typed view fails while being constructed.",
        "premises": [{"premise_id": "byte-unit", "statement": "Check the intended element width.",
                      "unknown_question": "Which consumer establishes the element width?"}],
        "procedure": [{"step_id": "read-consumer", "instruction": "Read the consumer before changing capacity.",
                       "branches": [{"observation": "Consumer width remains unknown.",
                                     "next_action": "Keep the premise unknown and inspect its caller."}]}],
        "preserves": ["Preserve the intended data encoding."],
        "counterexamples": ["A constructor failure may instead follow a misaligned offset."],
        "exit_conditions": ["Stop copying this procedure when its premise is contradicted."],
    }))


def history(result: str = "passed") -> HistoricalSource:
    return HistoricalSource.model_validate({
        "task_id": "fixture-task", "run_id": "fixture-run", "lineage_group_id": "fixture-family",
        "source_snapshot_sha256": "a" * 64, "candidate_id": "a" * 64,
        "candidate_snapshot_sha256": "b" * 64, "report_sha256": "a" * 64,
        "artifact_sha256": "b" * 64,
        "validation": LimitedValidationScope.model_validate({
            "level": "synthetic_fixture", "result": result, "scope_names": ("synthetic-boundary",),
        }),
        "oracle_descriptor": "Host-declared synthetic text predicate.",
        "environment_descriptor": "An offline fixture; no application execution.",
    })


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class Inputs:
    source: Path
    store_root: Path
    manifest: HostMaterialManifest
    draft: ExperienceDraft

    def registry(self) -> HostMaterialRegistry:
        return HostMaterialRegistry(self.source, self.manifest)


def make_inputs(
        root: Path, *, projection_changes: dict | None = None,
        history_changes: dict | None = None, inferred: bool = False,
        result: str = "passed", raw_projection: bytes | None = None,
        without_history: bool = False) -> Inputs:
    source = root / "source"
    (source / "approved").mkdir(parents=True)
    store_root = root / "store"
    store_root.mkdir()
    record = history(result)
    projection = SolverProjection(guidance=guidance(),
                                  derivation_authority="model_inference" if inferred else "host_curated",
                                  validation_scopes=() if without_history else (record.validation,))
    projection_raw = raw_projection if raw_projection is not None else encode(projection).encode("utf-8")
    history_raw = encode(record).encode("utf-8")
    specifications = [("projection", "inference_projection" if inferred else "curated_projection",
                       projection_raw, projection_changes or {})]
    if not without_history:
        specifications.append(("history", "synthetic_history", history_raw, history_changes or {}))
    grants = []
    for identifier, role, raw, changes in specifications:
        relative = "approved/" + identifier + ".json"
        (source / relative).write_bytes(raw)
        grants.append(MaterialGrant.model_validate({
            "material_id": identifier, "path": relative, "sha256": digest(raw), "bytes": len(raw),
            "role": role, "origin_split": "curated", "visibility": "solver_approved",
            "allowed_uses": ("learn", "solve"), "allowed_splits": ("development", "confirmation"),
            "derivation_call_id": "extraction-call" if inferred and identifier == "projection" else None,
            "known_tokens": 7 if inferred and identifier == "projection" else None,
            **changes,
        }))
    manifest = HostMaterialManifest(registry_id="fixture-registry", grants=tuple(grants))
    draft = ExperienceDraft(projection_material_id="projection",
                            history_material_ids=() if without_history else ("history",))
    return Inputs(source, store_root, manifest, draft)


def inventory(root: Path) -> dict[str, tuple[bytes, int]]:
    return {path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


def admitted(value: Inputs):
    store = KnowledgeStore(value.store_root)
    entry = store.admit_entry(value.draft, value.registry())
    snapshot = store.freeze_snapshot((entry.entry_id,), PermissionScope(split="confirmation"))
    return store, entry, snapshot


def prospective_snapshot(entries: tuple[ExperienceEntry, ...]) -> KnowledgeSnapshot:
    """A canonical host manifest, used to measure/bypass publication in tests."""
    references = tuple(SnapshotRef(entry_id=item.entry_id, content_sha256=item.content_sha256,
                                  projection_sha256=content_hash(item.contents()["projection"]))
                       for item in sorted(entries, key=lambda item: item.entry_id))
    payload = SnapshotPayload(entries=references, scope=PermissionScope(split="confirmation"),
                              retriever=RetrieverSpec())
    return KnowledgeSnapshot(snapshot_sha256=content_hash(payload), payload_json=encode(payload))


def entry_closure_paths(store_root: Path, entries: tuple[ExperienceEntry, ...]) -> set[Path]:
    paths = {store_root / "entries" / (item.entry_id + ".json") for item in entries}
    for item in entries:
        paths.update(store_root / "materials" / (grant["sha256"] + ".blob")
                     for grant in item.contents()["source_grants"])
    return paths


def two_entries(root: Path, *, shared_materials: bool = False):
    first = make_inputs(root / "first")
    second_projection = SolverProjection(guidance=guidance(), derivation_authority="host_curated",
                                         validation_scopes=(history().validation,)).model_dump(mode="json")
    if not shared_materials:
        second_projection["guidance"]["trigger"] = "A view fails after its offset is supplied."
    second = make_inputs(root / "second", raw_projection=encode(second_projection).encode("utf-8"),
                         projection_changes={"origin_split": "development"} if shared_materials else None)
    store = KnowledgeStore(first.store_root)
    one = store.admit_entry(first.draft, first.registry())
    two = store.admit_entry(second.draft, second.registry())
    assert one.entry_id != two.entry_id
    return first, second, store, (one, two)


def test_constructors_are_readonly_and_host_source_files_stay_untouched(tmp_path: Path) -> None:
    value = make_inputs(tmp_path)
    before = inventory(tmp_path)
    store = KnowledgeStore(value.store_root)
    registry = value.registry()
    assert inventory(tmp_path) == before and list(value.store_root.iterdir()) == []
    raw = (value.source / value.manifest.grants[0].path).read_bytes()
    grant, captured = registry.material("projection")
    assert grant == value.manifest.grants[0] and captured == raw
    entry = store.admit_entry(value.draft, registry)
    assert entry.contents()["application_qualification"] == "none"
    assert inventory(value.source) == {key.removeprefix("source/"): item
                                       for key, item in before.items() if key.startswith("source/")}


def test_readback_is_closed_readonly_and_repeated_admission_is_idempotent(tmp_path: Path) -> None:
    value = make_inputs(tmp_path)
    store, entry, snapshot = admitted(value)
    before = inventory(value.store_root)
    assert store.read_entry(entry.entry_id) == entry
    assert store.read_snapshot(snapshot.snapshot_sha256) == snapshot
    projection = store.read_projection(snapshot.snapshot_sha256, entry.entry_id)
    assert projection.guidance == guidance()
    assert projection.epistemic_status == "historical_guidance_not_current_evidence"
    assert store.admit_entry(value.draft, value.registry()) == entry
    assert store.freeze_snapshot((entry.entry_id,), PermissionScope(split="confirmation")) == snapshot
    reopened = KnowledgeStore(value.store_root)
    assert reopened.read_projection(snapshot.snapshot_sha256, entry.entry_id) == projection
    assert inventory(value.store_root) == before


def test_raw_projection_whitespace_is_preserved_separately_from_semantic_identity(tmp_path: Path) -> None:
    original = SolverProjection(guidance=guidance(), derivation_authority="host_curated",
                                validation_scopes=(history().validation,))
    raw = json.dumps(original.model_dump(mode="json"), indent=2, ensure_ascii=False).encode("utf-8")
    value = make_inputs(tmp_path, raw_projection=raw)
    store, entry, snapshot = admitted(value)
    source_grant = entry.contents()["source_grants"][0]
    assert source_grant["sha256"] == digest(raw) != content_hash(original)
    assert (value.store_root / "materials" / (digest(raw) + ".blob")).read_bytes() == raw
    assert store.read_projection(snapshot.snapshot_sha256, entry.entry_id) == original


def test_curated_guidance_without_execution_history_stays_explicitly_unvalidated(tmp_path: Path) -> None:
    value = make_inputs(tmp_path, without_history=True)
    store, entry, snapshot = admitted(value)
    payload = entry.contents()
    assert payload["history"] == [] and payload["application_qualification"] == "none"
    projection = store.read_projection(snapshot.snapshot_sha256, entry.entry_id)
    assert projection.validation_scopes == ()
    assert projection.derivation_authority == "host_curated"
    assert projection.epistemic_status == "historical_guidance_not_current_evidence"


@pytest.mark.parametrize("role", ["hidden_test", "reference_patch", "evaluation_label", "raw_session"])
def test_registry_audits_forbidden_roles_but_store_does_not_admit_them(tmp_path: Path, role: str) -> None:
    value = make_inputs(tmp_path, projection_changes={"role": role})
    registry = value.registry()
    assert registry.material("projection")[0].role == role
    before = inventory(value.store_root)
    with pytest.raises(KnowledgeError):
        KnowledgeStore(value.store_root).admit_entry(value.draft, registry)
    assert inventory(value.store_root) == before


@pytest.mark.parametrize("source", ["projection", "history"])
@pytest.mark.parametrize("changes", [
    {"visibility": "restricted"}, {"allowed_uses": ("solve",)},
    {"origin_split": "candidate_validation"}, {"origin_split": "confirmation"},
])
def test_every_source_must_be_eligible_for_learning(tmp_path: Path, source: str, changes: dict) -> None:
    arguments = {source + "_changes": changes}
    value = make_inputs(tmp_path, **arguments)
    registry = value.registry()
    with pytest.raises(KnowledgeError):
        KnowledgeStore(value.store_root).admit_entry(value.draft, registry)
    assert list(value.store_root.iterdir()) == []


def test_projection_requires_solver_approval_while_history_can_remain_audit_only(tmp_path: Path) -> None:
    blocked = make_inputs(tmp_path / "blocked", projection_changes={"visibility": "audit_only"})
    with pytest.raises(KnowledgeError):
        KnowledgeStore(blocked.store_root).admit_entry(blocked.draft, blocked.registry())
    allowed = make_inputs(tmp_path / "allowed", history_changes={"visibility": "audit_only"})
    store, entry, snapshot = admitted(allowed)
    assert entry.contents()["source_grants"][1]["visibility"] == "audit_only"
    projection = store.read_projection(snapshot.snapshot_sha256, entry.entry_id)
    assert "fixture-task" not in encode(projection)
    assert "oracle_descriptor" not in encode(projection)


@pytest.mark.parametrize("source", ["projection", "history"])
def test_solve_use_is_an_intersection_across_all_sources(tmp_path: Path, source: str) -> None:
    value = make_inputs(tmp_path, **{source + "_changes": {"allowed_uses": ("learn",)}})
    store = KnowledgeStore(value.store_root)
    entry = store.admit_entry(value.draft, value.registry())
    before = inventory(value.store_root)
    with pytest.raises(KnowledgeError):
        store.freeze_snapshot((entry.entry_id,), PermissionScope(split="confirmation"))
    assert inventory(value.store_root) == before


@pytest.mark.parametrize("source", ["projection", "history"])
def test_target_split_uses_all_grants_instead_of_union(tmp_path: Path, source: str) -> None:
    value = make_inputs(tmp_path, **{source + "_changes": {"allowed_splits": ("development",)}})
    store = KnowledgeStore(value.store_root)
    entry = store.admit_entry(value.draft, value.registry())
    with pytest.raises(KnowledgeError):
        store.freeze_snapshot((entry.entry_id,), PermissionScope(split="confirmation"))
    allowed = store.freeze_snapshot((entry.entry_id,), PermissionScope(split="development"))
    assert store.read_projection(allowed.snapshot_sha256, entry.entry_id).guidance == guidance()


def test_excluded_history_lineage_cannot_be_read_via_another_projection(tmp_path: Path) -> None:
    value = make_inputs(tmp_path)
    store = KnowledgeStore(value.store_root)
    entry = store.admit_entry(value.draft, value.registry())
    scope = PermissionScope(split="confirmation", excluded_lineage_groups=("fixture-family",))
    before = inventory(value.store_root)
    with pytest.raises(KnowledgeError):
        store.freeze_snapshot((entry.entry_id,), scope)
    assert inventory(value.store_root) == before
    different = PermissionScope(split="confirmation", excluded_lineage_groups=("other-family",))
    snapshot = store.freeze_snapshot((entry.entry_id,), different)
    assert store.read_snapshot(snapshot.snapshot_sha256).contents()["scope"]["excluded_lineage_groups"] == [
        "other-family"]


@pytest.mark.parametrize("field", ["reference_patch", "hidden_test", "current_fact", "task_id"])
def test_host_projection_cannot_carry_disallowed_extra_fields(tmp_path: Path, field: str) -> None:
    projection = SolverProjection(guidance=guidance(), derivation_authority="host_curated",
                                  validation_scopes=(history().validation,)).model_dump(mode="json")
    projection[field] = "unapproved answer or case identity"
    value = make_inputs(tmp_path, raw_projection=encode(projection).encode("utf-8"))
    with pytest.raises(KnowledgeError):
        KnowledgeStore(value.store_root).admit_entry(value.draft, value.registry())
    assert list(value.store_root.iterdir()) == []


@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_json_keys_are_not_hidden_by_projection_parser(tmp_path: Path, nested: bool) -> None:
    projection = SolverProjection(guidance=guidance(), derivation_authority="host_curated",
                                  validation_scopes=(history().validation,))
    raw = encode(projection)
    if nested:
        raw = raw.replace('"trigger":', '"trigger":"unapproved trigger","trigger":', 1)
    else:
        raw = '{"derivation_authority":"model_inference",' + raw[1:]
    value = make_inputs(tmp_path, raw_projection=raw.encode("utf-8"))
    with pytest.raises(KnowledgeError):
        KnowledgeStore(value.store_root).admit_entry(value.draft, value.registry())


@pytest.mark.parametrize("alteration", ["authority", "scope", "no_history", "wrong_history_role"])
def test_admission_cross_checks_authority_and_validation_against_original_materials(
        tmp_path: Path, alteration: str) -> None:
    projection = SolverProjection(guidance=guidance(), derivation_authority="model_inference",
                                  validation_scopes=(history().validation,)).model_dump(mode="json")
    options: dict = {"inferred": True}
    if alteration == "authority":
        projection["derivation_authority"] = "host_curated"
    elif alteration == "scope":
        projection["validation_scopes"][0]["result"] = "failed"
    elif alteration == "no_history":
        options["without_history"] = True
    else:
        options["history_changes"] = {"role": "raw_session"}
    options["raw_projection"] = encode(projection).encode("utf-8")
    value = make_inputs(tmp_path, **options)
    with pytest.raises(KnowledgeError):
        KnowledgeStore(value.store_root).admit_entry(value.draft, value.registry())


@pytest.mark.parametrize("result", ["unknown", "failed"])
@pytest.mark.parametrize("known_tokens", [None, 0, 19])
def test_unknown_or_failed_history_and_host_cost_are_preserved_without_promotion(
        tmp_path: Path, result: str, known_tokens: int | None) -> None:
    value = make_inputs(tmp_path, result=result, inferred=True,
                        projection_changes={"known_tokens": known_tokens})
    store, entry, snapshot = admitted(value)
    payload = entry.contents()
    assert payload["known_tokens"] == known_tokens
    assert payload["cost_authority"] == ("unknown" if known_tokens is None else "host_declaration")
    assert payload["extraction_call_id"] == "extraction-call"
    assert payload["application_qualification"] == "none"
    projected = store.read_projection(snapshot.snapshot_sha256, entry.entry_id)
    assert projected.derivation_authority == "model_inference"
    assert projected.validation_scopes[0].result == result
    assert projected.validation_scopes[0].level == "synthetic_fixture"


def test_registry_capture_and_materialized_snapshot_do_not_follow_changed_original_files(tmp_path: Path) -> None:
    value = make_inputs(tmp_path)
    registry = value.registry()
    original_grant, original_raw = registry.material("projection")
    source_file = value.source / original_grant.path
    altered = json.loads(original_raw)
    altered["guidance"]["counterexamples"][0] += " Check alignment at its caller."
    changed_raw = encode(altered).encode("utf-8")
    source_file.write_bytes(changed_raw)
    assert registry.material("projection") == (original_grant, original_raw)
    store = KnowledgeStore(value.store_root)
    old_entry = store.admit_entry(value.draft, registry)
    old_snapshot = store.freeze_snapshot((old_entry.entry_id,), PermissionScope(split="confirmation"))
    old_closure = inventory(value.store_root)
    new_grant = original_grant.model_copy(update={"sha256": digest(changed_raw), "bytes": len(changed_raw)})
    new_manifest = HostMaterialManifest(registry_id="changed-registry",
                                        grants=(new_grant, value.manifest.grants[1]))
    new_registry = HostMaterialRegistry(value.source, new_manifest)
    new_entry = store.admit_entry(value.draft, new_registry)
    new_snapshot = store.freeze_snapshot((new_entry.entry_id,), PermissionScope(split="confirmation"))
    assert new_entry.entry_id != old_entry.entry_id
    assert new_snapshot.snapshot_sha256 != old_snapshot.snapshot_sha256
    assert store.read_projection(old_snapshot.snapshot_sha256, old_entry.entry_id).guidance == guidance()
    assert store.read_projection(new_snapshot.snapshot_sha256, new_entry.entry_id).guidance != guidance()
    for path, original in old_closure.items():
        assert inventory(value.store_root)[path] == original
    source_file.unlink()
    (value.source / value.manifest.grants[1].path).unlink()
    assert KnowledgeStore(value.store_root).read_snapshot(old_snapshot.snapshot_sha256) == old_snapshot


@pytest.mark.parametrize("field", ["premises", "counterexamples"])
@pytest.mark.parametrize("alteration", ["change", "remove"])
def test_original_grant_hash_detects_premise_or_counterexample_edits(
        tmp_path: Path, field: str, alteration: str) -> None:
    value = make_inputs(tmp_path)
    target = value.source / value.manifest.grants[0].path
    raw = json.loads(target.read_bytes())
    if alteration == "remove":
        raw["guidance"][field] = []
    elif field == "premises":
        raw["guidance"][field][0]["unknown_question"] = "Another question."
    else:
        raw["guidance"][field][0] = "Another counterexample."
    target.write_bytes(encode(raw).encode("utf-8"))
    with pytest.raises(KnowledgeError):
        value.registry()
    assert list(value.store_root.iterdir()) == []


def test_extra_registry_grants_do_not_rewrite_existing_entry_identity(tmp_path: Path) -> None:
    value = make_inputs(tmp_path)
    store, entry, snapshot = admitted(value)
    extra_raw = b"audit-only synthetic hidden material"
    relative = "approved/unselected.json"
    (value.source / relative).write_bytes(extra_raw)
    extra = MaterialGrant(material_id="unselected", path=relative, sha256=digest(extra_raw), bytes=len(extra_raw),
                          role="hidden_test", origin_split="confirmation", visibility="restricted",
                          allowed_uses=(), allowed_splits=())
    expanded = HostMaterialManifest(registry_id="expanded-registry", grants=(*value.manifest.grants, extra))
    registry = HostMaterialRegistry(value.source, expanded)
    assert registry.material("unselected")[1] == extra_raw
    assert store.admit_entry(value.draft, registry) == entry
    assert store.freeze_snapshot((entry.entry_id,), PermissionScope(split="confirmation")) == snapshot
    assert not (value.store_root / "materials" / (digest(extra_raw) + ".blob")).exists()


def test_frozen_snapshots_bind_membership_order_permissions_and_retriever(tmp_path: Path) -> None:
    first = make_inputs(tmp_path / "first")
    second_projection = SolverProjection(guidance=guidance(), derivation_authority="host_curated",
                                         validation_scopes=(history().validation,)).model_dump(mode="json")
    second_projection["guidance"]["trigger"] = "A view fails after its offset is supplied."
    second = make_inputs(tmp_path / "second", raw_projection=encode(second_projection).encode("utf-8"))
    store = KnowledgeStore(first.store_root)
    one = store.admit_entry(first.draft, first.registry())
    two = store.admit_entry(second.draft, second.registry())
    scope = PermissionScope(split="confirmation")
    initial = store.freeze_snapshot((one.entry_id,), scope)
    frozen_before = inventory(first.store_root)
    larger = store.freeze_snapshot((two.entry_id, one.entry_id), scope)
    assert [item["entry_id"] for item in larger.contents()["entries"]] == sorted([one.entry_id, two.entry_id])
    assert store.freeze_snapshot((one.entry_id, two.entry_id), scope) == larger
    assert initial.snapshot_sha256 != larger.snapshot_sha256
    assert store.read_snapshot(initial.snapshot_sha256) == initial
    for path, original in frozen_before.items():
        assert inventory(first.store_root)[path] == original
    with pytest.raises(KnowledgeError):
        store.read_projection(initial.snapshot_sha256, two.entry_id)
    with pytest.raises(KnowledgeError):
        store.freeze_snapshot((one.entry_id, one.entry_id), scope)
    changed_retriever = store.freeze_snapshot((one.entry_id,), scope, RetrieverSpec(tag_weight=3))
    changed_scope = store.freeze_snapshot((one.entry_id,), PermissionScope(split="development"))
    assert len({initial.snapshot_sha256, changed_retriever.snapshot_sha256, changed_scope.snapshot_sha256}) == 3
    assert initial.contents()["skill_files"] == []


def test_returned_content_mutation_cannot_change_frozen_store(tmp_path: Path) -> None:
    value = make_inputs(tmp_path)
    store, entry, snapshot = admitted(value)
    before = inventory(value.store_root)
    entry_contents = store.read_entry(entry.entry_id).contents()
    entry_contents["projection"]["guidance"]["premises"].clear()
    entry_contents["source_grants"][0]["allowed_splits"].clear()
    snapshot_contents = store.read_snapshot(snapshot.snapshot_sha256).contents()
    snapshot_contents["entries"].clear()
    snapshot_contents["retriever"]["tag_weight"] = 100
    projected = store.read_projection(snapshot.snapshot_sha256, entry.entry_id).model_dump(mode="json")
    projected["guidance"]["counterexamples"].clear()
    assert store.read_entry(entry.entry_id) == entry
    assert store.read_snapshot(snapshot.snapshot_sha256) == snapshot
    assert store.read_projection(snapshot.snapshot_sha256, entry.entry_id).guidance == guidance()
    assert inventory(value.store_root) == before


def tamper_same_size(target: Path, old: bytes, new: bytes) -> None:
    before = target.stat()
    original = target.read_bytes()
    changed = original.replace(old, new, 1)
    assert changed != original and len(changed) == len(original)
    target.write_bytes(changed)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert target.stat().st_size == before.st_size and target.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize("location", ["material", "entry", "snapshot"])
@pytest.mark.parametrize("mutation", ["change", "missing"])
def test_each_independent_read_rechecks_entire_frozen_closure_without_repair(
        tmp_path: Path, location: str, mutation: str) -> None:
    value = make_inputs(tmp_path)
    store, entry, snapshot = admitted(value)
    assert store.read_projection(snapshot.snapshot_sha256, entry.entry_id).guidance == guidance()
    target = {"material": value.store_root / "materials" / (value.manifest.grants[0].sha256 + ".blob"),
              "entry": value.store_root / "entries" / (entry.entry_id + ".json"),
              "snapshot": value.store_root / "snapshots" / (snapshot.snapshot_sha256 + ".json")}[location]
    if mutation == "missing":
        target.unlink()
    elif location == "snapshot":
        tamper_same_size(target, b"tags_text_v1", b"tags_text_v0")
    else:
        tamper_same_size(target, b"typed", b"bogus")
    corrupted = inventory(value.store_root)
    with pytest.raises(KnowledgeError):
        store.read_projection(snapshot.snapshot_sha256, entry.entry_id)
    with pytest.raises(KnowledgeError):
        KnowledgeStore(value.store_root).read_snapshot(snapshot.snapshot_sha256)
    if location != "snapshot":
        with pytest.raises(KnowledgeError):
            store.read_entry(entry.entry_id)
    assert inventory(value.store_root) == corrupted


@pytest.mark.parametrize("source", ["projection", "history"])
def test_registry_rechecks_actual_bytes_on_every_new_construction(tmp_path: Path, source: str) -> None:
    value = make_inputs(tmp_path)
    registry = value.registry()
    target = value.source / ("approved/" + source + ".json")
    if source == "projection":
        tamper_same_size(target, b"typed", b"bogus")
    else:
        tamper_same_size(target, b"fixture-task", b"changed-task")
    with pytest.raises(KnowledgeError):
        value.registry()
    assert registry.material(source)[1] != target.read_bytes()


def test_registry_rejects_wrong_byte_declaration_even_when_actual_sha_is_correct(tmp_path: Path) -> None:
    value = make_inputs(tmp_path)
    original = value.manifest.grants[0]
    changed = original.model_copy(update={"bytes": original.bytes - 1})
    manifest = HostMaterialManifest(registry_id="wrong-length", grants=(changed, value.manifest.grants[1]))
    assert digest((value.source / original.path).read_bytes()) == changed.sha256
    with pytest.raises(KnowledgeError):
        HostMaterialRegistry(value.source, manifest)
    assert list(value.store_root.iterdir()) == []


def test_registry_rejects_file_descriptor_identity_change_during_read(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = make_inputs(tmp_path)
    original_fstat = os.fstat
    observations = 0

    def changed_fstat(fd: int):
        nonlocal observations
        observations += 1
        info = original_fstat(fd)
        if observations == 2:
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1,
                                   st_mode=info.st_mode, st_nlink=info.st_nlink,
                                   st_size=info.st_size, st_mtime_ns=info.st_mtime_ns,
                                   st_ctime_ns=info.st_ctime_ns,
                                   st_file_attributes=getattr(info, "st_file_attributes", 0))
        return info

    with monkeypatch.context() as patched:
        patched.setattr(os, "fstat", changed_fstat)
        with pytest.raises(KnowledgeError):
            value.registry()
    assert observations >= 2 and list(value.store_root.iterdir()) == []


def test_registry_rejects_real_source_growth_while_the_file_is_open(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = make_inputs(tmp_path)
    target = value.source / value.manifest.grants[0].path
    original_open = Path.open
    changed = False

    class GrowingReader:
        def __init__(self, source):
            self.source = source

        def fileno(self):
            return self.source.fileno()

        def read(self, size):
            nonlocal changed
            raw = self.source.read(size)
            with original_open(target, "ab") as output:
                output.write(b" changed after reading")
            changed = True
            return raw

    @contextmanager
    def observed_open(path: Path, mode="r", *args, **kwargs):
        with original_open(path, mode, *args, **kwargs) as source:
            yield GrowingReader(source) if path == target and mode == "rb" else source

    with monkeypatch.context() as patched:
        patched.setattr(Path, "open", observed_open)
        with pytest.raises(KnowledgeError):
            value.registry()
    assert changed and list(value.store_root.iterdir()) == []


def test_same_byte_material_parent_swap_cannot_be_hidden_by_restoring_it_after_close(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = make_inputs(tmp_path)
    store, entry, snapshot = admitted(value)
    directory = value.store_root / "materials"
    target = directory / (value.manifest.grants[0].sha256 + ".blob")
    replacement = tmp_path / "replacement-materials"
    replacement.mkdir()
    for blob in directory.iterdir():
        (replacement / blob.name).write_bytes(blob.read_bytes())
    backup = tmp_path / "original-materials"
    original_open = Path.open
    before = inventory(value.store_root)
    changed = False

    @contextmanager
    def swapped_open(path: Path, *args, **kwargs):
        nonlocal changed
        if path != target:
            with original_open(path, *args, **kwargs) as source:
                yield source
            return
        changed = True
        directory.rename(backup)
        replacement.rename(directory)
        try:
            with original_open(path, *args, **kwargs) as source:
                yield source
        finally:
            directory.rename(replacement)
            backup.rename(directory)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "open", swapped_open)
        with pytest.raises(KnowledgeError):
            store.read_projection(snapshot.snapshot_sha256, entry.entry_id)
    assert changed and inventory(value.store_root) == before


def test_snapshot_rejects_earlier_material_drift_while_later_material_is_read(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = make_inputs(tmp_path)
    store, entry, snapshot = admitted(value)
    projection = value.store_root / "materials" / (value.manifest.grants[0].sha256 + ".blob")
    later = value.store_root / "materials" / (value.manifest.grants[1].sha256 + ".blob")
    original_open = Path.open
    original_bytes = projection.read_bytes()
    changed_bytes = original_bytes.replace(b"typed", b"bogus", 1)
    assert changed_bytes != original_bytes and len(changed_bytes) == len(original_bytes)
    before = projection.stat()
    changed = False

    class MutatingReader:
        def __init__(self, source):
            self.source = source

        def fileno(self):
            return self.source.fileno()

        def read(self, size):
            nonlocal changed
            raw = self.source.read(size)
            if not changed:
                with original_open(projection, "wb") as output:
                    output.write(changed_bytes)
                os.utime(projection, ns=(before.st_atime_ns, before.st_mtime_ns))
                changed = True
            return raw

    @contextmanager
    def observed_open(path: Path, mode="r", *args, **kwargs):
        with original_open(path, mode, *args, **kwargs) as source:
            yield MutatingReader(source) if path == later and mode == "rb" else source

    with monkeypatch.context() as patched:
        patched.setattr(Path, "open", observed_open)
        with pytest.raises(KnowledgeError):
            store.read_projection(snapshot.snapshot_sha256, entry.entry_id)
    assert changed and projection.read_bytes() == changed_bytes


@pytest.mark.parametrize("operation", ["read_entry", "read_snapshot", "read_projection"])
def test_finish_rejects_late_change_to_an_earlier_receipt_after_the_last_material_reread(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    value = make_inputs(tmp_path)
    store, entry, snapshot = admitted(value)
    projection = value.store_root / "materials" / (value.manifest.grants[0].sha256 + ".blob")
    history_material = value.store_root / "materials" / (value.manifest.grants[1].sha256 + ".blob")
    original_read = knowledge._read
    original_bytes = projection.read_bytes()
    changed_bytes = original_bytes.replace(b"typed", b"bogus", 1)
    assert changed_bytes != original_bytes and len(changed_bytes) == len(original_bytes)
    before = projection.stat()
    history_reads = 0
    changed = False

    def observed_read(root: Path, path: Path, **kwargs):
        nonlocal history_reads, changed
        raw = original_read(root, path, **kwargs)
        if path == history_material:
            history_reads += 1
            if history_reads == 2:
                # The second full history read is the closure's finish pass.
                # It has returned valid bytes; now invalidate its earlier
                # projection receipt after that blob was already reread.
                projection.write_bytes(changed_bytes)
                os.utime(projection, ns=(before.st_atime_ns, before.st_mtime_ns))
                changed = True
        return raw

    with monkeypatch.context() as patched:
        patched.setattr(knowledge, "_read", observed_read)
        with pytest.raises(KnowledgeError):
            if operation == "read_entry":
                store.read_entry(entry.entry_id)
            elif operation == "read_snapshot":
                store.read_snapshot(snapshot.snapshot_sha256)
            else:
                store.read_projection(snapshot.snapshot_sha256, entry.entry_id)
    assert changed and history_reads == 2
    assert projection.read_bytes() == changed_bytes
    assert projection.stat().st_size == before.st_size
    assert projection.stat().st_mtime_ns == before.st_mtime_ns


def test_oversize_snapshot_selection_is_rejected_before_any_material_io(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = make_inputs(tmp_path)
    store = KnowledgeStore(value.store_root)
    entry_ids = tuple("experience-" + f"{index:064x}" for index in range(MAX_SNAPSHOT_ENTRIES + 1))
    assert len(set(entry_ids)) == MAX_SNAPSHOT_ENTRIES + 1
    before = inventory(value.store_root)

    def unexpected_read(*_args, **_kwargs):
        pytest.fail("an oversize snapshot selection must be rejected before material I/O")

    with monkeypatch.context() as patched:
        patched.setattr(knowledge, "_read", unexpected_read)
        with pytest.raises(KnowledgeError):
            store.freeze_snapshot(entry_ids, PermissionScope(split="confirmation"))
    assert inventory(value.store_root) == before


def test_entry_closure_capacity_is_checked_before_creating_any_publication_directory(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = make_inputs(tmp_path)
    registry = value.registry()
    reference_store = KnowledgeStore(value.store_root)
    entry = reference_store.admit_entry(value.draft, registry)
    paths = entry_closure_paths(value.store_root, (entry,))
    full_bytes = sum(path.stat().st_size for path in paths)
    assert full_bytes == len(encode(entry).encode("utf-8")) + sum(
        path.stat().st_size for path in paths if path.suffix == ".blob")
    fresh_root = tmp_path / "fresh-store"
    fresh_root.mkdir()
    store = KnowledgeStore(fresh_root)
    with monkeypatch.context() as patched:
        patched.setattr(knowledge, "MAX_CLOSURE_BYTES", full_bytes - 1)
        with pytest.raises(KnowledgeError):
            store.admit_entry(value.draft, registry)
        assert list(fresh_root.iterdir()) == []
        patched.setattr(knowledge, "MAX_CLOSURE_BYTES", full_bytes)
        assert store.admit_entry(value.draft, registry) == entry
        assert store.read_entry(entry.entry_id) == entry


def test_individually_valid_registries_and_entries_cannot_publish_an_oversize_combined_snapshot(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second, store, entries = two_entries(tmp_path)
    singleton_snapshots = tuple(prospective_snapshot((item,)) for item in entries)
    per_entry_closure = tuple(sum(path.stat().st_size for path in entry_closure_paths(first.store_root, (item,)))
                              for item in entries)
    limit = max(size + len(encode(snapshot).encode("utf-8"))
                for size, snapshot in zip(per_entry_closure, singleton_snapshots, strict=True)) + 16
    combined = prospective_snapshot(entries)
    combined_paths = entry_closure_paths(first.store_root, entries)
    assert sum(path.stat().st_size for path in combined_paths) > limit
    assert all(sum(grant.bytes for grant in value.manifest.grants) < limit for value in (first, second))
    original_read = knowledge._read
    seen: set[Path] = set()
    observed_bytes = 0

    def bounded_read(root: Path, path: Path, **kwargs):
        nonlocal observed_bytes
        if path not in seen:
            size = path.lstat().st_size
            if observed_bytes + size > limit:
                pytest.fail("the first over-budget closure path must not reach material _read")
            seen.add(path)
            observed_bytes += size
        return original_read(root, path, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(knowledge, "MAX_CLOSURE_BYTES", limit)
        for entry, expected in zip(entries, singleton_snapshots, strict=True):
            assert store.read_entry(entry.entry_id) == entry
            assert store.freeze_snapshot((entry.entry_id,), PermissionScope(split="confirmation")) == expected
        before = inventory(first.store_root)
        patched.setattr(knowledge, "_read", bounded_read)
        with pytest.raises(KnowledgeError):
            store.freeze_snapshot(tuple(item.entry_id for item in entries), PermissionScope(split="confirmation"))
    assert seen and observed_bytes <= limit
    assert inventory(first.store_root) == before
    assert not (first.store_root / "snapshots" / (combined.snapshot_sha256 + ".json")).exists()


def test_final_snapshot_envelope_counts_toward_capacity_before_publication(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = make_inputs(tmp_path)
    store = KnowledgeStore(value.store_root)
    entry = store.admit_entry(value.draft, value.registry())
    expected = prospective_snapshot((entry,))
    entry_bytes = sum(path.stat().st_size for path in entry_closure_paths(value.store_root, (entry,)))
    snapshot_bytes = len(encode(expected).encode("utf-8"))
    before = inventory(value.store_root)
    with monkeypatch.context() as patched:
        patched.setattr(knowledge, "MAX_CLOSURE_BYTES", entry_bytes + snapshot_bytes - 1)
        assert store.read_entry(entry.entry_id) == entry
        with pytest.raises(KnowledgeError):
            store.freeze_snapshot((entry.entry_id,), PermissionScope(split="confirmation"))
        assert inventory(value.store_root) == before
        assert not (value.store_root / "snapshots").exists()
        patched.setattr(knowledge, "MAX_CLOSURE_BYTES", entry_bytes + snapshot_bytes)
        assert store.freeze_snapshot((entry.entry_id,), PermissionScope(split="confirmation")) == expected
        assert store.read_snapshot(expected.snapshot_sha256) == expected


def test_shared_cas_references_use_one_capacity_slot_while_all_entries_remain_present(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, _, store, entries = two_entries(tmp_path, shared_materials=True)
    expected = prospective_snapshot(entries)
    paths = entry_closure_paths(first.store_root, entries)
    assert len(paths) == 4  # Two entry envelopes and two shared CAS materials.
    unique_bytes = sum(path.stat().st_size for path in paths)
    limit = unique_bytes + len(encode(expected).encode("utf-8"))
    double_counted = sum(sum(path.stat().st_size for path in entry_closure_paths(first.store_root, (item,)))
                         for item in entries) + len(encode(expected).encode("utf-8"))
    assert double_counted > limit
    with monkeypatch.context() as patched:
        patched.setattr(knowledge, "MAX_CLOSURE_BYTES", limit)
        snapshot = store.freeze_snapshot(tuple(item.entry_id for item in entries), PermissionScope(split="confirmation"))
        assert snapshot == expected
        assert store.read_snapshot(snapshot.snapshot_sha256) == expected
        for entry in entries:
            assert store.read_projection(snapshot.snapshot_sha256, entry.entry_id).guidance == guidance()


@pytest.mark.parametrize("operation", ["read_snapshot", "read_projection"])
def test_manual_canonical_snapshot_cannot_bypass_aggregate_closure_capacity_on_readback(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    first, _, store, entries = two_entries(tmp_path)
    snapshot = prospective_snapshot(entries)
    raw = encode(snapshot).encode("utf-8")
    closure_paths = entry_closure_paths(first.store_root, entries)
    full_bytes = sum(path.stat().st_size for path in closure_paths) + len(raw)
    limit = max(sum(path.stat().st_size for path in entry_closure_paths(first.store_root, (item,)))
                for item in entries) + len(raw) + 16
    assert full_bytes > limit
    directory = first.store_root / "snapshots"
    directory.mkdir()
    target = directory / (snapshot.snapshot_sha256 + ".json")
    target.write_bytes(raw)
    before = inventory(first.store_root)
    with monkeypatch.context() as patched:
        patched.setattr(knowledge, "MAX_CLOSURE_BYTES", limit)
        for entry in entries:
            assert store.read_entry(entry.entry_id) == entry
        with pytest.raises(KnowledgeError):
            if operation == "read_snapshot":
                store.read_snapshot(snapshot.snapshot_sha256)
            else:
                store.read_projection(snapshot.snapshot_sha256, entries[0].entry_id)
    assert inventory(first.store_root) == before and target.read_bytes() == raw


@pytest.mark.parametrize("location", ["source", "material", "entry", "snapshot"])
def test_shared_hardlinked_files_are_rejected_without_modifying_their_external_owner(
        tmp_path: Path, location: str) -> None:
    value = make_inputs(tmp_path)
    if location == "source":
        target = value.source / value.manifest.grants[0].path
        store = snapshot = None
    else:
        store, entry, snapshot = admitted(value)
        target = {"material": value.store_root / "materials" / (value.manifest.grants[0].sha256 + ".blob"),
                  "entry": value.store_root / "entries" / (entry.entry_id + ".json"),
                  "snapshot": value.store_root / "snapshots" / (snapshot.snapshot_sha256 + ".json")}[location]
    outside = tmp_path / "external-owner"
    expected = target.read_bytes()
    try:
        os.link(target, outside)
    except OSError:
        pytest.skip("filesystem cannot create a hard link")
    with pytest.raises(KnowledgeError):
        if location == "source":
            value.registry()
        else:
            assert store is not None and snapshot is not None
            store.read_snapshot(snapshot.snapshot_sha256)
    assert outside.read_bytes() == expected and target.read_bytes() == expected


@pytest.mark.parametrize("location", ["above_root", "source_root", "source_parent", "source_leaf",
                                       "store_root", "material_parent", "entry_leaf", "snapshot_leaf"])
def test_full_unresolved_path_chain_rejects_reparse_points_before_reading(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str) -> None:
    value = make_inputs(tmp_path)
    store, entry, snapshot = admitted(value)
    targeted = {"above_root": tmp_path.parent, "source_root": value.source,
                "source_parent": value.source / "approved",
                "source_leaf": value.source / value.manifest.grants[0].path,
                "store_root": value.store_root, "material_parent": value.store_root / "materials",
                "entry_leaf": value.store_root / "entries" / (entry.entry_id + ".json"),
                "snapshot_leaf": value.store_root / "snapshots" / (snapshot.snapshot_sha256 + ".json")}[location]
    original_stat = Path.lstat

    def reparse_stat(path: Path):
        info = original_stat(path)
        if path == targeted:
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino, st_mode=info.st_mode,
                                   st_nlink=info.st_nlink, st_size=info.st_size,
                                   st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return info

    with monkeypatch.context() as patched:
        patched.setattr(Path, "lstat", reparse_stat)
        with pytest.raises(KnowledgeError):
            if location in {"above_root", "source_root", "source_parent", "source_leaf"}:
                value.registry()
            else:
                store.read_snapshot(snapshot.snapshot_sha256)


def test_registry_enforces_aggregate_raw_material_capacity_before_admission(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    raw = b"x" * (MAX_PAYLOAD_BYTES // 2 + 1)
    grants = []
    for identifier in ("first", "second"):
        (source / identifier).write_bytes(raw)
        grants.append(MaterialGrant(material_id=identifier, path=identifier, sha256=digest(raw), bytes=len(raw),
                                    role="hidden_test", origin_split="confirmation", visibility="restricted",
                                    allowed_uses=(), allowed_splits=()))
    manifest = HostMaterialManifest(registry_id="oversize-registry", grants=tuple(grants))
    with pytest.raises(KnowledgeError):
        HostMaterialRegistry(source, manifest)


def test_empty_snapshot_is_explicit_and_unknown_id_does_not_fall_back_to_latest(tmp_path: Path) -> None:
    value = make_inputs(tmp_path)
    store, entry, populated = admitted(value)
    empty = store.freeze_snapshot((), PermissionScope(split="confirmation"))
    assert store.read_snapshot(empty.snapshot_sha256).contents()["entries"] == []
    with pytest.raises(KnowledgeError):
        store.read_projection(empty.snapshot_sha256, entry.entry_id)
    with pytest.raises(KnowledgeError):
        store.read_projection("f" * 64, entry.entry_id)
    with pytest.raises(KnowledgeError):
        store.read_entry("experience-" + "f" * 64)
    assert store.read_snapshot(populated.snapshot_sha256) == populated


def test_readback_rechecks_permissions_even_for_a_well_formed_rehashed_snapshot(tmp_path: Path) -> None:
    value = make_inputs(tmp_path, history_changes={"allowed_splits": ("confirmation",)})
    store, entry, original = admitted(value)
    changed = copy.deepcopy(original.contents())
    changed["scope"]["split"] = "development"
    forged = KnowledgeSnapshot(snapshot_sha256=content_hash(changed), payload_json=encode(changed))
    target = value.store_root / "snapshots" / (forged.snapshot_sha256 + ".json")
    target.write_bytes(encode(forged).encode("utf-8"))
    before = inventory(value.store_root)
    with pytest.raises(KnowledgeError):
        store.read_projection(forged.snapshot_sha256, entry.entry_id)
    assert inventory(value.store_root) == before
    assert store.read_snapshot(original.snapshot_sha256) == original


def test_readback_rechecks_projection_binding_inside_a_well_formed_rehashed_snapshot(tmp_path: Path) -> None:
    value = make_inputs(tmp_path)
    store, entry, original = admitted(value)
    changed = copy.deepcopy(original.contents())
    changed["entries"][0]["projection_sha256"] = "f" * 64
    forged = KnowledgeSnapshot(snapshot_sha256=content_hash(changed), payload_json=encode(changed))
    target = value.store_root / "snapshots" / (forged.snapshot_sha256 + ".json")
    target.write_bytes(encode(forged).encode("utf-8"))
    before = inventory(value.store_root)
    with pytest.raises(KnowledgeError):
        store.read_projection(forged.snapshot_sha256, entry.entry_id)
    assert inventory(value.store_root) == before
    assert store.read_snapshot(original.snapshot_sha256) == original
