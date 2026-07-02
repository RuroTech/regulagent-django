"""
TDD RED PHASE — signed-token auth for the document-download endpoint.

Bug: ``RetrievedDocumentDownloadView`` (apps/public_core/views/retrieved_document_download.py)
requires ``IsAuthenticated`` + a Bearer token, but the frontend renders the
download URL as a plain ``<a href>``.  When a browser navigates to it directly
(no ``Authorization`` header), the request 401s instead of serving the file.

Fix under test (not yet implemented)
-------------------------------------
``apps/public_core/views/retrieved_document_download.py`` gains two
module-level helpers:

    make_download_token(rd_id) -> str
    verify_download_token(token, pk) -> bool

Signing uses ``django.core.signing.TimestampSigner(salt="retrieved-document-download")``.
A token is minted as ``signer.sign(str(rd.id))``.  The view authorizes a
request if EITHER ``request.user.is_authenticated`` (existing Bearer path) OR
a ``token`` query param that unsigns successfully AND whose unsigned value
equals ``str(pk)``.  Otherwise 401.

``apps/public_core/views/research.py`` (``ResearchSessionDocumentsView``) will
append ``?token=<token>`` to the ``download_url`` it builds for LOCAL-file
rows (rows whose href is NOT an http/https URL); rows with an http(s) href
are left unchanged.

Everything in this file must FAIL against current code (401 where 200 is
expected; ImportError for the two helpers) and turn green, unmodified, once
BE1 implements the fix.
"""

import tempfile
from pathlib import Path

import pytest
from rest_framework.test import APIClient

# ---------------------------------------------------------------------------
# Helpers — copied from test_retrieved_document_manifest.py's tenant/user/auth
# setup pattern (public-schema tenant + force_authenticate APIClient).
# ---------------------------------------------------------------------------


def _make_public_tenant():
    from apps.tenants.models import Tenant, Domain
    from django.contrib.auth import get_user_model
    from django.db import connection as db_conn, transaction

    User = get_user_model()
    owner, _ = User.objects.get_or_create(
        email="download_auth_public_owner@test.internal",
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


# Distinct API14 range for this file — not used elsewhere in the suite.
TEST_API14 = "42901888880000"


@pytest.fixture
def public_tenant(db):
    return _make_public_tenant()


@pytest.fixture
def test_user(db, public_tenant):
    return _make_user("download_auth_user@test.internal", tenant=public_tenant)


@pytest.fixture
def auth_client(test_user, public_tenant):
    client = APIClient()
    client.force_authenticate(user=test_user)
    return client


def _make_pdf_file(tmp_path, name="doc.pdf"):
    """Write a small real file to disk and return its absolute path."""
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4\n%mock pdf content for download_auth tests\n")
    return str(path)


@pytest.fixture
def rd(db, tmp_path):
    from apps.public_core.models import RetrievedDocument

    local_path = _make_pdf_file(tmp_path, "download_auth_doc.pdf")
    return RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href="/CMPL/viewPdfReportFormAction.do?pkt=8801",
        filename="download_auth_doc.pdf",
        local_path=local_path,
        kind="w2",
        index_status="success",
        source_type="rrc",
    )


@pytest.fixture
def rd_other(db, tmp_path):
    """A second RetrievedDocument, used for the token-must-be-bound-to-pk test."""
    from apps.public_core.models import RetrievedDocument

    local_path = _make_pdf_file(tmp_path, "download_auth_doc_other.pdf")
    return RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href="/CMPL/viewPdfReportFormAction.do?pkt=8802",
        filename="download_auth_doc_other.pdf",
        local_path=local_path,
        kind="w2",
        index_status="success",
        source_type="rrc",
    )


def _download_url(rd_id: int) -> str:
    return f"/api/retrieved-documents/{rd_id}/download/"


# ---------------------------------------------------------------------------
# TEST 1 — baseline: no auth, no token -> 401.
# Pins today's behavior; must keep passing after the fix (unauthenticated,
# tokenless requests are still rejected).
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_download_no_auth_no_token_returns_401(rd):
    client = APIClient()
    resp = client.get(_download_url(rd.id))
    assert resp.status_code == 401, (
        f"Expected 401 with no auth and no token, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# TEST 2 — genuine RED: a valid signed token, with NO Bearer auth, should
# serve the file. Today the token query param is ignored entirely, so this
# 401s.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_download_with_valid_signed_token_serves_file(rd):
    from apps.public_core.views.retrieved_document_download import make_download_token

    token = make_download_token(rd.id)
    client = APIClient()
    resp = client.get(f"{_download_url(rd.id)}?token={token}")

    assert resp.status_code == 200, (
        f"Expected 200 for a valid signed token with no Bearer auth, got {resp.status_code}"
    )
    assert resp["Content-Type"].startswith("application/pdf"), (
        f"Expected application/pdf content type, got {resp['Content-Type']}"
    )
    body = b"".join(resp.streaming_content)
    assert body == Path(rd.local_path).read_bytes(), (
        "Streamed body did not match the file on disk"
    )


# ---------------------------------------------------------------------------
# TEST 3 — a tampered token must NOT authorize the request.
#
# NOTE: today this is trivially green (the view 401s because it ignores the
# token entirely, tampered or not). It only becomes a *meaningful* red/green
# signal once the token path exists, which is why we assert the tamper case
# alongside a positive control (a genuinely valid token on the same pk
# succeeds) so a no-op "always 401" implementation cannot pass both halves.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_download_tampered_token_returns_401(rd):
    from apps.public_core.views.retrieved_document_download import make_download_token

    token = make_download_token(rd.id)
    tampered = token[:-1] + ("x" if token[-1] != "x" else "y")

    client = APIClient()
    tampered_resp = client.get(f"{_download_url(rd.id)}?token={tampered}")
    assert tampered_resp.status_code == 401, (
        f"Expected 401 for a tampered token, got {tampered_resp.status_code}"
    )

    # Positive control: the real token for the same pk must succeed. This is
    # the genuine-red half — it fails today because the token path doesn't
    # exist yet.
    valid_resp = client.get(f"{_download_url(rd.id)}?token={token}")
    assert valid_resp.status_code == 200, (
        f"Expected 200 for the untampered token (positive control), got {valid_resp.status_code}"
    )


# ---------------------------------------------------------------------------
# TEST 4 — a token minted for one RetrievedDocument must not authorize
# download of a different one.
#
# Same trivially-green-for-the-wrong-reason caveat as test 3: today both
# requests 401 because tokens are ignored. The positive control (rd_other's
# own token on rd_other's URL succeeding) is what makes this a genuine red
# test.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_download_token_bound_to_pk(rd, rd_other):
    from apps.public_core.views.retrieved_document_download import make_download_token

    token_for_rd = make_download_token(rd.id)
    client = APIClient()

    cross_resp = client.get(f"{_download_url(rd_other.id)}?token={token_for_rd}")
    assert cross_resp.status_code == 401, (
        f"Expected 401 when a token minted for rd={rd.id} is used against "
        f"rd_other={rd_other.id}, got {cross_resp.status_code}"
    )

    # Positive control: rd_other's own token on rd_other's URL must succeed.
    token_for_other = make_download_token(rd_other.id)
    own_resp = client.get(f"{_download_url(rd_other.id)}?token={token_for_other}")
    assert own_resp.status_code == 200, (
        f"Expected 200 when rd_other's own token is used on its own URL, got {own_resp.status_code}"
    )


# ---------------------------------------------------------------------------
# TEST 5 — the existing Bearer-auth path must keep working with no token.
# Should pass today AND after the fix (regression guard).
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_download_valid_bearer_still_works(auth_client, rd):
    resp = auth_client.get(_download_url(rd.id))
    assert resp.status_code == 200, (
        f"Expected 200 for an authenticated Bearer request, got {resp.status_code}"
    )
    body = b"".join(resp.streaming_content)
    assert body == Path(rd.local_path).read_bytes()


# ---------------------------------------------------------------------------
# TEST 6 — unit test for the signing helpers themselves.
# FAILS today with ImportError (helpers don't exist yet).
# ---------------------------------------------------------------------------

def test_verify_download_token_helper():
    from apps.public_core.views.retrieved_document_download import (
        make_download_token,
        verify_download_token,
    )

    token = make_download_token("174")
    assert verify_download_token(token, 174) is True, (
        "A token minted for pk=174 must verify against pk=174"
    )
    assert verify_download_token(token, 999) is False, (
        "A token minted for pk=174 must NOT verify against pk=999"
    )
    assert verify_download_token("not-a-real-token", 174) is False, (
        "A garbage string must not verify"
    )


# ---------------------------------------------------------------------------
# TEST 7 — the research-documents endpoint appends a signed token to
# download_url for LOCAL-file rows, and leaves http(s) hrefs unchanged.
#
# Exercised through the real endpoint (ResearchSessionDocumentsView, GET
# /api/research/sessions/{id}/documents/) the same way
# test_retrieved_document_manifest.py::test_documents_api_returns_manifest_counts_and_list
# does.
# ---------------------------------------------------------------------------

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
        total_documents=2,
        indexed_documents=2,
    )


@pytest.mark.django_db
def test_research_documents_url_includes_token_for_local_files(
    auth_client, well, research_session, tmp_path
):
    from apps.public_core.models import RetrievedDocument

    local_path = _make_pdf_file(tmp_path, "research_docs_local.pdf")
    rd_local = RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href="/CMPL/viewPdfReportFormAction.do?pkt=8901",
        well=well,
        filename="research_docs_local.pdf",
        local_path=local_path,
        kind="w2",
        index_status="success",
        source_type="rrc",
    )
    rd_remote = RetrievedDocument.objects.create(
        api_number=TEST_API14,
        href="https://ocd.example.gov/documents/remote.pdf",
        well=well,
        filename="remote.pdf",
        local_path="",
        kind="c103",
        index_status="success",
        source_type="rrc",
    )

    resp = auth_client.get(f"/api/research/sessions/{research_session.id}/documents/")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    by_id = {item["id"]: item for item in resp.data["retrieved_documents"]}

    local_url = by_id[rd_local.id]["download_url"]
    assert "token=" in local_url, (
        f"Expected local-file download_url to include a signed token, got: {local_url}"
    )

    remote_url = by_id[rd_remote.id]["download_url"]
    assert remote_url == rd_remote.href, (
        f"Expected an http(s) href to pass through unchanged (no token appended), got: {remote_url}"
    )
