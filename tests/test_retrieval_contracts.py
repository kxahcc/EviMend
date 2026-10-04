"""Retrieval records reject misleading arithmetic and preserve unresolved guidance."""

import copy
import hashlib
import json

import pytest
from pydantic import ValidationError

from dfx_agent.knowledge_contracts import (
    MAX_CLOSURE_BYTES,
    MAX_SNAPSHOT_ENTRIES,
    ExperienceContent,
    ExperiencePremise,
    LimitedValidationScope,
    PermissionScope,
    ProcedureStep,
    RetrieverSpec,
    SolverProjection,
    content_hash,
    encode,
)
from dfx_agent.retrieval_contracts import (
    MAX_QUERY_TERMS,
    MAX_SQL_BYTES,
    IndexDescriptor,
    MemoryQuery,
    RetrievalHit,
    RetrievalPayload,
    RetrievalRecord,
    query_char_count,
    query_tags,
    query_terms,
)

HASH = "a" * 64
OTHER_HASH = "b" * 64


def projection():
    return SolverProjection(
        guidance=ExperienceContent(
            tags=("buffer",), trigger="A container overflow is observed.",
            premises=(ExperiencePremise(premise_id="units", statement="The relevant units remain unknown.",
                                       unknown_question="Which interface defines the unit?"),),
            procedure=(ProcedureStep(step_id="read", instruction="Read the unit definition.", branches=()),),
            preserves=("Preserve encoded output.",), counterexamples=("Do not truncate output.",),
            exit_conditions=("Stop when required information is unavailable.",)),
        derivation_authority="host_curated",
        validation_scopes=(LimitedValidationScope(level="none", result="unknown", scope_names=()),))


def query(**changes):
    return MemoryQuery(**{"snapshot_sha256": HASH, "scope": PermissionScope(split="development"),
                          "tags": (" Buffer ",), "text": "OVERflow? Buffer buffer", **changes})


def hit(digest=HASH, **changes):
    view = projection()
    return RetrievalHit(**{"entry_id": "experience-" + digest, "content_sha256": digest,
                           "projection_sha256": content_hash(view), "projection": view,
                           "score": 3, "matched_tags": ("buffer",), "matched_terms": ("overflow",), **changes})


def descriptor(**changes):
    return IndexDescriptor(**{"snapshot_sha256": HASH, "sqlite_sha256": OTHER_HASH,
                              "sqlite_bytes": 4096, "indexed_entries": 2,
                              "rows_sha256": HASH, "retriever_sha256": content_hash(RetrieverSpec()), **changes})


def payload(**changes):
    return RetrievalPayload(**{"query": query(), "index_sha256": content_hash(descriptor()),
                               "retriever": RetrieverSpec(max_results=1), "candidate_count": 2,
                               "positive_count": 1, "hits": (hit(),), "next_offset": 1,
                               "source_closure_bytes": 100, "index_bytes": 4096, **changes})


def record(value=None):
    value = value if value is not None else payload()
    digest = content_hash(value)
    return RetrievalRecord(record_id="retrieval-" + digest, content_sha256=digest, payload_json=encode(value))


def replace_json(model, **changes):
    value = model.model_dump(mode="json")
    value.update(changes)
    return type(model).model_validate_json(encode(value))


def test_query_preserves_raw_input_and_normalizes_only_matching_spelling():
    value = query(tags=(" Buffer ", "Straße", "unknown-domain"))
    assert value.tags == (" Buffer ", "Straße", "unknown-domain")
    assert query_tags(value) == ("buffer", "strasse", "unknown-domain")
    assert query_char_count(value) == len(value.text) + sum(map(len, value.tags))
    assert query_terms(value.text) == ("buffer", "overflow")
    assert query_terms("甲乙 β B_2 b_2!? Ａ") == ("b_2", "β", "甲乙", "ａ")
    assert content_hash(value) != content_hash(query(tags=("buffer", "strasse", "unknown-domain")))


@pytest.mark.parametrize("tags", [("buffer", " Buffer "), ("Straße", "STRASSE"), (" ",), ("ß" * 128,)])
def test_query_rejects_duplicate_or_unrepresentable_normalized_tags(tags):
    with pytest.raises(ValidationError):
        query(tags=tags)


@pytest.mark.parametrize("field,value", [
    ("snapshot_sha256", "A" * 64), ("snapshot_sha256", "a" * 63), ("scope", None),
    ("tags", ["buffer"]), ("tags", ("x" * 129,)), ("tags", (7,)),
    ("tags", tuple(f"tag_{index}" for index in range(129))),
    ("text", 7), ("text", "x" * 65_537), ("offset", -1),
    ("offset", MAX_SNAPSHOT_ENTRIES + 1), ("offset", True), ("offset", "1"),
    ("limit", 0), ("limit", 21), ("limit", False), ("current_fact", "untrusted"),
], ids=lambda value: "oversize-string" if isinstance(value, str) and len(value) > 256 else None)
def test_query_has_explicit_scope_and_strict_bounded_inputs(field, value):
    with pytest.raises(ValidationError):
        query(**{field: value})


def test_query_combined_character_limit_counts_original_tag_spaces():
    value = query(tags=(" " * 127 + "x",), text="y" * 65_408)
    assert query_char_count(value) == 65_536
    with pytest.raises(ValidationError):
        query(tags=value.tags, text=value.text + "y")


def test_query_distinct_term_bound_rejects_excess_without_truncating_repetition():
    terms = tuple(f"word_{index:03d}" for index in range(MAX_QUERY_TERMS))
    value = query(tags=(), text=" ".join(terms))
    assert query_terms(value.text) == terms
    with pytest.raises(ValidationError):
        query(tags=(), text=value.text + " word_extra")
    assert query_terms(query(text="again " * 1000).text) == ("again",)


def test_empty_query_and_unknown_tags_retain_zero_score_browse_candidates():
    value = query(tags=(), text="")
    zero = hit(score=0, matched_tags=(), matched_terms=())
    result = payload(query=value, positive_count=0, hits=(zero,))
    assert result.candidate_count == 2 and result.hits[0].score == 0
    unknown = payload(query=query(tags=("not-in-a-vocabulary",), text=""), positive_count=0, hits=(zero,))
    assert unknown.query.tags == ("not-in-a-vocabulary",)
    assert unknown.ranking_status == "relevance_not_applicability"


@pytest.mark.parametrize("field,value", [
    ("snapshot_sha256", "A" * 64), ("sqlite_sha256", "no-hash"), ("rows_sha256", ""),
    ("retriever_sha256", "b" * 63), ("sqlite_bytes", 0), ("sqlite_bytes", MAX_SQL_BYTES + 1),
    ("sqlite_bytes", True), ("indexed_entries", -1), ("indexed_entries", MAX_SNAPSHOT_ENTRIES + 1),
    ("indexed_entries", "2"), ("latest_alias", "newest"),
])
def test_descriptor_binds_exact_index_bytes_rows_and_retriever(field, value):
    with pytest.raises(ValidationError):
        descriptor(**{field: value})


@pytest.mark.parametrize("field,value", [
    ("entry_id", "experience-" + OTHER_HASH), ("entry_id", "bad id"),
    ("content_sha256", "A" * 64), ("projection_sha256", HASH),
    ("score", -1), ("score", True), ("matched_tags", ("Buffer",)),
    ("matched_tags", (" buffer ",)), ("matched_tags", (" ",)),
    ("matched_tags", ("buffer", "buffer")), ("matched_tags", ("z", "a")),
    ("matched_tags", ["buffer"]), ("matched_terms", ("Overflow",)),
    ("matched_terms", ("two words",)), ("matched_terms", ("overflow", "overflow")),
    ("matched_terms", ("z", "a")), ("matched_terms", ["overflow"]),
    ("matched_terms", ("x" * 65_537,)), ("application_success", True),
])
def test_hit_rejects_projection_drift_and_noncanonical_matches(field, value):
    with pytest.raises(ValidationError):
        hit(**{field: value})


def test_hit_hash_covers_all_guidance_and_limited_historical_scope():
    original = hit()
    raw = original.projection.model_dump(mode="json")
    for section, field in (("guidance", "counterexamples"), ("validation_scopes", "result")):
        changed = copy.deepcopy(raw)
        if section == "guidance":
            changed[section][field][0] += " Keep the full payload."
        else:
            changed[section][0] = {"schema_version": 1, "level": "synthetic_fixture",
                                   "result": "failed", "scope_names": ["unit-boundary"]}
        view = SolverProjection.model_validate_json(encode(changed))
        with pytest.raises(ValidationError):
            hit(projection=view)
        rebound = hit(projection=view, projection_sha256=content_hash(view))
        assert content_hash(rebound) != content_hash(original)


def test_hit_term_count_is_bounded_even_if_every_term_has_valid_shape():
    terms = tuple(f"word_{index:03d}" for index in range(MAX_QUERY_TERMS + 1))
    with pytest.raises(ValidationError):
        hit(matched_terms=terms)


def test_exact_protocol_byte_limits_and_maximum_page_offset_remain_representable():
    assert descriptor(sqlite_bytes=MAX_SQL_BYTES, indexed_entries=MAX_SNAPSHOT_ENTRIES).sqlite_bytes == MAX_SQL_BYTES
    result = payload(source_closure_bytes=MAX_CLOSURE_BYTES, index_bytes=MAX_CLOSURE_BYTES)
    assert result.source_closure_bytes == result.index_bytes == MAX_CLOSURE_BYTES
    assert query(offset=MAX_SNAPSHOT_ENTRIES).offset == MAX_SNAPSHOT_ENTRIES


def test_a_long_single_term_is_not_silently_cut_to_a_shorter_match():
    term = "x" * 65_536
    result = payload(query=query(tags=(), text=term),
                     retriever=RetrieverSpec(max_results=1, max_query_chars=65_536),
                     candidate_count=1, hits=(hit(score=1, matched_tags=(), matched_terms=(term,)),),
                     next_offset=None)
    assert result.hits[0].matched_terms == (term,)


@pytest.mark.parametrize("changes", [
    {"candidate_count": -1}, {"candidate_count": MAX_SNAPSHOT_ENTRIES + 1},
    {"positive_count": -1}, {"positive_count": 3}, {"positive_count": 0},
    {"hits": ()}, {"hits": [hit()]}, {"next_offset": None}, {"next_offset": 2},
    {"source_closure_bytes": MAX_CLOSURE_BYTES + 1}, {"source_closure_bytes": -1},
    {"index_bytes": MAX_CLOSURE_BYTES + 1}, {"index_bytes": -1},
    {"index_sha256": "A" * 64}, {"ranking_status": "applicable"},
])
def test_payload_rejects_missing_pages_invented_qualifications_and_resource_overflow(changes):
    with pytest.raises(ValidationError):
        payload(**changes)


def test_page_uses_frozen_limit_and_counts_remaining_candidates_including_zero_hits():
    first = payload()
    zero = hit(OTHER_HASH, score=0, matched_tags=(), matched_terms=())
    last = payload(query=query(offset=1), hits=(zero,), next_offset=None)
    assert first.next_offset == 1 and last.next_offset is None
    past = payload(query=query(offset=MAX_SNAPSHOT_ENTRIES), hits=(), next_offset=None)
    assert past.hits == ()
    empty = payload(candidate_count=0, positive_count=0, hits=(), next_offset=None)
    assert empty.hits == ()
    with pytest.raises(ValidationError):
        payload(query=query(limit=2))
    with pytest.raises(ValidationError):
        replace_json(last, next_offset=2)


def test_explicit_lower_limit_changes_page_without_changing_retriever():
    result = payload(query=query(limit=1), retriever=RetrieverSpec(max_results=2))
    assert len(result.hits) == 1 and result.next_offset == 1
    all_results = payload(retriever=RetrieverSpec(max_results=2),
                          hits=(hit(), hit(OTHER_HASH, score=0, matched_tags=(), matched_terms=())),
                          next_offset=None)
    assert len(all_results.hits) == 2


@pytest.mark.parametrize("kind", ["duplicate", "score_order", "tie_order", "wrong_score", "foreign_tag", "foreign_term"])
def test_page_order_match_membership_and_score_arithmetic_are_consistent(kind):
    raw = payload(retriever=RetrieverSpec(max_results=2), hits=(hit(), hit(OTHER_HASH)),
                  positive_count=2, next_offset=None).model_dump(mode="json")
    if kind == "duplicate":
        raw["hits"][1] = copy.deepcopy(raw["hits"][0])
    elif kind == "score_order":
        raw["hits"][0].update(score=0, matched_tags=[], matched_terms=[])
    elif kind == "tie_order":
        raw["hits"].reverse()
    elif kind == "wrong_score":
        raw["hits"][0]["score"] += 1
    elif kind == "foreign_tag":
        raw["hits"][0]["matched_tags"] = ["unknown"]
    else:
        raw["hits"][0]["matched_terms"] = ["absent"]
    with pytest.raises(ValidationError):
        RetrievalPayload.model_validate_json(encode(raw))


def test_frozen_character_limit_counts_original_input_and_zero_weights_remain_valid():
    with pytest.raises(ValidationError):
        payload(query=query(tags=(" buffer ",), text=""), retriever=RetrieverSpec(max_results=1, max_query_chars=6))
    result = payload(retriever=RetrieverSpec(max_results=1, tag_weight=0, text_weight=0),
                     positive_count=0, hits=(hit(score=0),))
    assert result.hits[0].matched_tags == ("buffer",) and result.hits[0].score == 0


def test_structural_validation_does_not_claim_to_prove_source_rows_or_actual_matches():
    invented_match = hit(matched_terms=("unobserved",))
    result = payload(query=query(text="unobserved"), hits=(invented_match,))
    assert "unobserved" not in result.hits[0].projection.guidance.trigger
    assert result.ranking_status == "relevance_not_applicability"
    assert result.hits[0].projection.epistemic_status == "historical_guidance_not_current_evidence"
    assert result.hits[0].projection.validation_scopes[0].result == "unknown"


@pytest.mark.parametrize("model", [query(), descriptor(), hit(), payload(), record()])
def test_every_independent_retrieval_object_is_strict_frozen_schema_one(model):
    assert model.schema_version == 1
    with pytest.raises(ValidationError):
        replace_json(model, schema_version=2)
    with pytest.raises(ValidationError):
        replace_json(model, semantic_authority="trusted")
    with pytest.raises(ValidationError, match="frozen"):
        model.schema_version = 1


def test_nested_copy_and_construct_cannot_bypass_required_checks():
    invalid_query = query().model_copy(update={"tags": ("buffer", "BUFFER")})
    with pytest.raises(ValidationError):
        payload(query=invalid_query)
    invalid_projection = projection().model_copy(update={"guidance": projection().guidance.model_copy(update={"preserves": ()})})
    with pytest.raises(ValidationError):
        hit(projection=invalid_projection)
    invalid_hit = hit().model_copy(update={"projection_sha256": HASH})
    with pytest.raises(ValidationError):
        payload(hits=(invalid_hit,))
    invalid_spec = RetrieverSpec.model_construct(max_results=0)
    with pytest.raises(ValidationError):
        payload(retriever=invalid_spec)


def test_record_contents_are_independent_copies_and_full_query_is_hashed():
    original = record()
    value = original.contents()
    value["query"]["tags"].clear()
    value["hits"][0]["projection"]["guidance"]["counterexamples"].clear()
    assert original.contents()["query"]["tags"] == [" Buffer "]
    assert original.contents()["hits"][0]["projection"]["guidance"]["counterexamples"]
    changed = record(payload(query=query(tags=("buffer",))))
    assert changed.record_id != original.record_id
    with pytest.raises(ValidationError):
        replace_json(original, record_id="retrieval-" + OTHER_HASH)


@pytest.mark.parametrize("alteration", ["noncanonical", "duplicate_key", "omitted_default", "extra_field", "wrong_hash"])
def test_record_rejects_reencoded_or_invalid_content_even_with_a_recomputed_raw_hash(alteration):
    original = record()
    outer = original.model_dump(mode="json")
    raw = original.contents()
    if alteration == "noncanonical":
        outer["payload_json"] = json.dumps(raw, indent=2)
    elif alteration == "duplicate_key":
        existing = encode({"query": raw["query"]})[1:-1]
        outer["payload_json"] = "{" + existing + "," + encode(raw)[1:]
    elif alteration == "omitted_default":
        raw.pop("ranking_status")
        outer["payload_json"] = encode(raw)
    elif alteration == "extra_field":
        raw["hidden_answer"] = "unapproved"
        outer["payload_json"] = encode(raw)
    else:
        outer["content_sha256"] = OTHER_HASH
    if alteration != "wrong_hash":
        digest = hashlib.sha256(outer["payload_json"].encode("utf-8")).hexdigest()
        outer["content_sha256"] = digest
        outer["record_id"] = "retrieval-" + digest
    with pytest.raises(ValidationError):
        RetrievalRecord.model_validate(outer)


def test_record_rejects_nonfinite_and_excessive_nesting_without_accepting_the_hash():
    for raw in ('{"unapproved":NaN}', '{"deep":' + '[' * 2000 + '0' + ']' * 2000 + '}'):
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with pytest.raises(ValidationError):
            RetrievalRecord(record_id="retrieval-" + digest, content_sha256=digest, payload_json=raw)
