"""
GET /api/documents/
List ExtractedDocuments for a well, filtered by source type.
"""
import os
import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.authentication import JWTAuthentication
from django.db.models import Q

from apps.public_core.models import ExtractedDocument

logger = logging.getLogger(__name__)

RESEARCH_SOURCES = ('neubus', 'rrc')
TENANT_SOURCES = ('tenant_upload', 'operator_packet')


class DocumentListView(APIView):
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        api_number = request.query_params.get('api_number', '').strip()
        if not api_number:
            return Response(
                {"error": "api_number query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        source = request.query_params.get('source', '')  # 'research' | 'tenant' | ''

        # Get requesting tenant
        user_tenant = request.user.tenants.first()
        tenant_id = user_tenant.id if user_tenant else None

        # Base queryset
        qs = ExtractedDocument.objects.filter(api_number=api_number)

        if source == 'research':
            qs = qs.filter(source_type__in=RESEARCH_SOURCES)
        elif source == 'tenant':
            if tenant_id:
                qs = qs.filter(
                    source_type__in=TENANT_SOURCES,
                    uploaded_by_tenant=tenant_id,
                )
            else:
                qs = qs.none()
        else:
            # No source param: public research docs + this tenant's own uploads
            if tenant_id:
                qs = qs.filter(
                    Q(source_type__in=RESEARCH_SOURCES) |
                    Q(source_type__in=TENANT_SOURCES, uploaded_by_tenant=tenant_id)
                )
            else:
                qs = qs.filter(source_type__in=RESEARCH_SOURCES)

        qs = qs.order_by('-created_at')

        documents = []
        for doc in qs:
            source_path = doc.source_path or ''
            file_name = os.path.basename(source_path) if source_path else None
            documents.append({
                'id': doc.id,
                'document_type': doc.document_type,
                'source_type': doc.source_type,
                'status': doc.status,
                'api_number': doc.api_number,
                'created_at': doc.created_at.isoformat(),
                'is_public': doc.is_public(),
                'file_name': file_name,
            })

        return Response({'documents': documents, 'total': len(documents)})
