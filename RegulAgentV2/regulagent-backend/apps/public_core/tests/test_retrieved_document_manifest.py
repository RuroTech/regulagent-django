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
