"""
Management command: reparse_rejections

Re-queues existing RejectionRecord objects for AI re-parsing so that
the new policy_references field is populated on historical records.

Only reparsing records that are safe to overwrite:
  - parse_status='parsed'  (not verified, not pending)
  - correction_status='none'  (no user corrections applied yet)
"""
from django.core.management.base import BaseCommand

from apps.intelligence.models import RejectionRecord
from apps.intelligence.tasks import parse_rejection_notes


class Command(BaseCommand):
    help = "Re-queue existing parsed RejectionRecords for AI re-parsing to populate policy_references."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be queued without actually dispatching tasks.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of records to requeue (useful for testing).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]

        qs = RejectionRecord.objects.filter(
            parse_status="parsed",
            correction_status="none",
        ).exclude(
            raw_rejection_notes=""
        ).order_by("created_at")

        total = qs.count()
        if limit:
            qs = qs[:limit]

        self.stdout.write(
            f"Found {total} eligible record(s)"
            + (f" — processing first {limit}" if limit and limit < total else "")
            + ("  [DRY RUN]" if dry_run else "")
        )

        queued = 0
        skipped = 0
        for record in qs:
            # Skip records whose issues already have policy_references populated
            issues = record.parsed_issues or []
            if issues and all("policy_references" in issue for issue in issues):
                skipped += 1
                if dry_run:
                    self.stdout.write(f"  SKIP  {record.id} — policy_references already present")
                continue

            if dry_run:
                self.stdout.write(
                    f"  WOULD QUEUE  {record.id}  ({record.agency}/{record.form_type}, "
                    f"{len(issues)} issue(s))"
                )
            else:
                parse_rejection_notes.delay(str(record.id))
                self.stdout.write(
                    f"  QUEUED  {record.id}  ({record.agency}/{record.form_type}, "
                    f"{len(issues)} issue(s))"
                )
            queued += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. {queued} queued, {skipped} skipped (already have policy_references)."
                if not dry_run
                else f"\nDry run complete. {queued} would be queued, {skipped} already up to date."
            )
        )
