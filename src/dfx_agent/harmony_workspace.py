"""Prepare storage for the single optional app demo; never run an installer.

The existing manager supplies ownership, Job, accounting and stop. This module
only assigns paths and relocates the already frozen original/candidate copies.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .managed_runtime import Runtime, _now, _read, _write, checked

_FOLDERS = (
    "downloads", "ide", "sdk", "emulator/images", "emulator/devices",
    "cache/hvigor", "cache/ohpm", "cache/npm", "cache/ide",
    "config/ide", "config/ohpm", "home", "tmp", "logs", "evidence",
    "projects/officeattendance",
)


def _manifest(folder: Path, repo: Path) -> dict[str, str]:
    checked(folder, repo, exists=True)
    result = {}
    for path in sorted(folder.rglob("*")):
        checked(path, repo, exists=True)
        if path.is_file():
            if path.stat().st_nlink != 1:
                raise ValueError("linked app source is refused")
            with path.open("rb") as stream:
                result[path.relative_to(folder).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def prepare_workspace(repo: Path) -> dict[str, Any]:
    runtime = Runtime(repo)
    storage = runtime.harmony_root
    if storage is None:
        raise ValueError("HarmonyOS storage root is not configured")
    owner = runtime.owner()
    report_path = repo / "runs/exports/robustness/application-demo/preparation.json"
    report = _read(report_path, repo)
    if report.get("case_id") != "b3-017-officeattendance1" or report.get("application_verified") is not False:
        raise ValueError("this preparation only accepts the frozen, unexecuted office demo")
    installer = checked(storage / "deveco-studio-26.0.0.851.exe", repo, exists=True)
    if not installer.is_file() or installer.stat().st_nlink != 1:
        raise ValueError("installer input differs")
    marker_path = storage / "workspace.json"
    previous = None
    if marker_path.exists():
        marker = _read(marker_path, repo)
        if marker.get("project_instance_id") != owner["project_instance_id"] or marker.get("root") != str(storage):
            raise ValueError("HarmonyOS workspace owner differs")
        if marker.get("state") != "storage_prepared_not_installed" or marker.get("installed") is not False or marker.get("launched") is not False:
            raise ValueError("already admitted platform workspace requires review")
        previous = marker
    with installer.open("rb") as stream:
        installer_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    if previous is not None and previous.get("installer", {}).get("sha256") != installer_hash:
        raise ValueError("installer changed after workspace preparation")

    # Check both copies before moving either; a partial interrupted move can be
    # resumed by recognizing the matching destination, never deleting a tree.
    moves = []
    for variant in ("original", "patched"):
        old = checked(runtime.root / "tmp/application-officeattendance" / variant, repo)
        new = checked(storage / "projects/officeattendance" / variant, repo)
        if old.exists() and new.exists():
            raise ValueError("both old and new app copies exist; manual review required")
        current = old if old.exists() else new
        if _manifest(current, repo) != report.get(variant + "_manifest"):
            raise ValueError("prepared application copy differs from its frozen manifest")
        moves.append((old, new))
    for relative in _FOLDERS:
        checked(storage / relative, repo).mkdir(parents=True, exist_ok=True)
    for old, new in moves:
        if old.exists():
            checked(old, repo, exists=True)
            checked(new, repo)
            old.rename(new)
        if _manifest(new, repo) != report[new.name + "_manifest"]:
            raise ValueError("relocated application copy differs")

    paths = {name: str(storage / name) for name in _FOLDERS}
    environment_plan = {
        "TEMP": paths["tmp"], "TMP": paths["tmp"],
        "HVIGOR_USER_HOME": paths["cache/hvigor"],
        "npm_config_cache": paths["cache/npm"],
        "HOME": paths["home"],
    }
    marker = {
        "schema_version": 1, "project_instance_id": owner["project_instance_id"],
        "root": str(storage), "prepared_at_utc": _now(),
        "state": "storage_prepared_not_installed", "paths": paths,
        "installer": {"path": str(installer), "bytes": installer.stat().st_size, "sha256": installer_hash},
        "process_environment_plan": environment_plan,
        "pending_tool_configuration": ["IDE config/system/plugins/log paths", "SDK location",
                                       "ohpm configuration/cache", "emulator images and instances"],
        "environment_applied": False, "installed": False, "launched": False,
        "cleanup": "Keep outside Agent env/all cleanup; stop all admitted platform processes before separate removal",
    }
    _write(marker_path, marker, repo)
    report["private_workspaces"] = {variant: str(storage / "projects/officeattendance" / variant)
                                    for variant in ("original", "patched")}
    report["environment"] = {
        "installed": False, "launched": False,
        "download_status": "User supplied deveco-studio-26.0.0.851.exe; original installer retained",
        "storage_root": str(storage), "installer": marker["installer"],
        "admission": "Storage prepared and counted in existing disk budget; SDK/tool compatibility, installation footprint and platform stop remain pending",
        "user_decision": "All optional HarmonyOS tooling, caches and device files are to remain under DFX-HarmonyOS",
    }
    _write(report_path, report, repo)
    readme = storage / "README.md"
    checked(readme, repo)
    if readme.exists() and readme.stat().st_nlink != 1:
        raise ValueError("linked local README is refused")
    readme.write_text(
        "# HarmonyOS 应用演示工作区\n\n"
        "状态：存储已准备，尚未安装或启动平台工具。\n\n"
        "本目录已被 Git 忽略。workspace.json 记录目录、安装包身份及待配置事项。\n\n"
        "| 目录 | 用途 |\n|---|---|\n"
        "| 根目录的原始 exe、downloads/ | 已下载的安装包及后续下载 |\n"
        "| ide/、sdk/ | DevEco 工具与 SDK；目前为空 |\n"
        "| emulator/images/、emulator/devices/ | 模拟器镜像与设备数据；目前为空 |\n"
        "| cache/、config/、home/、tmp/ | 构建/IDE 缓存、专属配置、工具用户目录及临时文件 |\n"
        "| projects/officeattendance/original/、patched/ | 已核对冻结清单的原版及补丁工程 |\n"
        "| logs/、evidence/ | 后续构建/运行日志和截图原件 |\n\n"
        "路径规划不代表工具已经采用这些路径；须在首次启动前配置并核验。"
        "SDK、镜像和实例分别指定，不只修改 IDE 安装位置。"
        "正式结果仍另导出到 runs/exports/robustness/application-demo/。\n\n"
        "受管 Python 入口已将本目录计入共同磁盘预算；Agent env/all 清理保留本目录。"
        "后续清理前须确认 IDE、构建器、hdc 与模拟器均已停止；目前无这些活动进程由本项目启动。"
        "不要仅删除正在运行工具的文件夹。\n", encoding="utf-8",
    )
    return {"storage_root": str(storage), "installer_bytes": installer.stat().st_size,
            "installer_sha256": installer_hash, "copies_relocated_and_verified": True,
            "installed": False, "launched": False}
