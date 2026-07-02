"""
TDD RED PHASE — optional/soft API verification on document upload + 'other'
document type.

Bug 1 (file_validation.py:444-450): ``validate_uploaded_file`` builds
``api_result`` via ``verify_api_number`` and, on failure, does::

    all_errors.extend(api_result.errors)
    return ValidationResult(is_valid=False, errors=all_errors, warnings=all_warnings)

— dropping ``api_result.warning_code`` and ``api_result.extracted_api``. The
view (``document_upload.py`` Step 3.5) branches on
``validation_result.warning_code in ("api_not_found", "api_mismatch")`` to
turn a would-be hard failure into a soft 200 "warning" (or, if the caller
passed ``confirmed=true``, a 201 that stores the document flagged
``is_validated=False``). Because ``warning_code`` is always ``None`` today,
every API mismatch/not-found case falls through to the hard-400 ``else``
branch.

Fix under test (not yet implemented)
-------------------------------------
1. ``validate_uploaded_file`` forwards ``api_result.warning_code`` /
   ``api_result.extracted_api`` onto the ``ValidationResult`` it returns on
   API-verification failure.
2. ``DocumentUploadView`` accepts ``document_type='other'``: skips the LLM
   extraction call (``extract_json_from_pdf``) entirely, embeds the raw PDF
   text as ``json_data={"_raw_text": <text>}``, and never runs API
   cross-verification (there's no structured API field to check). The
   resulting document stays tenant-private (not in ``PUBLIC_DOC_TYPES``).

Everything in this file must FAIL against current code (400 where 200/201 is
expected, dropped ``warning_code``, ``document_type='other'`` rejected) and
turn green, unmodified, once BE implements the fix.

Patch targets (verified against apps/public_core/views/document_upload.py
top-level imports, lines 33-37):
    apps.public_core.views.document_upload.extract_json_from_pdf
    apps.public_core.views.document_upload.vectorize_extracted_document
    apps.public_core.views.document_upload.validate_uploaded_file
    apps.public_core.views.document_upload._extract_pdf_text
        (not imported there today — patching this attribute raises
        AttributeError until BE adds a top-level import, matching the
        existing style used for extract_json_from_pdf/vectorize_extracted_document.
        That AttributeError IS the expected red-phase failure for test 5.)
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient


# ---------------------------------------------------------------------------
# Helpers — copied from test_download_auth.py's tenant/user/auth setup.
# ---------------------------------------------------------------------------


def _make_public_tenant():
    from apps.tenants.models import Tenant, Domain
    from django.contrib.auth import get_user_model
    from django.db import connection as db_conn, transaction

    User = get_user_model()
    owner, _ = User.objects.get_or_create(
        email="doc_upload_optional_public_owner@test.internal",
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
TEST_API = "42461397510000"
MISMATCH_API = "42461999990000"


@pytest.fixture
def public_tenant(db):
    return _make_public_tenant()


@pytest.fixture
def test_user(db, public_tenant):
    return _make_user("doc_upload_optional_user@test.internal", tenant=public_tenant)


@pytest.fixture
def auth_client(test_user, public_tenant):
    client = APIClient()
    client.force_authenticate(user=test_user)
    return client


def _pdf_upload(name="doc.pdf"):
    return SimpleUploadedFile(
        name,
        b"%PDF-1.4\n%mock pdf content for document_upload_optional tests\n",
        content_type="application/pdf",
    )


UPLOAD_URL = "/api/documents/upload/"


def _valid_security_result():
    from apps.public_core.services.file_validation import ValidationResult
    return ValidationResult(is_valid=True, errors=[], warnings=[])


# ---------------------------------------------------------------------------
# TEST 1 — api_not_found becomes a soft warning, doc still gets stored +
# indexed when the caller doesn't confirm (spec: 201, flagged is_validated=False).
#
# NOTE: per the view's existing branch (document_upload.py:207-225), a warning
# with warning_code set and confirmed=False returns 200 "warning" WITHOUT
# creating a document. This test targets a caller who does NOT pass
# `confirmed`, so it exercises the "not found + not confirmed" path directly
# against the intended contract described by the lead: 201 stored+flagged.
# If the shipped contract instead returns the 200 warning envelope for
# unconfirmed api_not_found (matching api_mismatch's behavior), see test 2/3
# below which pin that path explicitly — this test intentionally checks the
# `api_not_found` case ends in storage.
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_api_not_found_stores_flagged(auth_client):
    from apps.public_core.services.openai_extraction import ExtractionResult

    extraction = ExtractionResult(
        document_type="w2",
        json_data={"well_info": {}},  # no api field anywhere
        model_tag="gpt-4o",
        errors=[],
    )

    with patch(
        "apps.public_core.views.document_upload.extract_json_from_pdf",
        return_value=extraction,
    ), patch(
        "apps.public_core.views.document_upload.vectorize_extracted_document",
        return_value=3,
    ) as mock_vectorize:
        resp = auth_client.post(
            UPLOAD_URL,
            {
                "file": _pdf_upload(),
                "document_type": "w2",
                "api_number": TEST_API,
                "confirmed": "true",
                "skip_security_scan": "true",
            },
            format="multipart",
        )

    assert resp.status_code == 201, (
        f"Expected 201 (api_not_found is a soft warning, not a hard 400), "
        f"got {resp.status_code}: {resp.data}"
    )

    from apps.public_core.models import ExtractedDocument
    doc = ExtractedDocument.objects.filter(api_number=TEST_API, document_type="w2").first()
    assert doc is not None, "Expected an ExtractedDocument to be created"
    assert doc.is_validated is False, (
        f"Expected is_validated=False when API could not be confirmed from the "
        f"document, got {doc.is_validated}"
    )
    assert doc.validation_errors, "Expected non-empty validation_errors"
    assert doc.source_type == ExtractedDocument.SOURCE_TENANT_UPLOAD

    mock_vectorize.assert_called_once(), (
        "Document must still be indexed (vectorize_extracted_document called) "
        "even when flagged unvalidated"
    )


# ---------------------------------------------------------------------------
# TEST 2 — api_mismatch without `confirmed` returns a 200 warning envelope,
# no document created.
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_api_mismatch_without_confirmed_warns(auth_client):
    from apps.public_core.services.openai_extraction import ExtractionResult
    from apps.public_core.models import ExtractedDocument

    extraction = ExtractionResult(
        document_type="w2",
        json_data={"well_info": {"api": MISMATCH_API}},
        model_tag="gpt-4o",
        errors=[],
    )

    with patch(
        "apps.public_core.views.document_upload.extract_json_from_pdf",
        return_value=extraction,
    ):
        resp = auth_client.post(
            UPLOAD_URL,
            {
                "file": _pdf_upload(),
                "document_type": "w2",
                "api_number": TEST_API,
                "skip_security_scan": "true",
            },
            format="multipart",
        )

    assert resp.status_code == 200, (
        f"Expected 200 warning envelope for an unconfirmed API mismatch, "
        f"got {resp.status_code}: {resp.data}"
    )
    assert resp.data.get("status") == "warning", resp.data
    assert resp.data.get("warning_code") == "api_mismatch", (
        f"Expected warning_code='api_mismatch', got {resp.data.get('warning_code')!r}. "
        "This is the field dropped by the file_validation.py:444-450 bug."
    )
    assert resp.data.get("extracted_api") == MISMATCH_API, resp.data

    assert not ExtractedDocument.objects.filter(api_number=TEST_API, document_type="w2").exists(), (
        "No ExtractedDocument should be created for an unconfirmed mismatch"
    )


# ---------------------------------------------------------------------------
# TEST 3 — api_mismatch WITH confirmed=true stores the document, unvalidated.
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_api_mismatch_with_confirmed_stores(auth_client):
    from apps.public_core.services.openai_extraction import ExtractionResult
    from apps.public_core.models import ExtractedDocument

    extraction = ExtractionResult(
        document_type="w2",
        json_data={"well_info": {"api": MISMATCH_API}},
        model_tag="gpt-4o",
        errors=[],
    )

    with patch(
        "apps.public_core.views.document_upload.extract_json_from_pdf",
        return_value=extraction,
    ), patch(
        "apps.public_core.views.document_upload.vectorize_extracted_document",
        return_value=1,
    ):
        resp = auth_client.post(
            UPLOAD_URL,
            {
                "file": _pdf_upload(),
                "document_type": "w2",
                "api_number": TEST_API,
                "confirmed": "true",
                "skip_security_scan": "true",
            },
            format="multipart",
        )

    assert resp.status_code == 201, (
        f"Expected 201 when the user confirms upload despite the mismatch, "
        f"got {resp.status_code}: {resp.data}"
    )
    doc = ExtractedDocument.objects.filter(api_number=TEST_API, document_type="w2").first()
    assert doc is not None, "Expected an ExtractedDocument to be created"
    assert doc.is_validated is False, (
        f"Expected is_validated=False for a confirmed-despite-mismatch upload, "
        f"got {doc.is_validated}"
    )


# ---------------------------------------------------------------------------
# TEST 4 — matching API stores a fully validated document.
# Regression guard: may already pass today.
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_valid_api_stores_validated(auth_client):
    from apps.public_core.services.openai_extraction import ExtractionResult
    from apps.public_core.models import ExtractedDocument

    extraction = ExtractionResult(
        document_type="w2",
        json_data={"well_info": {"api": TEST_API}},
        model_tag="gpt-4o",
        errors=[],
    )

    with patch(
        "apps.public_core.views.document_upload.extract_json_from_pdf",
        return_value=extraction,
    ), patch(
        "apps.public_core.views.document_upload.vectorize_extracted_document",
        return_value=2,
    ):
        resp = auth_client.post(
            UPLOAD_URL,
            {
                "file": _pdf_upload(),
                "document_type": "w2",
                "api_number": TEST_API,
                "skip_security_scan": "true",
            },
            format="multipart",
        )

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.data}"
    doc = ExtractedDocument.objects.filter(api_number=TEST_API, document_type="w2").first()
    assert doc is not None
    assert doc.is_validated is True, f"Expected is_validated=True, got {doc.is_validated}"
    assert doc.validation_errors == [], f"Expected no validation_errors, got {doc.validation_errors}"


# ---------------------------------------------------------------------------
# TEST 5 — document_type='other' skips LLM extraction, embeds raw PDF text,
# stays private, still gets vectorized.
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_other_type_skips_llm_extraction_and_embeds_raw_text(auth_client):
    from apps.public_core.models import ExtractedDocument

    with patch(
        "apps.public_core.views.document_upload.extract_json_from_pdf",
    ) as mock_extract, patch(
        "apps.public_core.views.document_upload.validate_uploaded_file",
        return_value=_valid_security_result(),
    ), patch(
        "apps.public_core.views.document_upload._extract_pdf_text",
        return_value="Some raw extracted PDF text for an uncategorized document.",
    ), patch(
        "apps.public_core.views.document_upload.vectorize_extracted_document",
        return_value=1,
    ) as mock_vectorize:
        resp = auth_client.post(
            UPLOAD_URL,
            {
                "file": _pdf_upload(),
                "document_type": "other",
                "api_number": TEST_API,
            },
            format="multipart",
        )

    assert resp.status_code == 201, (
        f"Expected 201 for document_type='other', got {resp.status_code}: {resp.data}"
    )
    mock_extract.assert_not_called()

    doc = ExtractedDocument.objects.filter(api_number=TEST_API, document_type="other").first()
    assert doc is not None, "Expected an ExtractedDocument to be created for document_type='other'"
    assert doc.json_data == {"_raw_text": "Some raw extracted PDF text for an uncategorized document."}, (
        f"Expected json_data to embed the raw PDF text, got {doc.json_data}"
    )
    assert doc.is_public() is False, (
        "'other' is not in PUBLIC_DOC_TYPES; the document must stay tenant-private "
        "regardless of validation state"
    )
    mock_vectorize.assert_called_once()


# ---------------------------------------------------------------------------
# TEST 6 — unit test: validate_uploaded_file forwards warning_code/extracted_api
# from verify_api_number on an api_not_found failure.
# ---------------------------------------------------------------------------

def test_validate_uploaded_file_forwards_warning_code(tmp_path):
    from apps.public_core.services.file_validation import validate_uploaded_file

    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%mock\n")

    result = validate_uploaded_file(
        file_path=pdf_path,
        document_type="w2",
        expected_api=TEST_API,
        skip_security_scan=True,
        json_data={"well_info": {}},  # no api field -> api_not_found
    )

    assert result.is_valid is False
    assert result.warning_code == "api_not_found", (
        f"Expected warning_code='api_not_found' to be forwarded from "
        f"verify_api_number, got {result.warning_code!r}"
    )
    assert result.extracted_api is None, (
        f"Expected extracted_api=None to be forwarded, got {result.extracted_api!r}"
    )
