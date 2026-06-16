from rest_framework import serializers

from ..models import WellRegistry


class WellRegistrySerializer(serializers.ModelSerializer):
    workspace_name = serializers.CharField(source='workspace.name', read_only=True, allow_null=True)
    well_name = serializers.SerializerMethodField()

    class Meta:
        model = WellRegistry
        fields = [
            'id', 'api14', 'state', 'county', 'district', 'lat', 'lon',
            'operator_name', 'field_name', 'lease_name', 'well_number',
            'well_name', 'workspace', 'workspace_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def get_well_name(self, obj) -> str:
        lease = obj.lease_name or ""
        well_no = obj.well_number or ""
        if lease and well_no:
            return f"{lease} #{well_no}"
        if lease:
            return lease
        return ""


