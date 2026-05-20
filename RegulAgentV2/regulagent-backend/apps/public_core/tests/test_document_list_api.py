"""
Failing tests for GET /api/documents/ endpoint.

Spec:
  - Query params: api_number (required), source (optional: 'research' | 'tenant')
  - Authentication: JWT required (401 without auth)
  - Tenant isolation:
      source=research -> returns docs with source_type in ['neubus', 'rrc'] for the api_number
      source=tenant   -> returns ONLY docs where uploaded_by_tenant == requesting user's tenant
      (no source)     -> research docs + requesting tenant's own upload docs
  - Response shape: {"documents": [...], "total": <int>}
  - file_name: basename of source_path, or None if source_path is empty

These tests are written BEFORE implementation and MUST FAIL against the current
codebase.  The endpoint /api/documents/ is not yet registered in ra_config/urls.py.
"""
from __future__ import annotations

import uuid
import pytest
from rest_framework.test import APIClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_NUMBER = "42383314310000"
ENDPOINT = "/api/documents/"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """Bare (unauthenticated) DRF API client."""
    return APIClient()


@pytest.fixture
def own_tenant_uuid(db, public_tenant):
    """
    Create a real Tenant and return its id (used as uploaded_by_tenant UUID).

    The Tenant.id is an integer PK but ExtractedDocument.uploaded_by_tenant
    is a UUIDField.  In production, the view casts tenant.id to UUID via
    UUID(int=tenant.id) or stores it directly.  For these tests we generate a
    plain UUID and use it directly — the view will need to resolve the same
    value from the authenticated user's tenant.
    """
    return uuid.uuid4()


@pytest.fixture
def other_tenant_uuid():
    """A different tenant's UUID (no DB row required)."""
    return uuid.uuid4()


@pytest.fixture
def test_user(db, public_tenant):
    """Authenticated user whose tenant UUID is own_tenant_uuid."""
    from apps.tenants.models import User, Tenant, Domain
    import uuid as _uuid

    unique = str(_uuid.uuid4())[:8]
    tenant = Tenant.objects.create(
        name=f"DocListTest {unique}",
        slug=f"doclist-{unique}",
        schema_name=f"doclist_{unique}",
    )
    Domain.objects.create(
        domain=f"doclist-{unique}.localhost",
        tenant=tenant,
        is_primary=True,
    )

    user = User.objects.create_user(
        email=f"doclist-{unique}@example.com",
        password="testpass123",
        is_active=True,
    )
    tenant.add_user(user, is_superuser=False, is_staff=False)
    return user


@pytest.fixture
def auth_client(client, test_user):
    """Authenticated API client (force_authenticate — no token plumbing)."""
    client.force_authenticate(user=test_user)
    return client


def _make_doc(api_number=API_NUMBER, source_type="neubus", uploaded_by_tenant=None,
              source_path="", document_type="w2", status="success"):
    """Create and return an ExtractedDocument directly via ORM."""
    from apps.public_core.models import ExtractedDocument

    return ExtractedDocument.objects.create(
        api_number=api_number,
        document_type=document_type,
        source_type=source_type,
        uploaded_by_tenant=uploaded_by_tenant,
        status=status,
        source_path=source_path,
        json_data={},
    )


# ---------------------------------------------------------------------------
# Helper: extract tenant UUID from user (mirrors view logic)
# ---------------------------------------------------------------------------

def _tenant_uuid_for_user(user):
    """Return the id of the user's first tenant (mirrors document_upload view logic)."""
    tenant = user.tenants.first()
    return tenant.id if tenant else None


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestDocumentListAPI:
    """Integration tests for GET /api/documents/."""

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def test_requires_authentication(self, client, db, public_tenant):
        """Unauthenticated request must return 401."""
        response = client.get(ENDPOINT, {"api_number": API_NUMBER})
        assert response.status_code == 401, (
            f"Expected 401 for unauthenticated request, got {response.status_code}"
        )

    # ------------------------------------------------------------------
    # source=research
    # ------------------------------------------------------------------

    def test_returns_research_docs_for_source_research(self, auth_client, test_user, db):
        """
        source=research returns only docs with source_type in ['neubus', 'rrc'].
        Documents with source_type='tenant_upload' must be excluded.
        """
        tenant_uuid = _tenant_uuid_for_user(test_user)

        neubus_doc = _make_doc(source_type="neubus", uploaded_by_tenant=None)
        tenant_doc = _make_doc(
            source_type="tenant_upload",
            uploaded_by_tenant=uuid.uuid4(),  # a different tenant
        )

        response = auth_client.get(ENDPOINT, {"api_number": API_NUMBER, "source": "research"})

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.data}"
        )

        data = response.json()
        assert "documents" in data, "Response must have 'documents' key"
        assert "total" in data, "Response must have 'total' key"

        doc_ids = [d["id"] for d in data["documents"]]
        assert neubus_doc.id in doc_ids, "neubus doc should appear in source=research results"
        assert tenant_doc.id not in doc_ids, (
            "tenant_upload doc must NOT appear in source=research results"
        )
        assert data["total"] == len(data["documents"])

    # ------------------------------------------------------------------
    # source=tenant
    # ------------------------------------------------------------------

    def test_returns_only_own_tenant_uploads_for_source_tenant(self, auth_client, test_user, db):
        """
        source=tenant returns ONLY documents where uploaded_by_tenant equals
        the requesting user's tenant.  Another tenant's upload must be excluded.
        """
        own_uuid = _tenant_uuid_for_user(test_user)
        other_uuid = uuid.uuid4()

        own_doc = _make_doc(source_type="tenant_upload", uploaded_by_tenant=own_uuid)
        other_doc = _make_doc(source_type="tenant_upload", uploaded_by_tenant=other_uuid)

        response = auth_client.get(ENDPOINT, {"api_number": API_NUMBER, "source": "tenant"})

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.data}"
        )

        data = response.json()
        doc_ids = [d["id"] for d in data["documents"]]

        assert own_doc.id in doc_ids, (
            "Own tenant upload must appear in source=tenant results"
        )
        assert other_doc.id not in doc_ids, (
            "Another tenant's upload must NOT appear in source=tenant results"
        )

    # ------------------------------------------------------------------
    # No source param
    # ------------------------------------------------------------------

    def test_no_source_param_returns_research_and_own_tenant_docs(
        self, auth_client, test_user, db
    ):
        """
        Without a source param, the endpoint returns:
          - research docs (neubus/rrc) for the api_number
          - the requesting tenant's own uploads for the api_number
          - NOT another tenant's uploads
        """
        own_uuid = _tenant_uuid_for_user(test_user)
        other_uuid = uuid.uuid4()

        neubus_doc = _make_doc(source_type="neubus", uploaded_by_tenant=None)
        own_upload = _make_doc(source_type="tenant_upload", uploaded_by_tenant=own_uuid)
        other_upload = _make_doc(source_type="tenant_upload", uploaded_by_tenant=other_uuid)

        response = auth_client.get(ENDPOINT, {"api_number": API_NUMBER})

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.data}"
        )

        data = response.json()
        doc_ids = [d["id"] for d in data["documents"]]

        assert neubus_doc.id in doc_ids, "Research (neubus) doc must be included"
        assert own_upload.id in doc_ids, "Own tenant upload must be included"
        assert other_upload.id not in doc_ids, (
            "Another tenant's upload must NOT be included"
        )
        assert data["total"] == 2, (
            f"Expected total=2 (neubus + own upload), got {data['total']}"
        )

    # ------------------------------------------------------------------
    # api_number required
    # ------------------------------------------------------------------

    def test_api_number_required(self, auth_client, db):
        """GET /api/documents/ without api_number must return 400."""
        response = auth_client.get(ENDPOINT)
        assert response.status_code == 400, (
            f"Expected 400 when api_number is missing, got {response.status_code}"
        )

    # ------------------------------------------------------------------
    # file_name field
    # ------------------------------------------------------------------

    def test_file_name_is_basename_of_source_path(self, auth_client, test_user, db):
        """
        The response 'file_name' field must be the basename of source_path.
        If source_path is empty, file_name must be None.
        """
        source_path = "/mediafiles/uploads/tenant/w2/42383314310000_file.pdf"
        expected_basename = "42383314310000_file.pdf"

        doc_with_path = _make_doc(
            source_type="neubus",
            source_path=source_path,
        )
        doc_no_path = _make_doc(
            source_type="rrc",
            source_path="",
            document_type="gau",
        )

        response = auth_client.get(ENDPOINT, {"api_number": API_NUMBER, "source": "research"})

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.data}"
        )

        data = response.json()
        docs_by_id = {d["id"]: d for d in data["documents"]}

        # Doc with source_path
        assert doc_with_path.id in docs_by_id, (
            f"Expected doc {doc_with_path.id} in response"
        )
        assert docs_by_id[doc_with_path.id]["file_name"] == expected_basename, (
            f"Expected file_name='{expected_basename}', "
            f"got '{docs_by_id[doc_with_path.id]['file_name']}'"
        )

        # Doc without source_path
        assert doc_no_path.id in docs_by_id, (
            f"Expected doc {doc_no_path.id} in response"
        )
        assert docs_by_id[doc_no_path.id]["file_name"] is None, (
            f"Expected file_name=None for empty source_path, "
            f"got '{docs_by_id[doc_no_path.id]['file_name']}'"
        )

    # ------------------------------------------------------------------
    # Response shape
    # ------------------------------------------------------------------

    def test_response_shape(self, auth_client, test_user, db):
        """
        Each document in the response must include the required fields:
        id, document_type, source_type, status, api_number, created_at,
        is_public, file_name.
        """
        _make_doc(source_type="neubus", source_path="/mediafiles/test.pdf")

        response = auth_client.get(ENDPOINT, {"api_number": API_NUMBER, "source": "research"})

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.data}"
        )

        data = response.json()
        assert len(data["documents"]) >= 1, "Expected at least one document"

        required_fields = {
            "id", "document_type", "source_type", "status",
            "api_number", "created_at", "is_public", "file_name",
        }
        for doc in data["documents"]:
            missing = required_fields - set(doc.keys())
            assert not missing, (
                f"Document response missing fields: {missing}. Got keys: {list(doc.keys())}"
            )
