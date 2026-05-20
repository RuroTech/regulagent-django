"""
Well component API endpoints.

Provides tenant-scoped read and write access to WellComponent records.
Tenants can:
  - List resolved components for a well (GET)
  - Add a tenant-layer component (POST)
  - Soft-delete a tenant-layer component (DELETE)
"""

import logging

from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.public_core.models import WellRegistry, WellComponent
from apps.public_core.services.component_resolver import (
    resolve_well_components,
    build_well_geometry_from_components,
)
from apps.tenant_overlay.services.engagement_tracker import track_well_interaction
from apps.tenant_overlay.views.tenant_wells import get_tenant_id_from_request

logger = logging.getLogger(__name__)


def _serialize_component(c, effective_layer=None) -> dict:
    """Serialize a WellComponent to the response shape."""
    return {
        "id": str(c.id),
        "component_type": c.component_type,
        "layer": effective_layer or c.layer,
        "lifecycle_state": c.lifecycle_state,
        "top_ft": float(c.top_ft) if c.top_ft is not None else None,
        "bottom_ft": float(c.bottom_ft) if c.bottom_ft is not None else None,
        "outside_dia_in": float(c.outside_dia_in) if c.outside_dia_in is not None else None,
        "weight_ppf": float(c.weight_ppf) if c.weight_ppf is not None else None,
        "grade": c.grade or "",
        "cement_top_ft": float(c.cement_top_ft) if c.cement_top_ft is not None else None,
        "hole_size_in": float(c.hole_size_in) if c.hole_size_in is not None else None,
        "cement_class": c.cement_class or "",
        "sacks": float(c.sacks) if c.sacks is not None else None,
        "properties": c.properties or {},
        "source_document_type": c.source_document_type or "",
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@api_view(["GET", "POST"])
@authentication_classes([JWTAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def well_components_view(request, api14):
    """
    GET  /api/tenant/wells/<api14>/components/ — List resolved components for a well.
    POST /api/tenant/wells/<api14>/components/ — Add a tenant-layer component.
    """
    if request.method == "GET":
        return _list_well_components(request, api14)
    return _add_well_component(request, api14)


def _list_well_components(request, api14):
    tenant_id = get_tenant_id_from_request(request)
    if not tenant_id:
        return Response(
            {"error": "No tenant associated with user"},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        well = WellRegistry.objects.get(api14=api14)
    except WellRegistry.DoesNotExist:
        return Response(
            {"error": f"Well with API {api14} not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    resolved = resolve_well_components(well, tenant_id=tenant_id)
    geometry = build_well_geometry_from_components(well, tenant_id=tenant_id)

    components = [_serialize_component(rc.component, rc.effective_layer) for rc in resolved]

    return Response(
        {
            "api14": api14,
            "total_components": len(components),
            "components": components,
            "geometry": geometry,
        },
        status=status.HTTP_200_OK,
    )


def _add_well_component(request, api14):
    tenant_id = get_tenant_id_from_request(request)
    if not tenant_id:
        return Response(
            {"error": "No tenant associated with user"},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        well = WellRegistry.objects.get(api14=api14)
    except WellRegistry.DoesNotExist:
        return Response(
            {"error": f"Well with API {api14} not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    data = request.data
    component_type = data.get("component_type")
    valid_types = [ct.value for ct in WellComponent.ComponentType]
    if not component_type or component_type not in valid_types:
        return Response(
            {"error": f"Invalid or missing component_type. Must be one of: {valid_types}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    component = WellComponent.objects.create(
        well=well,
        component_type=component_type,
        layer=WellComponent.Layer.TENANT,
        tenant_id=tenant_id,
        lifecycle_state=WellComponent.LifecycleState.INSTALLED,
        top_ft=data.get("top_ft"),
        bottom_ft=data.get("bottom_ft"),
        outside_dia_in=data.get("outside_dia_in"),
        weight_ppf=data.get("weight_ppf"),
        grade=data.get("grade", ""),
        cement_top_ft=data.get("cement_top_ft"),
        hole_size_in=data.get("hole_size_in"),
        cement_class=data.get("cement_class", ""),
        sacks=data.get("sacks"),
        properties=data.get("properties", {}),
    )

    track_well_interaction(
        tenant_id=tenant_id,
        well=well,
        interaction_type="component_added",
        user=request.user,
        metadata_update={"component_id": str(component.id), "component_type": component_type},
    )

    logger.info(
        "Tenant %s added component %s (%s) to well %s",
        tenant_id,
        component.id,
        component_type,
        api14,
    )

    return Response(_serialize_component(component), status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
@authentication_classes([JWTAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def delete_well_component_view(request, api14, component_id):
    """
    DELETE /api/tenant/wells/<api14>/components/<uuid:component_id>/

    Soft-delete a tenant-layer component (sets is_archived=True).
    Only the owning tenant may delete their own components.
    """
    tenant_id = get_tenant_id_from_request(request)
    if not tenant_id:
        return Response(
            {"error": "No tenant associated with user"},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        component = WellComponent.objects.get(
            id=component_id,
            well__api14=api14,
        )
    except WellComponent.DoesNotExist:
        return Response(
            {"error": "Component not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    if component.layer != WellComponent.Layer.TENANT:
        return Response(
            {"error": "Only tenant-layer components can be deleted"},
            status=status.HTTP_403_FORBIDDEN,
        )

    if component.tenant_id != tenant_id:
        return Response(
            {"error": "Component does not belong to your tenant"},
            status=status.HTTP_403_FORBIDDEN,
        )

    component.is_archived = True
    component.save(update_fields=["is_archived", "updated_at"])

    logger.info(
        "Tenant %s archived component %s (%s) on well %s",
        tenant_id,
        component_id,
        component.component_type,
        api14,
    )

    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['GET'])
@authentication_classes([JWTAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def well_wbd_sync_view(request, api14: str):
    """
    GET /api/tenant/wells/<api14>/wbd-sync/

    Return aggregated casing and plug data from all successfully extracted
    documents for this well (tenant uploads + research pipeline docs).
    Response shape matches WellSummary from /api/research/sessions/{id}/summary/.
    """
    from apps.public_core.models import ExtractedDocument

    try:
        well = WellRegistry.objects.get(api14=api14)
    except WellRegistry.DoesNotExist:
        return Response({"detail": "Well not found."}, status=404)

    # All successfully extracted documents for this well, any source
    eds = ExtractedDocument.objects.filter(
        well=well,
        status="success",
    ).order_by("-created_at")

    def _safe_dict(val):
        return val if isinstance(val, dict) else {}

    # ------------------------------------------------------------------
    # Well info — from header field (present in W-3A uploads)
    # ------------------------------------------------------------------
    well_info: dict = {}
    for ed in eds:
        data = ed.json_data or {}
        header = _safe_dict(data.get("header"))
        if not well_info.get("field") and header.get("field"):
            well_info["field"] = header.get("field", "")
        if not well_info.get("county") and header.get("county"):
            well_info["county"] = header.get("county", "")
        if not well_info.get("operator") and header.get("operator"):
            well_info["operator"] = header.get("operator", "")
        if not well_info.get("lease") and header.get("well_name"):
            well_info["lease"] = header.get("well_name", "")
        if not well_info.get("total_depth_ft") and header.get("total_depth_ft"):
            well_info["total_depth_ft"] = header.get("total_depth_ft")

    # Fall back to WellRegistry fields for anything still missing
    if not well_info.get("operator") and well.operator_name:
        well_info["operator"] = well.operator_name
    if not well_info.get("county") and well.county:
        well_info["county"] = well.county

    # ------------------------------------------------------------------
    # Casing records — from casing_record field (W-3A, W-3, W-2)
    # Deduplicated by size_in
    # ------------------------------------------------------------------
    casing_records: list = []
    seen_sizes: dict = {}

    for ed in eds:
        data = ed.json_data or {}
        date_filed = _safe_dict(data.get("header")).get("date_filed", "")
        for c in (data.get("casing_record") or []):
            casing_type = c.get("string_type") or c.get("string") or None
            size = c.get("size_in")
            if not size:
                continue
            record = {**c}
            if "string" in record and "string_type" not in record:
                record["string_type"] = record.pop("string")
            if "weight_per_ft" in record and "weight_ppf" not in record:
                record["weight_ppf"] = record.pop("weight_per_ft")
            record["source_doc_type"] = ed.document_type
            record["source_date"] = date_filed
            if size not in seen_sizes:
                seen_sizes[size] = len(casing_records)
                casing_records.append(record)
            elif casing_type and not casing_records[seen_sizes[size]].get("string_type"):
                casing_records[seen_sizes[size]] = record

    # ------------------------------------------------------------------
    # Plug records — check both 'plugging_proposal' (W-3A) and 'plug_record' (W-3)
    # ------------------------------------------------------------------
    plug_records: list = []
    seen_plugs: set = set()

    for ed in eds:
        data = ed.json_data or {}
        date_filed = _safe_dict(data.get("header")).get("date_filed", "")
        # W-3A uses 'plugging_proposal'; W-3 uses 'plug_record'
        raw_plugs = data.get("plugging_proposal") or data.get("plug_record") or []
        for p in raw_plugs:
            top = p.get("depth_top_ft")
            bottom = p.get("depth_bottom_ft")
            if isinstance(top, str) and top.lower() == "surface":
                top = 0
            if isinstance(bottom, str) and bottom.lower() == "surface":
                bottom = 0
            if top is not None and bottom is not None:
                try:
                    if float(top) > float(bottom):
                        top, bottom = bottom, top
                except (ValueError, TypeError):
                    pass
            key = (top, bottom, p.get("sacks"))
            if key not in seen_plugs and top is not None:
                seen_plugs.add(key)
                record = {**p}
                record["depth_top_ft"] = top
                record["depth_bottom_ft"] = bottom
                # Normalize plug_type: W-3A uses 'type' field
                if "plug_type" not in record and "type" in record:
                    record["plug_type"] = record.get("type")
                record["source_doc_type"] = ed.document_type
                record["source_date"] = date_filed
                plug_records.append(record)

    # Sort plugs by depth (shallowest first)
    plug_records.sort(key=lambda p: p.get("depth_top_ft") or 0)

    # ------------------------------------------------------------------
    # Tubing records — from tubing_record field
    # ------------------------------------------------------------------
    tubing_records: list[dict] = []
    seen_tubing: set = set()
    for ed in eds:
        data = ed.json_data or {}
        for t in (data.get("tubing_record") or []):
            size = t.get("size_in")
            bottom = t.get("bottom_ft")
            key = (size, bottom)
            if key not in seen_tubing and size is not None:
                seen_tubing.add(key)
                tubing_records.append({
                    "size_in": size,
                    "top_ft": t.get("top_ft"),
                    "bottom_ft": bottom,
                    "source_doc_type": ed.document_type,
                })

    # ------------------------------------------------------------------
    # Perforation records — from producing_injection_disposal_interval
    # ------------------------------------------------------------------
    perf_records: list[dict] = []
    seen_perfs: set = set()
    for ed in eds:
        data = ed.json_data or {}
        for p in (data.get("producing_injection_disposal_interval") or []):
            top = p.get("from_ft")
            bottom = p.get("to_ft")
            key = (top, bottom)
            if key not in seen_perfs and top is not None:
                seen_perfs.add(key)
                perf_records.append({
                    "top_ft": top,
                    "bottom_ft": bottom,
                    "source_doc_type": ed.document_type,
                })

    # ------------------------------------------------------------------
    # Cement job records — from cementing_data (W-15)
    # ------------------------------------------------------------------
    cement_job_records: list[dict] = []
    seen_cement: set = set()
    for ed in eds:
        data = ed.json_data or {}
        for j in (data.get("cementing_data") or []):
            bottom = j.get("interval_bottom_ft")
            sacks = j.get("sacks")
            key = (j.get("job"), bottom)
            if key not in seen_cement and bottom is not None:
                seen_cement.add(key)
                cement_job_records.append({
                    "job_type": j.get("job", ""),
                    "interval_top_ft": j.get("interval_top_ft"),
                    "interval_bottom_ft": bottom,
                    "sacks": sacks,
                    "cement_top_ft": j.get("cement_top_ft"),
                    "source_doc_type": ed.document_type,
                })

    # ------------------------------------------------------------------
    # Tool records — from mechanical_equipment (W-15)
    # ------------------------------------------------------------------
    tool_records: list[dict] = []
    seen_tools: set = set()
    for ed in eds:
        data = ed.json_data or {}
        for t in (data.get("mechanical_equipment") or []):
            depth = t.get("depth_ft")
            equip_type = t.get("equipment_type", "")
            key = (equip_type, depth)
            if key not in seen_tools and depth is not None:
                seen_tools.add(key)
                tool_records.append({
                    "tool_type": equip_type,
                    "depth_ft": depth,
                    "size_in": t.get("size_in"),
                    "source_doc_type": ed.document_type,
                })

    return Response({
        "api_number": api14,
        "state": well.state or "",
        "well_info": well_info,
        "casing_records": casing_records[:50],
        "plug_records": plug_records[:50],
        "tubing_records": tubing_records[:20],
        "perf_records": perf_records[:20],
        "cement_job_records": cement_job_records[:20],
        "tool_records": tool_records[:20],
        "document_counts": {"total": eds.count()},
        "filing_timeline": [],
    })
