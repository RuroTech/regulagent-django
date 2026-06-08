"""
W-3 From PNA Serializers

Django REST Framework serializers for request/response validation
when pnaexchange calls the W-3 form generation endpoint.

Request: POST /api/w3/build-from-pna/
Response: 200 OK with W-3 form data
"""

from rest_framework import serializers
from typing import Any, Dict


class PNAEventSerializer(serializers.Serializer):
    """Serializer for a single pnaexchange event."""
    
    event_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="Event type ID (1-12) from pnaexchange FormContext (optional if event_type provided)"
    )
    
    event_type = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=500,
        help_text="Event type as text (e.g., 'Set Intermediate Plug') - NEW pnaexchange format"
    )
    
    display_text = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=500,
        help_text="Human-readable event name (e.g., 'Set Intermediate Plug')"
    )
    
    api_number = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=20,
        help_text="RRC API number (10-digit format, e.g., '42-501-70575' - will be normalized to 8-digit)"
    )
    
    event_detail = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
        help_text="Detailed event description from pnaexchange"
    )
    
    form_template_text = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Template with *_N_* placeholders"
    )
    
    input_values = serializers.DictField(
        required=False,
        help_text="Dictionary of input values keyed by placeholder position"
    )
    
    transformation_rules = serializers.DictField(
        required=False,
        help_text="Rules like {'jump_plugs_to_next_casing': true}"
    )
    
    date = serializers.DateField(
        required=True,
        help_text="Event date (ISO format: YYYY-MM-DD)"
    )
    
    start_time = serializers.TimeField(
        required=False,
        allow_null=True,
        help_text="Event start time (ISO format: HH:MM:SS)"
    )
    
    end_time = serializers.TimeField(
        required=False,
        allow_null=True,
        help_text="Event end time (ISO format: HH:MM:SS)"
    )
    
    work_assignment_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="Reference to pnaexchange work assignment"
    )
    
    dwr_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="Reference to DWR (Daily Work Report) record"
    )
    
    def validate(self, data):
        """Validate that required inputs are present for the event type."""
        from apps.public_core.services.w3_mapper import validate_event_inputs
        
        # Either event_id or event_type must be present
        event_id = data.get("event_id")
        event_type = data.get("event_type", "")
        display_text = data.get("display_text", "")
        
        if not event_id and not event_type:
            raise serializers.ValidationError("Either 'event_id' (numeric) or 'event_type' (text) must be provided")
        
        # Validate input values
        input_values = data.get("input_values", {})
        is_valid, error_msg = validate_event_inputs(event_id, input_values)
        
        # DEBUG: Print for Plug 5 (display_text contains "Plug 5")
        if "Plug 5" in display_text or "surface" in display_text.lower():
            print(f"🔴 PNAEventSerializer.validate: event_id={event_id}, event_type='{event_type}', display='{display_text[:50]}...', input_count={len(input_values)}, is_valid={is_valid}, error={error_msg}", flush=True)
        
        if not is_valid:
            raise serializers.ValidationError(error_msg)
        
        return data


class W3AReference(serializers.Serializer):
    """Reference to W-3A form (either database or PDF upload)."""
    
    type = serializers.ChoiceField(
        choices=["regulagent", "pdf", "auto"],
        required=True,
        help_text="'regulagent' to load from database, 'pdf' to upload/base64, 'auto' to auto-generate from RRC data"
    )
    
    w3a_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="W-3A form ID in RegulAgent database (if type='regulagent')"
    )
    
    w3a_file = serializers.FileField(
        required=False,
        allow_null=True,
        help_text="W-3A PDF file upload (if type='pdf' with multipart)"
    )
    
    w3a_file_base64 = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Base64-encoded PDF content (if type='pdf' with JSON request)"
    )
    
    w3a_filename = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=255,
        help_text="Original filename (optional, used with base64)"
    )
    
    def validate(self, data):
        """Validate that appropriate fields are present for the type."""
        ref_type = data.get("type")
        
        if ref_type == "regulagent" and not data.get("w3a_id"):
            raise serializers.ValidationError(
                "w3a_id is required when type='regulagent'"
            )
        
        if ref_type == "pdf":
            # Either w3a_file (multipart) or w3a_file_base64 (JSON) must be present
            has_file = bool(data.get("w3a_file"))
            has_base64 = bool(data.get("w3a_file_base64"))  # Non-empty string
            
            if not has_file and not has_base64:
                available_keys = list(data.keys())
                raise serializers.ValidationError(
                    f"Either 'w3a_file' (multipart) or 'w3a_file_base64' (JSON) is required when type='pdf'. Got keys: {available_keys}"
                )
        
        if ref_type == "auto":
            # Auto mode doesn't require any additional fields
            # The system will automatically generate W-3A from RRC data
            pass
        
        return data


class BuildW3FromPNARequestSerializer(serializers.Serializer):
    """Request payload for POST /api/w3/build-from-pna/"""

    subproject_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="Subproject ID (RegulAgent identifier for this W-3 run)"
    )

    dwr_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="Deprecated (was previously subproject ID). Provided for backwards compatibility."
    )

    api_number = serializers.CharField(
        required=True,
        max_length=20,
        help_text="RRC API number (10-digit format, e.g., '42-501-70575' - will be normalized to 8-digit and attached to all events)"
    )

    well_name = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=500,
        help_text="Well name/lease name"
    )

    w3a_reference = W3AReference(
        required=True,
        help_text="Reference to W-3A form"
    )

    pna_events = PNAEventSerializer(
        many=True,
        required=True,
        help_text="List of operational events from pnaexchange"
    )

    tenant_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="Multi-tenant support: tenant ID"
    )

    workspace_id = serializers.IntegerField(required=False, allow_null=True)
    
    def validate(self, data):
        """
        Ensure subproject_id is present (either via new field or legacy dwr_id)
        and normalize legacy payloads.
        """
        subproject_id = data.get("subproject_id")
        legacy_dwr_id = data.get("dwr_id")
        
        if subproject_id is None and legacy_dwr_id is None:
            raise serializers.ValidationError({
                "subproject_id": "This field is required.",
                "dwr_id": "Provide either 'subproject_id' or (legacy) 'dwr_id'."
            })
        
        # Normalize: always set subproject_id, keep legacy dwr_id for logging.
        data["subproject_id"] = subproject_id or legacy_dwr_id
        return data


class PlugRowSerializer(serializers.Serializer):
    """Single plug row in W-3 form output."""
    
    plug_number = serializers.IntegerField()
    depth_top_ft = serializers.FloatField(allow_null=True)
    depth_bottom_ft = serializers.FloatField(allow_null=True)
    type = serializers.CharField(max_length=50)
    cement_class = serializers.CharField(allow_null=True, max_length=10)
    sacks = serializers.FloatField(allow_null=True)
    slurry_weight_ppg = serializers.FloatField(allow_null=True, help_text="Weight of slurry (default 14.8 lbs/gal)")
    hole_size_in = serializers.FloatField(allow_null=True, help_text="Hole size at plug depth (from casing record)")
    top_of_plug_ft = serializers.FloatField(allow_null=True, help_text="TOC for RRC submission (measured or calculated)")
    measured_top_of_plug_ft = serializers.FloatField(allow_null=True, help_text="Measured TOC from pnaexchange 'Tag TOC' event")
    calculated_top_of_plug_ft = serializers.FloatField(allow_null=True, help_text="Calculated TOC from sacks and cement yield")
    toc_variance_ft = serializers.FloatField(allow_null=True, help_text="Difference between measured and calculated TOC")
    remarks = serializers.CharField(allow_blank=True)


class CasingRowSerializer(serializers.Serializer):
    """Single casing string in casing record output."""
    
    string_type = serializers.CharField(max_length=50)
    size_in = serializers.FloatField()
    weight_ppf = serializers.FloatField(allow_null=True)
    hole_size_in = serializers.FloatField(allow_null=True)
    top_ft = serializers.FloatField()
    bottom_ft = serializers.FloatField()
    shoe_depth_ft = serializers.FloatField(allow_null=True)
    cement_top_ft = serializers.FloatField(allow_null=True)
    removed_to_depth_ft = serializers.FloatField(allow_null=True)


class PerforationRowSerializer(serializers.Serializer):
    """Single perforation/open hole interval in output."""
    
    interval_top_ft = serializers.FloatField(allow_null=True)
    interval_bottom_ft = serializers.FloatField(allow_null=True)
    formation = serializers.CharField(allow_null=True, allow_blank=True)
    status = serializers.CharField(max_length=50)
    perforation_date = serializers.DateField(allow_null=True)


class W3FormHeaderSerializer(serializers.Serializer):
    """W-3 form header information."""
    
    api_number = serializers.CharField(max_length=20)
    well_name = serializers.CharField(allow_blank=True)
    operator = serializers.CharField(allow_blank=True)
    county = serializers.CharField(allow_blank=True)
    rrc_district = serializers.CharField(allow_blank=True)
    field = serializers.CharField(allow_blank=True)
    total_depth_ft = serializers.FloatField(allow_null=True)


class DUQWSerializer(serializers.Serializer):
    """Deepest Usable Quality Water information."""
    
    depth_ft = serializers.FloatField(allow_null=True)
    formation = serializers.CharField(allow_null=True, allow_blank=True)
    determination_method = serializers.CharField(allow_null=True, allow_blank=True)


class W3FormOutputSerializer(serializers.Serializer):
    """Complete W-3 form output ready for submission or storage."""
    
    header = W3FormHeaderSerializer()
    plugs = PlugRowSerializer(many=True)
    casing_record = CasingRowSerializer(many=True)
    perforations = PerforationRowSerializer(many=True)
    duqw = DUQWSerializer()
    remarks = serializers.CharField(allow_blank=True)
    pdf_url = serializers.CharField(allow_null=True, allow_blank=True, required=False)


class ValidationResultSerializer(serializers.Serializer):
    """Validation result information."""
    
    warnings = serializers.ListField(
        child=serializers.CharField(),
        help_text="Non-fatal warnings (e.g., missing optional fields)"
    )
    
    errors = serializers.ListField(
        child=serializers.CharField(),
        help_text="Fatal errors (if any)"
    )


class ExistingToolsSerializer(serializers.Serializer):
    """Existing mechanical tools from well history."""
    
    existing_mechanical_barriers = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of barrier types: CIBP, PACKER, DV_TOOL"
    )
    
    existing_cibp_ft = serializers.FloatField(
        required=False,
        allow_null=True,
        help_text="Depth of existing Cast Iron Bridge Plug"
    )
    
    existing_packer_ft = serializers.FloatField(
        required=False,
        allow_null=True,
        help_text="Depth of existing packer"
    )
    
    existing_dv_tool_ft = serializers.FloatField(
        required=False,
        allow_null=True,
        help_text="Depth of existing DV tool"
    )


class RetainerToolSerializer(serializers.Serializer):
    """Retainer tools (float collar, pup joint, straddle packer, retainer)."""
    
    tool_type = serializers.CharField(
        help_text="Tool type: float_collar, pup_joint, straddle_packer, retainer"
    )
    
    depth_ft = serializers.FloatField(
        help_text="Depth of tool in feet"
    )


class HistoricCementJobSerializer(serializers.Serializer):
    """Historic cement job from W-15 data."""
    
    job_type = serializers.CharField(
        required=False,
        allow_null=True,
        help_text="Type: surface, intermediate, production, plug, squeeze"
    )
    
    interval_top_ft = serializers.FloatField(
        required=False,
        allow_null=True,
        help_text="Top of cement interval"
    )
    
    interval_bottom_ft = serializers.FloatField(
        required=False,
        allow_null=True,
        help_text="Bottom of cement interval"
    )
    
    cement_top_ft = serializers.FloatField(
        required=False,
        allow_null=True,
        help_text="Depth where cement reaches"
    )
    
    sacks = serializers.FloatField(
        required=False,
        allow_null=True,
        help_text="Sacks of cement used"
    )
    
    slurry_density_ppg = serializers.FloatField(
        required=False,
        allow_null=True,
        help_text="Slurry density in pounds per gallon"
    )


class KOPSerializer(serializers.Serializer):
    """Kick-Off Point data for horizontal wells."""
    
    kop_md_ft = serializers.FloatField(
        required=False,
        allow_null=True,
        help_text="Measured depth of KOP"
    )
    
    kop_tvd_ft = serializers.FloatField(
        required=False,
        allow_null=True,
        help_text="True vertical depth of KOP"
    )


class W3AWellGeometrySerializer(serializers.Serializer):
    """Well geometry and historical data extracted from W-3A."""
    
    casing_record = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        help_text="Casing strings with sizes, depths, and removal info"
    )
    
    existing_tools = ExistingToolsSerializer(
        required=False,
        help_text="Existing mechanical barriers in wellbore"
    )
    
    retainer_tools = RetainerToolSerializer(
        many=True,
        required=False,
        help_text="Retainer tools (float collar, pup joint, etc)"
    )
    
    historic_cement_jobs = HistoricCementJobSerializer(
        many=True,
        required=False,
        help_text="Historic cement jobs from well history"
    )
    
    kop = KOPSerializer(
        required=False,
        allow_null=True,
        help_text="Kick-off point for horizontal wells"
    )


class MetadataSerializer(serializers.Serializer):
    """Metadata about the generated W-3 form."""
    
    api_number = serializers.CharField(max_length=20)
    subproject_id = serializers.IntegerField()
    dwr_id = serializers.IntegerField(required=False, allow_null=True)
    events_processed = serializers.IntegerField(help_text="Number of pnaexchange events processed")
    plugs_grouped = serializers.IntegerField(help_text="Number of plugs in final form")
    generated_at = serializers.DateTimeField(help_text="ISO format timestamp")


class BuildW3FromPNAResponseSerializer(serializers.Serializer):
    """Response payload for POST /api/w3/build-from-pna/"""
    
    success = serializers.BooleanField()
    
    w3_form = W3FormOutputSerializer(
        required=False,
        allow_null=True,
        help_text="Complete W-3 form (if success=true)"
    )
    
    w3a_well_geometry = W3AWellGeometrySerializer(
        required=False,
        allow_null=True,
        help_text="Well geometry and historical data from auto-generated W-3A (for plugged wellbore diagram)"
    )
    
    error = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Error message (if success=false)"
    )
    
    validation = ValidationResultSerializer()
    
    metadata = MetadataSerializer()


class W3SubmissionSerializer(serializers.Serializer):
    """Serializer for submitting final W-3 form to RRC."""
    
    w3_form = W3FormOutputSerializer(
        help_text="Complete W-3 form data"
    )
    
    operator_email = serializers.EmailField(
        help_text="Operator contact email for submission"
    )
    
    submission_notes = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional notes for RRC"
    )
    
    tentative = serializers.BooleanField(
        default=False,
        help_text="If true, saves as draft without submitting"
    )

