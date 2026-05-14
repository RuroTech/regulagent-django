"""
Serializers for unified well filings endpoint.

Handles W-3A plans, W-3 forms, and future form types.
"""

from rest_framework import serializers


class FilingMetadataSerializer(serializers.Serializer):
    """Base metadata for filings"""
    pass


class W3AFilingSerializer(serializers.Serializer):
    """W-3A Plan Snapshot serializer"""
    id = serializers.SerializerMethodField()
    form_type = serializers.SerializerMethodField()
    status = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.SerializerMethodField()
    metadata = serializers.SerializerMethodField()
    
    def get_id(self, obj):
        return str(obj.id)
    
    def get_form_type(self, obj):
        # policy_id is the most reliable indicator (set explicitly by orchestrator)
        if (obj.policy_id or "").startswith("nm."):
            return "C-103"
        payload = obj.payload or {}
        # NM orchestrator stores jurisdiction at payload top level
        if payload.get("jurisdiction") == "NM":
            return "C-103"
        # Fallback: well_header.state (TX orchestrator path)
        if ((payload.get("well_header") or {}).get("state", "")) == "NM":
            return "C-103"
        # Last resort: linked well's state
        if obj.well and (obj.well.state or "").upper() == "NM":
            return "C-103"
        return "W-3A"

    def get_updated_at(self, obj):
        # PlanSnapshot only has created_at, so we use that for updated_at
        return obj.created_at
    
    def get_metadata(self, obj):
        return {
            "plan_id": obj.plan_id,
            "kernel_version": obj.kernel_version,
            "visibility": obj.visibility,
            "kind": obj.kind,
        }


class W3FilingSerializer(serializers.Serializer):
    """W-3 Form ORM serializer"""
    id = serializers.SerializerMethodField()
    form_type = serializers.SerializerMethodField()
    status = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    metadata = serializers.SerializerMethodField()
    
    def get_id(self, obj):
        return str(obj.id)
    
    def get_form_type(self, obj):
        # Check linked wizard session jurisdiction first
        session = getattr(obj, "w3_wizard_sessions", None)
        if session is not None:
            try:
                ws = session.first()
                if ws and getattr(ws, "jurisdiction", "") == "NM":
                    return "Sundry"
            except Exception:
                pass
        # Fall back to state in form_data header
        state = ((obj.form_data or {}).get("header") or {}).get("state", "")
        if state == "NM":
            return "Sundry"
        # Fall back to API number prefix (NM APIs start with "30-0")
        api = getattr(obj, "api_number", "") or ""
        if api.startswith("30-0") or api.startswith("300"):
            return "Sundry"
        return "W-3"

    def get_metadata(self, obj):
        w3_events_count = 0
        if hasattr(obj, 'w3_events'):
            try:
                w3_events_count = obj.w3_events.count()
            except Exception:
                w3_events_count = 0

        # Find linked wizard session for navigation
        session_id = None
        pdf_url = None
        try:
            ws = obj.w3_wizard_sessions.first()
            if ws:
                session_id = str(ws.id)
                gen_result = ws.w3_generation_result or {}
                pdf_url = gen_result.get("pdf_url")
        except Exception:
            pass

        return {
            "submitted_by": obj.submitted_by,
            "submitted_at": obj.submitted_at.isoformat() if obj.submitted_at else None,
            "rrc_confirmation_number": obj.rrc_confirmation_number,
            "events_count": w3_events_count,
            "session_id": session_id,
            "pdf_url": pdf_url,
        }


class WellFilingsResponseSerializer(serializers.Serializer):
    """Unified filings response"""
    api14 = serializers.CharField()
    total = serializers.IntegerField()
    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    filings = serializers.ListField(child=serializers.JSONField())

