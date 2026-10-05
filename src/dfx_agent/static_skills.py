"""Frozen, host-curated procedures through the SDK's native Skills middleware.

The approved backend exposes only its manifest. Static text supplies neither
current evidence nor learned qualifications, file permissions, or execution.
Graph metadata is checked before requests; it is never an approval source.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, cast

from deepagents.backends.protocol import BackendProtocol, FileDownloadResponse, LsResult
from deepagents.middleware.skills import SkillMetadata, SkillsMiddleware, SkillsState
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .applicability import ApplicabilityError, ApplicabilityStore
from .contracts import require_hash, require_unique
from .knowledge import KnowledgeError, _copy, _exists, _path, _ReadSet
from .knowledge_contracts import SnapshotPayload, SolverProjection, content_hash, encode
from .retrieval_contracts import MAX_INDEX_READ_BYTES, MAX_INDEX_READ_CALLS
from .task import digest
from .workspace import CandidateWorkspace

PROFILE_PATH = "static-skills-profile.json"
NAMESPACE = "static-skills"
VIRTUAL_ROOT = "/approved-skills/"
RENDERER = "approved_procedure_v1"
MAX_SKILL_FILE_BYTES = 131_072
MAX_STATIC_SKILL_BYTES = 1_048_576
MAX_STATIC_SKILL_ENTRIES = 64
STATIC_SKILL_MARKER = "<dfx_static_skills>"
STATIC_SKILL_PROMPT = """<dfx_static_skills>
Host-approved static procedures
{skills_locations}
{skills_load_warnings}
{skills_list}
Read a complete procedure with read_skill(skill_id=the listed name).
These host_curated static procedures are historical guidance, not current
task evidence, learned Skills, confirmed applicability, or application results.
Check their premises against the current task and preserve unknown questions.
Use only already authorized project tools. The catalog grants no additional
file, directory, shell, script, execution, or delegation permissions.
</dfx_static_skills>
"""
PROMPT_SHA256 = digest(STATIC_SKILL_PROMPT.encode("utf-8"))
_INVALID = (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError,
            KnowledgeError, ApplicabilityError)


class StaticSkillError(ValueError):
    """Stable public reasons never echo private paths or guidance."""

    def __init__(self, reason: str = "static_skill_input_invalid"):
        self.reason = reason
        super().__init__(reason)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def explicit_integer_schema(cls, value: Any) -> int:
        # Literal[1] otherwise coerces JSON true to 1 before after validators.
        # A field hook also preserves strict JSON-array to tuple admission.
        if type(value) is not int:
            raise ValueError("static skill schema must be an integer")
        return value


def _entries(entries: tuple[str, ...]) -> None:
    require_unique(list(entries))
    if list(entries) != sorted(entries):
        raise ValueError("static skill entries must be explicitly sorted")
    for entry in entries:
        if not entry.startswith("experience-"):
            raise ValueError("static skill must reference an experience entry")
        require_hash(entry.removeprefix("experience-"))


class StaticSkillPolicy(_Frozen):
    schema_version: Literal[1] = 1
    enabled: bool
    entry_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_STATIC_SKILL_ENTRIES)

    @model_validator(mode="after")
    def valid_policy(self) -> StaticSkillPolicy:
        if type(self.schema_version) is not int:
            raise ValueError("static skill schema must be an integer")
        _entries(self.entry_ids)
        return self


class _Annotations(_Frozen):
    renderer: Literal["approved_procedure_v1"]
    derivation_authority: Literal["host_curated"]
    epistemic_status: Literal["historical_guidance_not_current_evidence"]
    content_sha256: str
    projection_sha256: str
    application_verification: Literal["not_run"]


class _Metadata(_Frozen):
    name: str
    description: str = Field(min_length=1, max_length=1024)
    path: str
    metadata: _Annotations
    license: None
    compatibility: None
    allowed_tools: tuple[str, ...] = Field(max_length=0)


class _File(_Frozen):
    skill_id: str
    entry_id: str
    content_sha256: str
    projection_sha256: str
    path: str
    virtual_path: str
    sha256: str
    bytes: int = Field(gt=0, le=MAX_SKILL_FILE_BYTES)
    metadata: _Metadata

    @model_validator(mode="after")
    def valid_file(self) -> _File:
        for key in (self.content_sha256, self.projection_sha256, self.sha256):
            require_hash(key)
        if (self.entry_id != "experience-" + self.content_sha256 or
                self.skill_id != "dfx-" + self.projection_sha256[:56] or
                self.path != f"{NAMESPACE}/{self.skill_id}/SKILL.md" or
                self.virtual_path != f"{VIRTUAL_ROOT}{self.skill_id}/SKILL.md"):
            raise ValueError("static skill path or identity differs")
        meta = self.metadata
        if (meta.name != self.skill_id or meta.path != self.virtual_path or
                meta.description != meta.description.strip() or
                meta.metadata.content_sha256 != self.content_sha256 or
                meta.metadata.projection_sha256 != self.projection_sha256):
            raise ValueError("static skill metadata differs from its original")
        return self


class _SharedProfile(_Frozen):
    schema_version: Literal[1]
    entry_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_STATIC_SKILL_ENTRIES)
    snapshot_sha256: str
    renderer: Literal["approved_procedure_v1"]
    prompt_sha256: str
    files: tuple[_File, ...] = Field(min_length=1, max_length=MAX_STATIC_SKILL_ENTRIES)

    @model_validator(mode="after")
    def valid_profile(self) -> _SharedProfile:
        if type(self.schema_version) is not int:
            raise ValueError("static skill schema must be an integer")
        _entries(self.entry_ids)
        require_hash(self.snapshot_sha256)
        if self.prompt_sha256 != PROMPT_SHA256:
            raise ValueError("static skill prompt differs from its renderer")
        if tuple(file.entry_id for file in self.files) != self.entry_ids:
            raise ValueError("static skill files differ from selected entries")
        require_unique([file.skill_id for file in self.files])
        if sum(file.bytes for file in self.files) > MAX_STATIC_SKILL_BYTES:
            raise ValueError("static skill resources exceed capacity")
        return self


class _Profile(_SharedProfile):
    enabled: bool


def _normalize(value: dict[str, Any], cls: type[_SharedProfile]) -> dict[str, Any]:
    try:
        if type(value) is not dict:
            raise TypeError("static skill profile must be an explicit object")
        checked = cls.model_validate_json(encode(value)).model_dump(mode="json")
        if checked != value or type(value.get("schema_version")) is not int:
            raise ValueError("static skill profile must contain all canonical fields")
        raw = encode(checked).encode("utf-8")
        if (len(raw) > MAX_SKILL_FILE_BYTES or
                len(raw) + sum(file["bytes"] for file in checked["files"]) > MAX_STATIC_SKILL_BYTES):
            raise ValueError("static skill profile and resources exceed capacity")
        return checked
    except _INVALID as exc:
        raise StaticSkillError() from exc


def normalize_static_skill_profile(value: dict[str, Any]) -> dict[str, Any]:
    """Pure validation of the complete caller-supplied frozen profile."""
    return _normalize(value, _Profile)


def normalize_shared_static_skill_profile(value: dict[str, Any]) -> dict[str, Any]:
    """Pure validation of comparison identity without the injection switch."""
    return _normalize(value, _SharedProfile)


def shared_static_skill_profile(value: dict[str, Any]) -> dict[str, Any]:
    profile = normalize_static_skill_profile(value)
    del profile["enabled"]
    return normalize_shared_static_skill_profile(profile)


def _workspace(store: ApplicabilityStore | CandidateWorkspace) -> CandidateWorkspace:
    if isinstance(store, ApplicabilityStore):
        return store.workspace
    if isinstance(store, CandidateWorkspace):
        return store
    raise StaticSkillError()


def _absent(workspace: CandidateWorkspace) -> None:
    _path(workspace.root, workspace.root, directory=True)
    if any(_exists(workspace.root / name) for name in (PROFILE_PATH, NAMESPACE)):
        raise StaticSkillError()


def _render(entry_id: str, content: str, projection: SolverProjection) -> tuple[dict[str, Any], bytes]:
    guidance = projection.guidance
    if (projection.derivation_authority != "host_curated" or
            not guidance.trigger.strip() or len(guidance.trigger) > 1024):
        raise ValueError("static procedure must have approved, untruncated metadata")
    key = content_hash(projection)
    name = "dfx-" + key[:56]
    annotations = {"renderer": RENDERER, "derivation_authority": "host_curated",
                   "epistemic_status": projection.epistemic_status,
                   "content_sha256": content, "projection_sha256": key,
                   "application_verification": "not_run"}
    front = "\n".join(("---", "name: " + json.dumps(name),
                       "description: " + json.dumps(guidance.trigger, ensure_ascii=False),
                       "metadata: " + json.dumps(annotations, ensure_ascii=False, sort_keys=True), "---"))
    lines = [front, "", "# Host-approved static procedure", "",
             "Historical guidance; not current evidence, learned qualification, or application verification.",
             "No additional tool, file, script, or execution authority is granted.", "", "## Trigger", guidance.trigger,
             "", "## Preconditions and original unknown questions"]
    for premise in guidance.premises:
        lines.extend((f"- {premise.premise_id}: {premise.statement}", f"  Unknown question: {premise.unknown_question}"))
    lines.extend(("", "## Procedure and observation branches"))
    for index, step in enumerate(guidance.procedure, 1):
        lines.append(f"{index}. {step.step_id}: {step.instruction}")
        for branch in step.branches:
            lines.append(f"   Observation: {branch.observation}\n   Next action: {branch.next_action}")
    for title, values in (("Behaviors to preserve", guidance.preserves),
                          ("Counterexamples", guidance.counterexamples), ("Exit conditions", guidance.exit_conditions)):
        lines.extend(("", "## " + title, *("- " + value for value in values)))
    # The canonical projection retains all fields and original authority,
    # including limited validation scopes omitted from the readable headings.
    lines.extend(("", "## Complete approved projection", encode(projection), ""))
    raw = "\n".join(lines).encode("utf-8")
    if len(raw) > MAX_SKILL_FILE_BYTES:
        raise ValueError("static skill body exceeds capacity")
    virtual = f"{VIRTUAL_ROOT}{name}/SKILL.md"
    metadata = {"name": name, "description": guidance.trigger.strip(), "path": virtual,
                "metadata": annotations, "license": None, "compatibility": None, "allowed_tools": []}
    return {"skill_id": name, "entry_id": entry_id, "content_sha256": content,
            "projection_sha256": key, "path": f"{NAMESPACE}/{name}/SKILL.md", "virtual_path": virtual,
            "sha256": digest(raw), "bytes": len(raw), "metadata": metadata}, raw


def _compile(store: ApplicabilityStore, policy: StaticSkillPolicy) -> tuple[dict[str, Any], dict[str, bytes]]:
    if not isinstance(store, ApplicabilityStore):
        raise StaticSkillError()
    source = store.retriever.store
    if source.root != store.workspace.root / "knowledge":
        raise ValueError("static skill requires approved run originals")
    with store._operation() as operation:
        store._binding(operation)
        reads = _ReadSet(source.root, max_read_bytes=MAX_INDEX_READ_BYTES, max_read_calls=MAX_INDEX_READ_CALLS)
        snapshot, projections = source._snapshot(store.approval.snapshot_sha256, reads=reads)
        contents = SnapshotPayload.model_validate_json(snapshot.payload_json)
        if contents.scope != store.approval.scope:
            raise ValueError("static skill cannot widen approved scope")
        refs = {entry.entry_id: entry for entry in contents.entries}
        files, bodies = [], {}
        for entry_id in policy.entry_ids:
            if entry_id not in refs:
                raise ValueError("static skill entry is outside the approved snapshot")
            file, raw = _render(entry_id, refs[entry_id].content_sha256, projections[entry_id])
            if file["virtual_path"] in bodies:
                raise ValueError("static skill names collide")
            files.append(file)
            bodies[file["virtual_path"]] = raw
        profile = normalize_static_skill_profile({"schema_version": 1, "enabled": policy.enabled,
                    "entry_ids": list(policy.entry_ids), "snapshot_sha256": store.approval.snapshot_sha256,
                    "renderer": RENDERER, "prompt_sha256": PROMPT_SHA256, "files": files})
        operation.knowledge_bytes = reads.byte_count
        operation.capacity(sum(len(raw) for raw in bodies.values()) + len(encode(profile).encode("utf-8")))
        reads.finish()
        operation.finish()
    return profile, bodies


def _names(workspace: CandidateWorkspace, files: list[dict[str, Any]]) -> None:
    root = _path(workspace.root, workspace.root / NAMESPACE, directory=True)
    expected = {file["skill_id"] for file in files}
    with os.scandir(root) as children:
        children = list(children)
    if len(children) != len(expected) or {child.name for child in children} != expected:
        raise ValueError("static skill namespace differs from the approved manifest")
    for child in children:
        folder = _path(workspace.root, Path(child.path), directory=True)
        with os.scandir(folder) as leaves:
            leaves = list(leaves)
        if len(leaves) != 1 or leaves[0].name != "SKILL.md":
            raise ValueError("static skill has unapproved support files")
        _path(workspace.root, Path(leaves[0].path))


class _ApprovedBackend(BackendProtocol):
    """Native SDK discovery reads an exact manifest, never a filesystem root."""

    def __init__(self, files: list[dict[str, Any]], bodies: dict[str, bytes], verify: Any = None):
        self._files = json.loads(encode({"files": files}))["files"]
        self._bodies = dict(bodies)
        self._verify = verify

    def _check(self) -> None:
        if self._verify is not None:
            self._verify()

    def ls(self, path: str) -> LsResult:
        if path != VIRTUAL_ROOT:
            raise StaticSkillError()
        self._check()
        return LsResult(entries=[{"path": VIRTUAL_ROOT + file["skill_id"], "is_dir": True}
                                 for file in self._files])

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        if (type(paths) is not list or len(paths) > MAX_STATIC_SKILL_ENTRIES or
                any(type(path) is not str or path not in self._bodies for path in paths)):
            raise StaticSkillError()
        self._check()
        return [FileDownloadResponse(path=path, content=self._bodies[path]) for path in paths]

    def _denied(self, *args: Any, **kwargs: Any) -> Any:
        raise StaticSkillError()

    read = grep = glob = write = edit = delete = upload_files = execute = _denied


def _native_catalog(backend: _ApprovedBackend) -> list[dict[str, Any]]:
    native = SkillsMiddleware(backend=backend, sources=[(VIRTUAL_ROOT, "Approved static")], system_prompt=None)
    update = native.before_agent(cast(SkillsState, {}), cast(Any, None), {})
    if update is None or update.get("skills_load_errors"):
        raise StaticSkillError()
    return cast(list[dict[str, Any]], update["skills_metadata"])


class StaticSkillService:
    """Read-only service reopened under a caller-supplied frozen identity."""

    def __init__(self, store: ApplicabilityStore, profile: dict[str, Any]):
        self._store = store
        self._profile_json = encode(normalize_static_skill_profile(profile))

    @property
    def profile(self) -> dict[str, Any]:
        return json.loads(self._profile_json)

    def _validate(self) -> dict[str, bytes]:
        try:
            profile = self.profile
            policy = StaticSkillPolicy.model_validate_json(encode({key: profile[key]
                                             for key in ("schema_version", "enabled", "entry_ids")}))
            expected, bodies = _compile(self._store, policy)
            if expected != profile:
                raise ValueError("static skill profile differs from approved content")
            workspace = self._store.workspace
            _names(workspace, profile["files"])
            reads = _ReadSet(workspace.root, max_read_bytes=2 * MAX_STATIC_SKILL_BYTES,
                             max_read_calls=2 * (MAX_STATIC_SKILL_ENTRIES + 1))
            raw = reads.read(workspace.root / PROFILE_PATH)
            if raw != self._profile_json.encode("utf-8"):
                raise ValueError("static skill profile original differs")
            for file in profile["files"]:
                raw = reads.read(workspace.root / file["path"], expected_hash=file["sha256"],
                                 expected_bytes=file["bytes"])
                if raw != bodies[file["virtual_path"]]:
                    raise ValueError("static skill body differs from approved renderer")
            reads.finish()
            _names(workspace, profile["files"])
            return bodies
        except _INVALID as exc:
            raise StaticSkillError() from exc

    def body(self, skill_id: str) -> str:
        if type(skill_id) is not str:
            raise StaticSkillError()
        matches = [file for file in self.profile["files"] if file["skill_id"] == skill_id]
        if len(matches) != 1:
            raise StaticSkillError()
        return self._validate()[matches[0]["virtual_path"]].decode("utf-8")

    def expected_metadata(self) -> list[dict[str, Any]]:
        return [file["metadata"] for file in self.profile["files"]]

    def catalog(self) -> list[dict[str, Any]]:
        self._validate()
        return self.expected_metadata()

    def middleware(self) -> FrozenSkillsMiddleware:
        return FrozenSkillsMiddleware(self)

    def expected_section(self) -> str | None:
        middleware = self.middleware()
        self._validate()
        if not self.profile["enabled"]:
            return None
        return STATIC_SKILL_PROMPT.format(skills_locations=middleware._format_skills_locations(),
                    skills_load_warnings="", skills_list=middleware._format_skills_list(
                        cast(list[SkillMetadata], self.expected_metadata())))


class FrozenSkillsMiddleware(SkillsMiddleware):
    """SDK loading and injection with strict frozen cache admission."""

    def __init__(self, service: StaticSkillService):
        self._service = service
        profile = service.profile
        bodies = {file["virtual_path"]: b"" for file in profile["files"]}
        # Exact originals replace these placeholders before SDK loading.
        backend = _ApprovedBackend(profile["files"], bodies, self._check_resources)
        super().__init__(backend=backend, sources=[(VIRTUAL_ROOT, "Approved static")],
                         system_prompt=STATIC_SKILL_PROMPT if profile["enabled"] else None)

    def _check_resources(self) -> None:
        backend = cast(_ApprovedBackend, self._backend)
        backend._bodies = self._service._validate()

    def _check_state(self, state: Any) -> None:
        if (state.get("skills_metadata") != self._service.expected_metadata() or
                state.get("skills_load_errors", []) != []):
            raise StaticSkillError()

    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        # Native catalog formatting otherwise assumes an unregistered read_file.
        return "\n".join(f"- **{skill['name']}**: {skill['description']}\n"
                         f"  -> read_skill(skill_id={json.dumps(skill['name'])})" for skill in skills)

    def before_agent(self, state: Any, runtime: Any, config: Any) -> Any:
        self._service._validate()
        update = super().before_agent(state, runtime, config)
        self._check_state(state if update is None else update)
        return update

    async def abefore_agent(self, state: Any, runtime: Any, config: Any) -> Any:
        return self.before_agent(state, runtime, config)

    def modify_request(self, request: Any) -> Any:
        self._service._validate()
        self._check_state(request.state)
        return super().modify_request(request)


def prepare_run_static_skills(store: ApplicabilityStore | CandidateWorkspace,
                              policy: StaticSkillPolicy | None) -> dict[str, Any] | None:
    """The only publication entry; validate everything before the first write."""
    try:
        workspace = _workspace(store)
        _absent(workspace)
        if policy is None:
            return None
        if not isinstance(store, ApplicabilityStore) or not isinstance(policy, StaticSkillPolicy):
            raise StaticSkillError()
        policy = _copy(policy)
        profile, bodies = _compile(store, policy)
        expected = [file["metadata"] for file in profile["files"]]
        if _native_catalog(_ApprovedBackend(profile["files"], bodies)) != expected:
            raise StaticSkillError()
        _absent(workspace)
        (workspace.root / NAMESPACE).mkdir()
        for file in profile["files"]:
            (workspace.root / NAMESPACE / file["skill_id"]).mkdir()
            store.io._publish(f"{NAMESPACE}/{file['skill_id']}", "SKILL.md", bodies[file["virtual_path"]])
        store.io._publish("", PROFILE_PATH, encode(profile).encode("utf-8"))
        read_run_static_skills(store, profile)
        return profile
    except _INVALID as exc:
        raise StaticSkillError() from exc


def read_run_static_skills(store: ApplicabilityStore | CandidateWorkspace,
                           profile: dict[str, Any] | None) -> StaticSkillService | None:
    """Pure reopening never grants approval from a local profile or cache."""
    try:
        workspace = _workspace(store)
        if profile is None:
            _absent(workspace)
            return None
        if not isinstance(store, ApplicabilityStore):
            raise StaticSkillError()
        service = StaticSkillService(store, profile)
        bodies = service._validate()
        if _native_catalog(_ApprovedBackend(service.profile["files"], bodies)) != service.expected_metadata():
            raise StaticSkillError()
        return service
    except _INVALID as exc:
        raise StaticSkillError() from exc
