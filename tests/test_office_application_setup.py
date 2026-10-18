"""Confinement checks before unpacking an optional platform tool archive."""

from __future__ import annotations

import json
import runpy
from contextlib import nullcontext
from pathlib import Path

import pytest

from dfx_agent import managed_runtime


@pytest.fixture
def parse():
    script = Path(__file__).parents[1] / "scripts/preparation/office_application_demo.py"
    return runpy.run_path(str(script))["parse_listing"]


def test_listing_keeps_unicode_and_space_paths_and_sizes(parse) -> None:
    entries = parse("Path = sdk\\中文目录\\file name.txt\nSize = 128\nAttributes = A\n\nPath = bin\nSize = 0\nAttributes = D\n")
    assert entries == [{"path": "sdk/中文目录/file name.txt", "size": 128, "attributes": "A"},
                       {"path": "bin", "size": 0, "attributes": "D"}]


@pytest.mark.parametrize("entry", ["../outside", "C:/outside", "/outside", "ide/../../outside"])
def test_archive_cannot_write_outside_destination(parse, entry: str) -> None:
    with pytest.raises(ValueError, match="non-relative"):
        parse(f"Path = {entry}\nSize = 1\n")


@pytest.mark.parametrize("extra", ["Symbolic Link = ../outside", "Hard Link = ../outside", "Size = -1"])
def test_link_or_negative_size_is_refused(parse, extra: str) -> None:
    with pytest.raises(ValueError, match="linked or invalid"):
        parse(f"Path = sdk/file\nSize = 1\n{extra}\n")


def test_installed_toolchain_does_not_require_retained_installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "project"
    (repo / "configs").mkdir(parents=True)
    root = Path(__file__).parents[1]
    policy = json.loads((root / "configs/local-runtime.json").read_text())
    (repo / "configs/local-runtime.json").write_text(json.dumps(policy))
    monkeypatch.setattr(managed_runtime.Runtime, "mutex", lambda self: nullcontext())
    runtime = managed_runtime.Runtime(repo)
    runtime.prepare()
    storage = repo / "DFX-HarmonyOS"
    storage.mkdir()
    marker = {"project_instance_id": runtime.owner()["project_instance_id"], "root": str(storage),
              "installed": True, "installer": {"path": str(storage / "missing-installer.exe"), "sha256": "0" * 64}}
    (storage / "workspace.json").write_text(json.dumps(marker))
    context = runpy.run_path(str(root / "scripts/preparation/office_application_demo.py"))["storage_context"]
    assert context(repo)[2]["installed"] is True
    with pytest.raises(FileNotFoundError):
        context(repo, require_installer=True)
