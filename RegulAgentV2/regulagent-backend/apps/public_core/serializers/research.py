from rest_framework import serializers

from apps.public_core.models import ExtractedDocument, ResearchSession, ResearchMessage


class ResearchSessionCreateSerializer(serializers.Serializer):
    """Input serializer for creating a research session."""
    api_number = serializers.CharField(max_length=16)
    state = serializers.ChoiceField(choices=["TX", "NM", "UT"], required=False, default=None, allow_null=True)
    force_fetch = serializers.BooleanField(required=False, default=False)


class ResearchSessionSerializer(serializers.ModelSerializer):
    """Read serializer for ResearchSession."""
    progress_pct = serializers.IntegerField(read_only=True)
    lease_sibling_wells = serializers.SerializerMethodField()
    data_last_fetched = serializers.SerializerMethodField()

    class Meta:
        model = ResearchSession
        fields = [
            "id",
            "api_number",
            "state",
            "status",
            "well",
            "total_documents",
            "indexed_documents",
            "failed_documents",
            "error_message",
            "document_list",
            "celery_task_id",
            "progress_pct",
            "force_fetch",
            "created_at",
            "updated_at",
            "lease_sibling_wells",
            "data_last_fetched",
        ]
        read_only_fields = fields

    def get_lease_sibling_wells(self, obj):
        """Return other wells on the same lease (for cold-storage discovery)."""
        if not obj.well or not obj.well.lease_id:
            return []
        from apps.public_core.models import WellRegistry
        siblings = WellRegistry.objects.filter(
            lease_id=obj.well.lease_id
        ).exclude(
            api14=obj.well.api14
        ).values('api14', 'data_status', 'operator_name', 'lease_name', 'well_number')[:20]
        return list(siblings)

    def get_data_last_fetched(self, obj):
        """Return when well data was last fetched from source."""
        if not obj.well:
            return None
        # For TX: check NeubusLease.last_checked via well.lease_id
        if obj.well.lease_id:
            from apps.public_core.models.neubus_lease import NeubusLease
            lease = NeubusLease.objects.filter(lease_id=obj.well.lease_id).first()
            if lease and lease.last_checked:
                return lease.last_checked.isoformat()
        # Fallback: check WellRegistry updated_at or created_at
        if hasattr(obj.well, 'updated_at') and obj.well.updated_at:
            return obj.well.updated_at.isoformat()
        return None


class ResearchDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExtractedDocument
        fields = [
            "id", "document_type", "source_path", "neubus_filename",
            "source_page", "status", "model_tag", "created_at",
            "attribution_confidence", "attribution_method",
        ]


class ResearchMessageSerializer(serializers.ModelSerializer):
    """Read serializer for ResearchMessage."""

    class Meta:
        model = ResearchMessage
        fields = [
            "id",
            "role",
            "content",
            "citations",
            "metadata",
            "created_at",
        ]
        read_only_fields = fields


class ResearchAskSerializer(serializers.Serializer):
    """Input serializer for POST /sessions/{id}/ask/."""
    question = serializers.CharField(min_length=1, max_length=2000)
    top_k = serializers.IntegerField(min_value=1, max_value=30, required=False, default=15)


class BulkResearchSessionCreateSerializer(serializers.Serializer):
    """Input serializer for POST /api/research/sessions/bulk/."""
    api_numbers = serializers.ListField(
        child=serializers.CharField(max_length=25),
        min_length=1,
        max_length=50,
        error_messages={'max_length': 'Maximum 50 API numbers per bulk request.'},
    )
    state = serializers.ChoiceField(
        choices=["TX", "NM"],
        required=False,
        default=None,
        allow_null=True,
        help_text="Global state override for wells whose prefix is not 30/42.",
    )
