"""
TDD tests for Card 9: "change from failed to not supported files on indexing"

Acceptance criteria:
  When a document cannot be processed because its file type is unsupported
  (e.g. .docx, .xlsx, .pptx), the system should record status='unsupported'
  on the ExtractedDocument — NOT status='error'. This distinguishes
  format-level rejections from actual extraction failures.

These tests are written BEFORE implementation and must fail until
document_pipeline.py is updated to detect unsupported extensions and write
status='unsupported' instead of status='error'.

Run with:
    docker compose -f compose.dev.yml exec web python -m pytest \
        apps/public_core/tests/test_document_pipeline_unsupported.py -v

Expected pre-implementation results:
  test_docx_file_sets_status_unsupported         FAIL (status='error', not 'unsupported')
  test_xlsx_file_sets_status_unsupported         FAIL (status='error', not 'unsupported')
  test_pptx_file_sets_status_unsupported         FAIL (status='error', not 'unsupported')
  test_unsupported_file_records_extension_in_errors  FAIL (status='error', assertion on status fails first)
  test_pdf_file_does_not_get_unsupported_status  PASS (regression guard — .pdf gets 'success')
  test_extracted_document_model_accepts_unsupported_status  PASS (no DB constraint)
  test_unsupported_file_skips_openai_classification  FAIL (classify_document IS called, then status='error')
"""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

from apps.public_core.services.adapters.base import DocumentSpec


# ---------------------------------------------------------------------------
# Helper: build a minimal DocumentSpec for a given filename
# ---------------------------------------------------------------------------

def _make_doc(filename: str) -> DocumentSpec:
    return DocumentSpec(
        filename=filename,
        url=f"https://example.com/{filename}",
        file_size=1024,
        date="2024-01-01",
        doc_type=None,
    )


# ---------------------------------------------------------------------------
# Shared mock for classify_document on unsupported paths.
#
# The current pipeline calls classify_document even on .docx/.xlsx files
# because it has no extension guard. With a MagicMock local_path the
# pdfplumber/OCR steps fail gracefully and the LLM classifier would be
# called. We mock it to return "unknown" to:
#   (a) avoid real OpenAI API calls during the pre-implementation test run, and
#   (b) reflect the actual outcome: the LLM cannot classify a Word document and
#       returns "unknown", which today writes status='error'.
#
# After implementation the pipeline must short-circuit BEFORE classify_document
# is called, setting status='unsupported' directly. At that point the mocked
# classify_document will never be reached, but the assertion ed.status ==
# 'unsupported' will pass.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test 1 (MUST FAIL before implementation):
# .docx file → ExtractedDocument.status == 'unsupported'
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@patch("apps.public_core.services.document_pipeline.vectorize_extracted_document")
@patch("apps.public_core.services.document_pipeline.classify_document", return_value="unknown")
@patch("apps.public_core.services.document_pipeline.get_adapter")
def test_docx_file_sets_status_unsupported(mock_get_adapter, mock_classify, mock_vectorize):
    """
    FAILING TEST — implementation not yet present.

    When index_single_document receives a .docx file, the resulting
    ExtractedDocument.status must equal 'unsupported', not 'error'.

    Pre-implementation: classify_document returns 'unknown', pipeline writes
    status='error'. Assertion fails: 'error' != 'unsupported'.

    Post-implementation: pipeline detects .docx extension, skips classify,
    writes status='unsupported'. Assertion passes.
    """
    from apps.public_core.services.document_pipeline import index_single_document

    mock_adapter = MagicMock()
    mock_local_path = MagicMock(spec=Path)
    mock_local_path.name = "well_report.docx"
    mock_local_path.suffix = ".docx"
    mock_adapter.download_document.return_value = mock_local_path
    mock_get_adapter.return_value = mock_adapter

    doc = _make_doc("well_report.docx")
    ed = index_single_document(doc, "42501705750000", well=None, session=None)

    assert ed is not None, "Should return an ExtractedDocument (not None)"
    assert ed.status == "unsupported", (
        f"Expected status='unsupported' for .docx file, got status='{ed.status}'. "
        "Implementation must detect unsupported file types and record status='unsupported'."
    )


# ---------------------------------------------------------------------------
# Test 2 (MUST FAIL before implementation):
# .xlsx file → ExtractedDocument.status == 'unsupported'
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@patch("apps.public_core.services.document_pipeline.vectorize_extracted_document")
@patch("apps.public_core.services.document_pipeline.classify_document", return_value="unknown")
@patch("apps.public_core.services.document_pipeline.get_adapter")
def test_xlsx_file_sets_status_unsupported(mock_get_adapter, mock_classify, mock_vectorize):
    """
    FAILING TEST — implementation not yet present.

    Same as test 1 but for .xlsx (Excel spreadsheet). Must record 'unsupported',
    not 'error'.
    """
    from apps.public_core.services.document_pipeline import index_single_document

    mock_adapter = MagicMock()
    mock_local_path = MagicMock(spec=Path)
    mock_local_path.name = "casing_data.xlsx"
    mock_local_path.suffix = ".xlsx"
    mock_adapter.download_document.return_value = mock_local_path
    mock_get_adapter.return_value = mock_adapter

    doc = _make_doc("casing_data.xlsx")
    ed = index_single_document(doc, "42501705750000", well=None, session=None)

    assert ed is not None
    assert ed.status == "unsupported", (
        f"Expected status='unsupported' for .xlsx file, got status='{ed.status}'. "
        "Implementation must detect unsupported file types and record status='unsupported'."
    )


# ---------------------------------------------------------------------------
# Test 3 (MUST FAIL before implementation):
# .pptx file → ExtractedDocument.status == 'unsupported'
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@patch("apps.public_core.services.document_pipeline.vectorize_extracted_document")
@patch("apps.public_core.services.document_pipeline.classify_document", return_value="unknown")
@patch("apps.public_core.services.document_pipeline.get_adapter")
def test_pptx_file_sets_status_unsupported(mock_get_adapter, mock_classify, mock_vectorize):
    """
    FAILING TEST — implementation not yet present.

    Same as test 1 but for .pptx (PowerPoint). Must record 'unsupported',
    not 'error'.
    """
    from apps.public_core.services.document_pipeline import index_single_document

    mock_adapter = MagicMock()
    mock_local_path = MagicMock(spec=Path)
    mock_local_path.name = "presentation.pptx"
    mock_local_path.suffix = ".pptx"
    mock_adapter.download_document.return_value = mock_local_path
    mock_get_adapter.return_value = mock_adapter

    doc = _make_doc("presentation.pptx")
    ed = index_single_document(doc, "42501705750000", well=None, session=None)

    assert ed is not None
    assert ed.status == "unsupported", (
        f"Expected status='unsupported' for .pptx file, got status='{ed.status}'. "
        "Implementation must detect unsupported file types and record status='unsupported'."
    )


# ---------------------------------------------------------------------------
# Test 4 (MUST FAIL before implementation):
# Unsupported-type document must include a meaningful error message
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@patch("apps.public_core.services.document_pipeline.vectorize_extracted_document")
@patch("apps.public_core.services.document_pipeline.classify_document", return_value="unknown")
@patch("apps.public_core.services.document_pipeline.get_adapter")
def test_unsupported_file_records_extension_in_errors(mock_get_adapter, mock_classify, mock_vectorize):
    """
    FAILING TEST — implementation not yet present.

    The errors list on the ExtractedDocument for an unsupported file must
    contain a message that names the unsupported extension so operators can
    act on it in the audit trail.

    Pre-implementation fails because status='error' assertion fails first.
    Post-implementation must pass: status='unsupported' AND errors mentions '.docx'.
    """
    from apps.public_core.services.document_pipeline import index_single_document

    mock_adapter = MagicMock()
    mock_local_path = MagicMock(spec=Path)
    mock_local_path.name = "well_report.docx"
    mock_local_path.suffix = ".docx"
    mock_adapter.download_document.return_value = mock_local_path
    mock_get_adapter.return_value = mock_adapter

    doc = _make_doc("well_report.docx")
    ed = index_single_document(doc, "42501705750000", well=None, session=None)

    assert ed is not None
    assert ed.status == "unsupported", (
        f"Expected status='unsupported', got '{ed.status}'"
    )
    # The errors list must reference the unsupported extension for audit trail
    errors_combined = " ".join(str(e) for e in ed.errors).lower()
    assert ".docx" in errors_combined or "docx" in errors_combined or "unsupported" in errors_combined, (
        f"errors list should reference the unsupported extension or 'unsupported'. Got: {ed.errors}"
    )


# ---------------------------------------------------------------------------
# Test 5 — Regression guard (must PASS before AND after implementation):
# A supported .pdf file must NOT receive status='unsupported'
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@patch("apps.public_core.services.document_pipeline.vectorize_extracted_document")
@patch("apps.public_core.services.document_pipeline.extract_json_from_pdf")
@patch("apps.public_core.services.document_pipeline.classify_document")
@patch("apps.public_core.services.document_pipeline.get_adapter")
def test_pdf_file_does_not_get_unsupported_status(
    mock_get_adapter,
    mock_classify,
    mock_extract,
    mock_vectorize,
):
    """
    REGRESSION GUARD — must pass before and after implementation.

    A .pdf file that successfully classifies must receive status='success'
    or status='partial', never 'unsupported'.
    """
    from apps.public_core.services.document_pipeline import index_single_document
    from apps.public_core.services.openai_extraction import ExtractionResult

    mock_adapter = MagicMock()
    mock_local_path = MagicMock(spec=Path)
    mock_local_path.name = "c-103_form.pdf"
    mock_local_path.suffix = ".pdf"
    mock_adapter.download_document.return_value = mock_local_path
    mock_get_adapter.return_value = mock_adapter

    mock_classify.return_value = "c_103"
    mock_extract.return_value = ExtractionResult(
        document_type="c_103",
        json_data={"header": {"permit_number": "NM-2024-001"}},
        model_tag="gpt-4o",
        errors=[],
    )

    doc = _make_doc("c-103_form.pdf")
    ed = index_single_document(doc, "30015288410000", well=None, session=None)

    assert ed is not None
    assert ed.status != "unsupported", (
        f"A valid .pdf should never receive status='unsupported'. Got: '{ed.status}'"
    )
    assert ed.status in ("success", "partial"), (
        f"A successfully classified PDF should be 'success' or 'partial'. Got: '{ed.status}'"
    )


# ---------------------------------------------------------------------------
# Test 6 — Model accepts 'unsupported' status without DB constraint errors
# (must PASS before implementation — verifies no migration is needed)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_extracted_document_model_accepts_unsupported_status():
    """
    GUARD TEST — should pass before implementation.

    ExtractedDocument.status is a plain CharField with no choices constraint,
    so 'unsupported' must be storable without a DB error. This verifies no
    migration is required for the new status value.
    """
    from apps.public_core.models import ExtractedDocument

    ed = ExtractedDocument.objects.create(
        api_number="42501705750000",
        document_type="unknown",
        source_path="/tmp/well_report.docx",
        status="unsupported",
        errors=["File type .docx is not supported for extraction"],
        json_data={},
    )

    # Re-fetch from DB to confirm it round-trips correctly
    refreshed = ExtractedDocument.objects.get(pk=ed.pk)
    assert refreshed.status == "unsupported", (
        f"ExtractedDocument should persist status='unsupported'. Got: '{refreshed.status}'"
    )


# ---------------------------------------------------------------------------
# Test 7 (MUST FAIL before implementation):
# Unsupported file must NOT call classify_document (no wasted OpenAI API calls)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@patch("apps.public_core.services.document_pipeline.vectorize_extracted_document")
@patch("apps.public_core.services.document_pipeline.classify_document", return_value="unknown")
@patch("apps.public_core.services.document_pipeline.get_adapter")
def test_unsupported_file_skips_openai_classification(
    mock_get_adapter,
    mock_classify,
    mock_vectorize,
):
    """
    FAILING TEST — implementation not yet present.

    When the file extension is unsupported (.docx), the pipeline should
    short-circuit BEFORE calling classify_document. Sending a .docx to
    the OpenAI classifier is wasteful and should be avoided.

    Pre-implementation: classify_document IS called (mock_classify.call_count == 1),
    and status='error'. Both assertions fail (status check fails first).

    Post-implementation: classify_document is NOT called, status='unsupported'. Both pass.
    """
    from apps.public_core.services.document_pipeline import index_single_document

    mock_adapter = MagicMock()
    mock_local_path = MagicMock(spec=Path)
    mock_local_path.name = "well_report.docx"
    mock_local_path.suffix = ".docx"
    mock_adapter.download_document.return_value = mock_local_path
    mock_get_adapter.return_value = mock_adapter

    doc = _make_doc("well_report.docx")
    ed = index_single_document(doc, "42501705750000", well=None, session=None)

    assert ed is not None
    assert ed.status == "unsupported", (
        f"Expected status='unsupported', got '{ed.status}'"
    )
    assert mock_classify.call_count == 0, (
        f"classify_document should NOT be called for unsupported file types — "
        f"it wastes OpenAI API calls and cannot classify non-PDF formats. "
        f"Got call_count={mock_classify.call_count}"
    )
