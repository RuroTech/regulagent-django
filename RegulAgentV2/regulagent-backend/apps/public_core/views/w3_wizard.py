"""
W-3 Wizard REST API Views

Endpoints for the W-3 Daily Ticket Upload & Reconciliation Wizard.

POST   /api/w3-wizard/              — Create session
GET    /api/w3-wizard/              — List sessions
GET    /api/w3-wizard/{id}/         — Full session state
DELETE /api/w3-wizard/{id}/         — Abandon session
POST   /api/w3-wizard/{id}/upload/  — Upload daily tickets
POST   /api/w3-wizard/{id}/parse/   — Trigger async parse
GET    /api/w3-wizard/{id}/parse-status/ — Poll parse status
POST   /api/w3-wizard/{id}/reconciliation/ — Trigger reconciliation
GET    /api/w3-wizard/{id}/reconciliation/ — Get reconciliation result
PATCH  /api/w3-wizard/{id}/justifications/ — Save engineer justifications
POST   /api/w3-wizard/{id}/generate-w3/   — Generate W-3 form
GET    /api/w3-wizard/{id}/w3-form/        — Retrieve generated W-3 form
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import HttpResponse
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.public_core.models import (
    PlanSnapshot,
    W3FormORM,
    W3WizardSession,
    WellRegistry,
)
from apps.public_core.serializers.w3_wizard import (
    PlanVerificationSerializer,
    TaskStatusSerializer,
    W3WizardCreateSerializer,
    W3WizardJustificationsSerializer,
    W3WizardListSerializer,
    W3WizardSessionSerializer,
    W3WizardUploadResponseSerializer,
)
from apps.public_core.services.universal_ticket_parser import SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)


def _get_tenant_id(request):
    """
    Extract tenant UUID from the authenticated user.

    Matches the pattern used across bulk_operations and w3a_from_api views:
    request.user.tenants.first().id
    """
    user_tenant = request.user.tenants.first()
    return user_tenant.id if user_tenant else None


def _get_session(pk, tenant_id):
    """
    Load a W3WizardSession by PK and enforce tenant isolation.
    Returns (session, error_response) — one of which is None.

    Tenant isolation is enforced at the database level by filtering on
    tenant_id directly in the queryset. This avoids a string comparison
    bug where session.tenant_id (a UUID like 00000000-0000-0000-0000-000000000002)
    would never equal tenant_id (an integer like 2) via str() coercion.
    PostgreSQL handles the int->UUID cast automatically.
    """
    try:
        qs = W3WizardSession.objects.select_related('plan_snapshot').filter(pk=pk)
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        session = qs.get()
    except W3WizardSession.DoesNotExist:
        return None, Response(
            {"error": "Wizard session not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    return session, None


class W3WizardCreateView(APIView):
    """
    POST /api/w3-wizard/  — Create a new wizard session.
    GET  /api/w3-wizard/  — List wizard sessions for the current tenant.
    """

    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = W3WizardCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "Invalid request", "validation_errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tenant_id = _get_tenant_id(request)
        if not tenant_id:
            return Response(
                {"error": "User not associated with any tenant"},
                status=status.HTTP_403_FORBIDDEN,
            )

        api_number = serializer.validated_data["api_number"]
        workspace_id = serializer.validated_data.get("workspace_id")

        # Normalize API number (strip dashes for flexible lookup)
        normalized_api = api_number.replace("-", "")

        # Try to find well — optional, not required
        well = None
        for lookup in [api_number, normalized_api]:
            well = WellRegistry.objects.filter(api14=lookup).first()
            if well:
                break

        # Auto-resolve plan snapshot: approved first, then baseline
        plan_snapshot = None
        if well:
            plan_snapshot = (
                PlanSnapshot.objects.filter(well=well, kind=PlanSnapshot.KIND_APPROVED)
                .order_by("-created_at")
                .first()
            )
            if not plan_snapshot:
                plan_snapshot = (
                    PlanSnapshot.objects.filter(well=well, kind=PlanSnapshot.KIND_BASELINE)
                    .order_by("-created_at")
                    .first()
                )

        # Resolve workspace
        workspace = None
        if workspace_id:
            from apps.tenants.models import ClientWorkspace
            workspace = ClientWorkspace.objects.filter(
                id=workspace_id, tenant_id=tenant_id
            ).first()

        # Create session — well and plan_snapshot are optional
        session = W3WizardSession.objects.create(
            well=well,
            plan_snapshot=plan_snapshot,
            tenant_id=tenant_id,
            workspace=workspace,
            api_number=api_number,
            status=W3WizardSession.STATUS_CREATED,
            created_by=request.user.email,
        )

        logger.info(
            "W3WizardSession created: id=%s api=%s tenant=%s",
            session.id,
            api_number,
            tenant_id,
        )

        return Response(
            W3WizardSessionSerializer(session).data,
            status=status.HTTP_201_CREATED,
        )

    def get(self, request, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        if not tenant_id:
            return Response(
                {"error": "User not associated with any tenant"},
                status=status.HTTP_403_FORBIDDEN,
            )

        qs = W3WizardSession.objects.filter(tenant_id=tenant_id)

        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        workspace_filter = request.query_params.get("workspace")
        if workspace_filter:
            qs = qs.filter(workspace_id=workspace_filter)

        return Response(W3WizardListSerializer(qs, many=True).data)


class W3WizardDetailView(APIView):
    """
    GET    /api/w3-wizard/{id}/ — Full session state.
    DELETE /api/w3-wizard/{id}/ — Abandon session.
    """

    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        # Refresh last_accessed_at without triggering auto_now on updated_at
        W3WizardSession.objects.filter(pk=session.pk).update(
            last_accessed_at=datetime.now(tz=timezone.utc)
        )
        session.refresh_from_db()

        return Response(W3WizardSessionSerializer(session).data)

    def delete(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        session.status = W3WizardSession.STATUS_ABANDONED
        session.save(update_fields=["status", "updated_at"])

        logger.info("W3WizardSession abandoned: id=%s", session.id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class W3WizardUploadView(APIView):
    """
    POST /api/w3-wizard/{id}/upload/ — Upload daily ticket files.

    Accepts multipart/form-data with one or more files under the 'files' key.
    """

    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        uploaded_files = request.FILES.getlist("files")
        if not uploaded_files:
            return Response(
                {"error": "No files provided. Send files under the 'files' key."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        category = request.data.get("category", "tickets")
        if category not in ("plan", "tickets"):
            return Response(
                {"error": "Invalid category. Must be 'plan' or 'tickets'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Plan upload: exactly 1 .docx file
        if category == "plan":
            if len(uploaded_files) != 1:
                return Response(
                    {"error": "Plan upload requires exactly 1 file."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            f = uploaded_files[0]
            ext = Path(f.name).suffix.lower()
            if ext not in (".docx", ".doc", ".pdf"):
                return Response(
                    {"error": "Plan file must be a .docx or .pdf document."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            storage_key = f"w3_wizard/{session.id}/plan/{f.name}"
            default_storage.save(storage_key, f)
            file_type = "pdf" if ext == ".pdf" else "docx"
            doc_meta = {
                "file_name": f.name,
                "file_type": file_type,
                "storage_key": storage_key,
                "uploaded_at": datetime.now(tz=timezone.utc).isoformat(),
                "size_bytes": f.size,
                "category": "plan",
            }

            # Replace any existing plan doc
            existing_docs = [d for d in (session.uploaded_documents or []) if d.get("category") != "plan"]
            session.uploaded_documents = existing_docs + [doc_meta]
            session.status = W3WizardSession.STATUS_IMPORTING_PLAN
            session.save(update_fields=["uploaded_documents", "status", "updated_at"])

            # Kick off async plan import
            from apps.public_core.tasks_w3_wizard import import_wizard_plan
            task = import_wizard_plan.delay(str(session.id), storage_key)
            session.plan_import_task_id = task.id
            session.save(update_fields=["plan_import_task_id", "updated_at"])

            logger.info(
                "W3WizardUpload: session %s plan upload → task %s",
                session.id, task.id,
            )

            return Response(
                {
                    "uploaded_count": 1,
                    "documents": [doc_meta],
                    "session_status": session.status,
                    "plan_import_task_id": task.id,
                },
                status=status.HTTP_200_OK,
            )

        # Ticket upload: current behavior with category tag
        new_documents = []
        rejected = []

        for f in uploaded_files:
            ext = Path(f.name).suffix.lower()
            file_type = SUPPORTED_EXTENSIONS.get(ext)
            if not file_type:
                rejected.append(f.name)
                logger.warning(
                    "W3WizardUpload: unsupported file type '%s' for session %s",
                    f.name,
                    session.id,
                )
                continue

            storage_key = f"w3_wizard/{session.id}/{f.name}"
            default_storage.save(storage_key, f)
            doc_meta = {
                "file_name": f.name,
                "file_type": file_type,
                "storage_key": storage_key,
                "uploaded_at": datetime.now(tz=timezone.utc).isoformat(),
                "size_bytes": f.size,
                "category": "tickets",
            }
            new_documents.append(doc_meta)

        if not new_documents:
            return Response(
                {
                    "error": "All files were rejected — unsupported file types.",
                    "rejected": rejected,
                    "supported_extensions": list(SUPPORTED_EXTENSIONS.keys()),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        session.uploaded_documents = (session.uploaded_documents or []) + new_documents
        session.status = W3WizardSession.STATUS_UPLOADING
        session.save(update_fields=["uploaded_documents", "status", "updated_at"])

        logger.info(
            "W3WizardUpload: session %s accepted %d file(s), rejected %d",
            session.id,
            len(new_documents),
            len(rejected),
        )

        response_data = {
            "uploaded_count": len(new_documents),
            "documents": new_documents,
            "session_status": session.status,
        }
        if rejected:
            response_data["rejected"] = rejected

        return Response(
            W3WizardUploadResponseSerializer(response_data).data,
            status=status.HTTP_200_OK,
        )


class W3WizardDocumentToggleExclusionView(APIView):
    """
    PATCH /api/w3-wizard/{id}/documents/toggle-exclusion/
    Toggle the is_excluded flag on an uploaded document.
    Body: {"storage_key": "...", "is_excluded": true/false}
    """
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def patch(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        storage_key = request.data.get("storage_key")
        is_excluded = request.data.get("is_excluded", True)

        if not storage_key:
            return Response(
                {"error": "storage_key is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Find the document by storage_key
        doc_found = False
        for doc in (session.uploaded_documents or []):
            if doc.get("storage_key") == storage_key:
                doc["is_excluded"] = bool(is_excluded)
                doc_found = True
                break

        if not doc_found:
            return Response(
                {"error": f"Document with storage_key '{storage_key}' not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        session.save(update_fields=["uploaded_documents", "updated_at"])

        return Response(
            {"documents": session.uploaded_documents},
            status=status.HTTP_200_OK,
        )


class W3WizardParseView(APIView):
    """
    POST /api/w3-wizard/{id}/parse/ — Kick off async ticket parsing.
    """

    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        if not session.uploaded_documents:
            return Response(
                {"error": "No uploaded documents to parse. Upload files first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Block parsing until plan data has been verified
        if session.plan_snapshot and session.status == W3WizardSession.STATUS_PLAN_IMPORTED:
            return Response(
                {"error": "Plan data must be verified before parsing tickets. Please review and confirm the extracted plan data."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.public_core.tasks_w3_wizard import parse_wizard_tickets

        task = parse_wizard_tickets.delay(str(session.id))
        session.celery_task_id = task.id
        session.save(update_fields=["celery_task_id", "updated_at"])

        logger.info(
            "W3WizardParse: session %s → task %s", session.id, task.id
        )

        return Response(
            {"task_id": task.id, "status": "PENDING"},
            status=status.HTTP_202_ACCEPTED,
        )


class W3WizardParseStatusView(APIView):
    """
    GET /api/w3-wizard/{id}/parse-status/ — Poll the current parse task.
    """

    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        if not session.celery_task_id:
            return Response(
                {"error": "No parse task has been started for this session."},
                status=status.HTTP_404_NOT_FOUND,
            )

        from celery.result import AsyncResult

        result = AsyncResult(session.celery_task_id)

        # Refresh session from DB so status reflects Celery task completion
        session.refresh_from_db()

        data = {
            "task_id": session.celery_task_id,
            "status": result.status,
            "session_status": session.status,
        }
        if result.status == "SUCCESS":
            data["result"] = result.result if isinstance(result.result, dict) else {}
        elif result.status == "FAILURE":
            data["result"] = {"error": str(result.result)}

        return Response(TaskStatusSerializer(data).data)


class W3WizardPlanImportStatusView(APIView):
    """
    GET /api/w3-wizard/{id}/plan-import-status/ — Poll plan import task.
    """

    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        if not session.plan_import_task_id:
            return Response(
                {"error": "No plan import task has been started for this session."},
                status=status.HTTP_404_NOT_FOUND,
            )

        from celery.result import AsyncResult

        task_result = AsyncResult(session.plan_import_task_id)
        celery_status = task_result.status  # PENDING, STARTED, SUCCESS, FAILURE

        # Refresh session to get latest status
        session.refresh_from_db()

        response = {
            "task_id": session.plan_import_task_id,
            "status": celery_status,
            "session_status": session.status,
            "plan_snapshot": str(session.plan_snapshot_id) if session.plan_snapshot_id else None,
        }

        # Include any import errors stored in parse_result
        if session.parse_result and session.parse_result.get("plan_import_error"):
            response["error"] = session.parse_result["plan_import_error"]
            response["reasons"] = session.parse_result.get("plan_import_reasons", [])

        return Response(response)


class W3WizardVerifyPlanView(APIView):
    """
    GET  /api/w3-wizard/{id}/plan-verification/ — Return extracted plan data for review.
    PUT  /api/w3-wizard/{id}/plan-verification/ — Accept corrected plan data, advance status.
    """

    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def get(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        if not session.plan_snapshot or not session.plan_snapshot.payload:
            return Response(
                {"error": "No plan snapshot available to verify."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = session.plan_snapshot.payload
        return Response({
            "plan_snapshot_id": str(session.plan_snapshot_id),
            "sections": {
                "well_header": payload.get("well_header", {}),
                "steps": payload.get("steps", []),
                "formations": payload.get("formations", []),
                "casing_record": payload.get("casing_record", []),
                "existing_perforations": payload.get("existing_perforations", []),
            },
            "session_status": session.status,
        })

    def put(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        if session.status != W3WizardSession.STATUS_PLAN_IMPORTED:
            return Response(
                {"error": "Plan must be in 'plan_imported' state to verify. Current status: " + session.status},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not session.plan_snapshot:
            return Response(
                {"error": "No plan snapshot to verify."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = PlanVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        corrections = serializer.validated_data

        # Merge corrections into plan_snapshot.payload
        plan_snapshot = session.plan_snapshot
        payload = plan_snapshot.payload or {}

        for key in ["well_header", "steps", "formations", "casing_record", "existing_perforations"]:
            if corrections.get(key):
                payload[key] = corrections[key]

        # Sync well_geometry if it exists — update formation_tops from corrected formations
        if payload.get("well_geometry") and isinstance(payload["well_geometry"], dict):
            if corrections.get("formations"):
                payload["well_geometry"]["formation_tops"] = [
                    {"formation": f.get("formation_name", ""), "top_ft": f.get("top_ft")}
                    for f in corrections["formations"]
                ]

        plan_snapshot.payload = payload
        plan_snapshot.save()

        # Advance session status
        session.status = W3WizardSession.STATUS_PLAN_VERIFIED
        session.save(update_fields=["status", "updated_at"])

        logger.info(
            "W3WizardVerifyPlan: session %s plan verified, advancing to plan_verified",
            session.id,
        )

        return Response(
            W3WizardSessionSerializer(session).data,
            status=status.HTTP_200_OK,
        )


class W3WizardReconcileView(APIView):
    """
    POST /api/w3-wizard/{id}/reconciliation/ — Trigger reconciliation task.
    GET  /api/w3-wizard/{id}/reconciliation/ — Return cached result.
    """

    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        from apps.public_core.tasks_w3_wizard import run_wizard_reconciliation

        task = run_wizard_reconciliation.delay(str(session.id))
        session.celery_task_id = task.id
        session.save(update_fields=["celery_task_id", "updated_at"])

        logger.info(
            "W3WizardReconcile: session %s → task %s", session.id, task.id
        )

        return Response(
            {"task_id": task.id, "status": "PENDING"},
            status=status.HTTP_202_ACCEPTED,
        )

    def get(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        return Response(
            {
                "reconciliation_result": session.reconciliation_result,
                "session_status": session.status,
            }
        )


class W3WizardJustificationsView(APIView):
    """
    PATCH /api/w3-wizard/{id}/justifications/ — Merge engineer justifications.

    Incoming justifications are merged (not replaced) into the existing dict.
    If all major divergences are resolved the session advances to STATUS_READY.
    """

    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        serializer = W3WizardJustificationsSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "Invalid request", "validation_errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        incoming = serializer.validated_data["justifications"]

        # Merge — incoming entries override existing ones with the same key
        existing = session.justifications or {}
        existing.update(incoming)
        session.justifications = existing

        # Advance status
        if session.status not in (
            W3WizardSession.STATUS_READY,
            W3WizardSession.STATUS_GENERATING,
            W3WizardSession.STATUS_COMPLETED,
        ):
            session.status = W3WizardSession.STATUS_JUSTIFYING

        # Check whether all major divergences are resolved
        all_resolved = all(
            entry.get("resolved", False)
            for entry in existing.values()
            if isinstance(entry, dict)
        )
        if all_resolved and existing:
            session.status = W3WizardSession.STATUS_READY
            logger.info(
                "W3WizardJustifications: all divergences resolved — session %s → ready",
                session.id,
            )

        # Sync justification overrides into reconciliation_result for WBD preview
        save_fields = ["justifications", "status", "updated_at"]
        if session.reconciliation_result and session.reconciliation_result.get("comparisons"):
            comparisons = session.reconciliation_result["comparisons"]
            for comp in comparisons:
                plug_key = str(comp.get("plug_number"))
                entry = existing.get(plug_key, {})
                if not isinstance(entry, dict):
                    continue
                # Apply depth/sack overrides to comparison actuals
                if "depth_top_ft_override" in entry:
                    comp["actual_top_ft"] = entry["depth_top_ft_override"]
                if "depth_bottom_ft_override" in entry:
                    comp["actual_bottom_ft"] = entry["depth_bottom_ft_override"]
                if "sacks_override" in entry:
                    comp["actual_sacks"] = entry["sacks_override"]
                # Mark excluded
                if entry.get("excluded"):
                    comp["excluded"] = True
                elif "excluded" in comp:
                    # Un-exclude if user toggled back
                    del comp["excluded"]

            # Remove excluded plugs from comparisons for WBD rendering
            session.reconciliation_result["comparisons"] = [
                c for c in comparisons if not c.get("excluded")
            ]
            save_fields.append("reconciliation_result")

        session.save(update_fields=save_fields)

        return Response(
            {"justifications": session.justifications, "session_status": session.status}
        )


class W3WizardGenerateView(APIView):
    """
    POST /api/w3-wizard/{id}/generate-w3/ — Trigger W-3 generation.

    Session must be in STATUS_READY (all major divergences resolved).
    """

    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        if session.status != W3WizardSession.STATUS_READY:
            return Response(
                {
                    "error": (
                        f"Session is not ready for generation. "
                        f"Current status: {session.status}. "
                        "Resolve all major divergences first."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.public_core.tasks_w3_wizard import generate_wizard_w3

        task = generate_wizard_w3.delay(str(session.id))
        session.celery_task_id = task.id
        session.status = W3WizardSession.STATUS_GENERATING
        session.save(update_fields=["celery_task_id", "status", "updated_at"])

        logger.info(
            "W3WizardGenerate: session %s → task %s", session.id, task.id
        )

        return Response(
            {"task_id": task.id, "status": "PENDING"},
            status=status.HTTP_202_ACCEPTED,
        )


class W3WizardW3FormView(APIView):
    """
    GET /api/w3-wizard/{id}/w3-form/ — Retrieve the generated W-3 form.
    """

    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        if not session.w3_form_id:
            return Response(
                {"error": "No W-3 form has been generated for this session yet."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            w3_form = W3FormORM.objects.get(pk=session.w3_form_id)
        except W3FormORM.DoesNotExist:
            return Response(
                {"error": "W-3 form record not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "id": str(w3_form.id),
                "api_number": w3_form.api_number,
                "status": w3_form.status,
                "form_data": w3_form.form_data,
                "pdf_url": session.w3_generation_result.get("pdf_url") if session.w3_generation_result else None,
            }
        )


class W3WizardUploadWbdImageView(APIView):
    """
    POST /api/w3-wizard/{id}/upload-wbd-image/ — Upload captured WBD PNG.
    """

    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        uploaded_file = request.FILES.get("image")
        if not uploaded_file:
            return Response(
                {"error": "No image provided. Send file under the 'image' key."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate file type
        if not uploaded_file.content_type.startswith("image/"):
            return Response(
                {"error": "File must be an image (PNG/JPEG)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        save_dir = Path(settings.MEDIA_ROOT) / "wbd_images"
        save_dir.mkdir(parents=True, exist_ok=True)

        dest = save_dir / f"{session.id}.png"
        with open(dest, "wb") as out:
            for chunk in uploaded_file.chunks():
                out.write(chunk)

        session.wbd_image_path = str(dest)
        session.save(update_fields=["wbd_image_path", "updated_at"])

        logger.info("W3WizardUploadWbdImage: session %s → %s", session.id, dest)

        return Response({"wbd_image_path": str(dest)}, status=status.HTTP_200_OK)


class W3WizardExportWBDExcelView(APIView):
    """
    GET /api/w3-wizard/{id}/export-wbd-excel/ — Download WBD as Excel workbook.
    """
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        reconciliation = session.reconciliation_result
        if not reconciliation or not reconciliation.get("comparisons"):
            return Response(
                {"error": "No reconciliation data available. Complete reconciliation before exporting WBD."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Build well geometry using the same logic as the serializer
        from apps.public_core.serializers.w3_wizard import W3WizardSessionSerializer
        serializer = W3WizardSessionSerializer()
        well_geometry = serializer.get_plan_snapshot_well_geometry(session) or {}

        # Build well header
        well_header = {}
        if session.plan_snapshot and hasattr(session.plan_snapshot, 'payload') and session.plan_snapshot.payload:
            well_header = session.plan_snapshot.payload.get("well_header", {})

        data = {
            "well_header": well_header,
            "jurisdiction": getattr(session, 'jurisdiction', None) or "TX",
            "comparisons": reconciliation["comparisons"],
            "well_geometry": well_geometry,
        }

        from apps.public_core.services.wbd_excel_generator import generate_wbd_excel
        excel_buffer = generate_wbd_excel(data)

        api14 = session.api_number or "unknown"
        id_short = str(session.id)[:8]
        filename = f"WBD_{api14}_{id_short}.xlsx"

        response = HttpResponse(
            excel_buffer.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        logger.info("W3WizardExportWBDExcel: session %s → %s", session.id, filename)
        return response


class W3WizardImportWBDExcelView(APIView):
    """
    POST /api/w3-wizard/{id}/import-wbd-excel/ — Upload edited WBD Excel to update diagram data.
    """
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, pk, *args, **kwargs):
        tenant_id = _get_tenant_id(request)
        session, err = _get_session(pk, tenant_id)
        if err:
            return err

        excel_file = request.FILES.get("excel_file")
        if not excel_file:
            return Response(
                {"error": "No excel_file provided. Send the .xlsx file under the 'excel_file' key."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not excel_file.name.lower().endswith(".xlsx"):
            return Response(
                {"error": "File must be .xlsx format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.public_core.services.wbd_excel_parser import parse_wbd_excel
        try:
            parsed = parse_wbd_excel(excel_file.read())
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        # Store as non-destructive overlay in reconciliation_result
        reconciliation = session.reconciliation_result or {}
        reconciliation["excel_overrides"] = {
            "plugs": parsed.get("plugs", []),
            "well_geometry": parsed.get("well_geometry", {}),
        }
        session.reconciliation_result = reconciliation
        session.save(update_fields=["reconciliation_result", "updated_at"])

        logger.info(
            "W3WizardImportWBDExcel: session %s — %d plugs, %d warnings",
            session.id, len(parsed.get("plugs", [])), len(parsed.get("warnings", [])),
        )

        return Response({
            "status": "success",
            "updated_fields": [k for k in parsed if k != "warnings"],
            "warnings": parsed.get("warnings", []),
        })
