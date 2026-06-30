"""
TDD RED PHASE — RetrievedDocument model + manifest API.

These tests are written BEFORE the feature is implemented.  Every test here
must FAIL (ImportError / AttributeError / AssertionError / KeyError) until
the model and API changes are built.

Feature: Research Documents — fetch-all + manifest (Trello qubVjWuF)

Contract under test
-------------------
Model  ``RetrievedDocument`` (apps/public_core/models):
  - well             FK(WellRegistry, null=True, on_delete=SET_NULL)
  - api_number       CharField(16), db_index
  - href             TextField
  - filename         CharField(255)
  - local_path       TextField
  - file_hash        CharField(64, blank=True)
  - kind             CharField(64, blank=True)
  - index_status     CharField(32) choices pending|success|partial|error|
                     unsupported|skipped_directional|no_forms|quota_exceeded|
                     failed  (default "pending")
  - extracted_document FK(ExtractedDocument, null=True, on_delete=SET_NULL)
  - source_type      CharField default "rrc"
  - downloaded_at    auto_now_add
  - updated_at       auto_now
  - UniqueConstraint on (api_number, href)

API  GET /api/research/sessions/{session_id}/documents/
  Existing keys are preserved; new keys added:
  - total_documents        count of RetrievedDocument for session api_number
  - remembered_documents   count where index_status == "success"
  - retrieved_documents    list of {id, filename, kind, document_type,
                           index_status, remembered, download_url, created_at}

Async-ORM teardown note
-----------------------
parts of this pipeline write via run_in_executor which commits outside the
per-test transaction. The root conftest.py applies CASCADE-flush + autouse
purge. We use a distinct test api14 range (42901xxxxx0000) so rows from
concurrent/parallel runs don't collide.  We do NOT add parallel test infra.
"""

import uuid
import pytest
from unittest.mock import patch, MagicMock
from rest_framework.test import APIClient


# ---------------------------------------------------------------------------
# Helpers — reuse tenant helpers from the isolation test module
# ---------------------------------------------------------------------------

def _make_public_tenant():
    from apps.tenants.models import Tenant, Domain
    from django.contrib.auth import get_user_model
    from django.db import connection as db_conn, transaction

    User = get_user_model()
    owner, _ = User.objects.get_or_create(
        email="rd_manifest_public_owner@test.internal",
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


def _make_user(email: str, tenant=None):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user, _ = User.objects.get_or_create(
        email=email,
        defaults={"is_active": True, "first_name": "", "last_name": ""},
    )
    if tenant is not None:
        user.tenants.add(tenant)
    return user


# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------

TEST_API14 = "42901007570000"  # unique range, not used elsewhere in tests


@pytest.fixture
def public_tenant(db):
    return _make_public_tenant()


@pytest.fixture
def test_user(db, public_tenant):
    return _make_user("rd_manifest_user@test.internal", tenant=public_tenant)


@pytest.fixture
def auth_client(test_user, public_tenant):
    client = APIClient()
    client.force_authenticate(user=test_user)
    return client


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
        status="ready",
        tenant=public_tenant,
        well=well,
        total_documents=3,
        indexed_documents=3,
    )


# ---------------------------------------------------------------------------
# TEST 1 — Model import: RetrievedDocument must be importable from
#           apps.public_core.models (will fail until model is added).
# ---------------------------------------------------------------------------

def test_retrieved_document_model_is_importable():
    """
    RED: ImportError until RetrievedDocument is defined and exported in
    apps/public_core/models/__init__.py.
    """
    from apps.public_core.models import RetrievedDocument  # noqa: F401


# ---------------------------------------------------------------------------
# TEST 2 — Manifest creation + deduplication on download.
#
# Given N hrefs with 1 duplicate, after the download step there are N-1
# RetrievedDocument rows (dedupe on (api_number, href) unique constraint).
#
# We simulate the manifest-write logic by calling the helper that is expected
# to create/get-or-create rows.  Since the model does not yet exist this test
# will fail at the import stage.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_manifest_deduplication_on_download(well):
    """
    RED: RetrievedDocument.objects does not exist yet.

    After the feature ships:
    - Provide 4 hrefs (with 1 duplicate).
    - Create/get_or_create rows for each href.
    - Expect 3 rows in the DB (the duplicate is deduplicated by the unique
      constraint on (api_number, href)).
    """
    from apps.public_core.models import RetrievedDocument

    hrefs = [
        "/CMPL/viewPdfReportFormAction.do?pkt=1001",
        "/CMPL/viewPdfReportFormAction.do?pkt=1002",
        "/CMPL/viewPdfReportFormAction.do?pkt=1003",
        "/CMPL/viewPdfReportFormAction.do?pkt=1001",  # duplicate
    ]

    created_count = 0
    for i, href in enumerate(hrefs):
        _, created = RetrievedDocument.objects.get_or_create(
            api_number=TEST_API14,
            href=href,
            defaults={
                "well": well,
                "filename": f"doc_{i:03d}.pdf",
                "local_path": f"/media/rrc/completions/{TEST_API14}/doc_{i:03d}.pdf",
                "kind": "w15",
                "index_status": "pending",
                "source_type": "rrc",
            },
        )
        if created:
            created_count += 1

    total = RetrievedDocument.objects.filter(api_number=TEST_API14).count()
    assert total == 3, (
        f"Expected 3 rows after deduplication (4 hrefs - 1 duplicate), got {total}"
    )
    assert created_count == 3, (
        f"Expected 3 creates (duplicate should return existing), got {created_count}"
    )


# ---------------------------------------------------------------------------
# TEST 3 — Directional survey: downloaded → skipped_directional status,
#           NO ExtractedDocument created.
#
# Current behavior: directional surveys are silently SKIPPED at download time
# and never reach the manifest.
# New behavior: a RetrievedDocument IS created with index_status=
# "skipped_directional" and NO ExtractedDocument linked.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_directional_survey_gets_manifest_row_with_skipped_status(well):
    """
    RED: RetrievedDocument does not exist yet; also directional-survey filename
    currently causes the row to be skipped before creation.
    """
    from apps.public_core.models import RetrievedDocument, ExtractedDocument

    directional_filename = "Directional_Survey_42901007570000_001.pdf"
    directional_href = "/CMPL/viewPdfReportFormAction.do?pkt=9999"

    # Simulate the new behavior: manifest row IS created for directional surveys
    rd = RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href=directional_href,
        well=well,
        filename=directional_filename,
        local_path=f"/media/rrc/completions/{TEST_API14}/{directional_filename}",
        kind="",
        index_status="skipped_directional",
        source_type="rrc",
    )

    assert rd.index_status == "skipped_directional", (
        f"Expected index_status='skipped_directional', got '{rd.index_status}'"
    )
    assert rd.extracted_document is None, (
        "Directional survey row must NOT be linked to any ExtractedDocument"
    )

    # Also verify: NO ExtractedDocument was created for this filename
    ed_count = ExtractedDocument.objects.filter(
        api_number=TEST_API14,
        source_path__contains=directional_filename,
    ).count()
    assert ed_count == 0, (
        f"Expected 0 ExtractedDocuments for directional survey, found {ed_count}"
    )


# ---------------------------------------------------------------------------
# TEST 4 — No-ED outcomes (no_forms / quota_exceeded / worker-killed)
#           still produce a manifest row.
#
# Currently these paths increment the session counter and log the failure but
# create NO RetrievedDocument.  After the feature ships they must create one
# with the appropriate index_status.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize("outcome_status,href_suffix", [
    ("no_forms",        "pkt=2001"),
    ("quota_exceeded",  "pkt=2002"),
    ("failed",          "pkt=2003"),   # worker SIGKILL path
])
def test_no_ed_outcomes_get_manifest_row(well, outcome_status, href_suffix):
    """
    RED: RetrievedDocument does not exist yet.

    After the feature ships: each outcome must produce a row with the
    corresponding index_status and NO linked extracted_document.
    """
    from apps.public_core.models import RetrievedDocument

    href = f"/CMPL/viewPdfReportFormAction.do?{href_suffix}"
    filename = f"doc_{outcome_status}.pdf"

    rd = RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href=href,
        well=well,
        filename=filename,
        local_path=f"/media/rrc/completions/{TEST_API14}/{filename}",
        kind="",
        index_status=outcome_status,
        source_type="rrc",
    )

    assert rd.index_status == outcome_status, (
        f"Expected index_status='{outcome_status}', got '{rd.index_status}'"
    )
    assert rd.extracted_document is None, (
        f"No-ED outcome '{outcome_status}' must NOT be linked to an ExtractedDocument"
    )

    # Confirm row is retrievable
    db_rd = RetrievedDocument.objects.get(api_number=TEST_API14, href=href)
    assert db_rd.index_status == outcome_status


# ---------------------------------------------------------------------------
# TEST 5 — API returns total_documents, remembered_documents, and
#           retrieved_documents with the correct shape.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_documents_api_returns_manifest_counts_and_list(
    auth_client, well, research_session
):
    """
    RED: The current ResearchSessionDocumentsView does not query RetrievedDocument
    at all, so:
    - 'total_documents' is the session-level counter (not RD count)
    - 'remembered_documents' key does not exist
    - 'retrieved_documents' key does not exist

    After the feature ships:
    - total_documents  == count of RetrievedDocument for the session api_number
    - remembered_documents == count of RD with index_status='success'
    - retrieved_documents  includes all RD rows (including non-success)
      each row has: id, filename, kind, document_type, index_status,
      remembered (bool), download_url (non-empty str), created_at
    """
    from apps.public_core.models import RetrievedDocument, ExtractedDocument

    # Create one success RD linked to an ED
    ed = ExtractedDocument.objects.create(
        api_number=TEST_API14,
        well=well,
        document_type="w15",
        source_path=f"/media/rrc/completions/{TEST_API14}/W-15_001.pdf",
        model_tag="gpt-4o",
        status="success",
        errors=[],
        json_data={"header": {}},
    )
    rd_success = RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href="/CMPL/viewPdfReportFormAction.do?pkt=3001",
        well=well,
        filename="W-15_001.pdf",
        local_path=f"/media/rrc/completions/{TEST_API14}/W-15_001.pdf",
        kind="w15",
        index_status="success",
        extracted_document=ed,
        source_type="rrc",
    )

    # Create one error RD (no ED)
    rd_error = RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href="/CMPL/viewPdfReportFormAction.do?pkt=3002",
        well=well,
        filename="unknown_001.pdf",
        local_path=f"/media/rrc/completions/{TEST_API14}/unknown_001.pdf",
        kind="",
        index_status="error",
        source_type="rrc",
    )

    # Create one skipped_directional RD (no ED)
    rd_dir = RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href="/CMPL/viewPdfReportFormAction.do?pkt=3003",
        well=well,
        filename="Directional_Survey_001.pdf",
        local_path=f"/media/rrc/completions/{TEST_API14}/Directional_Survey_001.pdf",
        kind="",
        index_status="skipped_directional",
        source_type="rrc",
    )

    resp = auth_client.get(
        f"/api/research/sessions/{research_session.id}/documents/"
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    data = resp.data

    # --- New keys must exist ---
    assert "total_documents" in data, (
        "Response must include 'total_documents' (count of RetrievedDocument rows)"
    )
    assert "remembered_documents" in data, (
        "Response must include 'remembered_documents' (count of RD with index_status='success')"
    )
    assert "retrieved_documents" in data, (
        "Response must include 'retrieved_documents' list"
    )

    # --- Count correctness ---
    assert data["total_documents"] == 3, (
        f"Expected total_documents=3 (all RD rows), got {data['total_documents']}"
    )
    assert data["remembered_documents"] == 1, (
        f"Expected remembered_documents=1 (only success row), got {data['remembered_documents']}"
    )

    # --- Shape of retrieved_documents list ---
    rd_list = data["retrieved_documents"]
    assert len(rd_list) == 3, (
        f"Expected 3 items in retrieved_documents, got {len(rd_list)}"
    )

    # Check each item has the required fields
    required_fields = {"id", "filename", "kind", "document_type",
                       "index_status", "remembered", "download_url", "created_at"}
    for item in rd_list:
        missing = required_fields - set(item.keys())
        assert not missing, f"retrieved_documents item missing fields: {missing}"

    # --- Non-success rows have a non-empty download_url ---
    non_success = [r for r in rd_list if r["index_status"] != "success"]
    assert len(non_success) == 2, (
        f"Expected 2 non-success rows, got {len(non_success)}"
    )
    for item in non_success:
        assert item["download_url"], (
            f"Non-success row {item['filename']} must have a non-empty download_url"
        )

    # --- Success row is marked remembered=True ---
    success_rows = [r for r in rd_list if r["index_status"] == "success"]
    assert len(success_rows) == 1
    assert success_rows[0]["remembered"] is True, (
        "Success row must have remembered=True"
    )

    # --- existing extracted_documents key is still present ---
    assert "extracted_documents" in data, (
        "Existing 'extracted_documents' key must still be present (additive change)"
    )


# ---------------------------------------------------------------------------
# TEST 6 — api_number matching uses normalize_api_14digit.
#
# RetrievedDocument rows stored under a normalized api14 are returned when the
# session api_number is the same well in a different format (e.g. hyphenated).
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_api_number_matching_uses_normalization(auth_client, well, db):
    """
    RED: RetrievedDocument does not exist yet.

    After the feature ships: rows keyed by normalized api14 (42901007570000)
    must appear in the documents API when the session api_number resolves to
    the same well, regardless of input format.
    """
    from apps.public_core.models import RetrievedDocument, ResearchSession
    from apps.public_core.services.api_normalization import normalize_api_14digit

    # Normalized form of the same well
    normalized_api14 = normalize_api_14digit(TEST_API14)
    assert normalized_api14 == TEST_API14, (
        f"Normalization sanity: expected {TEST_API14}, got {normalized_api14}"
    )

    # Create a manifest row under the normalized api14
    rd = RetrievedDocument.objects.create(
        api_number=normalized_api14,
        href="/CMPL/viewPdfReportFormAction.do?pkt=4001",
        well=well,
        filename="W-2_42901007570000_001.pdf",
        local_path=f"/media/rrc/completions/{normalized_api14}/W-2_42901007570000_001.pdf",
        kind="w2",
        index_status="success",
        source_type="rrc",
    )

    # Create a session whose api_number is in hyphenated format but resolves to the same well
    # (The view should normalize and match.)
    from apps.tenants.models import Tenant
    public_tenant = Tenant.objects.get(schema_name="public")
    session = ResearchSession.objects.create(
        api_number="42-901-00757-0000",   # hyphenated — normalizes to same api14
        state="TX",
        status="ready",
        tenant=public_tenant,
        well=well,
        total_documents=1,
        indexed_documents=1,
    )

    resp = auth_client.get(
        f"/api/research/sessions/{session.id}/documents/"
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    data = resp.data

    # The manifest row must be visible regardless of api_number format in the session
    assert "retrieved_documents" in data, (
        "Response must include 'retrieved_documents'"
    )
    assert data["total_documents"] >= 1, (
        f"Expected at least 1 RetrievedDocument, got {data['total_documents']}. "
        "Matching must normalize the session api_number to find RD rows."
    )

    filenames = [r["filename"] for r in data.get("retrieved_documents", [])]
    assert "W-2_42901007570000_001.pdf" in filenames, (
        "The manifest row created under the normalized api14 must appear in "
        "retrieved_documents even when the session api_number is hyphenated. "
        f"Found filenames: {filenames}"
    )


# ---------------------------------------------------------------------------
# TDD RED PHASE — _record_retrieved_document helper (manifest-write fix)
#
# These tests define the contract for the helper function the BE must add to
# apps/public_core/tasks_research.py.  They ALL fail right now because the
# function does not exist (ImportError).  Once BE implements the helper the
# tests must turn green without modification.
#
# EXACT signature the BE must implement:
#
#   def _record_retrieved_document(
#       session: ResearchSession,
#       doc: DocumentSpec,
#       *,
#       index_status: str,
#       file_hash: str = "",
#       local_path: str = "",
#       extracted_document=None,
#   ) -> RetrievedDocument:
#
# Behaviour contract (pinned by tests below):
#   1. Creates a RetrievedDocument row with:
#        api_number  = normalize_api_14digit(session.api_number)
#        href        = f"disk:{api_number}:{doc.filename}"
#        filename    = doc.filename
#        well        = session.well
#        source_type = "neubus"
#        index_status, local_path, file_hash, extracted_document as passed
#   2. Idempotent: calling twice (same session + doc.filename) does NOT create
#      a duplicate — it UPDATEs via update_or_create(api_number, href).
#      Count stays 1; second call's index_status wins.
#   3. api_number is normalised regardless of session.api_number format
#      (8-/10-digit inputs must produce a stored 14-digit value).
#   4. Does NOT collide with a pre-existing source_type="rrc" row for the same
#      api_number but a different filename/href — both rows must coexist.
# ---------------------------------------------------------------------------

# Distinct API14 for this test group — not used anywhere else in the suite
NEUBUS_API14 = "42901999990000"


@pytest.fixture
def neubus_well(db):
    from apps.public_core.models import WellRegistry
    return WellRegistry.objects.create(
        api14=NEUBUS_API14,
        state="TX",
        county="Neubus Test County",
    )


@pytest.fixture
def neubus_session(db, neubus_well, public_tenant):
    from apps.public_core.models import ResearchSession
    return ResearchSession.objects.create(
        api_number=NEUBUS_API14,
        state="TX",
        status="ready",
        tenant=public_tenant,
        well=neubus_well,
        total_documents=1,
        indexed_documents=0,
    )


@pytest.fixture
def sample_neubus_doc():
    from apps.public_core.services.adapters.base import DocumentSpec
    return DocumentSpec(
        filename="W2_neubus_test.pdf",
        local_path=f"/media/neubus/{NEUBUS_API14}/W2_neubus_test.pdf",
    )


# ---------------------------------------------------------------------------
# TEST 7 — _record_retrieved_document creates a row with all correct fields.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_record_retrieved_document_creates_row_with_correct_fields(
    neubus_session, neubus_well, sample_neubus_doc
):
    """
    RED: ImportError — _record_retrieved_document does not exist yet.

    After BE implements the helper:
    - Exactly 1 RetrievedDocument row is created.
    - api_number equals the normalised 14-digit form of session.api_number.
    - href == f"disk:{api_number}:{doc.filename}".
    - filename, well, source_type, index_status, file_hash, local_path are
      stored as passed; extracted_document is None.
    """
    from apps.public_core.tasks_research import _record_retrieved_document  # RED: ImportError
    from apps.public_core.models import RetrievedDocument

    rd = _record_retrieved_document(
        neubus_session,
        sample_neubus_doc,
        index_status="pending",
        file_hash="deadbeef01234567",
        local_path=f"/media/neubus/{NEUBUS_API14}/W2_neubus_test.pdf",
    )

    expected_href = f"disk:{NEUBUS_API14}:W2_neubus_test.pdf"

    assert rd.api_number == NEUBUS_API14, (
        f"Expected api_number='{NEUBUS_API14}', got '{rd.api_number}'"
    )
    assert rd.href == expected_href, (
        f"Expected href='{expected_href}', got '{rd.href}'"
    )
    assert rd.filename == "W2_neubus_test.pdf", (
        f"Expected filename='W2_neubus_test.pdf', got '{rd.filename}'"
    )
    assert rd.well_id == neubus_well.id, (
        f"Expected well FK to neubus_well.id={neubus_well.id}, got {rd.well_id}"
    )
    assert rd.source_type == "neubus", (
        f"Expected source_type='neubus', got '{rd.source_type}'"
    )
    assert rd.index_status == "pending", (
        f"Expected index_status='pending', got '{rd.index_status}'"
    )
    assert rd.file_hash == "deadbeef01234567", (
        f"Expected file_hash='deadbeef01234567', got '{rd.file_hash}'"
    )
    assert rd.local_path == f"/media/neubus/{NEUBUS_API14}/W2_neubus_test.pdf", (
        f"Unexpected local_path: '{rd.local_path}'"
    )
    assert rd.extracted_document is None, (
        "extracted_document must be None when not passed"
    )

    # Confirm the row is persisted
    assert RetrievedDocument.objects.filter(
        api_number=NEUBUS_API14, href=expected_href
    ).exists(), "Row was not persisted to the database"


# ---------------------------------------------------------------------------
# TEST 8 — _record_retrieved_document is idempotent: second call updates the
#           existing row rather than creating a duplicate.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_record_retrieved_document_is_idempotent(neubus_session, sample_neubus_doc):
    """
    RED: ImportError — _record_retrieved_document does not exist yet.

    After BE implements the helper:
    - First call  → creates row with index_status='pending'.
    - Second call → updates the same row to index_status='success'.
    - Total row count for (api_number, href) remains 1.
    """
    from apps.public_core.tasks_research import _record_retrieved_document  # RED: ImportError
    from apps.public_core.models import RetrievedDocument

    expected_href = f"disk:{NEUBUS_API14}:W2_neubus_test.pdf"

    # First call: pending
    _record_retrieved_document(
        neubus_session,
        sample_neubus_doc,
        index_status="pending",
    )

    # Second call: success
    rd2 = _record_retrieved_document(
        neubus_session,
        sample_neubus_doc,
        index_status="success",
        file_hash="updated_hash_abc",
    )

    # Must be exactly 1 row — unique constraint on (api_number, href)
    count = RetrievedDocument.objects.filter(
        api_number=NEUBUS_API14,
        href=expected_href,
    ).count()
    assert count == 1, (
        f"Expected 1 row after idempotent upsert, got {count}. "
        "Helper must use update_or_create, not get_or_create or blind create."
    )

    # The returned object must reflect the SECOND call's values
    assert rd2.index_status == "success", (
        f"Expected index_status='success' after second call, got '{rd2.index_status}'"
    )
    assert rd2.file_hash == "updated_hash_abc", (
        f"Expected file_hash='updated_hash_abc', got '{rd2.file_hash}'"
    )


# ---------------------------------------------------------------------------
# TEST 9 — api_number is normalised: a 10-digit session.api_number produces a
#           stored 14-digit value.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_record_retrieved_document_normalizes_api_number(
    neubus_well, public_tenant, sample_neubus_doc
):
    """
    RED: ImportError — _record_retrieved_document does not exist yet.

    After BE implements the helper:
    - A session with api_number='4290199999' (10-digit) must produce a
      RetrievedDocument row with api_number='42901999990000' (14-digit).
    - The href also uses the normalised 14-digit form.
    """
    from apps.public_core.tasks_research import _record_retrieved_document  # RED: ImportError
    from apps.public_core.models import RetrievedDocument, ResearchSession

    # 10-digit form; normalize_api_14digit("4290199999") == "42901999990000"
    short_api = "4290199999"
    session_short = ResearchSession.objects.create(
        api_number=short_api,
        state="TX",
        status="ready",
        tenant=public_tenant,
        well=neubus_well,
        total_documents=1,
        indexed_documents=0,
    )

    rd = _record_retrieved_document(
        session_short,
        sample_neubus_doc,
        index_status="success",
    )

    assert rd.api_number == NEUBUS_API14, (
        f"Expected normalised api_number='{NEUBUS_API14}' (14-digit), "
        f"got '{rd.api_number}'. "
        "Helper must call normalize_api_14digit(session.api_number)."
    )
    expected_href = f"disk:{NEUBUS_API14}:W2_neubus_test.pdf"
    assert rd.href == expected_href, (
        f"Expected href='{expected_href}' (uses normalised api_number), got '{rd.href}'"
    )

    db_rd = RetrievedDocument.objects.get(api_number=NEUBUS_API14, href=expected_href)
    assert db_rd.api_number == NEUBUS_API14


# ---------------------------------------------------------------------------
# TEST 10 — A pre-existing source_type='rrc' row for the same api_number but
#            a different filename/href is NOT touched; both rows coexist.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_record_retrieved_document_no_collision_with_rrc_row(
    neubus_session, neubus_well
):
    """
    RED: ImportError — _record_retrieved_document does not exist yet.

    After BE implements the helper:
    - A pre-existing RetrievedDocument with source_type='rrc' and a different
      href must survive untouched.
    - The helper creates a separate row with source_type='neubus'.
    - Total row count for api_number=NEUBUS_API14 is 2.
    """
    from apps.public_core.tasks_research import _record_retrieved_document  # RED: ImportError
    from apps.public_core.models import RetrievedDocument
    from apps.public_core.services.adapters.base import DocumentSpec

    # Pre-create an RRC row (different filename → different href)
    rrc_href = f"/CMPL/viewPdfReportFormAction.do?pkt=rrc9001"
    RetrievedDocument.objects.create(
        api_number=NEUBUS_API14,
        href=rrc_href,
        well=neubus_well,
        filename="W-2_rrc_preexisting.pdf",
        local_path=f"/media/rrc/completions/{NEUBUS_API14}/W-2_rrc_preexisting.pdf",
        kind="w2",
        index_status="success",
        source_type="rrc",
    )

    # Call helper with a different filename → different href → new row
    neubus_doc = DocumentSpec(filename="W2_neubus_new.pdf")
    rd = _record_retrieved_document(
        neubus_session,
        neubus_doc,
        index_status="pending",
    )

    # Both rows must coexist
    total = RetrievedDocument.objects.filter(api_number=NEUBUS_API14).count()
    assert total == 2, (
        f"Expected 2 rows (1 rrc + 1 neubus) for api_number={NEUBUS_API14}, got {total}. "
        "Helper must key uniqueness on (api_number, href); different filename → different href."
    )

    # RRC row is untouched
    rrc_row = RetrievedDocument.objects.get(href=rrc_href)
    assert rrc_row.source_type == "rrc", (
        f"RRC row source_type was modified; expected 'rrc', got '{rrc_row.source_type}'"
    )
    assert rrc_row.filename == "W-2_rrc_preexisting.pdf"

    # Neubus row has correct shape
    neubus_href = f"disk:{NEUBUS_API14}:W2_neubus_new.pdf"
    neubus_row = RetrievedDocument.objects.get(href=neubus_href)
    assert neubus_row.source_type == "neubus", (
        f"Expected neubus row source_type='neubus', got '{neubus_row.source_type}'"
    )
    assert neubus_row.index_status == "pending"
