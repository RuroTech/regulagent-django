"""
Document upload API endpoint for tenant file uploads.

Handles PDF uploads with:
- Security scanning (OpenAI Moderation + prompt injection detection)
- API number verification
- Tenant-aware storage (S3 or local filesystem)
- Automatic extraction and vectorization

POST /api/documents/upload/
    - file: PDF file (required)
    - document_type: w2, w15, gau, schematic, formation_tops (required)
    - api_number: Expected API number for verification (required)
    - skip_security_scan: Skip security checks (optional, dev only)
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from django.core.files.storage import default_storage
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.public_core.models import ExtractedDocument, WellRegistry
from apps.public_core.services.file_validation import validate_uploaded_file
from apps.public_core.services.openai_extraction import (
    _extract_pdf_text,
    extract_json_from_pdf,
    vectorize_extracted_document,
)
from apps.tenant_overlay.models import WellEngagement
from apps.tenant_overlay.services.engagement_tracker import track_well_interaction

logger = logging.getLogger(__name__)


class DocumentUploadView(APIView):
    """
    Upload and validate tenant documents (W2, W15, GAU, schematic, etc.)
    
    Request:
        - file: PDF file
        - document_type: w2, w15, gau, schematic, formation_tops, w3, w3a
        - api_number: Expected API (for verification)
        - skip_security_scan: (optional, dev only) Skip security checks
    
    Response (success):
        {
            "success": true,
            "extracted_document_id": "uuid",
            "api_number": "42-123-45678",
            "document_type": "w2",
            "is_public": true,
            "vectors_created": 5,
            "storage_path": "tenant-uuid/w2/42-123-45678_file.pdf",
            "message": "Document uploaded, validated, and processed successfully."
        }
    
    Response (validation failure):
        {
            "error": "Validation failed",
            "reasons": ["Security scan failed: ...", "API mismatch: ..."],
            "warnings": []
        }
    """
    
    # Authentication enabled
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]
    
    parser_classes = [MultiPartParser, FormParser]
    
    def post(self, request):
        # ========== Extract Parameters ==========
        uploaded_file = request.FILES.get('file')
        document_type = request.data.get('document_type', '').lower().strip()
        api_number = request.data.get('api_number', '').strip()
        skip_security_scan = request.data.get('skip_security_scan', 'false').lower() == 'true'
        confirmed = request.data.get('confirmed', 'false').lower() == 'true'
        
        # Get tenant ID from authenticated user
        tenant_id = None
        if request.user.is_authenticated:
            # Get the first tenant the user belongs to
            user_tenant = request.user.tenants.first()
            tenant_id = user_tenant.id if user_tenant else None
            logger.info(f"document_upload: authenticated user {request.user.email}, tenant_id={tenant_id}")
        
        # ========== Validation: Input Parameters ==========
        if not uploaded_file:
            return Response(
                {"error": "No file provided", "detail": "Request must include a 'file' parameter"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate document type
        SUPPORTED_TYPES = ['w2', 'w15', 'gau', 'schematic', 'formation_tops', 'w3', 'w3a', 'other']
        if document_type not in SUPPORTED_TYPES:
            return Response({
                "error": "Invalid document_type",
                "detail": f"Must be one of: {', '.join(SUPPORTED_TYPES)}",
                "received": document_type
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if not api_number:
            return Response(
                {"error": "api_number is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate file extension
        if not uploaded_file.name.lower().endswith('.pdf'):
            return Response({
                "error": "Invalid file type",
                "detail": "Only PDF files are supported",
                "received": uploaded_file.name
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # ========== Step 1: Save to Temporary Location ==========
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp_file:
                for chunk in uploaded_file.chunks():
                    tmp_file.write(chunk)
                tmp_path = Path(tmp_file.name)
            
            logger.info(f"document_upload: saved temp file {tmp_path} for {document_type}")
        
        except Exception as e:
            logger.exception("document_upload: failed to save temp file")
            return Response({
                "error": "Failed to save uploaded file",
                "detail": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # ========== Step 2: Security Scan (lightweight, no full extraction) ==========
        try:
            logger.info(f"document_upload: running security scan for {document_type}")

            security_result = validate_uploaded_file(
                file_path=tmp_path,
                document_type=document_type,
                expected_api=api_number,
                skip_security_scan=skip_security_scan,
                # No json_data yet — only runs the security scan step
                json_data=None,
            )

            if not security_result.is_valid:
                logger.warning(f"document_upload: security scan FAILED - {security_result.errors}")
                return Response({
                    "error": "Validation failed",
                    "reasons": security_result.errors,
                    "warnings": security_result.warnings
                }, status=status.HTTP_400_BAD_REQUEST)

            logger.info(f"document_upload: security scan PASSED for {api_number}")

        except Exception as e:
            logger.exception("document_upload: security scan error")
            return Response({
                "error": "Validation system error",
                "detail": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # ========== Step 3: Extract Document (single OpenAI call) ==========
        # 'other' has no structured schema to extract against, so it skips
        # extraction entirely and falls back to raw PDF text (embedded as-is
        # in Step 7). It also has no API field to cross-check, so it never
        # runs Step 3.5 and is always stored unvalidated.
        api_verified = True
        validation_warnings = []
        validation_errors = []

        if document_type == 'other':
            try:
                logger.info("document_upload: extracting raw text for 'other' document")

                raw_text = _extract_pdf_text(tmp_path)
                json_data = {"_raw_text": raw_text}
                model_tag = "none"
                api_verified = False

            except Exception as e:
                logger.exception("document_upload: raw text extraction error")
                return Response({
                    "error": "Extraction system error",
                    "detail": str(e)
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        else:
            try:
                logger.info(f"document_upload: extracting {document_type}")

                extraction_result = extract_json_from_pdf(tmp_path, document_type)

                if extraction_result.errors:
                    logger.error(f"document_upload: extraction FAILED - {extraction_result.errors}")
                    return Response({
                        "error": "Document extraction failed",
                        "reasons": extraction_result.errors,
                        "detail": "Unable to extract structured data from PDF"
                    }, status=status.HTTP_400_BAD_REQUEST)

                logger.info(f"document_upload: extraction successful for {api_number}")
                json_data = extraction_result.json_data
                model_tag = extraction_result.model_tag

            except Exception as e:
                logger.exception("document_upload: extraction error")
                return Response({
                    "error": "Extraction system error",
                    "detail": str(e)
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # ========== Step 3.5: Verify API from extracted JSON ==========
            try:
                logger.info(f"document_upload: verifying API number for {api_number}")

                validation_result = validate_uploaded_file(
                    file_path=tmp_path,
                    document_type=document_type,
                    expected_api=api_number,
                    skip_security_scan=True,  # Already done in Step 2
                    json_data=json_data,
                )

                if not validation_result.is_valid:
                    warning_code = validation_result.warning_code
                    if warning_code == "api_not_found":
                        # Non-blocking: the well API is authoritative from page
                        # context. Store the document, flagged as unvalidated.
                        logger.warning(f"document_upload: api_not_found for {api_number} - storing flagged")
                        api_verified = False
                        validation_errors = validation_result.errors
                    elif warning_code == "api_mismatch" and not confirmed:
                        logger.warning(f"document_upload: API warning ({warning_code}) - {validation_result.errors}")
                        return Response({
                            "status": "warning",
                            "warning_code": warning_code,
                            "extracted_api": validation_result.extracted_api,
                            "reasons": validation_result.errors,
                        }, status=status.HTTP_200_OK)
                    elif warning_code == "api_mismatch" and confirmed:
                        logger.info(f"document_upload: user confirmed upload despite {warning_code}, proceeding")
                        api_verified = False
                        validation_errors = validation_result.errors
                    else:
                        logger.warning(f"document_upload: API verification FAILED - {validation_result.errors}")
                        return Response({
                            "error": "Validation failed",
                            "reasons": validation_result.errors,
                            "warnings": validation_result.warnings
                        }, status=status.HTTP_400_BAD_REQUEST)
                else:
                    logger.info(f"document_upload: API verification PASSED for {api_number}")

                validation_warnings = validation_result.warnings

            except Exception as e:
                logger.exception("document_upload: API verification error")
                return Response({
                    "error": "Validation system error",
                    "detail": str(e)
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # ========== Step 4: Save to Permanent Storage ==========
        try:
            # Build tenant-aware path: <tenant_id>/<document_type>/<api>_<filename>
            tenant_prefix = str(tenant_id) if tenant_id else 'public'
            safe_filename = uploaded_file.name.replace(' ', '_')  # Remove spaces
            storage_filename = f"{tenant_prefix}/{document_type}/{api_number}_{safe_filename}"
            
            # Save via default_storage (S3 or local, configured in settings)
            with open(tmp_path, 'rb') as f:
                saved_path = default_storage.save(storage_filename, f)
            
            logger.info(f"document_upload: saved to permanent storage at {saved_path}")
        
        except Exception as e:
            logger.exception("document_upload: storage save failed")
            return Response({
                "error": "Failed to save file to storage",
                "detail": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        finally:
            # Clean up temp file
            try:
                tmp_path.unlink()
            except Exception:
                pass
        
        # ========== Step 5: Get or Create Well Registry ==========
        try:
            # Extract well info from JSON (empty for 'other' — no schema)
            well_info = json_data.get('well_info', {})
            
            well, created = WellRegistry.objects.get_or_create(
                api14=api_number,
                defaults={
                    'state': 'TX',  # Default to Texas
                    'county': well_info.get('county', ''),
                    'operator_name': well_info.get('operator', ''),
                    'field_name': well_info.get('field', ''),
                }
            )
            
            if created:
                logger.info(f"document_upload: created new WellRegistry for {api_number}")
            else:
                logger.info(f"document_upload: using existing WellRegistry for {api_number}")
        
        except Exception as e:
            logger.exception("document_upload: failed to create/get WellRegistry")
            # Continue anyway - well can be null
            well = None
        
        # ========== Step 6: Create ExtractedDocument ==========
        try:
            extracted_doc = ExtractedDocument.objects.create(
                well=well,
                api_number=api_number,
                document_type=document_type,
                source_path=saved_path,
                model_tag=model_tag,
                status='success',
                errors=[],
                json_data=json_data,
                # Phase 1 fields (tenant attribution & validation)
                uploaded_by_tenant=tenant_id,
                source_type=ExtractedDocument.SOURCE_TENANT_UPLOAD,
                is_validated=api_verified,
                validation_errors=[] if api_verified else validation_errors,
                attribution_confidence='low' if confirmed else 'high'
            )
            
            logger.info(f"document_upload: created ExtractedDocument {extracted_doc.id}")
        
        except Exception as e:
            logger.exception("document_upload: failed to create ExtractedDocument")
            return Response({
                "error": "Failed to save extraction record",
                "detail": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # ========== Step 6.5: Track Tenant Engagement ==========
        if tenant_id and well:
            try:
                track_well_interaction(
                    tenant_id=tenant_id,
                    well=well,
                    interaction_type=WellEngagement.InteractionType.DOCUMENT_UPLOADED,
                    user=request.user if request.user.is_authenticated else None,
                    metadata_update={
                        'document_id': str(extracted_doc.id),
                        'document_type': document_type,
                        'source_path': saved_path,
                        'is_public': extracted_doc.is_public()
                    }
                )
                logger.info(f"document_upload: tracked engagement for tenant {tenant_id}, well {api_number}")
            except Exception as e:
                logger.exception("document_upload: failed to track engagement (non-fatal)")
                # Don't fail upload if engagement tracking fails
        
        # ========== Step 7: Vectorize Document ==========
        vectors_created = 0
        try:
            logger.info(f"document_upload: vectorizing document {extracted_doc.id}")
            vectors_created = vectorize_extracted_document(extracted_doc)
            logger.info(f"document_upload: created {vectors_created} vectors")
        
        except Exception as e:
            logger.exception("document_upload: vectorization failed (non-fatal)")
            # Don't fail the upload if vectorization fails
            # Document is already saved and can be re-vectorized later
        
        # ========== Step 8: Return Success Response ==========
        is_public = extracted_doc.is_public()
        visibility_msg = "Public (shareable for learning)" if is_public else "Private (tenant-only)"
        
        return Response({
            "success": True,
            "extracted_document_id": str(extracted_doc.id),
            "api_number": api_number,
            "document_type": document_type,
            "is_public": is_public,
            "vectors_created": vectors_created,
            "storage_path": saved_path,
            "warnings": (security_result.warnings or []) + (validation_warnings or []),
            "message": f"Document uploaded, validated, and processed successfully. {visibility_msg}"
        }, status=status.HTTP_201_CREATED)

