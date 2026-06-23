"""
Serve downloaded source files for RetrievedDocuments.

GET /api/retrieved-documents/<id>/download/

Auth-protected (IsAuthenticated).  Serves the file at local_path as
application/pdf (or octet-stream for non-PDF files).  If local_path does
not exist the view returns 404.
"""
import logging
import mimetypes
from pathlib import Path

from django.http import FileResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.public_core.models import RetrievedDocument

logger = logging.getLogger(__name__)


class RetrievedDocumentDownloadView(APIView):
    """
    GET /api/retrieved-documents/<pk>/download/

    Serve the locally-cached file for a RetrievedDocument.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk: int):
        try:
            rd = RetrievedDocument.objects.get(pk=pk)
        except RetrievedDocument.DoesNotExist:
            return Response(
                {"detail": "Document not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not rd.local_path:
            return Response(
                {"detail": "No local file path stored for this document."},
                status=status.HTTP_404_NOT_FOUND,
            )

        file_path = Path(rd.local_path)
        if not file_path.exists():
            logger.warning(
                "[RetrievedDocumentDownload] File not found on disk: "
                f"rd_id={rd.pk} path={rd.local_path}"
            )
            return Response(
                {"detail": "Source file not found on server."},
                status=status.HTTP_404_NOT_FOUND,
            )

        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        filename = file_path.name

        logger.info(
            f"[RetrievedDocumentDownload] Serving {file_path} for RD {rd.pk}"
        )
        response = FileResponse(
            open(file_path, "rb"),
            content_type=content_type,
            filename=filename,
        )
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response
