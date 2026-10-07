"""Opt-in source accounting; pure contracts are not semantic source approval.

No SDK/transport fixture runs here. The seed export test uses the real immutable
store. Synthetic serialized model bundles test only the pure adapter contract;
the public service integration must separately authenticate actual copied roots.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from test_knowledge_contracts import guidance
from test_skill_validation_integration import selection_task

from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.feedback_contracts import (
    FeedbackCapsule,
    FeedbackFile,
    FeedbackPayload,
    HostFeedbackApproval,
    content_hash,
    encode,
)
from dfx_agent.knowledge_contracts import (
    PermissionScope,
    RetrieverSpec,
    SolverProjection,
)
from dfx_agent.learning import _cost
from dfx_agent.learning_contracts import (
    HostLearningApproval,
    LearningDraft,
    LearningDraftPayload,
    LearningInputText,
    LearningOwner,
    LearningRequest,
    LearningSource,
    LearningSourcePayload,
)
from dfx_agent.model_calls import (
    ModelCallIntentV2,
    ModelCallRecord,
    normalize_message,
    sha256,
)
from dfx_agent.model_provider import PreparedRequest, ProviderSettings
from dfx_agent.policies import PolicyFeatures
from dfx_agent.research_accounting import (
    LearningCallAtom,
    ResearchCostInventory,
    ResearchInventoryContext,
    ResearchLearningSourceCallMember,
    ResearchLearningSourceMembership,
    ResearchPublicationEvent,
    ResearchValidationPlanMembership,
    ResearchValidationPlanSlotMember,
    ResearchValidationSlot,
    ResearchVersionReference,
    ValidationRunCostAtom,
    create_research_cost_inventory,
    inventory_from_authenticated_bundle,
    union_research_cost_inventories,
)
from dfx_agent.skill_evolution import SkillTransitionStore
from dfx_agent.skill_evolution_contracts import (
    HostModelSkillVersionApproval,
    HostSkillVersionApproval,
    HostValidationPlanApprovalV2,
    ModelVersionPayload,
    PlanPayloadV2,
    PlanSpecV2,
    ReceiptPayloadV2,
    SkillValidationPlanV2,
    SkillValidationReceiptV2,
    SkillVersion,
    SkillVersionV2,
    ValidationPairV2,
    VersionPayload,
)
from dfx_agent.skill_transition_contracts import (
    HostPublicationApproval,
    PublicationPayload,
    SkillPublication,
)
from dfx_agent.skill_validation import SkillValidationStore
from dfx_agent.task import CaseSelection, verify_task
from dfx_agent.version_skill_contracts import TaskVersionSkillBinding, VersionSkillGrant
from dfx_agent.version_skills import (
    PROMPT_SHA256,
    RENDERER,
    SOURCE_NAMESPACE,
    HostDerivedSkillInput,
    prepare_run_version_skills,
    read_run_version_skills,
)
from dfx_agent.workspace import CandidateWorkspace


def _source_id(label: str = "source") -> str:
    return "learning-source-" + sha256(label)


def _reidentify(raw: dict[str, Any]) -> dict[str, Any]:
    unsigned = {key: item for key, item in raw.items() if key not in {"inventory_id", "content_sha256"}}
    raw["content_sha256"] = content_hash(unsigned)
    raw["inventory_id"] = "research-cost-inventory-" + raw["content_sha256"]
    return raw


def _atom(call_id: str = "extract", *, source_id: str | None = None, usage: bool = True,
          owner: str | None = None, binding: str | None = None, uncertain: bool = False,
          repair_of: str | None = None, invalid_text: bool = False, tool_response: bool = False) -> LearningCallAtom:
    source = source_id or _source_id()
    body = encode({"messages": [{"role": "user", "content": "fixture " + call_id}], "tools": [],
                   "model": "fixture-model", "max_completion_tokens": 64})
    prepared = PreparedRequest(body=body, body_sha256=sha256(body), profile_sha256=sha256("fixture profile"))
    intent = ModelCallIntentV2(call_id=call_id, task_id="learning-operation", run_id="learning-run",
        kind="experience_extraction" if repair_of is None else "learning_format_repair", parent_call_id=repair_of,
        provider_id="fixture", model_id="fixture-model", prompt_sha256=sha256(encode({"messages": prepared.messages})),
        tool_schema_sha256=sha256("[]"), parameters_sha256=sha256(encode(prepared.parameters)), tool_names=(),
        learning_owner_sha256=owner or sha256("owner"), source_binding_sha256=binding or sha256("binding"))
    request = LearningRequest(intent=intent, prepared=prepared)
    response = None if uncertain else AIMessage(content="invalid JSON" if invalid_text else encode(guidance().model_dump(mode="json")),
        id="response-" + call_id, usage_metadata={"input_tokens": 12, "output_tokens": 5, "total_tokens": 17} if usage else None,
        response_metadata={"finish_reason": "tool_calls" if tool_response else "stop"},
        tool_calls=[{"name": "forbidden_tool", "args": {}, "id": "forbidden-response"}] if tool_response else [])
    record = ModelCallRecord(intent=intent, state="unknown" if uncertain else "completed",
        observation=None if response is None else normalize_message(intent, response),
        uncertainty_reason="simulated_disconnect" if uncertain else None)
    item = {"request": request.model_dump(mode="json"), "record": record.model_dump(mode="json"),
            "response": None if response is None else response.model_dump(mode="json"),
            "learning_content_status": "not_completed" if response is None else
                "invalid_experience_content" if invalid_text or tool_response else "valid_experience_content"}
    return LearningCallAtom(operation_id=intent.task_id, run_id=intent.run_id, call_id=intent.call_id,
        owner_sha256=intent.learning_owner_sha256, source_binding_sha256=intent.source_binding_sha256,
        source_ids=(source,), call_item_json=encode(item))


def _learning_inventory(*atoms: LearningCallAtom) -> ResearchCostInventory:
    sources = sorted({source for atom in atoms for source in atom.source_ids})
    memberships = []
    for source_id in sources:
        members = sorted((atom for atom in atoms if source_id in atom.source_ids), key=lambda atom: atom.call_id)
        first = members[0]
        capture = {"schema_version": 1, "source_id": source_id, "operation_id": first.operation_id,
            "run_id": first.run_id, "owner_sha256": first.owner_sha256, "source_binding_sha256": first.source_binding_sha256,
            "expected_calls": [ResearchLearningSourceCallMember(call_id=atom.call_id,
                call_item_sha256=sha256(atom.call_item_json)).model_dump(mode="json") for atom in members]}
        memberships.append(ResearchLearningSourceMembership.model_validate_json(encode({**capture,
            "membership_sha256": content_hash(capture)})))
    return create_research_cost_inventory(contexts=(ResearchInventoryContext(task_id="current-task",
        run_id="current-run", purpose="deployment"),), learning_calls=atoms,
        learning_sources=tuple(memberships),
        versions=tuple(ResearchVersionReference(version_id="skill-version-" + sha256(source),
            learning_source_id=source) for source in sources))


def _validation_inventory(receipt: str = "receipt", plan: str = "plan", *, cost: bool = True,
                          complete: bool = True, missing_role: bool = False,
                          outcome: str = "pass") -> ResearchCostInventory:
    receipt_id = "skill-receipt-" + sha256(receipt)
    plan_id = "skill-plan-" + sha256(plan)
    row = {"task_id": "validation-task", "run_id": "validation-run", "record_status": "verified",
        "synthetic_outcome": outcome, "timing": {"repair_ms": 4.5},
        "cost": {"reserved_calls": 1 if complete else 2, "completed_calls": 1 if complete else 2,
                 "known_input_tokens": 5, "known_output_tokens": 3,
                 "known_total_tokens": 8, "total_tokens": 8 if complete else None, "undispatched_model_calls": 0,
                 "usage_complete": complete, "uncertain_calls": 0, "missing_usage_calls": 0 if complete else 1,
                 "invalid_usage_calls": 0} if cost else None}
    atom = ValidationRunCostAtom(task_id=row["task_id"], run_id=row["run_id"], receipt_ids=(receipt_id,), row_json=encode(row))
    slots = [ResearchValidationSlot(plan_id=plan_id, case_id="representative", role="old",
        task_id=atom.task_id, run_id=atom.run_id, manifest_sha256=sha256("old role"), receipt_ids=(receipt_id,))]
    if missing_role:
        slots.append(ResearchValidationSlot(plan_id=plan_id, case_id="representative", role="new",
            task_id=atom.task_id, run_id="validation-other", manifest_sha256=sha256("new role")))
    return create_research_cost_inventory(validation_runs=(atom,), validation_slots=tuple(slots),
        validation_plans=(_validation_plan_membership(plan_id, tuple(slots)),))


def _validation_plan_membership(plan_id: str, slots: tuple[ResearchValidationSlot, ...]) -> ResearchValidationPlanMembership:
    """Synthetic internal capture; it grants no source authenticity or approval."""
    members = tuple(sorted((ResearchValidationPlanSlotMember(case_id=slot.case_id, role=slot.role,
        slot_sha256=content_hash(slot.model_dump(mode="json", exclude={"receipt_ids"}))) for slot in slots),
        key=lambda item: item.key))
    capture = {"schema_version": 1, "plan_id": plan_id,
        "plan_content_sha256": plan_id.removeprefix("skill-plan-"),
        "expected_slots": [member.model_dump(mode="json") for member in members]}
    return ResearchValidationPlanMembership.model_validate_json(encode({**capture,
        "membership_sha256": content_hash(capture)}))


def test_inventory_content_identity_and_roundtrip_recompute_summary() -> None:
    value = _learning_inventory(_atom())
    assert ResearchCostInventory.model_validate_json(value.model_dump_json()) == value
    assert value.inventory_id == "research-cost-inventory-" + content_hash(value.contents())
    changed = value.model_dump(mode="json")
    changed["summary"]["learning"]["known_tokens_lower_bound"] = 999
    unsigned = {key: item for key, item in changed.items() if key not in {"inventory_id", "content_sha256"}}
    changed["content_sha256"] = content_hash(unsigned)
    changed["inventory_id"] = "research-cost-inventory-" + changed["content_sha256"]
    with pytest.raises(ValueError, match="summary differs"):
        ResearchCostInventory.model_validate_json(encode(changed))
    changed = value.model_dump(mode="json")
    changed["contexts"][0]["run_id"] = "different-run"
    with pytest.raises(ValueError, match="content identity"):
        ResearchCostInventory.model_validate_json(encode(changed))


def test_identical_inventory_union_is_idempotent_and_does_not_rebill_sources() -> None:
    value = _learning_inventory(_atom())
    combined = union_research_cost_inventories((value, value))
    assert combined == value
    assert combined.summary.learning.reserved_calls == 1
    assert combined.summary.learning.total_tokens == 17


def test_compatible_source_prefixes_union_calls_and_reference_sources() -> None:
    first_source, second_source = _source_id("prefix-one"), _source_id("prefix-two")
    first = _learning_inventory(_atom(source_id=first_source))
    second = _learning_inventory(_atom(source_id=second_source),
                                 _atom("repair", source_id=second_source, repair_of="extract"))
    combined = union_research_cost_inventories((first, second))
    assert len(combined.learning_calls) == 2
    assert combined.learning_calls[0].source_ids == tuple(sorted((first_source, second_source)))
    assert combined.summary.learning.reserved_calls == 2
    assert combined.summary.learning.known_tokens_lower_bound == combined.summary.learning.total_tokens == 34
    assert union_research_cost_inventories((second, first)) == combined


def test_captured_source_membership_rejects_call_deletion_even_after_rehashing() -> None:
    value = _learning_inventory(_atom(), _atom("repair", repair_of="extract"))
    first = value.learning_calls[0]
    with pytest.raises(ValueError, match="call membership"):
        create_research_cost_inventory(contexts=value.contexts, versions=value.versions,
            learning_sources=value.learning_sources, learning_calls=(first,))
    raw = value.model_dump(mode="json")
    raw["learning_calls"] = raw["learning_calls"][:1]
    # Recompute a plausible one-call cost and the outer identity. The captured
    # complete membership still requires both calls from the original source.
    raw["summary"] = _learning_inventory(first).summary.model_dump(mode="json")
    with pytest.raises(ValueError, match="call membership"):
        ResearchCostInventory.model_validate_json(encode(_reidentify(raw)))


def test_overlapping_prefix_union_keeps_each_sources_complete_membership() -> None:
    first_source, second_source = _source_id("short-prefix"), _source_id("long-prefix")
    short = _learning_inventory(_atom(source_id=first_source))
    long = _learning_inventory(_atom(source_id=second_source),
        _atom("repair", source_id=second_source, repair_of="extract"))
    value = union_research_cost_inventories((short, long))
    assert sorted(len(source.expected_calls) for source in value.learning_sources) == [1, 2]
    raw = value.model_dump(mode="json")
    raw["learning_calls"] = raw["learning_calls"][:1]
    raw["summary"] = _learning_inventory(value.learning_calls[0]).summary.model_dump(mode="json")
    with pytest.raises(ValueError, match="call membership"):
        ResearchCostInventory.model_validate_json(encode(_reidentify(raw)))


def test_membership_hash_and_required_capture_are_rechecked() -> None:
    value = _learning_inventory(_atom(), _atom("repair", repair_of="extract"))
    with pytest.raises(ValueError, match="source coverage"):
        create_research_cost_inventory(versions=value.versions, learning_calls=value.learning_calls)
    raw = value.model_dump(mode="json")
    raw["learning_sources"][0]["expected_calls"] = raw["learning_sources"][0]["expected_calls"][:1]
    with pytest.raises(ValueError, match="membership identity"):
        ResearchCostInventory.model_validate_json(encode(_reidentify(raw)))


@pytest.mark.parametrize("kind", ["valid_text", "invalid_text", "tool_response"])
def test_completed_learning_classification_is_rederived_from_original_response(kind: str) -> None:
    atom = _atom(invalid_text=kind == "invalid_text", tool_response=kind == "tool_response")
    value = _learning_inventory(atom)
    item = json.loads(atom.call_item_json)
    expected = "valid_experience_content" if kind == "valid_text" else "invalid_experience_content"
    assert item["learning_content_status"] == expected
    assert value.summary.learning.total_tokens == 17  # Invalid output is still charged.
    item["learning_content_status"] = "invalid_experience_content" if expected == "valid_experience_content" else "valid_experience_content"
    changed_item = encode(item)
    with pytest.raises(ValueError, match="content classification"):
        LearningCallAtom(**{**atom.model_dump(exclude={"call_item_json"}), "call_item_json": changed_item})
    raw = value.model_dump(mode="json")
    raw["learning_calls"][0]["call_item_json"] = changed_item
    source = raw["learning_sources"][0]
    source["expected_calls"][0]["call_item_sha256"] = sha256(changed_item)
    source["membership_sha256"] = content_hash({key: item for key, item in source.items() if key != "membership_sha256"})
    with pytest.raises(ValueError, match="content classification"):
        ResearchCostInventory.model_validate_json(encode(_reidentify(raw)))


@pytest.mark.parametrize("uncertain", [False, True])
def test_unknown_learning_calls_keep_known_lower_bound(uncertain: bool) -> None:
    value = _learning_inventory(_atom(), _atom("repair", usage=False, uncertain=uncertain, repair_of="extract"))
    assert value.summary.learning.known_tokens_lower_bound == 17
    assert value.summary.learning.total_tokens is None
    assert value.summary.learning.reserved_calls == 2
    assert value.summary.learning.unknown_calls == 1
    assert value.summary.learning.status == "unknown"
    assert value.summary.total.known_tokens_lower_bound == 17
    assert value.summary.total.total_tokens is value.summary.total.elapsed_ms is value.summary.total.deadline_ms is None
    assert value.summary.total.monetary_cost is None


def test_changed_call_item_is_rejected_even_when_usage_costs_match() -> None:
    original = _atom()
    changed_item = copy.deepcopy(json.loads(original.call_item_json))
    prepared = changed_item["request"]["prepared"]
    body = json.loads(prepared["body"])
    body["messages"][0]["content"] = "changed actual request"
    prepared["body"] = encode(body)
    prepared["body_sha256"] = sha256(prepared["body"])
    changed = LearningCallAtom(**{**original.model_dump(exclude={"call_item_json"}), "call_item_json": encode(changed_item)})
    with pytest.raises(ValueError, match="conflicting captured call membership"):
        union_research_cost_inventories((_learning_inventory(original), _learning_inventory(changed)))


def test_same_operation_cannot_change_owner_under_another_call_id() -> None:
    first = _learning_inventory(_atom(source_id=_source_id("first")))
    second = _learning_inventory(_atom("other", source_id=_source_id("other"), owner=sha256("different owner")))
    with pytest.raises(ValueError, match="original owner"):
        union_research_cost_inventories((first, second))


def test_validation_receipts_share_a_run_charge_and_union_references() -> None:
    first = _validation_inventory("receipt-one", "plan-one")
    second = _validation_inventory("receipt-two", "plan-two")
    combined = union_research_cost_inventories((first, second))
    assert len(combined.validation_runs) == 1
    assert len(combined.validation_runs[0].receipt_ids) == 2
    assert combined.summary.validation.reserved_calls == 1
    assert combined.summary.validation.total_tokens == 8
    assert combined.summary.validation.elapsed_ms == 4.5
    assert combined.summary.planned_validation_roles == 2


def test_changed_validation_outcome_cannot_hide_under_identical_cost() -> None:
    with pytest.raises(ValueError, match="conflicting original rows"):
        union_research_cost_inventories((_validation_inventory(), _validation_inventory(outcome="fail")))


@pytest.mark.parametrize("change", ["complete_total_differs", "tokens_without_calls"])
def test_impossible_validation_cost_is_rejected_after_identity_recomputation(change: str) -> None:
    value = _validation_inventory()
    raw = value.model_dump(mode="json")
    row = json.loads(raw["validation_runs"][0]["row_json"])
    if change == "complete_total_differs":
        row["cost"]["total_tokens"] = 9
        raw["summary"]["validation"]["total_tokens"] = 9
        message = "known token sum"
    else:
        row["cost"]["reserved_calls"] = 0
        raw["summary"]["validation"]["known_reserved_calls_lower_bound"] = 0
        raw["summary"]["validation"]["reserved_calls"] = 0
        raw["summary"]["total"]["known_reserved_calls_lower_bound"] = 0
        message = "without reserved calls"
    raw["validation_runs"][0]["row_json"] = encode(row)
    with pytest.raises(ValueError, match=message):
        ResearchCostInventory.model_validate_json(encode(_reidentify(raw)))


def test_explicit_zero_call_zero_token_validation_cost_is_allowed() -> None:
    atom = _validation_inventory().validation_runs[0]
    row = json.loads(atom.row_json)
    row["cost"].update(reserved_calls=0, completed_calls=0, known_input_tokens=0, known_output_tokens=0,
        known_total_tokens=0, total_tokens=0)
    changed = ValidationRunCostAtom(**{**atom.model_dump(exclude={"row_json"}), "row_json": encode(row)})
    assert json.loads(changed.row_json)["cost"]["total_tokens"] == 0


def test_missing_validation_role_preserves_denominator_and_cost_uncertainty() -> None:
    value = _validation_inventory(missing_role=True)
    assert value.summary.planned_validation_roles == 2
    assert value.summary.missing_validation_roles == 1
    assert value.summary.validation.known_reserved_calls_lower_bound == 1
    assert value.summary.validation.reserved_calls is None
    assert value.summary.validation.known_tokens_lower_bound == 8
    assert value.summary.validation.total_tokens is value.summary.validation.elapsed_ms is None
    assert "planned_validation_roles_missing" in value.summary.validation.unknown_reasons


def test_captured_plan_membership_rejects_missing_role_deletion_after_rehashing() -> None:
    value = _validation_inventory(missing_role=True)
    retained = tuple(slot for slot in value.validation_slots if slot.receipt_ids)
    with pytest.raises(ValueError, match="slot membership"):
        create_research_cost_inventory(validation_plans=value.validation_plans,
            validation_runs=value.validation_runs, validation_slots=retained)
    raw = value.model_dump(mode="json")
    raw["validation_slots"] = [slot.model_dump(mode="json") for slot in retained]
    raw["summary"] = _validation_inventory().summary.model_dump(mode="json")
    with pytest.raises(ValueError, match="slot membership"):
        ResearchCostInventory.model_validate_json(encode(_reidentify(raw)))
    # Union revalidates inputs even when a caller bypassed the normal constructor.
    unvalidated = value.model_copy(update={"validation_slots": retained,
        "summary": _validation_inventory().summary})
    digest = content_hash(unvalidated.contents())
    unvalidated = unvalidated.model_copy(update={"content_sha256": digest,
        "inventory_id": "research-cost-inventory-" + digest})
    with pytest.raises(ValueError, match="slot membership"):
        union_research_cost_inventories((unvalidated,))


def test_plan_capture_is_required_and_binds_exact_case_run_and_manifest() -> None:
    value = _validation_inventory(missing_role=True)
    with pytest.raises(ValueError, match="plan coverage"):
        create_research_cost_inventory(validation_runs=value.validation_runs, validation_slots=value.validation_slots)
    for field, replacement in (("case_id", "changed-case"), ("run_id", "changed-run"),
                                ("manifest_sha256", sha256("changed manifest"))):
        raw = value.model_dump(mode="json")
        missing = next(slot for slot in raw["validation_slots"] if not slot["receipt_ids"])
        missing[field] = replacement
        raw["validation_slots"].sort(key=lambda item: (item["plan_id"], item["case_id"], item["role"]))
        with pytest.raises(ValueError, match="slot membership"):
            ResearchCostInventory.model_validate_json(encode(_reidentify(raw)))
    raw = value.model_dump(mode="json")
    raw["validation_plans"][0]["expected_slots"].pop()
    with pytest.raises(ValueError, match="plan membership identity"):
        ResearchCostInventory.model_validate_json(encode(_reidentify(raw)))
    raw = value.model_dump(mode="json")
    plan = raw["validation_plans"][0]
    plan["plan_content_sha256"] = sha256("different plan content")
    plan["membership_sha256"] = content_hash({key: item for key, item in plan.items() if key != "membership_sha256"})
    with pytest.raises(ValueError, match="plan content identity"):
        ResearchCostInventory.model_validate_json(encode(_reidentify(raw)))


def test_union_rejects_replaced_capture_but_can_fill_original_planned_roles() -> None:
    earlier = _validation_inventory(missing_role=True)
    retained = tuple(slot for slot in earlier.validation_slots if slot.receipt_ids)
    # A caller-created smaller capture can be internally consistent; it cannot
    # prove an original plan and conflicts with the already captured full plan.
    smaller = create_research_cost_inventory(validation_runs=earlier.validation_runs,
        validation_slots=retained, validation_plans=(_validation_plan_membership(retained[0].plan_id, retained),))
    with pytest.raises(ValueError, match="conflicting captured slot membership"):
        union_research_cost_inventories((earlier, smaller))
    missing = next(slot for slot in earlier.validation_slots if not slot.receipt_ids)
    receipt_id = "skill-receipt-" + sha256("completed new role")
    row = json.loads(earlier.validation_runs[0].row_json)
    row.update(task_id=missing.task_id, run_id=missing.run_id)
    new_atom = ValidationRunCostAtom(task_id=missing.task_id, run_id=missing.run_id,
        receipt_ids=(receipt_id,), row_json=encode(row))
    filled = ResearchValidationSlot(**{**missing.model_dump(exclude={"receipt_ids"}), "receipt_ids": (receipt_id,)})
    later = create_research_cost_inventory(validation_plans=earlier.validation_plans,
        validation_runs=(*earlier.validation_runs, new_atom), validation_slots=(*retained, filled))
    combined = union_research_cost_inventories((earlier, later))
    assert len(combined.validation_plans) == 1
    assert combined.validation_plans == earlier.validation_plans
    assert combined.summary.planned_validation_roles == 2
    assert combined.summary.missing_validation_roles == 0
    assert combined.summary.validation.total_tokens == 16


def test_same_plan_receipt_reference_union_does_not_change_captured_matrix() -> None:
    first = _validation_inventory("receipt-one")
    second = _validation_inventory("receipt-two")
    combined = union_research_cost_inventories((first, second))
    assert first.validation_plans == second.validation_plans == combined.validation_plans
    assert len(combined.validation_runs) == len(combined.validation_slots) == 1
    assert len(combined.validation_slots[0].receipt_ids) == 2
    assert combined.summary.validation.total_tokens == 8


@pytest.mark.parametrize(("updates", "omitted", "message"), [
    ({"known_input_tokens": 6}, (), "token components"),
    ({"known_input_tokens": 9}, ("known_output_tokens",), "token component exceeds"),
    ({"known_output_tokens": -1}, (), "known_output_tokens"),
    ({"completed_calls": 2}, (), "classification exceeds reserved"),
    ({"completed_calls": 0}, (), "incomplete calls"),
    ({"uncertain_calls": 1}, ("completed_calls",), "unknown calls"),
    ({"undispatched_model_calls": 1}, ("completed_calls",), "unknown calls"),
    ({"usage_complete": False, "total_tokens": 8}, (), "cannot claim a token total"),
    ({"usage_complete": False, "total_tokens": None, "reserved_calls": 2, "completed_calls": 2,
      "uncertain_calls": 1}, (), "state classifications"),
    ({"usage_complete": False, "total_tokens": None, "reserved_calls": 2, "completed_calls": 1,
      "missing_usage_calls": 2, "undispatched_model_calls": 1}, (), "usage classifications exceed completed"),
    ({"usage_complete": False, "total_tokens": None, "reserved_calls": 2, "missing_usage_calls": 1,
      "invalid_usage_calls": 1, "uncertain_calls": 1}, ("completed_calls",), "unknown classifications"),
    ({"usage_complete": False, "total_tokens": None, "reserved_calls": 2,
      "uncertain_calls": 3}, ("completed_calls",), "classification exceeds reserved"),
    ({"usage_complete": False, "total_tokens": None, "reserved_calls": 2, "completed_calls": 1},
     (), "do not cover reserved calls"),
    ({"usage_complete": False, "total_tokens": None, "completed_calls": 0, "uncertain_calls": 1},
     (), "require a completed call with valid usage"),
    ({"usage_complete": False, "total_tokens": None, "missing_usage_calls": 1},
     (), "require a completed call with valid usage"),
])
def test_saved_validation_metrics_counter_conservation_is_checked_after_rehashing(
        updates: dict[str, Any], omitted: tuple[str, ...], message: str) -> None:
    value = _validation_inventory()
    original = value.validation_runs[0]
    row = json.loads(original.row_json)
    row["cost"].update(updates)
    for name in omitted:
        del row["cost"][name]
    with pytest.raises(ValueError, match=message):
        ValidationRunCostAtom(**{**original.model_dump(exclude={"row_json"}), "row_json": encode(row)})
    raw = value.model_dump(mode="json")
    raw["validation_runs"][0]["row_json"] = encode(row)
    with pytest.raises(ValueError, match=message):
        ResearchCostInventory.model_validate_json(encode(_reidentify(raw)))


def test_missing_optional_metrics_counters_are_retained_as_unknown() -> None:
    value = _validation_inventory()
    atom = value.validation_runs[0]
    row = json.loads(atom.row_json)
    for name in ("completed_calls", "undispatched_model_calls", "known_input_tokens", "known_output_tokens"):
        del row["cost"][name]
    partial = ValidationRunCostAtom(**{**atom.model_dump(exclude={"row_json"}), "row_json": encode(row)})
    combined = create_research_cost_inventory(validation_plans=value.validation_plans,
        validation_slots=value.validation_slots, validation_runs=(partial,))
    assert json.loads(combined.validation_runs[0].row_json) == row
    assert combined.summary.validation.known_tokens_lower_bound == 8
    assert combined.summary.validation.total_tokens is None
    assert "validation_call_classification_absent" in combined.summary.validation.unknown_reasons
    assert "validation_token_components_absent" in combined.summary.validation.unknown_reasons


def test_saved_incomplete_state_counts_keep_only_observed_lower_bounds() -> None:
    value = _validation_inventory()
    atom = value.validation_runs[0]
    row = json.loads(atom.row_json)
    row["cost"].update(reserved_calls=3, completed_calls=1, uncertain_calls=1, undispatched_model_calls=1,
        usage_complete=False, total_tokens=None)
    partial = ValidationRunCostAtom(**{**atom.model_dump(exclude={"row_json"}), "row_json": encode(row)})
    combined = create_research_cost_inventory(validation_plans=value.validation_plans,
        validation_slots=value.validation_slots, validation_runs=(partial,))
    assert combined.summary.validation.known_reserved_calls_lower_bound == 3
    assert combined.summary.validation.known_tokens_lower_bound == 8
    assert combined.summary.validation.unknown_calls == 2
    assert combined.summary.validation.total_tokens is None
    assert combined.summary.validation.status == "unknown"


def test_later_complete_inventory_does_not_hide_an_earlier_missing_role() -> None:
    earlier = _validation_inventory("early", "early-plan", missing_role=True)
    later = _validation_inventory("later", "later-plan")
    value = union_research_cost_inventories((earlier, later))
    assert value.summary.planned_validation_roles == 3
    assert value.summary.missing_validation_roles == 1
    assert value.summary.validation.total_tokens is None


@pytest.mark.parametrize("cost", [False, True])
def test_validation_missing_cost_or_usage_is_unknown(cost: bool) -> None:
    value = _validation_inventory(cost=cost, complete=False)
    assert value.summary.validation.total_tokens is None
    assert value.summary.validation.known_tokens_lower_bound == (8 if cost else 0)
    assert value.summary.validation.status == "unknown"


def test_missing_input_inventory_never_becomes_zero_cost() -> None:
    value = _learning_inventory(_atom())
    combined = union_research_cost_inventories((None, value))
    assert combined.unknown_inventory_present
    assert combined.summary.learning.known_tokens_lower_bound == 17
    assert combined.summary.learning.total_tokens is combined.summary.learning.reserved_calls is None
    assert "source_inventory_absent" in combined.summary.total.unknown_reasons
    assert union_research_cost_inventories(()).unknown_inventory_present
    assert union_research_cost_inventories((combined,)).unknown_inventory_present


def test_manual_version_learning_remains_not_run_and_null() -> None:
    value = create_research_cost_inventory(versions=(ResearchVersionReference(version_id="skill-version-" + sha256("seed")),))
    assert value.summary.learning.status == "not_run"
    assert value.summary.learning.reserved_calls == 0
    assert value.summary.learning.total_tokens is value.summary.learning.monetary_cost is None
    assert value.summary.validation.status == value.summary.deployment.status == value.summary.rollback.status == "unknown"


def _seed() -> tuple[SkillVersion, HostSkillVersionApproval]:
    projection = SolverProjection(guidance=guidance(), derivation_authority="host_curated")
    approval = HostSkillVersionApproval(approval_id="seed-approval", skill_id="capacity",
        projection_sha256=content_hash(projection), allowed_splits=("development", "candidate_validation", "confirmation"),
        learning_lineage_groups=("seed-lineage",))
    body = VersionPayload(approval=approval, projection=projection, learning_lineage_groups=("seed-lineage",))
    digest = content_hash(body)
    return SkillVersion(version_id="skill-version-" + digest, content_sha256=digest, payload_json=encode(body)), approval


def _serialized_model_bundle() -> tuple[dict[str, bytes], SkillVersion, HostSkillVersionApproval, SkillVersionV2]:
    """Internally valid serialized objects only; no source approval is asserted."""
    seed, parent_approval = _seed()
    manifest_text = encode({"scope": "offline_synthetic"})
    feedback_approval = HostFeedbackApproval(approval_id="feedback-approval", manifest_sha256=sha256(manifest_text),
        allowed_splits=("development", "candidate_validation", "confirmation"))
    feedback_body = FeedbackPayload(approval=feedback_approval, manifest_json=manifest_text,
        aggregate_json=encode({"scope": "offline_synthetic", "manifest_sha256": sha256(manifest_text)}), directories=(),
        files=(FeedbackFile(path="input/crash.log", sha256=sha256("fixture crash"), bytes=len(b"fixture crash")),))
    digest = content_hash(feedback_body)
    capsule = FeedbackCapsule(capsule_id="feedback-" + digest, content_sha256=digest, payload_json=encode(feedback_body))
    learning_approval = HostLearningApproval(approval_id="learning-approval", operation_id="learning-operation", run_id="learning-run",
        capsule_id=capsule.capsule_id, root_binding_sha256=sha256("synthetic physical root"), feedback_approval=feedback_approval,
        provider_settings=ProviderSettings(provider_id="fixture", model_id="fixture-model", max_output_tokens=64),
        max_calls=4, max_known_tokens=1000)
    owner = LearningOwner(approval=learning_approval, source=capsule, source_binding_sha256=content_hash(capsule),
        provider_profile_json=encode({"fixture": "serialized contract only"}), learning_lineage_groups=("learning-lineage",),
        input_texts=(LearningInputText(path="input/crash.log", sha256=sha256("fixture crash"), bytes=len(b"fixture crash"),
            text="fixture crash", role="original_crash"),))
    atom = _atom(owner=content_hash(owner), binding=owner.source_binding_sha256)
    calls_json = encode({"calls": [json.loads(atom.call_item_json)]})
    cost = _cost([atom.record])
    projection = SolverProjection(guidance=guidance(), derivation_authority="model_inference")
    draft_body = LearningDraftPayload(owner=owner, learning_call_id=atom.call_id, calls_json=calls_json, cost=cost,
        projection=projection, allowed_splits=feedback_approval.allowed_splits, excluded_lineage_groups=(),
        learning_lineage_groups=owner.learning_lineage_groups)
    digest = content_hash(draft_body)
    draft = LearningDraft(draft_id="learning-draft-" + digest, content_sha256=digest, payload_json=encode(draft_body))
    source_body = LearningSourcePayload(approval=learning_approval, owner=owner, draft=draft, operation_cost=cost,
        operation_calls_json=calls_json, directories=(), files=())
    digest = content_hash(source_body)
    source = LearningSource(source_id="learning-source-" + digest, content_sha256=digest, payload_json=encode(source_body))
    version_approval = HostModelSkillVersionApproval(approval_id="review-model", skill_id="capacity",
        projection_sha256=content_hash(projection), parent_version_id=seed.version_id, source_draft_id=draft.draft_id,
        learning_source_id=source.source_id, allowed_splits=feedback_approval.allowed_splits)
    version_body = ModelVersionPayload(approval=version_approval, projection=projection, parent_approval=parent_approval,
        learning_approval=learning_approval, learning_source_id=source.source_id, draft_cost=cost, operation_cost=cost,
        learning_lineage_groups=owner.learning_lineage_groups)
    digest = content_hash(version_body)
    version = SkillVersionV2(version_id="skill-version-" + digest, content_sha256=digest, payload_json=encode(version_body))
    files = {"versions/" + seed.version_id + ".json": encode(seed).encode(),
             "versions/" + version.version_id + ".json": encode(version).encode(),
             "source-learning/" + source.source_id + "/source.json": encode(source).encode()}
    return files, seed, parent_approval, version


def test_pure_adapter_extracts_full_model_call_and_manual_ancestor() -> None:
    files, seed, _approval, child = _serialized_model_bundle()
    before = dict(files)
    value = inventory_from_authenticated_bundle(files, task_id="current-task", run_id="current-run", purpose="deployment")
    assert {version.version_id for version in value.versions} == {seed.version_id, child.version_id}
    assert value.learning_calls[0].record.intent.schema_version == 2
    assert value.summary.learning.total_tokens == value.summary.learning.known_tokens_lower_bound == 17
    assert value.summary.learning.reserved_calls == 1
    assert files == before


def test_top_level_model_cost_is_not_pruned_when_publication_rolls_back_to_seed() -> None:
    # The fixture is a parser-level event history, not runtime publication proof.
    files, seed, approval, child = _serialized_model_bundle()
    initial_approval = HostPublicationApproval(approval_id="initial", alias="capacity", action="initialize_seed", version_id=seed.version_id)
    initial_body = PublicationPayload(approval=initial_approval, version_approval=approval, sequence=1,
        projection_sha256=approval.projection_sha256)
    digest = content_hash(initial_body)
    initial = SkillPublication(publication_id="skill-publication-" + digest, content_sha256=digest, payload_json=encode(initial_body))
    rollback_approval = HostPublicationApproval(approval_id="rollback", alias="capacity", action="rollback", version_id=seed.version_id,
        previous_publication_id=initial.publication_id)
    rollback_body = PublicationPayload(approval=rollback_approval, version_approval=approval, previous_approval=initial_approval,
        sequence=2, projection_sha256=approval.projection_sha256)
    digest = content_hash(rollback_body)
    rollback = SkillPublication(publication_id="skill-publication-" + digest, content_sha256=digest, payload_json=encode(rollback_body))
    files["publications/capacity/0001-" + initial.publication_id + ".json"] = encode(initial).encode()
    files["publications/capacity/0002-" + rollback.publication_id + ".json"] = encode(rollback).encode()
    value = inventory_from_authenticated_bundle(files, task_id="current-task", run_id="current-run", purpose="deployment")
    assert child.version_id in {version.version_id for version in value.versions}
    assert value.summary.learning.total_tokens == 17
    assert value.summary.rollback_event_ids == (rollback.publication_id,)
    assert value.summary.rollback.total_tokens is value.summary.rollback.elapsed_ms is value.summary.deployment.total_tokens is None
    assert ResearchPublicationEvent(publication_id=rollback.publication_id, event_json=encode(rollback)).action == "rollback"


def test_linked_model_source_missing_is_rejected() -> None:
    files, _seed_version, _approval, _child = _serialized_model_bundle()
    files = {key: raw for key, raw in files.items() if not key.startswith("source-learning/")}
    with pytest.raises(KeyError):
        inventory_from_authenticated_bundle(files, task_id="current-task", run_id="current-run", purpose="deployment")


def test_model_source_cannot_acquire_the_manual_schema() -> None:
    files, _seed_version, _approval, _child = _serialized_model_bundle()
    path = next(key for key in files if key.startswith("source-learning/"))
    raw = json.loads(files[path])
    raw["schema_version"] = 1
    files[path] = encode(raw).encode()
    with pytest.raises(ValueError):
        inventory_from_authenticated_bundle(files, task_id="current-task", run_id="current-run", purpose="deployment")


def _declared_manifest(case: str, role: str, seed: SkillVersion, child: SkillVersionV2) -> str:
    """A fully parsed declaration; it supplies no file/source authentication."""
    snapshot, index = sha256("snapshot"), sha256("index")
    knowledge = {"snapshot_sha256": snapshot, "index_sha256": index,
        "scope": PermissionScope(split="candidate_validation").model_dump(mode="json"),
        "retriever": RetrieverSpec().model_dump(mode="json"),
        "files": sorted([{"path": "knowledge/snapshots/" + snapshot + ".json", "sha256": sha256("snapshot bytes"), "bytes": 0},
            {"path": "knowledge/indexes/" + snapshot + ".json", "sha256": index, "bytes": 0},
            {"path": "knowledge/indexes/" + snapshot + ".sqlite", "sha256": sha256("index bytes"), "bytes": 0}], key=lambda item: item["path"])}
    resources = [{"version_id": seed.version_id}, {"version_id": child.version_id}]
    shared = {"schema_version": 1, "source_type": "approved_versions", "renderer": RENDERER,
        "prompt_sha256": PROMPT_SHA256, "resources": resources, "files": resources,
        "source_files": [], "source_directories": [],
        "snapshot_sha256": sha256(encode({"resources": resources, "source_files": [], "source_directories": []}))}
    selection = CaseSelection(task_id=case, case_key="cases/" + case, lineage_group_id="validation-" + case,
        fault_variant_id="fault-" + case, app="apps/" + case, log={"path": "logs/" + case + ".log", "sha256": sha256("crash " + case)})
    value = {"schema_version": 6, "evaluation_id": "validation-" + case + "-" + role, "scope": "offline_synthetic",
        "configuration": {"model_profile_sha256": sha256("model"), "context_profile_sha256": sha256("context"),
            "tool_schema_sha256": sha256("tools"), "search_limits_sha256": sha256("search"), "capabilities": [],
            "max_calls": 8, "max_known_tokens": 1000, "max_seconds": 60.0, "max_candidates": 2, "max_tool_calls": 8,
            "working_context": {"max_packet_bytes": 131072, "max_view_bytes": 786432, "retain_groups": 2},
            "knowledge_profile": knowledge, "shared_version_skill_profile": shared},
        "tasks": [{"task_id": case, "case_key": selection.case_key, "lineage_group_id": selection.lineage_group_id,
            "fault_variant_id": selection.fault_variant_id, "task_path": case + "/input", "task_sha256": sha256("task " + case),
            "source_snapshot_sha256": sha256("source " + case), "crash_sha256": selection.log.sha256,
            "protocol_sha256": sha256("protocol " + case), "observations_sha256": sha256("observations " + case),
            "selection": selection.model_dump(mode="json")}],
        "variants": [{"variant_id": "procedure", "policy_features": PolicyFeatures().model_dump(mode="json"),
            "working_context_enabled": False, "applicability_enabled": False, "version_skills_enabled": True}],
        "repeat_ids": ["repeat"], "runs": [{"task_id": case, "variant_id": "procedure", "repeat_id": "repeat",
            "run_id": case + "-" + role, "work_path": case + "-" + role}]}
    from dfx_agent.metrics import parse_evaluation_manifest

    return encode(parse_evaluation_manifest(value))


def _serialized_validation_bundle() -> tuple[dict[str, bytes], str]:
    files, seed, old_approval, child = _serialized_model_bundle()
    new_body = ModelVersionPayload.model_validate_json(child.payload_json)
    pairs = tuple(ValidationPairV2(case_id=case, kind=kind,
        old_manifest_json=_declared_manifest(case, "old", seed, child),
        new_manifest_json=_declared_manifest(case, "new", seed, child),
        old_entry_id=seed.version_id, new_entry_id=child.version_id)
        for case, kind in (("representative", "representative"), ("boundary", "boundary")))
    spec = PlanSpecV2(old_version_id=seed.version_id, new_version_id=child.version_id, pairs=pairs)
    approved = HostValidationPlanApprovalV2(approval_id="plan-approval", spec_sha256=content_hash(spec),
        old_version_approval=old_approval, new_version_approval=new_body.approval)
    plan_body = PlanPayloadV2(approval=approved, spec=spec, frozen_at="2026-10-07T00:00:00+00:00",
        evidence_root="C:/synthetic/declaration", evidence_root_identity=(1, 2, 3, 4), learning_lineage_groups=(),
        observed_absent_work_paths=("boundary-new", "boundary-old", "representative-new", "representative-old"))
    digest = content_hash(plan_body)
    plan = SkillValidationPlanV2(plan_id="skill-plan-" + digest, content_sha256=digest, payload_json=encode(plan_body))
    manifest_text = pairs[0].old_manifest_json
    row = json.loads(_validation_inventory().validation_runs[0].row_json)
    row.update(task_id="representative", run_id="representative-old")
    aggregate = {"schema_version": 6, "scope": "offline_synthetic", "manifest_sha256": sha256(manifest_text), "rows": [row]}
    receipt_body = ReceiptPayloadV2(plan_id=plan.plan_id, approval_sha256=content_hash(approved), case_id="representative", role="old",
        version_id=seed.version_id, manifest_json=manifest_text, aggregate_json=encode(aggregate), directories=(), files=(),
        target_projection_sha256=old_approval.projection_sha256,
        target_skill_id="dfx-" + seed.version_id.removeprefix("skill-version-")[:56], use_status="unknown", use_reasons=())
    digest = content_hash(receipt_body)
    receipt = SkillValidationReceiptV2(receipt_id="skill-receipt-" + digest, content_sha256=digest, payload_json=encode(receipt_body))
    files["plans/" + plan.plan_id + ".json"] = encode(plan).encode()
    files["receipts/" + receipt.receipt_id + "/receipt.json"] = encode(receipt).encode()
    return files, receipt.receipt_id


def test_adapter_extracts_exact_validation_row_and_all_missing_planned_roles() -> None:
    files, receipt_id = _serialized_validation_bundle()
    value = inventory_from_authenticated_bundle(files, task_id="current-task", run_id="current-run", purpose="deployment")
    assert value.summary.planned_validation_roles == 4
    assert value.summary.missing_validation_roles == 3
    assert value.summary.validation.known_tokens_lower_bound == 8
    assert value.summary.validation.total_tokens is value.summary.validation.reserved_calls is None
    assert value.summary.total.known_tokens_lower_bound == 25
    assert value.validation_runs[0].receipt_ids == (receipt_id,)
    assert value.validation_runs[0].key == ("representative", "representative-old")
    assert len(value.validation_plans) == 1
    assert len(value.validation_plans[0].expected_slots) == 4
    assert value.validation_plans[0].plan_id == "skill-plan-" + value.validation_plans[0].plan_content_sha256


def test_real_seed_export_is_read_only_and_keeps_manual_cost_meaning(tmp_path: Path) -> None:
    root = local_evidence_path(tmp_path / "seed-store")
    root.mkdir()
    store = SkillValidationStore(root)
    _seed_version, approval = _seed()
    projection = SolverProjection(guidance=guidance(), derivation_authority="host_curated")
    seed = store.create_seed(projection, approval=approval)
    before = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    files, _directories = store.export_version_bundle(seed.version_id, approval=approval)
    value = inventory_from_authenticated_bundle(files, task_id="current-task", run_id="current-run", purpose="candidate_validation")
    assert value.summary.learning.status == "not_run" and value.summary.learning.total_tokens is None
    assert value.summary.validation.status == "unknown"
    assert before == {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_real_frozen_seed_service_inventory_is_read_only_portable_and_rejects_copy_mutation(tmp_path: Path) -> None:
    # Legitimate public seed/publication/startup only: no SDK, model, transport,
    # application, action ledger or synthetic semantic-authority monkeypatch.
    root = local_evidence_path(tmp_path / "frozen-inventory")
    root.mkdir()
    original_root = root / "original-store"
    original_root.mkdir()
    store = SkillValidationStore(original_root)
    _declared, approval = _seed()
    projection = SolverProjection(guidance=guidance(), derivation_authority="host_curated")
    seed = store.create_seed(projection, approval=approval)
    transitions = SkillTransitionStore(store)
    publication_host = HostPublicationApproval(approval_id="inventory-seed-publication", alias="capacity",
        action="initialize_seed", version_id=seed.version_id, split="development")
    publication = transitions.publish_alias(approval=publication_host, version_approval=approval)
    selected = transitions.freeze_alias("capacity", approval=publication_host)
    task, case = selection_task(root / "task", identifier="inventory-task", lineage="unseen-inventory")
    info, run_id = verify_task(task), "inventory-run"
    workspace = CandidateWorkspace(task, root / "work", run_id)
    grant = VersionSkillGrant(version_id=seed.version_id, version_approval=approval.model_dump(mode="json"),
        selection=selected.model_dump(mode="json"), publication_approval=publication_host.model_dump(mode="json"))
    binding = TaskVersionSkillBinding(approval_id="inventory-development-startup", task_id=case.task_id,
        run_id=run_id, source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
        case_selection=case, scope=PermissionScope(split="development"), purpose="deployment", grants=(grant,))
    profile = prepare_run_version_skills(workspace, task, HostDerivedSkillInput(transitions, binding, enabled=True),
        ledger=None, max_calls=4, max_tool_calls=4)
    assert profile is not None
    service = read_run_version_skills(workspace, task, profile, ledger=None)
    assert service is not None and service.ledger is None
    frozen_profile = copy.deepcopy(profile)
    before = {path.relative_to(workspace.root).as_posix(): path.read_bytes()
        for path in workspace.root.rglob("*") if path.is_file()}
    value = service.research_cost_inventory()
    assert value.summary.learning.status == "not_run"
    assert value.summary.learning.total_tokens is None
    assert value.summary.deployment.status == value.summary.total.status == "unknown"
    assert value.contexts == (ResearchInventoryContext(task_id=case.task_id, run_id=run_id, purpose="deployment"),)
    assert tuple(event.publication_id for event in value.publications) == (publication.publication_id,)
    assert service.profile == profile == frozen_profile
    assert before == {path.relative_to(workspace.root).as_posix(): path.read_bytes()
        for path in workspace.root.rglob("*") if path.is_file()}
    original_root.rename(root / "unavailable-original-store")
    reopened = CandidateWorkspace.reopen(task, workspace.root, run_id)
    restored = read_run_version_skills(reopened, task, frozen_profile, ledger=None)
    assert restored is not None
    assert restored.research_cost_inventory() == value
    assert restored.profile == frozen_profile
    assert before == {path.relative_to(workspace.root).as_posix(): path.read_bytes()
        for path in workspace.root.rglob("*") if path.is_file()}
    copied = workspace.root / SOURCE_NAMESPACE / seed.version_id / "versions" / (seed.version_id + ".json")
    copied.write_bytes(copied.read_bytes() + b"\n")
    changed = {path.relative_to(workspace.root).as_posix(): path.read_bytes()
        for path in workspace.root.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="original differs from frozen inventory"):
        restored.research_cost_inventory()
    assert restored.profile == frozen_profile
    assert changed == {path.relative_to(workspace.root).as_posix(): path.read_bytes()
        for path in workspace.root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("schema", [True, 2, "1"])
def test_inventory_generation_is_exact(schema: Any) -> None:
    value = _learning_inventory(_atom())
    raw = value.model_dump(mode="json")
    raw["schema_version"] = schema
    with pytest.raises(ValueError):
        ResearchCostInventory.model_validate_json(encode(raw))


def test_bundle_capacity_rejects_before_parsing_or_mutating_inputs() -> None:
    files = {"versions/entry.json": b"x" * (16_777_216 + 1)}
    before = dict(files)
    with pytest.raises(ValueError, match="oversized"):
        inventory_from_authenticated_bundle(files, task_id="current-task", run_id="current-run", purpose="deployment")
    assert files == before
