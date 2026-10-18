"""Optional platform storage never runs installers or widens Agent cleanup."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from dfx_agent import managed_runtime
from dfx_agent.harmony_workspace import prepare_workspace


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "project"
    (repo / "configs").mkdir(parents=True)
    policy = json.loads((Path(__file__).parents[1] / "configs/local-runtime.json").read_text(encoding="utf-8"))
    policy["paths"]["harmony_root"] = "DFX-HarmonyOS"
    (repo / "configs/local-runtime.json").write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(managed_runtime.Runtime, "mutex", lambda self: nullcontext())
    runtime = managed_runtime.Runtime(repo)
    runtime.prepare()
    storage = repo / "DFX-HarmonyOS"
    storage.mkdir()
    (storage / "deveco-studio-26.0.0.851.exe").write_bytes(b"MZ fake installer: must not execute")
    report = {"case_id": "b3-017-officeattendance1", "application_verified": False}
    for variant, content in (("original", b"faulty source"), ("patched", b"fixed source")):
        folder = runtime.root / "tmp/application-officeattendance" / variant
        folder.mkdir(parents=True)
        (folder / "DataManager.ets").write_bytes(content)
        report[variant + "_manifest"] = {"DataManager.ets": hashlib.sha256(content).hexdigest()}
    target = repo / "runs/exports/robustness/application-demo/preparation.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(report), encoding="utf-8")
    return repo


def test_prepare_moves_exact_copies_without_running_installer_or_changing_environment(project: Path) -> None:
    before = dict(os.environ)
    result = prepare_workspace(project)
    storage = project / "DFX-HarmonyOS"
    marker = json.loads((storage / "workspace.json").read_text())
    assert dict(os.environ) == before
    assert result["installed"] is False and result["launched"] is False
    assert marker["environment_applied"] is False
    assert all(Path(path).is_relative_to(storage) for path in marker["paths"].values())
    assert all(Path(path).is_relative_to(storage) for path in marker["process_environment_plan"].values())
    assert (storage / "projects/officeattendance/original/DataManager.ets").read_bytes() == b"faulty source"
    assert (storage / "projects/officeattendance/patched/DataManager.ets").read_bytes() == b"fixed source"
    assert not (project / ".dfx-local/tmp/application-officeattendance/original").exists()
    assert list((storage / "ide").iterdir()) == []
    assert prepare_workspace(project)["installer_sha256"] == result["installer_sha256"]


def test_source_drift_is_rejected_before_either_tree_is_moved(project: Path) -> None:
    old = project / ".dfx-local/tmp/application-officeattendance"
    (old / "patched/DataManager.ets").write_text("drift")
    with pytest.raises(ValueError, match="frozen manifest"):
        prepare_workspace(project)
    assert (old / "original/DataManager.ets").exists()
    assert not (project / "DFX-HarmonyOS/projects").exists()


def test_partial_move_resumes_without_replacing_existing_tree(project: Path) -> None:
    old = project / ".dfx-local/tmp/application-officeattendance/original"
    new = project / "DFX-HarmonyOS/projects/officeattendance/original"
    new.parent.mkdir(parents=True)
    old.rename(new)
    assert prepare_workspace(project)["copies_relocated_and_verified"] is True


def test_installer_drift_and_running_workspace_are_not_reset(project: Path) -> None:
    prepare_workspace(project)
    storage = project / "DFX-HarmonyOS"
    installer = storage / "deveco-studio-26.0.0.851.exe"
    original = installer.read_bytes()
    installer.write_bytes(b"changed installer")
    with pytest.raises(ValueError, match="installer changed"):
        prepare_workspace(project)
    installer.write_bytes(original)
    marker = json.loads((storage / "workspace.json").read_text())
    marker["launched"] = True
    (storage / "workspace.json").write_text(json.dumps(marker))
    with pytest.raises(ValueError, match="requires review"):
        prepare_workspace(project)


def test_platform_bytes_share_disk_budget_and_are_preserved_by_agent_cleanup(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = managed_runtime.Runtime(project)
    sizes, exports, missing = runtime.accounting()
    assert not missing and sizes["harmonyos"] > 0
    monkeypatch.setattr(managed_runtime, "physical_memory", lambda: (32 * 1024**3, 16 * 1024**3))
    monkeypatch.setattr(managed_runtime.shutil, "disk_usage", lambda _: SimpleNamespace(total=200 * 1024**3, free=100 * 1024**3))
    runtime.policy["limits"]["managed_disk_budget_bytes"] = sum(sizes.values()) + exports - 1
    with pytest.raises(ValueError, match="disk limit"):
        runtime.limits(30)
    assert all(not (project / "DFX-HarmonyOS").is_relative_to(target) for target in runtime.targets("all"))
    plan = runtime.clean(SimpleNamespace(Scope="all", DiscardEvidence=True, Apply=False, PlanId=""))
    assert "DFX-HarmonyOS" in plan["preserved"]
    assert runtime.status()["harmony_storage_cleanup"] == "preserved_separately"


def test_legacy_policy_is_compatible_and_arbitrary_platform_roots_are_rejected(project: Path) -> None:
    config = project / "configs/local-runtime.json"
    policy = json.loads(config.read_text())
    del policy["paths"]["harmony_root"]
    config.write_text(json.dumps(policy))
    assert managed_runtime.Runtime(project).harmony_root is None
    policy["paths"]["harmony_root"] = "data_B"
    config.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="unsupported management policy"):
        managed_runtime.Runtime(project)
