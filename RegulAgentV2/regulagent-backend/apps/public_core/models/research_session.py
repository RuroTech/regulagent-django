import uuid

from django.db import models
from django.db.models import SET_NULL

from apps.tenants.models import Tenant

from .well_registry import WellRegistry


class ResearchSession(models.Model):
    STATE_CHOICES = [
        ("NM", "New Mexico"),
        ("TX", "Texas"),
        ("UT", "Utah"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("fetching", "Fetching"),
        ("indexing", "Indexing"),
        ("ready", "Ready"),
        ("error", "Error"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    api_number = models.CharField(max_length=20, db_index=True)
    state = models.CharField(max_length=2, choices=STATE_CHOICES, default="TX")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="research_sessions",
    )
    well = models.ForeignKey(
        WellRegistry,
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="research_sessions",
    )
    total_documents = models.PositiveIntegerField(default=0)
    indexed_documents = models.PositiveIntegerField(default=0)
    failed_documents = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, default="")
    document_list = models.JSONField(default=list, blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    force_fetch = models.BooleanField(default=False, help_text="If True, bypass document cache and re-fetch from source")
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Flexible metadata store (e.g., lease_well_map for cross-reference during extraction)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["api_number", "state", "status"]),
        ]

    def __str__(self) -> str:
        return f"ResearchSession {self.api_number} ({self.state}) - {self.status}"

    @property
    def progress_pct(self) -> int:
        return int(self.indexed_documents / self.total_documents * 100) if self.total_documents > 0 else 0
