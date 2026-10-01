"""Literal retrieval must preserve source scope, coordinates and uncertainty."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from test_agent_loop import task_snapshot

from dfx_agent import source_search
from dfx_agent.source_search import SourceSearch
from dfx_agent.task import digest, snapshot_digest
from dfx_agent.workspace import CandidateWorkspace, TextEdit


def workspace_with_files(root: Path, files: dict[str, bytes]) -> CandidateWorkspace:
    task = task_snapshot(root)
    source = task / "source"
    for relative, raw in files.items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    manifest = [
        {"path": path.relative_to(source).as_posix(), "sha256": digest(path.read_bytes())}
        for path in sorted(source.rglob("*")) if path.is_file()
    ]
    (task / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    task_path = task / "task.json"
    task_record = json.loads(task_path.read_text(encoding="utf-8"))
    task_record["source_snapshot_sha256"] = snapshot_digest(manifest)
    task_path.write_text(json.dumps(task_record), encoding="utf-8")
    return CandidateWorkspace(task, root / "work", "run1")


def match_coordinates(result: dict) -> list[tuple[str, int, int]]:
    return [(match["path"], match["line"], match["column"]) for match in result["matches"]]


def test_large_match_set_has_a_reachable_next_page(tmp_path: Path) -> None:
    workspace = workspace_with_files(tmp_path, {"src/many.ets": b"a\n" * 100101})
    search = SourceSearch(workspace)
    page = search.search("a", path_prefix="src/many.ets", offset=100000, limit=100)
    assert page["next_offset"] == 100100 and page["has_more"] is True
    last = search.search("a", path_prefix="src/many.ets", offset=page["next_offset"], limit=100)
    assert last["matches"][0]["line"] == 100101 and len(last["matches"]) == 1
    assert last["complete"] is True and last["has_more"] is False and last["next_offset"] is None


def test_cross_file_symbol_search_returns_upstream_definition_and_consumers(tmp_path: Path) -> None:
    files = {
        "src/allocate.ets": b"export function allocateBytes(n: number) {\n  return n;\n}\n",
        "src/page.ets": b"import { allocateBytes } from './allocate';\nconst capacity = allocateBytes(count);\n",
        "src/unused.ets": b"const other = 1;\n",
    }
    workspace = workspace_with_files(tmp_path, files)
    result = SourceSearch(workspace).search("allocateBytes")
    assert match_coordinates(result) == [
        ("src/allocate.ets", 1, 17), ("src/page.ets", 1, 10), ("src/page.ets", 2, 18),
    ]
    assert result["scope"] == "baseline"
    assert result["candidate_id"] is None
    assert result["source_snapshot_sha256"] == result["snapshot_sha256"] == workspace.source_snapshot_sha256
    assert result["total_matched_lines"] == 3
    assert result["complete"] is True and result["skipped"] == []
    assert result["has_more"] is False and result["next_offset"] is None
    for match in result["matches"]:
        assert match["file_sha256"] == digest(files[match["path"]])
    assert not workspace.draft.exists()


def test_paging_is_stable_and_preserves_repeated_matching_lines(tmp_path: Path) -> None:
    workspace = workspace_with_files(tmp_path, {
        "src/a.ets": b"needle needle\nneedle\nabsent\nneedle\n",
        "src/b.ets": b"needle\nneedle\n",
    })
    search = SourceSearch(workspace)
    expected = [("src/a.ets", 1, 1), ("src/a.ets", 2, 1), ("src/a.ets", 4, 1),
                ("src/b.ets", 1, 1), ("src/b.ets", 2, 1)]
    first = search.search("needle", limit=2)
    second = search.search("needle", offset=first["next_offset"], limit=2)
    last = search.search("needle", offset=second["next_offset"], limit=2)
    assert match_coordinates(first) + match_coordinates(second) + match_coordinates(last) == expected
    assert first["next_offset"] == 2 and second["next_offset"] == 4
    assert first["has_more"] is True and second["has_more"] is True
    assert last["has_more"] is False and last["next_offset"] is None
    assert all(page["total_matched_lines"] == 5 for page in (first, second, last))
    assert first == search.search("needle", limit=2)


def test_offset_beyond_end_returns_empty_page_without_losing_total(tmp_path: Path) -> None:
    search = SourceSearch(workspace_with_files(tmp_path, {"src/a.ets": b"needle\n"}))
    result = search.search("needle", offset=100000, limit=100)
    assert result["matches"] == [] and result["total_matched_lines"] == 1
    assert result["has_more"] is False and result["next_offset"] is None


def test_unicode_crlf_columns_count_characters_and_keep_original_byte_hash(tmp_path: Path) -> None:
    raw = "标题\r\n汉😀 äbc tail\r\n  ÄBC\r\n".encode()
    search = SourceSearch(workspace_with_files(tmp_path, {"src/unicode.ets": raw}))
    sensitive = search.search("äbc")
    assert match_coordinates(sensitive) == [("src/unicode.ets", 2, 4)]
    insensitive = search.search("äbc", case_sensitive=False)
    assert match_coordinates(insensitive) == [("src/unicode.ets", 2, 4), ("src/unicode.ets", 3, 3)]
    assert [match["text"] for match in insensitive["matches"]] == ["汉😀 äbc tail", "  ÄBC"]
    assert all(match["file_sha256"] == digest(raw) for match in insensitive["matches"])
    assert insensitive["case_sensitive"] is False


def test_query_metacharacters_are_literal_not_regex(tmp_path: Path) -> None:
    query = "[a-z]+.*()"
    search = SourceSearch(workspace_with_files(tmp_path, {
        "src/expression.ets": f"literal {query}\nthis must not match\n".encode(),
    }))
    result = search.search(query)
    assert match_coordinates(result) == [("src/expression.ets", 1, 9)]
    assert result["query"] == query
    assert result["total_matched_lines"] == 1


def test_prefix_respects_directory_component_and_exact_file_boundary(tmp_path: Path) -> None:
    search = SourceSearch(workspace_with_files(tmp_path, {
        "src/sub/a.ets": b"needle\n",
        "src/sub/b.ets": b"needle\n",
        "src/submarine/a.ets": b"needle\n",
        "src/sub/a.ets.more": b"needle\n",
    }))
    directory = search.search("needle", path_prefix="src/sub")
    assert [match["path"] for match in directory["matches"]] == [
        "src/sub/a.ets", "src/sub/a.ets.more", "src/sub/b.ets",
    ]
    exact = search.search("needle", path_prefix="src/sub/a.ets")
    assert match_coordinates(exact) == [("src/sub/a.ets", 1, 1)]
    missing = search.search("needle", path_prefix="src/missing")
    assert missing["matches"] == [] and missing["total_matched_lines"] == 0
    assert missing["complete"] is True


@pytest.mark.parametrize("prefix", ["src/*", "src/?.ets", "src/[ab].ets"])
def test_glob_prefix_cannot_expand_to_other_files(tmp_path: Path, prefix: str) -> None:
    search = SourceSearch(workspace_with_files(tmp_path, {"src/a.ets": b"needle\n"}))
    try:
        result = search.search("needle", path_prefix=prefix)
    except ValueError:
        return
    assert result["matches"] == [] and result["total_matched_lines"] == 0


def test_baseline_search_ignores_mutable_draft_and_candidate_search_is_frozen(tmp_path: Path) -> None:
    workspace = workspace_with_files(tmp_path, {})
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="good")])
    search = SourceSearch(workspace)
    assert search.search("good")["matches"] == []
    candidate = workspace.register_candidate(1)
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="draft-only")])
    baseline = search.search("bad")
    frozen = search.search("good", candidate_id=candidate.candidate_id)
    assert match_coordinates(baseline) == [("src/main.ets", 1, 7)]
    assert match_coordinates(frozen) == [("src/main.ets", 1, 7)]
    assert frozen["scope"] == "candidate" and frozen["candidate_id"] == candidate.candidate_id
    assert frozen["snapshot_sha256"] == candidate.candidate_snapshot_sha256
    assert frozen["source_snapshot_sha256"] == workspace.source_snapshot_sha256
    assert frozen["snapshot_sha256"] != baseline["snapshot_sha256"]
    assert search.search("draft-only")["matches"] == []
    assert search.search("draft-only", candidate_id=candidate.candidate_id)["matches"] == []


@pytest.mark.parametrize("target", ["matched", "outside_prefix", "new_file"])
def test_baseline_tampering_is_rejected_even_outside_requested_prefix(tmp_path: Path, target: str) -> None:
    workspace = workspace_with_files(tmp_path, {"src/other.ets": b"untouched\n"})
    relative = {"matched": "src/main.ets", "outside_prefix": "src/other.ets", "new_file": "src/new.ets"}[target]
    (workspace.base / relative).write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="baseline"):
        SourceSearch(workspace).search("bad", path_prefix="src/main.ets")


def test_registered_candidate_tampering_is_rejected_before_search(tmp_path: Path) -> None:
    workspace = workspace_with_files(tmp_path, {})
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="good")])
    candidate = workspace.register_candidate(1)
    frozen = workspace.require_registered(candidate.candidate_id)
    (frozen / "src/main.ets").write_bytes(b"value=tampered\n")
    with pytest.raises(ValueError, match="changed"):
        SourceSearch(workspace).search("tampered", candidate_id=candidate.candidate_id)


def test_candidate_search_also_rejects_changed_baseline(tmp_path: Path) -> None:
    workspace = workspace_with_files(tmp_path, {})
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="good")])
    candidate = workspace.register_candidate(1)
    (workspace.base / "src/main.ets").write_bytes(b"value=changed\n")
    with pytest.raises(ValueError, match="baseline"):
        SourceSearch(workspace).search("good", candidate_id=candidate.candidate_id)


def test_unregistered_candidate_cannot_select_draft_or_external_directory(tmp_path: Path) -> None:
    workspace = workspace_with_files(tmp_path, {})
    workspace.start_draft()
    with pytest.raises((ValueError, FileNotFoundError)):
        SourceSearch(workspace).search("bad", candidate_id="0" * 64)


def test_binary_and_non_utf8_files_make_search_incomplete_not_false_no_match(tmp_path: Path) -> None:
    search = SourceSearch(workspace_with_files(tmp_path, {
        "src/nul.ets": b"needle\0data\n",
        "src/non_utf8.ets": b"needle\xff\n",
        "src/valid.ets": b"needle\n",
    }))
    result = search.search("needle")
    assert match_coordinates(result) == [("src/valid.ets", 1, 1)]
    assert result["complete"] is False
    assert {item["path"] for item in result["skipped"]} == {"src/nul.ets", "src/non_utf8.ets"}
    assert all(item["reason"] for item in result["skipped"])
    missing = search.search("absent")
    assert missing["matches"] == [] and missing["complete"] is False
    scoped = search.search("absent", path_prefix="src/valid.ets")
    assert scoped["complete"] is True and scoped["skipped"] == []


def test_long_line_snippet_covers_match_and_preserves_original_column(tmp_path: Path) -> None:
    query = "关键容量检查"
    line = "前" * 500 + query + "后" * 500
    search = SourceSearch(workspace_with_files(tmp_path, {"src/long.ets": line.encode("utf-8")}))
    match = search.search(query)["matches"][0]
    assert match["line"] == 1 and match["column"] == 501
    assert match["text_truncated"] is True
    assert query in match["text"] and len(match["text"]) <= 240
    assert match["snippet_start_column"] + match["text"].index(query) == match["column"]


def test_maximum_length_query_is_not_truncated_out_of_snippet(tmp_path: Path) -> None:
    query = "q" * 200
    line = "before" * 50 + query + "after" * 50
    search = SourceSearch(workspace_with_files(tmp_path, {"src/long.ets": line.encode()}))
    match = search.search(query)["matches"][0]
    assert query in match["text"] and len(match["text"]) <= 240
    assert match["column"] == 301 and match["text_truncated"] is True


@pytest.mark.parametrize("query", ["", " \t ", "a\nb", "a\rb", "a\0b", "x" * 201, 1, b"needle", None])
def test_invalid_query_is_rejected(tmp_path: Path, query: object) -> None:
    search = SourceSearch(workspace_with_files(tmp_path, {}))
    with pytest.raises((ValueError, TypeError)):
        search.search(query)  # type: ignore[arg-type]


@pytest.mark.parametrize("options", [
    {"offset": -1}, {"offset": source_search.MAX_TREE_BYTES + 1}, {"offset": True}, {"offset": 1.0},
    {"limit": 0}, {"limit": 101}, {"limit": False}, {"limit": "2"},
    {"case_sensitive": 1}, {"case_sensitive": "false"},
    {"path_prefix": "../data_B"}, {"path_prefix": "/tmp"},
    {"path_prefix": "C:/outside"}, {"path_prefix": "src\\main.ets"},
    {"path_prefix": "src//main.ets"}, {"path_prefix": "src/./main.ets"},
    {"path_prefix": "src/"}, {"path_prefix": ""}, {"path_prefix": 2},
    {"candidate_id": "../draft"}, {"candidate_id": ""}, {"candidate_id": "g" * 64},
])
def test_invalid_options_cannot_change_search_scope(tmp_path: Path, options: dict) -> None:
    search = SourceSearch(workspace_with_files(tmp_path, {}))
    with pytest.raises((ValueError, TypeError)):
        search.search("bad", **options)


@pytest.mark.parametrize("resource", ["file", "tree", "count"])
def test_resource_limits_refuse_partial_scan_instead_of_claiming_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource: str,
) -> None:
    workspace = workspace_with_files(tmp_path, {
        "src/a.ets": b"needle\n", "src/b.ets": b"needle\n",
    })
    constants = {"file": ("MAX_FILE_BYTES", 4), "tree": ("MAX_TREE_BYTES", 12), "count": ("MAX_FILES", 2)}
    name, bound = constants[resource]
    monkeypatch.setattr(source_search, name, bound)
    with pytest.raises(ValueError, match="limit"):
        SourceSearch(workspace).search("needle", path_prefix="src/a.ets")


@pytest.mark.parametrize("resource", ["file", "tree", "count"])
def test_candidate_search_obeys_limits_when_baseline_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource: str,
) -> None:
    workspace = workspace_with_files(tmp_path, {})
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="needle" * 6)])
    if resource == "count":
        (workspace.draft / "src/extra.ets").write_bytes(b"needle\n")
    candidate = workspace.register_candidate(1)
    constants = {"file": ("MAX_FILE_BYTES", 16), "tree": ("MAX_TREE_BYTES", 16), "count": ("MAX_FILES", 1)}
    name, bound = constants[resource]
    monkeypatch.setattr(source_search, name, bound)
    assert SourceSearch(workspace).search("bad")["complete"] is True
    with pytest.raises(ValueError, match="limit"):
        SourceSearch(workspace).search("needle", candidate_id=candidate.candidate_id)


def test_source_link_is_rejected_without_reading_external_target(tmp_path: Path) -> None:
    workspace = workspace_with_files(tmp_path, {})
    external = tmp_path / "external.ets"
    external.write_bytes(b"private needle\n")
    link = workspace.base / "src/link.ets"
    try:
        os.symlink(external, link)
    except OSError:
        pytest.skip("symlink creation unavailable on this Windows account")
    with pytest.raises(ValueError, match="link"):
        SourceSearch(workspace).search("private")
    assert external.read_bytes() == b"private needle\n"
