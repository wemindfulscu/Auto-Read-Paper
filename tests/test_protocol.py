"""Tests for auto_read_paper.protocol: Paper.generate_tldr, Paper.generate_affiliations."""

import pytest

from tests.canned_responses import make_sample_paper, make_stub_llm_client


# ---------------------------------------------------------------------------
# generate_tldr
# ---------------------------------------------------------------------------


def test_tldr_returns_response():
    llm = make_stub_llm_client()
    paper = make_sample_paper()
    result = paper.generate_tldr(llm, "English")
    assert result == "Hello! How can I assist you today?"
    assert paper.tldr == result


def test_tldr_without_abstract_or_fulltext():
    llm = make_stub_llm_client()
    paper = make_sample_paper(abstract="", full_text=None)
    result = paper.generate_tldr(llm, "English")
    assert "Failed to generate TLDR" in result


def test_tldr_falls_back_to_abstract_on_error():
    paper = make_sample_paper()
    broken = make_stub_llm_client(raises=RuntimeError("API down"))
    result = paper.generate_tldr(broken, "English")
    assert result == paper.abstract


def test_tldr_truncates_long_prompt():
    llm = make_stub_llm_client()
    paper = make_sample_paper(full_text="word " * 10000)
    result = paper.generate_tldr(llm, "English")
    assert result is not None


# ---------------------------------------------------------------------------
# generate_affiliations
# ---------------------------------------------------------------------------


def test_affiliations_returns_parsed_list():
    llm = make_stub_llm_client()
    paper = make_sample_paper()
    result = paper.generate_affiliations(llm)
    assert isinstance(result, list)
    assert "TsingHua University" in result
    assert "Peking University" in result


def test_affiliations_none_without_fulltext():
    llm = make_stub_llm_client()
    paper = make_sample_paper(full_text=None)
    result = paper.generate_affiliations(llm)
    assert result is None


def test_affiliations_deduplicates():
    llm = make_stub_llm_client()
    paper = make_sample_paper()
    result = paper.generate_affiliations(llm)
    assert len(result) == len(set(result))


def test_affiliations_malformed_llm_output():
    """LLM returns affiliations without JSON brackets — tolerant parser returns None."""
    llm = make_stub_llm_client(
        responses={"extracts affiliations": "TsingHua University, Peking University"},
    )
    paper = make_sample_paper()
    result = paper.generate_affiliations(llm)
    assert result is None


def test_affiliations_error_returns_none():
    broken = make_stub_llm_client(raises=RuntimeError("boom"))
    paper = make_sample_paper()
    result = paper.generate_affiliations(broken)
    assert result is None
    assert paper.affiliations is None
