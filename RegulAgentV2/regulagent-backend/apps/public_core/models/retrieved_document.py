"""
RetrievedDocument — manifest record for every file downloaded from an RRC (or other)
source during a research session.

One row is created per (api_number, href) pair.  The index_status field tracks the
lifecycle of the document from raw download → extraction → indexed/failed.

Choices for index_status
------------------------
pending             — downloaded, waiting for extraction
success             — extracted successfully; linked ExtractedDocument exists
partial             — extraction completed with warnings
error               — extraction failed hard
unsupported         — file type is not extractable
skipped_directional — directional survey: downloaded but not enqueued for extraction
no_forms            — extraction returned zero recognizable forms
quota_exceeded      — OpenAI/LLM quota hit; extraction aborted
failed              — worker was killed (SIGKILL) or crashed before extraction completed
"""
from __future__ import annotations

from django.db import models

from .extracted_document import ExtractedDocument
from .well_registry import WellRegistry


INDEX_STATUS_CHOICES = [
    ("pending",              "Pending"),
    ("success",              "Success"),
    ("partial",              "Partial"),
    ("error",                "Error"),
    ("unsupported",          "Unsupported"),
    ("skipped_directional",  "Skipped — Directional Survey"),
    ("no_forms",             "No Forms Found"),
    ("quota_exceeded",       "Quota Exceeded"),
    ("failed",               "Failed (worker killed)"),
]


class RetrievedDocument(models.Model):
    """Manifest row for a single downloaded regulatory document."""

    well = models.ForeignKey(
        WellRegistry,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="retrieved_documents",
    )
    # Normalised 14-digit API number (42-digit-stripped, zero-padded to 14 chars).
    # Always store via normalize_api_14digit() on write.
    api_number = models.CharField(max_length=16, db_index=True)

    # Source URL path (relative or absolute) used as the stable unique key
    href = models.TextField()

    # Original filename used when saving to local_path
    filename = models.CharField(max_length=255)

    # Absolute path on the server where the file was saved.  Blank when not yet
    # saved or when only the href is stored for deduplication purposes.
    local_path = models.TextField(blank=True)

    # SHA-256 of the downloaded file (blank until computed)
    file_hash = models.CharField(max_length=64, blank=True)

    # Form kind inferred from filename / URL (e.g. "w2", "w15", "gau", "other")
    kind = models.CharField(max_length=64, blank=True)

    # Lifecycle status — see module docstring for the full set
    index_status = models.CharField(
        max_length=32,
        choices=INDEX_STATUS_CHOICES,
        default="pending",
        db_index=True,
    )

    # Linked ExtractedDocument (populated once extraction completes successfully)
    extracted_document = models.ForeignKey(
        ExtractedDocument,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="retrieved_documents",
    )

    # Data origin — currently always "rrc" for portal downloads; reserved for
    # "neubus", "tenant_upload", etc. in the future.
    source_type = models.CharField(max_length=16, default="rrc")

    downloaded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "public_core_retrieved_documents"
        constraints = [
            models.UniqueConstraint(
                fields=["api_number", "href"],
                name="uniq_retrieved_doc_api_href",
            )
        ]
        indexes = [
            models.Index(fields=["api_number", "index_status"]),
            models.Index(fields=["downloaded_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return (
            f"RetrievedDocument<{self.api_number}:{self.filename}:"
            f"{self.index_status}:{self.id}>"
        )
