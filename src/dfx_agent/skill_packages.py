"""Standard Skill packages bridged to the existing immutable knowledge store.

Authors edit ordinary SKILL.md and adjacent text resources. The bridge keeps
their exact text; it does not parse domain headings or invent a Skill schema.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from deepagents.middleware.skills import _parse_skill_metadata, _validate_skill_name

from .contracts import require_path, require_unique
from .knowledge import _path, _ReadSet
from .knowledge_contracts import (
    ExperienceContent,
    ExperiencePremise,
    ProcedureStep,
    SolverProjection,
)

PACKAGE_TAG = "standard-skill-v1"
FILE_TAG = "skill-file:"
NAME_TAG = "skill-name:"
MAX_PACKAGE_FILES = 64
MAX_PACKAGE_BYTES = 131_072
TEXT_PREFIX = "Skill source text:\n"
CHUNK_LENGTH = 4096 - len(TEXT_PREFIX)


def package_metadata(body: str, directory: str, path: str) -> dict[str, Any]:
    """Reuse the locked SDK parser; reject invalid names instead of truncating."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", body, re.DOTALL)
    if match is None:
        raise ValueError("standard Skill must start with YAML frontmatter")
    try:
        front = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError("standard Skill YAML frontmatter is invalid") from exc
    if not isinstance(front, dict):
        raise ValueError("standard Skill frontmatter must be a mapping")  # noqa: TRY004 -- Invalid document metadata.
    for field, bound in (("name", 64), ("description", 1024)):
        if not isinstance(front.get(field), str) or not front[field].strip() or len(front[field]) > bound:
            raise ValueError("standard Skill required metadata is invalid or too long")
    for field, bound in (("license", 4096), ("compatibility", 500)):
        if field in front and (not isinstance(front[field], str) or len(front[field]) > bound):
            raise ValueError("standard Skill optional metadata is invalid or too long")
    annotations = front.get("metadata", {})
    if not isinstance(annotations, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                              for key, value in annotations.items()):
        raise ValueError("standard Skill metadata must contain string keys and values")
    if annotations.get("dfx-load", "on-demand") not in {"always", "on-demand"}:
        raise ValueError("standard Skill dfx-load must be always or on-demand")
    tools = front.get("allowed-tools")
    if tools is not None and (not isinstance(tools, str) and
            (not isinstance(tools, list) or any(not isinstance(tool, str) for tool in tools))):
        raise ValueError("standard Skill allowed-tools must be a string or a list of strings")
    metadata = _parse_skill_metadata(body, path, directory)
    if metadata is None or not _validate_skill_name(metadata["name"], directory)[0]:
        raise ValueError("invalid standard Skill frontmatter or directory name")
    return dict(metadata)


def package_projection(directory: Path) -> SolverProjection:
    """Freeze one ordinary package, including read-only adjacent text assets."""
    directory = directory.absolute()
    _path(directory.parent, directory, directory=True)
    paths: list[Path] = []
    for parent, folders, leaves in os.walk(directory, followlinks=False):
        for name in folders:
            _path(directory, Path(parent) / name, directory=True)
        for name in leaves:
            paths.append(_path(directory, Path(parent) / name))
    if len(paths) > MAX_PACKAGE_FILES or directory / "SKILL.md" not in paths:
        raise ValueError("standard Skill package has no SKILL.md or too many files")
    paths.sort(key=lambda value: (value.name != "SKILL.md" or value.parent != directory,
                                  value.relative_to(directory).as_posix()))
    files: dict[str, str] = {}
    total = 0
    reads = _ReadSet(directory, max_read_bytes=MAX_PACKAGE_BYTES, max_read_calls=MAX_PACKAGE_FILES)
    for path in paths:
        relative = path.relative_to(directory).as_posix()
        require_path(relative)
        if len(FILE_TAG + relative) > 128:
            raise ValueError("Skill resource path exceeds the knowledge bridge bound")
        raw = reads.read(path)
        total += len(raw)
        if total > MAX_PACKAGE_BYTES:
            raise ValueError("standard Skill package exceeds its byte bound")
        # Explicit UTF-8 resources avoid lossy conversion and accidental binary
        # instructions; scripts are stored as text, never executed by loading.
        files[relative] = raw.decode("utf-8")
        if "\x00" in files[relative] or relative == "SKILL.md" and not files[relative].strip():
            raise ValueError("standard Skill package requires a nonempty Skill body and text resources")
    reads.finish()
    require_unique([path.casefold() for path in files])
    metadata = package_metadata(files["SKILL.md"], directory.name, "/approved-skills/" + directory.name + "/SKILL.md")
    steps = tuple(ProcedureStep(step_id=f"file-{index:03d}-part-{part:03d}",
        instruction=TEXT_PREFIX + text[offset:offset + CHUNK_LENGTH], branches=())
        for index, text in enumerate(files.values())
        for part, offset in enumerate(range(0, max(1, len(text)), CHUNK_LENGTH)))
    guidance = ExperienceContent(tags=(PACKAGE_TAG, NAME_TAG + directory.name, *(FILE_TAG + path for path in files)),
        trigger=metadata["description"],
        premises=(ExperiencePremise(premise_id="current-applicability",
            statement="Use this procedure only after checking its applicability against the current task.",
            unknown_question="Which current source or log observations establish the Skill's prerequisites?"),),
        procedure=steps,
        preserves=("Preserve current business behavior and all required independent checks.",),
        counterexamples=("A catalog match or a Skill read alone does not establish the current root cause.",),
        exit_conditions=("The actual application checks decide repair acceptance; Skill loading does not.",))
    return SolverProjection(guidance=guidance, derivation_authority="host_curated", validation_scopes=())


def projection_package(projection: SolverProjection) -> dict[str, str] | None:
    """Recover exact approved source text without interpreting its headings."""
    guidance = projection.guidance
    if not guidance.tags or guidance.tags[0] != PACKAGE_TAG:
        return None
    paths = [tag.removeprefix(FILE_TAG) for tag in guidance.tags[2:] if tag.startswith(FILE_TAG)]
    if (len(paths) != len(guidance.tags) - 2 or not paths or paths[0] != "SKILL.md" or
            not guidance.tags[1].startswith(NAME_TAG)):
        raise ValueError("standard Skill package bridge differs")
    files: dict[str, str] = {}
    used = set()
    for index, path in enumerate(paths):
        require_path(path)
        parts = [step for step in guidance.procedure if step.step_id.startswith(f"file-{index:03d}-part-")]
        if not parts or [step.step_id for step in parts] != [f"file-{index:03d}-part-{part:03d}"
                                                          for part in range(len(parts))]:
            raise ValueError("standard Skill text chunks differ")
        if any(step.branches or not step.instruction.startswith(TEXT_PREFIX) for step in parts):
            raise ValueError("standard Skill chunks cannot carry synthesized branches")
        used.update(step.step_id for step in parts)
        files[path] = "".join(step.instruction.removeprefix(TEXT_PREFIX) for step in parts)
    if len(used) != len(guidance.procedure) or sum(len(text.encode("utf-8")) for text in files.values()) > MAX_PACKAGE_BYTES:
        raise ValueError("standard Skill package bridge has unaccounted text")
    return files
