"""
Tenant-aware wells API endpoints.

Provides tenant-isolated well queries and interaction history.
Tenants can ONLY query:
- Specific well(s) by API number
- Their own interaction history

Tenants CANNOT query all wells (no unauthenticated browsing).
"""

import logging
from typing import Optional
from uuid import UUID

from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.public_core.models import WellRegistry
from apps.public_core.models.bulk_job import BulkJob
from apps.tenant_overlay.services.engagement_tracker import get_tenant_engagement_list
from apps.tenant_overlay.serializers.tenant_wells import (
    TenantWellSerializer,
    BulkWellRequestSerializer
)

logger = logging.getLogger(__name__)


def get_tenant_id_from_request(request) -> Optional[UUID]:
    """Extract tenant_id from authenticated user."""
    if request.user.is_authenticated:
        user_tenant = request.user.tenants.first()
        return user_tenant.id if user_tenant else None
    return None


@api_view(['GET'])
@authentication_classes([JWTAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def get_well_by_api(request, api14):
    """
    Get a specific well by API-14 number with tenant's interaction history.
    
    GET /api/tenant/wells/{api14}/
    
    Returns:
        - Well data (public info)
        - Tenant's interaction history with this well (private to tenant)
    """
    tenant_id = get_tenant_id_from_request(request)
    
    if not tenant_id:
        return Response(
            {"error": "No tenant associated with user"},
            status=status.HTTP_403_FORBIDDEN
        )
    
    try:
        well = WellRegistry.objects.get(api14=api14)
    except WellRegistry.DoesNotExist:
        return Response(
            {"error": f"Well with API {api14} not found"},
            status=status.HTTP_404_NOT_FOUND
        )
    
    serializer = TenantWellSerializer(well, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(['POST'])
@authentication_classes([JWTAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def bulk_get_wells(request):
    """
    Bulk query wells by list of API numbers with tenant's interaction history.
    
    POST /api/tenant/wells/bulk/
    {
        "api_numbers": ["42123456780000", "42987654320000", ...]
    }
    
    Returns:
        - List of wells found (with tenant interaction history)
        - List of API numbers not found
    
    Limit: 100 wells per request
    """
    tenant_id = get_tenant_id_from_request(request)
    
    if not tenant_id:
        return Response(
            {"error": "No tenant associated with user"},
            status=status.HTTP_403_FORBIDDEN
        )
    
    # Validate request
    request_serializer = BulkWellRequestSerializer(data=request.data)
    if not request_serializer.is_valid():
        return Response(request_serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    api_numbers = request_serializer.validated_data['api_numbers']
    
    # Query wells
    wells = WellRegistry.objects.filter(api14__in=api_numbers)
    found_apis = set(well.api14 for well in wells)
    not_found_apis = [api for api in api_numbers if api not in found_apis]
    
    # Serialize wells with tenant interaction history
    wells_serializer = TenantWellSerializer(wells, many=True, context={'request': request})
    
    logger.info(
        f"Bulk well query by tenant {tenant_id}: requested {len(api_numbers)}, "
        f"found {len(found_apis)}, not found {len(not_found_apis)}"
    )
    
    return Response({
        "wells": wells_serializer.data,
        "not_found": not_found_apis,
        "summary": {
            "requested": len(api_numbers),
            "found": len(found_apis),
            "not_found": len(not_found_apis)
        }
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@authentication_classes([JWTAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def get_tenant_well_history(request):
    """
    Get all wells the authenticated tenant has interacted with.
    
    GET /api/tenant/wells/history/
    
    Query params:
        - limit: Number of wells to return (default: 50, max: 500)
        - offset: Pagination offset (default: 0)
    
    Returns:
        - List of wells tenant has interacted with
        - Ordered by most recent interaction first
        - Includes full interaction history for each well
    """
    tenant_id = get_tenant_id_from_request(request)
    
    if not tenant_id:
        return Response(
            {"error": "No tenant associated with user"},
            status=status.HTTP_403_FORBIDDEN
        )
    
    # Get pagination params
    try:
        limit = min(int(request.query_params.get('limit', 50)), 500)
        offset = int(request.query_params.get('offset', 0))
    except ValueError:
        return Response(
            {"error": "Invalid limit or offset parameter"},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Check for workspace filter
    workspace_id = request.query_params.get('workspace')

    if workspace_id:
        # Derive wells from work products in this workspace
        from apps.public_core.models import PlanSnapshot, W3FormORM
        from apps.public_core.models import WellRegistry
        from django.db.models import Q

        plan_wells = PlanSnapshot.objects.filter(
            tenant_id=tenant_id, workspace_id=workspace_id
        ).values_list('well_id', flat=True)

        w3_wells = W3FormORM.objects.filter(
            tenant_id=tenant_id, workspace_id=workspace_id
        ).values_list('well_id', flat=True)

        well_ids = set(plan_wells) | set(w3_wells)
        wells = WellRegistry.objects.filter(id__in=well_ids).order_by('-updated_at')
        total_count = wells.count()
        wells_page = wells[offset:offset + limit]
        wells_serializer = TenantWellSerializer(wells_page, many=True, context={'request': request})

        return Response({
            "wells": wells_serializer.data,
            "pagination": {
                "total": total_count,
                "limit": limit,
                "offset": offset,
                "has_more": offset + limit < total_count
            }
        }, status=status.HTTP_200_OK)

    # Well registry is a public shared resource — return all wells
    from apps.public_core.models import WellRegistry
    wells_qs = WellRegistry.objects.all().order_by('-updated_at')
    total_count = wells_qs.count()

    wells_page = list(wells_qs[offset:offset + limit])
    wells_serializer = TenantWellSerializer(wells_page, many=True, context={'request': request})

    logger.info(
        f"Well registry query by tenant {tenant_id}: total {total_count} wells, "
        f"returned {len(wells_page)} (offset={offset}, limit={limit})"
    )

    return Response({
        "wells": wells_serializer.data,
        "pagination": {
            "total": total_count,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total_count
        }
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes([JWTAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def import_wells_view(request):
    """
    POST /api/tenant/wells/import/

    Bulk import wells by API numbers. For each well:
    1. Get or create WellRegistry
    2. Create WellEngagement (tenant-well link)
    3. Queue async extraction -> WellComponent population

    Request:  {"api_numbers": ["42383396820000", ...], "workspace_id": 1}
    Response (202): {"job_id": "uuid", "status": "queued", "total_wells": 5, ...}
    """
    from apps.public_core.tasks import bulk_import_wells

    tenant_id = get_tenant_id_from_request(request)
    if not tenant_id:
        return Response(
            {"error": "No tenant associated with user"},
            status=status.HTTP_403_FORBIDDEN,
        )

    api_numbers = request.data.get("api_numbers")
    workspace_id = request.data.get("workspace_id")

    # Validate api_numbers
    if not api_numbers or not isinstance(api_numbers, list):
        return Response(
            {"error": "api_numbers must be a non-empty list"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if len(api_numbers) > 100:
        return Response(
            {"error": "Maximum 100 wells per import request"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    for item in api_numbers:
        if not isinstance(item, str):
            return Response(
                {"error": "Each API number must be a string"},
                status=status.HTTP_400_BAD_REQUEST,
            )

    # Normalize: strip dashes/spaces, pad/truncate to 14 digits where possible
    normalized = []
    for raw in api_numbers:
        cleaned = raw.replace("-", "").replace(" ", "")
        if len(cleaned) < 14:
            cleaned = cleaned.zfill(14)
        elif len(cleaned) > 14:
            cleaned = cleaned[:14]
        normalized.append(cleaned)

    # Create BulkJob
    job = BulkJob.objects.create(
        tenant_id=tenant_id,
        job_type="well_import",
        status=BulkJob.STATUS_QUEUED,
        total_items=len(normalized),
        input_data={
            "api_numbers": normalized,
            "workspace_id": workspace_id,
        },
        created_by=request.user.email,
    )

    logger.info(
        f"Created bulk well import job {job.id} for {len(normalized)} wells "
        f"by user {request.user.email}"
    )

    # Queue Celery batch task
    task = bulk_import_wells.delay(
        job_id=str(job.id),
        api_numbers=normalized,
        tenant_id=str(tenant_id),
        workspace_id=workspace_id,
    )

    logger.info(f"Queued Celery task {task.id} for import job {job.id}")

    return Response(
        {
            "job_id": str(job.id),
            "status": job.status,
            "total_wells": len(normalized),
            "message": f"Well import job queued for {len(normalized)} wells",
        },
        status=status.HTTP_202_ACCEPTED,
    )

