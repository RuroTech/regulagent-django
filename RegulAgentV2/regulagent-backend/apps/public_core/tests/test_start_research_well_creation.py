"""
TDD red-phase tests for deferred WellRegistry creation in start_research_session_task.

Bug (Behavior B)
----------------
start_research_session_task creates a bare WellRegistry stub at ~lines 235-247,
BEFORE calling adapter.fetch_document_list (~line 296).  When fetch_document_list
returns [] the session transitions to status='error' but the empty stub is never
cleaned up.

Expected behaviour (post-fix)
------------------------------
B1 — zero docs returned  → NO WellRegistry created for that api14.
     Creation is deferred until ≥1 document is confirmed from the adapter.
B2 — ≥1 doc returned     → WellRegistry IS created so documents can be linked.
     This is a regression guard: the fix must not over-correct to "never create".

Test api14: 42901099990000
  Distinct from 42901008880000 (test_retrieved_document_pipeline.py) and
  30015286920000 (test_research_session_counting.py).

Mock seams
----------
start_research_session_task:
  - ``apps.public_core.tasks_research.get_adapter``
      Returned mock adapter controls fetch_document_list's return value.
      Patched at the module-level binding, which both start_research_session_task
      and index_document_task use.
  - ``apps.public_core.tasks_research.group``  (B2 only)
      Replaced with a MagicMock so the Celery task fan-out is a no-op.
      The generator expression passed to group() is never consumed by the mock,
      so index_document_task.s() is never called.
  - ``apps.public_core.tasks_research.finalize_session_task``  (B2 only)
      Replaced so that .apply_async() is a no-op (no watchdog dispatch).

BE ordering notes (for the implementer)
----------------------------------------
Lines between the current WellRegistry creation and fetch_document_list that
reference `well` / `session.well` and MUST move with the deferred creation:

  ~249-265  lease_well_map building — reads session.well.lease_id
  ~267-277  track_well_interaction — receives `well` as a positional arg
  ~330-333  session.well.data_status = "indexing" — must run AFTER well is created,
            but it already lives after the fetch_document_list block so it moves
            naturally; just make sure session.well is set before that block.

After the fix the sequence should be roughly:
  1. Fetch doc list (adapter.fetch_document_list)
  2. If empty → error-return (no well creation, no lease map, no engagement track)
  3. get_or_create WellRegistry
  4. session.well = well; session.save(["well"])
  5. Build lease_well_map
  6. track_well_interaction
  7. Dispatch index tasks
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from apps.public_core.models import ResearchSession, WellRegistry

# Unique api14 for this module — do NOT reuse in other test files
B_API14 = "42901099990000"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_b_session(db) -> ResearchSession:
    """Create a ResearchSession with well=None, mirroring the views/research.py entry."""
    return ResearchSession.objects.create(
        api_number=B_API14,
        state="TX",
        status="pending",
        total_documents=0,
        indexed_documents=0,
    )


# ---------------------------------------------------------------------------
# B1 — zero docs → no WellRegistry stub left behind
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_zero_docs_leaves_no_well_registry(db):
    """
    RED: when fetch_document_list returns [], start_research_session_task currently
    creates a bare WellRegistry at ~line 238 BEFORE checking the doc list.  The
    session then transitions to status='error' but the empty stub is never deleted.

    After fix: WellRegistry must NOT exist when zero docs are discovered.

    Patch target:
      apps.public_core.tasks_research.get_adapter
        → mock_adapter.fetch_document_list.return_value = []
    """
    from apps.public_core.tasks_research import start_research_session_task

    session = _make_b_session(db)

    # Pre-condition: confirm the well does not exist yet
    assert not WellRegistry.objects.filter(api14=B_API14).exists(), (
        "Test pre-condition failed: WellRegistry must not exist before the task runs"
    )

    mock_adapter = MagicMock()
    mock_adapter.fetch_document_list.return_value = []
    # _last_fetch_error: None → task uses the plain "No documents" message
    mock_adapter._last_fetch_error = None

    with patch("apps.public_core.tasks_research.get_adapter", return_value=mock_adapter):
        start_research_session_task(str(session.id))

    session.refresh_from_db()
    assert session.status == "error", (
        f"Expected session.status='error' after zero-doc response, "
        f"got '{session.status}'"
    )
    assert not WellRegistry.objects.filter(api14=B_API14).exists(), (
        f"Expected NO WellRegistry when fetch_document_list returns [] "
        f"(api14={B_API14!r}), but a row was found in the DB. "
        f"Current bug: WellRegistry.objects.get_or_create(api14=...) at ~line 238 "
        f"runs BEFORE adapter.fetch_document_list (~line 296), so the stub is created "
        f"even when zero documents are discovered. "
        f"Fix: defer the get_or_create (and the session.well assignment) until AFTER "
        f"the 'if not doc_list:' early-return block (~line 298).  "
        f"See module docstring for the full ordering notes."
    )


# ---------------------------------------------------------------------------
# B2 — ≥1 doc → WellRegistry IS created (regression guard, expected to pass)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_at_least_one_doc_creates_well_registry(db):
    """
    GUARD — expected to pass today AND after the fix.

    When fetch_document_list returns ≥1 document, start_research_session_task MUST
    create the WellRegistry so that downstream index tasks can link documents to it.
    This test prevents the fix from over-correcting to "never create the well".

    Downstream Celery dispatch is suppressed:
      - apps.public_core.tasks_research.group is replaced with a MagicMock whose
        return_value.apply_async() is a no-op.  The generator passed to group() is
        never consumed, so index_document_task.s() is never invoked.
      - apps.public_core.tasks_research.finalize_session_task is replaced so that
        .apply_async() is a no-op (no watchdog timer scheduled).

    Patch targets:
      apps.public_core.tasks_research.get_adapter
      apps.public_core.tasks_research.group
      apps.public_core.tasks_research.finalize_session_task
    """
    from apps.public_core.tasks_research import start_research_session_task

    session = _make_b_session(db)

    assert not WellRegistry.objects.filter(api14=B_API14).exists(), (
        "Test pre-condition failed: WellRegistry must not exist before the task runs"
    )

    # One document — enough to confirm docs were discovered
    mock_doc = MagicMock()
    mock_doc.filename = f"NB_{B_API14}_001.pdf"
    mock_doc.url = None
    mock_doc.local_path = None
    mock_doc.file_size = 1024
    mock_doc.date = "2024-01-01"
    mock_doc.doc_type = None
    mock_doc.metadata = {}

    mock_adapter = MagicMock()
    mock_adapter.fetch_document_list.return_value = [mock_doc]

    with (
        patch("apps.public_core.tasks_research.get_adapter", return_value=mock_adapter),
        # group(generator).apply_async() → no-op; generator is never consumed
        patch("apps.public_core.tasks_research.group") as mock_group,
        # finalize_session_task.apply_async → no-op
        patch("apps.public_core.tasks_research.finalize_session_task") as mock_finalize,
    ):
        mock_group.return_value.apply_async.return_value = None
        mock_finalize.apply_async.return_value = None

        start_research_session_task(str(session.id))

    assert WellRegistry.objects.filter(api14=B_API14).exists(), (
        f"Expected WellRegistry to be created when ≥1 doc is discovered "
        f"(api14={B_API14!r}), but no row found. "
        f"The fix must still call get_or_create AFTER confirming docs exist — "
        f"do not skip creation entirely when doc_list is non-empty."
    )
