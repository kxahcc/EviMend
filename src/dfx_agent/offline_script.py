"""Strict offline CLI scripts with immutable configuration and isolated mock response streams."""

from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from .actions import TOOL_ACTIONS
from .applicability_contracts import TaskKnowledgeBinding
from .applicability_handoff import ApplicabilityPolicy, normalize_applicability_profile
from .context_compaction import CompactionPolicy
from .contracts import require_path
from .knowledge import KnowledgeError, KnowledgeStore, _path
from .knowledge_retrieval import KnowledgeRetriever
from .knowledge_run import HostKnowledgeInput
from .model_provider import (
    AdapterTransportError,
    ChatCompletionsAdapter,
    ProviderSettings,
)
from .policies import PolicyFeatures
from .retrieval_contracts import MemoryQuery
from .static_skills import StaticSkillPolicy
from .task import verify_task
from .verification import FixedTextExecutor
from .working_context_plan import WorkingContextPolicy

MAX_SCRIPT_BYTES = 1_048_576
LIMIT_NAMES = frozenset({"max_calls", "max_known_tokens", "max_seconds", "max_candidates", "max_tool_calls"})
USAGE_NAMES = frozenset({"input_tokens", "output_tokens", "total_tokens"})
_FORBIDDEN_LIBRARY_PARTS = frozenset({"data_b", "archive", ".git", ".agents", ".codex"})
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul"} | {
    prefix + suffix for prefix in ("com", "lpt") for suffix in (*map(str, range(1, 10)), "¹", "²", "³")
}


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("offline script has duplicate JSON keys")
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError("offline script has a non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("offline script has a non-finite JSON number")
    return number


def _safe_path(path: Path) -> os.stat_result:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        info = current.lstat()
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("offline script path is linked")
    info = absolute.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("offline script must be an unlinked regular file")
    if info.st_size > MAX_SCRIPT_BYTES:
        raise ValueError("offline script exceeds 1 MiB")
    return info


def _limits(value: object) -> dict[str, int | float]:
    if not isinstance(value, dict) or set(value) - LIMIT_NAMES:
        raise ValueError("invalid offline script limits")
    for name, number in value.items():
        if name == "max_seconds":
            try:
                valid = type(number) in (int, float) and number > 0 and math.isfinite(number)
            except OverflowError:
                valid = False
        else:
            valid = type(number) is int and number > 0
        if not valid:
            raise ValueError("invalid offline script limits")
    return dict(value)


def _usage(value: object) -> None:
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != USAGE_NAMES or
            any(type(number) is not int or number < 0 for number in value.values()) or
            value["total_tokens"] != value["input_tokens"] + value["output_tokens"]):
        raise ValueError("invalid fixed-response usage")


def _compaction(value: object, *, fixed_mode: bool) -> None:
    if not isinstance(value, dict) or "policy" not in value or set(value) - {"policy", "responses"}:
        raise ValueError("invalid offline context compaction fields")
    try:
        CompactionPolicy.model_validate(value["policy"])
    except ValidationError:
        raise ValueError("invalid offline context compaction policy") from None
    if "responses" not in value:
        return
    if not fixed_mode:
        raise ValueError("mock context compaction responses must use the shared wire stream")
    responses = value["responses"]
    if not isinstance(responses, list):
        raise ValueError("fixed compaction responses must be a list")  # noqa: TRY004 -- Keep loader's validation contract.
    for item in responses:
        if (not isinstance(item, dict) or set(item) != {"text", "usage"} or
                not isinstance(item["text"], str)):
            raise ValueError("invalid fixed compaction response fields")
        try:
            item["text"].encode("utf-8")
        except UnicodeError:
            raise ValueError("invalid fixed compaction response text") from None
        _usage(item["usage"])


def _library_path(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid offline knowledge library path")
    try:
        require_path(value)
        for part in value.split("/"):
            if (part.casefold() in _FORBIDDEN_LIBRARY_PARTS or part.rstrip(". ") != part or
                    part.split(".", 1)[0].casefold() in _WINDOWS_DEVICES or
                    any(ord(character) < 32 or ord(character) == 127 or character in ':<>"|?*'
                        for character in part)):
                raise ValueError("unsupported library component")
    except ValueError:
        raise ValueError("invalid offline knowledge library path") from None
    return value


def _knowledge(value: object) -> None:
    if (not isinstance(value, dict) or not {"library_path", "approval"} <= set(value) or
            set(value) - {"library_path", "approval", "applicability_policy", "static_skill_policy"}):
        raise ValueError("invalid offline knowledge configuration")
    _library_path(value["library_path"])
    try:
        approval = TaskKnowledgeBinding.model_validate_json(_canonical(value["approval"]))
        if _canonical(approval.model_dump(mode="json")) != _canonical(value["approval"]):
            raise ValueError("approval must contain every canonical field")
        if "applicability_policy" in value:
            normalize_applicability_profile(value["applicability_policy"])
        if "static_skill_policy" in value:
            if "applicability_policy" not in value:
                raise ValueError("static procedures require explicit applicability configuration")
            static = StaticSkillPolicy.model_validate_json(_canonical(value["static_skill_policy"]))
            if _canonical(static.model_dump(mode="json")) != _canonical(value["static_skill_policy"]):
                raise ValueError("static policy must contain every canonical field")
    except (ValidationError, ValueError, TypeError):
        raise ValueError("invalid offline knowledge approval or policies") from None


def _validate(value: object) -> Literal["fixed_response", "mock_chat_completions"]:
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] not in (1, 2, 3, 4, 5, 6):
        raise ValueError("unsupported offline script schema")
    fixed_mode = value["schema_version"] in (1, 3, 5)
    required = {"schema_version", "expected", "responses" if fixed_mode else "model"}
    if value["schema_version"] in (3, 4, 5, 6):
        required.add("working_context")
    if value["schema_version"] in (5, 6):
        required.add("knowledge")
    if not required <= set(value) or set(value) - required - {"policy_features", "limits", "context_compaction"}:
        raise ValueError("invalid offline script fields")
    expected = value["expected"]
    if (not isinstance(expected, dict) or set(expected) != {"path", "text"} or
            not isinstance(expected["path"], str) or not isinstance(expected["text"], str)):
        raise ValueError("invalid synthetic expectation")
    try:
        require_path(expected["path"])
        if any(ord(character) < 32 or ord(character) == 127 for character in expected["path"]):
            raise ValueError("control character")
    except ValueError:
        raise ValueError("invalid synthetic expectation path") from None
    _limits(value.get("limits", {}))
    if "knowledge" in value:
        _knowledge(value["knowledge"])
    if "context_compaction" in value:
        _compaction(value["context_compaction"], fixed_mode=fixed_mode)
    if "working_context" in value:
        try:
            WorkingContextPolicy.model_validate(value["working_context"])
        except ValidationError:
            raise ValueError("invalid offline working context policy") from None
    try:
        PolicyFeatures.model_validate(value.get("policy_features", {}))
    except ValidationError:
        raise ValueError("invalid offline policy features") from None
    if fixed_mode:
        responses = value["responses"]
        if not isinstance(responses, list):
            raise ValueError("fixed responses must be a list")
        for item in responses:
            if not isinstance(item, dict) or set(item) != {"tool", "args", "usage"}:
                raise ValueError("invalid fixed-response fields")
            name, arguments = item["tool"], item["args"]
            if name is not None and (not isinstance(name, str) or name not in TOOL_ACTIONS):
                raise ValueError("invalid fixed-response tool")
            if not isinstance(arguments, dict) or (name is None and arguments):
                raise ValueError("invalid fixed-response arguments")
            _usage(item["usage"])
        return "fixed_response"
    model = value["model"]
    if (not isinstance(model, dict) or set(model) != {"mode", "settings", "responses"} or
            model["mode"] != "mock_chat_completions"):
        raise ValueError("invalid offline model configuration")
    try:
        ProviderSettings.model_validate(model["settings"])
    except ValidationError:
        raise ValueError("invalid offline model settings") from None
    if not isinstance(model["responses"], list) or any(not isinstance(item, dict) for item in model["responses"]):
        raise ValueError("mock wire responses must be a list of objects")
    return "mock_chat_completions"


@dataclass(frozen=True, slots=True)
class OfflineScript:
    """Only immutable JSON is stored; every returned model, message and option is fresh."""

    mode: Literal["fixed_response", "mock_chat_completions"]
    _document: str = field(repr=False)

    @property
    def schema_version(self) -> int:
        return json.loads(self._document)["schema_version"]

    def knowledge_approval(self) -> TaskKnowledgeBinding | None:
        value = json.loads(self._document).get("knowledge")
        return None if value is None else TaskKnowledgeBinding.model_validate_json(_canonical(value["approval"]))

    def applicability_policy(self) -> ApplicabilityPolicy | None:
        value = json.loads(self._document).get("knowledge", {}).get("applicability_policy")
        return None if value is None else ApplicabilityPolicy.model_validate_json(_canonical(value))

    def static_skill_policy(self) -> StaticSkillPolicy | None:
        value = json.loads(self._document).get("knowledge", {}).get("static_skill_policy")
        return None if value is None else StaticSkillPolicy.model_validate_json(_canonical(value))

    def validate_knowledge_paths(self, project_root: Path | None, task: Path, work: Path) -> Path | None:
        """Check only lexical authority; resume does not stat an obsolete library."""
        value = json.loads(self._document).get("knowledge")
        if value is None:
            return None
        if project_root is None or not project_root.is_absolute() or ".." in project_root.parts:
            raise ValueError("offline knowledge requires an explicit canonical project root")
        library = project_root / _library_path(value["library_path"])
        library.relative_to(project_root)
        for other in (task.absolute(), work.absolute()):
            if ".." in other.parts or library.is_relative_to(other) or other.is_relative_to(library):
                raise ValueError("offline knowledge must be separate from task and work directories")
        return library

    def knowledge_input(self, project_root: Path | None, task: Path, work: Path, run_id: str) -> HostKnowledgeInput | None:
        """Authenticate the approved original snapshot/index without creating anything."""
        library = self.validate_knowledge_paths(project_root, task, work)
        approval = self.knowledge_approval()
        if library is None or approval is None:
            return None
        try:
            info = verify_task(task)
            if (approval.task_id != info["task_id"] or approval.run_id != run_id or
                    approval.source_snapshot_sha256 != info["source_snapshot_sha256"] or
                    approval.crash_sha256 != info["crash_sha256"]):
                raise ValueError("knowledge approval differs from current task")
            assert project_root is not None
            _path(project_root, library, directory=True)
            retriever = KnowledgeRetriever(KnowledgeStore(library))
            retriever.query(MemoryQuery(snapshot_sha256=approval.snapshot_sha256, scope=approval.scope, limit=1))
            return HostKnowledgeInput(retriever, approval)
        except (OSError, ValueError, TypeError, KeyError, KnowledgeError):
            raise ValueError("offline knowledge originals, index or approval rejected") from None

    def check_resume_knowledge(self, session: dict[str, Any]) -> None:
        """Compare saved declarations with fresh host inputs, never deriving approval."""
        approval = self.knowledge_approval()
        profile = session.get("knowledge_profile")
        if ((approval is None) != (profile is None) or
                (approval is not None and (not isinstance(profile, dict) or
                 profile.get("approval") != approval.model_dump(mode="json")))):
            raise ValueError("resume offline knowledge approval differs")
        applicability = self.applicability_policy()
        expected = None if applicability is None else applicability.model_dump(mode="json")
        if session.get("applicability_profile") != expected:
            raise ValueError("resume offline applicability configuration differs")
        static = self.static_skill_policy()
        saved = session.get("static_skill_profile")
        if ((static is None) != (saved is None) or
                (static is not None and (not isinstance(saved, dict) or
                 saved.get("enabled") != static.enabled or saved.get("entry_ids") != list(static.entry_ids)))):
            raise ValueError("resume offline static skill configuration differs")

    def executor(self) -> FixedTextExecutor:
        expected = json.loads(self._document)["expected"]
        return FixedTextExecutor(expected["path"], expected["text"])

    def features(self) -> PolicyFeatures:
        return PolicyFeatures.model_validate(json.loads(self._document).get("policy_features", {}))

    def options(self) -> dict[str, int | float]:
        return json.loads(self._document).get("limits", {})

    def compaction_policy(self) -> CompactionPolicy:
        value = json.loads(self._document).get("context_compaction", {})
        return CompactionPolicy.model_validate(value.get("policy", {}))

    def working_context_policy(self) -> WorkingContextPolicy:
        return WorkingContextPolicy.model_validate(json.loads(self._document).get("working_context", {}))

    def compaction_responses(self) -> list[AIMessage]:
        if self.mode != "fixed_response":
            return []
        value = json.loads(self._document).get("context_compaction", {})
        return [AIMessage(content=item["text"], usage_metadata=item["usage"])
                for item in value.get("responses", [])]

    def fixed_responses(self) -> list[AIMessage]:
        if self.mode != "fixed_response":
            raise ValueError("offline script mode does not provide fixed responses")
        found = []
        for index, item in enumerate(json.loads(self._document)["responses"], start=1):
            calls = [] if item["tool"] is None else [{"name": item["tool"], "args": item["args"], "id": f"call-{index}"}]
            found.append(AIMessage(content="done" if not calls else "", tool_calls=calls, usage_metadata=item["usage"]))
        return found

    def adapter(self, *, response_offset: int = 0) -> ChatCompletionsAdapter:
        if self.mode != "mock_chat_completions":
            raise ValueError("offline script mode does not provide a mock adapter")
        if type(response_offset) is not int or response_offset < 0:
            raise ValueError("mock response offset must be a nonnegative integer")
        model = json.loads(self._document)["model"]
        settings = ProviderSettings.model_validate(model["settings"])
        responses = iter(tuple(_canonical(item).encode("utf-8") for item in model["responses"][response_offset:]))

        def handle(request: httpx.Request) -> httpx.Response:
            try:
                body = next(responses)
            except StopIteration:
                raise AdapterTransportError("mock_responses_exhausted") from None
            return httpx.Response(200, content=body, headers={"Content-Type": "application/json"})

        return ChatCompletionsAdapter(settings, httpx.MockTransport(handle))


def load_offline_script(path: Path) -> OfflineScript:
    """Load bounded scripts; 3/4 add context and 5/6 add host-approved knowledge."""
    before = _safe_path(path)
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(MAX_SCRIPT_BYTES + 1)
    after = _safe_path(path)
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
            (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) or
            (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ValueError("offline script changed while loading")
    if len(raw) > MAX_SCRIPT_BYTES:
        raise ValueError("offline script exceeds 1 MiB")
    try:
        value: Any = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                                parse_constant=_invalid_constant, parse_float=_finite_float)
        document = _canonical(value)
        document.encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid offline script JSON") from None
    mode = _validate(value)
    return OfflineScript(mode=mode, _document=document)
