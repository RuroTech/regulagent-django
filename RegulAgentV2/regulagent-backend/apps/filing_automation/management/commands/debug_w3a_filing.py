"""Management command: debug_w3a_filing

Dev-only headed runner for the W-3A auto-filing pipeline.
Launches Playwright in headed (visible browser) mode so engineers can watch
and debug the filing flow against a real or sandbox RRC account.

Usage examples:

    # Bare run (500 ms slow-mo, headless=False):
    python manage.py debug_w3a_filing <job_id>

    # 1-second slow-mo:
    python manage.py debug_w3a_filing <job_id> --slow-mo 1000

    # Open Playwright Inspector (PWDEBUG=1):
    python manage.py debug_w3a_filing <job_id> --inspector

    # Pause at a specific section:
    python manage.py debug_w3a_filing <job_id> --pause-at basic_fields

WARNINGS:
  - This command is intended for LOCAL development only.
  - It refuses to run inside a Docker container (/.dockerenv detected).
  - It refuses to run against already-completed jobs.
  - Remove before shipping to production.
"""
from __future__ import annotations

import asyncio
import os
import sys

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Dev-only: run a FilingJob in a headed (visible) browser for inspection. "
        "Refuses to run inside Docker or against completed jobs."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "job_id",
            type=str,
            help="UUID of the FilingJob to replay.",
        )
        parser.add_argument(
            "--slow-mo",
            type=int,
            default=500,
            dest="slow_mo",
            metavar="MS",
            help="Playwright slow-mo delay in milliseconds (default: 500).",
        )
        parser.add_argument(
            "--inspector",
            action="store_true",
            default=False,
            help="Open the Playwright Inspector (sets PWDEBUG=1 before launch).",
        )
        parser.add_argument(
            "--pause-at",
            dest="pause_at",
            default="",
            metavar="SECTION",
            help=(
                "Section name to pause at inside RRCFormAutomator._step(). "
                "Choices: basic_fields, location_fields, well_type_fields, "
                "file_attachments, contact_information, agreement."
            ),
        )

    def handle(self, *args, **options):
        # ── Guard: refuse to run inside Docker ──────────────────────────────
        if os.path.exists("/.dockerenv"):
            raise CommandError(
                "debug_w3a_filing refuses to run inside a Docker container. "
                "Headed Playwright requires a display. Run this command on the "
                "host machine, not inside the container."
            )

        job_id: str = options["job_id"]
        slow_mo: int = options["slow_mo"]
        inspector: bool = options["inspector"]
        pause_at: str = options["pause_at"]

        # ── Set env vars BEFORE importing Playwright ────────────────────────
        if inspector:
            os.environ["PWDEBUG"] = "1"
            self.stdout.write("  PWDEBUG=1: Playwright Inspector will open.")

        if pause_at:
            os.environ["W3A_PAUSE_AT"] = pause_at
            self.stdout.write(f"  W3A_PAUSE_AT={pause_at!r}: automation will pause at this section.")

        # ── Load job (and refuse completed/non-existent) ────────────────────
        from apps.filing_automation.models import FilingJob

        try:
            job = FilingJob.objects.select_related("plan_snapshot", "plan_snapshot__well").get(pk=job_id)
        except FilingJob.DoesNotExist:
            raise CommandError(f"FilingJob with pk={job_id} does not exist.")

        terminal_statuses = {"succeeded", "failed", "cancelled"}
        if job.status in terminal_statuses:
            raise CommandError(
                f"FilingJob {job_id} is already in terminal status {job.status!r}. "
                "Only queued or running jobs can be replayed. "
                "Create a new job or reset the status manually if you want to re-run."
            )

        # ── Resolve credential and form data ────────────────────────────────
        from django.conf import settings
        from apps.tenants.models import Tenant, TenantBusinessProfile
        from apps.intelligence.models import PortalCredential
        from apps.public_core.models import PlanSnapshot
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data
        from apps.filing_automation._vendor.regulagent_core.automation.base.data_models import AuthData
        from apps.filing_automation.tasks import _normalize_tenant_id, _run_filing

        tenant = Tenant.objects.get(id=job.tenant_id)

        try:
            credential_tenant_id = _normalize_tenant_id(tenant.id)
        except Exception:
            credential_tenant_id = tenant.id

        cred = PortalCredential.objects.filter(
            tenant_id=credential_tenant_id, agency="RRC", is_active=True
        ).first()
        if cred is None:
            raise CommandError(
                "No active RRC portal credential found for this tenant. "
                "Add credentials under Settings → Portal Credentials."
            )

        try:
            cred_username = cred.get_username()
            cred_password = cred.get_password()
        except Exception as exc:
            raise CommandError(f"Failed to decrypt portal credentials: {exc}") from exc

        auth = AuthData(username=cred_username, password=cred_password)

        snap = PlanSnapshot.objects.select_related("well").get(id=job.plan_snapshot_id)
        profile = TenantBusinessProfile.objects.filter(tenant=tenant).first()
        form_data, well_record = plan_snapshot_to_form_data(
            snap, job.attestation or {}, profile, enforce_profile=False
        )

        # Debug mode always uses test_mode so no real submission fires.
        form_data.test_mode = True
        self.stdout.write("  test_mode=True forced (debug runner never submits live).")

        self.stdout.write(
            self.style.NOTICE(
                f"\nLaunching headed browser for FilingJob {job_id} "
                f"(slow_mo={slow_mo}ms, inspector={inspector}) …\n"
                "  Close the browser window to exit.\n"
            )
        )

        # ── Run the filing ───────────────────────────────────────────────────
        try:
            result = asyncio.run(
                _run_filing(
                    auth,
                    form_data,
                    well_record,
                    job_id,
                    headless=False,
                    slow_mo=slow_mo,
                )
            )
        except KeyboardInterrupt:
            self.stdout.write("\nInterrupted by user.")
            sys.exit(0)
        except Exception as exc:
            raise CommandError(f"Filing run failed: {exc}") from exc

        if getattr(result, "success", False):
            self.stdout.write(
                self.style.SUCCESS(
                    f"Filing completed (draft/test mode). "
                    f"confirmation={result.confirmation_number!r}"
                )
            )
        else:
            self.stdout.write(
                self.style.ERROR(
                    f"Filing failed: {getattr(result, 'error', 'unknown error')}"
                )
            )
