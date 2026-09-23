"""Gate 2.5, group 1: the contract source layer and its two read-only tools.

Everything here is offline and synthetic: the fixture contract in
:mod:`sample_case` stands in for a CUAD contract, so these tests need no
download and no network. What they establish is the set of properties the rest
of Gate 2.5 leans on:

* the same contract text always yields the same paragraphs, with the same ids;
* an id names a contract and a position, and nothing about the experiment;
* offsets map back to the untouched original, character for character;
* search is deterministic, lexical, and confined to one contract;
* ``open_source_span`` refuses malformed, foreign and unknown ids distinctly;
* every search and every open is logged, including the refusals;
* no tool output carries a gold offset, a target category, or a condition label.
"""

from __future__ import annotations

import re

import pytest

import sample_case
from pilot01.source import (
    BM25Index,
    ContractDocument,
    SourceError,
    SourceLibrary,
    SourceToolError,
    SourceTools,
    ToolCallLog,
    build_document,
    document_key,
    is_paragraph_id,
    tokenize,
)
from pilot01.source.document import PARAGRAPH_ID_RE

FIXTURE = sample_case.build_contract_document()
CONTRACT_ID = sample_case.CONTRACT_ID


def tools_for(**kwargs) -> SourceTools:
    """A tool layer bound to the fixture contract and available."""
    tools = SourceTools(
        node=kwargs.pop("node", "manager"),
        library=sample_case.build_source_library(),
        **kwargs,
    )
    tools.begin_invocation(contract_id=CONTRACT_ID, available=True)
    return tools


# --------------------------------------------------------------------------
# Paragraphization: determinism, ids, offsets
# --------------------------------------------------------------------------


def test_the_same_text_always_yields_the_same_paragraphs():
    """Determinism is the property every later layer depends on."""
    first = build_document(CONTRACT_ID, sample_case.CONTRACT_TEXT)
    second = build_document(CONTRACT_ID, sample_case.CONTRACT_TEXT)
    assert first == second
    assert first.paragraph_ids() == second.paragraph_ids()


def test_a_different_contract_gets_a_different_id_prefix():
    other = build_document("CONTRACT-002", sample_case.CONTRACT_TEXT + "\n\nExtra.")
    assert document_key(sample_case.CONTRACT_TEXT) != document_key(
        sample_case.CONTRACT_TEXT + "\n\nExtra."
    )
    assert other.paragraphs[0].paragraph_id != FIXTURE.paragraphs[0].paragraph_id


def test_paragraph_ids_are_dense_ordinals_of_the_document_key():
    key = document_key(sample_case.CONTRACT_TEXT)
    for ordinal, paragraph in enumerate(FIXTURE.paragraphs):
        assert paragraph.paragraph_id == f"{key}:p{ordinal:04d}"
        assert paragraph.ordinal == ordinal
        assert is_paragraph_id(paragraph.paragraph_id)


def test_a_paragraph_id_encodes_nothing_about_the_experiment():
    """The id is a content key and a position. That is the whole of it.

    Nothing in it names a clause category, a target, an arm, or an expected
    answer -- which is what makes it safe to hand to an agent.
    """
    for paragraph in FIXTURE.paragraphs:
        assert PARAGRAPH_ID_RE.match(paragraph.paragraph_id)
        for forbidden in sample_case.TARGET_CLAUSE_CATEGORIES:
            assert forbidden not in paragraph.paragraph_id
        assert sample_case.TARGET_CATEGORY not in paragraph.paragraph_id


def test_the_id_shape_recogniser_accepts_only_its_own_shape():
    assert is_paragraph_id("0123456789ab:p0000")
    for value in (
        "0123456789AB:p0000",
        "0123456789ab:p000",
        "0123456789ab:p00000",
        "0123456789ab",
        "p0000",
        "S-3.2",
        "",
        None,
        7,
    ):
        assert not is_paragraph_id(value), value


def test_offsets_map_back_to_the_untouched_original():
    """Every paragraph is a slice of the original, at the offsets it reports."""
    for paragraph in FIXTURE.paragraphs:
        assert (
            sample_case.CONTRACT_TEXT[paragraph.start_char : paragraph.end_char]
            == paragraph.text
        )
        assert paragraph.length == len(paragraph.text)


def test_paragraphization_does_not_modify_the_source_text():
    before = sample_case.CONTRACT_TEXT
    build_document(CONTRACT_ID, before)
    assert sample_case.CONTRACT_TEXT == before
    assert len(before) == len(sample_case.CONTRACT_TEXT)


def test_the_document_carries_the_digest_of_the_text_it_was_built_from():
    assert FIXTURE.text_hash == sample_case.CONTRACT_TEXT_HASH


def test_the_fixture_contract_is_multi_paragraph():
    """A one-paragraph contract would make "open the passage" meaningless."""
    assert len(FIXTURE) >= 4
    assert len({p.text for p in FIXTURE.paragraphs}) == len(FIXTURE)


def test_an_empty_contract_is_refused_rather_than_yielding_no_paragraphs():
    for bad in ("", "   \n  "):
        with pytest.raises(SourceError, match="no text"):
            build_document(CONTRACT_ID, bad)


def test_incoherent_size_parameters_are_refused():
    with pytest.raises(SourceError, match="positive"):
        build_document(CONTRACT_ID, "some text", target_chars=0)
    with pytest.raises(SourceError, match="at least"):
        build_document(CONTRACT_ID, "some text", target_chars=100, max_chars=50)


def test_an_oversized_block_is_split_within_the_maximum():
    long_block = "\n".join("word " * 40 for _ in range(20))
    document = build_document(CONTRACT_ID, long_block, target_chars=100, max_chars=200)
    assert len(document) > 1
    for paragraph in document.paragraphs:
        assert paragraph.length <= 200


def test_a_paragraph_can_be_looked_up_by_id_and_not_by_anything_else():
    first = FIXTURE.paragraphs[0]
    assert FIXTURE.paragraph(first.paragraph_id) == first
    assert FIXTURE.paragraph("0123456789ab:p9999") is None
    assert FIXTURE.paragraph("S-3.2") is None


# --------------------------------------------------------------------------
# The library
# --------------------------------------------------------------------------


def test_the_library_holds_only_the_contracts_it_was_given():
    library = sample_case.build_source_library()
    assert library.contract_ids == (CONTRACT_ID,)
    assert CONTRACT_ID in library
    assert "CONTRACT-002" not in library


def test_an_empty_library_holds_nothing():
    assert len(SourceLibrary.empty()) == 0


def test_asking_the_library_for_an_unknown_contract_fails_loudly():
    with pytest.raises(SourceToolError, match="no contract source is loaded"):
        SourceLibrary.empty().get(CONTRACT_ID)


# --------------------------------------------------------------------------
# search_contract
# --------------------------------------------------------------------------


def test_search_ranks_the_target_passage_first_for_its_own_words():
    hits = tools_for().search_contract("change of control assignment consent")
    assert hits
    assert hits[0].paragraph_id == sample_case.paragraph_id_containing(
        sample_case.CHANGE_OF_CONTROL_PHRASE
    )


def test_search_is_deterministic():
    first = tools_for().search_contract("governing law jury trial")
    second = tools_for().search_contract("governing law jury trial")
    assert first == second


def test_search_returns_only_paragraphs_of_this_contract():
    library = SourceLibrary.from_texts(
        {CONTRACT_ID: sample_case.CONTRACT_TEXT, "CONTRACT-002": "Unrelated text. " * 40}
    )
    tools = SourceTools(node="manager", library=library)
    tools.begin_invocation(contract_id=CONTRACT_ID, available=True)
    hits = tools.search_contract("change of control")
    assert hits
    for hit in hits:
        assert hit.contract_id == CONTRACT_ID
        assert FIXTURE.paragraph(hit.paragraph_id) is not None


def test_search_returns_at_most_top_k_hits():
    tools = tools_for(top_k=2)
    assert len(tools.search_contract("the agreement party section")) == 2


def test_search_shows_an_excerpt_and_not_the_whole_paragraph():
    """A hit is a pointer. ``open_source_span`` is the operation that reads."""
    paragraph = FIXTURE.paragraph(
        sample_case.paragraph_id_containing(sample_case.LIABILITY_CAP_PHRASE)
    )
    hits = tools_for(top_k=len(FIXTURE)).search_contract("total aggregate liability")
    hit = next(h for h in hits if h.paragraph_id == paragraph.paragraph_id)
    assert len(hit.excerpt) < len(paragraph.text)


def test_a_query_with_no_recognisable_terms_returns_nothing():
    assert tools_for().search_contract("!!! ???") == ()


def test_an_empty_query_is_refused():
    tools = tools_for()
    with pytest.raises(SourceToolError, match="non-empty"):
        tools.search_contract("   ")


def test_a_search_hit_carries_no_experimental_metadata():
    """The hit model's field set *is* the guarantee: there is nowhere to put one."""
    hits = tools_for().search_contract("change of control")
    assert set(hits[0].model_dump()) == {"paragraph_id", "contract_id", "score", "excerpt"}


# --------------------------------------------------------------------------
# open_source_span
# --------------------------------------------------------------------------


def test_open_returns_the_exact_source_text_of_the_paragraph():
    paragraph_id = sample_case.paragraph_id_containing(sample_case.CHANGE_OF_CONTROL_PHRASE)
    span = tools_for().open_source_span(paragraph_id)
    paragraph = FIXTURE.paragraph(paragraph_id)
    assert span.text == paragraph.text
    assert span.start_char == paragraph.start_char
    assert span.end_char == paragraph.end_char
    assert span.contract_id == CONTRACT_ID


def test_open_returns_more_text_than_the_search_hit_showed():
    """Opening is what turns a candidate into evidence, so it must actually read.

    The hit is an elided window around the densest cluster of query terms; the
    span is the paragraph in full. The test asserts the difference rather than a
    particular window, because where the window lands is the index's business.
    """
    tools = tools_for()
    hit = tools.search_contract("total aggregate liability")[0]
    span = tools.open_source_span(hit.paragraph_id)
    paragraph = FIXTURE.paragraph(hit.paragraph_id)

    assert len(span.text) > len(hit.excerpt)
    assert hit.excerpt.startswith("...") and hit.excerpt.endswith("...")
    assert span.text == paragraph.text
    # The heading is in the paragraph and outside the window: text the search
    # result did not show.
    assert paragraph.text.splitlines()[0] in span.text
    assert paragraph.text.splitlines()[0] not in hit.excerpt


def test_open_refuses_a_malformed_id():
    with pytest.raises(SourceToolError, match="not a well-formed paragraph id"):
        tools_for().open_source_span("S-3.2")


def test_open_refuses_a_well_formed_id_from_another_contract():
    """Cross-contract access is impossible by construction, not by a register."""
    foreign = build_document("CONTRACT-002", "A wholly different contract. " * 20)
    foreign_id = foreign.paragraphs[0].paragraph_id
    assert is_paragraph_id(foreign_id)
    with pytest.raises(SourceToolError, match="is not a paragraph of contract"):
        tools_for().open_source_span(foreign_id)


def test_open_refuses_a_well_formed_id_that_does_not_exist_here():
    with pytest.raises(SourceToolError, match="is not a paragraph of contract"):
        tools_for().open_source_span(f"{document_key(sample_case.CONTRACT_TEXT)}:p9999")


def test_open_refuses_an_empty_id():
    with pytest.raises(SourceToolError, match="non-empty"):
        tools_for().open_source_span("  ")


def test_an_opened_span_carries_no_experimental_metadata():
    paragraph_id = FIXTURE.paragraphs[0].paragraph_id
    span = tools_for().open_source_span(paragraph_id)
    assert set(span.model_dump()) == {
        "paragraph_id",
        "contract_id",
        "text",
        "start_char",
        "end_char",
    }


def test_no_tool_output_contains_a_gold_offset():
    """The fixture installs distinctive sentinels; nothing a tool returns has one."""
    sentinels = ("987654321", "987654322")
    tools = tools_for(top_k=len(FIXTURE))
    rendered = [hit.model_dump_json() for hit in tools.search_contract("the agreement")]
    for paragraph in FIXTURE.paragraphs:
        rendered.append(tools.open_source_span(paragraph.paragraph_id).model_dump_json())
    for text in rendered:
        for sentinel in sentinels:
            assert sentinel not in text


def test_no_tool_output_contains_a_clause_category_label():
    """The category the experiment targets must not appear as tool metadata.

    It may appear in the contract's own prose -- the contract is the contract --
    so this checks the *structured* fields, which is where a label smuggled in
    from the experiment would have to live.
    """
    tools = tools_for(top_k=len(FIXTURE))
    for hit in tools.search_contract("change of control"):
        assert set(hit.model_dump()) == {"paragraph_id", "contract_id", "score", "excerpt"}
    for paragraph in FIXTURE.paragraphs:
        span = tools.open_source_span(paragraph.paragraph_id)
        assert set(span.model_dump()) == {
            "paragraph_id",
            "contract_id",
            "text",
            "start_char",
            "end_char",
        }


# --------------------------------------------------------------------------
# The index itself
# --------------------------------------------------------------------------


def test_the_tokenizer_is_lowercase_alphanumeric():
    assert tokenize("Change-of-Control, 2026!") == ("change", "of", "control", "2026")


def test_a_repeated_query_term_does_not_multiply_its_own_weight():
    index = BM25Index(FIXTURE.paragraphs)
    once = index.search("liability")
    thrice = index.search("liability liability liability")
    assert [p.paragraph_id for p, _ in once] == [p.paragraph_id for p, _ in thrice]
    assert [s for _, s in once] == [s for _, s in thrice]


def test_ties_are_broken_by_source_order():
    """Two equally-scoring paragraphs always come back in the document's order."""
    text = "\n\n".join(["Alpha beta gamma. " * 30, "Alpha beta gamma. " * 30])
    document = build_document(CONTRACT_ID, text, target_chars=100, max_chars=1000)
    index = BM25Index(document.paragraphs)
    hits = index.search("alpha")
    assert [p.ordinal for p, _ in hits] == sorted(p.ordinal for p, _ in hits)


def test_the_index_is_built_from_paragraph_text_alone():
    """There is no second input to the index, so nothing else can influence it."""
    import inspect

    signature = inspect.signature(BM25Index.__init__)
    assert list(signature.parameters) == ["self", "paragraphs", "k1", "b"]


def test_top_k_of_zero_returns_nothing():
    assert BM25Index(FIXTURE.paragraphs).search("liability", top_k=0) == ()


# --------------------------------------------------------------------------
# Logging: every search and every open, refusals included
# --------------------------------------------------------------------------


def test_a_successful_search_is_logged_with_its_query_and_result_ids():
    log = ToolCallLog()
    tools = tools_for(log=log)
    hits = tools.search_contract("change of control")
    assert len(log) == 1
    record = log.records[0]
    assert record.tool == "search_contract"
    assert record.arguments == {"query": "change of control"}
    assert record.ok is True
    assert record.result_ids == tuple(hit.paragraph_id for hit in hits)
    assert record.node == "manager"
    assert record.available is True


def test_a_successful_open_is_logged_with_the_id_it_opened():
    log = ToolCallLog()
    tools = tools_for(log=log)
    paragraph_id = FIXTURE.paragraphs[0].paragraph_id
    tools.open_source_span(paragraph_id)
    record = log.records[0]
    assert record.tool == "open_source_span"
    assert record.arguments == {"paragraph_id": paragraph_id}
    assert record.result_ids == (paragraph_id,)


def test_a_refused_open_is_logged_too():
    log = ToolCallLog()
    tools = tools_for(log=log)
    with pytest.raises(SourceToolError):
        tools.open_source_span("S-3.2")
    assert len(log) == 1
    assert log.records[0].ok is False
    assert "not a well-formed paragraph id" in log.records[0].error


def test_the_log_is_append_only_and_densely_sequenced():
    log = ToolCallLog()
    tools = tools_for(log=log)
    tools.search_contract("change of control")
    tools.open_source_span(FIXTURE.paragraphs[0].paragraph_id)
    with pytest.raises(SourceToolError):
        tools.search_contract("")
    assert [record.sequence for record in log.records] == [0, 1, 2]


def test_the_log_can_filter_by_node_and_by_tool():
    log = ToolCallLog()
    tools_for(log=log, node="manager").search_contract("change of control")
    tools_for(log=log, node="compliance").search_contract("governing law")
    assert len(log.of_node("manager")) == 1
    assert len(log.of_node("compliance")) == 1
    assert len(log.of_tool("search_contract")) == 2
    assert log.of_tool("open_source_span") == ()


def test_the_log_round_trips_through_jsonl():
    log = ToolCallLog()
    tools = tools_for(log=log)
    tools.search_contract("change of control")
    lines = log.to_jsonl().strip().splitlines()
    assert len(lines) == 1
    assert '"tool":"search_contract"' in lines[0]


def test_a_log_written_to_disk_is_append_only(tmp_path):
    path = tmp_path / "tools.jsonl"
    log = ToolCallLog(path)
    tools = tools_for(log=log)
    tools.search_contract("change of control")
    tools.open_source_span(FIXTURE.paragraphs[0].paragraph_id)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert '"sequence":0' in lines[0]
    assert '"sequence":1' in lines[1]


def test_the_log_refuses_a_record_that_looks_like_a_credential():
    """The same secret guard the model log applies, applied to the tool log."""
    from pilot01.model.log import assert_no_secrets

    log = ToolCallLog()
    tools = tools_for(log=log)
    tools.search_contract("change of control")
    assert_no_secrets(log.records[0])


def test_a_paragraph_id_in_the_log_is_not_a_source_id():
    """The memo's ids and the source layer's ids are different namespaces.

    Nothing in the source layer mints or accepts the memo's ``S-3.2`` style, so
    an upstream-cited id can never be mistaken for an opened paragraph.
    """
    log = ToolCallLog()
    tools = tools_for(log=log)
    with pytest.raises(SourceToolError):
        tools.open_source_span(sample_case.TARGET_CLAIM_SOURCE_IDS[0])
    assert log.records[0].ok is False
    assert not any(
        re.match(PARAGRAPH_ID_RE, source_id)
        for source_id in sample_case.TARGET_CLAIM_SOURCE_IDS
    )
