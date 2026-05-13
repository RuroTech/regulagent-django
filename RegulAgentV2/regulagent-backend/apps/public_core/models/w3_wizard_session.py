from __future__ import annotations

import uuid

from django.db import models
from simple_history.models import HistoricalRecords

from .well_registry import WellRegistry
from .plan_snapshot import PlanSnapshot
from .w3_orm import W3FormORM
from apps.kernel.services.jurisdiction_registry import detect_jurisdiction


class W3WizardSession(models.Model):
    """
    Persistent wizard state for the W-3 Daily Ticket Upload & Reconciliation Wizard.

    Status lifecycle:
        created → uploading → importing_plan → plan_imported → plan_verified → parsing → parsed → reconciled → justifying → ready → generating → completed
                                                                                                                                                   └→ abandoned
    """

    STATUS_CREATED = "created"
    STATUS_UPLOADING = "uploading"
    STATUS_IMPORTING_PLAN = "importing_plan"
    STATUS_PLAN_IMPORTED = "plan_imported"
    STATUS_PLAN_VERIFIED = "plan_verified"
    STATUS_PARSING = "parsing"
    STATUS_PARSED = "parsed"
    STATUS_RECONCILED = "reconciled"
    STATUS_JUSTIFYING = "justifying"
    STATUS_READY = "ready"
    STATUS_GENERATING = "generating"
    STATUS_COMPLETED = "completed"
    STATUS_ABANDONED = "abandoned"

    STATUS_CHOICES = [
        (STATUS_CREATED, STATUS_CREATED),
        (STATUS_UPLOADING, STATUS_UPLOADING),
        (STATUS_IMPORTING_PLAN, STATUS_IMPORTING_PLAN),
        (STATUS_PLAN_IMPORTED, STATUS_PLAN_IMPORTED),
        (STATUS_PLAN_VERIFIED, STATUS_PLAN_VERIFIED),
        (STATUS_PARSING, STATUS_PARSING),
        (STATUS_PARSED, STATUS_PARSED),
        (STATUS_RECONCILED, STATUS_RECONCILED),
        (STATUS_JUSTIFYING, STATUS_JUSTIFYING),
        (STATUS_READY, STATUS_READY),
        (STATUS_GENERATING, STATUS_GENERATING),
        (STATUS_COMPLETED, STATUS_COMPLETED),
        (STATUS_ABANDONED, STATUS_ABANDONED),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    well = models.ForeignKey(
        WellRegistry,
        on_delete=models.CASCADE,
        related_name="w3_wizard_sessions",
        null=True,
        blank=True,
    )
    plan_snapshot = models.ForeignKey(
        PlanSnapshot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="w3_wizard_sessions",
    )
    w3_form = models.ForeignKey(
        W3FormORM,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="w3_wizard_sessions",
    )

    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)
    workspace = models.ForeignKey(
        'tenants.ClientWorkspace',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='w3_wizard_sessions',
        db_index=True,
    )

    api_number = models.CharField(max_length=14, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_CREATED,
        db_index=True,
    )
    current_step = models.IntegerField(
        default=1,
        help_text="1=upload, 2=verify-plan, 3=review, 4=reconciliation, 5=justification, 6=as-plugged-diagram, 7=generate",
    )

    uploaded_documents = models.JSONField(
        default=list,
        help_text="List of {file_name, file_type, storage_key, uploaded_at, size_bytes}",
    )
    parse_result = models.JSONField(
        default=dict,
        blank=True,
        help_text="Full DWRParseResult as dict",
    )
    reconciliation_result = models.JSONField(
        default=dict,
        blank=True,
        help_text="Full ReconciliationResult as dict",
    )
    justifications = models.JSONField(
        default=dict,
        blank=True,
        help_text="{plug_number: {note, resolved, resolved_by, resolved_at}}",
    )
    w3_generation_result = models.JSONField(default=dict, blank=True)
    plan_options = models.JSONField(
        default=dict,
        blank=True,
        help_text="Operator plan generation preferences (combine_nearby_plugs, combine_threshold_ft, surface_plug_bottom_ft)",
    )
    formation_audit = models.JSONField(
        default=dict,
        blank=True,
        help_text="FormationAuditResult from formation_isolation_auditor",
    )
    compliance_result = models.JSONField(
        default=dict,
        blank=True,
        help_text="ComplianceResult from coa_compliance_checker",
    )
    event_compliance_flags = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-event regulatory compliance flags from event_compliance_checker",
    )

    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    plan_import_task_id = models.CharField(max_length=255, blank=True, default="")
    wbd_image_path = models.CharField(
        max_length=500, blank=True, default="",
        help_text="Path to captured as-plugged wellbore diagram PNG",
    )
    created_by = models.EmailField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_accessed_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["tenant_id", "status"]),
            models.Index(fields=["api_number", "tenant_id"]),
            models.Index(fields=["well", "status"]),
            models.Index(fields=["workspace", "status"]),
        ]
        verbose_name = "W-3 Wizard Session"
        verbose_name_plural = "W-3 Wizard Sessions"

    @property
    def jurisdiction(self) -> str:
        """Infer jurisdiction from API number prefix."""
        return detect_jurisdiction(self.api_number or "")

    @property
    def form_type(self) -> str:
        """Return the form type label for this jurisdiction."""
        return "sundry" if self.jurisdiction == "NM" else "w3"

    def __str__(self) -> str:  # pragma: no cover
        return f"W3Wizard {self.api_number} ({self.status})"
