from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.public_core.models import DocumentVector

BATCH_SIZE = 500


class Command(BaseCommand):
    help = (
        "Backfill DocumentVector.metadata['visibility'] (and best-effort "
        "'source_type') on rows indexed before Part B provenance tagging."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - no records will be written"))

        updated = 0
        skipped = 0
        batch: list[DocumentVector] = []

        qs = DocumentVector.objects.all()
        for idx, vec in enumerate(qs.iterator(chunk_size=200)):
            if idx > 0 and idx % 500 == 0:
                self.stdout.write(f"  processed {idx} vectors...")

            meta = vec.metadata or {}
            if "visibility" in meta:
                skipped += 1
                continue

            meta["visibility"] = "private" if meta.get("tenant_id") else "public"

            # Best-effort source_type enrichment via the linked ExtractedDocument.
            if not meta.get("source_type"):
                ed_id = meta.get("ed_id")
                if ed_id:
                    from apps.public_core.models import ExtractedDocument

                    source_type = (
                        ExtractedDocument.objects.filter(id=ed_id)
                        .values_list("source_type", flat=True)
                        .first()
                    )
                    if source_type:
                        meta["source_type"] = source_type

            vec.metadata = meta
            batch.append(vec)
            updated += 1

            if len(batch) >= BATCH_SIZE:
                if not dry_run:
                    with transaction.atomic():
                        DocumentVector.objects.bulk_update(batch, ["metadata"])
                batch = []

        if batch and not dry_run:
            with transaction.atomic():
                DocumentVector.objects.bulk_update(batch, ["metadata"])

        self.stdout.write(
            self.style.SUCCESS(f"Updated {updated} vectors, skipped {skipped} (already had visibility)")
        )
