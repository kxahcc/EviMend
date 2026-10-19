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


def test_cleaned_platform_copies_can_be_rebuilt_without_old_tmp(project: Path) -> None:
    old = project / ".dfx-local/tmp/application-officeattendance"
    original = project / "data_B/data_B3/benchmarkfordfx-better/App/OfficeAttendance1"
    original.parent.mkdir(parents=True)
    (old / "original").rename(original)
    repaired = project / "runs/exports/cases/b3-017-officeattendance1/frozen/repaired-source"
    repaired.parent.mkdir(parents=True)
    (old / "patched").rename(repaired)
    report_path = project / "runs/exports/robustness/application-demo/preparation.json"
    report = json.loads(report_path.read_text())
    report.update({"patch_run": "frozen", "changed_files": ["DataManager.ets"]})
    report_path.write_text(json.dumps(report))
    assert prepare_workspace(project)["copies_relocated_and_verified"] is True
    copies = project / "DFX-HarmonyOS/projects/officeattendance"
    assert (copies / "original/DataManager.ets").read_bytes() == b"faulty source"
    assert (copies / "patched/DataManager.ets").read_bytes() == b"fixed source"
    assert (original / "DataManager.ets").read_bytes() == b"faulty source"


@pytest.mark.skipif(os.name != "nt", reason="ohpm uses Windows directory junctions")
def test_platform_package_junctions_are_counted_without_following_or_double_counting(project: Path) -> None:
    from _winapi import CreateJunction

    runtime = managed_runtime.Runtime(project)
    storage = project / "DFX-HarmonyOS"
    package = storage / "cache/packages/axios"
    package.mkdir(parents=True)
    (package / "index.js").write_bytes(b"verified package")
    before, _, _ = runtime.accounting()
    modules = storage / "projects/app/oh_modules"
    modules.mkdir(parents=True)
    junction = modules / "axios"
    CreateJunction(str(package), str(junction))
    try:
        after, _, missing = runtime.accounting()
        assert not missing
        assert after["harmonyos"] == before["harmonyos"]
    finally:
        junction.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="ohpm uses Windows directory junctions")
@pytest.mark.parametrize("outside", ["data_B", ".dfx-local/cache"])
def test_platform_junctions_cannot_escape_into_other_project_roots(project: Path, outside: str) -> None:
    from _winapi import CreateJunction

    runtime = managed_runtime.Runtime(project)
    target = project / outside
    target.mkdir(parents=True, exist_ok=True)
    (target / "foreign.txt").write_bytes(b"must not be counted or followed")
    junction = project / "DFX-HarmonyOS/foreign"
    CreateJunction(str(target), str(junction))
    try:
        with pytest.raises(ValueError, match="unsafe linked inventory"):
            runtime.accounting()
    finally:
        junction.rmdir()


def _external_build_runtime(project: Path, monkeypatch: pytest.MonkeyPatch) -> managed_runtime.Runtime:
    home = project.parent / "profile"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runtime = managed_runtime.Runtime(project)
    marker = {"project_instance_id": runtime.owner()["project_instance_id"], "root": str(project / "DFX-HarmonyOS"),
              "build_root_authorized": True, "paths": {"build_root": str(home / "DFX-HarmonyOS-build")}}
    (project / "DFX-HarmonyOS/workspace.json").write_text(json.dumps(marker))
    return managed_runtime.Runtime(project)


def test_approved_build_root_is_owned_counted_and_preserved(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _external_build_runtime(project, monkeypatch)
    runtime.prepare_harmony_build_root()
    root = runtime.harmony_build_root
    assert root is not None
    before, _, _ = runtime.accounting()
    (root / "result.hap").write_bytes(b"application package")
    after, _, missing = runtime.accounting()
    assert not missing and after["harmony_build"] == before["harmony_build"] + len(b"application package")
    assert all(not root.is_relative_to(target) for target in runtime.targets("all"))
    preview = runtime.clean(SimpleNamespace(Scope="all", DiscardEvidence=True, Apply=False, PlanId=""))
    assert str(root) in preview["preserved"]
    owner = root / ".dfx-build-owner.json"
    record = json.loads(owner.read_text())
    owner.write_text(json.dumps({**record, "project_instance_id": "another project"}))
    with pytest.raises(ValueError, match="owner differs"):
        runtime.prepare_harmony_build_root()


def test_external_build_root_does_not_adopt_an_existing_foreign_folder(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _external_build_runtime(project, monkeypatch)
    root = runtime.harmony_build_root
    assert root is not None
    root.mkdir()
    (root / "foreign.txt").write_bytes(b"existing user file")
    with pytest.raises(FileNotFoundError):
        runtime.prepare_harmony_build_root()
    assert (root / "foreign.txt").read_bytes() == b"existing user file"
    assert not (root / ".dfx-build-owner.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="SDK resource compiler needs a Windows junction")
def test_external_sdk_alias_counts_the_physical_sdk_once(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from _winapi import CreateJunction

    runtime = _external_build_runtime(project, monkeypatch)
    sdk = project / "DFX-HarmonyOS/ide/sdk"
    sdk.mkdir(parents=True)
    (sdk / "resource.bin").write_bytes(b"SDK resource")
    runtime.prepare_harmony_build_root()
    root = runtime.harmony_build_root
    assert root is not None
    alias = root / "sdk"
    try:
        sizes, _, missing = runtime.accounting()
        assert not missing
        assert sizes["harmony_build"] == (root / ".dfx-build-owner.json").stat().st_size
        assert alias.resolve(strict=True) == sdk
        runtime.prepare_harmony_build_root()
        alias.rmdir()
        (project / "data_B").mkdir()
        CreateJunction(str(project / "data_B"), str(alias))
        with pytest.raises(ValueError, match="SDK alias differs"):
            runtime.prepare_harmony_build_root()
        with pytest.raises(ValueError, match="unsafe linked inventory"):
            runtime.accounting()
    finally:
        if os.path.lexists(alias):
            alias.rmdir()


@pytest.mark.parametrize("change", ["wrong-root", "not-authorized"])
def test_external_build_configuration_cannot_select_other_directories(project: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    _external_build_runtime(project, monkeypatch)
    path = project / "DFX-HarmonyOS/workspace.json"
    marker = json.loads(path.read_text())
    if change == "wrong-root":
        marker["paths"]["build_root"] = str(Path.home() / "unapproved-folder")
    else:
        marker["build_root_authorized"] = False
    path.write_text(json.dumps(marker))
    with pytest.raises(ValueError, match="unapproved external"):
        managed_runtime.Runtime(project)
