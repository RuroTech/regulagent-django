from __future__ import annotations

import logging
import uuid

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.filing_automation.models import FilingJob
from apps.filing_automation.services.adapter import (
    BusinessProfileIncomplete,
    PayloadIncomplete,
    plan_snapshot_to_form_data,
)
from apps.filing_automation.tasks import submit_w3a_to_rrc
from apps.public_core.models import PlanSnapshot


logger = logging.getLogger(__name__)


_ACTIVE_JOB_STATUSES = {"queued", "running", "retrying"}
_SUBMITTABLE_SNAPSHOT_STATUSES = {
    PlanSnapshot.STATUS_ENGINEER_APPROVED,
    PlanSnapshot.STATUS_REVISION_REQUESTED,
}


def _request_tenant_id(request):
    """Best-effort tenant id resolution from the authenticated user.

    Tests force-authenticate a user with an attached ``tenant_id`` attribute.
    In production, ``TenantContextMiddleware`` sets the tenant on the request.
    """
    tenant_id = getattr(request, "tenant_id", None)
    if tenant_id:
        return tenant_id
    user = getattr(request, "user", None)
    if user is not None:
        for attr in ("tenant_id", "current_tenant_id"):
            val = getattr(user, attr, None)
            if val:
                return val
    tenant = getattr(request, "tenant", None)
    if tenant is not None:
        return getattr(tenant, "id", None)
    return None


def _tenant_ids_equal(a, b) -> bool:
    """Compare two tenant ids that may be UUID, str, or int."""
    if a is None or b is None:
        return False
    if a == b:
        return True
    try:
        ua = uuid.UUID(str(a))
    except (ValueError, AttributeError):
        ua = None
    try:
        ub = uuid.UUID(str(b))
    except (ValueError, AttributeError):
        ub = None
    if ua and ub:
        return ua == ub
    return str(a) == str(b)


class W3ASubmitSerializer(serializers.Serializer):
    submitter_name = serializers.CharField(allow_blank=False)
    submitter_title = serializers.CharField(allow_blank=False)
    certification_checked = serializers.BooleanField()

    def validate_certification_checked(self, value):
        if not value:
            raise serializers.ValidationError("Certification must be acknowledged.")
        return value


class W3ASubmitView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, snapshot_id):
        request_tenant_id = _request_tenant_id(request)

        # PlanSnapshot pk is a BigAutoField — accept int directly, but be
        # forgiving of UUID-shaped ids in case the model migrates later.
        try:
            snap_pk = int(snapshot_id)
        except (TypeError, ValueError):
            try:
                snap_pk = uuid.UUID(str(snapshot_id))
            except (ValueError, AttributeError):
                return Response({"detail": "Invalid snapshot id."}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            qs = PlanSnapshot.objects.select_for_update().filter(pk=snap_pk)
            snap = qs.first()
            if snap is None:
                return Response({"detail": "Snapshot not found."}, status=status.HTTP_404_NOT_FOUND)

            # Tenant scope check — 404 to avoid leaking existence.
            if not _tenant_ids_equal(snap.tenant_id, request_tenant_id):
                return Response({"detail": "Snapshot not found."}, status=status.HTTP_404_NOT_FOUND)

            if snap.status not in _SUBMITTABLE_SNAPSHOT_STATUSES:
                return Response(
                    {
                        "detail": (
                            f"Cannot submit snapshot in status '{snap.status}'. "
                            f"Must be one of: {sorted(_SUBMITTABLE_SNAPSHOT_STATUSES)}."
                        ),
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            active_jobs = FilingJob.objects.filter(
                plan_snapshot=snap, status__in=_ACTIVE_JOB_STATUSES,
            )
            if active_jobs.exists():
                return Response(
                    {"detail": "An active filing job already exists for this snapshot."},
                    status=status.HTTP_409_CONFLICT,
                )

            serializer = W3ASubmitSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            attestation = serializer.validated_data

            # Resolve TenantBusinessProfile (best-effort) and gate the call.
            profile = _load_business_profile(snap.tenant_id)
            try:
                form_data, _well_record = plan_snapshot_to_form_data(
                    snap, attestation, profile, enforce_profile=True
                )
            except BusinessProfileIncomplete as exc:
                return Response(
                    {
                        "detail": "Business profile is incomplete for this filing.",
                        "missing_field": exc.field,
                        "settings_url": "/settings/business-profile",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            except PayloadIncomplete as exc:
                return Response(
                    {
                        "detail": "Plan snapshot payload is incomplete.",
                        "missing_field": exc.field,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            job = FilingJob.objects.create(
                plan_snapshot=snap,
                tenant_id=snap.tenant_id,
                status=FilingJob.STATUS_QUEUED,
                attestation=dict(attestation),
            )
            async_result = submit_w3a_to_rrc.apply_async(
                args=[str(snap.id), str(snap.tenant_id), str(job.id)],
                queue="browser",
            )
            celery_task_id = getattr(async_result, "id", "") or ""
            if celery_task_id:
                job.celery_task_id = celery_task_id
                job.save(update_fields=["celery_task_id"])

        return Response(
            {
                "job_id": str(job.id),
                "poll_url": f"/api/w3a/jobs/{job.id}/",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class FilingJobDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, job_id):
        try:
            job_uuid = uuid.UUID(str(job_id))
        except (ValueError, AttributeError):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        job = FilingJob.objects.filter(pk=job_uuid).first()
        if job is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        request_tenant_id = _request_tenant_id(request)
        if not _tenant_ids_equal(job.tenant_id, request_tenant_id):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(
            {
                "status": job.status,
                "confirmation_number": job.confirmation_number,
                "error_message": job.error_message,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "plan_snapshot_id": str(job.plan_snapshot_id),
            },
            status=status.HTTP_200_OK,
        )


def _load_business_profile(tenant_id):
    """Return the TenantBusinessProfile for the given tenant id, or None."""
    from apps.tenants.models import TenantBusinessProfile, Tenant

    if not tenant_id:
        return None

    # Tenant pk is a BigAutoField; tenant_id on most tenant-scoped models
    # is UUIDField. Try both lookup shapes.
    try:
        tenant_uuid = uuid.UUID(str(tenant_id))
    except (ValueError, AttributeError):
        tenant_uuid = None

    candidates = []
    if tenant_uuid is not None:
        # UUID(int=N) round-trip — recover the original int pk.
        if tenant_uuid.int < 2**31:
            candidates.append(tenant_uuid.int)
    candidates.append(tenant_id)

    for tid in candidates:
        try:
            tenant = Tenant.objects.filter(id=tid).first()
        except (ValueError, TypeError):
            continue
        if tenant is not None:
            return TenantBusinessProfile.objects.filter(tenant=tenant).first()
    return None
