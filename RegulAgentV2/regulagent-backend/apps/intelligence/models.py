import base64
import hashlib
import os
import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q
from apps.intelligence.constants import (
    AGENCY_CHOICES,
    FILING_SOURCE_CHOICES,
    FILING_STATUS_CHOICES,
    FORM_TYPE_CHOICES,
    INTERACTION_ACTION_CHOICES,
    PARSE_STATUS_CHOICES,
    PRIORITY_CHOICES,
    RECOMMENDATION_SCOPE_CHOICES,
)


class FilingStatusRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    filing_id = models.CharField(max_length=128, db_index=True, help_text="Agency tracking/confirmation number")

    # Nullable FKs to form models (polymorphic pattern)
    w3_form = models.ForeignKey(
        'public_core.W3FormORM',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='filing_statuses',
    )
    plan_snapshot = models.ForeignKey(
        'public_core.PlanSnapshot',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='filing_statuses',
    )
    c103_form = models.ForeignKey(
        'public_core.C103FormORM',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='filing_statuses',
    )

    tenant_id = models.UUIDField(db_index=True)
    well = models.ForeignKey(
        'public_core.WellRegistry',
        on_delete=models.CASCADE,
        related_name='filing_statuses',
    )

    agency = models.CharField(max_length=10, choices=AGENCY_CHOICES)
    form_type = models.CharField(max_length=10, choices=FORM_TYPE_CHOICES)
    status = models.CharField(
        max_length=20,
        choices=FILING_STATUS_CHOICES,
        default='pending',
        db_index=True,
    )

    agency_remarks = models.TextField(blank=True)
    reviewer_name = models.CharField(max_length=128, blank=True)
    status_date = models.DateField(null=True, blank=True)
    portal_url = models.URLField(blank=True)
    source = models.CharField(
        max_length=20,
        choices=FILING_SOURCE_CHOICES,
        default='manual',
        db_index=True,
        help_text="How this filing entered the system",
    )
    raw_portal_data = models.JSONField(default=dict)
    polled_at = models.DateTimeField(null=True, blank=True)

    # Denormalized geo
    state = models.CharField(max_length=2, blank=True, db_index=True)
    district = models.CharField(max_length=10, blank=True)
    county = models.CharField(max_length=64, blank=True)
    land_type = models.CharField(max_length=20, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'intelligence_filing_status'
        indexes = [
            models.Index(fields=['tenant_id', '-status_date']),
            models.Index(fields=['agency', 'form_type', 'status']),
            models.Index(fields=['filing_id', 'agency']),
        ]

    def __str__(self):
        return f"Filing {self.filing_id} ({self.agency} / {self.form_type}) — {self.status}"


class RejectionRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    filing_status = models.ForeignKey(
        FilingStatusRecord,
        on_delete=models.CASCADE,
        related_name='rejections',
    )

    # Nullable FKs to form models (polymorphic pattern)
    w3_form = models.ForeignKey(
        'public_core.W3FormORM',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='rejections',
    )
    plan_snapshot = models.ForeignKey(
        'public_core.PlanSnapshot',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='rejections',
    )
    c103_form = models.ForeignKey(
        'public_core.C103FormORM',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='rejections',
    )

    tenant_id = models.UUIDField(db_index=True)
    well = models.ForeignKey(
        'public_core.WellRegistry',
        on_delete=models.CASCADE,
        related_name='rejections',
    )

    # Denormalized geo
    state = models.CharField(max_length=2, blank=True, db_index=True)
    district = models.CharField(max_length=10, blank=True)
    county = models.CharField(max_length=64, blank=True)
    land_type = models.CharField(max_length=20, blank=True)

    agency = models.CharField(max_length=10, choices=AGENCY_CHOICES)
    form_type = models.CharField(max_length=10, choices=FORM_TYPE_CHOICES)

    raw_rejection_notes = models.TextField(blank=True)
    rejection_date = models.DateField(null=True, blank=True)
    reviewer_name = models.CharField(max_length=128, blank=True)

    parsed_issues = models.JSONField(default=list, help_text="AI-parsed list of field-level issues")
    parse_status = models.CharField(
        max_length=10,
        choices=PARSE_STATUS_CHOICES,
        default='pending',
        db_index=True,
    )

    submitted_form_snapshot = models.JSONField(
        default=dict,
        help_text="Snapshot of form data at time of submission",
    )

    accepted_corrections = models.JSONField(
        default=list,
        blank=True,
        help_text="User-accepted corrections. Each entry: {issue_index, field_name, applied_value, accepted_at}",
    )
    correction_status = models.CharField(
        max_length=20,
        choices=[
            ("none", "None"),
            ("partial", "Partial"),
            ("all_applied", "All Applied"),
        ],
        default="none",
        db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'intelligence_rejection_records'
        indexes = [
            models.Index(fields=['tenant_id', '-rejection_date']),
            models.Index(fields=['agency', 'form_type']),
            models.Index(fields=['parse_status']),
        ]

    def __str__(self):
        return f"Rejection ({self.agency} / {self.form_type}) on {self.rejection_date}"


class RejectionPattern(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    form_type = models.CharField(max_length=10, choices=FORM_TYPE_CHOICES)
    field_name = models.CharField(max_length=128)
    issue_category = models.CharField(max_length=30)
    issue_subcategory = models.CharField(max_length=30, blank=True)

    state = models.CharField(max_length=2, blank=True)
    district = models.CharField(max_length=10, blank=True)
    agency = models.CharField(max_length=10, choices=AGENCY_CHOICES)

    pattern_description = models.TextField()
    example_bad_value = models.CharField(max_length=255, blank=True)
    example_good_value = models.CharField(max_length=255, blank=True)

    # Stats
    occurrence_count = models.IntegerField(default=0)
    tenant_count = models.IntegerField(default=0)
    rejection_rate = models.FloatField(default=0.0)
    first_observed = models.DateTimeField(null=True, blank=True)
    last_observed = models.DateTimeField(null=True, blank=True)

    # Trend
    is_trending = models.BooleanField(default=False)
    trend_direction = models.FloatField(
        default=0.0,
        help_text="Slope: positive=increasing, negative=decreasing",
    )

    confidence = models.FloatField(default=0.0)
    embedding_vector = models.ForeignKey(
        'public_core.DocumentVector',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='rejection_patterns',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'intelligence_rejection_patterns'
        unique_together = [('form_type', 'field_name', 'issue_category', 'state', 'district', 'agency')]
        indexes = [
            models.Index(fields=['form_type', 'state']),
            models.Index(fields=['is_trending']),
            models.Index(fields=['-occurrence_count']),
        ]

    def __str__(self):
        return f"Pattern: {self.form_type}/{self.field_name} — {self.issue_category} ({self.agency})"


class Recommendation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pattern = models.ForeignKey(
        RejectionPattern,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='recommendations',
    )

    # Targeting
    form_type = models.CharField(max_length=10, choices=FORM_TYPE_CHOICES)
    field_name = models.CharField(max_length=128)
    state = models.CharField(max_length=2, blank=True)
    district = models.CharField(max_length=10, blank=True)
    county = models.CharField(max_length=64, blank=True)
    land_type = models.CharField(max_length=20, blank=True)

    # Content
    title = models.CharField(max_length=255)
    description = models.TextField()
    suggested_value = models.CharField(max_length=255, blank=True)

    trigger_condition = models.JSONField(
        default=dict,
        help_text="JSON: {field_name, trigger_values, trigger_pattern, context_match}",
    )

    scope = models.CharField(
        max_length=15,
        choices=RECOMMENDATION_SCOPE_CHOICES,
        db_index=True,
    )
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='medium')

    # Effectiveness
    times_shown = models.IntegerField(default=0)
    times_accepted = models.IntegerField(default=0)
    times_dismissed = models.IntegerField(default=0)
    acceptance_rate = models.FloatField(default=0.0)

    is_active = models.BooleanField(default=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'intelligence_recommendations'
        indexes = [
            models.Index(fields=['form_type', 'field_name', 'state']),
            models.Index(fields=['scope', 'is_active']),
            models.Index(fields=['priority']),
        ]

    def __str__(self):
        return f"Rec: {self.title} ({self.form_type}/{self.field_name})"


class RecommendationInteraction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recommendation = models.ForeignKey(
        Recommendation,
        on_delete=models.CASCADE,
        related_name='interactions',
    )
    tenant_id = models.UUIDField(db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='recommendation_interactions',
    )

    action = models.CharField(max_length=10, choices=INTERACTION_ACTION_CHOICES)
    field_value_at_time = models.CharField(max_length=255, blank=True)
    dismissal_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'intelligence_recommendation_interactions'
        indexes = [
            models.Index(fields=['recommendation', '-created_at']),
            models.Index(fields=['tenant_id', 'action']),
        ]

    def __str__(self):
        return f"Interaction: {self.action} on {self.recommendation_id} by {self.user_id}"


class PortalCredential(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(db_index=True)
    agency = models.CharField(max_length=10, choices=AGENCY_CHOICES)

    # Owning user. Nullable in Phase 1 (backfill assigns existing rows to tenant
    # owner/admin; Phase 5 makes this non-null once all rows are covered).
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        db_index=True,
        related_name='portal_credentials',
    )

    # True on exactly one credential per (tenant_id, agency) — the one used by
    # automated/scheduled jobs (polling, sync) that have no acting user in scope.
    # Enforced at the DB level via a partial unique constraint below.
    is_default_for_automation = models.BooleanField(default=False)

    # Encrypted at rest using per-tenant Fernet key (derived from tenant_id + ENCRYPTION_PEPPER + key_salt)
    encrypted_username = models.BinaryField()
    encrypted_password = models.BinaryField()

    # Per-credential random salt for key derivation — ensures unique key per credential
    # even within the same tenant. Must never change after first encryption.
    key_salt = models.BinaryField(default=b'', help_text="Per-credential salt for key derivation")

    last_successful_login = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_test = models.BooleanField(
        default=False,
        help_text=(
            "True when this credential is paired with a sandbox / test account. "
            "Live RRC submissions only fire when settings.RRC_LIVE_SUBMIT_ENABLED is "
            "True AND is_test is False — i.e. production credentials submit for real, "
            "test credentials always save as draft."
        ),
    )

    # --- Circuit-breaker state ---
    AUTH_STATE_CHOICES = [
        ('ok', 'OK'),
        ('needs_reauth', 'Needs Re-auth'),
        ('locked', 'Locked'),
    ]
    auth_state = models.CharField(
        max_length=20,
        choices=AUTH_STATE_CHOICES,
        default='ok',
        db_index=True,
        help_text="Current authentication circuit-breaker state for this credential.",
    )
    consecutive_login_failures = models.PositiveIntegerField(
        default=0,
        help_text="Number of consecutive login failures since last success.",
    )
    last_login_failure_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the most recent login failure.",
    )
    last_login_error = models.TextField(
        blank=True,
        default='',
        help_text="Error message from the most recent login failure (truncated to 1000 chars).",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'intelligence_portal_credentials'
        constraints = [
            # Each user may hold at most one credential per (tenant, agency).
            models.UniqueConstraint(
                fields=['user', 'tenant_id', 'agency'],
                name='uniq_cred_user_tenant_agency',
            ),
            # At most one credential per (tenant, agency) may be the automation
            # default. Enforced as a partial index — only rows where the flag is
            # True participate, so non-default rows are unrestricted.
            models.UniqueConstraint(
                fields=['tenant_id', 'agency'],
                condition=Q(is_default_for_automation=True),
                name='uniq_one_automation_default_per_tenant_agency',
            ),
        ]

    def __str__(self):
        return f"PortalCredential: {self.agency} / tenant {self.tenant_id}"

    @staticmethod
    def _derive_key(tenant_id: str, salt: bytes) -> bytes:
        """
        Derive a Fernet-compatible key from tenant_id + server pepper + per-credential salt.

        Each tenant gets a unique encryption key. Compromising one tenant's credentials
        (even knowing tenant_id and salt) requires the server-side ENCRYPTION_PEPPER to
        reconstruct the key. PBKDF2 with 100,000 iterations resists brute-force.

        Returns URL-safe base64-encoded 32-byte key suitable for Fernet.
        """
        pepper = os.environ.get('ENCRYPTION_PEPPER', '')
        if not pepper:
            raise ValueError(
                "ENCRYPTION_PEPPER environment variable is required for credential encryption. "
                "Set it in the container environment."
            )
        raw_key = hashlib.pbkdf2_hmac(
            'sha256',
            f"{tenant_id}:{pepper}".encode(),
            salt,
            iterations=100_000,
            dklen=32,
        )
        return base64.urlsafe_b64encode(raw_key)

    def _ensure_salt(self) -> bytes:
        """Return the existing salt, or generate and store a new one."""
        if not self.key_salt:
            self.key_salt = os.urandom(16)
        return bytes(self.key_salt)

    def _get_fernet(self):
        """Return a Fernet instance using the per-tenant derived key."""
        from cryptography.fernet import Fernet
        salt = self._ensure_salt()
        key = self._derive_key(str(self.tenant_id), salt)
        return Fernet(key)

    def encrypt(self, value: str) -> bytes:
        return self._get_fernet().encrypt(value.encode())

    def decrypt(self, encrypted_value: bytes) -> str:
        return self._get_fernet().decrypt(bytes(encrypted_value)).decode()

    def set_username(self, username: str) -> None:
        self._ensure_salt()  # lock in the salt before first encryption
        self.encrypted_username = self.encrypt(username)

    def get_username(self) -> str:
        return self.decrypt(self.encrypted_username)

    def set_password(self, password: str) -> None:
        self._ensure_salt()  # lock in the salt before first encryption
        self.encrypted_password = self.encrypt(password)

    def get_password(self) -> str:
        return self.decrypt(self.encrypted_password)

    # --- Circuit-breaker methods ---

    def is_login_blocked(self) -> bool:
        """Return True when this credential must not be used for login attempts."""
        return self.auth_state in {'needs_reauth', 'locked'}

    def record_login_failure(self, kind: str, message: str = "") -> None:
        """
        Record a login failure and update the circuit-breaker state.

        kind='locked'  -> auth_state='locked'  (RRC has locked the account)
        kind='invalid' -> auth_state='needs_reauth' (bad password / username)
        """
        from django.utils import timezone

        self.auth_state = 'locked' if kind == 'locked' else 'needs_reauth'
        self.consecutive_login_failures += 1
        self.last_login_failure_at = timezone.now()
        self.last_login_error = (message or "")[:1000]
        self.save(update_fields=[
            'auth_state',
            'consecutive_login_failures',
            'last_login_failure_at',
            'last_login_error',
            'updated_at',
        ])

    def record_login_success(self) -> None:
        """
        Reset all circuit-breaker state after a successful login.
        Also updates last_successful_login so the frontend can display it.
        """
        from django.utils import timezone

        self.auth_state = 'ok'
        self.consecutive_login_failures = 0
        self.last_login_error = ''
        self.last_successful_login = timezone.now()
        self.save(update_fields=[
            'auth_state',
            'consecutive_login_failures',
            'last_login_error',
            'last_successful_login',
            'updated_at',
        ])
