"""Read-only DFX inventory. Does not execute dataset scripts or unpack archives."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import zipfile


FRAME = re.compile(r"\((?:file:)?(.+?\.(?:ets|ts|js)):(\d+):(\d+)\)")
CASE = re.compile(r"TC_(?:JSError|JSRangeError)_\d+")


def decode(data: bytes) -> tuple[str, str]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding), encoding
        except UnicodeError:
            pass
    return data.decode("utf-8", errors="replace"), "utf-8-replacement"


def inspect(root: Path) -> dict:
    root = root.resolve()
    files = sorted(p for p in root.rglob("*") if p.is_file())
    relative = lambda p: p.relative_to(root).as_posix()
    app_roots = []
    for group in (root / "data_B1", root / "data_B2"):
        app_roots.extend(p for p in group.iterdir() if (p / "AppScope").is_dir())
    b3 = root / "data_B3" / "benchmarkfordfx-better"
    app_roots.extend(p for p in (b3 / "App").iterdir() if (p / "AppScope").is_dir())
    app_roots.sort()
    app_by_name = defaultdict(list)
    for p in app_roots:
        app_by_name[p.name].append(p)

    source_by_app_name = defaultdict(lambda: defaultdict(list))
    for p in files:
        if p.suffix.lower() not in (".ets", ".ts", ".js"):
            continue
        for app in app_roots:
            if p.is_relative_to(app):
                source_by_app_name[app][p.name].append(p)
                break

    def is_framework(frame: dict) -> bool:
        path = frame["path"]
        return path.startswith(("/usr", "obj/foundation/", "internal/", "/system/")) or Path(path).name in ("stateMgmt.js", "jsXNode.js")

    def source_check(frame: dict, app: Path) -> dict:
        target = (app / frame["path"]).resolve()
        inside = target.is_relative_to(app.resolve())
        exists = inside and target.is_file()
        result = {"app": relative(app), "file_exists": exists, "external_path": not inside}
        if exists:
            text, _ = decode(target.read_bytes())
            result["line_in_range"] = 1 <= frame["line"] <= len(text.splitlines())
        else:
            candidates = source_by_app_name[app].get(Path(frame["path"]).name, [])
            result["basename_candidates"] = [p.relative_to(app).as_posix() for p in candidates]
            if len(candidates) == 1:
                text, _ = decode(candidates[0].read_bytes())
                result["unique_basename_line_in_range"] = 1 <= frame["line"] <= len(text.splitlines())
        return result

    logs = []

    def add_log(location: str, data: bytes, candidates: list[Path], kind: str) -> None:
        text, encoding = decode(data)
        lines = text.splitlines()
        headers = {}
        for line in lines:
            for field in ("Module name", "Reason", "Error name", "Error message"):
                if line.strip().startswith(field + ":"):
                    headers[field] = line.strip().split(":", 1)[1].strip()
        frames = []
        frame_lines = 0
        for line in lines:
            if not re.match(r"\s*at\s", line):
                continue
            frame_lines += 1
            match = FRAME.search(line)
            if match:
                frames.append({"path": match[1].replace("\\", "/"), "line": int(match[2]), "column": int(match[3])})
        business = next((f for f in frames if not is_framework(f)), None)
        signature = json.dumps({"error": headers.get("Error name"), "message": headers.get("Error message"), "business_frames": [f for f in frames if not is_framework(f)][:8]}, sort_keys=True)
        logs.append({
            "location": location,
            "kind": kind,
            "sha256": hashlib.sha256(data).hexdigest(),
            "encoding": encoding,
            "case_ids_in_path": sorted(set(CASE.findall(location))),
            "headers": headers,
            "frame_count": frame_lines,
            "first_frame": frames[0] if frames else None,
            "first_business_frame": business,
            "source_candidates": [relative(p) for p in candidates],
            "source_checks": [source_check(business, p) for p in candidates] if business else [],
            "signature_sha256": hashlib.sha256(signature.encode()).hexdigest(),
        })

    for p in files:
        if not p.name.startswith("jscrash"):
            continue
        owners = [app for app in app_roots if p.is_relative_to(app)]
        if not owners:
            case_parent = next((part for part in p.parts if CASE.match(part) and "-" in part), "")
            app_name = case_parent.split("-", 1)[-1]
            owners = app_by_name.get(app_name, [])
            if app_name == "MoneyTrack1":
                owners = owners + app_by_name.get("MoneyTrack1_2", [])
        add_log(relative(p), p.read_bytes(), owners, "file")

    archives = []
    for p in files:
        if p.suffix.lower() != ".zip":
            continue
        try:
            with zipfile.ZipFile(p) as archive:
                members = [m for m in archive.infolist() if not m.is_dir()]
                names = [m.filename for m in members]
                fault_archive = p.is_relative_to(b3 / "Logs")
                crash_members = [m for m in members if Path(m.filename).name.startswith("jscrash")]
                names_are_safe = all(not Path(n).is_absolute() and ".." not in Path(n).parts and not re.match(r"^[A-Za-z]:", n) for n in names)
                archives.append({
                    "path": relative(p), "fault_archive": fault_archive,
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                    "members": names, "crash_member_count": len(crash_members),
                    "safe_member_paths": names_are_safe,
                    "has_expected_case_root": all(n.startswith(p.stem + "/") for n in names) if fault_archive else None,
                })
                if fault_archive:
                    app_name = p.stem.split("-", 1)[-1]
                    owners = [q for q in app_by_name.get(app_name, []) if q.is_relative_to(b3)]
                    for m in crash_members:
                        if m.file_size > 10_000_000:
                            raise ValueError(f"Unexpectedly large crash member in {relative(p)}")
                        add_log(relative(p) + "!" + m.filename, archive.read(m), owners, "zip_member")
        except zipfile.BadZipFile:
            archives.append({"path": relative(p), "invalid_zip": True})

    cases = []
    for app in app_roots:
        if app.is_relative_to(b3):
            continue
        for folder in sorted(p for p in app.iterdir() if p.is_dir() and CASE.match(p.name)):
            cases.append({"key": relative(folder), "case_id": CASE.match(folder.name)[0], "app": relative(app), "origin": "fault_directory", "log_count": sum(log["location"].startswith(relative(folder) + "/") for log in logs)})
    readme = (b3 / "README.md").read_text(encoding="utf-8-sig")
    for match in re.finditer(r"^\|\s*(TC_JSError_\d+)\s*\|\s*([^|]+?)\s*\|", readme, re.MULTILINE):
        case_id, name = match.groups()
        prefix = f"{case_id}-{name}"
        matched = [a for a in archives if a.get("fault_archive") and Path(a["path"]).stem == prefix]
        cases.append({"key": f"data_B3/{prefix}", "case_id": case_id, "app": relative(b3 / "App" / name), "origin": "readme_and_archive_check", "archive_present": bool(matched), "log_count": sum(a["crash_member_count"] for a in matched), "test_script_present": (b3 / "HypiumProject" / "testcases" / (case_id + ".py")).is_file()})

    duplicates = defaultdict(list)
    for log in logs:
        duplicates[log["sha256"]].append(log["location"])
    owned = [log for log in logs if len(log["source_candidates"]) == 1 and log["first_business_frame"]]
    summary = {
        "files": len(files), "bytes": sum(p.stat().st_size for p in files),
        "ets_files": sum(p.suffix == ".ets" for p in files),
        "app_roots": len(app_roots), "distinct_app_directory_names": len(app_by_name),
        "candidate_case_variants": len(cases), "case_variants_with_any_crash_artifact": sum(c["log_count"] > 0 for c in cases),
        "crash_artifacts_including_duplicates": len(logs), "unique_crash_bytes": len(duplicates),
        "error_names_by_artifact_not_case": dict(Counter(log["headers"].get("Error name", "missing") for log in logs)),
        "encodings": dict(Counter(log["encoding"] for log in logs)),
        "crashes_with_business_source_location": sum(log["first_business_frame"] is not None for log in logs),
        "single_owner_logs_with_business_frame": len(owned),
        "single_owner_first_business_file_exists": sum(log["source_checks"][0]["file_exists"] for log in owned),
        "single_owner_first_business_line_in_range": sum(log["source_checks"][0].get("line_in_range", False) for log in owned),
        "single_owner_unique_basename_fallback_line_in_range": sum(log["source_checks"][0].get("unique_basename_line_in_range", False) for log in owned),
        "archives": len(archives), "model_repair_runs": 0, "device_replays": 0,
    }
    return {
        "schema_version": 1, "status": "read_only_inventory_not_repair_evaluation",
        "input_root": root.name,
        "summary": summary,
        "limitations": ["Case variants and repeated logs are not independent defects", "A matching file and line range do not prove source-version alignment or root cause", "Construction docs and baseline resources are analyst/evaluator material, not clean solver input", "No dataset Python scripts or application code were executed"],
        "apps": [relative(p) for p in app_roots], "cases": cases,
        "archives": archives, "logs": logs,
        "duplicate_byte_groups": [v for v in duplicates.values() if len(v) > 1],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data_B"))
    parser.add_argument("--output", type=Path, default=Path("resources/preparation/data-b-inventory.json"))
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.root.resolve()):
        parser.error("Write the inventory outside the original dataset")
    result = inspect(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
