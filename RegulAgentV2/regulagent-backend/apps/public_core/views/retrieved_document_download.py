"""
Serve downloaded source files for RetrievedDocuments.

GET /api/retrieved-documents/<id>/download/

Auth-protected: either an authenticated Bearer request (IsAuthenticated
today), or a signed ``?token=`` query param minted by make_download_token()
for that specific pk. The token path lets the frontend render a plain
``<a href>`` that works without an Authorization header. Serves the file
at local_path as application/pdf (or octet-stream for non-PDF files).  If
local_path does not exist the view returns 404.
"""
import logging
import mimetypes
from pathlib import Path

from django.conf import settings
from django.core import signing
from django.http import FileResponse
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.public_core.models import RetrievedDocument

logger = logging.getLogger(__name__)

_DOWNLOAD_SALT = "retrieved-document-download"
# TTL for signed download links. A research page can stay open a while; keep it
# bounded. Read from settings with a safe default.
DOWNLOAD_TOKEN_MAX_AGE = int(getattr(settings, "DOWNLOAD_TOKEN_MAX_AGE", 86400))  # 24h


def make_download_token(rd_id) -> str:
    return signing.TimestampSigner(salt=_DOWNLOAD_SALT).sign(str(rd_id))


def verify_download_token(token, pk) -> bool:
    try:
        original = signing.TimestampSigner(salt=_DOWNLOAD_SALT).unsign(
            token, max_age=DOWNLOAD_TOKEN_MAX_AGE
        )
    except (signing.BadSignature, signing.SignatureExpired):
        return False
    except Exception:
        return False
    return original == str(pk)


class RetrievedDocumentDownloadView(APIView):
    """
    GET /api/retrieved-documents/<pk>/download/

    Serve the locally-cached file for a RetrievedDocument.
    """
    # AllowAny: DRF still runs authentication and populates request.user for a
    # valid Bearer, so we can check that first. This just stops the permission
    # layer from rejecting a tokenless browser navigation before get() gets a
    # chance to check the signed token.
    permission_classes = [AllowAny]

    def get(self, request, pk: int):
        authorized = bool(request.user and request.user.is_authenticated)
        if not authorized:
            token = request.query_params.get("token")
            authorized = bool(token) and verify_download_token(token, pk)
        if not authorized:
            return Response(
                {"detail": "Authentication credentials were not provided."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

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
