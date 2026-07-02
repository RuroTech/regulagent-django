"""
TDD RED PHASE — RRC document-manifest wiring gaps (Documents panel "0" bug).

Two production bugs in the RRC document-manifest pipeline:

Gap 1 (primary) — apps/public_core/tasks_research.py, index_document_task,
the ``is_rrc`` branch (~line 471-480). This branch calls index_single_document()
and _record_document_result() but never touches RetrievedDocument at all. The
extractor (rrc_completions_extractor.py) writes a "pending" row at download
time, but nothing ever flips it to a terminal status — so the Documents panel
shows "Documents (0)" forever for RRC-sourced docs.

Expected fix: the is_rrc branch must, after calling index_single_document(),
update (or create) the RetrievedDocument row for (api_number, filename):
  - ed is not None  -> index_status="success", extracted_document=ed
  - ed is None      -> index_status="no_forms"
and must NOT create a duplicate row when the extractor's "pending" row
already exists (dedup invariant: exactly ONE row per (api_number, filename)).

Gap 2 (defensive) — apps/public_core/services/rrc_completions_extractor.py,
the manifest ``RetrievedDocument.objects.update_or_create(...)`` write
(~line 347) is sync ORM called from a sync function, but in production this
function has been observed running on a thread with a *running* asyncio
event loop, which makes Django raise SynchronousOnlyOperation. Expected fix:
extract the write into a small, independently-testable, loop-safe helper —
agreed name ``_write_rrc_manifest_row(api_number, href, defaults)`` — that
detects a running loop and offloads the ORM call to a worker thread instead
of calling it directly.

All tests below FAIL against current code (see inline docstrings for why).
Nothing outside this file is modified — tests only.
"""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Constants — distinct API14 range, not reused by any other test module.
# ---------------------------------------------------------------------------

TEST_API14 = "42901004440000"


# ---------------------------------------------------------------------------
# Fixtures — mirrors apps/public_core/tests/test_retrieved_document_pipeline.py
# ---------------------------------------------------------------------------

def _ensure_public_tenant():
    from apps.tenants.models import Tenant, Domain
    from django.contrib.auth import get_user_model
    from django.db import connection as db_conn, transaction

    User = get_user_model()
    owner, _ = User.objects.get_or_create(
        email="rd_manifest_gaps_public_owner@test.internal",
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


def _make_doc_spec(filename: str) -> dict:
    return {
        "filename": filename,
        "url": f"https://webapps.rrc.texas.gov/CMPL/viewPdfReportFormAction.do?pkt=555",
        "local_path": f"/media/rrc/completions/{TEST_API14}/{filename}",
        "file_size": 1024,
        "date": "2024-01-01",
        "doc_type": "w2",
        "metadata": {"rrc_source": True},   # forces the is_rrc branch
    }


def _make_extracted_document(well, status="success"):
    from apps.public_core.models import ExtractedDocument
    return ExtractedDocument.objects.create(
        api_number=TEST_API14,
        well=well,
        document_type="w2",
        source_path=f"/media/rrc/completions/{TEST_API14}/W-2_gaps_001.pdf",
        model_tag="gpt-4o",
        status=status,
        errors=[],
        json_data={"header": {}},
    )


# ===========================================================================
# Gap 1 — is_rrc branch must write/update the RetrievedDocument manifest row.
# ===========================================================================

@pytest.mark.django_db(transaction=True)
def test_is_rrc_branch_creates_manifest_row(db, well, research_session):
    """
    RED: the is_rrc branch of index_document_task (~line 471) calls
    index_single_document() then _record_document_result(), but never touches
    RetrievedDocument. Today this asserts 0 rows exist — AssertionError.

    After the fix: exactly ONE RetrievedDocument row exists for
    (api_number, filename) with index_status="success", source_type="rrc",
    and extracted_document set to the returned ExtractedDocument.
    """
    from apps.public_core.models import RetrievedDocument
    from apps.public_core.tasks_research import index_document_task

    filename = "W-2_gaps_001.pdf"
    doc_spec = _make_doc_spec(filename)
    ed = _make_extracted_document(well, status="success")

    with (
        patch(
            "apps.public_core.tasks_research.index_single_document",
            return_value=ed,
        ),
        patch("apps.public_core.tasks_research.finalize_session_task.delay"),
    ):
        index_document_task(
            str(research_session.id),
            doc_spec,
            state="TX",
        )

    rows = RetrievedDocument.objects.filter(api_number=TEST_API14, filename=filename)
    assert rows.count() == 1, (
        f"Expected exactly 1 RetrievedDocument row for ({TEST_API14}, {filename}), "
        f"got {rows.count()}. The is_rrc branch of index_document_task must write/"
        f"update a manifest row after calling index_single_document()."
    )
    rd = rows.first()
    assert rd.index_status == "success", (
        f"Expected index_status='success', got '{rd.index_status}'"
    )
    assert rd.source_type == "rrc", (
        f"Expected source_type='rrc', got '{rd.source_type}'"
    )
    assert rd.extracted_document_id == ed.id, (
        f"Expected extracted_document_id={ed.id}, got {rd.extracted_document_id}"
    )


@pytest.mark.django_db(transaction=True)
def test_is_rrc_flips_existing_pending_row_no_duplicate(db, well, research_session):
    """
    RED (dedup): the extractor writes a "pending" row at download time keyed
    on the http href. Today the is_rrc branch never updates it — it stays
    "pending" forever, and even once a fix lands it must UPDATE that row
    in place rather than creating a second `disk:` row.

    Pre-creates the extractor's pending row (http href), then runs
    index_document_task for a matching DocumentSpec. Asserts: still exactly
    ONE row for (api, filename), now "success", and href is still the
    original http URL (not replaced by a disk: duplicate).
    """
    from apps.public_core.models import RetrievedDocument
    from apps.public_core.tasks_research import index_document_task

    filename = "W-2_gaps_002.pdf"
    http_href = "https://webapps.rrc.texas.gov/CMPL/viewPdfReportFormAction.do?pkt=8002"

    RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href=http_href,
        well=well,
        filename=filename,
        local_path=f"/media/rrc/completions/{TEST_API14}/{filename}",
        kind="w2",
        index_status="pending",
        source_type="rrc",
    )

    doc_spec = _make_doc_spec(filename)
    ed = _make_extracted_document(well, status="success")

    with (
        patch(
            "apps.public_core.tasks_research.index_single_document",
            return_value=ed,
        ),
        patch("apps.public_core.tasks_research.finalize_session_task.delay"),
    ):
        index_document_task(
            str(research_session.id),
            doc_spec,
            state="TX",
        )

    rows = RetrievedDocument.objects.filter(api_number=TEST_API14, filename=filename)
    assert rows.count() == 1, (
        f"Expected exactly 1 row (updated in place, no duplicate), got {rows.count()}. "
        f"The fix must UPDATE the existing pending row keyed by (api_number, filename), "
        f"not create a second `disk:` row."
    )
    rd = rows.first()
    assert rd.index_status == "success", (
        f"Expected the pre-existing pending row to flip to 'success', got '{rd.index_status}'"
    )
    assert rd.href.startswith("https://"), (
        f"Expected the original http download href to be preserved, got '{rd.href}'"
    )


@pytest.mark.django_db(transaction=True)
def test_is_rrc_no_extracted_doc_marks_no_forms(db, well, research_session):
    """
    RED: same as test_is_rrc_branch_creates_manifest_row but index_single_document
    returns None (no recognizable form). Today no RetrievedDocument row is
    created/updated at all — 0 rows, AssertionError.

    After the fix: exactly one row exists with index_status="no_forms".
    """
    from apps.public_core.models import RetrievedDocument
    from apps.public_core.tasks_research import index_document_task

    filename = "W-2_gaps_003.pdf"
    doc_spec = _make_doc_spec(filename)

    with (
        patch(
            "apps.public_core.tasks_research.index_single_document",
            return_value=None,
        ),
        patch("apps.public_core.tasks_research.finalize_session_task.delay"),
    ):
        index_document_task(
            str(research_session.id),
            doc_spec,
            state="TX",
        )

    rows = RetrievedDocument.objects.filter(api_number=TEST_API14, filename=filename)
    assert rows.count() == 1, (
        f"Expected exactly 1 RetrievedDocument row for ({TEST_API14}, {filename}) "
        f"even when no form was recognized, got {rows.count()}."
    )
    rd = rows.first()
    assert rd.index_status == "no_forms", (
        f"Expected index_status='no_forms' when index_single_document returns None, "
        f"got '{rd.index_status}'"
    )


# ===========================================================================
# Gap 2 — extractor's manifest write must be loop-safe.
# ===========================================================================

@pytest.mark.django_db(transaction=True)
def test_extractor_manifest_write_survives_running_event_loop(db, well):
    """
    RED: ImportError — ``_write_rrc_manifest_row`` does not exist yet.

    TODO(be2): extract the manifest-write logic currently inline in
    apps/public_core/services/rrc_completions_extractor.py (~line 347, the
    ``RetrievedDocument.objects.update_or_create(api_number=..., href=...,
    defaults={...})`` call inside extract_completions_all_documents) into a
    standalone helper:

        def _write_rrc_manifest_row(api_number: str, href: str, defaults: dict) -> None

    The helper must detect a currently-running asyncio event loop
    (``asyncio.get_running_loop()``) and, when one is present, offload the
    sync ORM write to a worker thread (e.g. via a executor/thread hop) instead
    of calling it directly on the loop's thread — avoiding Django's
    SynchronousOnlyOperation, which is the exact failure observed in prod.

    This test drives the helper from inside a real running event loop
    (``asyncio.run``) and asserts the write succeeds with no exception and the
    row is persisted. Today this fails at the import line (ImportError);
    once BE2 adds the loop-safe helper it must pass unchanged.
    """
    from apps.public_core.services.rrc_completions_extractor import (
        _write_rrc_manifest_row,  # RED: ImportError until BE2 extracts this helper
    )
    from apps.public_core.models import RetrievedDocument

    href = "https://webapps.rrc.texas.gov/CMPL/viewPdfReportFormAction.do?pkt=9999"
    filename = "W-2_gaps_loop_001.pdf"

    async def _call_from_running_loop():
        # The coroutine itself is running inside asyncio.run()'s loop, so any
        # sync ORM call made directly on this thread would raise
        # SynchronousOnlyOperation — reproducing the prod trigger.
        _write_rrc_manifest_row(
            api_number=TEST_API14,
            href=href,
            defaults={
                "well": well,
                "filename": filename,
                "local_path": f"/media/rrc/completions/{TEST_API14}/{filename}",
                "kind": "w2",
                "index_status": "pending",
                "source_type": "rrc",
            },
        )

    # Must not raise SynchronousOnlyOperation (or anything else).
    asyncio.run(_call_from_running_loop())

    assert RetrievedDocument.objects.filter(
        api_number=TEST_API14, href=href
    ).exists(), (
        "Expected _write_rrc_manifest_row to create the RetrievedDocument row "
        "even when called from within a running event loop."
    )


# ===========================================================================
# Gap 3 (hotfix regression) — is_rrc branch must persist local_path onto the
# RetrievedDocument manifest row, so the download endpoint can find the file.
#
# Distinct API14 range from TEST_API14 above, so these tests own their own
# well/research_session fixtures and can't collide on (api_number, filename)
# or (api_number, href) with the Gap 1/2 tests in this module.
# ===========================================================================

LOCAL_PATH_TEST_API14 = "42901004450000"


@pytest.fixture
def local_path_well(db):
    from apps.public_core.models import WellRegistry
    return WellRegistry.objects.create(
        api14=LOCAL_PATH_TEST_API14,
        state="TX",
        county="Test County",
    )


@pytest.fixture
def local_path_research_session(db, local_path_well, public_tenant):
    from apps.public_core.models import ResearchSession
    return ResearchSession.objects.create(
        api_number=LOCAL_PATH_TEST_API14,
        state="TX",
        status="indexing",
        tenant=public_tenant,
        well=local_path_well,
        total_documents=1,
        indexed_documents=0,
    )


def _make_local_path_doc_spec(filename: str, local_path: str) -> dict:
    return {
        "filename": filename,
        "url": "https://webapps.rrc.texas.gov/CMPL/viewPdfReportFormAction.do?pkt=555",
        "local_path": local_path,
        "file_size": 1024,
        "date": "2024-01-01",
        "doc_type": "w2",
        "metadata": {"rrc_source": True},   # forces the is_rrc branch
    }


def _make_local_path_extracted_document(well, filename, status="success"):
    from apps.public_core.models import ExtractedDocument
    return ExtractedDocument.objects.create(
        api_number=LOCAL_PATH_TEST_API14,
        well=well,
        document_type="w2",
        source_path=f"/media/rrc/completions/{LOCAL_PATH_TEST_API14}/{filename}",
        model_tag="gpt-4o",
        status=status,
        errors=[],
        json_data={"header": {}},
    )


@pytest.mark.django_db(transaction=True)
def test_is_rrc_fallback_row_persists_local_path(
    db, local_path_well, local_path_research_session
):
    """
    GREEN (hotfix regression): the fallback path — no pre-existing manifest
    row for this (api, filename) — must persist doc_spec["local_path"] onto
    the created RetrievedDocument row.

    Before the fix, ``_record_retrieved_document(...)`` was called without a
    ``local_path`` kwarg, so the fallback ``disk:`` row was created with
    local_path="" and the download endpoint returned 404 ("No local file
    path stored for this document."). The fix threads doc.local_path into
    the fallback call.
    """
    from apps.public_core.models import RetrievedDocument
    from apps.public_core.tasks_research import index_document_task

    filename = "W-2_gaps_local_001.pdf"
    local_path = (
        f"/app/ra_config/mediafiles/rrc/completions/42901004/"
        f"W-2_{filename}"
    )
    doc_spec = _make_local_path_doc_spec(filename, local_path)
    ed = _make_local_path_extracted_document(local_path_well, filename, status="success")

    with (
        patch(
            "apps.public_core.tasks_research.index_single_document",
            return_value=ed,
        ),
        patch("apps.public_core.tasks_research.finalize_session_task.delay"),
    ):
        index_document_task(
            str(local_path_research_session.id),
            doc_spec,
            state="TX",
        )

    rows = RetrievedDocument.objects.filter(
        api_number=LOCAL_PATH_TEST_API14, filename=filename
    )
    assert rows.count() == 1, (
        f"Expected exactly 1 RetrievedDocument row for "
        f"({LOCAL_PATH_TEST_API14}, {filename}), got {rows.count()}."
    )
    rd = rows.first()
    assert rd.local_path == local_path, (
        f"Expected local_path='{local_path}' persisted onto the fallback "
        f"disk: row, got '{rd.local_path}'. This is the primary regression: "
        f"before the fix, local_path was silently dropped ('')."
    )


@pytest.mark.django_db(transaction=True)
def test_is_rrc_update_path_sets_local_path_when_missing(
    db, local_path_well, local_path_research_session
):
    """
    GREEN (hotfix regression): the update path — a pre-existing manifest row
    (e.g. from the extractor's "pending" write) that has an http href but an
    EMPTY local_path — must be backfilled with doc_spec["local_path"] when
    index_document_task runs, not just flipped to a terminal status.
    """
    from apps.public_core.models import RetrievedDocument
    from apps.public_core.tasks_research import index_document_task

    filename = "W-2_gaps_local_002.pdf"
    http_href = "https://webapps.rrc.texas.gov/CMPL/viewPdfReportFormAction.do?pkt=8102"
    local_path = f"/app/ra_config/mediafiles/rrc/completions/42901004/{filename}"

    RetrievedDocument.objects.create(
        api_number=LOCAL_PATH_TEST_API14,
        href=http_href,
        well=local_path_well,
        filename=filename,
        local_path="",
        kind="w2",
        index_status="pending",
        source_type="rrc",
    )

    doc_spec = _make_local_path_doc_spec(filename, local_path)
    ed = _make_local_path_extracted_document(local_path_well, filename, status="success")

    with (
        patch(
            "apps.public_core.tasks_research.index_single_document",
            return_value=ed,
        ),
        patch("apps.public_core.tasks_research.finalize_session_task.delay"),
    ):
        index_document_task(
            str(local_path_research_session.id),
            doc_spec,
            state="TX",
        )

    rows = RetrievedDocument.objects.filter(
        api_number=LOCAL_PATH_TEST_API14, filename=filename
    )
    assert rows.count() == 1, (
        f"Expected the pre-existing pending row to be updated in place, no "
        f"duplicate, got {rows.count()} rows."
    )
    rd = rows.first()
    assert rd.index_status == "success", (
        f"Expected index_status='success', got '{rd.index_status}'"
    )
    assert rd.local_path == local_path, (
        f"Expected the empty local_path to be backfilled to '{local_path}', "
        f"got '{rd.local_path}'."
    )


@pytest.mark.django_db(transaction=True)
def test_is_rrc_update_preserves_existing_local_path_when_doc_has_none(
    db, local_path_well, local_path_research_session
):
    """
    GREEN (hotfix regression): when doc_spec has no local_path, the update
    path must NOT clobber a good pre-existing local_path with an empty
    string. The fix only sets local_path in the .update() call when
    doc.local_path is truthy.
    """
    from apps.public_core.models import RetrievedDocument
    from apps.public_core.tasks_research import index_document_task

    filename = "W-2_gaps_local_003.pdf"
    http_href = "https://webapps.rrc.texas.gov/CMPL/viewPdfReportFormAction.do?pkt=8103"
    good_local_path = "/existing/good/path.pdf"

    RetrievedDocument.objects.create(
        api_number=LOCAL_PATH_TEST_API14,
        href=http_href,
        well=local_path_well,
        filename=filename,
        local_path=good_local_path,
        kind="w2",
        index_status="pending",
        source_type="rrc",
    )

    doc_spec = _make_local_path_doc_spec(filename, local_path="")
    doc_spec["local_path"] = None
    ed = _make_local_path_extracted_document(local_path_well, filename, status="success")

    with (
        patch(
            "apps.public_core.tasks_research.index_single_document",
            return_value=ed,
        ),
        patch("apps.public_core.tasks_research.finalize_session_task.delay"),
    ):
        index_document_task(
            str(local_path_research_session.id),
            doc_spec,
            state="TX",
        )

    rows = RetrievedDocument.objects.filter(
        api_number=LOCAL_PATH_TEST_API14, filename=filename
    )
    assert rows.count() == 1, (
        f"Expected the pre-existing row to be updated in place, no "
        f"duplicate, got {rows.count()} rows."
    )
    rd = rows.first()
    assert rd.local_path == good_local_path, (
        f"Expected the pre-existing good local_path to be preserved when "
        f"doc_spec has no local_path, got '{rd.local_path}'. A None/empty "
        f"doc.local_path must never clobber a good stored path."
    )
