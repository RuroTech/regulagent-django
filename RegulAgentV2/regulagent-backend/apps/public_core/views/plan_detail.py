"""
Plan detail endpoint - retrieve full plan payload for viewing and chat interaction.

This is the primary endpoint users interact with to:
- View the complete baseline plan
- Initiate chat-based modifications
- See current workflow status
"""

import logging
import uuid as _uuid

from django.db import connection
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication
from django_tenants.utils import get_tenant_model, get_public_schema_name

from apps.public_core.models import PlanSnapshot, ExtractedDocument
from apps.public_core.services.well_geometry_builder import build_well_geometry

logger = logging.getLogger(__name__)


def _resolve_tenant(user):
    """Return the business Tenant for the current request (prod + test safe)."""
    Tenant = get_tenant_model()
    public_schema = get_public_schema_name()
    schema = connection.schema_name
    if schema != public_schema:
        return Tenant.objects.get(schema_name=schema)
    return user.tenants.exclude(schema_name=public_schema).first()


@api_view(['GET'])
@authentication_classes([JWTAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def get_plan_detail(request, plan_id):
    """
    Retrieve complete plan with full payload.
    
    GET /api/plans/{plan_id}/
    
    Accepts either:
    - plan_id string (e.g., "4230132998:isolated") - returns latest snapshot with that plan_id
    - snapshot ID (e.g., "145") - returns that specific snapshot
    
    Returns:
        - Full plan JSON (steps, violations, materials, etc.)
        - Workflow status
        - Well information
        - Metadata (kernel version, policy, extraction info)
    
    This is the primary plan view that users interact with before
    making modifications via chat or manual edits.
    """
    # 🚀 CRITICAL: Log that this view was called
    logger.error(f"🚀🚀🚀 get_plan_detail CALLED with plan_id={plan_id}")
    
    user_tenant = _resolve_tenant(request.user)
    if not user_tenant:
        return Response(
            {"error": "User not associated with any tenant"},
            status=status.HTTP_403_FORBIDDEN
        )

    tenant_uuid = _uuid.UUID(int=user_tenant.pk)

    # Try to get snapshot — accept plan_id string, UUID, or integer PK
    try:
        if plan_id.isdigit():
            # Numeric integer PK
            snapshot = (
                PlanSnapshot.objects
                .select_related('well')
                .filter(id=int(plan_id), tenant_id=tenant_uuid)
                .first()
            )
        else:
            # Try UUID first (PlanSnapshot.id is UUID on some deployments)
            try:
                uid = _uuid.UUID(plan_id)
                snapshot = (
                    PlanSnapshot.objects
                    .select_related('well')
                    .filter(id=uid, tenant_id=tenant_uuid)
                    .first()
                )
            except ValueError:
                snapshot = None

            # Fall back to plan_id string lookup (canonical case)
            if not snapshot:
                snapshot = (
                    PlanSnapshot.objects
                    .select_related('well')
                    .filter(plan_id=plan_id, tenant_id=tenant_uuid)
                    .order_by('-created_at')
                    .first()
                )

        if not snapshot:
            raise PlanSnapshot.DoesNotExist

        logger.info(f"Found snapshot for plan_id={plan_id}: snapshot.id={snapshot.id}")

    except PlanSnapshot.DoesNotExist:
        return Response(
            {"error": f"Plan {plan_id} not found"},
            status=status.HTTP_404_NOT_FOUND
        )
    
    # Get payload first (needed for formation extraction)
    payload = snapshot.payload.copy() if isinstance(snapshot.payload, dict) else snapshot.payload
    
    # 🔍 DEBUG: Log what's in the retrieved payload
    logger.error(f"❌ RETRIEVED SNAPSHOT PAYLOAD: casing_strings count = {len(payload.get('casing_strings', []))}")
    if payload.get('casing_strings'):
        logger.error(f"❌ RETRIEVED: First casing = {payload['casing_strings'][0]}")
    else:
        logger.error(f"❌❌❌ RETRIEVED: casing_strings is EMPTY in payload!")
    
    # Fetch well geometry from extracted documents (pass payload for formation extraction)
    # Derive jurisdiction from the well's state so W-2 (TX) data is not used for NM wells
    _well_state = (snapshot.well.state or "").upper().strip()
    _jurisdiction = "NM" if _well_state == "NM" else ("TX" if _well_state == "TX" else None)
    well_geometry = build_well_geometry(snapshot.well.api14, payload, jurisdiction=_jurisdiction)
    if isinstance(payload, dict):
        if well_geometry.get("historic_cement_jobs"):
            payload["historic_cement_jobs"] = well_geometry["historic_cement_jobs"]
        if well_geometry.get("production_perforations"):
            payload["production_perforations"] = well_geometry["production_perforations"]
        if well_geometry.get("mechanical_equipment"):
            payload["mechanical_equipment"] = well_geometry["mechanical_equipment"]
        if well_geometry.get("existing_tools"):
            payload["existing_tools"] = well_geometry["existing_tools"]
    
    # Build response with full plan data
    response_data = {
        # Plan metadata
        "id": snapshot.id,
        "plan_id": snapshot.plan_id,
        "kind": snapshot.kind,
        "status": snapshot.status,
        "visibility": snapshot.visibility,
        "tenant_id": str(snapshot.tenant_id) if snapshot.tenant_id else None,
        
        # Well information
        "well": {
            "api14": snapshot.well.api14,
            "state": snapshot.well.state,
            "county": snapshot.well.county,
            "operator_name": snapshot.well.operator_name,
            "field_name": snapshot.well.field_name,
            "lease_name": snapshot.well.lease_name,
            "well_number": snapshot.well.well_number,
            "lat": float(snapshot.well.lat) if snapshot.well.lat else None,
            "lon": float(snapshot.well.lon) if snapshot.well.lon else None,
        },
        
        # Well geometry (casing, formations, perforations) - critical for chat context
        "well_geometry": well_geometry,
        
        # Provenance
        "kernel_version": snapshot.kernel_version,
        "policy_id": snapshot.policy_id,
        "overlay_id": snapshot.overlay_id,
        "extraction_meta": snapshot.extraction_meta,
        
        # Timestamps
        "created_at": snapshot.created_at,
        
        # THE ACTUAL PLAN - This is what the user sees and modifies
        "payload": payload,
    }
    
    # 🔍 DEBUG: Log what's in the final response being sent to frontend
    logger.error(f"❌ FINAL RESPONSE: well_geometry.casing_strings count = {len(response_data['well_geometry'].get('casing_strings', []))}")
    if response_data['well_geometry'].get('casing_strings'):
        logger.error(f"❌ FINAL RESPONSE: First casing = {response_data['well_geometry']['casing_strings'][0]}")
    else:
        logger.error(f"❌❌❌ FINAL RESPONSE: casing_strings is EMPTY in well_geometry!")
    
    logger.info(f"Retrieved plan {plan_id} (status: {snapshot.status}) for user {request.user.email}")
    
    return Response(response_data, status=status.HTTP_200_OK)

