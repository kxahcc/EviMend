"""Bounded raw materials survive deduplication and crashes without gaining authority."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dfx_agent import working_context_materials as materials
from dfx_agent.context_compaction import (
    MAX_ARTIFACT_BYTES,
    CompactionPolicy,
    CompactionStore,
)
from dfx_agent.task import digest
from dfx_agent.working_context import WorkingContextError
from dfx_agent.working_context_materials import WorkingContextMaterials

PROFILE = {"transport": "fixed_response", "provider_id": "dfxfixed", "model_id": "offline"}


def store(root: Path) -> WorkingContextMaterials:
    root.mkdir(exist_ok=True)
    return WorkingContextMaterials(CompactionStore(root, "task1", "run1", CompactionPolicy(), PROFILE))


def blob_path(value: WorkingContextMaterials, raw: bytes) -> Path:
    return value.directory / (digest(raw) + ".blob")


def snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_construction_and_empty_audit_are_readonly(tmp_path: Path) -> None:
    value = store(tmp_path)
    before = list(tmp_path.iterdir())
    assert value.audit_namespace() == {
        "unique_blob_count": 0, "unique_blob_bytes": 0, "pending_count": 0, "pending_bytes": 0,
    }
    assert list(tmp_path.iterdir()) == before and not value.directory.exists()
    assert value.resolve({}) == {}
    assert not value.directory.exists()


@pytest.mark.parametrize("raw", [b"", b"original bytes\x00\xff\xfe", "保留原始字节\n".encode()])
def test_exact_raw_bytes_and_repeat_put_are_immutable(tmp_path: Path, raw: bytes) -> None:
    value = store(tmp_path)
    ref = value.put(raw)
    path = blob_path(value, raw)
    before = path.stat()
    assert ref == {"sha256": digest(raw), "bytes": len(raw)}
    assert path.read_bytes() == raw and value.read(ref) == raw
    assert value.put(raw) == ref
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert path.stat().st_ino == before.st_ino and path.stat().st_nlink == 1
    assert value.audit_namespace() == {
        "unique_blob_count": 1, "unique_blob_bytes": len(raw), "pending_count": 0, "pending_bytes": 0,
    }


def test_baseline_candidates_and_multiple_points_share_bytes_without_losing_references(tmp_path: Path) -> None:
    value = store(tmp_path)
    unchanged = value.put(b"same source in every candidate\n")
    changed = value.put(b"candidate-only edit\n")
    manifest = {
        "workspace/base/src/a.ets": unchanged,
        "workspace/candidates/one/source/src/a.ets": unchanged,
        "workspace/candidates/two/source/src/a.ets": unchanged,
        "workspace/candidates/two/source/src/b.ets": changed,
        "task/source/src/a.ets": unchanged,
    }
    before = snapshot(tmp_path)
    first = value.resolve(manifest)
    second = store(tmp_path).resolve(manifest)
    assert first == second and len(first) == 5
    assert first["workspace/base/src/a.ets"] is first["task/source/src/a.ets"]
    assert snapshot(tmp_path) == before
    observed = value.audit_namespace()
    assert observed["unique_blob_count"] == 2
    assert observed["unique_blob_bytes"] < sum(len(raw) for raw in first.values())


def test_empty_put_manifest_does_not_create_material_namespace(tmp_path: Path) -> None:
    value = store(tmp_path)
    before = snapshot(tmp_path)
    assert value.put_manifest({}) == {}
    assert not value.directory.exists() and snapshot(tmp_path) == before


def test_put_manifest_keeps_all_aliases_and_publishes_each_unique_original_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = store(tmp_path)
    raw = b"unchanged across baseline and candidates\n"
    material = {
        "workspace/base/src/页面.ets": raw,
        "workspace/candidates/one/source/src/页面.ets": raw,
        "workspace/candidates/two/source/src/页面.ets": raw,
        "task/source/src/页面.ets": raw,
        "workspace/base/empty.ets": b"",
        "task/source/empty.ets": b"",
        "workspace/candidates/two/source/changed.ets": b"candidate-only change",
    }
    publish = value.put
    published: list[bytes] = []

    def observed_put(blob: bytes):
        published.append(blob)
        return publish(blob)

    monkeypatch.setattr(value, "put", observed_put)
    manifest = value.put_manifest(material)
    assert published == [raw, b"", b"candidate-only change"]
    assert set(manifest) == set(material) and value.resolve(manifest) == material
    assert manifest["workspace/base/src/页面.ets"] == manifest["task/source/src/页面.ets"]
    assert manifest["workspace/base/src/页面.ets"] is not manifest["task/source/src/页面.ets"]
    assert value.audit_namespace()["unique_blob_count"] == 3


def test_put_manifest_rereads_existing_blob_on_every_independent_invocation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = store(tmp_path)
    raw = b"original bytes in repeated plan points"
    material = {"workspace/base/a.ets": raw, "task/source/a.ets": raw}
    first = value.put_manifest(material)
    target = blob_path(value, raw)
    before = target.lstat()
    read_file = value._read_file
    reads: list[Path] = []

    def observed_read(path: Path, **kwargs):
        reads.append(path)
        return read_file(path, **kwargs)

    monkeypatch.setattr(value, "_read_file", observed_read)
    assert value.put_manifest(material) == first
    assert value.put_manifest(material) == first
    assert reads == [target, target]
    assert target.lstat().st_ino == before.st_ino
    assert target.lstat().st_mtime_ns == before.st_mtime_ns


def test_put_manifest_rejects_same_size_existing_blob_change_with_restored_mtime(
        tmp_path: Path) -> None:
    value = store(tmp_path)
    raw = b"original"
    material = {"workspace/base/a.ets": raw, "task/source/a.ets": raw}
    value.put_manifest(material)
    target = blob_path(value, raw)
    before = target.lstat()
    target.write_bytes(b"modified")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert target.lstat().st_size == before.st_size
    assert target.lstat().st_mtime_ns == before.st_mtime_ns
    corrupted = snapshot(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.put_manifest(material)
    assert snapshot(tmp_path) == corrupted


@pytest.mark.parametrize("component", ["above_root", "root", "directory", "blob"])
def test_material_guard_rejects_reparse_components_in_full_unresolved_chain(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str) -> None:
    value = store(tmp_path)
    raw = b"valid original"
    ref = value.put(raw)
    target = blob_path(value, raw)
    intercepted = {"above_root": tmp_path.parent, "root": tmp_path,
                   "directory": value.directory, "blob": target}[component]
    original_stat = Path.lstat

    def reparse_stat(path: Path):
        info = original_stat(path)
        if path == intercepted:
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return info

    monkeypatch.setattr(Path, "lstat", reparse_stat)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.read(ref)


@pytest.mark.parametrize("escape", ["prefix", "parent"])
def test_material_guard_rejects_component_escape_before_filesystem_access(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, escape: str) -> None:
    value = store(tmp_path)
    target = (tmp_path.with_name(tmp_path.name + "-other") / "blob" if escape == "prefix"
              else tmp_path / ".." / "blob")

    def unexpected_stat(_path: Path):
        pytest.fail("escaping paths must be rejected before filesystem access")

    monkeypatch.setattr(Path, "lstat", unexpected_stat)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value._guard(target)


@pytest.mark.parametrize("operation", ["read", "resolve", "inspect"])
def test_same_byte_parent_replacement_during_open_is_rejected_even_after_restoration(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    value = store(tmp_path)
    raw = b"exact original material"
    ref = value.put(raw)
    target = blob_path(value, raw)
    replacement = tmp_path / "replacement-materials"
    replacement.mkdir()
    (replacement / target.name).write_bytes(raw)
    backup = tmp_path / "original-materials"
    original_open = Path.open
    initial = target.lstat()
    before = snapshot(tmp_path)
    changed = False

    @contextmanager
    def swapped_open(path: Path, *args, **kwargs):
        nonlocal changed
        if path != target:
            with original_open(path, *args, **kwargs) as source:
                yield source
            return
        changed = True
        value.directory.rename(backup)
        replacement.rename(value.directory)
        try:
            with original_open(path, *args, **kwargs) as source:
                yield source
        finally:
            value.directory.rename(replacement)
            backup.rename(value.directory)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", swapped_open)
        with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
            if operation == "read":
                value.read(ref)
            else:
                getattr(value, operation)({"task/a": ref})
    assert changed and target.lstat().st_ino == initial.st_ino
    assert snapshot(tmp_path) == before and value.read(ref) == raw


@pytest.mark.parametrize("first,second", [(b"a", b"b"), (b"a", b"bb")])
def test_put_manifest_rejects_simulated_digest_collision_before_any_publication(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: bytes, second: bytes) -> None:
    value = store(tmp_path)
    monkeypatch.setattr(materials, "digest", lambda _raw: "a" * 64)

    def unexpected_put(_raw: bytes):
        pytest.fail("all exact-byte collision checks must precede publication")

    monkeypatch.setattr(value, "put", unexpected_put)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.put_manifest({"workspace/base/a.ets": first, "task/source/a.ets": second})
    assert not value.directory.exists()


@pytest.mark.parametrize("material", [None, [], b"not a manifest"])
def test_put_manifest_rejects_non_object_input_without_creating_namespace(
        tmp_path: Path, material: Any) -> None:
    value = store(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.put_manifest(material)
    assert not value.directory.exists()


@pytest.mark.parametrize("raw", [None, "text", bytearray(b"mutable"), memoryview(b"view")])
def test_put_manifest_rejects_non_bytes_after_valid_entry_before_writing(
        tmp_path: Path, raw: Any) -> None:
    value = store(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.put_manifest({"workspace/base/good.ets": b"valid first input", "task/source/bad.ets": raw})
    assert not value.directory.exists()


@pytest.mark.parametrize("reference", [
    "outside/a.ets", "workspace", "workspace/../a.ets", "task//crash.log", "task/./crash.log",
    "/workspace/a.ets", "workspace\\a.ets", "workspace/C:/a.ets", "workspace/a\x00.ets",
    "workspace/a.ets ", "workspace/a.ets.",
])
def test_put_manifest_validates_every_scope_reference_before_first_write(
        tmp_path: Path, reference: str) -> None:
    value = store(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.put_manifest({"workspace/base/good.ets": b"valid first input", reference: b"bad reference"})
    assert not value.directory.exists()


def test_put_manifest_rejects_case_collision_before_first_write(tmp_path: Path) -> None:
    value = store(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.put_manifest({"workspace/base/A.ets": b"same", "workspace/base/a.ets": b"same"})
    assert not value.directory.exists()


@pytest.mark.parametrize("capacity", ["logical_entries", "raw_bytes", "manifest_bytes"])
def test_put_manifest_checks_all_capacity_bounds_before_first_write(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capacity: str) -> None:
    value = store(tmp_path)
    material = {"workspace/base/good.ets": b"valid first input", "task/source/good.ets": b"valid first input"}
    if capacity == "logical_entries":
        monkeypatch.setattr(materials, "MAX_MATERIAL_FILES", 1)
    elif capacity == "raw_bytes":
        monkeypatch.setattr(materials, "MAX_ARTIFACT_BYTES", 128)
        material["task/source/bad.ets"] = b"x" * 129
    else:
        monkeypatch.setattr(materials, "MAX_ARTIFACT_BYTES", 128)
        material["workspace/base/" + "a" * 256] = b"x"
    with pytest.raises(WorkingContextError, match="working_context_material_limit"):
        value.put_manifest(material)
    assert not value.directory.exists()


@pytest.mark.parametrize("size", [6_400_000, MAX_ARTIFACT_BYTES])
def test_raw_large_source_fits_original_single_artifact_limit_without_base64(tmp_path: Path, size: int) -> None:
    value = store(tmp_path)
    raw = b"a" * size
    ref = value.put(raw)
    assert value.resolve({"workspace/base/large.ets": ref}) == {"workspace/base/large.ets": raw}
    assert blob_path(value, raw).stat().st_size == size
    assert value.audit_namespace()["unique_blob_bytes"] == size


@pytest.mark.parametrize("size", [0, 31, 6_400_000])
def test_material_read_requests_observed_size_and_one_growth_sentinel(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    value = store(tmp_path)
    raw = b"x" * size
    ref = value.put(raw)
    original_open = Path.open
    requests: list[int] = []

    class ObservedReader:
        def __init__(self, source):
            self.source = source

        def fileno(self):
            return self.source.fileno()

        def read(self, count: int):
            requests.append(count)
            return self.source.read(count)

    @contextmanager
    def observed_open(path: Path, *args, **kwargs):
        with original_open(path, *args, **kwargs) as source:
            yield ObservedReader(source)

    monkeypatch.setattr(Path, "open", observed_open)
    assert value.read(ref) == raw
    assert requests == [size + 1]


def test_oversize_put_rejects_before_creating_namespace(tmp_path: Path) -> None:
    value = store(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_material_limit"):
        value.put(b"x" * (MAX_ARTIFACT_BYTES + 1))
    assert not value.directory.exists()


def test_oversize_existing_blob_rejects_before_opening_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = store(tmp_path)
    ref = value.put(b"x")
    path = blob_path(value, b"x")
    with path.open("wb") as output:
        output.truncate(MAX_ARTIFACT_BYTES + 1)

    def unexpected_open(*_args, **_kwargs):
        pytest.fail("oversize material must be rejected before opening")

    monkeypatch.setattr(Path, "open", unexpected_open)
    with pytest.raises(WorkingContextError, match="working_context_material_limit"):
        value.read(ref)


@pytest.mark.parametrize("operation", ["read", "put", "audit"])
def test_corrupt_existing_blob_is_rejected_without_replacement(tmp_path: Path, operation: str) -> None:
    value = store(tmp_path)
    raw = b"original"
    ref = value.put(raw)
    path = blob_path(value, raw)
    path.write_bytes(b"modified")
    before = snapshot(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        if operation == "read":
            value.read(ref)
        elif operation == "put":
            value.put(raw)
        else:
            value.audit_namespace()
    assert snapshot(tmp_path) == before


def test_missing_reference_is_not_recreated_from_pending_or_other_run(tmp_path: Path) -> None:
    first = store(tmp_path / "first")
    second = store(tmp_path / "second")
    ref = first.put(b"private source")
    second.directory.mkdir(parents=True)
    (second.directory / ("pending-" + "a" * 32 + ".blob-part")).write_bytes(b"private source")
    before = snapshot(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        second.read(ref)
    assert snapshot(tmp_path) == before
    assert second.audit_namespace()["unique_blob_count"] == 0


def test_separate_runs_never_share_a_filesystem_link(tmp_path: Path) -> None:
    first = store(tmp_path / "first")
    second = store(tmp_path / "second")
    raw = b"identical input in two runs"
    assert first.put(raw) == second.put(raw)
    one, two = blob_path(first, raw), blob_path(second, raw)
    assert one.stat().st_nlink == two.stat().st_nlink == 1
    assert (one.stat().st_dev, one.stat().st_ino) != (two.stat().st_dev, two.stat().st_ino)
    one.write_bytes(b"modified local source")
    assert second.read({"sha256": digest(raw), "bytes": len(raw)}) == raw


@pytest.mark.parametrize("failure", ["fsync", "rename"])
def test_write_crash_preserves_bounded_pending_without_authenticating_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    value = store(tmp_path)
    raw = b"full bytes written before publication"

    def interrupted(*_args, **_kwargs):
        raise OSError("synthetic crash")

    with monkeypatch.context() as patch:
        patch.setattr(materials.os, failure, interrupted)
        with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
            value.put(raw)
    before = snapshot(tmp_path)
    assert value.audit_namespace() == {
        "unique_blob_count": 0, "unique_blob_bytes": 0, "pending_count": 1, "pending_bytes": len(raw),
    }
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.read({"sha256": digest(raw), "bytes": len(raw)})
    assert snapshot(tmp_path) == before
    restarted = store(tmp_path)
    assert restarted.put(raw) == {"sha256": digest(raw), "bytes": len(raw)}
    assert restarted.audit_namespace() == {
        "unique_blob_count": 1, "unique_blob_bytes": len(raw), "pending_count": 1, "pending_bytes": len(raw),
    }
    for path, content in before.items():
        assert (tmp_path / path).read_bytes() == content


def test_complete_orphan_and_partial_pending_are_readonly_namespace_evidence(tmp_path: Path) -> None:
    value = store(tmp_path)
    value.put(b"blob published before any plan")
    (value.directory / ("pending-" + "a" * 32 + ".blob-part")).write_bytes(b"partial")
    before = snapshot(tmp_path)
    assert value.audit_namespace() == {
        "unique_blob_count": 1, "unique_blob_bytes": 30, "pending_count": 1, "pending_bytes": 7,
    }
    assert snapshot(tmp_path) == before


def test_inspect_resolves_aliases_and_audits_orphans_and_pending_in_one_readonly_operation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = store(tmp_path)
    ref = value.put(b"shared")
    empty = value.put(b"")
    value.put(b"unreferenced")
    (value.directory / ("pending-" + "a" * 32 + ".blob-part")).write_bytes(b"partial")
    manifest = {"task/a": ref, "workspace/base/a": ref, "task/empty": empty}
    before = snapshot(tmp_path)
    original = value._read_file
    reads: list[Path] = []

    def observed_read(path: Path, **kwargs):
        reads.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(value, "_read_file", observed_read)
    material, observation = value.inspect(manifest)
    assert material == {"task/a": b"shared", "workspace/base/a": b"shared", "task/empty": b""}
    assert observation == {"unique_blob_count": 3, "unique_blob_bytes": 18, "pending_count": 1, "pending_bytes": 7}
    assert len(reads) == 4 and len(set(reads)) == 4
    assert material["task/a"] is material["workspace/base/a"]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("fault", ["missing", "size", "same_size_restored_mtime"])
def test_inspect_rejects_missing_conflicting_or_changed_reference_without_repair(
        tmp_path: Path, fault: str) -> None:
    value = store(tmp_path)
    ref = value.put(b"original")
    target = blob_path(value, b"original")
    if fault == "missing":
        target.unlink()
    elif fault == "size":
        ref = {**ref, "bytes": ref["bytes"] + 1}
    else:
        before = target.lstat()
        target.write_bytes(b"modified")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    corrupted = snapshot(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.inspect({"task/a": ref})
    assert snapshot(tmp_path) == corrupted


@pytest.mark.parametrize("mutation", ["add", "remove", "change"])
def test_inspect_rejects_namespace_drift_after_reading_referenced_blob(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    value = store(tmp_path)
    raw = b"original"
    ref = value.put(raw)
    target = blob_path(value, raw)
    original = value._read_file
    changed = False

    def observed_read(path: Path, **kwargs):
        nonlocal changed
        result = original(path, **kwargs)
        if not changed:
            changed = True
            if mutation == "add":
                blob_path(value, b"new").write_bytes(b"new")
            elif mutation == "remove":
                target.unlink()
            else:
                target.write_bytes(b"changed after audit read")
        return result

    monkeypatch.setattr(value, "_read_file", observed_read)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.inspect({"task/a": ref})
    assert changed


@pytest.mark.parametrize("operation", ["resolve", "inspect"])
def test_batched_material_read_rechecks_shared_ancestor_at_end(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    value = store(tmp_path)
    ref = value.put(b"original")
    original = value._read_file
    changed = False
    original_stat = Path.lstat

    def observed_read(path: Path, **kwargs):
        nonlocal changed
        result = original(path, **kwargs)
        if not changed:
            changed = True
        return result

    def replaced_parent(path: Path):
        info = original_stat(path)
        if changed and path == tmp_path:
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1, st_mode=info.st_mode,
                                   st_file_attributes=getattr(info, "st_file_attributes", 0))
        return info

    monkeypatch.setattr(value, "_read_file", observed_read)
    monkeypatch.setattr(Path, "lstat", replaced_parent)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        getattr(value, operation)({"task/a": ref})
    assert changed


@pytest.mark.parametrize("operation", ["resolve", "inspect"])
def test_material_batch_allows_unrelated_sibling_creation_without_changing_directory_identity(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    value = store(tmp_path)
    raw = b"original"
    ref = value.put(raw)
    original = value._read_file

    def observed_read(path: Path, **kwargs):
        result = original(path, **kwargs)
        (tmp_path.parent / "unrelated-sibling").write_bytes(b"outside the material namespace")
        return result

    monkeypatch.setattr(value, "_read_file", observed_read)
    result = getattr(value, operation)({"task/a": ref})
    assert (result if operation == "resolve" else result[0]) == {"task/a": raw}


@pytest.mark.parametrize("filename", ["unexpected.json", "A" * 64 + ".blob", "pending-short.blob-part", "nested"])
def test_unregistered_namespace_entries_are_rejected(tmp_path: Path, filename: str) -> None:
    value = store(tmp_path)
    value.put(b"valid")
    path = value.directory / filename
    if filename == "nested":
        path.mkdir()
    else:
        path.write_bytes(b"unregistered")
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.audit_namespace()
    assert path.exists()


def test_oversize_pending_is_not_ignored_or_deleted(tmp_path: Path) -> None:
    value = store(tmp_path)
    value.put(b"valid")
    path = value.directory / ("pending-" + "a" * 32 + ".blob-part")
    with path.open("wb") as output:
        output.truncate(MAX_ARTIFACT_BYTES + 1)
    with pytest.raises(WorkingContextError, match="working_context_material_limit"):
        value.audit_namespace()
    assert path.stat().st_size == MAX_ARTIFACT_BYTES + 1


@pytest.mark.parametrize("reference", [
    "outside/a.ets", "workspace", "task", "workspace/../a.ets", "task//crash.log",
    "task/./crash.log", "/workspace/a.ets", "workspace\\a.ets", "workspace/C:/a.ets",
    "workspace/a\x00.ets", "workspace/a.ets ", "workspace/a.ets.",
])
def test_manifest_scope_paths_are_normalized_and_cannot_escape(tmp_path: Path, reference: str) -> None:
    value = store(tmp_path)
    manifest = {reference: {"sha256": digest(b"x"), "bytes": 1}}
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.validate_manifest(manifest)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.resolve(manifest)
    assert not value.directory.exists()


@pytest.mark.parametrize("descriptor", [
    {"sha256": "a" * 64, "bytes": True}, {"sha256": "a" * 64, "bytes": -1},
    {"sha256": "A" * 64, "bytes": 1}, {"sha256": "a" * 64, "bytes": 1.0},
    {"sha256": "a" * 64, "bytes": 1, "path": "outside"}, {"sha256": "a" * 64},
])
def test_manifest_descriptors_are_strict_and_have_no_external_path(
        tmp_path: Path, descriptor: dict) -> None:
    value = store(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.validate_manifest({"task/crash.log": descriptor})
    assert not value.directory.exists()


def test_manifest_validation_is_pure_and_retains_unicode_references(tmp_path: Path) -> None:
    value = store(tmp_path)
    manifest = {"workspace/base/页面.ets": {"sha256": digest(b"absent"), "bytes": 6}}
    assert value.validate_manifest(manifest) is None
    assert not value.directory.exists()
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.resolve(manifest)


def test_manifest_case_collision_and_conflicting_digest_sizes_are_rejected(tmp_path: Path) -> None:
    value = store(tmp_path)
    ref = value.put(b"x")
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.validate_manifest({"workspace/base/A.ets": ref, "workspace/base/a.ets": ref})
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.resolve({"workspace/base/a.ets": ref, "workspace/base/b.ets": {**ref, "bytes": 2}})


def test_manifest_capacity_and_namespace_file_count_remain_bounded(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = store(tmp_path)
    first = value.put(b"first")
    monkeypatch.setattr(materials, "MAX_MATERIAL_FILES", 1)
    with pytest.raises(WorkingContextError, match="working_context_material_limit"):
        value.validate_manifest({"task/a": first, "task/b": first})
    with pytest.raises(WorkingContextError, match="working_context_material_limit"):
        value.put(b"second")
    assert value.read(first) == b"first" and value.audit_namespace()["unique_blob_count"] == 1


def test_failed_write_pending_keeps_a_conservative_capacity_slot(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = store(tmp_path)
    monkeypatch.setattr(materials, "MAX_MATERIAL_FILES", 1)

    def interrupted(*_args, **_kwargs):
        raise OSError("synthetic interrupted flush")

    with monkeypatch.context() as patch:
        patch.setattr(materials.os, "fsync", interrupted)
        with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
            value.put(b"pending occupies a slot")
    with pytest.raises(WorkingContextError, match="working_context_material_limit"):
        value.put(b"cannot ignore retained pending")
    with pytest.raises(WorkingContextError, match="working_context_material_limit"):
        store(tmp_path).put(b"restarted cannot ignore retained pending")
    assert value.audit_namespace()["pending_count"] == 1


def test_manifest_serialized_size_is_checked_independently_of_entry_count(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = store(tmp_path)
    monkeypatch.setattr(materials, "MAX_ARTIFACT_BYTES", 128)
    manifest = {"workspace/base/" + "a" * 256: {"sha256": digest(b"x"), "bytes": 1}}
    with pytest.raises(WorkingContextError, match="working_context_material_limit"):
        value.validate_manifest(manifest)
    assert not value.directory.exists()


@pytest.mark.parametrize("mutation", ["add", "change"])
def test_namespace_audit_rejects_growth_or_changes_to_an_already_read_blob(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    value = store(tmp_path)
    raw = b"stable original"
    value.put(raw)
    target = blob_path(value, raw)
    read_file = value._read_file
    changed = False

    def observed_read(path, **kwargs):
        nonlocal changed
        observed = read_file(path, **kwargs)
        if not changed:
            changed = True
            if mutation == "add":
                blob_path(value, b"added").write_bytes(b"added")
            else:
                target.write_bytes(b"mutated original")
        return observed

    monkeypatch.setattr(value, "_read_file", observed_read)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.audit_namespace()


def test_read_rejects_real_growth_while_file_is_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = store(tmp_path)
    raw = b"stable material"
    ref = value.put(raw)
    target = blob_path(value, raw)
    original_open = Path.open

    class GrowingReader:
        def __init__(self, source):
            self.source = source

        def fileno(self):
            return self.source.fileno()

        def read(self, size):
            observed = self.source.read(size)
            with original_open(target, "ab") as output:
                output.write(b"changed")
            return observed

    @contextmanager
    def observed_open(path, mode="r", *args, **kwargs):
        with original_open(path, mode, *args, **kwargs) as source:
            yield GrowingReader(source) if path == target and mode == "rb" else source

    monkeypatch.setattr(Path, "open", observed_open)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.read(ref)


@pytest.mark.parametrize("linked", ["parent", "directory", "blob", "pending"])
def test_symlink_materials_and_parent_directories_are_rejected(tmp_path: Path, linked: str) -> None:
    value = store(tmp_path / "run")
    outside = tmp_path / "outside"
    outside.mkdir()
    raw = b"outside bytes"
    source = outside / "source"
    source.write_bytes(raw)
    if linked in {"parent", "directory"}:
        target = value.directory.parent if linked == "parent" else value.directory
        target.parent.mkdir(exist_ok=True)
        link_source = outside
    else:
        value.directory.mkdir(parents=True)
        target = blob_path(value, raw) if linked == "blob" else (
            value.directory / ("pending-" + "a" * 32 + ".blob-part"))
        link_source = source
    try:
        target.symlink_to(link_source, target_is_directory=linked in {"parent", "directory"})
    except OSError:
        pytest.skip("current account cannot create symbolic links")
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.audit_namespace()
    if linked != "pending":
        with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
            value.put(raw)
    assert source.read_bytes() == raw


@pytest.mark.parametrize("pending", [False, True])
def test_hardlinked_blob_and_pending_are_rejected(tmp_path: Path, pending: bool) -> None:
    value = store(tmp_path / "run")
    value.directory.mkdir(parents=True)
    raw = b"shared bytes"
    outside = tmp_path / "outside"
    outside.write_bytes(raw)
    target = blob_path(value, raw) if not pending else (
        value.directory / ("pending-" + "a" * 32 + ".blob-part"))
    try:
        os.link(outside, target)
    except OSError:
        pytest.skip("filesystem cannot create a hard link")
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.audit_namespace()
    assert outside.read_bytes() == raw


def fd_stat_with_ctime(info: os.stat_result, ctime_ns: int) -> SimpleNamespace:
    return SimpleNamespace(
        st_dev=info.st_dev, st_ino=info.st_ino, st_mode=info.st_mode,
        st_size=info.st_size, st_mtime_ns=info.st_mtime_ns,
        st_ctime_ns=ctime_ns, st_nlink=info.st_nlink,
    )


def test_fd_ctime_can_differ_from_path_ctime_when_each_interface_is_stable(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = store(tmp_path)
    raw = b"different ctime meanings retain identical bytes"
    ref = value.put(raw)
    path = blob_path(value, raw)
    before = snapshot(tmp_path)
    fd_ctime = path.lstat().st_ctime_ns + 1_000_000_007
    real_fstat = os.fstat

    def observed_fstat(fd):
        return fd_stat_with_ctime(real_fstat(fd), fd_ctime)

    monkeypatch.setattr(materials.os, "fstat", observed_fstat)
    assert value.read(ref) == raw
    assert snapshot(tmp_path) == before


def test_ctime_change_between_fd_observations_is_rejected(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = store(tmp_path)
    raw = b"same bytes do not excuse changed fd metadata"
    ref = value.put(raw)
    before = snapshot(tmp_path)
    fd_ctime = blob_path(value, raw).lstat().st_ctime_ns + 1_000_000_007
    real_fstat = os.fstat
    observations = 0

    def observed_fstat(fd):
        nonlocal observations
        observations += 1
        return fd_stat_with_ctime(real_fstat(fd), fd_ctime + (observations - 1))

    monkeypatch.setattr(materials.os, "fstat", observed_fstat)
    with pytest.raises(WorkingContextError, match="working_context_material_invalid"):
        value.read(ref)
    assert snapshot(tmp_path) == before
