"""Curated generic procedures, frozen through the existing approved-knowledge path."""

from pathlib import Path

from .applicability_contracts import TaskKnowledgeBinding
from .evidence_io import local_evidence_path
from .knowledge import HostMaterialRegistry, KnowledgeStore, _path
from .knowledge_contracts import (
    ExperienceDraft,
    HostMaterialManifest,
    MaterialGrant,
    PermissionScope,
    encode,
)
from .knowledge_retrieval import KnowledgeRetriever
from .knowledge_run import HostKnowledgeInput
from .skill_packages import package_projection
from .task import digest, verify_task

FAMILIES = ("bounds", "lifecycle", "recursion", "dynamic-pattern")


def prepare_domain_knowledge(resources: Path, library: Path, task: Path,
                             run_id: str) -> tuple[HostKnowledgeInput, tuple[str, ...]]:
    """Admit declared guidance only; no synthetic successes or dataset solutions."""
    if library.exists():
        raise ValueError("domain library already exists")
    resources = resources.absolute()
    _path(resources, resources, directory=True)
    packages = sorted(path for path in resources.iterdir() if path.is_dir() and (path / "SKILL.md").is_file())
    if not 1 <= len(packages) <= 64:
        raise ValueError("Skill library must contain between one and 64 standard packages")
    projections = [(package.name, package_projection(package)) for package in packages]
    library.mkdir(parents=True)
    (library / "store").mkdir()
    store = KnowledgeStore(local_evidence_path(library / "store"))
    entries = []
    # Standard package discovery is independent of our four starter families.
    # Adding a skill/<name>/SKILL.md therefore needs no Python registry change.
    for family, projection in projections:
        raw = encode(projection).encode("utf-8")
        material = library / (family + "-material")
        material.mkdir()
        (material / "projection.json").write_bytes(raw)
        grant = MaterialGrant(material_id="projection", path="projection.json", sha256=digest(raw), bytes=len(raw),
            role="curated_projection", origin_split="curated", allowed_uses=("learn", "solve"),
            visibility="solver_approved", allowed_splits=("development", "candidate_validation", "confirmation"))
        registry = HostMaterialRegistry(material, HostMaterialManifest(
            registry_id="domain-" + digest(family.encode("utf-8"))[:32], grants=(grant,)))
        entries.append(store.admit_entry(ExperienceDraft(projection_material_id="projection", history_material_ids=()), registry))
    scope = PermissionScope(split="development")
    identifiers = tuple(sorted(entry.entry_id for entry in entries))
    snapshot = store.freeze_snapshot(identifiers, scope)
    retriever = KnowledgeRetriever(store)
    retriever.build_index(snapshot.snapshot_sha256, scope)
    info = verify_task(task)
    approval = TaskKnowledgeBinding(task_id=info["task_id"], run_id=run_id,
        source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
        snapshot_sha256=snapshot.snapshot_sha256, scope=scope)
    return HostKnowledgeInput(retriever, approval), identifiers
