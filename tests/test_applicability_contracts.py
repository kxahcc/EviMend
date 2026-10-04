"""Current-task assessments preserve complete guidance and inference authority."""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from dfx_agent.applicability_contracts import (
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_FILES,
    ApplicabilityContext,
    ApplicabilityProposal,
    ApplicabilityReview,
    BoundQuery,
    BoundQueryPayload,
    ContextMaterial,
    ContextPayload,
    EvidenceUse,
    PremiseAssessment,
    PremiseQuestion,
    ReviewPayload,
    TaskKnowledgeBinding,
)
from dfx_agent.knowledge_contracts import (
    MAX_PAYLOAD_BYTES,
    ExperienceContent,
    ExperiencePremise,
    LimitedValidationScope,
    ObservationBranch,
    PermissionScope,
    ProcedureStep,
    SolverProjection,
    content_hash,
    encode,
)
from dfx_agent.working_context import WorkingContextPacket

HASH = "a" * 64
OTHER_HASH = "b" * 64
THIRD_HASH = "c" * 64


def binding(**changes):
    return TaskKnowledgeBinding(**{
        "task_id": "current-task", "run_id": "current-run", "source_snapshot_sha256": HASH,
        "crash_sha256": OTHER_HASH, "snapshot_sha256": THIRD_HASH,
        "scope": PermissionScope(split="development"), **changes,
    })


def bound_payload(**changes):
    return BoundQueryPayload(**{
        "binding_sha256": content_hash(binding()), "retrieval_id": "retrieval-" + HASH,
        "retrieval_sha256": HASH, **changes,
    })


def bound_query(value=None):
    value = bound_payload() if value is None else value
    digest = content_hash(value)
    return BoundQuery(record_id="bound-query-" + digest, content_sha256=digest, payload_json=encode(value))


def packet(**changes):
    content = {"task_id": "current-task", "run_id": "current-run", "source_snapshot_sha256": HASH,
               "sources": [], "evidence": {"observed": {"statement": "The unit remains unknown."}}}
    return WorkingContextPacket(**{
        "canonical_payload_json": encode(content), "payload_sha256": content_hash(content),
        "max_bytes": MAX_CONTEXT_BYTES, **changes,
    })


def material(**changes):
    return ContextMaterial(**{"reference": "task/task.json", "sha256": HASH, "bytes": 10, **changes})


def context_payload(**changes):
    return ContextPayload(**{
        "binding_sha256": content_hash(binding()), "bound_query_id": bound_query().record_id,
        "packet": packet(), "materials": (material(),), **changes,
    })


def context(value=None):
    value = context_payload() if value is None else value
    digest = content_hash(value)
    return ApplicabilityContext(context_id="app-context-" + digest, payload_sha256=digest, payload_json=encode(value))


def evidence(**changes):
    return EvidenceUse(**{"evidence_id": "observed", "scope": "original_analysis", **changes})


def premise(identifier="units", status="unknown", **changes):
    values = {"premise_id": identifier, "status": status, "reason": "Current evidence leaves a semantic question."}
    if status == "supported":
        values["support"] = (evidence(),)
    elif status == "contradicted":
        values["contradictions"] = (evidence(evidence_id="contrary"),)
    return PremiseAssessment(**{**values, **changes})


def projection():
    return SolverProjection(
        guidance=ExperienceContent(
            tags=("buffer",), trigger="A typed view cannot be constructed.",
            premises=(ExperiencePremise(premise_id="units", statement="Element units must agree with the consumer.",
                                       unknown_question="Which consumer establishes the element unit?"),
                      ExperiencePremise(premise_id="offset", statement="The relevant offset must be aligned.",
                                       unknown_question="What offset actually reaches the constructor?")),
            procedure=(ProcedureStep(step_id="inspect", instruction="Inspect the consumer and original allocation.",
                                     branches=(ObservationBranch(observation="The unit remains unknown.",
                                                                 next_action="Read its caller before changing capacity."),)),),
            preserves=("Preserve complete output encoding.",),
            counterexamples=("A misaligned offset may produce a similar failure.",),
            exit_conditions=("Stop copying the procedure when a premise is contradicted.",)),
        derivation_authority="host_curated",
        validation_scopes=(LimitedValidationScope(level="synthetic_fixture", result="passed",
                                                  scope_names=("host-fixture-only",)),))


def proposal(states=("unknown", "supported"), **changes):
    return ApplicabilityProposal(**{
        "context_id": context().context_id, "entry_id": "experience-" + HASH, "content_sha256": HASH,
        "projection_sha256": content_hash(projection()), "task_id": "current-task", "run_id": "current-run",
        "source_snapshot_sha256": HASH, "diagnosis_id": "diagnosis-current", "hypothesis_ids": ("hypothesis-unit",),
        "premises": tuple(premise(identifier, state) for identifier, state in zip(("units", "offset"), states, strict=True)),
        **changes,
    })


def review_payload(states=("unknown", "supported"), **changes):
    proposed = proposal(states)
    view = projection()
    status = "contradicted" if "contradicted" in states else "unknown" if "unknown" in states else "supported"
    return ReviewPayload(**{
        "binding": binding(), "context_id": proposed.context_id, "bound_query_id": bound_query().record_id,
        "retrieval_id": "retrieval-" + HASH, "proposal": proposed, "projection": view, "status": status,
        "next_action": {"supported": "offer_process", "contradicted": "avoid_copy", "unknown": "clarify"}[status],
        "questions": tuple(PremiseQuestion(premise_id=item.premise_id, question=item.unknown_question)
                           for item, state in zip(view.guidance.premises, states, strict=True) if state == "unknown"),
        "obligation_ids": ("preserve-output", "restore-capacity"), **changes,
    })


def review(value=None):
    value = review_payload() if value is None else value
    digest = content_hash(value)
    return ApplicabilityReview(record_id="app-review-" + digest, content_sha256=digest, payload_json=encode(value))


def replace_json(model, **changes):
    value = model.model_dump(mode="json")
    value.update(changes)
    return type(model).model_validate_json(encode(value))


@pytest.mark.parametrize("field,value", [
    ("task_id", "bad owner"), ("run_id", ""), ("run_id", True), ("source_snapshot_sha256", "A" * 64),
    ("crash_sha256", "a" * 63), ("snapshot_sha256", "not-a-hash"), ("scope", None),
    ("model_authorized", True),
])
def test_binding_has_explicit_host_owner_and_scope_without_execution_authority(field, value):
    with pytest.raises(ValidationError):
        binding(**{field: value})


def test_binding_identity_changes_for_each_owner_version_log_and_scope():
    original = content_hash(binding())
    for changes in ({"task_id": "other-task"}, {"run_id": "other-run"}, {"source_snapshot_sha256": OTHER_HASH},
                    {"crash_sha256": HASH}, {"snapshot_sha256": HASH},
                    {"scope": PermissionScope(split="confirmation")}):
        assert content_hash(binding(**changes)) != original


@pytest.mark.parametrize("field,value", [
    ("binding_sha256", "A" * 64), ("retrieval_sha256", ""), ("retrieval_id", "retrieval-" + OTHER_HASH),
    ("retrieval_id", "experience-" + HASH), ("retrieval_id", "retrieval-" + "A" * 64),
    ("retrieval_id", "bound query"), ("current_task_authenticated", True),
])
def test_bound_query_payload_requires_specific_content_addressed_retrieval(field, value):
    with pytest.raises(ValidationError):
        bound_payload(**{field: value})


@pytest.mark.parametrize("reference", [
    "task", "workspace", "foreign/file", "/task/file", "task//file", "task/./file", "task/../file",
    "task\\file", "task/C:/file", "Task/file", "task/file.", "task/folder /file", "task/file ",
    "task/CON.txt", "workspace/aux", "task/LPT1.log", "task/COM¹.log", 'task/bad"name', "task/?.json",
    "task/a*b", "task/a|b", "task/<file>", "task/control\x00file", "task/line\nfile",
], ids=[f"invalid-path-{index}" for index in range(24)])
def test_context_material_rejects_ambiguous_or_cross_root_path_spelling(reference):
    with pytest.raises(ValidationError):
        material(reference=reference)


def test_context_material_accepts_explicit_normalized_nested_and_unicode_references():
    assert material(reference="workspace/diagnoses/diagnosis-current.json").reference.startswith("workspace/")
    assert material(reference="task/源码/页面.ets", bytes=0).bytes == 0
    assert material(reference="task/comfortable.txt", bytes=MAX_PAYLOAD_BYTES).bytes == MAX_PAYLOAD_BYTES


@pytest.mark.parametrize("field,value", [
    ("sha256", "A" * 64), ("bytes", -1), ("bytes", MAX_PAYLOAD_BYTES + 1), ("bytes", True),
    ("bytes", "10"), ("reference", 7), ("reference", "task/" + "x" * 2048), ("authority", "observed"),
], ids=[f"invalid-material-{index}" for index in range(8)])
def test_context_material_is_strict_and_bounded(field, value):
    with pytest.raises(ValidationError):
        material(**{field: value})


@pytest.mark.parametrize("materials", [
    (material(), material()),
    (material(reference="task/A.json"), material(reference="task/a.json")),
    (material(reference="workspace/z.json"), material(reference="task/a.json")),
    (material(bytes=MAX_CONTEXT_BYTES), material(reference="workspace/b.json", bytes=1)),
    tuple(material(reference=f"task/{index:04d}.json", bytes=0) for index in range(MAX_CONTEXT_FILES + 1)),
], ids=["duplicate", "case-collision", "unsorted", "total-capacity", "file-capacity"])
def test_context_materials_share_total_capacity_and_canonical_unique_order(materials):
    with pytest.raises(ValidationError):
        context_payload(materials=materials)


def test_context_material_boundaries_accept_complete_collection_without_truncation():
    items = tuple(material(reference=f"task/{index:04d}.json", bytes=0) for index in range(MAX_CONTEXT_FILES))
    assert len(context_payload(materials=items).materials) == MAX_CONTEXT_FILES
    assert sum(item.bytes for item in context_payload(materials=(material(bytes=MAX_CONTEXT_BYTES),)).materials
               ) == MAX_CONTEXT_BYTES
    assert context_payload(materials=()).materials == ()


@pytest.mark.parametrize("field,value", [
    ("binding_sha256", OTHER_HASH.upper()), ("bound_query_id", "bound-query-" + "a" * 63),
    ("bound_query_id", "retrieval-" + HASH), ("packet", None), ("materials", [material()]),
    ("current_evidence_confirmed", True),
])
def test_context_payload_requires_frozen_packet_binding_and_bound_query(field, value):
    with pytest.raises(ValidationError):
        context_payload(**{field: value})


def test_context_packet_is_revalidated_when_its_existing_class_instance_was_constructed_without_checks():
    invalid = WorkingContextPacket.model_construct(**{**packet().model_dump(), "payload_sha256": HASH})
    with pytest.raises(ValidationError):
        context_payload(packet=invalid)
    invalid = WorkingContextPacket.model_construct(**{**packet().model_dump(), "max_bytes": 0})
    with pytest.raises(ValidationError):
        context_payload(packet=invalid)


@pytest.mark.parametrize("field,value", [
    ("scope", "historical_guidance"), ("scope", True), ("evidence_id", "bad id"),
    ("candidate_id", HASH), ("candidate_snapshot_sha256", OTHER_HASH),
    ("candidate_id", "A" * 64), ("source_authority", "source_fact"),
])
def test_original_evidence_use_has_no_candidate_or_user_supplied_authority(field, value):
    with pytest.raises(ValidationError):
        evidence(**{field: value})


def test_current_candidate_reference_requires_both_lowercase_hashes():
    value = evidence(scope="current_candidate", candidate_id=HASH, candidate_snapshot_sha256=OTHER_HASH)
    assert value.candidate_id == HASH
    for changes in ({"scope": "current_candidate"}, {"scope": "current_candidate", "candidate_id": HASH},
                    {"scope": "current_candidate", "candidate_snapshot_sha256": OTHER_HASH},
                    {"scope": "original_analysis", "candidate_id": HASH, "candidate_snapshot_sha256": OTHER_HASH},
                    {"scope": "current_candidate", "candidate_id": HASH, "candidate_snapshot_sha256": "B" * 64}):
        with pytest.raises(ValidationError):
            evidence(**changes)


def test_unknown_can_hold_unresolved_support_and_conflict_without_becoming_a_current_fact():
    value = premise(support=(evidence(),), contradictions=(evidence(evidence_id="contrary"),))
    assert value.status == "unknown"
    assert premise().support == premise().contradictions == ()
    assert premise(status="contradicted", support=(evidence(),)).support


@pytest.mark.parametrize("changes", [
    {"premise_id": "bad premise"}, {"status": "passed"}, {"status": True}, {"reason": " "},
    {"reason": "x" * 4097}, {"support": [evidence()]}, {"contradictions": [evidence()]},
    {"support": (evidence(), evidence())}, {"contradictions": (evidence(), evidence())},
    {"support": (evidence(),), "contradictions": (evidence(),)},
    {"status": "supported"}, {"status": "supported", "support": (evidence(),),
                              "contradictions": (evidence(evidence_id="contrary"),)},
    {"status": "contradicted"}, {"semantic_authority": "runtime_proven"},
], ids=[f"invalid-premise-{index}" for index in range(14)])
def test_premise_assessment_is_explicit_strict_and_never_counts_same_reference_twice(changes):
    with pytest.raises(ValidationError):
        PremiseAssessment(**{"premise_id": "units", "status": "unknown", "reason": "Current evidence is limited.",
                            **changes})


def test_duplicate_evidence_id_cannot_be_disguised_by_different_scope():
    current = evidence(scope="current_candidate", candidate_id=HASH, candidate_snapshot_sha256=OTHER_HASH)
    with pytest.raises(ValidationError):
        premise(support=(evidence(), current))
    with pytest.raises(ValidationError):
        premise(support=(evidence(),), contradictions=(current,))


@pytest.mark.parametrize("field,value", [
    ("context_id", "app-context-" + "A" * 64), ("entry_id", "experience-" + OTHER_HASH),
    ("content_sha256", "bad-hash"), ("projection_sha256", "A" * 64),
    ("task_id", "bad task"), ("run_id", ""), ("source_snapshot_sha256", "A" * 64),
    ("diagnosis_id", "bad diagnosis"), ("hypothesis_ids", ()),
    ("hypothesis_ids", ("hypothesis-unit", "hypothesis-unit")), ("hypothesis_ids", ["hypothesis-unit"]),
    ("candidate_id", HASH), ("candidate_snapshot_sha256", OTHER_HASH), ("premises", ()),
    ("premises", (premise(), premise())), ("premises", [premise()]),
    ("premises", tuple(premise(f"premise-{index}") for index in range(129))),
    ("application_verified", True),
], ids=[f"invalid-proposal-{index}" for index in range(18)])
def test_proposal_identifies_full_current_focus_and_cannot_forge_authority(field, value):
    with pytest.raises(ValidationError):
        proposal(**{field: value})


def test_proposal_candidate_evidence_must_identify_its_exact_candidate_and_snapshot():
    current = evidence(scope="current_candidate", candidate_id=HASH, candidate_snapshot_sha256=OTHER_HASH)
    assessments = (premise(status="supported", support=(current,)), premise("offset"))
    value = proposal(premises=assessments, candidate_id=HASH, candidate_snapshot_sha256=OTHER_HASH)
    assert value.premises[0].support[0] == current
    for changes in ({}, {"candidate_id": OTHER_HASH, "candidate_snapshot_sha256": OTHER_HASH},
                    {"candidate_id": HASH, "candidate_snapshot_sha256": THIRD_HASH}):
        with pytest.raises(ValidationError):
            proposal(premises=assessments, **changes)


@pytest.mark.parametrize("states,expected,action", [
    (("supported", "supported"), "supported", "offer_process"),
    (("supported", "unknown"), "unknown", "clarify"),
    (("unknown", "unknown"), "unknown", "clarify"),
    (("supported", "contradicted"), "contradicted", "avoid_copy"),
    (("unknown", "contradicted"), "contradicted", "avoid_copy"),
    (("contradicted", "unknown"), "contradicted", "avoid_copy"),
], ids=["fully-supported", "second-unknown", "both-unknown", "second-conflict", "unknown-conflict", "conflict-unknown"])
def test_review_derives_three_states_with_contradiction_priority_and_keeps_unknown_questions(states, expected, action):
    value = review_payload(states)
    assert value.status == expected and value.next_action == action
    assert len(value.questions) == states.count("unknown")
    assert value.semantic_authority == "model_inference"
    assert value.current_evidence_authentication == "registered_frozen_sources"
    assert value.focus_recency == "explicit_host_input_not_authenticated"
    assert value.application_verification == "not_run"


@pytest.mark.parametrize("field,value", [
    ("context_id", "app-context-" + HASH), ("bound_query_id", "retrieval-" + HASH),
    ("retrieval_id", "retrieval-" + "A" * 64), ("status", "supported"), ("next_action", "offer_process"),
    ("semantic_authority", "source_fact"), ("current_evidence_authentication", "semantic_truth"),
    ("focus_recency", "latest_authenticated"), ("application_verification", "passed"),
    ("obligation_ids", ("restore-capacity", "preserve-output")),
    ("obligation_ids", ("preserve-output", "preserve-output")), ("obligation_ids", ("bad obligation",)),
    ("obligation_ids", ["preserve-output"]), ("tool_execution_allowed", True),
], ids=[f"invalid-review-{index}" for index in range(14)])
def test_review_cannot_promote_model_interpretation_or_disagree_with_scope_status_and_action(field, value):
    with pytest.raises(ValidationError):
        review_payload(**{field: value})


@pytest.mark.parametrize("changes", [
    {"task_id": "other-task"}, {"run_id": "other-run"}, {"source_snapshot_sha256": OTHER_HASH},
    {"context_id": "app-context-" + HASH},
], ids=["other-task", "other-run", "other-source", "other-context"])
def test_review_rejects_other_owner_or_context_even_when_proposal_shape_is_valid(changes):
    with pytest.raises(ValidationError):
        review_payload(proposal=proposal(**changes))


def test_review_requires_complete_premise_coverage_and_original_order():
    original = proposal()
    for assessments in (original.premises[:1], tuple(reversed(original.premises)),
                        (premise("different"), original.premises[1])):
        with pytest.raises(ValidationError):
            review_payload(proposal=proposal(premises=assessments))


@pytest.mark.parametrize("questions", [
    (),
    (PremiseQuestion(premise_id="units", question="A shorter replacement question."),),
    (PremiseQuestion(premise_id="offset", question="What offset actually reaches the constructor?"),),
    (PremiseQuestion(premise_id="units", question="Which consumer establishes the element unit?"),) * 2,
], ids=["missing", "rewritten", "wrong-premise", "duplicate"])
def test_review_unknown_questions_are_exact_complete_original_guidance_questions(questions):
    with pytest.raises(ValidationError):
        review_payload(questions=questions)


def test_review_unknown_questions_keep_the_original_order_even_when_both_are_unknown():
    value = review_payload(("unknown", "unknown"))
    with pytest.raises(ValidationError):
        review_payload(("unknown", "unknown"), questions=tuple(reversed(value.questions)))
    with pytest.raises(ValidationError):
        review_payload(("supported", "supported"), questions=value.questions)


@pytest.mark.parametrize("field,value", [("premise_id", "bad id"), ("question", " "),
                                        ("question", True), ("question", "x" * 4097), ("answer", "not a fact")],
                         ids=["id", "blank", "type", "capacity", "extra"])
def test_unknown_question_contract_is_strict_and_bounded(field, value):
    with pytest.raises(ValidationError):
        PremiseQuestion(**{"premise_id": "units", "question": "What is its unit?", field: value})


def test_review_hash_preserves_every_guidance_section_and_limited_historical_validation():
    original = review_payload()
    assert original.projection == projection()
    for section in ("trigger", "tags", "premises", "procedure", "preserves", "counterexamples", "exit_conditions"):
        raw = projection().model_dump(mode="json")
        if section == "trigger":
            raw["guidance"][section] += " Do not assume a fix."
        elif section == "premises":
            raw["guidance"][section][0]["statement"] += " Additional condition."
        elif section == "procedure":
            raw["guidance"][section][0]["branches"][0]["next_action"] += " Preserve this branch."
        else:
            raw["guidance"][section][0] += " Preserve the full content."
        changed = SolverProjection.model_validate_json(encode(raw))
        with pytest.raises(ValidationError):
            review_payload(projection=changed)
    raw = projection().model_dump(mode="json")
    raw["validation_scopes"][0]["result"] = "failed"
    with pytest.raises(ValidationError):
        review_payload(projection=SolverProjection.model_validate_json(encode(raw)))
    assert original.projection.epistemic_status == "historical_guidance_not_current_evidence"


@pytest.mark.parametrize("factory,identity,digest", [
    (bound_query, "record_id", "content_sha256"), (context, "context_id", "payload_sha256"),
    (review, "record_id", "content_sha256"),
], ids=["bound-query", "app-context", "app-review"])
def test_record_content_is_immutable_and_returned_nested_objects_are_copies(factory, identity, digest):
    original = factory()
    changed = original.contents()
    changed.clear()
    assert original.contents()
    nested = original.contents()
    if factory is context:
        nested["materials"][0]["bytes"] = 1
        assert original.contents()["materials"][0]["bytes"] == 10
    if factory is review:
        nested["projection"]["guidance"]["procedure"][0]["branches"].clear()
        nested["proposal"]["premises"][0]["reason"] = "invented"
        assert original.contents()["projection"]["guidance"]["procedure"][0]["branches"]
        assert original.contents()["proposal"]["premises"][0]["reason"] != "invented"
    with pytest.raises(ValidationError, match="frozen"):
        setattr(original, digest, HASH)
    with pytest.raises(ValidationError):
        replace_json(original, **{identity: "wrong-prefix-" + getattr(original, digest)})
    with pytest.raises(ValidationError):
        replace_json(original, **{digest: OTHER_HASH})


@pytest.mark.parametrize("factory", [bound_query, context, review], ids=["bound-query", "app-context", "app-review"])
def test_records_reject_noncanonical_duplicate_nonfinite_and_nonobject_content(factory):
    original = factory()
    for text in (json.dumps(original.contents()), original.payload_json + "\n", "[]", '{"x":NaN}',
                 '{"x":1,"x":1}', "not-json"):
        with pytest.raises(ValidationError):
            replace_json(original, payload_json=text)
    assert type(original).model_validate_json(encode(original)) == original


def test_record_rechecks_nested_model_even_if_attacker_recomputes_whole_content_identity():
    for original, identity, digest, prefix in (
        (bound_query(), "record_id", "content_sha256", "bound-query-"),
        (context(), "context_id", "payload_sha256", "app-context-"),
        (review(), "record_id", "content_sha256", "app-review-"),
    ):
        payload = copy.deepcopy(original.contents())
        if isinstance(original, BoundQuery):
            payload["retrieval_id"] = "retrieval-" + OTHER_HASH
        elif isinstance(original, ApplicabilityContext):
            payload["materials"][0]["reference"] = "task/../outside"
        else:
            payload["semantic_authority"] = "source_fact"
        changed_hash = content_hash(payload)
        with pytest.raises(ValidationError):
            replace_json(original, **{identity: prefix + changed_hash, digest: changed_hash,
                                      "payload_json": encode(payload)})


@pytest.mark.parametrize("factory", [
    binding, bound_payload, bound_query, material, context_payload, context, evidence, premise, proposal,
    lambda: PremiseQuestion(premise_id="units", question="Which unit?"), review_payload, review,
], ids=["binding", "bound-payload", "bound-query", "material", "context-payload", "context", "evidence",
        "premise", "proposal", "question", "review-payload", "review"])
def test_all_new_contracts_forbid_unknown_fields_and_freeze_schema_version(factory):
    value = factory()
    with pytest.raises(ValidationError):
        replace_json(value, unreviewed_field="not admitted")
    with pytest.raises(ValidationError):
        replace_json(value, schema_version=2)
    with pytest.raises(ValidationError, match="frozen"):
        value.schema_version = 2
