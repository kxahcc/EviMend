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
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .applicability import ApplicabilityError, ApplicabilityStore
from .contracts import require_hash, require_path, require_unique
from .knowledge import KnowledgeError, _copy, _exists, _path, _ReadSet
from .knowledge_contracts import SnapshotPayload, SolverProjection, content_hash, encode
from .retrieval_contracts import MAX_INDEX_READ_BYTES, MAX_INDEX_READ_CALLS
from .skill_packages import NAME_TAG, package_metadata, projection_package
from .task import digest
from .workspace import CandidateWorkspace

PROFILE_PATH = "static-skills-profile.json"
NAMESPACE = "static-skills"
VIRTUAL_ROOT = "/approved-skills/"
RENDERER = "approved_procedure_v1"
STANDARD_RENDERER = "standard_skill_v1"
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
STANDARD_SKILL_PROMPT = """<dfx_static_skills>
Host-approved standard Skill packages
{skills_locations}
{skills_load_warnings}
{skills_list}
The catalog normally exposes names and descriptions only. Read a matching
Skill's complete original with read_skill(skill_id=its listed name) before
following it. Skills explicitly marked metadata.dfx-load=always include their
complete instructions above. Read referenced text assets only as needed with
read_skill(skill_id=its listed name, resource_path=the package-relative path).
These are host-curated guidance, not current evidence or application results.
Check the full Skill's prerequisites against this task and keep unknowns open.
Use only already authorized project tools; allowed-tools metadata and scripts
grant no additional file, shell, execution, or delegation permissions.
</dfx_static_skills>
"""
STANDARD_PROMPT_SHA256 = digest(STANDARD_SKILL_PROMPT.encode("utf-8"))
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


class _NativeMetadata(_Frozen):
    name: str
    description: str = Field(min_length=1, max_length=1024)
    path: str
    metadata: dict[str, str]
    license: str | None
    compatibility: str | None
    allowed_tools: tuple[str, ...]


class _SupportFile(_Frozen):
    resource_path: str
    path: str
    virtual_path: str
    sha256: str
    bytes: int = Field(ge=0, le=MAX_SKILL_FILE_BYTES)

    @model_validator(mode="after")
    def valid_resource(self) -> _SupportFile:
        require_path(self.resource_path)
        require_path(self.path)
        require_hash(self.sha256)
        if self.resource_path == "SKILL.md":
            raise ValueError("support resource cannot replace the Skill body")
        return self


class _File(_Frozen):
    skill_id: str
    entry_id: str
    content_sha256: str
    projection_sha256: str
    path: str
    virtual_path: str
    sha256: str
    bytes: int = Field(gt=0, le=MAX_SKILL_FILE_BYTES)
    metadata: _NativeMetadata
    support_files: tuple[_SupportFile, ...] | None = Field(default=None, max_length=63)
    always_loaded_body: str | None = None

    @model_validator(mode="after")
    def valid_file(self) -> _File:
        for key in (self.content_sha256, self.projection_sha256, self.sha256):
            require_hash(key)
        if (self.entry_id != "experience-" + self.content_sha256 or
                self.path != f"{NAMESPACE}/{self.skill_id}/SKILL.md" or
                self.virtual_path != f"{VIRTUAL_ROOT}{self.skill_id}/SKILL.md"):
            raise ValueError("static skill path or identity differs")
        meta = self.metadata
        if (meta.name != self.skill_id or meta.path != self.virtual_path or
                meta.description != meta.description.strip()):
            raise ValueError("static skill metadata differs from its original")
        # Source kind comes from the inventory, not author annotations that
        # can legitimately resemble the legacy renderer's metadata fields.
        if self.support_files is None:
            legacy = _Metadata.model_validate_json(encode(meta.model_dump(mode="json")))
            if (self.skill_id != "dfx-" + self.projection_sha256[:56] or self.support_files is not None or
                    self.always_loaded_body is not None or
                    legacy.metadata.content_sha256 != self.content_sha256 or
                    legacy.metadata.projection_sha256 != self.projection_sha256):
                raise ValueError("legacy static metadata differs from its original")
        else:
            from deepagents.middleware.skills import _validate_skill_name

            if not _validate_skill_name(self.skill_id, self.skill_id)[0] or self.support_files is None:
                raise ValueError("standard Skill identity or resources differ")
            require_unique([file.resource_path.casefold() for file in self.support_files])
            for file in self.support_files:
                if (file.path != f"{NAMESPACE}/{self.skill_id}/{file.resource_path}" or
                        file.virtual_path != f"{VIRTUAL_ROOT}{self.skill_id}/{file.resource_path}"):
                    raise ValueError("standard Skill support path differs")
            mode = meta.metadata.get("dfx-load", "on-demand")
            if mode not in {"always", "on-demand"}:
                raise ValueError("standard Skill loading mode differs")
            if mode == "always":
                if (self.always_loaded_body is None or digest(self.always_loaded_body.encode("utf-8")) != self.sha256 or
                        len(self.always_loaded_body.encode("utf-8")) != self.bytes):
                    raise ValueError("always-loaded Skill does not contain its exact original")
            elif self.always_loaded_body is not None:
                raise ValueError("on-demand Skill cannot inject a full body")
        return self


class _SharedProfile(_Frozen):
    schema_version: Literal[1]
    entry_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_STATIC_SKILL_ENTRIES)
    snapshot_sha256: str
    renderer: Literal["approved_procedure_v1", "standard_skill_v1"]
    prompt_sha256: str
    files: tuple[_File, ...] = Field(min_length=1, max_length=MAX_STATIC_SKILL_ENTRIES)

    @model_validator(mode="after")
    def valid_profile(self) -> _SharedProfile:
        if type(self.schema_version) is not int:
            raise ValueError("static skill schema must be an integer")
        _entries(self.entry_ids)
        require_hash(self.snapshot_sha256)
        if self.prompt_sha256 != (STANDARD_PROMPT_SHA256 if self.renderer == STANDARD_RENDERER else PROMPT_SHA256):
            raise ValueError("static skill prompt differs from its renderer")
        if tuple(file.entry_id for file in self.files) != self.entry_ids:
            raise ValueError("static skill files differ from selected entries")
        require_unique([file.skill_id for file in self.files])
        if sum(file.bytes + sum(resource.bytes for resource in file.support_files or ())
               for file in self.files) > MAX_STATIC_SKILL_BYTES:
            raise ValueError("static skill resources exceed capacity")
        return self


class _Profile(_SharedProfile):
    enabled: bool


def _normalize(value: dict[str, Any], cls: type[_SharedProfile]) -> dict[str, Any]:
    try:
        if type(value) is not dict:
            raise TypeError("static skill profile must be an explicit object")
        checked = cls.model_validate_json(encode(value)).model_dump(mode="json")
        # A new optional resource inventory must not alter legacy bytes or
        # frozen historical profile identities.
        for file in checked["files"]:
            for optional in ("support_files", "always_loaded_body"):
                if file[optional] is None:
                    del file[optional]
        if checked != value or type(value.get("schema_version")) is not int:
            raise ValueError("static skill profile must contain all canonical fields")
        raw = encode(checked).encode("utf-8")
        if (len(raw) > MAX_SKILL_FILE_BYTES or
                len(raw) + sum(file["bytes"] + sum(resource["bytes"] for resource in file.get("support_files", []))
                               for file in checked["files"]) > MAX_STATIC_SKILL_BYTES):
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
    package = projection_package(projection)
    if package is not None:
        name = guidance.tags[1].removeprefix(NAME_TAG)
        virtual = f"{VIRTUAL_ROOT}{name}/SKILL.md"
        metadata = package_metadata(package["SKILL.md"], name, virtual)
        if metadata["description"] != guidance.trigger:
            raise ValueError("standard Skill description differs from its frozen projection")
        raw = package["SKILL.md"].encode("utf-8")
        supports = []
        for path, text in package.items():
            if path == "SKILL.md":
                continue
            resource = text.encode("utf-8")
            supports.append({"resource_path": path, "path": f"{NAMESPACE}/{name}/{path}",
                "virtual_path": f"{VIRTUAL_ROOT}{name}/{path}", "sha256": digest(resource), "bytes": len(resource)})
        file = {"skill_id": name, "entry_id": entry_id, "content_sha256": content,
                "projection_sha256": key, "path": f"{NAMESPACE}/{name}/SKILL.md", "virtual_path": virtual,
                "sha256": digest(raw), "bytes": len(raw), "metadata": metadata, "support_files": supports}
        if metadata["metadata"].get("dfx-load", "on-demand") == "always":
            file["always_loaded_body"] = package["SKILL.md"]
        return file, raw
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
            package = projection_package(projections[entry_id])
            if package is not None:
                for resource in file["support_files"]:
                    bodies[resource["virtual_path"]] = package[resource["resource_path"]].encode("utf-8")
        standard = any("support_files" in file for file in files)
        profile = normalize_static_skill_profile({"schema_version": 1, "enabled": policy.enabled,
                    "entry_ids": list(policy.entry_ids), "snapshot_sha256": store.approval.snapshot_sha256,
                    "renderer": STANDARD_RENDERER if standard else RENDERER,
                    "prompt_sha256": STANDARD_PROMPT_SHA256 if standard else PROMPT_SHA256, "files": files})
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
        file = next(file for file in files if file["skill_id"] == child.name)
        expected_files = {file["path"], *(resource["path"] for resource in file.get("support_files", []))}
        expected_dirs = set()
        for path in expected_files:
            parent = Path(path).parent
            while parent != Path(NAMESPACE) / child.name:
                expected_dirs.add(parent.as_posix())
                parent = parent.parent
        actual_files, actual_dirs = set(), set()
        for parent, folders, leaves in os.walk(folder, followlinks=False):
            for name in folders:
                path = _path(workspace.root, Path(parent) / name, directory=True)
                actual_dirs.add(path.relative_to(workspace.root).as_posix())
            for name in leaves:
                path = _path(workspace.root, Path(parent) / name)
                actual_files.add(path.relative_to(workspace.root).as_posix())
        if actual_files != expected_files or actual_dirs != expected_dirs:
            raise ValueError("static skill has unapproved support files or directories")


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
                             max_read_calls=2 * (1 + sum(1 + len(file.get("support_files", []))
                                                       for file in profile["files"])))
            raw = reads.read(workspace.root / PROFILE_PATH)
            if raw != self._profile_json.encode("utf-8"):
                raise ValueError("static skill profile original differs")
            for file in profile["files"]:
                raw = reads.read(workspace.root / file["path"], expected_hash=file["sha256"],
                                 expected_bytes=file["bytes"])
                if raw != bodies[file["virtual_path"]]:
                    raise ValueError("static skill body differs from approved renderer")
                for resource in file.get("support_files", []):
                    raw = reads.read(workspace.root / resource["path"], expected_hash=resource["sha256"],
                                     expected_bytes=resource["bytes"])
                    if raw != bodies[resource["virtual_path"]]:
                        raise ValueError("static skill support file differs from approved original")
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

    def resource(self, skill_id: str, resource_path: str) -> str:
        """Read one exact frozen support file without granting script execution."""
        if type(skill_id) is not str or type(resource_path) is not str:
            raise StaticSkillError()
        matches = [resource for file in self.profile["files"] if file["skill_id"] == skill_id
                   for resource in file.get("support_files", []) if resource["resource_path"] == resource_path]
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
        self._validate()
        return static_skill_section(self.profile)


def _skill_list(skills: list[SkillMetadata], profile: dict[str, Any] | None = None) -> str:
    lines = []
    originals = ({file["skill_id"]: file["always_loaded_body"] for file in profile["files"]
                  if "always_loaded_body" in file} if profile is not None else {})
    for skill in skills:
        lines.append(f"- **{skill['name']}**: {skill['description']}\n"
                     f"  -> read_skill(skill_id={json.dumps(skill['name'])})")
        if skill["name"] in originals:
            lines.append("\nComplete always-loaded Skill original:\n" + originals[skill["name"]])
    return "\n".join(lines)


def static_skill_section(profile: dict[str, Any]) -> str | None:
    """Render the frozen native catalog before publication, without file access."""
    profile = normalize_static_skill_profile(profile)
    if not profile["enabled"]:
        return None
    native = SkillsMiddleware(backend=_ApprovedBackend(profile["files"], {}),
                              sources=[(VIRTUAL_ROOT, "Approved static")], system_prompt=None)
    prompt = STANDARD_SKILL_PROMPT if profile["renderer"] == STANDARD_RENDERER else STATIC_SKILL_PROMPT
    return prompt.format(skills_locations=native._format_skills_locations(),
        skills_load_warnings="", skills_list=_skill_list(
            cast(list[SkillMetadata], [file["metadata"] for file in profile["files"]]), profile))


class FrozenSkillsMiddleware(SkillsMiddleware):
    """SDK loading and injection with strict frozen cache admission."""

    def __init__(self, service: StaticSkillService):
        self._service = service
        profile = service.profile
        bodies = {file["virtual_path"]: b"" for file in profile["files"]}
        # Exact originals replace these placeholders before SDK loading.
        backend = _ApprovedBackend(profile["files"], bodies, self._check_resources)
        prompt = STANDARD_SKILL_PROMPT if profile["renderer"] == STANDARD_RENDERER else STATIC_SKILL_PROMPT
        super().__init__(backend=backend, sources=[(VIRTUAL_ROOT, "Approved static")],
                         system_prompt=prompt if profile["enabled"] else None)

    def _check_resources(self) -> None:
        backend = cast(_ApprovedBackend, self._backend)
        backend._bodies = self._service._validate()

    def _check_state(self, state: Any) -> None:
        if (state.get("skills_metadata") != self._service.expected_metadata() or
                state.get("skills_load_errors", []) != []):
            raise StaticSkillError()

    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        # Native catalog formatting otherwise assumes an unregistered read_file.
        return _skill_list(skills, self._service.profile)

    def before_agent(self, state: Any, runtime: Any, config: RunnableConfig) -> Any:
        self._service._validate()
        update = super().before_agent(state, runtime, config)
        self._check_state(state if update is None else update)
        return update

    async def abefore_agent(self, state: Any, runtime: Any, config: RunnableConfig) -> Any:
        return self.before_agent(state, runtime, config)

    def modify_request(self, request: Any) -> Any:
        self._service._validate()
        self._check_state(request.state)
        return super().modify_request(request)


def prepare_run_static_skills(store: ApplicabilityStore | CandidateWorkspace,
                              policy: StaticSkillPolicy | None, *, max_calls: int | None = None,
                              max_tool_calls: int | None = None) -> dict[str, Any] | None:
    """The only publication entry; validate everything before the first write."""
    try:
        workspace = _workspace(store)
        _absent(workspace)
        if (max_calls is None) != (max_tool_calls is None):
            raise StaticSkillError()
        if max_calls is not None and (type(max_calls) is not int or type(max_tool_calls) is not int or
                                      max_calls < 1 or max_tool_calls is None or max_tool_calls < 1):
            raise StaticSkillError()
        if policy is None:
            return None
        if not isinstance(store, ApplicabilityStore) or not isinstance(policy, StaticSkillPolicy):
            raise StaticSkillError()
        policy = _copy(policy)
        profile, bodies = _compile(store, policy)
        expected = [file["metadata"] for file in profile["files"]]
        if _native_catalog(_ApprovedBackend(profile["files"], bodies)) != expected:
            raise StaticSkillError()
        if max_calls is not None and max_tool_calls is not None:
            from .static_skill_handoff import preflight_static_skill_report

            preflight_static_skill_report(profile, static_skill_section(profile), max_calls, max_tool_calls)
        _absent(workspace)
        (workspace.root / NAMESPACE).mkdir()
        for file in profile["files"]:
            (workspace.root / NAMESPACE / file["skill_id"]).mkdir()
            store.io._publish(f"{NAMESPACE}/{file['skill_id']}", "SKILL.md", bodies[file["virtual_path"]])
            for resource in file.get("support_files", []):
                target = workspace.root / resource["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                store.io._publish(target.parent.relative_to(workspace.root).as_posix(), target.name,
                                  bodies[resource["virtual_path"]])
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
