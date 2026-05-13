from rest_framework import serializers
from .models import PolicyRule, PolicySection, DistrictOverlay, CountyOverlay


class PolicyRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = PolicyRule
        fields = (
            'id', 'rule_id', 'citation', 'title', 'source_urls', 'jurisdiction', 'doc_type', 'topic',
            'version_tag', 'effective_from', 'effective_to', 'html_sha256',
            'created_at', 'updated_at'
        )


class PolicySectionSerializer(serializers.ModelSerializer):
    rule = PolicyRuleSerializer(read_only=True)

    class Meta:
        model = PolicySection
        fields = (
            'id', 'rule', 'version_tag', 'path', 'heading', 'text', 'anchor', 'order_idx', 'created_at'
        )


class DistrictOverlaySerializer(serializers.ModelSerializer):
    class Meta:
        model = DistrictOverlay
        fields = [
            'id', 'jurisdiction', 'district_code', 'source_file',
            'requirements', 'preferences', 'plugging_chart', 'imported_at',
        ]


class CountyOverlaySerializer(serializers.ModelSerializer):
    class Meta:
        model = CountyOverlay
        fields = [
            'id', 'county_name', 'requirements', 'preferences',
            'county_procedures', 'formation_data',
        ]


