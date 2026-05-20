"""
Failing tests for DELETE /api/documents/<id>/ endpoint.

Spec:
  - Authentication: JWT required (401 without auth)
  - Tenant isolation: only delete docs where uploaded_by_tenant == requesting user's tenant
  - 403 if doc belongs to a different tenant
  - 404 if doc does not exist
  - 204 on success: deletes ExtractedDocument AND associated DocumentVector rows
    (vectors are linked via metadata['extracted_document_id'] == str(doc.id))
  - 403 if source_type is NOT in ['tenant_upload', 'operator_packet'] (i.e. neubus/rrc docs
    may not be deleted even if the tenant somehow matches)

These tests are written BEFORE implementation and MUST FAIL against the current
codebase.  Neither the URL /api/documents/<id>/ nor DocumentDeleteView exist yet.
"""
from __future__ import annotations

import uuid
import pytest
from rest_framework.test import APIClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DUMMY_EMBEDDING = [0.1] * 3072  # matches DocumentVector.embedding dimensions
API_NUMBER = "42383314310000"

DELETABLE_SOURCE_TYPES = ["tenant_upload", "operator_packet"]
RESEARCH_SOURCE_TYPES = ["neubus", "rrc"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _endpoint(doc_id: int) -> str:
    return f"/api/documents/{doc_id}/"


def _make_tenant(suffix=None):
    """Create a real Tenant row (no schema spin-up needed for public schema tests)."""
    from apps.tenants.models import Tenant, Domain

    uid = suffix or str(uuid.uuid4())[:8]
    tenant = Tenant.objects.create(
        name=f"DeleteTest {uid}",
        slug=f"deltest-{uid}",
        schema_name=f"deltest_{uid}",
    )
    Domain.objects.create(
        domain=f"deltest-{uid}.localhost",
        tenant=tenant,
        is_primary=True,
    )
    return tenant


def _make_user(tenant, suffix=None):
    """Create a User and attach them to the given tenant."""
    from apps.tenants.models import User

    uid = suffix or str(uuid.uuid4())[:8]
    user = User.objects.create_user(
        email=f"deltest-{uid}@example.com",
        password="testpass123",
        is_active=True,
    )
    tenant.add_user(user, is_superuser=False, is_staff=False)
    return user


def _make_doc(
    source_type="tenant_upload",
    uploaded_by_tenant=None,
    api_number=API_NUMBER,
    document_type="w2",
    status="success",
    source_path="",
):
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


def _make_vector(doc, section_name="header", section_text="Some section"):
    """
    Create a DocumentVector linked to the given ExtractedDocument via
    metadata['extracted_document_id'].  This mirrors the production
    vectorisation pipeline in neubus_semantic.py.
    """
    from apps.public_core.models import DocumentVector, WellRegistry

    # Reuse or create a minimal WellRegistry row for the FK
    well, _ = WellRegistry.objects.get_or_create(
        api14=doc.api_number,
        defaults={
            "state": "TX",
            "county": "Andrews",
            "operator_name": "Delete Test Op",
        },
    )

    return DocumentVector.objects.create(
        well=well,
        file_name=f"test_{doc.id}.pdf",
        document_type=doc.document_type,
        section_name=section_name,
        section_text=section_text,
        embedding=DUMMY_EMBEDDING,
        metadata={
            "api_number": doc.api_number,
            "extracted_document_id": str(doc.id),
        },
    )


def _tenant_id_for_user(user):
    """Return the integer PK of the user's first tenant (mirrors view logic)."""
    tenant = user.tenants.first()
    return tenant.id if tenant else None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """Bare (unauthenticated) DRF API client."""
    return APIClient()


@pytest.fixture
def tenant_a(db, public_tenant):
    """A real Tenant row for tenant A."""
    return _make_tenant("ta")


@pytest.fixture
def tenant_b(db, public_tenant):
    """A real Tenant row for tenant B (different from tenant A)."""
    return _make_tenant("tb")


@pytest.fixture
def user_a(db, tenant_a):
    """A user belonging to tenant A."""
    return _make_user(tenant_a, suffix="ua")


@pytest.fixture
def user_b(db, tenant_b):
    """A user belonging to tenant B."""
    return _make_user(tenant_b, suffix="ub")


@pytest.fixture
def auth_client_a(client, user_a):
    """API client authenticated as user_a (tenant A)."""
    client.force_authenticate(user=user_a)
    return client


@pytest.fixture
def auth_client_b(client, user_b):
    """API client authenticated as user_b (tenant B)."""
    client.force_authenticate(user=user_b)
    return client


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestDocumentDeleteAPI:
    """Integration tests for DELETE /api/documents/<id>/."""

    # ------------------------------------------------------------------
    # 1. Authentication required
    # ------------------------------------------------------------------

    def test_delete_requires_auth(self, client, db, public_tenant):
        """
        Unauthenticated DELETE must return 401.

        FAILS now: URL doesn't exist → 404, but spec requires 401 for
        unauthenticated requests regardless of whether the resource exists.
        """
        response = client.delete(_endpoint(99999))
        assert response.status_code == 401, (
            f"Expected 401 for unauthenticated request, got {response.status_code}"
        )

    # ------------------------------------------------------------------
    # 2. Successful deletion (own tenant doc)
    # ------------------------------------------------------------------

    def test_delete_own_tenant_doc_success(self, auth_client_a, user_a, db):
        """
        Authenticated tenant A deletes their own doc → 204.
        The ExtractedDocument row must be gone afterwards.

        FAILS now: URL /api/documents/<id>/ is not registered.
        """
        from apps.public_core.models import ExtractedDocument

        tenant_id = _tenant_id_for_user(user_a)
        doc = _make_doc(source_type="tenant_upload", uploaded_by_tenant=tenant_id)

        response = auth_client_a.delete(_endpoint(doc.id))

        assert response.status_code == 204, (
            f"Expected 204 on successful delete, got {response.status_code}. "
            f"Body: {getattr(response, 'data', response.content)}"
        )
        assert not ExtractedDocument.objects.filter(pk=doc.id).exists(), (
            "ExtractedDocument must be deleted from the DB after a successful DELETE"
        )

    # ------------------------------------------------------------------
    # 3. Cross-tenant deletion forbidden
    # ------------------------------------------------------------------

    def test_delete_other_tenant_doc_forbidden(
        self, auth_client_a, user_a, user_b, db
    ):
        """
        Tenant A tries to delete tenant B's doc → 403.
        The document must still exist in the DB.

        FAILS now: URL doesn't exist.
        """
        from apps.public_core.models import ExtractedDocument

        tenant_b_id = _tenant_id_for_user(user_b)
        doc = _make_doc(source_type="tenant_upload", uploaded_by_tenant=tenant_b_id)

        response = auth_client_a.delete(_endpoint(doc.id))

        assert response.status_code == 403, (
            f"Expected 403 when deleting another tenant's doc, got {response.status_code}"
        )
        assert ExtractedDocument.objects.filter(pk=doc.id).exists(), (
            "Document must NOT be deleted when a different tenant attempts deletion"
        )

    # ------------------------------------------------------------------
    # 4. Non-existent document → 404
    # ------------------------------------------------------------------

    def test_delete_nonexistent_doc(self, auth_client_a, db):
        """
        DELETE /api/documents/99999/ when no such doc exists → 404.

        FAILS now: URL doesn't exist → likely also 404, but via URL routing
        not view logic.  Once the URL is registered the view must return 404
        from its own lookup, not from Django's URL resolver.
        """
        response = auth_client_a.delete(_endpoint(99999))
        assert response.status_code == 404, (
            f"Expected 404 for non-existent document, got {response.status_code}"
        )

    # ------------------------------------------------------------------
    # 5. Research docs cannot be deleted (403)
    # ------------------------------------------------------------------

    def test_cannot_delete_neubus_doc(self, auth_client_a, user_a, db):
        """
        Authenticated user cannot delete a neubus doc (research source) → 403,
        even if uploaded_by_tenant matches (which it normally won't, but the
        source_type check is authoritative).

        FAILS now: URL doesn't exist.
        """
        from apps.public_core.models import ExtractedDocument

        tenant_id = _tenant_id_for_user(user_a)
        doc = _make_doc(source_type="neubus", uploaded_by_tenant=tenant_id)

        response = auth_client_a.delete(_endpoint(doc.id))

        assert response.status_code == 403, (
            f"Expected 403 when attempting to delete a neubus (research) doc, "
            f"got {response.status_code}"
        )
        assert ExtractedDocument.objects.filter(pk=doc.id).exists(), (
            "Neubus doc must NOT be deleted"
        )

    def test_cannot_delete_rrc_doc(self, auth_client_a, user_a, db):
        """
        Authenticated user cannot delete an RRC (public regulatory) doc → 403.

        FAILS now: URL doesn't exist.
        """
        from apps.public_core.models import ExtractedDocument

        # RRC docs have no uploaded_by_tenant
        doc = _make_doc(source_type="rrc", uploaded_by_tenant=None)

        response = auth_client_a.delete(_endpoint(doc.id))

        assert response.status_code == 403, (
            f"Expected 403 when attempting to delete an rrc doc, got {response.status_code}"
        )
        assert ExtractedDocument.objects.filter(pk=doc.id).exists(), (
            "RRC doc must NOT be deleted"
        )

    # ------------------------------------------------------------------
    # 6. Delete also removes associated DocumentVector rows
    # ------------------------------------------------------------------

    def test_delete_also_removes_document_vectors(self, auth_client_a, user_a, db):
        """
        On successful delete, ALL DocumentVector rows whose
        metadata['extracted_document_id'] == str(doc.id) must also be deleted.

        DocumentVector has no direct FK to ExtractedDocument — the link is
        stored in metadata['extracted_document_id'] (set by the vectorisation
        pipeline in neubus_semantic.py).

        FAILS now: URL doesn't exist.
        """
        from apps.public_core.models import ExtractedDocument, DocumentVector

        tenant_id = _tenant_id_for_user(user_a)
        doc = _make_doc(source_type="tenant_upload", uploaded_by_tenant=tenant_id)

        # Create multiple vectors linked to this document
        vec1 = _make_vector(doc, section_name="header", section_text="Header content")
        vec2 = _make_vector(doc, section_name="body", section_text="Body content")

        # Sanity check: vectors exist before delete
        assert DocumentVector.objects.filter(
            metadata__extracted_document_id=str(doc.id)
        ).count() == 2, "Expected 2 vectors to exist before delete"

        response = auth_client_a.delete(_endpoint(doc.id))

        assert response.status_code == 204, (
            f"Expected 204, got {response.status_code}"
        )

        # Document must be gone
        assert not ExtractedDocument.objects.filter(pk=doc.id).exists(), (
            "ExtractedDocument must be deleted"
        )

        # ALL associated vectors must be gone
        remaining_vectors = DocumentVector.objects.filter(
            metadata__extracted_document_id=str(doc.id)
        ).count()
        assert remaining_vectors == 0, (
            f"Expected 0 DocumentVector rows after delete, found {remaining_vectors}. "
            f"View must delete vectors via metadata['extracted_document_id'] lookup."
        )

    # ------------------------------------------------------------------
    # 7. operator_packet docs can be deleted by owning tenant
    # ------------------------------------------------------------------

    def test_delete_operator_packet_doc_success(self, auth_client_a, user_a, db):
        """
        source_type='operator_packet' is a deletable type — owning tenant can
        delete it, same as tenant_upload.

        FAILS now: URL doesn't exist.
        """
        from apps.public_core.models import ExtractedDocument

        tenant_id = _tenant_id_for_user(user_a)
        doc = _make_doc(source_type="operator_packet", uploaded_by_tenant=tenant_id)

        response = auth_client_a.delete(_endpoint(doc.id))

        assert response.status_code == 204, (
            f"Expected 204 for operator_packet delete, got {response.status_code}"
        )
        assert not ExtractedDocument.objects.filter(pk=doc.id).exists(), (
            "operator_packet doc must be deleted on 204"
        )

    # ------------------------------------------------------------------
    # 8. Vectors for OTHER docs are not touched
    # ------------------------------------------------------------------

    def test_delete_does_not_remove_unrelated_vectors(self, auth_client_a, user_a, db):
        """
        Deleting doc A must NOT delete vectors that belong to doc B
        (even if doc B is on the same well/api_number).

        FAILS now: URL doesn't exist.
        """
        from apps.public_core.models import ExtractedDocument, DocumentVector

        tenant_id = _tenant_id_for_user(user_a)

        doc_a = _make_doc(
            source_type="tenant_upload",
            uploaded_by_tenant=tenant_id,
            document_type="w2",
        )
        doc_b = _make_doc(
            source_type="tenant_upload",
            uploaded_by_tenant=tenant_id,
            document_type="gau",
        )

        vec_a = _make_vector(doc_a, section_name="header", section_text="Doc A section")
        vec_b = _make_vector(doc_b, section_name="header", section_text="Doc B section")

        response = auth_client_a.delete(_endpoint(doc_a.id))
        assert response.status_code == 204

        # Doc B's vector must survive
        assert DocumentVector.objects.filter(pk=vec_b.pk).exists(), (
            "DocumentVector for an unrelated document must NOT be deleted"
        )
        # Doc A's vector must be gone
        assert not DocumentVector.objects.filter(pk=vec_a.pk).exists(), (
            "DocumentVector for the deleted document must be removed"
        )
