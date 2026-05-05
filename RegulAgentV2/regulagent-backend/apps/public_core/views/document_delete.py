"""
DELETE /api/documents/<doc_id>/

Deletes a tenant-uploaded ExtractedDocument and all associated DocumentVector rows.
Only documents with source_type in ['tenant_upload', 'operator_packet'] can be deleted.
Only the owning tenant may delete the document.
"""
import logging

from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.public_core.models import DocumentVector, ExtractedDocument

logger = logging.getLogger(__name__)

DELETABLE_SOURCE_TYPES = {"tenant_upload", "operator_packet"}


class DocumentDeleteView(APIView):
    """
    DELETE /api/documents/<doc_id>/

    - Authentication required (401 if missing)
    - 404 if the ExtractedDocument does not exist
    - 403 if source_type is not in DELETABLE_SOURCE_TYPES
    - 403 if the document belongs to a different tenant
    - 204 on success (document + associated vectors removed)
    """

    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def delete(self, request, doc_id):
        # --- 1. Fetch document (404 if missing) ---
        try:
            doc = ExtractedDocument.objects.get(id=doc_id)
        except ExtractedDocument.DoesNotExist:
            return Response(
                {"detail": "Document not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # --- 2. Check source_type is deletable ---
        if doc.source_type not in DELETABLE_SOURCE_TYPES:
            return Response(
                {"detail": "Only tenant-uploaded documents can be deleted."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # --- 3. Tenant ownership check ---
        # Use .first() to mirror the tenant lookup used when storing uploaded_by_tenant.
        # Django ORM handles the coercion between the integer tenant PK and the
        # uploaded_by_tenant field in the filter() call below.
        tenant = request.user.tenants.first()
        tenant_id = tenant.id if tenant else None

        owns_doc = (
            tenant_id is not None
            and ExtractedDocument.objects.filter(
                id=doc_id, uploaded_by_tenant=tenant_id
            ).exists()
        )
        if not owns_doc:
            return Response(
                {"detail": "You do not have permission to delete this document."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # --- 4. Delete associated DocumentVector rows ---
        deleted_vectors, _ = DocumentVector.objects.filter(
            metadata__extracted_document_id=str(doc_id)
        ).delete()
        logger.info(f"[DocumentDelete] Deleted {deleted_vectors} vector(s) for document {doc_id}")

        # --- 5. Delete the ExtractedDocument ---
        doc.delete()
        logger.info(f"[DocumentDelete] Deleted ExtractedDocument {doc_id}")

        return Response(status=status.HTTP_204_NO_CONTENT)
