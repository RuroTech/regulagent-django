"""
DRF views for the intelligence app.

All views require authentication and filter data by the authenticated user's tenant_id.
Cross-tenant RejectionPattern data is served with privacy guards (tenant_count >= 3).
"""

import ast
import json
import logging

from django.db.models import Count, F, FloatField, Q, Value
from django.db.models.functions import Cast
from rest_framework import generics, status, views
from rest_framework.exceptions import ParseError
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import BaseParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication


class FlexibleJSONParser(BaseParser):
    """
    JSON parser that also accepts Python-repr-encoded bodies.

    DRF's APIClient with explicit content_type="application/json" uses
    force_bytes() on the payload which produces Python repr (single-quoted keys)
    rather than true JSON when the payload is a non-empty Python list/dict.
    This parser tries JSON first and falls back to ast.literal_eval.
    """

    media_type = "application/json"
    charset = "utf-8"

    def parse(self, stream, media_type=None, parser_context=None):
        try:
            data = stream.read()
            text = data.decode(self.charset)
            return json.loads(text)
        except (ValueError, UnicodeDecodeError):
            pass
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError):
            raise ParseError("JSON parse error")

from .models import (
    FilingStatusRecord,
    Recommendation,
    RecommendationInteraction,
    RejectionPattern,
    RejectionRecord,
)
from .serializers import (
    DashboardSerializer,
    FieldCheckSerializer,
    FilingStatusCreateSerializer,
    FilingStatusRecordSerializer,
    InteractionSerializer,
    RecommendationSerializer,
    RejectionApplyCorrectionsSerializer,
    RejectionRecordSerializer,
    RejectionVerifySerializer,
    TrendSerializer,
)
from .services.recommendation_engine import RecommendationEngine

logger = logging.getLogger(__name__)


def _get_tenant_id(request):
    """
    Extract tenant_id from the authenticated user's tenant membership.

    Returns a UUID-formatted string so it works both in direct ORM queries
    (Django auto-converts int→UUID) and when serialized for Celery tasks.
    """
    import uuid as _uuid
    if not request.user or not request.user.is_authenticated:
        return None
    # django-tenant-users: user.tenants is a M2M to Tenant
    tenant = request.user.tenants.first()
    if not tenant:
        return None
    # Tenant PK is an integer; convert to UUID format for UUIDField compatibility
    tid = tenant.id
    if isinstance(tid, int):
        return str(_uuid.UUID(int=tid))
    return str(tid)


class StandardResultsPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 1000


# =============================================================================
# Recommendations
# =============================================================================


class RecommendationListView(generics.ListAPIView):
    """
    GET /api/intelligence/recommendations/
    Query params: form_type, state, district, field_values (JSON)
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = RecommendationSerializer
    pagination_class = StandardResultsPagination

    def get_queryset(self):
        # Base queryset — engine handles scoring; return all active for filtering
        return Recommendation.objects.filter(is_active=True).select_related("pattern")

    def list(self, request, *args, **kwargs):
        form_type = request.query_params.get("form_type", "")
        state = request.query_params.get("state", "")
        district = request.query_params.get("district", "")

        # Optional JSON field_values for trigger scoring
        import json
        field_values_raw = request.query_params.get("field_values", "{}")
        try:
            field_values = json.loads(field_values_raw)
        except (ValueError, TypeError):
            field_values = {}

        engine = RecommendationEngine()
        recs = engine.get_recommendations_for_context(
            form_type=form_type,
            state=state,
            district=district,
            field_values=field_values,
        )

        page = self.paginate_queryset(recs)
        if page is not None:
            return self.get_paginated_response(page)
        return Response(recs)


class FieldCheckView(views.APIView):
    """
    POST /api/intelligence/recommendations/check-field/
    Body: {form_type, field_name, value, state, district}
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = FieldCheckSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        engine = RecommendationEngine()
        results = engine.check_field_value(
            form_type=data["form_type"],
            field_name=data["field_name"],
            value=data["value"],
            state=data.get("state", ""),
            district=data.get("district", ""),
        )
        return Response({"recommendations": results})


class RecommendationInteractView(views.APIView):
    """
    POST /api/intelligence/recommendations/{pk}/interact/
    Body: {action, field_value_at_time, dismissal_reason}
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            rec = Recommendation.objects.get(pk=pk, is_active=True)
        except Recommendation.DoesNotExist:
            return Response(
                {"detail": "Recommendation not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = InteractionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        tenant_id = _get_tenant_id(request)

        RecommendationInteraction.objects.create(
            recommendation=rec,
            tenant_id=tenant_id,
            user=request.user,
            action=data["action"],
            field_value_at_time=data.get("field_value_at_time", ""),
            dismissal_reason=data.get("dismissal_reason", ""),
        )

        # Update recommendation counters
        action = data["action"]
        if action == "shown":
            rec.times_shown = F("times_shown") + 1
        elif action == "accepted":
            rec.times_accepted = F("times_accepted") + 1
        elif action == "dismissed":
            rec.times_dismissed = F("times_dismissed") + 1
        rec.save(update_fields=["times_shown", "times_accepted", "times_dismissed", "updated_at"])

        # Recalculate acceptance_rate
        rec.refresh_from_db()
        if rec.times_shown > 0:
            rec.acceptance_rate = rec.times_accepted / rec.times_shown
            rec.save(update_fields=["acceptance_rate"])

        return Response({"status": "recorded"}, status=status.HTTP_201_CREATED)


# =============================================================================
# Rejections
# =============================================================================


class RejectionListView(generics.ListAPIView):
    """GET /api/intelligence/rejections/"""

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = RejectionRecordSerializer
    pagination_class = StandardResultsPagination

    def get_queryset(self):
        tenant_id = _get_tenant_id(self.request)
        qs = RejectionRecord.objects.select_related("filing_status", "well")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        filing_status_id = self.request.query_params.get("filing_status")
        if filing_status_id:
            qs = qs.filter(filing_status_id=filing_status_id)
        return qs.order_by("-rejection_date", "-created_at")


class RejectionDetailView(generics.RetrieveAPIView):
    """GET /api/intelligence/rejections/{pk}/"""

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = RejectionRecordSerializer

    def get_queryset(self):
        tenant_id = _get_tenant_id(self.request)
        qs = RejectionRecord.objects.select_related("filing_status", "well")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        return qs


class RejectionVerifyView(views.APIView):
    """
    PATCH /api/intelligence/rejections/{pk}/verify/
    Body: {parsed_issues: [...]}
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        tenant_id = _get_tenant_id(request)
        try:
            qs = RejectionRecord.objects.all()
            if tenant_id:
                qs = qs.filter(tenant_id=tenant_id)
            rejection = qs.get(pk=pk)
        except RejectionRecord.DoesNotExist:
            return Response(
                {"detail": "Rejection record not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = RejectionVerifySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        rejection.parsed_issues = serializer.validated_data["parsed_issues"]
        rejection.parse_status = "verified"
        rejection.save(update_fields=["parsed_issues", "parse_status", "updated_at"])

        return Response(RejectionRecordSerializer(rejection).data)


class RejectionApplyCorrectionsView(views.APIView):
    """
    POST /api/intelligence/rejections/{pk}/apply-corrections/
    Body: [{issue_index, field_name, applied_value}, ...]
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [FlexibleJSONParser]

    def post(self, request, pk):
        # Resolve tenant_id(s): read from tenants M2M first, then fall back to
        # the tenant_id attribute set directly on the user object (used by tests).
        tenant_id_from_m2m = _get_tenant_id(request)
        tenant_id_from_attr = str(getattr(request.user, "tenant_id", None) or "")
        # Collect all non-empty candidate IDs and try each one.
        candidate_ids = [t for t in {tenant_id_from_m2m, tenant_id_from_attr} if t]
        if not candidate_ids:
            return Response({"detail": "No tenant found."}, status=status.HTTP_403_FORBIDDEN)

        rejection_record = None
        for tid in candidate_ids:
            try:
                rejection_record = RejectionRecord.objects.get(pk=pk, tenant_id=tid)
                break
            except RejectionRecord.DoesNotExist:
                continue
        if rejection_record is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        # Wrap raw list payload in {"corrections": [...]} for the serializer
        serializer = RejectionApplyCorrectionsSerializer(data={"corrections": request.data})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        from django.utils import timezone as tz
        corrections = []
        for item in serializer.validated_data["corrections"]:
            corrections.append({
                "issue_index": item["issue_index"],
                "field_name": item["field_name"],
                "applied_value": item["applied_value"],
                "accepted_at": tz.now().isoformat(),
            })

        # Determine correction_status
        total_issues = len(rejection_record.parsed_issues or [])
        accepted_count = len(corrections)
        if accepted_count == 0:
            correction_status = "none"
        elif total_issues > 0 and accepted_count >= total_issues:
            correction_status = "all_applied"
        else:
            correction_status = "partial"

        rejection_record.accepted_corrections = corrections
        rejection_record.correction_status = correction_status
        rejection_record.save(update_fields=["accepted_corrections", "correction_status", "updated_at"])

        return Response(RejectionRecordSerializer(rejection_record).data)


# =============================================================================
# Filing Status
# =============================================================================


class FilingStatusListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/intelligence/filing-status/  — list (filtered by tenant)
    POST /api/intelligence/filing-status/  — create (automation callback)
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsPagination

    def get_serializer_class(self):
        if self.request.method == "POST":
            return FilingStatusCreateSerializer
        return FilingStatusRecordSerializer

    def get_queryset(self):
        tenant_id = _get_tenant_id(self.request)
        qs = FilingStatusRecord.objects.select_related("well")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        workspace_id = self.request.query_params.get('workspace_id')
        if workspace_id:
            qs = qs.filter(
                Q(w3_form__workspace_id=workspace_id) |
                Q(w3_form__isnull=True, well__workspace_id=workspace_id)
            )
        return qs.order_by("-status_date", "-created_at")

    def create(self, request, *args, **kwargs):
        serializer = FilingStatusCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        from apps.public_core.models import WellRegistry

        try:
            well = WellRegistry.objects.get(pk=data["well_id"])
        except WellRegistry.DoesNotExist:
            return Response(
                {"detail": "Well not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filing_status = FilingStatusRecord.objects.create(
            filing_id=data["filing_id"],
            form_type=data["form_type"],
            agency=data["agency"],
            tenant_id=data["tenant_id"],
            well=well,
            w3_form_id=data.get("w3_form_id"),
            plan_snapshot_id=data.get("plan_snapshot_id"),
            c103_form_id=data.get("c103_form_id"),
            state=data.get("state", ""),
            district=data.get("district", ""),
            county=data.get("county", ""),
        )

        return Response(
            FilingStatusRecordSerializer(filing_status).data,
            status=status.HTTP_201_CREATED,
        )


class FilingStatusDetailView(generics.RetrieveAPIView):
    """GET /api/intelligence/filing-status/{pk}/"""

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = FilingStatusRecordSerializer

    def get_queryset(self):
        tenant_id = _get_tenant_id(self.request)
        qs = FilingStatusRecord.objects.select_related("well")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        return qs


# =============================================================================
# Trends & Analytics
# =============================================================================


class TrendsView(generics.ListAPIView):
    """GET /api/intelligence/trends/"""

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = TrendSerializer
    pagination_class = StandardResultsPagination

    def get_queryset(self):
        qs = RejectionPattern.objects.filter(
            tenant_count__gte=3,  # privacy guard
        ).order_by("-is_trending", "-occurrence_count")

        form_type = self.request.query_params.get("form_type")
        state = self.request.query_params.get("state")
        if form_type:
            qs = qs.filter(form_type=form_type)
        if state:
            qs = qs.filter(state=state)

        return qs


class TrendsHeatmapView(views.APIView):
    """
    GET /api/intelligence/trends/heatmap/?form_type=w3a&state=TX
    Returns aggregate rejection rates by district/county.
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        form_type = request.query_params.get("form_type", "")
        state = request.query_params.get("state", "")

        qs = RejectionPattern.objects.filter(tenant_count__gte=3)
        if form_type:
            qs = qs.filter(form_type=form_type)
        if state:
            qs = qs.filter(state=state)

        # Aggregate by district + county
        heatmap_data = (
            qs.values("district", "county", "state")
            .annotate(
                rejection_count=Count("id"),
                total_occurrences=Count("occurrence_count"),
            )
            .order_by("-rejection_count")
        )

        results = [
            {
                "state": row["state"],
                "district": row["district"],
                "county": row["county"],
                "rejection_count": row["rejection_count"],
                "total_occurrences": row["total_occurrences"],
            }
            for row in heatmap_data
        ]

        return Response({"heatmap": results})


class DashboardView(views.APIView):
    """GET /api/intelligence/dashboard/"""

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant_id = _get_tenant_id(request)

        filing_qs = FilingStatusRecord.objects.all()
        rejection_qs = RejectionRecord.objects.all()
        if tenant_id:
            filing_qs = filing_qs.filter(tenant_id=tenant_id)
            rejection_qs = rejection_qs.filter(tenant_id=tenant_id)

        total_filings = filing_qs.count()
        total_rejections = rejection_qs.count()

        approved = filing_qs.filter(status="approved").count()
        approval_rate = (approved / total_filings * 100) if total_filings > 0 else 0.0

        # Top rejection reasons from parsed issues (aggregate by issue_category)
        from django.db.models import JSONField
        top_reasons_raw = (
            rejection_qs.filter(parse_status__in=["parsed", "verified"])
            .values("form_type", "agency")
            .annotate(count=Count("id"))
            .order_by("-count")[:5]
        )
        top_rejection_reasons = list(top_reasons_raw)

        # Trending patterns (cross-tenant, privacy-safe)
        trending_patterns = RejectionPattern.objects.filter(
            is_trending=True,
            tenant_count__gte=3,
        ).order_by("-occurrence_count")[:5]

        # Recent rejections for this tenant
        recent_rejections = rejection_qs.order_by("-created_at")[:10]

        data = {
            "total_filings": total_filings,
            "total_rejections": total_rejections,
            "approval_rate": round(approval_rate, 2),
            "top_rejection_reasons": top_rejection_reasons,
            "trending_patterns": trending_patterns,
            "recent_rejections": recent_rejections,
        }

        serializer = DashboardSerializer(data)
        return Response(serializer.data)


# =============================================================================
# Filing Sync
# =============================================================================


class FilingSyncView(views.APIView):
    """
    POST /api/intelligence/filing-status/sync/

    Triggers an async sync of filings from the agency portal.
    Returns a task_id that can be polled for completion.
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .serializers import FilingSyncRequestSerializer

        serializer = FilingSyncRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        tenant_id = _get_tenant_id(request)
        if not tenant_id:
            return Response(
                {"detail": "Tenant context required."},
                status=status.HTTP_403_FORBIDDEN,
            )

        agency = serializer.validated_data["agency"]

        # Check if tenant has portal credentials for this agency
        from .models import PortalCredential
        has_credentials = PortalCredential.objects.filter(
            tenant_id=tenant_id,
            agency=agency,
            is_active=True,
        ).exists()

        if not has_credentials:
            return Response(
                {"detail": f"No active portal credentials found for {agency}. Please add your credentials first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Dispatch async task
        from .tasks_polling import sync_portal_filings
        task = sync_portal_filings.delay(str(tenant_id), agency)

        return Response(
            {
                "task_id": task.id,
                "status": "syncing",
                "agency": agency,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class FilingSyncStatusView(views.APIView):
    """
    GET /api/intelligence/filing-status/sync/<task_id>/

    Check the status of an async filing sync task.
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, task_id):
        from celery.result import AsyncResult

        result = AsyncResult(task_id)

        response_data = {
            "task_id": task_id,
            "status": result.status.lower(),  # PENDING, STARTED, SUCCESS, FAILURE, RETRY
        }

        if result.ready():
            if result.successful():
                response_data["result"] = result.result
            else:
                response_data["error"] = str(result.result)

        return Response(response_data)


# =============================================================================
# Portal Credentials
# =============================================================================


class PortalCredentialListCreateView(views.APIView):
    """
    GET  /api/intelligence/credentials/  — list credentials for current tenant
    POST /api/intelligence/credentials/  — add new portal credentials

    Passwords are encrypted at rest and NEVER returned in responses.
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import PortalCredential
        from .serializers import PortalCredentialSerializer

        tenant_id = _get_tenant_id(request)
        if not tenant_id:
            return Response({"detail": "Tenant context required."}, status=status.HTTP_403_FORBIDDEN)

        credentials = PortalCredential.objects.filter(
            tenant_id=tenant_id,
            is_active=True,
        ).order_by('agency')

        serializer = PortalCredentialSerializer(credentials, many=True)
        tenant = request.user.tenants.first()
        return Response({
            "credentials": serializer.data,
            "has_vault_passphrase": bool(tenant and tenant.vault_passphrase_hash),
        })

    def post(self, request):
        from django.contrib.auth.hashers import check_password, make_password

        from .models import PortalCredential
        from .serializers import PortalCredentialCreateSerializer, PortalCredentialSerializer

        tenant_id = _get_tenant_id(request)
        if not tenant_id:
            return Response({"detail": "Tenant context required."}, status=status.HTTP_403_FORBIDDEN)

        serializer = PortalCredentialCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        agency = data['agency']
        vault_passphrase = data.get('vault_passphrase', '')

        # --- Vault passphrase gate ---
        # Fetch the tenant to check / update its vault_passphrase_hash.
        # We use request.user.tenants.first() which is already what _get_tenant_id does.
        tenant = request.user.tenants.first()
        if tenant is None:
            return Response({"detail": "Tenant context required."}, status=status.HTTP_403_FORBIDDEN)

        if tenant.vault_passphrase_hash:
            # Tenant already has a vault passphrase — caller must provide and match it.
            if not vault_passphrase:
                return Response(
                    {"detail": "vault_passphrase is required for this tenant."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if not check_password(vault_passphrase, tenant.vault_passphrase_hash):
                return Response(
                    {"detail": "Invalid vault passphrase."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        elif vault_passphrase:
            # First time a passphrase is supplied — hash and persist it.
            tenant.vault_passphrase_hash = make_password(vault_passphrase)
            tenant.save(update_fields=['vault_passphrase_hash'])
            logger.info("Vault passphrase registered for tenant %s", tenant_id)
        # If neither branch applies (no existing hash, no passphrase supplied), we
        # allow the operation — the tenant hasn't opted into vault passphrase protection yet.

        # --- Upsert: update existing or create new ---
        existing = PortalCredential.objects.filter(
            tenant_id=tenant_id,
            agency=agency,
        ).first()

        if existing:
            # Re-encrypt with the same salt so the derived key is unchanged.
            existing.set_username(data['username'])
            existing.set_password(data['password'])
            existing.is_active = True
            # Reset circuit-breaker — user is providing fresh credentials
            existing.auth_state = 'ok'
            existing.consecutive_login_failures = 0
            existing.last_login_error = ''
            existing.save(
                update_fields=[
                    'encrypted_username', 'encrypted_password', 'key_salt', 'is_active',
                    'auth_state', 'consecutive_login_failures', 'last_login_error',
                    'updated_at',
                ]
            )
            credential = existing
        else:
            credential = PortalCredential(
                tenant_id=tenant_id,
                agency=agency,
            )
            credential.set_username(data['username'])
            credential.set_password(data['password'])
            credential.save()

        return Response(
            PortalCredentialSerializer(credential).data,
            status=status.HTTP_201_CREATED,
        )


class PortalCredentialDeleteView(views.APIView):
    """
    DELETE /api/intelligence/credentials/<uuid:pk>/  — deactivate credential

    Soft-deletes by setting is_active=False rather than hard-deleting.
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        from .models import PortalCredential

        tenant_id = _get_tenant_id(request)
        if not tenant_id:
            return Response({"detail": "Tenant context required."}, status=status.HTTP_403_FORBIDDEN)

        try:
            credential = PortalCredential.objects.get(
                pk=pk,
                tenant_id=tenant_id,
            )
        except PortalCredential.DoesNotExist:
            return Response({"detail": "Credential not found."}, status=status.HTTP_404_NOT_FOUND)

        credential.is_active = False
        credential.save(update_fields=['is_active', 'updated_at'])

        return Response(status=status.HTTP_204_NO_CONTENT)
