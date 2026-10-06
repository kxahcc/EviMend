"""Explicit startup authority for immutable Skill versions, separate from v1 knowledge."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import require_hash, require_identifiers, require_unique
from .knowledge_contracts import PermissionScope, encode
from .model_calls import canonical, sha256
from .static_skills import MAX_STATIC_SKILL_ENTRIES, _Frozen
from .task import CaseSelection


class VersionSkillGrant(_Frozen):
    version_id: str
    version_approval: dict[str, Any]
    selection: dict[str, Any] | None = None
    publication_approval: dict[str, Any] | None = None

    @model_validator(mode="after")
    def valid(self) -> VersionSkillGrant:
        from .skill_evolution_contracts import parse_version_approval
        from .skill_transition_contracts import (
            parse_publication_approval,
            parse_selection,
        )

        require_hash(self.version_id.removeprefix("skill-version-"))
        if not self.version_id.startswith("skill-version-"):
            raise ValueError("startup requires an immutable version identity")
        approved = parse_version_approval(self.version_approval)
        if approved.model_dump(mode="json") != self.version_approval:
            raise ValueError("startup requires complete original version approval")
        if (self.selection is None) != (self.publication_approval is None):
            raise ValueError("deployment requires selection and original publication approval")
        if self.selection is not None:
            if self.publication_approval is None:
                raise ValueError("deployment is missing original publication approval")
            selection = parse_selection(self.selection)
            publication = parse_publication_approval(self.publication_approval)
            if (selection.model_dump(mode="json") != self.selection or
                    publication.model_dump(mode="json") != self.publication_approval or
                    selection.version_id != self.version_id or publication.version_id != self.version_id or
                    selection.alias != publication.alias):
                raise ValueError("deployment selection and publication differ")
        return self


class TaskVersionSkillBinding(_Frozen):
    schema_version: Literal[1] = 1
    approval_id: str
    task_id: str
    run_id: str
    source_snapshot_sha256: str
    crash_sha256: str
    case_selection: CaseSelection
    scope: PermissionScope
    purpose: Literal["candidate_validation", "deployment"]
    grants: tuple[VersionSkillGrant, ...] = Field(min_length=1, max_length=MAX_STATIC_SKILL_ENTRIES)
    validation_spec_sha256: str | None = None
    validation_plan_json: str | None = None
    validation_plan_approval: dict[str, Any] | None = None
    validation_case_id: str | None = None
    validation_role: Literal["old", "new"] | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("version startup requires integer schema 1")
        return value

    @model_validator(mode="after")
    def valid(self) -> TaskVersionSkillBinding:
        require_identifiers([self.approval_id, self.task_id, self.run_id])
        require_hash(self.source_snapshot_sha256)
        require_hash(self.crash_sha256)
        if self.case_selection.task_id != self.task_id:
            raise ValueError("version Skill approval names another case")
        keys = [grant.version_id for grant in self.grants]
        require_unique(keys)
        if keys != sorted(keys):
            raise ValueError("startup version grants must be sorted")
        if self.purpose == "candidate_validation":
            if (self.scope.split != "candidate_validation" or self.validation_spec_sha256 is None or
                    any(grant.selection is not None for grant in self.grants)):
                raise ValueError("validation resources cannot use deployment authority")
            require_hash(self.validation_spec_sha256)
            from .skill_evolution_contracts import (
                parse_plan,
                parse_plan_payload,
                parse_validation_approval,
            )

            if (self.validation_plan_json is None or self.validation_plan_approval is None or
                    self.validation_case_id is None or self.validation_role is None):
                raise ValueError("validation startup needs its exact prospective plan and role")
            plan = parse_plan(self.validation_plan_json)
            payload = parse_plan_payload(plan.payload_json)
            approved = parse_validation_approval(self.validation_plan_approval)
            if (payload.approval != approved or approved.spec_sha256 != self.validation_spec_sha256 or
                    self.validation_case_id not in {pair.case_id for pair in payload.spec.pairs}):
                raise ValueError("validation startup differs from its prospective approval")
        elif (any(value is not None for value in (self.validation_spec_sha256, self.validation_plan_json,
                self.validation_plan_approval, self.validation_case_id, self.validation_role)) or
                any(grant.selection is None for grant in self.grants)):
            raise ValueError("deployment requires published frozen selections")
        return self


def normalize_version_skill_profile(value: dict[str, Any]) -> dict[str, Any]:
    """Strict complete profile validation; original source authentication is separate."""
    if type(value) is not dict or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("version Skill profile needs explicit schema 1")
    expected = {"schema_version", "source_type", "enabled", "approval", "snapshot_sha256", "renderer",
                "prompt_sha256", "resources", "files", "source_files", "source_directories", "profile_sha256"}
    if set(value) != expected or value["source_type"] != "approved_versions" or type(value["enabled"]) is not bool:
        raise ValueError("version Skill profile fields differ")
    approval = TaskVersionSkillBinding.model_validate_json(encode(value["approval"]))
    if approval.model_dump(mode="json") != value["approval"]:
        raise ValueError("version Skill task approval is not complete")
    from .version_skills import PROMPT_SHA256, RENDERER

    if value["renderer"] != RENDERER or value["prompt_sha256"] != PROMPT_SHA256:
        raise ValueError("version Skill renderer differs")
    resources, sources = value["resources"], value["source_files"]
    if (type(resources) is not list or not resources or resources != value["files"] or
            [item["version_id"] for item in resources] != [grant.version_id for grant in approval.grants] or
            type(sources) is not list or not sources):
        raise ValueError("version Skill source or resource selection differs")
    require_unique([item["skill_id"] for item in resources])
    require_unique([item["path"].casefold() for item in sources])
    if sources != sorted(sources, key=lambda item: item["path"]):
        raise ValueError("version source inventory must be sorted")
    from .contracts import require_path

    for item in sources:
        if set(item) != {"path", "sha256", "bytes"}:
            raise ValueError("version source inventory fields differ")
        require_path(item["path"])
        require_hash(item["sha256"])
        if not item["path"].startswith("version-skill-sources/") or type(item["bytes"]) is not int or item["bytes"] < 0:
            raise ValueError("version source inventory is outside its namespace")
    require_hash(value["snapshot_sha256"])
    require_hash(value["profile_sha256"])
    directories = value["source_directories"]
    if type(directories) is not list or directories != sorted(set(directories)):
        raise ValueError("version source directories must be complete and sorted")
    for directory in directories:
        require_path(directory)
        if not directory.startswith("version-skill-sources/"):
            raise ValueError("version source directory is outside its namespace")
    if value["snapshot_sha256"] != sha256(canonical({"resources": resources, "source_files": sources,
                                                   "source_directories": directories})):
        raise ValueError("version source closure identity differs")
    identity = {key: item for key, item in value.items() if key != "profile_sha256"}
    if value["profile_sha256"] != sha256(canonical(identity)):
        raise ValueError("version Skill complete profile identity differs")
    return value


def shared_version_skill_profile(value: dict[str, Any]) -> dict[str, Any]:
    """Shared originals omit per-task approval, prospective plan and injection switch."""
    profile = normalize_version_skill_profile(value)
    return {key: profile[key] for key in ("schema_version", "source_type", "snapshot_sha256", "renderer",
                                        "prompt_sha256", "resources", "files", "source_files", "source_directories")}


def normalize_shared_version_skill_profile(value: dict[str, Any]) -> dict[str, Any]:
    from .version_skills import PROMPT_SHA256, RENDERER

    if (type(value) is not dict or set(value) != {"schema_version", "source_type", "snapshot_sha256", "renderer",
            "prompt_sha256", "resources", "files", "source_files", "source_directories"} or
            type(value["schema_version"]) is not int or
            value["schema_version"] != 1 or value["source_type"] != "approved_versions" or
            value["renderer"] != RENDERER or value["prompt_sha256"] != PROMPT_SHA256 or
            value["resources"] != value["files"] or not value["resources"] or
            value["snapshot_sha256"] != sha256(canonical({"resources": value["resources"],
                "source_files": value["source_files"], "source_directories": value["source_directories"]}))):
        raise ValueError("shared version Skill identity differs")
    return value
