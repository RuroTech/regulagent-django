"""
Filing Breakdown Timeline API Endpoint

GET /api/filings/breakdown-timeline/

Returns filing counts grouped by form type and time period,
with filtering by status events (created, submitted, approved, rejected).
Uses django-simple-history to track status changes over time.
"""

from datetime import datetime, timedelta
from collections import defaultdict
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.authentication import SessionAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication
from django.db.models import Q, Count
from django.utils import timezone
import uuid as _uuid
from django.db import connection
from django_tenants.utils import get_tenant_model, get_public_schema_name

from ..models import PlanSnapshot, W3FormORM


def _get_tenant_uuid(user):
    Tenant = get_tenant_model()
    public_schema = get_public_schema_name()
    schema = connection.schema_name
    if schema != public_schema:
        tenant = Tenant.objects.get(schema_name=schema)
    else:
        tenant = user.tenants.exclude(schema_name=public_schema).first()
    if not tenant:
        return None
    return _uuid.UUID(int=tenant.pk)


class FilingBreakdownTimelineView(APIView):
    """
    GET /api/filings/breakdown-timeline/?period=month&event=created&days=30

    Returns filing breakdown by form type over a time period.
    
    Query Parameters:
    - period: 'day', 'week', 'month', 'quarter', 'year' (default: 'month')
    - event: 'created', 'submitted', 'approved', 'rejected', 'rejected_and_submitted'
    - days: number of days to look back (optional, defaults based on period)
    
    Response:
    {
      "period": "month",
      "event": "created",
      "timeline": [
        {
          "date": "2025-01",
          "breakdown": [
            {"form_type": "W-3A", "count": 5},
            {"form_type": "W-3", "count": 8}
          ]
        },
        ...
      ]
    }
    """

    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request) -> Response:
        """Get filing breakdown timeline"""
        
        period = request.query_params.get("period", "month")  # day, week, month, quarter, year
        event = request.query_params.get("event", "created")  # created, submitted, approved, rejected, rejected_and_submitted
        form_types = request.query_params.getlist("form_type")  # W-3A, W-3, etc.
        
        # Validate period
        if period not in ["day", "week", "month", "quarter", "year"]:
            period = "month"
        
        # Validate event
        if event not in ["created", "submitted", "approved", "rejected", "rejected_and_submitted"]:
            event = "created"
        
        # Validate form types if provided
        valid_form_types = ["W-3A", "W-3", "W-2", "W-15", "GAU", "H-5"]
        if form_types:
            form_types = [ft for ft in form_types if ft in valid_form_types]
        if not form_types:
            form_types = valid_form_types  # Default to all if none provided
        
        # Determine lookback days based on period
        days_map = {
            "day": 1,
            "week": 7,
            "month": 30,
            "quarter": 90,
            "year": 365,
        }
        days = int(request.query_params.get("days", days_map[period]))
        
        # Get tenant UUID
        tenant_uuid = _get_tenant_uuid(request.user)

        # Build timeline
        timeline = self._build_timeline(period, event, days, tenant_uuid, form_types)
        
        return Response({
            "period": period,
            "event": event,
            "form_types": form_types,
            "timeline": timeline,
        }, status=status.HTTP_200_OK)

    def _build_timeline(self, period: str, event: str, days: int, tenant_uuid, form_types: list = None):
        """Build timeline of filing counts by period"""

        if form_types is None:
            form_types = ["W-3A", "W-3"]

        now = timezone.now()
        start_date = now - timedelta(days=days)

        timeline_data = defaultdict(lambda: defaultdict(int))

        if event == "created":
            self._process_created_events(start_date, now, period, timeline_data, tenant_uuid, form_types)
        elif event == "submitted":
            self._process_submitted_events(start_date, now, period, timeline_data, form_types)
        elif event == "approved":
            self._process_approved_events(start_date, now, period, timeline_data, tenant_uuid, form_types)
        elif event == "rejected":
            self._process_rejected_events(start_date, now, period, timeline_data, form_types)
        elif event == "rejected_and_submitted":
            self._process_rejected_and_submitted_events(start_date, now, period, timeline_data, form_types)
        
        # Generate complete timeline for the period
        period_keys = self._generate_period_keys(start_date, now, period)
        
        # Convert to response format with all periods (maintain order from period_keys)
        timeline = []
        for period_key in period_keys:
            breakdown = []
            # Get all form types that appear in data
            all_form_types = set()
            for pd in timeline_data.values():
                all_form_types.update(pd.keys())
            
            # Build breakdown for this period
            for form_type in sorted(all_form_types):
                breakdown.append({
                    "form_type": form_type,
                    "count": timeline_data.get(period_key, {}).get(form_type, 0),
                })
            
            timeline.append({
                "date": period_key,
                "breakdown": breakdown,
            })
        
        return timeline  # Return in the order generated by _generate_period_keys (chronological)
    
    def _generate_period_keys(self, start_date: datetime, now: datetime, period: str) -> list:
        """Generate all period keys for the time range"""
        
        keys = []
        
        if period == "day":
            # Generate 30 consecutive days
            # Format: MM/DD
            current = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=29)
            for i in range(30):
                keys.append(current.strftime("%m/%d"))
                current += timedelta(days=1)
        
        elif period == "week":
            # Generate weeks 1-52 for the year
            # Format: W1, W2, ..., W52
            keys = [f"W{i}" for i in range(1, 53)]
        
        elif period == "month":
            # Generate 12 consecutive months
            # Format: MMM 'YY (e.g., "Jan '25")
            current_year = now.year
            current_month = now.month
            
            for i in range(12):
                month = current_month - i
                year = current_year
                
                while month <= 0:
                    month += 12
                    year -= 1
                
                # Create datetime to format nicely
                dt = datetime(year, month, 1)
                keys.insert(0, dt.strftime("%b '%y"))
        
        elif period == "quarter":
            # Generate 4 consecutive quarters
            current_year = now.year
            current_quarter = (now.month - 1) // 3 + 1
            
            for i in range(4):
                q = current_quarter - i
                y = current_year
                
                while q <= 0:
                    q += 4
                    y -= 1
                
                keys.insert(0, f"{y}-Q{q}")
        
        elif period == "year":
            # Generate years (last 5 years)
            for i in range(5):
                keys.insert(0, str(now.year - i))
        
        # Return in chronological order (don't sort alphabetically for months)
        return list(dict.fromkeys(keys))  # Remove duplicates while preserving order

    def _get_period_key(self, dt: datetime, period: str) -> str:
        """Get period key for grouping"""
        if period == "day":
            return dt.strftime("%m/%d")
        elif period == "week":
            # Week 1-52 format
            week_num = int(dt.strftime("%V"))
            return f"W{week_num}"
        elif period == "month":
            return dt.strftime("%b '%y")
        elif period == "quarter":
            quarter = (dt.month - 1) // 3 + 1
            return f"{dt.year}-Q{quarter}"
        elif period == "year":
            return dt.strftime("%Y")
        return dt.strftime("%b '%y")

    def _process_created_events(self, start_date, now, period: str, timeline_data, tenant_uuid, form_types: list):
        """Count filings by created date"""

        # W-3A filings (PlanSnapshot)
        if "W-3A" in form_types:
            w3a_filter = Q(tenant_id=tenant_uuid) if tenant_uuid else Q(pk__isnull=True)

            w3a_filings = PlanSnapshot.objects.filter(w3a_filter).values('created_at').annotate(count=Count('id'))
            for item in w3a_filings:
                period_key = self._get_period_key(item['created_at'], period)
                timeline_data[period_key]["W-3A"] += item['count']
        
        # W-3 filings
        if "W-3" in form_types:
            w3_filings = W3FormORM.objects.all().values('created_at').annotate(count=Count('id'))
            for item in w3_filings:
                period_key = self._get_period_key(item['created_at'], period)
                timeline_data[period_key]["W-3"] += item['count']

    def _process_submitted_events(self, start_date, now, period: str, timeline_data, form_types: list):
        """Count filings by submitted date"""
        
        # W-3 filings with submitted_at
        if "W-3" in form_types:
            w3_filings = W3FormORM.objects.filter(
                submitted_at__isnull=False
            ).values('submitted_at').annotate(count=Count('id'))
            for item in w3_filings:
                period_key = self._get_period_key(item['submitted_at'], period)
                timeline_data[period_key]["W-3"] += item['count']

    def _process_approved_events(self, start_date, now, period: str, timeline_data, tenant_uuid, form_types: list):
        """Count filings by approved date using history"""

        # W-3A approvals (status changed to 'agency_approved')
        if "W-3A" in form_types:
            w3a_filter = Q(status='agency_approved')
            if tenant_uuid:
                w3a_filter &= Q(tenant_id=tenant_uuid)
            else:
                w3a_filter &= Q(pk__isnull=True)
            
            w3a_history = PlanSnapshot.history.filter(w3a_filter).values('history_date').annotate(count=Count('id'))
            for item in w3a_history:
                period_key = self._get_period_key(item['history_date'], period)
                timeline_data[period_key]["W-3A"] += item['count']
        
        # W-3 approvals (status changed to 'approved')
        if "W-3" in form_types:
            w3_history = W3FormORM.history.filter(
                status='approved'
            ).values('history_date').annotate(count=Count('id'))
            for item in w3_history:
                period_key = self._get_period_key(item['history_date'], period)
                timeline_data[period_key]["W-3"] += item['count']

    def _process_rejected_events(self, start_date, now, period: str, timeline_data, form_types: list):
        """Count filings by rejected date using history"""
        
        # W-3 rejections (status changed to 'rejected')
        if "W-3" in form_types:
            w3_history = W3FormORM.history.filter(
                status='rejected'
            ).values('history_date').annotate(count=Count('id'))
            for item in w3_history:
                period_key = self._get_period_key(item['history_date'], period)
                timeline_data[period_key]["W-3"] += item['count']

    def _process_rejected_and_submitted_events(self, start_date, now, period: str, timeline_data, form_types: list):
        """Count filings that were rejected AND submitted"""
        
        # Get W-3 forms that have been both submitted and rejected
        if "W-3" in form_types:
            w3_history = W3FormORM.history.filter(
                status='rejected'
            ).values('id', 'history_date').annotate(count=Count('id'))
            
            # For each rejection, check if it was previously submitted
            for item in w3_history:
                form_id = item['id']
                # Check if this form was ever in 'submitted' status before rejection
                was_submitted = W3FormORM.history.filter(
                    id=form_id,
                    status='submitted'
                ).exists()
                
                if was_submitted:
                    period_key = self._get_period_key(item['history_date'], period)
                    timeline_data[period_key]["W-3"] += item['count']

