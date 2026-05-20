from rest_framework import serializers
from .models import ClientWorkspace, Notification, Tenant, UsageRecord, User, WorkspaceMembership


class ClientWorkspaceSerializer(serializers.ModelSerializer):
    """
    Serializer for ClientWorkspace model with tenant context.
    """
    tenant_slug = serializers.CharField(source='tenant.slug', read_only=True)
    well_count = serializers.SerializerMethodField()
    filing_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = ClientWorkspace
        fields = [
            'id', 'tenant', 'tenant_slug', 'name', 'operator_number',
            'description', 'is_active', 'created_at', 'updated_at', 'well_count',
            'filing_count'
        ]
        read_only_fields = ['tenant', 'created_at', 'updated_at']

    def get_well_count(self, obj):
        """Return count of wells with work products in this workspace."""
        from apps.public_core.models import PlanSnapshot, W3FormORM
        plan_wells = set(PlanSnapshot.objects.filter(workspace=obj).values_list('well_id', flat=True))
        w3_wells = set(W3FormORM.objects.filter(workspace=obj).values_list('well_id', flat=True))
        return len(plan_wells | w3_wells)


class ClientWorkspaceCreateSerializer(serializers.ModelSerializer):
    """
    Serializer for creating/updating ClientWorkspace.
    Tenant is automatically assigned from request context.
    """
    class Meta:
        model = ClientWorkspace
        fields = ['name', 'operator_number', 'description', 'is_active']

    def validate_name(self, value):
        """Ensure workspace name is unique within tenant."""
        tenant = self.context.get('tenant')
        if tenant:
            # Check if name already exists for this tenant (excluding self on update)
            existing = ClientWorkspace.objects.filter(tenant=tenant, name=value)
            if self.instance:
                existing = existing.exclude(pk=self.instance.pk)
            if existing.exists():
                raise serializers.ValidationError(
                    f"A workspace named '{value}' already exists for this tenant."
                )
        return value


class UserListSerializer(serializers.ModelSerializer):
    """
    Read-only serializer for listing tenant users.
    """
    is_tenant_admin = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "email", "first_name", "last_name", "title", "is_active", "is_tenant_admin"]
        read_only_fields = ["id", "email", "first_name", "last_name", "title", "is_active", "is_tenant_admin"]

    def get_is_tenant_admin(self, obj):
        # When annotated by the list view, use the annotation directly
        if hasattr(obj, 'is_tenant_admin'):
            return bool(obj.is_tenant_admin)
        return False


class UserCreateSerializer(serializers.Serializer):
    """
    Serializer for creating a new tenant user.
    Email is required; all other fields are optional.
    """
    email = serializers.EmailField(required=True)
    first_name = serializers.CharField(required=False, allow_blank=True, default="")
    last_name = serializers.CharField(required=False, allow_blank=True, default="")
    title = serializers.CharField(required=False, allow_blank=True, allow_null=True, default=None)


class WorkspaceMembershipSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = WorkspaceMembership
        fields = ['id', 'workspace', 'user', 'user_email', 'user_name', 'created_at']
        read_only_fields = ['created_at', 'workspace']

    def get_user_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip() or obj.user.email


class NotificationSerializer(serializers.ModelSerializer):
    """
    Serializer for the Notification model.
    Read-mostly — supports list, retrieve, and action responses.
    """
    class Meta:
        model = Notification
        fields = [
            'id', 'verb', 'message', 'notif_type', 'action_url',
            'read', 'read_at', 'created_at',
        ]
        read_only_fields = [
            'id', 'verb', 'message', 'notif_type', 'action_url',
            'read', 'read_at', 'created_at',
        ]


class UsageRecordSerializer(serializers.ModelSerializer):
    """
    Serializer for UsageRecord model with related data.
    """
    tenant_slug = serializers.CharField(source='tenant.slug', read_only=True)
    workspace_name = serializers.CharField(source='workspace.name', read_only=True, allow_null=True)
    user_email = serializers.EmailField(source='user.email', read_only=True, allow_null=True)

    class Meta:
        model = UsageRecord
        fields = [
            'id', 'tenant', 'tenant_slug', 'workspace', 'workspace_name',
            'user', 'user_email', 'event_type', 'resource_type', 'resource_id',
            'tokens_used', 'processing_time_ms', 'metadata', 'created_at'
        ]
        read_only_fields = ['tenant', 'created_at']
