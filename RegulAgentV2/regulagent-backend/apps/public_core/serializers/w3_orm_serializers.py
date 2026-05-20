"""
DRF Serializers for W-3 ORM models.

Serializers for W3EventORM, W3PlugORM, and W3FormORM with support for:
- Nested relationships
- Read-only history fields
- List and detail views
- Create/update operations
"""

from __future__ import annotations

from rest_framework import serializers
from datetime import time

from apps.public_core.models import W3EventORM, W3PlugORM, W3FormORM, WellRegistry


class W3EventORM_ListSerializer(serializers.ModelSerializer):
    """Minimal serializer for listing W3 events."""
    
    event_type_display = serializers.CharField(source='get_event_type_display', read_only=True)
    cement_class_display = serializers.CharField(source='get_cement_class_display', read_only=True)
    
    class Meta:
        model = W3EventORM
        fields = [
            'id',
            'api_number',
            'event_type',
            'event_type_display',
            'event_date',
            'depth_top_ft',
            'depth_bottom_ft',
            'cement_class',
            'cement_class_display',
            'sacks',
            'plug_number',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class W3EventORM_DetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for individual W3 events."""
    
    event_type_display = serializers.CharField(source='get_event_type_display', read_only=True)
    cement_class_display = serializers.CharField(source='get_cement_class_display', read_only=True)
    well_details = serializers.SerializerMethodField(read_only=True)
    
    class Meta:
        model = W3EventORM
        fields = [
            'id',
            'well',
            'well_details',
            'api_number',
            'event_type',
            'event_type_display',
            'event_date',
            'event_start_time',
            'event_end_time',
            'depth_top_ft',
            'depth_bottom_ft',
            'perf_depth_ft',
            'tagged_depth_ft',
            'cement_class',
            'cement_class_display',
            'sacks',
            'volume_bbl',
            'pressure_psi',
            'plug_number',
            'raw_event_detail',
            'work_assignment_id',
            'dwr_id',
            'jump_to_next_casing',
            'casing_string',
            'raw_input_values',
            'raw_transformation_rules',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_well_details(self, obj):
        """Return minimal well info."""
        if obj.well:
            return {
                'id': obj.well.id,
                'api_number': obj.well.api_number,
                'well_name': obj.well.well_name,
            }
        return None


class W3EventORM_CreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating W3 events."""
    
    class Meta:
        model = W3EventORM
        fields = [
            'well',
            'api_number',
            'event_type',
            'event_date',
            'event_start_time',
            'event_end_time',
            'depth_top_ft',
            'depth_bottom_ft',
            'perf_depth_ft',
            'tagged_depth_ft',
            'cement_class',
            'sacks',
            'volume_bbl',
            'pressure_psi',
            'plug_number',
            'raw_event_detail',
            'work_assignment_id',
            'dwr_id',
            'jump_to_next_casing',
            'casing_string',
            'raw_input_values',
            'raw_transformation_rules',
        ]


class W3PlugORM_ListSerializer(serializers.ModelSerializer):
    """Minimal serializer for listing W3 plugs."""
    
    plug_type_display = serializers.CharField(source='get_plug_type_display', read_only=True)
    operation_type_display = serializers.CharField(source='get_operation_type_display', read_only=True)
    cement_class_display = serializers.CharField(source='get_cement_class_display', read_only=True)
    event_count = serializers.SerializerMethodField(read_only=True)
    
    class Meta:
        model = W3PlugORM
        fields = [
            'id',
            'api_number',
            'plug_number',
            'plug_type',
            'plug_type_display',
            'operation_type',
            'operation_type_display',
            'depth_top_ft',
            'depth_bottom_ft',
            'cement_class',
            'cement_class_display',
            'sacks',
            'slurry_weight_ppg',
            'event_count',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']
    
    def get_event_count(self, obj):
        """Return count of events in this plug."""
        return obj.events.count()


class W3PlugORM_DetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for individual W3 plugs with all events."""
    
    plug_type_display = serializers.CharField(source='get_plug_type_display', read_only=True)
    operation_type_display = serializers.CharField(source='get_operation_type_display', read_only=True)
    cement_class_display = serializers.CharField(source='get_cement_class_display', read_only=True)
    well_details = serializers.SerializerMethodField(read_only=True)
    events = W3EventORM_ListSerializer(many=True, read_only=True)
    
    class Meta:
        model = W3PlugORM
        fields = [
            'id',
            'well',
            'well_details',
            'api_number',
            'plug_number',
            'plug_type',
            'plug_type_display',
            'operation_type',
            'operation_type_display',
            'depth_top_ft',
            'depth_bottom_ft',
            'cement_class',
            'cement_class_display',
            'sacks',
            'volume_bbl',
            'slurry_weight_ppg',
            'hole_size_in',
            'calculated_top_of_plug_ft',
            'measured_top_of_plug_ft',
            'toc_variance_ft',
            'remarks',
            'events',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_well_details(self, obj):
        """Return minimal well info."""
        if obj.well:
            return {
                'id': obj.well.id,
                'api_number': obj.well.api_number,
                'well_name': obj.well.well_name,
            }
        return None


class W3PlugORM_CreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating W3 plugs."""
    
    class Meta:
        model = W3PlugORM
        fields = [
            'well',
            'api_number',
            'plug_number',
            'plug_type',
            'operation_type',
            'depth_top_ft',
            'depth_bottom_ft',
            'cement_class',
            'sacks',
            'volume_bbl',
            'slurry_weight_ppg',
            'hole_size_in',
            'calculated_top_of_plug_ft',
            'measured_top_of_plug_ft',
            'toc_variance_ft',
            'remarks',
        ]


class W3FormORM_ListSerializer(serializers.ModelSerializer):
    """Minimal serializer for listing W3 forms."""
    
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    plug_count = serializers.SerializerMethodField(read_only=True)
    
    class Meta:
        model = W3FormORM
        fields = [
            'id',
            'api_number',
            'status',
            'status_display',
            'plug_count',
            'auto_generated',
            'submitted_at',
            'submitted_by',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at', 'submitted_at']
    
    def get_plug_count(self, obj):
        """Return count of plugs in this form."""
        return obj.plugs.count()


class W3FormORM_DetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for individual W3 forms with all plugs and geometry."""
    
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    well_details = serializers.SerializerMethodField(read_only=True)
    plugs = W3PlugORM_ListSerializer(many=True, read_only=True)
    
    class Meta:
        model = W3FormORM
        fields = [
            'id',
            'well',
            'well_details',
            'api_number',
            'status',
            'status_display',
            'form_data',
            'well_geometry',
            'rrc_export',
            'validation_warnings',
            'validation_errors',
            'submitted_at',
            'submitted_by',
            'rrc_confirmation_number',
            'generated_from_w3a_snapshot',
            'auto_generated',
            'plugs',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'submitted_at']
    
    def get_well_details(self, obj):
        """Return well info."""
        if obj.well:
            return {
                'id': obj.well.id,
                'api_number': obj.well.api_number,
                'well_name': obj.well.well_name,
                'county': obj.well.county,
                'state': obj.well.state,
            }
        return None


class W3FormORM_CreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating W3 forms."""

    def validate_workspace(self, value):
        """Workspace must be active and belong to the same tenant as the form."""
        if not value.is_active:
            raise serializers.ValidationError("Cannot assign filing to an inactive workspace.")
        # Cross-tenant check: on update, compare new workspace's tenant against
        # the existing form's current workspace tenant (same integer FK type).
        # On create, fall back to the request user's first tenant.
        if self.instance is not None:
            # Update path: form already has a workspace; new workspace must share the same tenant.
            current_workspace = self.instance.workspace
            if current_workspace is not None and value.tenant_id != current_workspace.tenant_id:
                raise serializers.ValidationError("Workspace must belong to the current tenant.")
        else:
            # Create path: derive expected tenant from the authenticated user.
            request = self.context.get('request')
            if request and request.user.is_authenticated:
                user_tenant = request.user.tenants.first()
                if user_tenant and value.tenant_id != user_tenant.id:
                    raise serializers.ValidationError("Workspace must belong to the current tenant.")
        return value

    class Meta:
        model = W3FormORM
        fields = [
            'well',
            'api_number',
            'status',
            'form_data',
            'well_geometry',
            'rrc_export',
            'validation_warnings',
            'validation_errors',
            'generated_from_w3a_snapshot',
            'auto_generated',
            'workspace',
        ]


class W3FormORM_SubmitSerializer(serializers.Serializer):
    """Serializer for submitting a W3 form to RRC."""
    
    submitted_by = serializers.CharField(max_length=255)
    rrc_confirmation_number = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True
    )
    
    def update(self, instance, validated_data):
        """Update W3Form with submission info."""
        instance.mark_submitted(
            submitted_by=validated_data['submitted_by'],
            rrc_confirmation_number=validated_data.get('rrc_confirmation_number')
        )
        return instance

