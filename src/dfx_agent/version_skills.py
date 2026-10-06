"""Frozen approved version originals consumed through actual SDK read_skill calls.

Candidate validation and published deployment have separate host entry points.
Copied sources are read-only: they never invoke a learning transport or consult
the current alias when an old run is reopened.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from deepagents.middleware.skills import SkillsMiddleware

from .contracts import require_path
from .evidence_io import local_evidence_path
from .knowledge import _exists, _path, _ReadSet
from .knowledge_contracts import SolverProjection, encode
from .model_calls import canonical, sha256
from .static_skills import (
    MAX_SKILL_FILE_BYTES,
    MAX_STATIC_SKILL_BYTES,
    VIRTUAL_ROOT,
    FrozenSkillsMiddleware,
    StaticSkillError,
    StaticSkillService,
    _ApprovedBackend,
    _native_catalog,
    _skill_list,
)
from .task import digest, verify_task
from .version_skill_contracts import (
    TaskVersionSkillBinding,
    VersionSkillGrant,
    normalize_shared_version_skill_profile,
    normalize_version_skill_profile,
    shared_version_skill_profile,
)
from .workspace import CandidateWorkspace

PROFILE_PATH = "version-skills-profile.json"
NAMESPACE = "version-skills"
SOURCE_NAMESPACE = "version-skill-sources"
RENDERER = "approved_version_procedure_v1"
PROMPT = """<dfx_static_skills>
Explicitly approved immutable Skill versions
{skills_locations}
{skills_load_warnings}
{skills_list}
Read a complete version with read_skill(skill_id=the listed name).
Each original retains its host_curated or model_inference derivation authority.
Host review approves exact guidance; synthetic publication is limited to its
declared validation. It is not current task evidence or application verification.
Check premises, retain unknown questions and preserve business behavior.
Use only already authorized tools. These resources grant no file, script,
execution, delegation or learning permission.
</dfx_static_skills>
"""
PROMPT_SHA256 = sha256(PROMPT)
MAX_SOURCE_BYTES = 67_108_864
MAX_SOURCE_FILES = 10_000


@dataclass(frozen=True)
class HostValidationSkillInput:
    store: Any
    approval: TaskVersionSkillBinding
    enabled: bool = True


@dataclass(frozen=True)
class HostDerivedSkillInput:
    transitions: Any
    approval: TaskVersionSkillBinding
    enabled: bool = True


def _binding(value: TaskVersionSkillBinding) -> TaskVersionSkillBinding:
    if type(value) is not TaskVersionSkillBinding:
        raise ValueError("version startup needs explicit original task approval")
    return TaskVersionSkillBinding.model_validate_json(value.model_dump_json())


def _body(version: Any, payload: Any) -> tuple[dict[str, Any], bytes]:
    projection: SolverProjection = payload.projection
    guidance = projection.guidance
    if projection.validation_scopes or not guidance.trigger.strip() or len(guidance.trigger) > 1024:
        raise ValueError("version guidance has unsupported metadata")
    name = "dfx-" + version.content_sha256[:56]
    authority = projection.derivation_authority
    annotations = {"renderer": RENDERER, "derivation_authority": authority,
        "epistemic_status": projection.epistemic_status, "content_sha256": version.content_sha256,
        "projection_sha256": payload.approval.projection_sha256, "application_verification": "not_run"}
    front = "\n".join(("---", "name: " + json.dumps(name),
        "description: " + json.dumps(guidance.trigger, ensure_ascii=False),
        "metadata: " + json.dumps(annotations, ensure_ascii=False, sort_keys=True), "---"))
    lines = [front, "", "# Explicitly approved immutable procedure", "",
        "Derivation authority: " + authority + ". Historical guidance; application verification not_run.",
        "No additional execution, file or learning permission is granted.", "", "## Trigger", guidance.trigger,
        "", "## Preconditions and original unknown questions"]
    for premise in guidance.premises:
        lines.extend((f"- {premise.premise_id}: {premise.statement}", "  Unknown question: " + premise.unknown_question))
    lines.extend(("", "## Procedure and observation branches"))
    for index, step in enumerate(guidance.procedure, 1):
        lines.append(f"{index}. {step.step_id}: {step.instruction}")
        for branch in step.branches:
            lines.append("   Observation: " + branch.observation + "\n   Next action: " + branch.next_action)
    for title, values in (("Behaviors to preserve", guidance.preserves), ("Counterexamples", guidance.counterexamples),
                          ("Exit conditions", guidance.exit_conditions)):
        lines.extend(("", "## " + title, *("- " + value for value in values)))
    lines.extend(("", "## Complete approved projection", encode(projection), ""))
    raw = "\n".join(lines).encode("utf-8")
    if len(raw) > MAX_SKILL_FILE_BYTES:
        raise ValueError("version procedure exceeds resource capacity")
    virtual = VIRTUAL_ROOT + name + "/SKILL.md"
    metadata = {"name": name, "description": guidance.trigger.strip(), "path": virtual, "metadata": annotations,
                "license": None, "compatibility": None, "allowed_tools": []}
    costs = ({"draft": payload.draft_cost.model_dump(mode="json"),
              "operation": payload.operation_cost.model_dump(mode="json"),
              "learning_source_id": payload.learning_source_id}
             if authority == "model_inference" else {"learning_model_call_status": "not_run", "learning_tokens": None})
    return {"skill_id": name, "entry_id": version.version_id, "version_id": version.version_id,
        "content_sha256": version.content_sha256, "projection_sha256": payload.approval.projection_sha256,
        "path": NAMESPACE + "/" + name + "/SKILL.md", "virtual_path": virtual, "sha256": digest(raw),
        "bytes": len(raw), "metadata": metadata, "derivation_authority": authority,
        "learning_lineage_groups": list(payload.learning_lineage_groups), "source_costs": costs}, raw


def _bundle_authority(files: dict[str, bytes], grant: VersionSkillGrant) -> dict[str, Any]:
    """Summarize linked originals from an already authenticated closed bundle.

    Publication history can contain a model child even after rollback to its
    seed. All copied approved sources retain their permission and lineage; only
    the selected projection becomes a solver procedure. No directory scan or
    metadata declaration alone grants authority here.
    """
    from .skill_evolution_contracts import (
        parse_plan,
        parse_plan_payload,
        parse_version,
        parse_version_approval,
        parse_version_payload,
    )
    from .skill_transition_contracts import (
        parse_publication,
        parse_publication_approval,
        parse_publication_payload,
        parse_transition,
        parse_transition_payload,
    )

    versions: dict[str, Any] = {}
    events: dict[str, Any] = {}
    allowed = {"development", "candidate_validation", "confirmation"}
    excluded, lineages = set(), set()

    def event_path(namespace: str, identifier: str) -> str:
        found = [name for name in files if name.startswith(namespace + "/") and name.endswith("-" + identifier + ".json")]
        if len(found) != 1:
            raise ValueError("approved source bundle has missing or ambiguous event")
        return found[0]

    def version(identifier: str, approved: Any) -> None:
        previous = versions.get(identifier)
        if previous is not None:
            if previous != approved:
                raise ValueError("approved source links disagree about version authority")
            return
        if len(versions) >= 64:
            raise ValueError("approved version source closure exceeds depth capacity")
        record = parse_version(files["versions/" + identifier + ".json"].decode("utf-8"))
        body = parse_version_payload(record.payload_json)
        if record.version_id != identifier or body.approval != approved:
            raise ValueError("approved source version differs from its linked authority")
        versions[identifier] = approved
        allowed.intersection_update(body.approval.allowed_splits)
        excluded.update(body.approval.excluded_lineage_groups)
        lineages.update(body.learning_lineage_groups)
        if body.approval.parent_version_id is not None:
            if body.parent_approval is None:
                raise ValueError("approved source lacks original parent authority")
            version(body.approval.parent_version_id, body.parent_approval)

    def transition(identifier: str, approved: Any) -> None:
        previous = events.get(identifier)
        if previous is not None:
            if previous != approved:
                raise ValueError("approved transition links disagree")
            return
        events[identifier] = approved
        record = parse_transition(files[event_path("transitions", identifier)].decode("utf-8"))
        body = parse_transition_payload(record.payload_json)
        if record.transition_id != identifier or body.approval != approved:
            raise ValueError("approved transition source differs")
        plan = parse_plan(files["plans/" + approved.plan_id + ".json"].decode("utf-8"))
        payload = parse_plan_payload(plan.payload_json)
        if plan.plan_id != approved.plan_id or payload.approval != body.plan_approval:
            raise ValueError("approved transition plan source differs")
        version(payload.spec.old_version_id, payload.approval.old_version_approval)
        version(payload.spec.new_version_id, payload.approval.new_version_approval)
        if approved.previous_transition_id is not None:
            if body.previous_approval is None:
                raise ValueError("approved transition lacks original previous authority")
            transition(approved.previous_transition_id, body.previous_approval)

    def publication(identifier: str, approved: Any) -> None:
        if identifier in events:
            raise ValueError("approved publication source has repeated links")
        events[identifier] = approved
        record = parse_publication(files[event_path("publications", identifier)].decode("utf-8"))
        body = parse_publication_payload(record.payload_json)
        if record.publication_id != identifier or body.approval != approved:
            raise ValueError("approved publication source differs")
        version(body.approval.version_id, body.version_approval)
        if body.approval.transition_id is not None:
            if body.transition_approval is None:
                raise ValueError("approved publication lacks its original transition authority")
            transition(body.approval.transition_id, body.transition_approval)
        if body.approval.previous_publication_id is not None:
            if body.previous_approval is None:
                raise ValueError("approved publication lacks original previous authority")
            publication(body.approval.previous_publication_id, body.previous_approval)

    version(grant.version_id, parse_version_approval(grant.version_approval))
    if grant.selection is not None and grant.publication_approval is not None:
        publication(grant.selection["publication_id"], parse_publication_approval(grant.publication_approval))
    if {name for name in files if name.startswith("versions/")} != {"versions/" + identifier + ".json" for identifier in versions}:
        raise ValueError("approved source bundle includes unlinked versions")
    return {"version_ids": sorted(versions), "allowed_splits": sorted(allowed),
            "excluded_lineage_groups": sorted(excluded), "learning_lineage_groups": sorted(lineages)}


def _compile(store: Any, grants: tuple[VersionSkillGrant, ...], *, transitions: Any = None,
             capture_current: bool = False) -> tuple[dict[str, Any], dict[str, bytes], dict[str, bytes]]:
    from .skill_evolution import SkillTransitionStore
    from .skill_evolution_contracts import (
        parse_version,
        parse_version_approval,
        parse_version_payload,
    )
    from .skill_transition_contracts import parse_publication_approval, parse_selection
    from .skill_validation import SkillValidationStore, _learning_costs_from_bundle

    if type(store) is not SkillValidationStore:
        raise TypeError("version compilation requires the original closed source store")

    resources, bodies, originals, directories = [], {}, {}, set()
    for grant in grants:
        approval = parse_version_approval(grant.version_approval)
        selected_projection_hash = None
        if grant.selection is not None:
            if grant.publication_approval is None:
                raise ValueError("deployment requires original publication approval")
            if transitions is None:
                raise ValueError("deployment requires publication source reader")
            if type(transitions) is not SkillTransitionStore or transitions.validation is not store:
                raise TypeError("deployment requires the original closed publication store")
            selection = parse_selection(grant.selection)
            published = parse_publication_approval(grant.publication_approval)
            closure, source_dirs = transitions.export_selection_bundle(selection, publication_approval=published)
            selected_projection_hash = selection.projection_sha256
        else:
            closure, source_dirs = store.export_version_bundle(grant.version_id, approval=approval)
        version = parse_version(closure["versions/" + grant.version_id + ".json"].decode("utf-8"))
        payload = parse_version_payload(version.payload_json)
        if version.version_id != grant.version_id or payload.approval != approval:
            raise ValueError("version bundle differs from the exact original grant")
        if selected_projection_hash is not None and selected_projection_hash != payload.approval.projection_sha256:
            raise ValueError("published selection differs from version original")
        source_root = SOURCE_NAMESPACE + "/" + grant.version_id
        directories.add(source_root)
        for directory in source_dirs:
            require_path(directory)
            directories.add(source_root + "/" + directory)
        file, raw = _body(version, payload)
        file["source_costs"]["ancestry"] = _learning_costs_from_bundle(closure, grant.version_id, approval)
        file["source_authority"] = _bundle_authority(closure, grant)
        if file["virtual_path"] in bodies:
            raise ValueError("version catalog names collide")
        resources.append(file)
        bodies[file["virtual_path"]] = raw
        for relative, original in closure.items():
            require_path(relative)
            path = SOURCE_NAMESPACE + "/" + grant.version_id + "/" + relative
            if type(original) is not bytes or path.casefold() in {key.casefold() for key in originals}:
                raise ValueError("version source closure contains duplicate or invalid files")
            originals[path] = original
    inventory = [{"path": path, "sha256": digest(raw), "bytes": len(raw)} for path, raw in sorted(originals.items())]
    if (sum(map(len, originals.values())) > MAX_SOURCE_BYTES or len(originals) + len(directories) + 1 > MAX_SOURCE_FILES or
            sum(map(len, bodies.values())) > MAX_STATIC_SKILL_BYTES):
        raise ValueError("version source/resource closure exceeds capacity")
    shared = {"schema_version": 1, "source_type": "approved_versions", "renderer": RENDERER,
              "prompt_sha256": PROMPT_SHA256, "resources": resources, "files": resources, "source_files": inventory,
              "source_directories": sorted(directories),
              "snapshot_sha256": sha256(canonical({"resources": resources, "source_files": inventory,
                                                   "source_directories": sorted(directories)}))}
    if capture_current:
        # Check after the potentially long immutable-source export, immediately
        # before the caller publishes a task copy. Historical rebuilds skip this.
        for grant in grants:
            if grant.selection is not None and grant.publication_approval is not None:
                transitions.assert_current_selection(parse_selection(grant.selection),
                    publication_approval=parse_publication_approval(grant.publication_approval))
    return normalize_shared_version_skill_profile(shared), bodies, originals


def preview_version_skill_profile(store: Any, grants: tuple[VersionSkillGrant, ...]) -> dict[str, Any]:
    """Pure prospective pool identity, computed before the validation plan is frozen."""
    return _compile(store, grants)[0]


def _task(workspace: CandidateWorkspace, task_directory: Path, approval: TaskVersionSkillBinding,
          shared: dict[str, Any], *, startup: bool = False) -> None:
    from .metrics import EvaluationManifestV6, parse_evaluation_manifest
    from .skill_evolution_contracts import parse_plan, parse_plan_payload

    task = verify_task(task_directory)
    if (approval.task_id != workspace.task_id or approval.run_id != workspace.run_id or
            approval.source_snapshot_sha256 != workspace.source_snapshot_sha256 or
            approval.source_snapshot_sha256 != task["source_snapshot_sha256"] or
            approval.task_id != task["task_id"] or approval.crash_sha256 != task["crash_sha256"] or
            task.get("schema_version") != 2 or task.get("case_identity_sha256") != approval.case_selection.sha256()):
        raise ValueError("version task approval differs from actual workspace")
    lineage = approval.case_selection.lineage_group_id
    for grant in approval.grants:
        allowed = grant.version_approval["allowed_splits"]
        excluded = grant.version_approval["excluded_lineage_groups"]
        if (approval.scope.split not in allowed or not set(excluded) <= set(approval.scope.excluded_lineage_groups) or
                (grant.selection is not None and grant.selection["split"] != approval.scope.split)):
            raise ValueError("version startup widens original authority")
        resource = next(item for item in shared["resources"] if item["version_id"] == grant.version_id)
        authority = resource["source_authority"]
        if (approval.scope.split not in authority["allowed_splits"] or
                not set(authority["excluded_lineage_groups"]) <= set(approval.scope.excluded_lineage_groups)):
            raise ValueError("version startup widens its complete approved source authority")
        if lineage is not None and (lineage in approval.scope.excluded_lineage_groups or
                                    lineage in authority["learning_lineage_groups"]):
            raise ValueError("version startup intersects excluded or learned task lineage")
    if approval.purpose == "candidate_validation":
        if approval.validation_plan_json is None:
            raise ValueError("validation startup requires original prospective plan")
        plan = parse_plan(approval.validation_plan_json)
        payload = parse_plan_payload(plan.payload_json)
        pair = next(pair for pair in payload.spec.pairs if pair.case_id == approval.validation_case_id)
        raw = pair.old_manifest_json if approval.validation_role == "old" else pair.new_manifest_json
        manifest = parse_evaluation_manifest(json.loads(raw))
        if not isinstance(manifest, EvaluationManifestV6):
            raise ValueError("version validation requires an exact v6 manifest")
        cell, planned = manifest.runs[0], manifest.tasks[0]
        if cell.work_path is None:
            raise ValueError("version validation requires its planned work path")
        expected_work = Path(payload.evidence_root) / cell.work_path
        expected_task = Path(payload.evidence_root) / planned.task_path
        # Paths are meaningful at initial publication. Portable readback instead
        # authenticates task/run/hash and the immutable plan's role identities.
        if (cell.run_id != workspace.run_id or planned.task_id != workspace.task_id or
                planned.source_snapshot_sha256 != workspace.source_snapshot_sha256 or
                planned.crash_sha256 != approval.crash_sha256 or
                planned.task_sha256 != digest((local_evidence_path(task_directory) / "task.json").read_bytes()) or
                planned.selection != approval.case_selection or
                canonical(manifest.configuration.shared_version_skill_profile) != canonical(shared)):
            raise ValueError("version validation startup differs from exact planned role")
        if startup and (local_evidence_path(task_directory) != local_evidence_path(expected_task) or
                        local_evidence_path(workspace.root) != local_evidence_path(expected_work)):
            raise ValueError("validation role work path differs from prospective plan")


def _inventory(workspace: CandidateWorkspace, profile: dict[str, Any]) -> dict[str, bytes]:
    from .feedback import _ClosedTree, _ReadBudget
    from .feedback_contracts import FeedbackLimits

    root = _path(workspace.root, workspace.root / SOURCE_NAMESPACE, directory=True)
    limits = FeedbackLimits(max_entries=MAX_SOURCE_FILES, max_total_bytes=MAX_SOURCE_BYTES,
        max_file_bytes=16_777_216, max_capture_read_bytes=2 * MAX_SOURCE_BYTES)
    tree = _ClosedTree(root, limits, _ReadBudget(2 * MAX_SOURCE_BYTES))
    tree.scan(((".", None),))
    expected = {file["path"].removeprefix(SOURCE_NAMESPACE + "/"): file for file in profile["source_files"]}
    expected_dirs = {directory.removeprefix(SOURCE_NAMESPACE + "/") for directory in profile["source_directories"]}
    if set(tree.raw) != set(expected) or set(tree.directories) != expected_dirs:
        raise ValueError("version source closure has missing or additional originals")
    for relative, raw in tree.raw.items():
        file = expected[relative]
        if len(raw) != file["bytes"] or digest(raw) != file["sha256"]:
            raise ValueError("version source original differs from frozen inventory")
    tree.finish()
    return tree.raw


class VersionSkillService(StaticSkillService):
    def __init__(self, workspace: CandidateWorkspace, task_directory: Path, profile: dict[str, Any], ledger: Any):
        self.workspace, self.task_directory, self.ledger = workspace, task_directory, ledger
        self._profile_json = encode(normalize_version_skill_profile(profile))
        # Only this service instance may reuse a semantically authenticated
        # immutable closure. Each callback still checks the full raw inventory,
        # task identity and rendered files against the frozen original profile.
        # Resume/report readers construct a fresh service and authenticate again.
        self._authenticated_bodies: dict[str, bytes] | None = None

    def _validate(self) -> dict[str, bytes]:
        from .skill_evolution import SkillTransitionStore
        from .skill_validation import SkillValidationStore

        profile = self.profile
        approval = TaskVersionSkillBinding.model_validate_json(encode(profile["approval"]))
        _task(self.workspace, self.task_directory, approval, shared_version_skill_profile(profile))
        originals = _inventory(self.workspace, profile)
        bodies = self._authenticated_bodies
        if bodies is None:
            combined_resources, bodies, rebuilt_sources, directories = [], {}, {}, set()
            for grant in approval.grants:
                store = SkillValidationStore(self.workspace.root / SOURCE_NAMESPACE / grant.version_id)
                transition = SkillTransitionStore(store) if grant.selection is not None else None
                shared, version_bodies, version_sources = _compile(store, (grant,), transitions=transition)
                combined_resources.extend(shared["resources"])
                bodies.update(version_bodies)
                rebuilt_sources.update(version_sources)
                directories.update(shared["source_directories"])
            if (combined_resources != profile["resources"] or {
                    key.removeprefix(SOURCE_NAMESPACE + "/"): raw for key, raw in rebuilt_sources.items()} != originals or
                    sorted(directories) != profile["source_directories"]):
                raise ValueError("version originals differ from approved complete source closure")
        root = _path(self.workspace.root, self.workspace.root / NAMESPACE, directory=True)
        if {item.name for item in root.iterdir()} != {file["skill_id"] for file in profile["files"]}:
            raise ValueError("version resource namespace differs")
        reads = _ReadSet(self.workspace.root, max_read_bytes=2 * (MAX_STATIC_SKILL_BYTES + len(self._profile_json.encode())),
                         max_read_calls=2 * (len(profile["files"]) + 1))
        if reads.read(self.workspace.root / PROFILE_PATH) != self._profile_json.encode("utf-8"):
            raise ValueError("version profile original changed")
        for file in profile["files"]:
            folder = _path(self.workspace.root, self.workspace.root / file["path"], directory=False).parent
            if {item.name for item in folder.iterdir()} != {"SKILL.md"}:
                raise ValueError("version resource has unapproved support files")
            raw = reads.read(self.workspace.root / file["path"], expected_hash=file["sha256"], expected_bytes=file["bytes"])
            if raw != bodies[file["virtual_path"]]:
                raise ValueError("version body differs from exact approved renderer")
        reads.finish()
        self._authenticated_bodies = dict(bodies)
        return dict(bodies)

    def middleware(self) -> FrozenVersionSkillsMiddleware:
        return FrozenVersionSkillsMiddleware(self)

    def expected_section(self) -> str | None:
        self._validate()
        return version_skill_section(self.profile)


class FrozenVersionSkillsMiddleware(FrozenSkillsMiddleware):
    def __init__(self, service: VersionSkillService):
        self._service = service
        profile = service.profile
        backend = _ApprovedBackend(profile["files"], {file["virtual_path"]: b"" for file in profile["files"]},
                                   self._check_resources)
        SkillsMiddleware.__init__(self, backend=backend, sources=[(VIRTUAL_ROOT, "Approved versions")],
                                  system_prompt=PROMPT if profile["enabled"] else None)


def version_skill_section(profile: dict[str, Any]) -> str | None:
    profile = normalize_version_skill_profile(profile)
    if not profile["enabled"]:
        return None
    native = SkillsMiddleware(backend=_ApprovedBackend(profile["files"], {}),
                              sources=[(VIRTUAL_ROOT, "Approved versions")], system_prompt=None)
    return PROMPT.format(skills_locations=native._format_skills_locations(), skills_load_warnings="",
                        skills_list=_skill_list(cast(Any, [file["metadata"] for file in profile["files"]])))


def _absent(workspace: CandidateWorkspace) -> None:
    if any(_exists(workspace.root / name) for name in (PROFILE_PATH, NAMESPACE, SOURCE_NAMESPACE)):
        raise ValueError("run cannot acquire version resources outside startup")


def prepare_run_version_skills(workspace: CandidateWorkspace, task_directory: Path,
        source: HostValidationSkillInput | HostDerivedSkillInput | None, *, ledger: Any,
        max_calls: int, max_tool_calls: int) -> dict[str, Any] | None:
    _absent(workspace)
    if source is None:
        return None
    approval = _binding(source.approval)
    if type(source.enabled) is not bool:
        raise ValueError("version injection switch must be explicit boolean")
    transitions = None
    if type(source) is HostValidationSkillInput and approval.purpose == "candidate_validation":
        from .skill_evolution_contracts import parse_plan, parse_validation_approval

        if approval.validation_plan_json is None or approval.validation_plan_approval is None:
            raise ValueError("validation startup needs original prospective plan and approval")
        planned = parse_plan(approval.validation_plan_json)
        approved = parse_validation_approval(approval.validation_plan_approval)
        if source.store.read_plan(planned.plan_id, approval=approved) != planned:
            raise ValueError("validation plan differs from original source")
        store = source.store
    elif type(source) is HostDerivedSkillInput and approval.purpose == "deployment":
        transitions, store = source.transitions, source.transitions.validation
    else:
        raise ValueError("version startup input has the wrong authorization purpose")
    shared, bodies, originals = _compile(store, approval.grants, transitions=transitions, capture_current=True)
    _task(workspace, task_directory, approval, shared, startup=True)
    profile = {**shared, "enabled": source.enabled, "approval": approval.model_dump(mode="json")}
    profile["profile_sha256"] = sha256(canonical(profile))
    profile = normalize_version_skill_profile(profile)
    if _native_catalog(_ApprovedBackend(profile["files"], bodies)) != [file["metadata"] for file in profile["files"]]:
        raise StaticSkillError()
    from .static_skill_handoff import preflight_static_skill_report

    preflight_static_skill_report(profile, version_skill_section(profile), max_calls, max_tool_calls)
    _absent(workspace)
    for directory in profile["source_directories"]:
        target = workspace.root / directory
        target.mkdir(parents=True, exist_ok=True)
        _path(workspace.root, target, directory=True)
    for relative, raw in originals.items():
        target = workspace.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _path(workspace.root, target.parent, directory=True)
        with target.open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    for file in profile["files"]:
        path = workspace.root / file["path"]
        path.parent.mkdir(parents=True, exist_ok=False)
        with path.open("xb") as output:
            output.write(bodies[file["virtual_path"]])
            output.flush()
            os.fsync(output.fileno())
    with (workspace.root / PROFILE_PATH).open("xb") as output:
        output.write(encode(profile).encode("utf-8"))
        output.flush()
        os.fsync(output.fileno())
    # The compiler authenticated the complete immutable source closure. Exact
    # copied bytes and rendered resources are now closed by this same instance;
    # rebuilding the identical source semantics would not add new authority.
    service = VersionSkillService(workspace, task_directory, profile, ledger)
    service._authenticated_bodies = dict(bodies)
    service._validate()
    cast(Any, workspace)._frozen_version_skill_service = service
    read_run_version_skills(workspace, task_directory, profile, ledger=ledger)
    return profile


def read_run_version_skills(workspace: CandidateWorkspace, task_directory: Path, profile: dict[str, Any] | None,
                            *, ledger: Any) -> VersionSkillService | None:
    if profile is None:
        _absent(workspace)
        return None
    current = getattr(workspace, "_frozen_version_skill_service", None)
    if (type(current) is VersionSkillService and current.workspace is workspace and
            local_evidence_path(current.task_directory) == local_evidence_path(task_directory) and
            current.profile == profile):
        service = current
    else:
        service = VersionSkillService(workspace, task_directory, profile, ledger)
    bodies = service._validate()
    if _native_catalog(_ApprovedBackend(service.profile["files"], bodies)) != service.expected_metadata():
        raise ValueError("SDK catalog differs from frozen version resources")
    return service


def authenticated_learning_lineages(work: Path, original_session_profile: dict[str, Any], feedback_approval: Any) -> set[str]:
    """Authenticate every frozen ancestor before admitting a development feedback source."""
    from .skill_evolution import SkillTransitionStore
    from .skill_validation import SkillValidationStore

    profile = normalize_version_skill_profile(original_session_profile)
    approval = TaskVersionSkillBinding.model_validate_json(encode(profile["approval"]))
    if approval.purpose != "deployment" or approval.scope.split != "development":
        raise ValueError("candidate-validation/confirmation Skill use cannot become development learning")
    # A minimal owner is sufficient here: capsule aggregation already verified
    # the actual task/run. No graph, checks, tools or transport are constructed.
    workspace = CandidateWorkspace.__new__(CandidateWorkspace)
    workspace.root = work
    _inventory(workspace, profile)
    groups = set()
    for grant in approval.grants:
        if grant.selection is None or grant.publication_approval is None:
            raise ValueError("development feedback requires published frozen resources")
        store = SkillValidationStore(work / SOURCE_NAMESPACE / grant.version_id)
        transitions = SkillTransitionStore(store)
        shared, _bodies, _originals = _compile(store, (grant,), transitions=transitions)
        resource = shared["resources"][0]
        saved = next(item for item in profile["resources"] if item["version_id"] == grant.version_id)
        if resource != saved:
            raise ValueError("feedback source authority differs from frozen run original")
        authority = resource["source_authority"]
        if (not set(feedback_approval.allowed_splits) <= set(authority["allowed_splits"]) or
                not set(authority["excluded_lineage_groups"]) <= set(feedback_approval.excluded_lineage_groups)):
            raise ValueError("feedback learning widens its complete approved version sources")
        groups.update(authority["learning_lineage_groups"])
    return groups
