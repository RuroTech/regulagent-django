"""
RetrievedDocument pipeline wiring — behavioral tests.

Covers the non-Playwright wiring points:
  2. tasks_research.py no_forms / quota_exceeded / SIGKILL paths update RetrievedDocument
  3. index_single_document() links RetrievedDocument to the new ExtractedDocument

NOTE: The Playwright-mocked extractor tests (GROUP 1) have been removed per repo policy
(DOM-bound RRC filler/extractor code is validated against the real portal, not mocked DOM
tests). The extractor wiring (rrc_completions_extractor.py) is verified via Django shell
instead.

Test api14 range: 42901008880000 (distinct from 42901007570000 in the manifest file).

Mock seams
----------
tasks_research.py:
  - ``apps.public_core.models.neubus_lease.NeubusDocument`` is patched at the source module
    level (the import inside index_document_task re-binds to the same object) to control
    the is_neubus routing.
  - ``apps.public_core.tasks_research.classify_document_pages_v2`` is patched to return []
    to force the no_forms branch.
  - ``apps.public_core.tasks_research.index_single_document`` is patched to raise
    OpenAIQuotaExceededError to force the quota_exceeded branch.
  - ``apps.public_core.tasks_research.finalize_session_task.delay`` is patched to avoid
    real Celery dispatch.

document_pipeline.py:
  - ``apps.public_core.services.document_pipeline.get_adapter`` is patched to return a
    mock adapter whose download_document() returns a Path-like mock.
  - ``apps.public_core.services.document_pipeline.classify_document`` patched to return
    a known doc_type.
  - ``apps.public_core.services.document_pipeline.extract_json_from_pdf`` patched.
  - ``apps.public_core.services.document_pipeline.vectorize_extracted_document`` patched.
  - ``fitz`` is patched via sys.modules so the inline ``import fitz as _fitz`` inside
    index_single_document finds the mock without AttributeError.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_API14 = "42901008880000"   # distinct — do NOT reuse 42901007570000

HREF_W15 = "/CMPL/viewPdfReportFormAction.do?pkt=8001"
HREF_W2  = "/CMPL/viewPdfReportFormAction.do?pkt=8002"
HREF_GAU = "/CMPL/viewPdfReportFormAction.do?pkt=8003"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _ensure_public_tenant():
    from apps.tenants.models import Tenant, Domain
    from django.contrib.auth import get_user_model
    from django.db import connection as db_conn, transaction

    User = get_user_model()
    owner, _ = User.objects.get_or_create(
        email="rd_pipeline_public_owner@test.internal",
        defaults={"is_active": True, "first_name": "", "last_name": ""},
    )
    with transaction.atomic():
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenants_tenant
                    (schema_name, name, slug, owner_id, created, modified, created_on, vault_passphrase_hash)
                VALUES ('public', 'Public', 'public', %s, NOW(), NOW(), NOW(), '')
                ON CONFLICT (schema_name) DO NOTHING
                """,
                [owner.id],
            )
    tenant = Tenant.objects.get(schema_name="public")
    Domain.objects.get_or_create(
        domain="testserver",
        defaults={"tenant": tenant, "is_primary": True},
    )
    return tenant


@pytest.fixture
def public_tenant(db):
    return _ensure_public_tenant()


@pytest.fixture
def well(db):
    from apps.public_core.models import WellRegistry
    return WellRegistry.objects.create(
        api14=TEST_API14,
        state="TX",
        county="Test County",
    )


@pytest.fixture
def research_session(db, well, public_tenant):
    from apps.public_core.models import ResearchSession
    return ResearchSession.objects.create(
        api_number=TEST_API14,
        state="TX",
        status="indexing",
        tenant=public_tenant,
        well=well,
        total_documents=1,
        indexed_documents=0,
    )


# ---------------------------------------------------------------------------
# fitz (PyMuPDF) mock for use in sys.modules
# ---------------------------------------------------------------------------

def _make_fitz_mock(page_count=3):
    """Return a mock that satisfies 'import fitz as _fitz; pdf = _fitz.open(path)'."""
    mock_pdf = MagicMock()
    mock_pdf.__len__ = MagicMock(return_value=page_count)
    mock_pdf.page_count = page_count
    mock_pdf.close = MagicMock()

    mock_fitz = MagicMock()
    mock_fitz.open.return_value = mock_pdf
    return mock_fitz


# ===========================================================================
# GROUP 2 — tasks_research status-update paths
# ===========================================================================

@pytest.mark.django_db
def test_no_forms_path_updates_retrieved_document_status(db, well, research_session):
    """
    RED: the no_forms branch in index_document_task (Neubus path, ~line 530)
    calls _record_document_result but does NOT update any RetrievedDocument.
    After wiring: the matching row must transition from 'pending' to 'no_forms'.

    Mock seams:
    - apps.public_core.models.neubus_lease.NeubusDocument patched so that
      NeubusDocument.objects.filter(...).exists() returns True → is_neubus=True
      and NeubusDocument.objects.filter(...).first() returns None (no neubus_doc record).
    - apps.public_core.tasks_research.classify_document_pages_v2 returns [] → no_forms branch.
    - fitz injected via sys.modules so the inline `import fitz` inside the task works.
    """
    from apps.public_core.models import RetrievedDocument
    from apps.public_core.tasks_research import index_document_task

    rd = RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href=HREF_W2,
        well=well,
        filename="W-2_42901008880000_001.pdf",
        local_path=f"/media/rrc/completions/{TEST_API14}/W-2_42901008880000_001.pdf",
        kind="w2",
        index_status="pending",
        source_type="rrc",
    )

    doc_spec = {
        "filename": "W-2_42901008880000_001.pdf",
        "url": f"https://webapps.rrc.texas.gov{HREF_W2}",
        "local_path": f"/media/rrc/completions/{TEST_API14}/W-2_42901008880000_001.pdf",
        "file_size": 1024,
        "date": "2024-01-01",
        "doc_type": None,
        "metadata": None,   # no rrc_source, no neubus_lease_id
    }

    fake_local_path = MagicMock()
    fake_local_path.__str__ = MagicMock(return_value=doc_spec["local_path"])
    # Make it behave like a Path for the extractor's page-count check
    fake_local_path.suffix = ".pdf"
    fake_local_path.__fspath__ = MagicMock(return_value=doc_spec["local_path"])

    # Patch NeubusDocument at the module level so the inline import picks it up
    mock_neubus_cls = MagicMock()
    mock_neubus_cls.objects.filter.return_value.exists.return_value = True   # is_neubus
    mock_neubus_cls.objects.filter.return_value.first.return_value = None    # no neubus_doc

    mock_fitz = _make_fitz_mock(page_count=5)

    mock_adapter = MagicMock()
    mock_adapter.download_document.return_value = fake_local_path

    with (
        patch.dict(sys.modules, {"fitz": mock_fitz}),
        patch(
            "apps.public_core.models.neubus_lease.NeubusDocument",
            mock_neubus_cls,
        ),
        # classify_document_pages_v2 is imported *inside* the function body from
        # apps.public_core.services.neubus_classifier — patch the source module.
        patch(
            "apps.public_core.services.neubus_classifier.classify_document_pages_v2",
            return_value=[],   # empty → no_forms branch
        ),
        patch(
            "apps.public_core.tasks_research.get_adapter",
            return_value=mock_adapter,
        ),
        patch("apps.public_core.tasks_research.finalize_session_task.delay"),
    ):
        index_document_task(
            str(research_session.id),
            doc_spec,
            state="TX",
        )

    rd.refresh_from_db()
    assert rd.index_status == "no_forms", (
        f"Expected RetrievedDocument.index_status='no_forms' after the no_forms branch, "
        f"got '{rd.index_status}'.  "
        f"Wiring: in the no_forms branch of index_document_task add:\n"
        f"  RetrievedDocument.objects.filter(\n"
        f"      api_number=session.api_number, filename=doc.filename\n"
        f"  ).update(index_status='no_forms')"
    )


@pytest.mark.django_db
def test_quota_exceeded_path_updates_retrieved_document_status(db, well, research_session):
    """
    RED: the OpenAIQuotaExceededError catch-block (~line 574) calls
    _record_document_result but does NOT update RetrievedDocument.
    After wiring: the matching row must transition from 'pending' to 'quota_exceeded'.

    Mock seams:
    - metadata has rrc_source=True → is_rrc=True → RRC path → calls index_single_document.
    - index_single_document is patched to raise OpenAIQuotaExceededError.
    """
    from apps.public_core.models import RetrievedDocument
    from apps.public_core.tasks_research import index_document_task
    from apps.public_core.services.openai_config import OpenAIQuotaExceededError

    rd = RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href=HREF_GAU,
        well=well,
        filename="GAU_42901008880000_001.pdf",
        local_path=f"/media/rrc/completions/{TEST_API14}/GAU_42901008880000_001.pdf",
        kind="gau",
        index_status="pending",
        source_type="rrc",
    )

    doc_spec = {
        "filename": "GAU_42901008880000_001.pdf",
        "url": f"https://webapps.rrc.texas.gov{HREF_GAU}",
        "local_path": f"/media/rrc/completions/{TEST_API14}/GAU_42901008880000_001.pdf",
        "file_size": 2048,
        "date": "2024-01-01",
        "doc_type": "gau",
        "metadata": {"rrc_source": True},   # forces RRC path
    }

    with (
        patch(
            "apps.public_core.tasks_research.index_single_document",
            side_effect=OpenAIQuotaExceededError("quota hit"),
        ),
        patch("apps.public_core.tasks_research.finalize_session_task.delay"),
    ):
        index_document_task(
            str(research_session.id),
            doc_spec,
            state="TX",
        )

    rd.refresh_from_db()
    assert rd.index_status == "quota_exceeded", (
        f"Expected RetrievedDocument.index_status='quota_exceeded' after quota exception, "
        f"got '{rd.index_status}'.  "
        f"Wiring: in the OpenAIQuotaExceededError catch-block of index_document_task add:\n"
        f"  RetrievedDocument.objects.filter(\n"
        f"      api_number=session.api_number, filename=doc.filename\n"
        f"  ).update(index_status='quota_exceeded')"
    )


@pytest.mark.django_db
def test_sigkill_error_callback_updates_retrieved_document_status(db, well, research_session):
    """
    RED: _on_index_task_error (~line 155) only increments the session counter
    and does NOT update any RetrievedDocument row.
    After wiring: ALL pending RetrievedDocument rows for the session's api_number
    must transition to 'failed' (the killed task's doc can't be identified by
    name, so all pending rows for that session are marked failed).
    """
    from apps.public_core.models import RetrievedDocument
    from apps.public_core.tasks_research import _on_index_task_error

    rd = RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href=HREF_W15,
        well=well,
        filename="W-15_42901008880000_001.pdf",
        local_path=f"/media/rrc/completions/{TEST_API14}/W-15_42901008880000_001.pdf",
        kind="w15",
        index_status="pending",
        source_type="rrc",
    )

    with patch("apps.public_core.tasks_research.finalize_session_task.delay"):
        _on_index_task_error(
            task_id="fake-celery-task-id",
            session_id=str(research_session.id),
        )

    rd.refresh_from_db()
    assert rd.index_status == "failed", (
        f"Expected RetrievedDocument.index_status='failed' after SIGKILL error callback, "
        f"got '{rd.index_status}'.  "
        f"Wiring: _on_index_task_error must transition pending RetrievedDocument rows "
        f"for the session's api_number to index_status='failed':\n"
        f"  session = ResearchSession.objects.get(id=session_id)\n"
        f"  RetrievedDocument.objects.filter(\n"
        f"      api_number=session.api_number, index_status='pending'\n"
        f"  ).update(index_status='failed')"
    )


# ===========================================================================
# GROUP 3 — document_pipeline links RetrievedDocument to ExtractedDocument
# ===========================================================================

@pytest.mark.django_db
@patch("apps.public_core.services.document_pipeline.vectorize_extracted_document")
@patch("apps.public_core.services.document_pipeline.extract_json_from_pdf")
@patch("apps.public_core.services.document_pipeline.classify_document")
@patch("apps.public_core.services.document_pipeline.get_adapter")
def test_pipeline_links_retrieved_document_to_extracted_document(
    mock_get_adapter,
    mock_classify,
    mock_extract,
    mock_vectorize,
    db,
    well,
):
    """
    RED: index_single_document() currently creates an ExtractedDocument but never
    looks up or updates any RetrievedDocument.  After wiring:
    - The pre-existing pending RetrievedDocument must have extracted_document set
      to the newly created ExtractedDocument.
    - Its index_status must be synced to the ED status ('success').

    Mock seams:
    - get_adapter returns a mock adapter whose download_document returns a MagicMock path.
    - classify_document returns 'w15'.
    - extract_json_from_pdf returns a successful ExtractionResult.
    - fitz is injected via sys.modules so the inline 'import fitz as _fitz' works.
    """
    from apps.public_core.models import RetrievedDocument
    from apps.public_core.services.document_pipeline import index_single_document
    from apps.public_core.services.adapters.base import DocumentSpec
    from apps.public_core.services.openai_extraction import ExtractionResult

    rd = RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href=HREF_W15,
        well=well,
        filename="W-15_42901008880000_001.pdf",
        local_path=f"/media/rrc/completions/{TEST_API14}/W-15_42901008880000_001.pdf",
        kind="w15",
        index_status="pending",
        source_type="rrc",
    )

    fake_local_path = MagicMock()
    fake_local_path.name = "W-15_42901008880000_001.pdf"
    fake_local_path.__str__ = MagicMock(return_value=rd.local_path)
    # Simulate Path.suffix as a string property
    type(fake_local_path).suffix = ".pdf"

    mock_adapter = MagicMock()
    mock_adapter.download_document.return_value = fake_local_path
    mock_get_adapter.return_value = mock_adapter

    mock_classify.return_value = "w15"
    mock_extract.return_value = ExtractionResult(
        document_type="w15",
        json_data={"header": {"api": TEST_API14}},
        model_tag="gpt-4o",
        errors=[],
    )

    doc = DocumentSpec(
        filename="W-15_42901008880000_001.pdf",
        url=f"https://webapps.rrc.texas.gov{HREF_W15}",
        local_path=rd.local_path,
        file_size=2048,
        date="2024-01-01",
        doc_type=None,
        metadata={"rrc_source": True},
    )

    mock_fitz = _make_fitz_mock(page_count=3)

    with patch.dict(sys.modules, {"fitz": mock_fitz}):
        ed = index_single_document(doc, TEST_API14, well=well, session=None)

    assert ed is not None, "index_single_document must return an ExtractedDocument"
    assert ed.status == "success"

    rd.refresh_from_db()
    assert rd.extracted_document_id == ed.id, (
        f"Expected RetrievedDocument.extracted_document_id={ed.id}, "
        f"got {rd.extracted_document_id}.  "
        f"Wiring: after creating the ExtractedDocument in index_single_document, add:\n"
        f"  RetrievedDocument.objects.filter(\n"
        f"      api_number=api_number, filename=doc.filename\n"
        f"  ).update(extracted_document=ed, index_status=ed.status)"
    )
    assert rd.index_status == "success", (
        f"Expected RetrievedDocument.index_status='success' (synced from ED.status), "
        f"got '{rd.index_status}'."
    )


@pytest.mark.django_db
@patch("apps.public_core.services.document_pipeline.vectorize_extracted_document")
@patch("apps.public_core.services.document_pipeline.extract_json_from_pdf")
@patch("apps.public_core.services.document_pipeline.classify_document")
@patch("apps.public_core.services.document_pipeline.get_adapter")
def test_pipeline_links_retrieved_document_partial_status(
    mock_get_adapter,
    mock_classify,
    mock_extract,
    mock_vectorize,
    db,
    well,
):
    """
    RED: when index_single_document creates an ED with status='partial'
    (extraction had errors), the matching RetrievedDocument index_status must
    also be set to 'partial'.
    """
    from apps.public_core.models import RetrievedDocument
    from apps.public_core.services.document_pipeline import index_single_document
    from apps.public_core.services.adapters.base import DocumentSpec
    from apps.public_core.services.openai_extraction import ExtractionResult

    rd = RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href=HREF_W2,
        well=well,
        filename="W-2_42901008880000_001.pdf",
        local_path=f"/media/rrc/completions/{TEST_API14}/W-2_42901008880000_001.pdf",
        kind="w2",
        index_status="pending",
        source_type="rrc",
    )

    fake_local_path = MagicMock()
    fake_local_path.name = "W-2_42901008880000_001.pdf"
    fake_local_path.__str__ = MagicMock(return_value=rd.local_path)
    type(fake_local_path).suffix = ".pdf"

    mock_adapter = MagicMock()
    mock_adapter.download_document.return_value = fake_local_path
    mock_get_adapter.return_value = mock_adapter

    mock_classify.return_value = "w2"
    mock_extract.return_value = ExtractionResult(
        document_type="w2",
        json_data={"header": {}},
        model_tag="gpt-4o",
        errors=["Could not parse section 3"],   # → status='partial'
    )

    doc = DocumentSpec(
        filename="W-2_42901008880000_001.pdf",
        url=f"https://webapps.rrc.texas.gov{HREF_W2}",
        local_path=rd.local_path,
        file_size=2048,
        date="2024-01-01",
        doc_type=None,
        metadata={"rrc_source": True},
    )

    mock_fitz = _make_fitz_mock(page_count=2)

    with patch.dict(sys.modules, {"fitz": mock_fitz}):
        ed = index_single_document(doc, TEST_API14, well=well, session=None)

    assert ed is not None
    assert ed.status == "partial", f"Expected ED.status='partial', got '{ed.status}'"

    rd.refresh_from_db()
    assert rd.index_status == "partial", (
        f"Expected RetrievedDocument.index_status='partial' when ED has errors, "
        f"got '{rd.index_status}'.  "
        f"Wiring: sync index_status to ED.status (not only on 'success')."
    )
    assert rd.extracted_document_id == ed.id, (
        f"RetrievedDocument must be linked even when extraction is partial."
    )


# ===========================================================================
# GROUP 4 — Behavior A: succeeded==0 must flip RetrievedDocument to no_forms
# ===========================================================================

@pytest.mark.django_db
def test_succeeded_zero_flips_retrieved_document_to_no_forms(db, well, research_session):
    """
    RED: in the TX-Neubus branch, when classify_document_pages_v2 returns NON-empty
    form_groups (extraction runs) but all extraction results fail (succeeded==0),
    there is currently NO branch that updates the RetrievedDocument manifest row.
    The row stays 'pending' forever.

    After fix: the row must end at index_status='no_forms'.

    Mirrors test_no_forms_path_updates_retrieved_document_status; differences:
    - classify_document_pages_v2 returns a non-empty list  (extraction branch taken)
    - extract_form_groups returns [result(status='failed')]  (succeeded==0)
    - NeubusDocument.first() returns a mock neubus_doc (non-None) so
      file_hash / well_number attribute accesses inside the task don't crash

    Mock seams (identical to the no_forms test above except where noted):
    - apps.public_core.models.neubus_lease.NeubusDocument patched:
        .objects.filter().exists()=True  → is_neubus=True
        .objects.filter().first()=mock_neubus_doc  (NON-None, differs from no_forms test)
    - apps.public_core.services.neubus_classifier.classify_document_pages_v2:
        returns [MagicMock()]  (NON-empty, differs from no_forms test)
    - apps.public_core.services.neubus_extractor.extract_form_groups:
        returns [result(status='failed')]  (NEW — not needed in no_forms test)
    - apps.public_core.tasks_research.get_adapter: mock adapter
    - fitz: sys.modules injection, page_count=5 (<30 → no PDF splitting)
    """
    from apps.public_core.models import RetrievedDocument
    from apps.public_core.tasks_research import index_document_task

    FILENAME = "NB_42901008880000_forms.pdf"

    # Pre-create a pending row with a non-disk href so _record_retrieved_document
    # inside the task creates a second (disk:api:filename) row.  The expected fix
    # filters by (api_number, filename) and updates BOTH rows; we assert on this one.
    rd = RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href=f"neubus-stub:{TEST_API14}:{FILENAME}",
        well=well,
        filename=FILENAME,
        local_path=f"/media/neubus/{TEST_API14}/{FILENAME}",
        index_status="pending",
        source_type="neubus",
    )

    doc_spec = {
        "filename": FILENAME,
        "url": None,
        "local_path": f"/media/neubus/{TEST_API14}/{FILENAME}",
        "file_size": 2048,
        "date": "2024-01-01",
        "doc_type": None,
        "metadata": None,   # no rrc_source, no neubus_lease_id → routing via ND.exists()
    }

    fake_local_path = MagicMock()
    fake_local_path.__str__ = MagicMock(return_value=doc_spec["local_path"])
    fake_local_path.suffix = ".pdf"
    fake_local_path.__fspath__ = MagicMock(return_value=doc_spec["local_path"])

    # neubus_doc is present (not None) — file_hash / well_number / parent_document
    # are accessed by the task so we give them safe values
    mock_neubus_doc = MagicMock()
    mock_neubus_doc.classification_status = "pending"
    mock_neubus_doc.extraction_status = "pending"
    mock_neubus_doc.file_hash = "deadbeef"
    mock_neubus_doc.well_number = ""
    mock_neubus_doc.parent_document = None

    mock_neubus_cls = MagicMock()
    mock_neubus_cls.objects.filter.return_value.exists.return_value = True    # is_neubus=True
    mock_neubus_cls.objects.filter.return_value.first.return_value = mock_neubus_doc

    mock_fitz = _make_fitz_mock(page_count=5)   # < 30 → no PDF splitting

    mock_adapter = MagicMock()
    mock_adapter.download_document.return_value = fake_local_path

    # NON-empty form groups → extraction branch is taken (not the empty no_forms branch)
    mock_form_groups = [MagicMock()]

    # All extraction results fail → succeeded == 0
    mock_failed_result = MagicMock()
    mock_failed_result.status = "failed"

    with (
        patch.dict(sys.modules, {"fitz": mock_fitz}),
        patch(
            "apps.public_core.models.neubus_lease.NeubusDocument",
            mock_neubus_cls,
        ),
        # classify_document_pages_v2 is imported inside the function body from
        # apps.public_core.services.neubus_classifier — patch the source module
        patch(
            "apps.public_core.services.neubus_classifier.classify_document_pages_v2",
            return_value=mock_form_groups,  # NON-empty → extraction runs
        ),
        # extract_form_groups is imported inside the function body from
        # apps.public_core.services.neubus_extractor — patch the source module
        patch(
            "apps.public_core.services.neubus_extractor.extract_form_groups",
            return_value=[mock_failed_result],  # all fail → succeeded==0
        ),
        patch(
            "apps.public_core.tasks_research.get_adapter",
            return_value=mock_adapter,
        ),
        patch("apps.public_core.tasks_research.finalize_session_task.delay"),
    ):
        index_document_task(
            str(research_session.id),
            doc_spec,
            state="TX",
        )

    rd.refresh_from_db()
    assert rd.index_status == "no_forms", (
        f"Expected RetrievedDocument.index_status='no_forms' when form_groups are "
        f"non-empty but all extraction results fail (succeeded==0), "
        f"got '{rd.index_status}'. "
        f"Current behavior: when succeeded==0 the 'if succeeded > 0:' block is skipped "
        f"and no manifest update is issued — the row stays 'pending'. "
        f"Fix: after 'succeeded = sum(...)' in the TX-Neubus branch (~line 625), "
        f"add an else/zero clause:\n"
        f"  else:  # succeeded == 0\n"
        f"    from apps.public_core.models import RetrievedDocument as _RD\n"
        f"    _RD.objects.filter(\n"
        f"        api_number=session.api_number, filename=doc.filename\n"
        f"    ).update(index_status='no_forms')"
    )
