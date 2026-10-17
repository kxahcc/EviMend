"""Execution facts and compatibility are checked without touching the real checkout."""

from __future__ import annotations

import copy
import hashlib
import io
import os
import subprocess
from pathlib import Path
from typing import Any, Self

import pytest

from dfx_agent import provenance

_ORIGINAL_GIT_FACTS = provenance._git_facts


def test_current_managed_environment_can_freeze_execution_facts() -> None:
    """Exercise real installed metadata before a cloud run can spend anything."""
    record = provenance.capture_execution_provenance()
    assert provenance.validate_execution_provenance(record) == record
    assert record["runtime"]["distributions"]["deepagents"]


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for relative in (*provenance._HOST_SCRIPTS, "src/dfx_agent/__init__.py",
                     "src/dfx_agent/provenance.py", *provenance._INPUTS):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((relative + "\n").encode())
    (tmp_path / "src/dfx_agent/README.md").write_text("nonexecuted explanation", encoding="utf-8")
    monkeypatch.setattr(provenance, "_ROOT", tmp_path)
    monkeypatch.setattr(provenance, "_SOURCE_BASELINE", provenance._sha(provenance._inventory()))
    monkeypatch.setattr(provenance, "_runtime_facts", lambda: {
        "python": {"implementation": "cpython", "version": "3.13.5", "cache_tag": "cpython-313",
                   "platform": "win32", "machine": "AMD64"},
        "distributions": {"deepagents": "0.7.18", "pydantic": "2.11.7", "httpx": "0.28.1",
                          "langchain-core": "1.0.0", "langgraph": "1.0.0",
                          "langgraph-checkpoint-sqlite": "2.0.0"},
    })
    monkeypatch.setattr(provenance, "_git_facts", lambda: {
        "available": False, "commit": None, "dirty": None, "status_sha256": None,
        "status_entries": None,
    })
    return tmp_path


def test_capture_hashes_exact_bytes_and_has_no_paths_or_times(host: Path) -> None:
    record = provenance.capture_execution_provenance()
    assert provenance.validate_execution_provenance(record) == record
    assert provenance._seal(record) == record
    assert record["repository_inputs"]["uv.lock"] == {
        "size_bytes": len(b"uv.lock\n"), "sha256": hashlib.sha256(b"uv.lock\n").hexdigest(),
    }
    assert [item["path"] for item in record["source_inventory"]] == sorted([
        *provenance._HOST_SCRIPTS, "src/dfx_agent/__init__.py", "src/dfx_agent/provenance.py",
    ])
    assert "README.md" not in str(record)
    assert str(host) not in str(record)
    assert "mtime" not in str(record)
    assert record["git"]["commit"] is None
    assert record["runtime"]["distributions"]["deepagents"] == "0.7.18"


def test_validation_returns_independent_exact_record(host: Path) -> None:
    record = provenance.capture_execution_provenance()
    validated = provenance.validate_execution_provenance(record)
    validated["runtime"]["distributions"]["deepagents"] = "999"
    assert record["runtime"]["distributions"]["deepagents"] == "0.7.18"


@pytest.mark.parametrize("key", ["source_inventory_sha256", "compatibility_sha256", "provenance_sha256"])
def test_each_hash_tier_is_verified(host: Path, key: str) -> None:
    record = provenance.capture_execution_provenance()
    record[key] = "0" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        provenance.validate_execution_provenance(record)


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(schema_version=True),
    lambda value: value.update(extra="not accepted"),
    lambda value: value["source_inventory"][0].update(size_bytes=True),
    lambda value: value["source_inventory"][0].update(size_bytes=-1),
    lambda value: value["source_inventory"][0].update(sha256="A" * 64),
    lambda value: value["source_inventory"][0].update(extra="not accepted"),
    lambda value: value["repository_inputs"].pop("uv.lock"),
    lambda value: value["repository_inputs"].update(secret={"sha256": "0" * 64, "size_bytes": 1}),
    lambda value: value["runtime"]["python"].update(version="3.13"),
    lambda value: value["runtime"]["python"].update(machine="path\ncredential"),
    lambda value: value["runtime"]["python"].update(extra=True),
    lambda value: value["runtime"]["distributions"].update(DeepAgents="0.7.18"),
    lambda value: value["runtime"]["distributions"].update(deepagents=1),
    lambda value: value["runtime"]["distributions"].clear(),
    lambda value: value["git"].update(available=1),
    lambda value: value["git"].update(commit="1" * 40),
])
def test_strict_record_rejects_even_resealed_invalid_facts(host: Path, mutation: Any) -> None:
    record = provenance.capture_execution_provenance()
    mutation(record)
    with pytest.raises(ValueError):
        provenance.validate_execution_provenance(provenance._seal(record))


@pytest.mark.parametrize("path", ["../host.py", "src/dfx_agent/../x.py", "src\\dfx_agent\\x.py",
                                  "src/dfx_agent/__pycache__/x.py", "src/other/x.py",
                                  "src/dfx_agent/x.PY", "C:/host.py"])
def test_inventory_paths_are_canonical_scoped_sources(host: Path, path: str) -> None:
    record = provenance.capture_execution_provenance()
    record["source_inventory"].append({"path": path, "size_bytes": 1, "sha256": "1" * 64})
    record["source_inventory"].sort(key=lambda item: item["path"])
    with pytest.raises(ValueError):
        provenance.validate_execution_provenance(provenance._seal(record))


def test_inventory_rejects_case_collisions_and_unsorted_order(host: Path) -> None:
    record = provenance.capture_execution_provenance()
    record["source_inventory"].append({"path": "src/dfx_agent/PROVENANCE.py", "size_bytes": 1,
                                        "sha256": "1" * 64})
    record["source_inventory"].sort(key=lambda item: item["path"])
    with pytest.raises(ValueError, match="inventory scope"):
        provenance.validate_execution_provenance(provenance._seal(record))
    record = provenance.capture_execution_provenance()
    record["source_inventory"].reverse()
    with pytest.raises(ValueError, match="inventory scope"):
        provenance.validate_execution_provenance(provenance._seal(record))


def test_missing_host_script_cannot_be_resealed_away(host: Path) -> None:
    record = provenance.capture_execution_provenance()
    record["source_inventory"] = record["source_inventory"][1:]
    with pytest.raises(ValueError, match="inventory scope"):
        provenance.validate_execution_provenance(provenance._seal(record))


def test_session_old_provenance_stays_unknown_and_new_record_is_required(host: Path) -> None:
    record = provenance.capture_execution_provenance()
    assert provenance.validate_session_provenance({"schema_version": 8}) is None
    assert provenance.validate_session_provenance({"schema_version": 9,
                                                    "execution_provenance": record}) == record
    assert provenance.validate_session_provenance({"schema_version": 10,
                                                    "execution_provenance": record}) == record
    for value in ({"schema_version": 8, "execution_provenance": None},
                  {"schema_version": 8, "execution_provenance": record},
                  {"schema_version": 9}, {"schema_version": 9, "execution_provenance": None},
                  {"schema_version": 10}, {"schema_version": 10, "execution_provenance": None},
                  {"schema_version": 7}, {"schema_version": True}, []):
        with pytest.raises(ValueError):
            provenance.validate_session_provenance(value)


@pytest.mark.parametrize("relative", ["uv.lock", "pyproject.toml", ".python-version",
                                      "configs/local-runtime.json"])
def test_each_repository_input_drift_rejects_resume(host: Path, relative: str) -> None:
    saved = provenance.capture_execution_provenance()
    (host / relative).write_bytes(b"different fixed input\n")
    with pytest.raises(ValueError, match="incompatible"):
        provenance.ensure_execution_compatible(saved)
    assert provenance.validate_execution_provenance(saved) == saved


@pytest.mark.parametrize("area", ["python", "distribution"])
def test_actual_runtime_drift_rejects_resume(host: Path, monkeypatch: pytest.MonkeyPatch,
                                           area: str) -> None:
    saved = provenance.capture_execution_provenance()
    changed = copy.deepcopy(saved["runtime"])
    if area == "python":
        changed["python"]["version"] = "3.13.6"
    else:
        changed["distributions"]["deepagents"] = "0.7.19"
    monkeypatch.setattr(provenance, "_runtime_facts", lambda: changed)
    with pytest.raises(ValueError, match="incompatible"):
        provenance.ensure_execution_compatible(saved)


def test_changed_source_fails_import_baseline_before_new_record(host: Path) -> None:
    saved = provenance.capture_execution_provenance()
    source = host / "src/dfx_agent/__init__.py"
    source.write_text("different imported source", encoding="utf-8")
    with pytest.raises(ValueError, match="since provenance import"):
        provenance.capture_execution_provenance()
    with pytest.raises(ValueError, match="since provenance import"):
        provenance.ensure_execution_compatible(saved)


def test_untracked_source_is_bound_and_new_source_drift_rejects(host: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    extra = host / "src/dfx_agent/untracked.py"
    extra.write_bytes(b"synthetic extra module")
    with pytest.raises(ValueError, match="since provenance import"):
        provenance.verify_host_baseline()
    monkeypatch.setattr(provenance, "_SOURCE_BASELINE", provenance._sha(provenance._inventory()))
    saved = provenance.capture_execution_provenance()
    assert "src/dfx_agent/untracked.py" in [item["path"] for item in saved["source_inventory"]]
    extra.unlink()
    monkeypatch.setattr(provenance, "_SOURCE_BASELINE", provenance._sha(provenance._inventory()))
    with pytest.raises(ValueError, match="incompatible"):
        provenance.ensure_execution_compatible(saved)


def test_git_only_difference_keeps_compatibility_and_initial_record(host: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    initial = {"available": True, "commit": "1" * 40, "dirty": True,
               "status_sha256": hashlib.sha256(b" M private-name.py\0").hexdigest(),
               "status_entries": 1}
    monkeypatch.setattr(provenance, "_git_facts", lambda: initial)
    saved = provenance.capture_execution_provenance()
    original = copy.deepcopy(saved)
    monkeypatch.setattr(provenance, "_git_facts", lambda: {
        "available": True, "commit": "2" * 40, "dirty": False,
        "status_sha256": hashlib.sha256(b"").hexdigest(), "status_entries": 0,
    })
    current = provenance.capture_execution_provenance()
    provenance.ensure_execution_compatible(saved)
    assert current["compatibility_sha256"] == saved["compatibility_sha256"]
    assert current["provenance_sha256"] != saved["provenance_sha256"]
    assert saved == original
    assert "private-name.py" not in str(saved)


def test_source_hardlink_is_rejected(host: Path) -> None:
    source = host / "src/dfx_agent/__init__.py"
    os.link(source, host / "linked.py")
    with pytest.raises(ValueError, match="not independent"):
        provenance.capture_execution_provenance()


def test_source_symlink_is_rejected_without_following(host: Path) -> None:
    target = host / "target.py"
    target.write_bytes(b"must not be included")
    try:
        (host / "src/dfx_agent/linked.py").symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")
    with pytest.raises(ValueError, match="linked"):
        provenance.capture_execution_provenance()


def test_unsupported_binary_code_is_rejected(host: Path) -> None:
    (host / "src/dfx_agent/hidden.pyd").write_bytes(b"not executed")
    with pytest.raises(ValueError, match="unsupported code"):
        provenance.capture_execution_provenance()


def test_unknown_source_adjunct_is_rejected_instead_of_omitted(host: Path) -> None:
    (host / "src/dfx_agent/hidden.json").write_bytes(b'{"execution_setting": true}')
    with pytest.raises(ValueError, match="unregistered adjunct"):
        provenance.capture_execution_provenance()


def test_regular_bytecode_cache_is_ignored_without_changing_source_inventory(host: Path) -> None:
    saved = provenance.capture_execution_provenance()
    cache = host / "src/dfx_agent/__pycache__"
    cache.mkdir()
    (cache / "module.cpython-313.pyc").write_bytes(b"synthetic compiled cache")
    assert provenance.capture_execution_provenance() == saved


def test_missing_or_oversized_repository_input_rejects(host: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    (host / "uv.lock").unlink()
    with pytest.raises(ValueError, match="missing or inaccessible"):
        provenance.capture_execution_provenance()
    (host / "uv.lock").write_bytes(b"large for this fixture")
    monkeypatch.setattr(provenance, "_MAX_FILE_BYTES", 1)
    with pytest.raises(ValueError, match="exceeds its bound"):
        provenance.capture_execution_provenance()


def test_input_changes_during_capture_reject_before_return(host: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    original = provenance._runtime_facts

    def changed_runtime() -> dict[str, Any]:
        (host / "uv.lock").write_bytes(b"changed during capture")
        return original()

    monkeypatch.setattr(provenance, "_runtime_facts", changed_runtime)
    with pytest.raises(ValueError, match="changed while provenance was captured"):
        provenance.capture_execution_provenance()


def test_installed_distributions_are_normalized_and_duplicate_identity_rejects(
        monkeypatch: pytest.MonkeyPatch) -> None:
    class Distribution:
        def __init__(self, name: str, version: str) -> None:
            self.metadata = {"Name": name}
            self.version = version

    monkeypatch.setattr(provenance.importlib.metadata, "distributions", lambda: [
        Distribution("DeepAgents", "0.7.18"), Distribution("langchain_core", "1.0.0"),
    ])
    assert provenance._runtime_facts()["distributions"] == {
        "deepagents": "0.7.18", "langchain-core": "1.0.0",
    }
    monkeypatch.setattr(provenance.importlib.metadata, "distributions", lambda: [
        Distribution("same_name", "1"), Distribution("same-name", "2"),
    ])
    with pytest.raises(ValueError, match="duplicated"):
        provenance._runtime_facts()


def test_git_capture_hashes_status_and_does_not_store_names(host: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provenance, "_git_facts", _ORIGINAL_GIT_FACTS)
    monkeypatch.setattr(provenance.shutil, "which", lambda _: "git")
    status = b" M private-source.py\0R  new-private.py\0old-private.py\0"
    monkeypatch.setattr(provenance, "_git_output", lambda _, arguments:
                        b"a" * 40 + b"\n" if arguments[0] == "rev-parse" else status)
    facts = provenance._git_facts()
    assert facts == {"available": True, "commit": "a" * 40, "dirty": True,
                     "status_sha256": hashlib.sha256(status).hexdigest(), "status_entries": 2}
    assert "private-source.py" not in str(facts)


@pytest.mark.parametrize("failure", ["missing", "query", "malformed"])
def test_unavailable_git_is_unknown_not_fabricated_clean(host: Path,
                                                       monkeypatch: pytest.MonkeyPatch,
                                                       failure: str) -> None:
    monkeypatch.setattr(provenance, "_git_facts", _ORIGINAL_GIT_FACTS)
    if failure == "missing":
        monkeypatch.setattr(provenance.shutil, "which", lambda _: None)
    else:
        monkeypatch.setattr(provenance.shutil, "which", lambda _: "git")

        def query(_: str, arguments: list[str]) -> bytes:
            if failure == "query":
                raise ValueError("query unavailable")
            return b"a" * 40 if arguments[0] == "rev-parse" else b"malformed"

        monkeypatch.setattr(provenance, "_git_output", query)
    assert provenance._git_facts() == {
        "available": False, "commit": None, "dirty": None, "status_sha256": None,
        "status_entries": None,
    }


@pytest.mark.parametrize("mode", ["normal", "oversized", "timeout", "nonzero"])
def test_git_query_limits_and_disables_side_effect_options(host: Path,
                                                         monkeypatch: pytest.MonkeyPatch,
                                                         mode: str) -> None:
    calls: dict[str, Any] = {}
    monkeypatch.setattr(provenance, "_GIT_OUTPUT_BYTES", 16)
    monkeypatch.setenv("GIT_DIR", str(host / "another-repository"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(host / "another-index"))
    monkeypatch.setenv("GIT_TRACE", str(host / "should-not-create-log"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "should-not-execute")

    class Process:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            calls.update(command=command, **kwargs)
            self.stdout = io.BytesIO(b"x" * (17 if mode == "oversized" else 2))
            self.returncode = 1 if mode == "nonzero" else 0
            self.waits = 0

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            self.stdout.close()

        def kill(self) -> None:
            calls["killed"] = True
            self.returncode = -1

        def wait(self, timeout: int) -> int:
            self.waits += 1
            if mode == "timeout" and self.waits == 1:
                raise subprocess.TimeoutExpired("synthetic git", timeout)
            return self.returncode

    monkeypatch.setattr(provenance.subprocess, "Popen", Process)
    if mode == "normal":
        assert provenance._git_output("git", ["status"]) == b"xx"
    else:
        with pytest.raises(ValueError):
            provenance._git_output("git", ["status"])
    assert calls["cwd"] == host
    assert calls["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert calls["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
    assert calls["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert {key for key in calls["env"] if key.upper().startswith("GIT_")} == {
        "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT",
    }
    assert not (host / "should-not-create-log").exists()
    assert "core.fsmonitor=false" in calls["command"]
    assert "core.hooksPath=" + os.devnull in calls["command"]
    assert calls["stdin"] == subprocess.DEVNULL
    assert calls["stderr"] == subprocess.DEVNULL
    if mode in {"oversized", "timeout"}:
        assert calls["killed"] is True
