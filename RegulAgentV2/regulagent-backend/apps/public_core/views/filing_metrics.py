"""
Filing Metrics API Endpoint

GET /api/filings/metrics/

Returns aggregated metrics for dashboard display:
- Active filings count (status != 'approved')
- Requires action count (status = 'rejected')
- Average time to submission
- Rejection rate
"""

import uuid as _uuid
from datetime import datetime, timedelta

from django.db import connection
from django_tenants.utils import get_tenant_model, get_public_schema_name
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.authentication import SessionAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication
from django.db.models import Q, Count, Avg, F
from django.utils import timezone

from ..models import PlanSnapshot, W3FormORM


def _get_tenant_uuid(user):
    Tenant = get_tenant_model()
    public_schema = get_public_schema_name()
    schema = connection.schema_name
    if schema != public_schema:
        tenant = Tenant.objects.get(schema_name=schema)
    else:
        tenant = user.tenants.exclude(schema_name=public_schema).first()
    return _uuid.UUID(int=tenant.pk) if tenant else None


class FilingMetricsView(APIView):
    """
    GET /api/filings/metrics/

    Returns filing metrics for the authenticated tenant's filings.
    
    Response:
    {
      "active_filings": 42,
      "requires_action": 8,
      "avg_time_to_submission_seconds": 540,  # 9 minutes
      "rejection_rate": 5.2,  # percentage
      "total_submitted": 77,
      "total_rejected": 4
    }
    """

    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request) -> Response:
        """Get filing metrics"""
        
        tenant_uuid = _get_tenant_uuid(request.user)
        w3a_filter = Q(tenant_id=tenant_uuid) if tenant_uuid else Q(pk__isnull=True)
        
        # === Active Filings ===
        # Count W-3A plans not in 'approved' status
        active_w3a = PlanSnapshot.objects.filter(
            w3a_filter,
            status__in=['draft', 'internal_review', 'engineer_approved', 'filed', 'under_agency_review', 'agency_rejected']
        ).count()
        
        # Count W-3 forms not in 'approved' status
        active_w3 = W3FormORM.objects.filter(
            status__in=['draft', 'submitted', 'rejected', 'archived']
        ).count()
        
        active_filings = active_w3a + active_w3
        
        # === Requires Action ===
        # Count W-3A plans that were rejected
        rejected_w3a = PlanSnapshot.objects.filter(
            w3a_filter,
            status='agency_rejected'
        ).count()
        
        # Count W-3 forms that were rejected
        rejected_w3 = W3FormORM.objects.filter(
            status='rejected'
        ).count()
        
        requires_action = rejected_w3a + rejected_w3
        
        # === Time to Submission ===
        # For W-3 forms: calculate avg time between created_at and submitted_at
        w3_times = W3FormORM.objects.filter(
            submitted_at__isnull=False
        ).annotate(
            time_to_submission=F('submitted_at') - F('created_at')
        ).values_list('time_to_submission', flat=True)
        
        avg_seconds = None
        if w3_times.exists():
            total_seconds = sum(td.total_seconds() for td in w3_times if td is not None)
            count = len([td for td in w3_times if td is not None])
            if count > 0:
                avg_seconds = int(total_seconds / count)
        
        # === Rejection Rate ===
        # Calculate percentage of submitted forms that were rejected
        total_submitted = W3FormORM.objects.filter(
            submitted_at__isnull=False
        ).count()
        
        total_rejected = W3FormORM.objects.filter(
            submitted_at__isnull=False,
            status='rejected'
        ).count()
        
        rejection_rate = 0.0
        if total_submitted > 0:
            rejection_rate = (total_rejected / total_submitted) * 100
        
        return Response({
            "active_filings": active_filings,
            "requires_action": requires_action,
            "avg_time_to_submission_seconds": avg_seconds,
            "rejection_rate": round(rejection_rate, 1),
            "total_submitted": total_submitted,
            "total_rejected": total_rejected,
        }, status=status.HTTP_200_OK)





