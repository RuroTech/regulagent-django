from __future__ import annotations

import uuid

from django.db import models


class FilingJob(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_RETRYING = "retrying"

    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCEEDED, "Succeeded"),
        (STATUS_FAILED, "Failed"),
        (STATUS_RETRYING, "Retrying"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    plan_snapshot = models.ForeignKey(
        "public_core.PlanSnapshot",
        on_delete=models.CASCADE,
        related_name="filing_jobs",
    )
    workspace = models.ForeignKey(
        "tenants.ClientWorkspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="filing_jobs",
    )
    tenant_id = models.UUIDField(db_index=True)

    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_QUEUED,
        db_index=True,
    )
    celery_task_id = models.CharField(max_length=128, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    attestation = models.JSONField(default=dict, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    confirmation_number = models.CharField(max_length=128, blank=True)
    screenshot_path = models.CharField(max_length=512, blank=True)

    error_class = models.CharField(max_length=128, blank=True)
    error_message = models.TextField(blank=True)
    traceback_truncated = models.TextField(blank=True)

    filing_status = models.ForeignKey(
        "intelligence.FilingStatusRecord",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="filing_jobs",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "filing_automation_filing_job"
        indexes = [
            models.Index(fields=["tenant_id", "status"]),
            models.Index(fields=["plan_snapshot", "status"]),
        ]

    def __str__(self) -> str:
        return f"FilingJob<{self.id} {self.status}>"
