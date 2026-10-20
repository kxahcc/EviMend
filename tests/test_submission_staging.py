"""Audit selected submission copies without changing any frozen run or using a model."""
from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from dfx_agent.evidence_io import local_evidence_path

ROOT = Path(__file__).resolve().parents[1]
SUBMISSION = ROOT / "answer_B"
ENABLED = SUBMISSION.is_dir() and (SUBMISSION / "runs/exports/cases/index.json").is_file()
pytestmark = pytest.mark.skipif(not ENABLED, reason="local submission checkout is unavailable")


def _hash(path: Path) -> str:
    with local_evidence_path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _results() -> list[Path]:
    exports = ROOT / "runs/exports"
    normal = json.loads((exports / "cases/index.json").read_bytes())["cases"]
    extra = json.loads((exports / "evaluation-runs.json").read_bytes())["results"]
    return [Path("runs/exports/cases") / row["result_path"] for row in normal] + [
        Path("runs/exports") / row["result"] for row in extra
    ]


def test_submission_uses_repository_root_and_preserves_skills() -> None:
    assert not (SUBMISSION / "team50").exists()
    assert not (SUBMISSION / "wxy_test").exists()
    assert not list((SUBMISSION / "scripts").rglob("*.ps1"))
    for source in (ROOT / "skill").glob("*/SKILL.md"):
        assert _hash(source) == _hash(SUBMISSION / source.relative_to(ROOT))
    assert not (SUBMISSION / "configs/model.local.json").exists()
    assert len(_results()) == 43


@pytest.mark.parametrize("relative", _results() if ENABLED else [], ids=lambda path: str(path))
def test_selected_frozen_exports_remain_byte_identical(relative: Path) -> None:
    source, target = ROOT / relative, SUBMISSION / relative
    for name in ("summary.json", "root-cause.json", "verification.json", "evidence.zip", "patch.diff"):
        if (source / name).exists():
            assert _hash(source / name) == _hash(target / name), str(relative / name)
    summary = json.loads((source / "summary.json").read_bytes())
    for name, metadata in summary["files"].items():
        path = local_evidence_path(target / name)
        assert path.stat().st_size == metadata["bytes"], str(relative / name)
        assert _hash(path) == metadata["sha256"], str(relative / name)


def test_git_index_preserves_export_bytes_without_line_ending_conversion() -> None:
    staged = subprocess.run(["git", "-c", "core.longpaths=true", "ls-files", "--stage", "-z", "runs/exports"],
                            cwd=SUBMISSION, capture_output=True, check=True).stdout
    entries = []
    for value in staged.decode("utf-8").split("\0"):
        if value:
            metadata, path = value.split("\t", 1)
            entries.append((path, metadata.split()[1]))
    assert entries, "submission exports must be staged before release acceptance"
    quoted_paths = "\n".join(json.dumps(path, ensure_ascii=False) for path, _ in entries) + "\n"
    raw = subprocess.run(["git", "-c", "core.longpaths=true", "hash-object", "--no-filters", "--stdin-paths"],
                         cwd=SUBMISSION, input=quoted_paths.encode("utf-8"), capture_output=True, check=True)
    hashes = raw.stdout.decode("ascii").splitlines()
    assert len(hashes) == len(entries)
    for (path, staged_hash), original_hash in zip(entries, hashes, strict=True):
        assert staged_hash == original_hash, f"Git transformed exported evidence bytes: {path}"


def _contains(stream, secret: bytes) -> bool:
    tail = b""
    while block := stream.read(65536):
        data = tail + block
        if secret in data:
            return True
        tail = data[-max(0, len(secret) - 1):]
    return False


def test_submission_exports_do_not_contain_the_configured_private_key() -> None:
    private = ROOT / "configs/model.local.json"
    if not private.is_file():
        pytest.skip("private key is not available for exact-match local audit")
    secret = json.loads(private.read_bytes()).get("api_key", "").encode()
    if not secret:
        pytest.skip("private key is empty")
    for path in local_evidence_path(SUBMISSION / "runs/exports").rglob("*"):
        if not path.is_file():
            continue
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                for entry in archive.infolist():
                    if entry.is_dir():
                        continue
                    with archive.open(entry) as stream:
                        if _contains(stream, secret):
                            pytest.fail("Private credential found in an exported archive", pytrace=False)
        else:
            with path.open("rb") as stream:
                if _contains(stream, secret):
                    pytest.fail("Private credential found in an exported file", pytrace=False)
