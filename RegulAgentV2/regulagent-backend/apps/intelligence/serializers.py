"""
DRF serializers for the intelligence app.
"""

from rest_framework import serializers

from .models import (
    FilingStatusRecord,
    PortalCredential,
    Recommendation,
    RecommendationInteraction,
    RejectionPattern,
    RejectionRecord,
)


class FilingStatusRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = FilingStatusRecord
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class FilingStatusCreateSerializer(serializers.Serializer):
    """For POST /api/intelligence/filing-status/ (automation callback)"""

    filing_id = serializers.CharField()
    form_type = serializers.CharField()
    agency = serializers.CharField()
    tenant_id = serializers.UUIDField()
    well_id = serializers.UUIDField()
    # Optional form FKs
    w3_form_id = serializers.UUIDField(required=False, allow_null=True)
    plan_snapshot_id = serializers.UUIDField(required=False, allow_null=True)
    c103_form_id = serializers.UUIDField(required=False, allow_null=True)
    state = serializers.CharField(required=False, default="")
    district = serializers.CharField(required=False, default="")
    county = serializers.CharField(required=False, default="")
    source = serializers.CharField(required=False, default="manual")


class RejectionRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = RejectionRecord
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class RejectionVerifySerializer(serializers.Serializer):
    """For PATCH /rejections/{id}/verify/"""

    parsed_issues = serializers.ListField(child=serializers.DictField())


class RecommendationSerializer(serializers.ModelSerializer):
    pattern_description = serializers.CharField(
        source="pattern.pattern_description", read_only=True, default=""
    )

    class Meta:
        model = Recommendation
        fields = "__all__"
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
            "times_shown",
            "times_accepted",
            "times_dismissed",
            "acceptance_rate",
        ]


class FieldCheckSerializer(serializers.Serializer):
    """For POST /recommendations/check-field/"""

    form_type = serializers.CharField()
    field_name = serializers.CharField()
    value = serializers.CharField()
    state = serializers.CharField(required=False, default="")
    district = serializers.CharField(required=False, default="")


class InteractionSerializer(serializers.Serializer):
    """For POST /recommendations/{id}/interact/"""

    action = serializers.ChoiceField(choices=["shown", "accepted", "dismissed", "snoozed"])
    field_value_at_time = serializers.CharField(required=False, default="")
    dismissal_reason = serializers.CharField(required=False, default="")


class TrendSerializer(serializers.ModelSerializer):
    class Meta:
        model = RejectionPattern
        fields = [
            "id",
            "form_type",
            "field_name",
            "issue_category",
            "state",
            "district",
            "agency",
            "pattern_description",
            "occurrence_count",
            "tenant_count",
            "rejection_rate",
            "is_trending",
            "trend_direction",
            "confidence",
            "first_observed",
            "last_observed",
        ]


class DashboardSerializer(serializers.Serializer):
    """For GET /dashboard/"""

    total_filings = serializers.IntegerField()
    total_rejections = serializers.IntegerField()
    approval_rate = serializers.FloatField()
    top_rejection_reasons = serializers.ListField(child=serializers.DictField())
    trending_patterns = TrendSerializer(many=True)
    recent_rejections = RejectionRecordSerializer(many=True)


class FilingSyncRequestSerializer(serializers.Serializer):
    """For POST /api/intelligence/filing-status/sync/"""
    agency = serializers.ChoiceField(
        choices=[('RRC', 'Texas RRC'), ('NMOCD', 'New Mexico OCD')],
        default='RRC',
    )


class PortalCredentialSerializer(serializers.ModelSerializer):
    """Read serializer — NEVER exposes passwords."""
    username = serializers.SerializerMethodField()

    class Meta:
        model = PortalCredential
        fields = ['id', 'agency', 'username', 'last_successful_login', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['id', 'last_successful_login', 'created_at', 'updated_at']

    def get_username(self, obj):
        """Return decrypted username (safe to show, unlike password)."""
        try:
            return obj.get_username()
        except Exception:
            return "***"


class CorrectionItemSerializer(serializers.Serializer):
    """Single accepted correction item."""
    issue_index = serializers.IntegerField(min_value=0)
    field_name = serializers.CharField()
    applied_value = serializers.CharField()


class RejectionApplyCorrectionsSerializer(serializers.Serializer):
    """For POST /rejections/{id}/apply-corrections/ — list of accepted corrections."""
    corrections = CorrectionItemSerializer(many=True)


class PortalCredentialCreateSerializer(serializers.Serializer):
    """For POST — accepts plaintext credentials, encrypts on save.

    vault_passphrase is optional but required when the tenant already has one set.
    On first use, supplying a passphrase registers it for the tenant.
    """
    agency = serializers.ChoiceField(choices=[('RRC', 'Texas RRC'), ('NMOCD', 'New Mexico OCD')])
    username = serializers.CharField(max_length=128)
    password = serializers.CharField(max_length=128, write_only=True)
    vault_passphrase = serializers.CharField(
        max_length=128,
        write_only=True,
        required=False,
        allow_blank=True,
        help_text="Vault passphrase required when the tenant has configured one (authorization gate)",
    )
