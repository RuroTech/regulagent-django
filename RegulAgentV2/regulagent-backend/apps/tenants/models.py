from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from django_tenants.models import TenantMixin, DomainMixin
from django_tenants.utils import get_public_schema_name, schema_context
from tenant_users.tenants.models import TenantBase, tenant_user_added, UserProfileManager
from tenant_users.tenants.models import UserProfile as TenantUser


class TenantSchemaAwareUserManager(UserProfileManager):
    """
    Custom user manager that ensures create_user() always runs in the public
    schema context, regardless of which schema the caller is currently in.

    django-tenant-users raises SchemaError when create_user is called while
    connection.schema_name != 'public'.  Test helpers and some view code call
    create_user from inside a tenant schema context, so we transparently
    switch to the public schema first.
    """

    def create_user(self, email=None, password=None, **extra_fields):
        with schema_context(get_public_schema_name()):
            return super().create_user(email=email, password=password, **extra_fields)

    def create_superuser(self, email=None, password=None, **extra_fields):
        with schema_context(get_public_schema_name()):
            return super().create_superuser(email=email, password=password, **extra_fields)


class User(TenantUser):
    """
    Custom user model that extends TenantUser from django-tenant-users.
    This model supports multi-tenancy with tenant-scoped permissions.
    """
    objects = TenantSchemaAwareUserManager()

    # Add any custom fields here if needed
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    title = models.CharField(max_length=150, blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    organization = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'

    def __str__(self) -> str:
        return self.email if self.email else self.username


class Tenant(TenantBase):
    """
    Tenant model with multi-tenant support.
    Each tenant has its own PostgreSQL schema and isolated data.
    """
    # Override the owner FK from TenantBase to allow null (supports test fixtures
    # and admin-created tenants that don't yet have an owner assigned).
    owner = models.ForeignKey(
        "tenants.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=64, unique=True)
    created_on = models.DateTimeField(auto_now_add=True)

    # Vault passphrase hash (Argon2/PBKDF2 via Django's make_password) — authorization gate
    # for credential management. The actual encryption uses a per-tenant key derived from
    # tenant.id + ENCRYPTION_PEPPER, so background sync works without user intervention.
    vault_passphrase_hash = models.CharField(
        max_length=255,
        blank=True,
        help_text="Hashed vault passphrase for credential management authorization",
    )

    # Required by TenantMixin
    auto_create_schema = True
    auto_drop_schema = True  # Allows schema deletion when tenant is deleted

    class Meta:
        verbose_name = 'Tenant'
        verbose_name_plural = 'Tenants'

    def __str__(self) -> str:
        return f"Tenant<{self.slug}>"

    @transaction.atomic
    def add_user(self, user_obj, *, is_superuser: bool = False, is_staff: bool = False) -> None:
        """
        Add user to this tenant.

        Extends TenantBase.add_user to handle the case where a
        UserTenantPermissions row already exists for the user (created
        when they were initially linked to the public tenant via
        User.objects.create_user()).  In that case we reuse the existing
        permissions row and simply update the staff/superuser flags, then
        add the M2M tenant link — mirroring the rest of the base logic
        without hitting the unique-profile constraint.

        When adding to an org (non-public) tenant, the public tenant M2M link
        is removed so that request.user.tenants.first() reliably resolves to
        the org tenant rather than the public tenant.  This mirrors the real
        production onboarding flow where users belong exclusively to their org.
        """
        from tenant_users.permissions.models import UserTenantPermissions
        from tenant_users.tenants.models import ExistsError

        # Guard: already linked to this tenant
        if self.user_set.filter(pk=user_obj.pk).exists():
            raise ExistsError(f"User already added to tenant: {user_obj}")

        perms, created = UserTenantPermissions.objects.get_or_create(
            profile=user_obj,
            defaults={"is_staff": is_staff, "is_superuser": is_superuser},
        )
        if not created:
            # Row already exists (e.g. user was linked to public tenant by
            # create_user); only update flags when we are not assigning to
            # the public tenant so we don't accidentally downgrade permissions.
            if self.schema_name != get_public_schema_name():
                perms.is_staff = is_staff
                perms.is_superuser = is_superuser
                perms.save(update_fields=["is_staff", "is_superuser"])

        # When adding to an org tenant, remove the auto-added public-tenant
        # M2M link so user.tenants.first() returns the org tenant, not public.
        # This matches the production onboarding flow (one org per user).
        # Use the through table directly to avoid schema_required decorators on
        # TenantBase.remove_user().
        if self.schema_name != get_public_schema_name():
            user_obj.tenants.through.objects.filter(
                user=user_obj,
                tenant__schema_name=get_public_schema_name(),
            ).delete()

        # Add the M2M tenant link
        user_obj.tenants.add(self)

        tenant_user_added.send(
            sender=self.__class__,
            user=user_obj,
            tenant=self,
        )


class Domain(DomainMixin):
    """
    Domain model for routing requests to the correct tenant.
    """
    pass


class TenantBusinessProfile(models.Model):
    """
    Per-tenant schema-less JSON blob holding business identity values
    (cementing-company name, contact info, default submitters, etc.) that
    get spliced into agency forms at filing time.

    Shape is convention-only — enforced by `apps.filing_automation.services.adapter`
    and exposed to the frontend via the field-registry schema endpoint.
    """

    tenant = models.OneToOneField(
        'Tenant',
        on_delete=models.CASCADE,
        related_name='business_profile',
    )
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Tenant Business Profile'
        verbose_name_plural = 'Tenant Business Profiles'

    def __str__(self) -> str:
        return f"TenantBusinessProfile<{self.tenant.slug}>"

    def get(self, path: str, default=None):
        parts = path.split('.')
        node = self.data if isinstance(self.data, dict) else {}
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value) -> None:
        parts = path.split('.')
        if not isinstance(self.data, dict):
            self.data = {}
        node = self.data
        for part in parts[:-1]:
            existing = node.get(part)
            if not isinstance(existing, dict):
                existing = {}
                node[part] = existing
            node = existing
        node[parts[-1]] = value

    def merge(self, payload: dict) -> None:
        if not isinstance(self.data, dict):
            self.data = {}
        _deep_merge(self.data, payload or {})


def _deep_merge(dst: dict, src: dict) -> None:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], value)
        else:
            dst[key] = value


class ClientWorkspace(models.Model):
    """
    A workspace within a tenant for a specific client/operator.
    Regulatory firms have multiple clients, each with their own wells.
    This allows isolation of wells and plans by client within a single tenant.
    """
    tenant = models.ForeignKey('Tenant', on_delete=models.CASCADE, related_name='workspaces')
    name = models.CharField(max_length=255, help_text="Client/operator name (e.g., 'Acme Oil Co')")
    operator_number = models.CharField(max_length=50, blank=True, help_text="RRC operator number")
    description = models.TextField(blank=True, help_text="Additional notes about this client")
    is_active = models.BooleanField(default=True, help_text="Inactive workspaces are archived")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [['tenant', 'name']]
        ordering = ['name']
        verbose_name = 'Client Workspace'
        verbose_name_plural = 'Client Workspaces'
        indexes = [
            models.Index(fields=['tenant', 'is_active']),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.tenant.slug})"


class WorkspaceMembership(models.Model):
    workspace = models.ForeignKey(
        ClientWorkspace,
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='workspace_memberships',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [['workspace', 'user']]
        verbose_name = 'Workspace Membership'
        verbose_name_plural = 'Workspace Memberships'


class TenantAdminRole(models.Model):
    """
    Explicit tenant-admin flag, separate from Django's User.is_staff (admin portal access).
    Users with is_tenant_admin=True can manage workspaces, members, and other admin actions.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='admin_roles',
    )
    tenant = models.ForeignKey(
        'Tenant',
        on_delete=models.CASCADE,
        related_name='admin_roles',
    )
    is_tenant_admin = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [['user', 'tenant']]
        verbose_name = 'Tenant Admin Role'
        verbose_name_plural = 'Tenant Admin Roles'


class TenantPlan(models.Model):
    tenant = models.OneToOneField('Tenant', on_delete=models.CASCADE)
    plan = models.ForeignKey('plans.Plan', on_delete=models.SET_NULL, null=True)
    start_date = models.DateTimeField(default=timezone.now)
    end_date = models.DateTimeField(null=True, blank=True)
    user_limit = models.PositiveIntegerField()
    discount = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    sales_rep = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True, null=True)
    # Optional per-tenant overrides layered over plan defaults
    feature_overrides = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.tenant} - {self.plan} Plan"

    def has_space_for_user(self):
        current_users = self.tenant.user_set.count()
        return current_users < self.user_limit



class PlanFeature(models.Model):
    """
    Plan-level feature set stored as JSON for flexibility and easy evolution.
    Example payload keys:
      {
        "single_state": true,
        "multi_state": false,
        "auto_extraction": true,
        "actual_wellbore_diagrams": true,
        "as_plugged_diagrams": false,
        "ai_plan_mods": true,
        "regulatory_filing": true,
        "regulatory_tracking": true,
        "tenant_policies": true,
        "single_filings": true,
        "multi_filings": true,
        "estimator": true,
        "erp_integration": false
      }
    """
    plan = models.OneToOneField('plans.Plan', on_delete=models.CASCADE, related_name='features')
    features = models.JSONField(default=dict)

    class Meta:
        verbose_name = 'Plan Feature'
        verbose_name_plural = 'Plan Features'

    def __str__(self) -> str:
        return f"PlanFeature<{getattr(self.plan, 'name', str(self.plan_id))}>"


class DeletedTenantBackup(models.Model):
    """
    Track backups of deleted tenants for recovery and compliance.

    This model stores metadata about tenant backups created before deletion,
    including the pg_dump backup file path and verification status.
    """
    # Original tenant information
    tenant_id = models.UUIDField(db_index=True, help_text="Original tenant UUID")
    tenant_slug = models.CharField(max_length=64, db_index=True)
    tenant_name = models.CharField(max_length=255)
    schema_name = models.CharField(max_length=63, help_text="PostgreSQL schema name")

    # Backup metadata
    backup_path = models.CharField(
        max_length=512,
        help_text="Full path to pg_dump backup file"
    )
    backup_size_bytes = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Size of backup file in bytes"
    )
    backup_checksum = models.CharField(
        max_length=64,
        blank=True,
        help_text="SHA256 checksum of backup file"
    )

    # Deletion workflow
    soft_deleted_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When tenant was marked for deletion (soft delete)"
    )
    hard_deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When schema was actually dropped (hard delete)"
    )
    scheduled_deletion_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When schema is scheduled to be dropped"
    )

    # Backup verification
    backup_verified = models.BooleanField(
        default=False,
        help_text="Whether backup integrity was verified"
    )
    verification_message = models.TextField(
        blank=True,
        help_text="Details from backup verification"
    )

    # Additional context
    deleted_by_email = models.EmailField(
        blank=True,
        help_text="Email of user who initiated deletion"
    )
    deletion_reason = models.TextField(
        blank=True,
        help_text="Reason for deletion (audit trail)"
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional metadata (record counts, etc.)"
    )

    class Meta:
        verbose_name = 'Deleted Tenant Backup'
        verbose_name_plural = 'Deleted Tenant Backups'
        ordering = ['-soft_deleted_at']
        indexes = [
            models.Index(fields=['-soft_deleted_at']),
            models.Index(fields=['scheduled_deletion_at']),
        ]

    def __str__(self) -> str:
        return f"DeletedTenantBackup<{self.tenant_slug} @ {self.soft_deleted_at}>"

    def is_hard_deleted(self) -> bool:
        """Check if schema has been permanently dropped."""
        return self.hard_deleted_at is not None

    def is_pending_deletion(self) -> bool:
        """Check if schema is still pending hard deletion."""
        return self.hard_deleted_at is None and self.scheduled_deletion_at is not None


class UsageRecord(models.Model):
    """
    Track usage events for billing and analytics.

    Records all billable events per tenant including:
    - Plan generation
    - Document extraction
    - AI chat interactions
    - API calls

    Used for usage reporting, billing, and analytics dashboards.
    """

    # Event type choices
    EVENT_PLAN_GENERATED = 'plan_generated'
    EVENT_EXTRACTION_COMPLETED = 'extraction_completed'
    EVENT_AI_CHAT_MESSAGE = 'ai_chat_message'
    EVENT_API_CALL = 'api_call'
    EVENT_DOCUMENT_UPLOADED = 'document_uploaded'
    EVENT_PLAN_MODIFIED = 'plan_modified'

    EVENT_TYPE_CHOICES = [
        (EVENT_PLAN_GENERATED, 'Plan Generated'),
        (EVENT_EXTRACTION_COMPLETED, 'Extraction Completed'),
        (EVENT_AI_CHAT_MESSAGE, 'AI Chat Message'),
        (EVENT_API_CALL, 'API Call'),
        (EVENT_DOCUMENT_UPLOADED, 'Document Uploaded'),
        (EVENT_PLAN_MODIFIED, 'Plan Modified'),
    ]

    # Core relationships
    tenant = models.ForeignKey(
        'Tenant',
        on_delete=models.CASCADE,
        related_name='usage_records',
        help_text="Tenant this usage is attributed to"
    )
    workspace = models.ForeignKey(
        'ClientWorkspace',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='usage_records',
        help_text="Client workspace this usage is attributed to (if applicable)"
    )
    user = models.ForeignKey(
        'User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='usage_records',
        help_text="User who triggered this event (if applicable)"
    )

    # Event details
    event_type = models.CharField(
        max_length=50,
        choices=EVENT_TYPE_CHOICES,
        db_index=True,
        help_text="Type of usage event"
    )
    resource_type = models.CharField(
        max_length=50,
        blank=True,
        help_text="Resource type (e.g., 'well', 'plan', 'document')"
    )
    resource_id = models.CharField(
        max_length=100,
        blank=True,
        help_text="ID of the resource (e.g., API number, plan ID, document ID)"
    )

    # Usage metrics
    tokens_used = models.IntegerField(
        default=0,
        help_text="AI tokens consumed (for AI operations)"
    )
    processing_time_ms = models.IntegerField(
        default=0,
        help_text="Processing time in milliseconds"
    )

    # Additional metadata
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional event-specific data: model used, endpoint, parameters, etc."
    )

    # Timestamps
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="When this usage event occurred"
    )

    class Meta:
        db_table = 'tenants_usage_records'
        verbose_name = 'Usage Record'
        verbose_name_plural = 'Usage Records'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tenant', '-created_at']),
            models.Index(fields=['tenant', 'event_type', '-created_at']),
            models.Index(fields=['workspace', '-created_at']),
            models.Index(fields=['event_type', '-created_at']),
            models.Index(fields=['user', '-created_at']),
        ]

    def __str__(self) -> str:
        return f"UsageRecord<{self.tenant.slug}:{self.event_type} @ {self.created_at}>"


class FlexTenantIdField(models.BigIntegerField):
    """
    A BigIntegerField that gracefully handles UUID objects on write by hashing
    them into a positive 63-bit integer.  This allows tests to pass arbitrary
    UUID values as tenant IDs (for cross-tenant isolation checks) without
    overflowing PostgreSQL's bigint type.

    On read, the value is always a Python int, so `notification.tenant_id == tenant.id`
    works correctly (both sides are ints).
    """

    def get_prep_value(self, value):
        if isinstance(value, uuid.UUID):
            # Fold the 128-bit UUID int into a signed 63-bit space so it fits
            # in a PostgreSQL bigint.  This is intentionally lossy — the only
            # goal is a deterministic, collision-unlikely integer that differs
            # from any real tenant PK.
            return value.int & 0x7FFFFFFFFFFFFFFF
        return super().get_prep_value(value)

    def from_db_value(self, value, expression, connection):
        return value  # always an int from the DB


class Notification(models.Model):
    NOTIF_TYPES = [
        ('info', 'Info'), ('success', 'Success'),
        ('warning', 'Warning'), ('error', 'Error'),
    ]
    id         = models.UUIDField(primary_key=True, default=uuid.uuid4)
    user       = models.ForeignKey(
                   settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                   related_name='notifications'
                 )
    tenant_id  = FlexTenantIdField(db_index=True)
    verb       = models.CharField(max_length=255)
    message    = models.TextField(blank=True)
    notif_type = models.CharField(max_length=20, choices=NOTIF_TYPES, default='info')
    action_url = models.CharField(max_length=500, blank=True)
    read       = models.BooleanField(default=False, db_index=True)
    read_at    = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tenant_id', 'user', 'read']),
            models.Index(fields=['tenant_id', 'verb']),
        ]

    def __str__(self):
        return f"Notification<{self.verb}>"

