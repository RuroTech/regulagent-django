"""
Seed a test PlanSnapshot in `engineer_approved` status so we can exercise the
W3A auto-filing pipeline end-to-end (POST /api/w3a/<snap_id>/submit/).

Idempotent by default — re-running reuses the most recent engineer_approved
snapshot for the same well + workspace. Pass --force-new to always create a
fresh row.

Usage examples (run inside the web container):

    python manage.py seed_test_w3a_snapshot
    python manage.py seed_test_w3a_snapshot --tenant demo --api14 42-329-12345-00-00
    python manage.py seed_test_w3a_snapshot --payload-file /tmp/my_payload.json
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.public_core.models.plan_snapshot import PlanSnapshot
from apps.public_core.models.well_registry import WellRegistry
from apps.tenants.context import set_current_tenant
from apps.tenants.models import ClientWorkspace, Tenant


DEFAULT_TENANT_SLUG = "demo"
DEFAULT_API14 = "42-329-12345-00-00"
DEFAULT_OPERATOR = "Demo Operator LLC"
DEFAULT_LEASE = "Demo Lease #1"
DEFAULT_FIELD = "Demo Field"
DEFAULT_DISTRICT = "7C"
DEFAULT_PERMIT = "123456"
DEFAULT_WORKSPACE_NAME = "demo-default"


def _build_default_payload(api14: str, district: str) -> dict:
    """Synthetic payload shaped to satisfy the adapter contract.

    Matches the keys read by `apps.filing_automation.services.adapter.
    plan_snapshot_to_form_data` (inputs_summary.api14, steps, geometry.*,
    district) and its test fixtures in tests/test_adapter.py.
    """
    return {
        "jurisdiction": "TX",
        "form": "W-3A",
        "district": district,
        "inputs_summary": {"api14": api14},
        "steps": [
            {
                "type": "plug",
                "top_ft": 3000,
                "bottom_ft": 3100,
                "cement_class": "C",
                "sacks": 50,
                "formation": "Wolfcamp",
            },
            {
                "type": "plug",
                "top_ft": 1500,
                "bottom_ft": 1600,
                "cement_class": "A",
                "sacks": 30,
                "formation": "Spraberry",
            },
            {
                "type": "plug",
                "top_ft": 0,
                "bottom_ft": 50,
                "cement_class": "A",
                "sacks": 10,
                "formation": "Surface",
            },
        ],
        "geometry": {
            "formation_tops": [
                {"name": "Wolfcamp", "depth_ft": 3000},
                {"name": "Spraberry", "depth_ft": 1500},
            ],
            "mechanical_barriers": [
                {"type": "CIBP", "depth_ft": 2800, "description": "existing CIBP"},
            ],
            "casing_record": [
                {
                    "grade": "K-55",
                    "weight_ppf": 24,
                    "top_ft": 0,
                    "bottom_ft": 1200,
                    "role": "surface",
                    "size_in": 13.375,
                },
                {
                    "grade": "L-80",
                    "weight_ppf": 29,
                    "top_ft": 0,
                    "bottom_ft": 7000,
                    "role": "production",
                    "size_in": 7.0,
                },
            ],
        },
    }


class Command(BaseCommand):
    help = "Seed a test PlanSnapshot in engineer_approved status for W3A submit-flow testing."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=DEFAULT_TENANT_SLUG,
                            help="Tenant schema_name (default: demo)")
        parser.add_argument("--api14", default=DEFAULT_API14,
                            help="Well API14 (default: synthetic stable value)")
        parser.add_argument("--operator", default=DEFAULT_OPERATOR,
                            help="Operator name on the Well row")
        parser.add_argument("--lease", default=DEFAULT_LEASE,
                            help="Lease name on the Well row")
        parser.add_argument("--field", default=DEFAULT_FIELD, dest="field_name",
                            help="Field name on the Well row")
        parser.add_argument("--district", default=DEFAULT_DISTRICT,
                            help="RRC district (default: 7C)")
        parser.add_argument("--permit-number", default=DEFAULT_PERMIT, dest="permit_number",
                            help=("RRC permit number — NOTE: WellRegistry has no "
                                  "permit_number field, so this is echoed into the "
                                  "snapshot.payload only."))
        parser.add_argument("--payload-file", default=None, dest="payload_file",
                            help="Path to a JSON file with a full payload dict (verbatim).")
        parser.add_argument("--workspace", default=None, dest="workspace_slug",
                            help=("ClientWorkspace name to attach. If omitted, picks "
                                  "the first workspace in the tenant or creates "
                                  "'demo-default'."))
        parser.add_argument("--force-new", action="store_true", dest="force_new",
                            help="Always create a brand-new snapshot row.")

    def handle(self, *args, **opts):
        tenant_slug: str = opts["tenant"]
        api14: str = opts["api14"]
        operator: str = opts["operator"]
        lease: str = opts["lease"]
        field_name: str = opts["field_name"]
        district: str = opts["district"]
        permit_number: str = opts["permit_number"]
        payload_file: str | None = opts["payload_file"]
        workspace_slug: str | None = opts["workspace_slug"]
        force_new: bool = opts["force_new"]

        # --- Tenant resolution ---
        try:
            tenant = Tenant.objects.get(schema_name=tenant_slug)
        except Tenant.DoesNotExist as e:
            raise CommandError(
                f"Tenant with schema_name='{tenant_slug}' not found. "
                f"Available: {list(Tenant.objects.values_list('schema_name', flat=True))}"
            ) from e

        set_current_tenant(tenant)

        # --- Payload ---
        if payload_file:
            p = Path(payload_file)
            if not p.exists():
                raise CommandError(f"--payload-file not found: {payload_file}")
            try:
                payload = json.loads(p.read_text())
            except json.JSONDecodeError as e:
                raise CommandError(f"--payload-file is not valid JSON: {e}") from e
            if not isinstance(payload, dict):
                raise CommandError("--payload-file must contain a JSON object at the top level.")
            # If they didn't put api14 in inputs_summary, splice ours in.
            payload.setdefault("inputs_summary", {}).setdefault("api14", api14)
        else:
            payload = _build_default_payload(api14, district)

        # Echo permit_number into the payload so it isn't silently lost — WellRegistry
        # has no permit_number column, so this is the only place it can live.
        payload.setdefault("inputs_summary", {})["permit_number"] = permit_number

        with transaction.atomic():
            # --- Workspace ---
            if workspace_slug:
                workspace, ws_created = ClientWorkspace.objects.get_or_create(
                    tenant=tenant,
                    name=workspace_slug,
                    defaults={"description": "Created by seed_test_w3a_snapshot"},
                )
            else:
                workspace = ClientWorkspace.objects.filter(tenant=tenant).order_by("created_at").first()
                if workspace is None:
                    workspace, _ws_created = ClientWorkspace.objects.get_or_create(
                        tenant=tenant,
                        name=DEFAULT_WORKSPACE_NAME,
                        defaults={"description": "Auto-created by seed_test_w3a_snapshot"},
                    )
                    ws_created = True
                else:
                    ws_created = False

            # --- Well (WellRegistry) ---
            # WellRegistry has NO permit_number column — log what we did write.
            well, well_created = WellRegistry.objects.get_or_create(
                api14=api14,
                defaults={
                    "state": "TX",
                    "district": district,
                    "operator_name": operator,
                    "field_name": field_name,
                    "lease_name": lease,
                    "workspace": workspace,
                },
            )
            if not well_created:
                # Update mutable display fields on existing rows so repeat runs
                # converge on the requested values without exploding on the
                # unique api14 constraint.
                updated_fields: list[str] = []
                for attr, val in (
                    ("district", district),
                    ("operator_name", operator),
                    ("field_name", field_name),
                    ("lease_name", lease),
                ):
                    if getattr(well, attr) != val:
                        setattr(well, attr, val)
                        updated_fields.append(attr)
                if well.workspace_id is None:
                    well.workspace = workspace
                    updated_fields.append("workspace")
                if updated_fields:
                    well.save(update_fields=updated_fields)

            # --- Snapshot ---
            existing = None
            if not force_new:
                existing = (
                    PlanSnapshot.objects
                    .filter(
                        well=well,
                        workspace=workspace,
                        status=PlanSnapshot.STATUS_ENGINEER_APPROVED,
                    )
                    .order_by("-created_at")
                    .first()
                )

            if existing is not None:
                existing.payload = payload
                existing.save(update_fields=["payload"])
                snap = existing
                snap_action = "reused"
            else:
                snap = PlanSnapshot.objects.create(
                    well=well,
                    plan_id=f"seed-w3a-{uuid.uuid4().hex[:12]}",
                    kind=PlanSnapshot.KIND_POST_EDIT,
                    payload=payload,
                    tenant_id=tenant.id,
                    workspace=workspace,
                    visibility=PlanSnapshot.VISIBILITY_PRIVATE,
                    status=PlanSnapshot.STATUS_ENGINEER_APPROVED,
                )
                snap_action = "created"

        # --- Report ---
        out = self.stdout.write
        out(self.style.SUCCESS(f"=== seed_test_w3a_snapshot: {snap_action.upper()} ==="))
        out(f"  tenant         : {tenant.schema_name} (id={tenant.id})")
        out(f"  workspace      : {workspace.name} (id={workspace.id}) "
            f"{'[created]' if ws_created else '[existing]'}")
        out(f"  well api14     : {well.api14} "
            f"{'[created]' if well_created else '[existing]'}")
        out(f"    state        : {well.state}")
        out(f"    district     : {well.district}")
        out(f"    operator     : {well.operator_name}")
        out(f"    lease        : {well.lease_name}")
        out(f"    field        : {well.field_name}")
        out(self.style.WARNING(
            f"  NOTE: WellRegistry has no permit_number column; "
            f"permit_number={permit_number!r} was stored on snapshot.payload.inputs_summary."
        ))
        out(f"  snapshot id    : {snap.id}")
        out(f"  snapshot status: {snap.status}")
        out(f"  snapshot kind  : {snap.kind}")
        out(f"  plan_id        : {snap.plan_id}")
        out("")
        out(self.style.MIGRATE_HEADING("Next steps:"))
        out("  1) Mint a JWT for the demo user (adjust email if needed):")
        out("     docker compose -f compose.dev.yml exec web python -c \\")
        out("       \"from rest_framework_simplejwt.tokens import RefreshToken; "
            "from django.contrib.auth import get_user_model; "
            "U=get_user_model(); "
            "print(str(RefreshToken.for_user("
            "U.objects.get(email__iexact='demo@example.com')).access_token))\"")
        out("")
        out("  2) Submit the snapshot through the W3A pipeline:")
        out(f"     curl -X POST http://127.0.0.1:8001/api/w3a/{snap.id}/submit/ \\")
        out("       -H \"Authorization: Bearer <JWT>\" \\")
        out("       -H \"Content-Type: application/json\" \\")
        out("       -d '{\"submitter_name\":\"Demo User\",\"submitter_title\":\"Operations\","
            "\"certification_checked\":true}'")
