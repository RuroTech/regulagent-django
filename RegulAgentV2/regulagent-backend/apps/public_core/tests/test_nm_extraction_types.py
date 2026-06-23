"""
Tests for NM document type extraction support in openai_extraction.py.

Covers:
- SUPPORTED_TYPES entries for c_101, c_103, c_105, sundry
- classify_document() filename heuristics for NM types
- _load_prompt() returns non-empty strings for each NM type
"""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


def test_supported_types_has_nm_types():
    from apps.public_core.services.openai_extraction import SUPPORTED_TYPES
    for key in ["c_101", "c_103", "c_105", "sundry"]:
        assert key in SUPPORTED_TYPES, f"Missing key: {key}"
        assert "required_sections" in SUPPORTED_TYPES[key], f"Missing required_sections for {key}"
        assert len(SUPPORTED_TYPES[key]["required_sections"]) > 0, f"Empty required_sections for {key}"


def test_c101_required_sections():
    from apps.public_core.services.openai_extraction import SUPPORTED_TYPES
    sections = SUPPORTED_TYPES["c_101"]["required_sections"]
    assert "header" in sections
    assert "operator_info" in sections
    assert "well_info" in sections
    assert "proposed_work" in sections
    assert "casing_record" in sections
    assert "cement_record" in sections
    assert "remarks" in sections


def test_c103_required_sections():
    from apps.public_core.services.openai_extraction import SUPPORTED_TYPES
    sections = SUPPORTED_TYPES["c_103"]["required_sections"]
    assert "header" in sections
    assert "operator_info" in sections
    assert "notice_type" in sections
    assert "plugging_procedure" in sections


def test_c105_required_sections():
    from apps.public_core.services.openai_extraction import SUPPORTED_TYPES
    sections = SUPPORTED_TYPES["c_105"]["required_sections"]
    assert "header" in sections
    assert "completion_data" in sections
    assert "casing_record" in sections
    assert "perforation_record" in sections
    assert "production_test" in sections


def test_sundry_required_sections():
    from apps.public_core.services.openai_extraction import SUPPORTED_TYPES
    sections = SUPPORTED_TYPES["sundry"]["required_sections"]
    assert "header" in sections
    assert "operator_info" in sections
    assert "notice_type" in sections
    assert "description" in sections
    assert "remarks" in sections


def test_nm_types_have_prompt_key():
    from apps.public_core.services.openai_extraction import SUPPORTED_TYPES
    for key in ["c_101", "c_103", "c_105", "sundry"]:
        assert "prompt_key" in SUPPORTED_TYPES[key], f"Missing prompt_key for {key}"
        assert SUPPORTED_TYPES[key]["prompt_key"] == key


# classify_document heuristic tests — no OpenAI call for filename-matched docs

def test_classify_c101_hyphenated_filename():
    """'c-101_report.pdf' should match via heuristic without calling OpenAI."""
    from apps.public_core.services.openai_extraction import classify_document
    path = MagicMock(spec=Path)
    path.suffix = ".pdf"
    path.name = "c-101_report.pdf"
    result = classify_document(path)
    assert result == "c_101"


def test_classify_c101_plain_filename():
    from apps.public_core.services.openai_extraction import classify_document
    path = MagicMock(spec=Path)
    path.suffix = ".pdf"
    path.name = "c101_permit.pdf"
    result = classify_document(path)
    assert result == "c_101"


def test_classify_c101_underscore_filename():
    from apps.public_core.services.openai_extraction import classify_document
    path = MagicMock(spec=Path)
    path.suffix = ".pdf"
    path.name = "c_101.pdf"
    result = classify_document(path)
    assert result == "c_101"


def test_classify_c103_hyphenated_filename():
    from apps.public_core.services.openai_extraction import classify_document
    path = MagicMock(spec=Path)
    path.suffix = ".pdf"
    path.name = "C103_notice.pdf"
    result = classify_document(path)
    assert result == "c_103"


def test_classify_c103_hyphen_filename():
    from apps.public_core.services.openai_extraction import classify_document
    path = MagicMock(spec=Path)
    path.suffix = ".pdf"
    path.name = "c-103_abandon.pdf"
    result = classify_document(path)
    assert result == "c_103"


def test_classify_c105_filename():
    from apps.public_core.services.openai_extraction import classify_document
    path = MagicMock(spec=Path)
    path.suffix = ".pdf"
    path.name = "c_105.pdf"
    result = classify_document(path)
    assert result == "c_105"


def test_classify_c105_plain_filename():
    from apps.public_core.services.openai_extraction import classify_document
    path = MagicMock(spec=Path)
    path.suffix = ".pdf"
    path.name = "c105_completion.pdf"
    result = classify_document(path)
    assert result == "c_105"


def test_classify_sundry_filename():
    from apps.public_core.services.openai_extraction import classify_document
    path = MagicMock(spec=Path)
    path.suffix = ".pdf"
    path.name = "sundry_notice.pdf"
    result = classify_document(path)
    assert result == "sundry"


def test_classify_sundry_filename_uppercase():
    from apps.public_core.services.openai_extraction import classify_document
    path = MagicMock(spec=Path)
    path.suffix = ".pdf"
    path.name = "SUNDRY_2024.pdf"
    result = classify_document(path)
    assert result == "sundry"


def test_load_prompt_c101_nonempty():
    from apps.public_core.services.openai_extraction import _load_prompt
    prompt = _load_prompt("c_101")
    assert isinstance(prompt, str)
    assert len(prompt) > 0
    assert "C-101" in prompt or "c_101" in prompt.lower() or "Permit to Drill" in prompt


def test_load_prompt_c103_nonempty():
    from apps.public_core.services.openai_extraction import _load_prompt
    prompt = _load_prompt("c_103")
    assert isinstance(prompt, str)
    assert len(prompt) > 0
    assert "C-103" in prompt or "c_103" in prompt.lower() or "Plug" in prompt or "Workover" in prompt


def test_load_prompt_c105_nonempty():
    from apps.public_core.services.openai_extraction import _load_prompt
    prompt = _load_prompt("c_105")
    assert isinstance(prompt, str)
    assert len(prompt) > 0
    assert "C-105" in prompt or "c_105" in prompt.lower() or "Completion" in prompt


def test_load_prompt_sundry_nonempty():
    from apps.public_core.services.openai_extraction import _load_prompt
    prompt = _load_prompt("sundry")
    assert isinstance(prompt, str)
    assert len(prompt) > 0
    assert "Sundry" in prompt or "sundry" in prompt.lower()


def test_c104_required_sections():
    from apps.public_core.services.openai_extraction import SUPPORTED_TYPES
    sections = SUPPORTED_TYPES["c_104"]["required_sections"]
    assert "header" in sections
    assert "operator_info" in sections
    assert "well_info" in sections
    assert "allowable_info" in sections
    assert "transporter" in sections
    # subsequent_report was a leftover that never matched the actual C-104 form
    assert "subsequent_report" not in sections


def test_load_prompt_c104_nonempty():
    """c_104 previously had no prompt -> raw text only. It must now return a real
    prompt that maps the form's fields into the operator_info/well_info shape the
    WellRegistry enrichment consumes (lease<-property name, field<-pool name)."""
    from apps.public_core.services.openai_extraction import _load_prompt
    prompt = _load_prompt("c_104")
    assert isinstance(prompt, str)
    assert len(prompt) > 0
    assert "C-104" in prompt or "Transport" in prompt
    # The enrichment reads operator_info.name and well_info.{lease,field,well_no,county};
    # the prompt must instruct the model to populate those.
    assert "operator_info" in prompt
    assert "well_info" in prompt
    assert "lease" in prompt and "well_no" in prompt


def test_classify_image_file_returns_schematic():
    """Image files should always return 'schematic' regardless of name."""
    from apps.public_core.services.openai_extraction import classify_document
    path = MagicMock(spec=Path)
    path.suffix = ".png"
    path.name = "wellbore_diagram.png"
    result = classify_document(path)
    assert result == "schematic"
