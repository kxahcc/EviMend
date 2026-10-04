"""Historical guidance identity and limited qualification cannot be rewritten."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest
from pydantic import ValidationError

from dfx_agent.knowledge_contracts import (
    MAX_PAYLOAD_BYTES,
    ExperienceContent,
    ExperienceDraft,
    ExperienceEntry,
    ExperiencePayload,
    ExperiencePremise,
    HistoricalMaterial,
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

HASH = "a" * 64
OTHER_HASH = "b" * 64


def guidance():
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


def limited(result="passed"):
    return LimitedValidationScope(level="synthetic_fixture", result=result, scope_names=("synthetic-boundary",))


def history(validation=None):
    return HistoricalSource(task_id="fixture-task", run_id="fixture-run", lineage_group_id="fixture-family",
                            source_snapshot_sha256=HASH, candidate_id=HASH, candidate_snapshot_sha256=OTHER_HASH,
                            report_sha256=HASH, artifact_sha256=OTHER_HASH, validation=validation or limited(),
                            oracle_descriptor="Host-declared synthetic text predicate.",
                            environment_descriptor="An offline fixture; no application execution.")


def grant(role="curated_projection", **changes):
    raw = {"material_id": "projection", "path": "approved/projection.json", "sha256": HASH, "bytes": 31,
           "role": role, "origin_split": "curated", "visibility": "solver_approved",
           "allowed_uses": ("learn", "solve"), "allowed_splits": ("development", "confirmation"),
           "derivation_call_id": None, "known_tokens": None}
    raw.update(changes)
    return MaterialGrant.model_validate(raw)


def payload(*, inferred=False, with_history=True):
    record = history()
    authority = "model_inference" if inferred else "host_curated"
    projection = SolverProjection(guidance=guidance(), derivation_authority=authority,
                                  validation_scopes=(record.validation,) if with_history else ())
    projection_grant = grant("inference_projection" if inferred else "curated_projection",
                             derivation_call_id="extraction-call" if inferred else None,
                             known_tokens=7 if inferred else None)
    sources = (projection_grant,)
    histories = ()
    if with_history:
        sources += (grant("synthetic_history", material_id="history", path="approved/history.json"),)
        histories = (HistoricalMaterial(material_id="history", record=record),)
    return ExperiencePayload(projection=projection, source_grants=sources, history=histories,
                             extraction_call_id=projection_grant.derivation_call_id,
                             known_tokens=projection_grant.known_tokens,
                             cost_authority="host_declaration" if inferred else "unknown")


def entry(value=None):
    value = value or payload()
    digest = content_hash(value)
    return ExperienceEntry(entry_id="experience-" + digest, content_sha256=digest, payload_json=encode(value))


def snapshot_payload():
    item = entry()
    reference = SnapshotRef(entry_id=item.entry_id, content_sha256=item.content_sha256,
                            projection_sha256=content_hash(payload().projection))
    return SnapshotPayload(entries=(reference,), scope=PermissionScope(split="confirmation"), retriever=RetrieverSpec())


def snapshot(value=None):
    value = value or snapshot_payload()
    return KnowledgeSnapshot(snapshot_sha256=content_hash(value), payload_json=encode(value))


def replace_json(model, **changes):
    value = model.model_dump(mode="json")
    value.update(changes)
    return type(model).model_validate_json(encode(value))


def test_immutable_entry_contents_are_fresh_copies_and_all_sources_remain_audit_only():
    item = entry()
    first = item.contents()
    first["history"][0]["record"]["validation"]["result"] = "failed"
    first["projection"]["guidance"]["tags"].append("unapproved")
    assert item.contents()["history"][0]["record"]["validation"]["result"] == "passed"
    assert item.contents()["projection"]["guidance"]["tags"] == []
    assert item.contents()["application_qualification"] == "none"
    assert item.contents()["projection"]["epistemic_status"] == "historical_guidance_not_current_evidence"
    with pytest.raises(ValidationError, match="frozen"):
        item.content_sha256 = OTHER_HASH


def test_snapshot_contents_are_fresh_copies_and_unknown_tags_are_kept():
    item = snapshot()
    first = item.contents()
    first["retriever"]["unknown_tag_policy"] = "drop"
    first["entries"].clear()
    assert item.contents()["entries"]
    assert item.contents()["retriever"]["unknown_tag_policy"] == "keep"
    assert guidance().tags == ()


@pytest.mark.parametrize("field,value", [
    ("known_tokens", "4"), ("known_tokens", True), ("bytes", "31"), ("bytes", False),
    ("bytes", -1), ("bytes", MAX_PAYLOAD_BYTES + 1), ("allowed_uses", ["learn"]),
    ("allowed_uses", ("learn", "learn")), ("allowed_splits", ("development", "development")),
    ("sha256", "A" * 64), ("sha256", "a" * 63), ("path", "../private.json"),
    ("path", "approved\\private.json"), ("path", "C:/private.json"),
    ("material_id", "bad material"), ("derivation_call_id", " "),
])
def test_material_grant_is_strict_bounded_and_path_normalized(field, value):
    with pytest.raises(ValidationError):
        grant(**{field: value})


def test_registry_can_describe_forbidden_sources_without_admitting_them():
    forbidden = grant("hidden_test", visibility="restricted", allowed_uses=(), allowed_splits=(), bytes=0)
    manifest = HostMaterialManifest(registry_id="host-registry", grants=(forbidden,))
    assert manifest.grants[0].role == "hidden_test"
    assert manifest.grants[0].allowed_uses == ()
    assert HostMaterialManifest(registry_id="empty-registry", grants=()).grants == ()


@pytest.mark.parametrize("known_tokens", [0, 7])
def test_declared_cost_cannot_lose_its_call_attribution_even_when_it_is_zero(known_tokens):
    with pytest.raises(ValidationError):
        grant(known_tokens=known_tokens)
    assert grant(known_tokens=known_tokens, derivation_call_id="extraction-call").known_tokens == known_tokens


@pytest.mark.parametrize("model", [grant(), guidance(), payload(), entry(), snapshot_payload(), snapshot()])
def test_independent_knowledge_formats_have_an_explicit_fixed_schema_identity(model):
    assert model.schema_version == 1
    with pytest.raises(ValidationError):
        replace_json(model, schema_version=2)


@pytest.mark.parametrize("other", [
    {"material_id": "projection", "path": "approved/other.json"},
    {"material_id": "other", "path": "APPROVED/PROJECTION.JSON"},
])
def test_registry_rejects_id_duplicates_and_case_insensitive_path_aliases(other):
    with pytest.raises(ValidationError):
        HostMaterialManifest(registry_id="host-registry", grants=(grant(), grant(**other)))


@pytest.mark.parametrize("changes", [
    {"projection_material_id": " "}, {"history_material_ids": ("history", "history")},
    {"history_material_ids": ("projection",)}, {"authority": "host_curated"},
    {"application_qualification": "passed"}, {"known_tokens": 0},
])
def test_extraction_draft_cannot_self_assign_qualification_or_cost(changes):
    value = {"projection_material_id": "projection", **changes}
    with pytest.raises(ValidationError):
        ExperienceDraft.model_validate(value)


@pytest.mark.parametrize("field,value", [
    ("statement", " \n "), ("unknown_question", ""), ("unknown_question", "x" * 4097),
    ("premise_id", "bad premise"), ("trusted_current_fact", True),
])
def test_premise_requires_a_bounded_unknown_check_and_cannot_become_a_fact(field, value):
    raw = {"premise_id": "premise", "statement": "The encoding remains unknown.",
           "unknown_question": "Read which interface defines it?", field: value}
    with pytest.raises(ValidationError):
        ExperiencePremise.model_validate(raw)


@pytest.mark.parametrize("field,value", [
    ("premises", []), ("procedure", []), ("preserves", []), ("counterexamples", []),
    ("exit_conditions", []), ("tags", ["same", "same"]), ("tags", [" "]),
    ("trigger", " "), ("reference_patch", "hidden answer"),
])
def test_guidance_preserves_the_counterexample_and_unknown_path(field, value):
    with pytest.raises(ValidationError):
        replace_json(guidance(), **{field: value})


def test_duplicate_premise_step_or_observation_cannot_hide_a_branch():
    raw = guidance().model_dump(mode="json")
    for field in ("premises", "procedure"):
        changed = copy.deepcopy(raw)
        changed[field].append(copy.deepcopy(changed[field][0]))
        with pytest.raises(ValidationError):
            ExperienceContent.model_validate_json(encode(changed))
    changed = copy.deepcopy(raw)
    changed["procedure"][0]["branches"].append(copy.deepcopy(changed["procedure"][0]["branches"][0]))
    with pytest.raises(ValidationError):
        ExperienceContent.model_validate_json(encode(changed))


@pytest.mark.parametrize("level,result,scopes", [
    ("none", "passed", ()), ("none", "unknown", ("test",)),
    ("synthetic_fixture", "passed", ()), ("application", "passed", ("target",)),
    ("synthetic_fixture", "unknown", ("test", "test")),
])
def test_validation_cannot_claim_a_missing_or_application_scope(level, result, scopes):
    with pytest.raises(ValidationError):
        LimitedValidationScope(level=level, result=result, scope_names=scopes)


def test_unknown_or_failed_synthetic_history_remains_valid_but_not_application_evidence():
    assert LimitedValidationScope(level="none", result="unknown", scope_names=()).result == "unknown"
    for result in ("unknown", "failed"):
        assert history(limited(result)).validation.result == result
    with pytest.raises(ValidationError):
        replace_json(history(), validation={"level": "none", "result": "unknown", "scope_names": []})


@pytest.mark.parametrize("changes", [
    {"candidate_id": None}, {"candidate_snapshot_sha256": None},
    {"candidate_id": "candidate-name"}, {"lineage_group_id": " "},
    {"oracle_descriptor": " "}, {"environment_descriptor": " "},
    {"actual_application_success": True},
])
def test_history_requires_paired_snapshot_identity_and_explicit_fixture_scope(changes):
    with pytest.raises(ValidationError):
        replace_json(history(), **changes)


@pytest.mark.parametrize("role", ["hidden_test", "reference_patch", "evaluation_label", "raw_session"])
def test_non_projection_oracle_material_cannot_enter_experience_payload(role):
    value = payload(with_history=False).model_dump(mode="json")
    value["source_grants"][0]["role"] = role
    with pytest.raises(ValidationError):
        ExperiencePayload.model_validate_json(encode(value))


@pytest.mark.parametrize("alteration", [
    "authority", "second_projection", "missing_history", "foreign_history", "history_role",
    "scope_result", "scope_order", "call_id", "known_tokens", "cost_authority", "qualification",
])
def test_payload_authority_history_scope_and_cost_must_match_host_declarations(alteration):
    value = payload(inferred=True).model_dump(mode="json")
    if alteration == "authority":
        value["projection"]["derivation_authority"] = "host_curated"
    elif alteration == "second_projection":
        value["source_grants"].append(grant(material_id="extra", path="approved/extra.json").model_dump(mode="json"))
    elif alteration == "missing_history":
        value["history"] = []
    elif alteration == "foreign_history":
        value["history"][0]["material_id"] = "absent"
    elif alteration == "history_role":
        value["source_grants"][1]["role"] = "raw_session"
    elif alteration == "scope_result":
        value["projection"]["validation_scopes"][0]["result"] = "failed"
    elif alteration == "scope_order":
        extra = copy.deepcopy(value["history"][0])
        extra["material_id"] = "other-history"
        extra["record"]["validation"]["result"] = "failed"
        value["history"].append(extra)
        value["source_grants"].append(grant("synthetic_history", material_id="other-history",
                                             path="approved/other-history.json").model_dump(mode="json"))
        value["projection"]["validation_scopes"].insert(0, extra["record"]["validation"])
    elif alteration == "call_id":
        value["extraction_call_id"] = "different-call"
    elif alteration == "known_tokens":
        value["known_tokens"] = 0
    elif alteration == "cost_authority":
        value["cost_authority"] = "unknown"
    else:
        value["application_qualification"] = "eligible"
    with pytest.raises(ValidationError):
        ExperiencePayload.model_validate_json(encode(value))


def test_original_material_hash_is_distinct_from_the_parsed_projection_identity():
    value = payload()
    assert value.source_grants[0].sha256 != content_hash(value.projection)
    assert entry(value).content_sha256 == content_hash(value)
    inferred = payload(inferred=True)
    assert inferred.projection.derivation_authority == "model_inference"
    assert inferred.cost_authority == "host_declaration"


@pytest.mark.parametrize("field", ["task_id", "run_id", "current_fact", "hidden_test", "reference_patch"])
def test_solver_projection_cannot_carry_case_identity_or_relabel_history_as_current_evidence(field):
    raw = payload().projection.model_dump(mode="json")
    raw[field] = "unapproved case material"
    with pytest.raises(ValidationError):
        SolverProjection.model_validate_json(encode(raw))


@pytest.mark.parametrize("field,change", [
    ("projection", "guidance"), ("source_grants", "permission"), ("history", "oracle"),
    ("history", "lineage"), ("source_grants", "path"),
])
def test_entire_payload_participates_in_entry_identity(field, change):
    original = payload().model_dump(mode="json")
    changed = copy.deepcopy(original)
    if change == "guidance":
        changed[field]["guidance"]["counterexamples"][0] += " Inspect offset."
    elif change == "permission":
        changed[field][0]["allowed_splits"] = ["development"]
    elif change == "oracle":
        changed[field][0]["record"]["oracle_descriptor"] += " Scope changed."
    elif change == "lineage":
        changed[field][0]["record"]["lineage_group_id"] = "other-family"
    else:
        changed[field][0]["path"] = "approved/changed.json"
    validated = ExperiencePayload.model_validate_json(encode(changed))
    assert content_hash(validated) != content_hash(original)
    prior = entry()
    with pytest.raises(ValidationError):
        ExperienceEntry(entry_id=prior.entry_id, content_sha256=prior.content_sha256, payload_json=encode(validated))


@pytest.mark.parametrize("kind", ["entry", "snapshot"])
@pytest.mark.parametrize("alteration", [
    "noncanonical", "duplicate_key", "wrong_hash", "omitted_default", "extra_field",
])
def test_envelopes_reject_reencoded_or_structurally_invalid_content(kind, alteration):
    item = entry() if kind == "entry" else snapshot()
    data = item.model_dump(mode="json")
    payload_value = item.contents()
    if alteration == "noncanonical":
        data["payload_json"] = json.dumps(payload_value, indent=2)
    elif alteration == "duplicate_key":
        key = "projection" if kind == "entry" else "entries"
        existing = encode({key: payload_value[key]})[1:-1]
        data["payload_json"] = "{" + existing + "," + encode(payload_value)[1:]
    elif alteration == "wrong_hash":
        data["content_sha256" if kind == "entry" else "snapshot_sha256"] = OTHER_HASH
    elif alteration == "omitted_default":
        payload_value.pop("application_qualification" if kind == "entry" else "skill_files")
        data["payload_json"] = encode(payload_value)
    else:
        payload_value["raw_oracle"] = "unapproved hidden answer"
        data["payload_json"] = encode(payload_value)
    if alteration != "wrong_hash":
        digest = hashlib.sha256(data["payload_json"].encode("utf-8")).hexdigest()
        data["content_sha256" if kind == "entry" else "snapshot_sha256"] = digest
        if kind == "entry":
            data["entry_id"] = "experience-" + digest
    with pytest.raises(ValidationError):
        type(item).model_validate(data)


def test_entry_and_snapshot_reference_require_the_content_addressed_entry_id():
    item = entry()
    with pytest.raises(ValidationError):
        replace_json(item, entry_id="experience-" + OTHER_HASH)
    with pytest.raises(ValidationError):
        SnapshotRef(entry_id="experience-" + OTHER_HASH, content_sha256=item.content_sha256,
                    projection_sha256=HASH)


@pytest.mark.parametrize("field,value", [
    ("algorithm", "vector_service"), ("tag_weight", -1), ("text_weight", True),
    ("max_results", 0), ("max_results", 21), ("max_query_chars", 65_537),
    ("tie_breaker", "latest"), ("unknown_tag_policy", "drop"),
])
def test_retrieval_behavior_is_frozen_and_bounded_before_it_can_be_executed(field, value):
    with pytest.raises(ValidationError):
        replace_json(RetrieverSpec(), **{field: value})


@pytest.mark.parametrize("alteration", ["order", "duplicate", "skills", "use", "lineage"])
def test_snapshot_requires_sorted_unique_entries_and_explicit_solve_permission(alteration):
    value = snapshot_payload().model_dump(mode="json")
    other = {"entry_id": "experience-" + HASH, "content_sha256": HASH, "projection_sha256": OTHER_HASH}
    if alteration == "order":
        value["entries"] = sorted([value["entries"][0], other], key=lambda item: item["entry_id"], reverse=True)
    elif alteration == "duplicate":
        value["entries"].append(copy.deepcopy(value["entries"][0]))
    elif alteration == "skills":
        value["skill_files"] = ["skills/readme.md"]
    elif alteration == "use":
        value["scope"]["use"] = "learn"
    else:
        value["scope"]["excluded_lineage_groups"] = ["family", "family"]
    with pytest.raises(ValidationError):
        SnapshotPayload.model_validate_json(encode(value))


@pytest.mark.parametrize("field", ["retriever", "scope", "entries"])
def test_snapshot_identity_contains_configuration_permissions_and_projection_references(field):
    original = snapshot_payload().model_dump(mode="json")
    changed = copy.deepcopy(original)
    if field == "retriever":
        changed[field]["tag_weight"] = 3
    elif field == "scope":
        changed[field]["excluded_lineage_groups"] = ["fixture-family"]
    else:
        changed[field][0]["projection_sha256"] = OTHER_HASH
    validated = SnapshotPayload.model_validate_json(encode(changed))
    assert content_hash(validated) != content_hash(original)
    with pytest.raises(ValidationError):
        KnowledgeSnapshot(snapshot_sha256=content_hash(original), payload_json=encode(validated))


def test_nested_construct_or_copy_cannot_bypass_revalidation():
    invalid = guidance().model_copy(update={"counterexamples": ()})
    with pytest.raises(ValidationError):
        SolverProjection(guidance=invalid, derivation_authority="host_curated")
    invalid_grant = grant().model_copy(update={"known_tokens": True})
    with pytest.raises(ValidationError):
        HostMaterialManifest(registry_id="host-registry", grants=(invalid_grant,))


def test_canonical_hash_is_object_order_independent_but_preserves_tuple_order():
    assert encode({"b": 2, "a": 1}) == encode({"a": 1, "b": 2})
    assert content_hash({"a": [1, 2]}) != content_hash({"a": [2, 1]})
    assert content_hash({"a": "中文"}) == hashlib.sha256(encode({"a": "中文"}).encode("utf-8")).hexdigest()


@pytest.mark.parametrize("value", [{"a": float("nan")}, {"a": float("inf")}, {"a": "中" * (MAX_PAYLOAD_BYTES // 3)}])
def test_encoding_rejects_nonfinite_and_utf8_overflow_without_truncation(value):
    with pytest.raises(ValueError):
        encode(value)


@pytest.mark.parametrize("kind", ["entry", "snapshot"])
def test_envelope_rejects_nonfinite_value_even_with_a_matching_raw_hash(kind):
    raw = '{"unapproved":NaN}'
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with pytest.raises(ValidationError):
        if kind == "entry":
            ExperienceEntry(entry_id="experience-" + digest, content_sha256=digest, payload_json=raw)
        else:
            KnowledgeSnapshot(snapshot_sha256=digest, payload_json=raw)


def test_envelope_rejects_deep_json_without_accepting_the_raw_matching_hash():
    raw = '{"deep":' + '[' * 2000 + '0' + ']' * 2000 + '}'
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with pytest.raises(ValidationError):
        KnowledgeSnapshot(snapshot_sha256=digest, payload_json=raw)
