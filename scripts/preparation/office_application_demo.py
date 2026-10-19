"""Finite preparation for the one optional OfficeAttendance app demonstration.

Called only by the existing Windows Job worker. Artifacts stay in DFX-HarmonyOS;
only final application verification evidence is published in application-demo.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from dfx_agent.harmony_workspace import _manifest
from dfx_agent.managed_runtime import (
    Runtime,
    _io_path,
    _now,
    _read,
    _write,
    checked,
    require_managed_worker,
)


def storage_context(repo: Path, *, require_installer: bool = False) -> tuple[Runtime, Path, dict[str, Any]]:
    runtime = Runtime(repo)
    storage = runtime.harmony_root
    if storage is None:
        raise ValueError("platform storage is not configured")
    marker = _read(storage / "workspace.json", repo)
    if marker.get("project_instance_id") != runtime.owner()["project_instance_id"] or marker.get("root") != str(storage):
        raise ValueError("platform storage owner differs")
    if require_installer or marker.get("installed") is not True:
        installer = checked(Path(marker["installer"]["path"]), storage, exists=True)
        with installer.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != marker["installer"]["sha256"]:
                raise ValueError("installer differs from the prepared input")
    return runtime, storage, marker


def process_environment(storage: Path) -> dict[str, str]:
    env = os.environ.copy()
    home = storage / "home"
    env.update({"TEMP": str(storage / "tmp"), "TMP": str(storage / "tmp"),
                "HOME": str(home), "USERPROFILE": str(home),
                "APPDATA": str(home / "AppData/Roaming"), "LOCALAPPDATA": str(home / "AppData/Local"),
                "HVIGOR_USER_HOME": str(storage / "cache/hvigor"),
                "npm_config_cache": str(storage / "cache/npm")})
    env["HOMEDRIVE"] = home.drive
    env["HOMEPATH"] = str(home)[len(home.drive):]
    env.pop("_JAVA_OPTIONS", None)
    env.pop("JDK_JAVA_OPTIONS", None)
    env["JAVA_TOOL_OPTIONS"] = '-Duser.home="' + home.as_posix() + '"'
    env["JAVA_HOME"] = str(storage / "ide/jbr")
    env["NODE_HOME"] = str(storage / "ide/tools/node")
    env["DEVECO_SDK_HOME"] = str(storage / "ide/sdk")
    env["PATH"] = os.pathsep.join([env["NODE_HOME"], str(storage / "ide/jbr/bin"), env.get("PATH", "")])
    for name in ("TEMP", "APPDATA", "LOCALAPPDATA"):
        checked(Path(env[name]), storage).mkdir(parents=True, exist_ok=True)
    return env


def archiver(storage: Path | None = None) -> Path:
    # Reuse an existing full 7-Zip CLI read-only; no installer/global package.
    choices = [str(storage / "tools/7zip/7z.exe") if storage else None,
               shutil.which("7z"), r"C:\Program Files\NVIDIA Corporation\NVIDIA app\7z.exe",
               r"C:\Program Files\AMD\CIM\Bin64\7z.exe", r"C:\Program Files\7-Zip\7z.exe"]
    for value in choices:
        if value and Path(value).is_file():
            return Path(value)
    raise ValueError("existing full 7-Zip CLI is unavailable")


def execute(repo: Path, storage: Path, argv: list[str], log_name: str, timeout: int = 120,
            *, cwd: Path | None = None) -> Path:
    log = checked(storage / "logs" / log_name, repo)
    if log.exists() and log.stat().st_nlink != 1:
        raise ValueError("linked platform log is refused")
    runtime = Runtime(repo)
    working = cwd or storage
    boundary = storage if working.is_relative_to(storage) else runtime.harmony_build_root
    if boundary is None:
        raise ValueError("platform command working directory is not admitted")
    env = process_environment(storage)
    if boundary == runtime.harmony_build_root:
        alias = boundary / "sdk"
        if not alias.is_junction() or alias.resolve(strict=True) != storage / "ide/sdk":
            raise ValueError("external SDK alias is not the admitted SDK")
        env["DEVECO_SDK_HOME"] = str(alias)
    with log.open("wb") as output:
        result = subprocess.run(argv, cwd=checked(working, boundary, exists=True), env=env,
                                stdout=output, stderr=subprocess.STDOUT, shell=False, timeout=timeout, check=False)
    if log.stat().st_size > Runtime(repo).policy["limits"]["max_log_file_bytes"]:
        raise ValueError("platform command output exceeds the configured log cap")
    if result.returncode != 0:
        raise ValueError(f"platform command exited {result.returncode}; inspect {log_name}")
    return log


def parse_listing(text: str) -> list[dict[str, Any]]:
    entries = []
    record: dict[str, str] = {}
    for line in [*text.splitlines(), ""]:
        if not line.strip():
            if "Path" in record:
                path = record["Path"].replace("\\", "/")
                parts = path.split("/")
                if path.startswith("/") or any(part in ("", ".", "..") or ":" in part for part in parts):
                    raise ValueError("archive contains a non-relative extraction path")
                size = int(record.get("Size", "0"))
                if size < 0 or "Symbolic Link" in record or "Hard Link" in record:
                    raise ValueError("linked or invalid archive entry")
                entries.append({"path": path, "size": size,
                                "attributes": record.get("Attributes", "")})
                for field, output_key in (("Offset", "offset"), ("Unpack Size", "unpack_size")):
                    if record.get(field):
                        entries[-1][output_key] = int(record[field])
                if record.get("Type"):
                    entries[-1]["type"] = record["Type"]
            record = {}
        elif " = " in line:
            key, value = line.split(" = ", 1)
            record[key] = value
    if not entries:
        raise ValueError("archive inspection did not yield file entries")
    return entries


def inspect_package(repo: Path, context: tuple[Runtime, Path, dict[str, Any]] | None = None) -> dict[str, Any]:
    runtime, storage, marker = context or storage_context(repo, require_installer=True)
    tool = archiver(storage)
    listing = execute(repo, storage, [str(tool), "l", "-t#", "-slt", "-ba", "-sccUTF-8", marker["installer"]["path"]], "package-segments.txt", timeout=300)
    entries = parse_listing(listing.read_text(encoding="utf-8-sig", errors="strict"))
    sizes, exported, inaccessible = runtime.accounting()
    if inaccessible:
        raise ValueError("platform accounting is incomplete")
    report = {"installer_sha256": marker["installer"]["sha256"], "archiver": str(tool),
              "entry_count": len(entries), "uncompressed_bytes": sum(item["size"] for item in entries),
              "top_level_names": sorted({item["path"].split("/")[0] for item in entries}),
              "current_shared_bytes": sum(sizes.values()) + exported,
              "disk_budget_bytes": runtime.policy["limits"]["managed_disk_budget_bytes"],
              "volume_free_bytes": shutil.disk_usage(storage).free, "entries": entries,
              "sdk_executed": False, "installer_executed": False}
    report["full_payload_listed"] = True
    report["archive_type"] = "parser_segments"
    report["size_scope"] = "embedded container segments; nested archives require separate size admission"
    cursor = 0
    for entry in entries:
        if entry.get("offset") != cursor:
            raise ValueError("embedded segment listing has a gap or overlap")
        cursor += entry["size"]
    if cursor != marker["installer"]["bytes"]:
        raise ValueError("embedded segment listing does not cover the complete input")
    report["embedded_unpacked_bytes"] = sum(entry.get("unpack_size", 0) for entry in entries)
    _write(storage / "config/package-inspection.json", report, repo)
    return {key: value for key, value in report.items() if key != "entries"}


def prepare_extractor(repo: Path) -> dict[str, Any]:
    _, storage, _ = storage_context(repo, require_installer=True)
    url = "https://github.com/ip7z/7zip/releases/download/26.04/7z2604-x64.exe"
    download = checked(storage / "downloads/7z2604-x64.exe", repo)
    if not download.exists():
        pending = checked(download.with_suffix(".part"), repo)
        if pending.exists():
            raise ValueError("incomplete extractor download requires review")
        with urllib.request.urlopen(url, timeout=60) as response, pending.open("xb") as output:
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > 8 * 1024 * 1024:
                    raise ValueError("unexpected extractor download size")
                output.write(chunk)
        pending.rename(download)
    target = checked(storage / "tools/7zip", repo)
    target.mkdir(parents=True, exist_ok=True)
    if not (target / "7z.exe").exists():
        execute(repo, storage, [str(archiver()), "x", "-y", "-o" + str(target), str(download)], "extractor-unpack.txt")
    if not (target / "7z.exe").is_file() or not (target / "7z.dll").is_file():
        raise ValueError("portable extractor files were not unpacked")
    with download.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    _write(storage / "config/extractor.json", {"official_source": url, "sha256": digest,
                                              "storage": str(target), "installer_executed": False}, repo)
    return inspect_package(repo)


def extract_package(repo: Path) -> dict[str, Any]:
    runtime, storage, marker = storage_context(repo, require_installer=True)
    if runtime.policy["execution"]["allow_local_harmony_sdk"] is not True:
        raise ValueError("platform unpacking is not enabled")
    if marker.get("installed") is not False or marker.get("launched") is not False:
        raise ValueError("existing platform installation requires review")
    inspected = _read(storage / "config/package-inspection.json", repo, limit=16 * 1024 * 1024)
    if (inspected["installer_sha256"] != marker["installer"]["sha256"] or not inspected["entries"]
            or inspected.get("full_payload_listed") is not True):
        raise ValueError("package inspection differs")
    effective = require_managed_worker(repo, "harmony-prepare")["effective_limits"]
    sizes, exported, missing = runtime.accounting()
    if inspected.get("archive_type") != "parser_segments" or not inspected.get("payloads_prepared"):
        raise ValueError("embedded archives have not been prepared")
    required = inspected["embedded_unpacked_bytes"]
    if missing or sum(sizes.values()) + exported + required > effective["disk_budget_bytes"]:
        raise ValueError("expanded package exceeds the admitted disk budget")
    if shutil.disk_usage(storage).free - required < effective["disk_reserve_bytes"]:
        raise ValueError("expanded package violates the free space reserve")
    target = checked(storage / "ide", repo, exists=True)
    if any(target.iterdir()):
        raise ValueError("extraction destination is not empty")
    payloads = storage / "tmp/setup-payload"
    for entry in inspected["entries"]:
        if entry.get("type") != "7z":
            continue
        archive = checked(payloads / entry["path"], repo, exists=True)
        execute(repo, storage, [str(archiver(storage)), "x", "-y", "-sccUTF-8", "-o" + str(target),
                                str(archive)], "unpack-" + archive.stem + ".txt", timeout=900)
    product = checked(target / "product-info.json", repo)
    product_info = _read(product, repo) if product.is_file() else {"metadata": "IDE product-info not in embedded archives; tool manifests still require inspection"}
    marker.update({"state": "tool_files_unpacked_not_executed", "installed": True,
                   "portable_extraction": True, "installer_executed": False,
                   "product": product_info, "environment_applied": False})
    _write(storage / "workspace.json", marker, repo)
    report_path = repo / "runs/exports/robustness/application-demo/preparation.json"
    report = _read(report_path, repo)
    report["environment"].update({"installed": True, "portable_extraction": True,
                                  "installer_executed": False, "launched": False,
                                  "product": product_info,
                                  "admission": "Tool files extracted in project storage; execution paths, SDK compatibility and platform stop not yet verified"})
    _write(report_path, report, repo)
    readme = storage / "README.md"
    text = readme.read_text(encoding="utf-8")
    readme.write_text(text.replace("状态：存储已准备，尚未安装或启动平台工具。", "状态：工具文件已在本目录展开，尚未构建或运行应用。")
                     .replace("；目前为空", ""), encoding="utf-8")
    return {"installed_by_portable_extraction": True, "installer_executed": False,
            "product": product_info, "application_verified": False}


def prepare_payloads(repo: Path) -> dict[str, Any]:
    runtime, storage, marker = storage_context(repo, require_installer=True)
    inspect_package(repo, context=(runtime, storage, marker))
    inspection_path = storage / "config/package-inspection.json"
    inspected = _read(inspection_path, repo)
    archives = [entry for entry in inspected["entries"] if entry.get("type") == "7z"]
    target = checked(storage / "tmp/setup-payload", repo)
    target.mkdir(parents=True, exist_ok=True)
    expected_names = {entry["path"] for entry in archives}
    if any(path.name not in expected_names or not path.is_file() for path in target.iterdir()):
        raise ValueError("unexpected payload destination content")
    for entry in archives:
        existing = checked(target / entry["path"], repo)
        if existing.exists() and (existing.stat().st_nlink != 1 or existing.stat().st_size != entry["size"]):
            raise ValueError("existing embedded archive differs")
    missing_archives = [entry for entry in archives if not (target / entry["path"]).exists()]
    required = sum(entry["size"] for entry in missing_archives)
    effective = require_managed_worker(repo, "harmony-prepare")["effective_limits"]
    if inspected["current_shared_bytes"] + required > effective["disk_budget_bytes"]:
        raise ValueError("embedded archives exceed the admitted disk budget")
    if shutil.disk_usage(storage).free - required < effective["disk_reserve_bytes"]:
        raise ValueError("embedded archives violate the free space reserve")
    if missing_archives:
        execute(repo, storage, [str(archiver(storage)), "x", "-t#", "-y", "-sccUTF-8",
                                "-o" + str(target), marker["installer"]["path"],
                                *[entry["path"] for entry in missing_archives]], "payload-extraction.txt", timeout=300)
    for entry in archives:
        archive = checked(target / entry["path"], repo, exists=True)
        if archive.stat().st_size != entry["size"]:
            raise ValueError("extracted archive size differs")
        listing = execute(repo, storage, [str(archiver(storage)), "l", "-slt", "-ba", "-sccUTF-8", str(archive)],
                          "contents-" + archive.stem + ".txt")
        nested = parse_listing(listing.read_text(encoding="utf-8-sig", errors="strict"))
        if sum(item["size"] for item in nested) != entry["unpack_size"]:
            raise ValueError("embedded archive size differs from its listing")
        entry["nested_top_levels"] = sorted({item["path"].split("/")[0] for item in nested})
        entry["nested_count"] = len(nested)
    inspected["payloads_prepared"] = True
    _write(inspection_path, inspected, repo)
    return {"payload_archives": len(archives), "compressed_bytes": sum(entry["size"] for entry in archives),
            "expanded_bytes": inspected["embedded_unpacked_bytes"],
            "archive_layout": [{key: value for key, value in entry.items() if key in {"path", "nested_top_levels", "nested_count"}}
                               for entry in archives], "sdk_executed": False}


def configure_and_probe_tools(repo: Path) -> dict[str, Any]:
    runtime, storage, marker = storage_context(repo)
    if runtime.policy["execution"]["allow_local_harmony_sdk"] is not True or marker.get("installed") is not True:
        raise ValueError("platform tools have not been admitted and unpacked")
    ide = storage / "ide"
    properties = checked(ide / "bin/idea.properties", repo, exists=True)
    text = properties.read_text(encoding="utf-8")
    section = "# DFX project-local paths\n"
    text = text.split(section, 1)[0].rstrip() + "\n\n" + section
    text += "\n".join(key + "=" + (storage / relative).as_posix() for key, relative in (
        ("idea.config.path", "config/ide"), ("idea.system.path", "cache/ide"),
        ("idea.plugins.path", "config/ide/plugins"), ("idea.log.path", "logs/ide"))) + "\n"
    properties.write_text(text, encoding="utf-8")
    node = checked(ide / "tools/node/node.exe", repo, exists=True)
    java = checked(ide / "jbr/bin/java.exe", repo, exists=True)
    hdc = checked(ide / "sdk/default/openharmony/toolchains/hdc.exe", repo, exists=True)
    node_log = execute(repo, storage, [str(node), "-e", "console.log(JSON.stringify({version:process.version,home:require('os').homedir()}))"], "node-version.json")
    node_info = json.loads(node_log.read_text(encoding="utf-8-sig"))
    if Path(node_info["home"]) != storage / "home":
        raise ValueError("Node user storage is outside the workspace")
    execute(repo, storage, [str(java), "-XshowSettings:properties", "-version"], "java-settings.txt")
    execute(repo, storage, [str(hdc), "-v"], "hdc-version.txt")
    ohpm = checked(ide / "tools/ohpm/bin/pm-cli.js", repo, exists=True)
    hvigor = checked(ide / "tools/hvigor/bin/hvigorw.js", repo, exists=True)
    emulator = checked(ide / "tools/emulator/Emulator.exe", repo, exists=True)
    configure_package_network(repo, storage, node, ohpm)
    execute(repo, storage, [str(node), str(ohpm), "--version"], "ohpm-version.txt")
    execute(repo, storage, [str(node), str(hvigor), "--help"], "hvigor-help.txt")
    try:
        execute(repo, storage, [str(emulator), "--help"], "emulator-help.txt", timeout=30)
    except ValueError:
        # Some vendor help commands deliberately return nonzero. Preserve the
        # actual output without treating that as application execution proof.
        pass
    sdk = {}
    for family in ("hms", "openharmony"):
        filename = "uni-package.json" if family == "hms" else "oh-uni-package.json"
        path = checked(ide / "sdk/default" / family / "ets" / filename, repo)
        if path.is_file():
            sdk[family] = _read(path, repo)
    report = {"node": node_info, "sdk": sdk, "ide_product": marker.get("product"),
              "logs": ["node-version.json", "java-settings.txt", "hdc-version.txt"],
              "storage_root": str(storage), "process_environment_scoped": True,
              "application_built": False, "application_run": False}
    _write(storage / "config/toolchain-probe.json", report, repo)
    marker["paths"]["sdk"] = str(ide / "sdk")
    marker["process_environment_plan"] = {key: value for key, value in process_environment(storage).items()
                                           if key in {"HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
                                                      "HVIGOR_USER_HOME", "npm_config_cache", "JAVA_HOME", "NODE_HOME", "DEVECO_SDK_HOME"}}
    marker["state"] = "sdk_cli_probed_application_pending"
    _write(storage / "workspace.json", marker, repo)
    java_text = (storage / "logs/java-settings.txt").read_bytes().decode("ascii", errors="ignore")
    java_version = next((line.strip() for line in java_text.splitlines() if line.startswith("openjdk version")), "unknown")
    summary = {
        "assembled_at_utc": _now(), "scope": "toolchain_cli_probe_only",
        "case_id": "b3-017-officeattendance1", "patch_run": "live1017-retry03",
        "versions": {"deveco_studio": marker["product"]["version"], "node": node_info["version"],
                     "java": java_version, "hvigor": _read(ide / "tools/hvigor/hvigor/package.json", repo)["version"],
                     "ohpm": _read(ide / "tools/ohpm/package.json", repo)["version"],
                     "hdc": (storage / "logs/hdc-version.txt").read_text().strip(),
                     "emulator": (storage / "logs/emulator-version.txt").read_text().strip()
                     if (storage / "logs/emulator-version.txt").exists() else "not_probed"},
        "sdk": sdk, "declared_project_sdk": "5.0.3(15)",
        "installer_sha256": marker["installer"]["sha256"],
        "installer_executed": False, "platform_root": "DFX-HarmonyOS",
        "node_home_redirect_verified": True, "native_receipt": require_managed_worker(repo, "harmony-prepare")["run_id"],
        "new_model_calls": 0, "includes_application_execution_evidence": False,
    }
    _write(repo / "runs/exports/robustness/application-demo/environment.json", summary, repo)
    return report


def emulator_information(repo: Path) -> dict[str, Any]:
    runtime, storage, marker = storage_context(repo)
    if runtime.policy["execution"]["allow_local_harmony_sdk"] is not True or marker.get("installed") is not True:
        raise ValueError("emulator tooling has not been admitted")
    emulator = checked(storage / "ide/tools/emulator/Emulator.exe", repo, exists=True)
    execute(repo, storage, [str(emulator), "-version"], "emulator-version.txt", cwd=emulator.parent)
    execute(repo, storage, [str(emulator), "-config", "-instancePath", str(storage / "emulator/devices"),
                            "-imageRoot", str(storage / "emulator/images")],
            "emulator-config-paths.txt", timeout=30, cwd=emulator.parent)
    execute(repo, storage, [str(emulator), "-imageList", "-downloaded", "false"],
            "emulator-images-all.txt", timeout=90, cwd=emulator.parent)
    execute(repo, storage, [str(emulator), "-imageList", "-deviceType", "Phone", "-downloaded", "false"],
            "emulator-images-phone.txt", timeout=90, cwd=emulator.parent)
    execute(repo, storage, [str(emulator), "-imageList", "-deviceType", "Phone", "-downloaded", "false",
                            "-http_proxy", "http://127.0.0.1:7897"],
            "emulator-images-proxy.txt", timeout=90, cwd=emulator.parent)
    execute(repo, storage, [str(emulator), "-screenProfileList", "-deviceType", "Phone", "-details"],
            "emulator-screen-profiles.txt", timeout=90, cwd=emulator.parent)
    return {"application_run": False, "image_downloaded": False,
            "images_returned": bool((storage / "logs/emulator-images-phone.txt").read_bytes().strip()
                                     or (storage / "logs/emulator-images-proxy.txt").read_bytes().strip()),
            "logs": ["emulator-version.txt", "emulator-config-paths.txt", "emulator-images-all.txt",
                     "emulator-images-phone.txt", "emulator-images-proxy.txt", "emulator-screen-profiles.txt"]}


def configure_package_network(repo: Path, storage: Path, node: Path, ohpm: Path) -> None:
    # Let the official CLI update its configuration, preserving any future
    # SDK login/settings rather than replacing the whole user's .ohpmrc.
    limits = Runtime(repo).policy["limits"]
    settings = {"cache": str(storage / "cache/ohpm"), "strict_ssl": "true", "retry_times": "5",
                "retry_interval": "2000", "fetch_timeout": "60000",
                "max_concurrent": str(limits["max_download_concurrency"])}
    for key, value in settings.items():
        execute(repo, storage, [str(node), str(ohpm), "config", "set", key, value], "ohpm-config-" + key + ".txt")


def accept_emulator_license(repo: Path) -> dict[str, Any]:
    runtime, storage, marker = storage_context(repo)
    if runtime.policy["execution"]["allow_local_harmony_sdk"] is not True or marker.get("installed") is not True:
        raise ValueError("emulator tooling has not been admitted")
    emulator = checked(storage / "ide/tools/emulator/Emulator.exe", repo, exists=True)
    execute(repo, storage, [str(emulator), "-license", "accept"], "emulator-license-accept.txt")
    execute(repo, storage, [str(emulator), "-imageList", "-deviceType", "phone", "-downloaded", "false"],
            "emulator-images.txt", timeout=90)
    # These twelve files were produced from the retained original installer,
    # and every component subsequently passed the archiver's CRC extraction.
    # Remove only those exact derived files; never the user-supplied exe.
    inspected = _read(storage / "config/package-inspection.json", repo)
    payloads = checked(storage / "tmp/setup-payload", repo)
    removed = 0
    for entry in inspected["entries"]:
        if entry.get("type") != "7z":
            continue
        archive = checked(payloads / entry["path"], payloads)
        if archive.exists():
            if not archive.is_file() or archive.stat().st_nlink != 1 or archive.stat().st_size != entry["size"]:
                raise ValueError("temporary archive changed; cleanup requires review")
            removed += archive.stat().st_size
            archive.unlink()
    if payloads.exists() and not any(payloads.iterdir()):
        payloads.rmdir()
    marker["derived_payload_bytes_released"] = removed
    _write(storage / "workspace.json", marker, repo)
    return {"license_command_completed": True, "image_downloaded": False,
            "derived_payload_bytes_released": removed, "original_installer_preserved": True}


def stage_downloaded_axios(repo: Path, storage: Path) -> dict[str, Any] | None:
    archive = checked(storage / "downloads/axios-2.2.15.har", storage)
    if not archive.exists():
        return None
    metadata = _read(storage / "config/axios-registry-metadata.json", repo)
    integrity = metadata["versions"]["2.2.15"]["dist"]["integrity"]
    if not integrity.startswith("sha512-"):
        raise ValueError("official axios metadata does not declare SHA-512")
    expected = base64.b64decode(integrity[7:], validate=True)
    with _io_path(archive).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha512").digest()
    if len(expected) != 64 or digest != expected:
        raise ValueError("downloaded axios differs from the official package integrity")
    # The bundled ohpm/lib/core/cache/index.js uses this content-addressed
    # layout and rechecks the same registry digest before accepting a cache hit.
    # Seed only this verified download; keep dependency declarations unchanged.
    value = digest.hex()
    cached = checked(storage / "cache/ohpm/content-v1/sha512" / value[:2] / value[2:4] / value[4:], storage)
    _io_path(cached.parent).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_io_path(archive), _io_path(cached))
    record = {"name": "@ohos/axios", "version": "2.2.15", "bytes": _io_path(archive).stat().st_size,
              "official_integrity_matches": True, "integrity": integrity, "cache_path": str(cached)}
    _write(storage / "config/axios-offline-package.json", record, repo)
    return record


def build_application(repo: Path, variant: str) -> dict[str, Any]:
    runtime, storage, marker = storage_context(repo)
    if variant not in {"original", "patched"}:
        raise ValueError("unknown office application variant")
    if runtime.policy["execution"]["allow_application_experiments"] is not True or marker.get("installed") is not True:
        raise ValueError("application build has not been admitted")
    preparation = _read(repo / "runs/exports/robustness/application-demo/preparation.json", repo)
    source = checked(storage / "projects/officeattendance" / variant, repo, exists=True)
    if _manifest(source, repo) != preparation[variant + "_manifest"]:
        raise ValueError("frozen application input differs")
    boundary = runtime.harmony_build_root or repo
    work = (checked(runtime.harmony_build_root / variant, boundary) if runtime.harmony_build_root is not None
            else checked(source.with_name("build-" + variant), repo))
    if not work.exists():
        shutil.copytree(source, work)
    # Only build-system files may be adapted to the installed SDK. Application
    # source remains exactly the existing original or Agent's selected patch.
    for relative, expected in preparation[variant + "_manifest"].items():
        if relative.endswith(("build-profile.json5", "oh-package.json5", "hvigor-config.json5")):
            continue
        path = checked(work / relative, boundary, exists=True)
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError("application business source differs in build workspace")
    node = checked(storage / "ide/tools/node/node.exe", repo, exists=True)
    ohpm = checked(storage / "ide/tools/ohpm/bin/pm-cli.js", repo, exists=True)
    hvigor = checked(storage / "ide/tools/hvigor/bin/hvigorw.js", repo, exists=True)
    started = time.monotonic()
    configure_package_network(repo, storage, node, ohpm)
    stage_downloaded_axios(repo, storage)
    record: dict[str, Any] = {"variant": variant, "workspace": str(work), "status": "running"}
    destination = storage / "config" / ("build-" + variant + ".json")
    try:
        execute(repo, storage, [str(node), str(ohpm), "install", "--all"],
                "ohpm-install-" + variant + ".txt", timeout=600, cwd=work)
        execute(repo, storage, [str(node), str(hvigor), "--mode", "module", "-p", "product=default",
                                "-p", "module=phone@default", "assembleHap", "--no-daemon", "--no-parallel",
                                "--analyze=false", "--max-old-space-size", "2048", "--Xmx", "1024"],
                "build-" + variant + ".txt", timeout=900, cwd=work)
        packages = sorted(work.glob("product/phone/build/default/outputs/default/*.hap"))
        if not packages:
            raise ValueError("build returned success without a HAP")
        record["haps"] = []
        for path in packages:
            checked(path, boundary, exists=True)
            with path.open("rb") as stream:
                record["haps"].append({"path": str(path), "bytes": path.stat().st_size,
                                       "sha256": hashlib.file_digest(stream, "sha256").hexdigest()})
        record["status"] = "built"
        return record
    except Exception as error:
        record.update({"status": "failed", "error": str(error)})
        raise
    finally:
        record["elapsed_ms"] = (time.monotonic() - started) * 1000
        _write(destination, record, repo)


def export_build_evidence(repo: Path) -> dict[str, Any]:
    runtime, storage, _ = storage_context(repo)
    if runtime.harmony_build_root is None:
        raise ValueError("approved build directory is not configured")
    runtime.prepare_harmony_build_root()
    preparation = _read(repo / "runs/exports/robustness/application-demo/preparation.json", repo)
    output = checked(repo / "runs/exports/robustness/application-demo/build", repo)
    records = {}
    for variant in ("original", "patched"):
        record = _read(storage / "config" / ("build-" + variant + ".json"), repo)
        work = checked(runtime.harmony_build_root / variant, runtime.harmony_build_root, exists=True)
        if record["status"] != "built" or record["workspace"] != str(work) or not record.get("haps"):
            raise ValueError("application build has not completed")
        # Verify every supplied input again after compilation, including build
        # configuration. Generated files are not confused with input changes.
        for relative, expected in preparation[variant + "_manifest"].items():
            path = checked(work / relative, runtime.harmony_build_root, exists=True)
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise ValueError("application input changed during compilation")
        target = checked(output / variant, repo)
        target.mkdir(parents=True, exist_ok=True)
        packages = []
        for package in record["haps"]:
            path = checked(Path(package["path"]), work, exists=True)
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != package["sha256"]:
                    raise ValueError("compiled HAP changed before export")
            destination = checked(target / path.name, repo)
            shutil.copyfile(path, destination)
            packages.append({**package, "path": destination.relative_to(output).as_posix()})
        for prefix in ("build-", "ohpm-install-"):
            source = checked(storage / "logs" / (prefix + variant + ".txt"), repo, exists=True)
            shutil.copyfile(source, checked(target / source.name, repo))
        records[variant] = {**record, "haps": packages, "input_files_verified_after_build": len(preparation[variant + "_manifest"]),
                            "build_configuration_changes": [], "signed": False}
    report = {"case_id": preparation["case_id"], "patch_run": "live1017-retry03", "scope": "application_build_only",
              "application_run": False, "collected_at_utc": _now(), "builds": records,
              "native_receipt": require_managed_worker(repo, "harmony-prepare")["run_id"]}
    _write(output / "builds.json", report, repo)
    return report


def inspect_ide(repo: Path) -> dict[str, Any]:
    runtime, storage, _ = storage_context(repo)
    if not runtime.policy["execution"]["allow_application_experiments"]:
        raise ValueError("application inspection is not admitted")
    if runtime.harmony_build_root is None:
        raise ValueError("approved build directory is not configured")
    runtime.prepare_harmony_build_root()
    executable = checked(storage / "ide/bin/devecostudio64.exe", repo, exists=True)
    work = checked(runtime.harmony_build_root / "original", runtime.harmony_build_root, exists=True)
    env = process_environment(storage)
    env["DEVECO_SDK_HOME"] = str(runtime.harmony_build_root / "sdk")
    # Keep launcher descendants in the existing Job for a finite UI inspection;
    # the native manager closes that Job when this worker returns or is stopped.
    log = checked(storage / "logs/ide-launch.txt", repo)
    with log.open("wb") as output:
        process = subprocess.Popen([str(executable), str(work)], cwd=work, env=env,
                                   stdout=output, stderr=subprocess.STDOUT, shell=False)
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            if process.poll() not in (None, 0):
                raise ValueError("IDE launcher failed; inspect ide-launch.txt")
            time.sleep(1)
    return {"scope": "ide_inspection_only", "application_run": False}


def run(repo: Path, step: str) -> dict[str, Any]:
    require_managed_worker(repo, "harmony-prepare")
    handlers = {"inspect": inspect_package, "extractor": prepare_extractor,
                "payload": prepare_payloads, "extract": extract_package, "tools": configure_and_probe_tools,
                "emulator-info": emulator_information, "emulator-license": accept_emulator_license,
                "build-evidence": export_build_evidence,
                "ide-inspect": inspect_ide,
                "build-original": lambda root: build_application(root, "original"),
                "build-patched": lambda root: build_application(root, "patched")}
    if step not in handlers:
        raise ValueError("unsupported app preparation step")
    try:
        return handlers[step](repo)
    except Exception as error:
        print(f"office demo {step}: {type(error).__name__}: {error}", flush=True)
        raise
