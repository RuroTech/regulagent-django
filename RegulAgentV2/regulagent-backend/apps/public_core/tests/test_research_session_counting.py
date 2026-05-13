"""
TDD tests for Card 10 — indexed_documents counter accuracy on ResearchSession.

Two bugs cause over-counting:

Bug 1 — Chunking double-count:
    tasks_research.py line ~433 calls _increment_and_maybe_finalize() on the
    PARENT task right after dispatching chunk subtasks.  Each chunk ALSO calls
    _increment_and_maybe_finalize() at line ~470.  The parent call must be
    removed so that only the N chunk completions register N increments.

Bug 2 — Idempotency path over-count:
    document_pipeline.py lines ~84-88 increment indexed_documents on the early-
    return path for already-indexed documents.  A document that is skipped must
    NOT be counted — it was already counted in a prior session.

These tests are written BEFORE the fix is applied and are therefore expected to
FAIL with the current code.  Once a Backend Engineer removes the two spurious
increments the tests will turn green.
"""
import uuid
import pytest
from unittest.mock import patch, MagicMock, call

from apps.public_core.models import ResearchSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(db, *, total_documents: int, state: str = "NM") -> ResearchSession:
    """Create a ResearchSession with realistic defaults."""
    return ResearchSession.objects.create(
        api_number="30015286920000",
        state=state,
        status="indexing",
        total_documents=total_documents,
        indexed_documents=0,
    )


def _refresh(session: ResearchSession) -> ResearchSession:
    """Re-read from DB to get the latest counter values."""
    return ResearchSession.objects.get(id=session.id)


# ---------------------------------------------------------------------------
# Bug 1: chunking double-count
#
# Scenario: 1 document is split into 3 chunks.
#   total_documents starts at 1, then is bumped +2 (extra = 3-1) → 3.
#   Each chunk task calls _increment_and_maybe_finalize → 3 increments.
#   The parent task ALSO calls _increment_and_maybe_finalize → 4th increment.
#   Expected: indexed_documents == 3 (only chunk completions count).
#   Current (buggy): indexed_documents == 4.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@patch("apps.public_core.tasks_research.finalize_session_task")
@patch("apps.public_core.tasks_research.index_document_task.apply_async")
@patch("apps.public_core.tasks_research._split_pdf_into_chunks")
def test_chunked_document_counted_once_per_chunk_not_by_parent(
    mock_split,
    mock_apply_async,
    mock_finalize,
    db,
):
    """
    Processing 1 document that splits into 3 chunks must leave indexed_documents == 3,
    not 4.  Bug 1 causes the parent task to fire an extra increment, producing 4.
    """
    from apps.public_core.tasks_research import _increment_and_maybe_finalize

    NUM_CHUNKS = 3
    session = _make_session(db, total_documents=1)

    # Simulate the session total_documents bump that the chunking code does:
    # extra = NUM_CHUNKS - 1  (parent already counted once in total)
    session.total_documents = session.total_documents + (NUM_CHUNKS - 1)
    session.save(update_fields=["total_documents"])

    # Simulate each chunk task completing — each calls _increment_and_maybe_finalize.
    # We call the real function so the DB is updated correctly.
    for _ in range(NUM_CHUNKS):
        _increment_and_maybe_finalize(str(session.id))

    # Now simulate the PARENT task also calling _increment_and_maybe_finalize
    # (this is the bug — it should NOT do this after dispatching chunks).
    _increment_and_maybe_finalize(str(session.id))  # Bug 1: spurious extra increment

    session_after = _refresh(session)

    # Expected: 3 (one per chunk).  Actual with bug: 4.
    assert session_after.indexed_documents == NUM_CHUNKS, (
        f"Expected indexed_documents={NUM_CHUNKS} (one per chunk), "
        f"got {session_after.indexed_documents}. "
        f"Bug 1: parent task increments the counter after dispatching chunks, "
        f"which is a double-count — only chunk completions should count."
    )


@pytest.mark.django_db
@patch("apps.public_core.tasks_research.finalize_session_task")
@patch("apps.public_core.tasks_research.index_document_task.apply_async")
@patch("apps.public_core.tasks_research._split_pdf_into_chunks")
def test_chunked_session_total_equals_chunk_count(
    mock_split,
    mock_apply_async,
    mock_finalize,
    db,
):
    """
    After all chunks complete, indexed_documents must equal total_documents.
    With Bug 1 the parent fires an extra increment, so indexed > total.
    """
    from apps.public_core.tasks_research import _increment_and_maybe_finalize

    NUM_CHUNKS = 3
    session = _make_session(db, total_documents=1)

    # Simulate the total_documents bump
    extra = NUM_CHUNKS - 1
    session.total_documents = session.total_documents + extra
    session.save(update_fields=["total_documents"])

    # Simulate each chunk completing
    for _ in range(NUM_CHUNKS):
        _increment_and_maybe_finalize(str(session.id))

    # Bug 1: parent fires one more increment after dispatching chunks
    _increment_and_maybe_finalize(str(session.id))

    session_after = _refresh(session)

    assert session_after.indexed_documents == session_after.total_documents, (
        f"indexed_documents ({session_after.indexed_documents}) must equal "
        f"total_documents ({session_after.total_documents}) after all chunks complete. "
        f"Bug 1 pushes indexed_documents above total_documents."
    )


# ---------------------------------------------------------------------------
# Bug 2: idempotency path over-count
#
# Scenario: index_single_document is called for a document that is already
#   fully indexed (has_data=True, is_known_type=True).  The early-return path
#   currently calls ResearchSession.objects.filter(...).update(indexed_documents+1).
#   Expected: indexed_documents stays at 0 (doc was skipped, not newly indexed).
#   Current (buggy): indexed_documents becomes 1.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@patch("apps.public_core.services.document_pipeline.vectorize_extracted_document")
@patch("apps.public_core.services.document_pipeline.get_adapter")
def test_already_indexed_document_does_not_increment_counter(
    mock_get_adapter,
    mock_vectorize,
    db,
):
    """
    When a document has already been fully extracted and index_single_document
    hits the idempotency early-return path, indexed_documents must NOT increase.
    Bug 2 causes it to increment by 1 anyway.
    """
    from apps.public_core.services.document_pipeline import index_single_document
    from apps.public_core.services.adapters.base import DocumentSpec
    from apps.public_core.models import ExtractedDocument

    session = _make_session(db, total_documents=1)

    # Pre-create a fully-extracted document so the idempotency check fires
    ExtractedDocument.objects.create(
        api_number="30015286920000",
        document_type="c_101",
        source_path="/tmp/c-101_report.pdf",
        status="success",
        json_data={"header": {"permit_number": "NM-001"}},
        errors=[],
    )

    doc = DocumentSpec(
        filename="c-101_report.pdf",
        url="https://example.com/c-101_report.pdf",
        file_size=1024,
        date="2024-01-01",
        doc_type=None,
    )

    # Call the pipeline — should hit the idempotency early-return
    result = index_single_document(doc, "30015286920000", well=None, session=session)

    session_after = _refresh(session)

    # The document was SKIPPED — no new indexing happened.
    # indexed_documents must remain 0.
    assert session_after.indexed_documents == 0, (
        f"Expected indexed_documents=0 for a skipped (already-indexed) document, "
        f"got {session_after.indexed_documents}. "
        f"Bug 2: idempotency early-return path still calls the counter increment."
    )

    # Confirm the function did return the existing document (not None)
    assert result is not None, "index_single_document should return the existing ED on skip"


@pytest.mark.django_db
@patch("apps.public_core.services.document_pipeline.vectorize_extracted_document")
@patch("apps.public_core.services.document_pipeline.extract_json_from_pdf")
@patch("apps.public_core.services.document_pipeline.classify_document")
@patch("apps.public_core.services.document_pipeline.get_adapter")
def test_already_indexed_document_counter_unchanged_multiple_calls(
    mock_get_adapter,
    mock_classify,
    mock_extract,
    mock_vectorize,
    db,
):
    """
    Calling index_single_document twice for the same already-indexed document
    must NOT increment the counter twice.  The second call must be a no-op on
    the counter regardless of how many times the session re-processes the list.
    """
    from apps.public_core.services.document_pipeline import index_single_document
    from apps.public_core.services.adapters.base import DocumentSpec
    from apps.public_core.models import ExtractedDocument

    session = _make_session(db, total_documents=2)

    ExtractedDocument.objects.create(
        api_number="30015286920000",
        document_type="c_102",
        source_path="/tmp/c-102_report.pdf",
        status="success",
        json_data={"header": {"permit_number": "NM-002"}},
        errors=[],
    )

    doc = DocumentSpec(
        filename="c-102_report.pdf",
        url="https://example.com/c-102_report.pdf",
        file_size=512,
        date="2024-02-01",
        doc_type=None,
    )

    index_single_document(doc, "30015286920000", well=None, session=session)
    index_single_document(doc, "30015286920000", well=None, session=session)

    session_after = _refresh(session)

    assert session_after.indexed_documents == 0, (
        f"Two calls to index_single_document for the same already-indexed doc "
        f"must leave indexed_documents=0 (skips are not counts), "
        f"got {session_after.indexed_documents}."
    )


# ---------------------------------------------------------------------------
# Regression: 3 normal (non-chunked) documents
#
# Scenario: 3 separate documents, each processed normally through
#   _increment_and_maybe_finalize.  indexed_documents must equal 3.
#   This verifies the increment logic itself is sound — the test is expected
#   to PASS today and must continue passing after the fix.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@patch("apps.public_core.tasks_research.finalize_session_task")
def test_three_normal_documents_counter_equals_three(mock_finalize, db):
    """
    Processing 3 non-chunked documents (each calling _increment_and_maybe_finalize
    exactly once) must leave indexed_documents == 3.
    This is the happy-path baseline — it passes today and must stay green after
    Bug 1 and Bug 2 are fixed.
    """
    from apps.public_core.tasks_research import _increment_and_maybe_finalize

    session = _make_session(db, total_documents=3)

    for _ in range(3):
        _increment_and_maybe_finalize(str(session.id))

    session_after = _refresh(session)

    assert session_after.indexed_documents == 3, (
        f"Expected indexed_documents=3 for 3 normal documents, "
        f"got {session_after.indexed_documents}."
    )
