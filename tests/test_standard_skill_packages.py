"""Standard package discovery, exact native originals, and explicit loading modes."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from test_agent_loop import task_snapshot

from dfx_agent.domain_skills import prepare_domain_knowledge
from dfx_agent.knowledge_run import prepare_run_knowledge, read_run_knowledge
from dfx_agent.skill_packages import (
    package_metadata,
    package_projection,
    projection_package,
)
from dfx_agent.static_skills import (
    StaticSkillError,
    StaticSkillPolicy,
    prepare_run_static_skills,
    read_run_static_skills,
    shared_static_skill_profile,
)
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.workspace import CandidateWorkspace


@pytest.fixture
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("sp")


def package(root: Path, name: str = "external-check", *, mode: str = "on-demand") -> Path:
    folder = root / "skill" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Check a missing runtime prerequisite.\n"
        f"metadata:\n  dfx-load: {mode}\nallowed-tools: execute read_file\n---\n"
        "# External procedure\n\nRead [the reference](references/guide.md) when the failure concerns ordering.\n"
        "Inspect current evidence before using this historical procedure.\n", encoding="utf-8")
    (folder / "references").mkdir()
    (folder / "references/guide.md").write_bytes(b"Reference-only ordering example.\n")
    (folder / "scripts").mkdir()
    (folder / "scripts/check.py").write_bytes(b"raise RuntimeError('must not execute on Skill loading')\n")
    return folder


def prepare(root: Path, *, enabled: bool = True):
    task = task_snapshot(root)
    host, identifiers = prepare_domain_knowledge(root / "skill", root / "lib", task, "run1")
    workspace = CandidateWorkspace(task, root / "r", "run1")
    ledger = CheckLedger(workspace, FixedTextExecutor("src/main.ets", "value=good\n"))
    approved = prepare_run_knowledge(workspace, task, host, ledger=ledger)
    store = read_run_knowledge(workspace, task, approved, ledger=ledger)
    assert store is not None
    policy = StaticSkillPolicy(enabled=enabled, entry_ids=identifiers)
    profile = prepare_run_static_skills(store, policy)
    assert profile is not None
    service = read_run_static_skills(store, profile)
    assert service is not None
    return workspace, store, profile, service


def test_external_package_is_discovered_with_exact_body_and_assets(root: Path) -> None:
    source = package(root)
    original = (source / "SKILL.md").read_bytes()
    # Root notes and directories without a SKILL.md do not become procedures.
    (root / "skill/notes").mkdir()
    (root / "skill/README.txt").write_text("Authoring notes", encoding="utf-8")
    workspace, store, profile, service = prepare(root)
    assert profile["renderer"] == "standard_skill_v1"
    assert len(profile["files"]) == 1
    file = profile["files"][0]
    assert file["skill_id"] == "external-check"
    assert file["metadata"]["allowed_tools"] == ["execute", "read_file"]
    assert [item["resource_path"] for item in file["support_files"]] == ["references/guide.md", "scripts/check.py"]
    assert service.body("external-check").encode("utf-8") == original
    assert service.resource("external-check", "references/guide.md") == "Reference-only ordering example.\n"
    assert "must not execute" in service.resource("external-check", "scripts/check.py")
    section = service.expected_section()
    assert section is not None and "external-check" in section
    assert "# External procedure" not in section and "Reference-only ordering example" not in section
    native = service.middleware().before_agent({}, None, {})
    assert native["skills_metadata"] == service.expected_metadata()
    assert native["skills_load_errors"] == []
    assert (workspace.root / file["path"]).read_bytes() == original
    # Frozen package reads survive removal of the authoring library.
    shutil.rmtree(root / "skill")
    restored = read_run_static_skills(store, profile)
    assert restored is not None and restored.body("external-check").encode("utf-8") == original
    assert restored.resource("external-check", "references/guide.md") == "Reference-only ordering example.\n"


@pytest.mark.parametrize("path", ["../SKILL.md", "SKILL.md", "references/missing.md", "/outside.md"])
def test_support_reads_are_exact_manifest_members(root: Path, path: str) -> None:
    package(root)
    _, _, _, service = prepare(root)
    with pytest.raises(StaticSkillError):
        service.resource("external-check", path)


def test_tampered_support_original_is_rejected(root: Path) -> None:
    package(root)
    workspace, _, profile, service = prepare(root)
    asset = profile["files"][0]["support_files"][0]
    (workspace.root / asset["path"]).write_text("Changed content", encoding="utf-8")
    with pytest.raises(StaticSkillError):
        service.body("external-check")


def test_always_loading_is_explicit_and_disabled_catalog_injects_nothing(root: Path) -> None:
    on = package(root / "on", mode="always")
    package(root / "off", mode="always")
    _, _, on_profile, on_service = prepare(root / "on")
    _, _, off_profile, off_service = prepare(root / "off", enabled=False)
    body = (on / "SKILL.md").read_bytes().decode("utf-8")
    assert on_profile["files"][0]["always_loaded_body"] == body
    assert body in (on_service.expected_section() or "")
    assert "Reference-only ordering example" not in (on_service.expected_section() or "")
    assert off_service.expected_section() is None
    assert off_service.body("external-check") == body
    assert shared_static_skill_profile(on_profile) == shared_static_skill_profile(off_profile)


@pytest.mark.parametrize("front", [
    "name: invalid_name\ndescription: A description.",
    "name: external-check\ndescription: " + "x" * 1025,
    "name: external-check\ndescription: A description.\ncompatibility: " + "x" * 501,
    "name: external-check\ndescription: A description.\nmetadata:\n  dfx-load: sometimes",
    "name: external-check\ndescription: [not, text]",
    "name: external-check\ndescription: A description.\nmetadata: [not, a-map]",
    "name: external-check\ndescription: A description.\nmetadata:\n  retries: 3",
    "name: [broken",
])
def test_invalid_native_frontmatter_is_rejected_without_sdk_truncation(front: str) -> None:
    with pytest.raises(ValueError):
        package_metadata("---\n" + front + "\n---\n# Procedure\n", "external-check", "/example/SKILL.md")


def test_exact_text_bridge_preserves_long_and_whitespace_chunks(root: Path) -> None:
    source = package(root)
    path = source / "SKILL.md"
    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write("long instruction " * 600 + "\n" * 5000)
    projection = package_projection(source)
    files = projection_package(projection)
    assert files is not None
    assert files["SKILL.md"].encode("utf-8") == path.read_bytes()
    assert len(projection.guidance.procedure) > 3


def test_invalid_package_does_not_publish_a_partial_knowledge_library(root: Path) -> None:
    source = package(root)
    (source / "SKILL.md").write_text("# Missing required frontmatter\n", encoding="utf-8")
    task = task_snapshot(root)
    with pytest.raises(ValueError):
        prepare_domain_knowledge(root / "skill", root / "lib", task, "run1")
    assert not (root / "lib").exists()


def test_standard_author_annotations_do_not_change_the_source_kind(root: Path) -> None:
    source = package(root)
    # Arbitrary standard metadata can share the old renderer's field names.
    body = ("---\nname: external-check\ndescription: Check a missing prerequisite.\nmetadata:\n"
            "  renderer: approved_procedure_v1\n  derivation_authority: host_curated\n"
            "  epistemic_status: historical_guidance_not_current_evidence\n"
            "  content_sha256: " + "a" * 64 + "\n  projection_sha256: " + "b" * 64 +
            "\n  application_verification: not_run\n---\n# Standard external procedure\n")
    (source / "SKILL.md").write_bytes(body.encode("utf-8"))
    _, _, profile, service = prepare(root)
    assert profile["renderer"] == "standard_skill_v1"
    assert profile["files"][0]["metadata"]["metadata"]["renderer"] == "approved_procedure_v1"
    assert service.body("external-check") == body


def test_empty_package_markers_and_python_module_files_remain_valid(root: Path) -> None:
    source = package(root)
    (source / "references/.gitkeep").write_bytes(b"")
    (source / "scripts/__init__.py").write_bytes(b"")
    _, _, profile, service = prepare(root)
    empty = [item for item in profile["files"][0]["support_files"] if item["bytes"] == 0]
    assert len(empty) == 2
    assert service.resource("external-check", "scripts/__init__.py") == ""
    assert service.resource("external-check", "references/.gitkeep") == ""


def test_binary_null_resource_is_rejected_before_library_publication(root: Path) -> None:
    source = package(root)
    (source / "references/binary.bin").write_bytes(b"text\x00binary")
    task = task_snapshot(root)
    with pytest.raises(ValueError):
        prepare_domain_knowledge(root / "skill", root / "lib", task, "run1")
    assert not (root / "lib").exists()
