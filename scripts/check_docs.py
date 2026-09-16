"""Check documentation structure; semantic decision review remains a human task."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {"archive", "runs", "data_B"}


def git_paths(*arguments: str) -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", *arguments, "-z"], cwd=ROOT,
        check=True, capture_output=True,
    )
    return {p for p in result.stdout.decode("utf-8").split("\0") if p}


def check() -> tuple[list[str], list[str], int]:
    errors: list[str] = []
    notes: list[str] = []
    tracked = git_paths("--cached")
    visible = tracked | git_paths("--others", "--exclude-standard")
    for p in tracked:
        if p.split("/")[0] in EXCLUDED:
            errors.append(f"Local-only material remains tracked: {p}")
    ignore = subprocess.run(
        ["git", "check-ignore", "--no-index", "archive/__doc_check_probe__"],
        cwd=ROOT, capture_output=True,
    )
    if ignore.returncode != 0:
        errors.append("archive/ is not ignored")

    guide = (ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")
    match = re.search(
        r"<!-- document-registry:start -->(.*?)<!-- document-registry:end -->",
        guide, re.S,
    )
    if not match:
        return errors + ["Missing DEVELOPMENT document registry"], notes, 0
    rows = re.findall(r"^\| `([^`]+)` \| ([^|]+) \|", match.group(1), re.M)
    registry = {path: kind.strip() for path, kind in rows}
    if len(rows) != len(registry):
        errors.append("Duplicate registry paths")
    active = {
        p for p in visible
        if p.endswith(".md") and p.split("/")[0] not in EXCLUDED
        and (ROOT / p).is_file()
    }
    errors.extend(f"Unregistered Markdown: {p}" for p in sorted(active - registry.keys()))
    errors.extend(f"Missing/untracked registry document: {p}" for p in sorted(registry.keys() - active))

    for name, kind in registry.items():
        path = ROOT / name
        if not path.is_file() or kind == "提交模板":
            continue
        text = path.read_text(encoding="utf-8")
        dates = re.findall(r"最后维护：(\d{4}-\d{2}-\d{2})", text)
        if len(dates) != 1:
            errors.append(f"{name}: expected one maintenance date")
        else:
            try:
                date.fromisoformat(dates[0])
            except ValueError:
                errors.append(f"{name}: invalid maintenance date")
        if not re.search(r"^状态：", text, re.M):
            errors.append(f"{name}: missing document status")
        if re.search(r"^(?:>\s*)?版本：v\d|^>.*\d{4}-\d{2}-\d{2}.*更新", text, re.M):
            errors.append(f"{name}: manual document version or appended dated update")
        for target in re.findall(r"\]\(([^)]+)\)", text):
            target = target.strip("<>")
            if urlsplit(target).scheme or target.startswith("#"):
                continue
            target_path = unquote(target.split("#", 1)[0])
            if not target_path:
                continue
            resolved = (path.parent / target_path).resolve()
            if not resolved.is_relative_to(ROOT):
                errors.append(f"{name}: local link outside repository: {target}")
                continue
            relative = resolved.relative_to(ROOT)
            top = relative.parts[0] if relative.parts else ""
            if top in {"archive", "runs"}:
                errors.append(f"{name}: current document depends on local-only material: {target}")
            elif not resolved.exists():
                if top == "data_B":
                    notes.append(f"Optional local dataset link unavailable: {target}")
                else:
                    errors.append(f"{name}: broken local link: {target}")
    return errors, notes, len(registry)


if __name__ == "__main__":
    failures, notes, count = check()
    for item in failures:
        print("ERROR:", item)
    for item in sorted(set(notes)):
        print("NOTE:", item)
    print(f"Documents: {count}; errors: {len(failures)}; optional notes: {len(set(notes))}")
    raise SystemExit(bool(failures))
