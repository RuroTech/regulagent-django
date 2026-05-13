"""
TDD: Failing tests for card #59 — auto-creation of ClientWorkspace on provision_tenant()
and the backfill_workspace_assignments management command.

These tests define expected behaviour that has NOT been implemented yet.
Running this file should produce failures because:
  - provision_tenant() does not yet create a ClientWorkspace
  - The management command backfill_workspace_assignments does not yet exist

All six tests should FAIL (not error) once the import issues below are resolved,
or ERROR at collection time — both are acceptable "red" signals.
"""

import uuid
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django_tenants.utils import schema_context, get_public_schema_name
from io import StringIO

from apps.tenants.models import ClientWorkspace, Tenant, Domain, User
from apps.tenants.utils import provision_tenant

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_owner(suffix: str) -> User:
    """Create a unique user in the public schema to act as tenant owner."""
    with schema_context(get_public_schema_name()):
        return User.objects.create_user(
            email=f"owner-{suffix}@example.com",
            password="testpass123",
            is_active=True,
        )


def _call_backfill(*args, **kwargs):
    """
    Thin wrapper around call_command for backfill_workspace_assignments.
    Returns stdout output as a string.
    """
    out = StringIO()
    call_command(
        "backfill_workspace_assignments",
        *args,
        stdout=out,
        stderr=StringIO(),
        **kwargs,
    )
    return out.getvalue()


# ---------------------------------------------------------------------------
# 1. provision_tenant() creates a default ClientWorkspace
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestProvisionTenantCreatesWorkspace:
    """
    After provision_tenant() is called, a ClientWorkspace must exist for the
    newly created tenant.
    """

    def test_provision_tenant_creates_default_workspace(self, db, public_tenant):
        """
        Calling provision_tenant() must automatically create exactly one
        ClientWorkspace for the new tenant.
        """
        uid = str(uuid.uuid4())[:8]
        owner = _make_owner(uid)

        tenant, _domain = provision_tenant(
            tenant_name=f"Auto Workspace Corp {uid}",
            tenant_slug=f"auto-ws-{uid}",
            schema_name=f"auto_ws_{uid}",
            owner=owner,
        )

        workspace_count = ClientWorkspace.objects.filter(tenant=tenant).count()

        # Clean up schema to avoid test pollution
        try:
            tenant.delete(force_drop=True)
        except Exception:
            pass

        assert workspace_count == 1, (
            f"Expected 1 ClientWorkspace after provision_tenant(), got {workspace_count}"
        )

    def test_default_workspace_named_after_tenant(self, db, public_tenant):
        """
        The auto-created workspace's name must equal the tenant's name.
        """
        uid = str(uuid.uuid4())[:8]
        owner = _make_owner(uid)
        tenant_name = f"Named Workspace Corp {uid}"

        tenant, _domain = provision_tenant(
            tenant_name=tenant_name,
            tenant_slug=f"named-ws-{uid}",
            schema_name=f"named_ws_{uid}",
            owner=owner,
        )

        workspace = ClientWorkspace.objects.filter(tenant=tenant).first()

        try:
            tenant.delete(force_drop=True)
        except Exception:
            pass

        assert workspace is not None, "No ClientWorkspace was created"
        assert workspace.name == tenant_name, (
            f"Expected workspace.name='{tenant_name}', got '{workspace.name}'"
        )
        assert workspace.is_active is True, (
            "Default workspace must be created with is_active=True"
        )

    def test_provision_is_idempotent(self, db, public_tenant):
        """
        provision_tenant() must create exactly one workspace. Calling
        get_or_create a second time with the same (tenant, name) must leave
        exactly 1 workspace — no duplicates.

        This test will FAIL until provision_tenant() creates the initial
        workspace, because we assert the count is 1 AFTER the first call and
        AGAIN after the idempotent second call.
        """
        uid = str(uuid.uuid4())[:8]
        owner = _make_owner(uid)
        tenant_name = f"Idempotent Corp {uid}"

        tenant, _domain = provision_tenant(
            tenant_name=tenant_name,
            tenant_slug=f"idempotent-{uid}",
            schema_name=f"idempotent_{uid}",
            owner=owner,
        )

        # provision_tenant() must have created the workspace already
        after_provision = ClientWorkspace.objects.filter(tenant=tenant).count()
        assert after_provision == 1, (
            f"provision_tenant() must create 1 workspace; found {after_provision}"
        )

        # Simulate a second call / retry by using get_or_create directly —
        # this is the same mechanism provision_tenant() must use internally.
        ClientWorkspace.objects.get_or_create(
            tenant=tenant,
            name=tenant_name,
            defaults={"is_active": True},
        )

        after_second_call = ClientWorkspace.objects.filter(tenant=tenant).count()

        try:
            tenant.delete(force_drop=True)
        except Exception:
            pass

        assert after_second_call == 1, (
            f"Expected 1 ClientWorkspace after idempotent second call, got {after_second_call}"
        )


# ---------------------------------------------------------------------------
# 2. backfill_workspace_assignments management command
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestBackfillWorkspaceAssignments:
    """
    Tests for the management command:  manage.py backfill_workspace_assignments

    The command must:
      - Create a default workspace for any tenant that has none.
      - Skip tenants that already have a workspace.
      - Honour --dry-run: log intent but make NO DB changes.
      - Honour --tenant <slug>: limit backfill to a single tenant.
    """

    def test_backfill_creates_workspace_for_tenant_without_one(
        self, db, public_tenant, test_tenant
    ):
        """
        A tenant with no workspaces must receive a default workspace after the
        backfill command runs.
        """
        # Ensure test_tenant starts with no workspaces
        ClientWorkspace.objects.filter(tenant=test_tenant).delete()
        assert ClientWorkspace.objects.filter(tenant=test_tenant).count() == 0

        _call_backfill()

        workspace_count = ClientWorkspace.objects.filter(tenant=test_tenant).count()
        assert workspace_count == 1, (
            f"Expected 1 ClientWorkspace after backfill, got {workspace_count}"
        )
        workspace = ClientWorkspace.objects.get(tenant=test_tenant)
        assert workspace.name == test_tenant.name
        assert workspace.is_active is True

    def test_backfill_dry_run_makes_no_changes(
        self, db, public_tenant, test_tenant
    ):
        """
        With --dry-run, the command must log what it would do but must NOT
        create any database records.
        """
        ClientWorkspace.objects.filter(tenant=test_tenant).delete()
        assert ClientWorkspace.objects.filter(tenant=test_tenant).count() == 0

        output = _call_backfill("--dry-run")

        after_count = ClientWorkspace.objects.filter(tenant=test_tenant).count()
        assert after_count == 0, (
            f"--dry-run must not create records; found {after_count} workspace(s)"
        )
        # The command should have logged that it *would* create something
        assert test_tenant.name in output or test_tenant.slug in output, (
            "--dry-run output should mention the tenant that would be backfilled"
        )

    def test_backfill_skips_tenant_that_already_has_workspace(
        self, db, public_tenant, test_tenant
    ):
        """
        A tenant that already has a workspace must not get a duplicate when
        the backfill command runs.
        """
        existing = ClientWorkspace.objects.create(
            tenant=test_tenant,
            name=test_tenant.name,
            is_active=True,
        )

        _call_backfill()

        workspace_count = ClientWorkspace.objects.filter(tenant=test_tenant).count()
        assert workspace_count == 1, (
            f"Backfill should not create duplicates; found {workspace_count} workspace(s)"
        )
        # The workspace that was there originally must still be the same one
        assert ClientWorkspace.objects.filter(pk=existing.pk).exists(), (
            "The original workspace should still exist after backfill"
        )

    def test_backfill_tenant_flag_limits_scope(
        self, db, public_tenant, test_tenant
    ):
        """
        When --tenant <slug> is provided, only that tenant is backfilled.
        Other tenants without workspaces must remain unchanged.
        """
        # Create a second tenant without a workspace
        uid2 = str(uuid.uuid4())[:8]
        other_tenant = Tenant.objects.create(
            name=f"Other Tenant {uid2}",
            slug=f"other-{uid2}",
            schema_name=f"other_{uid2}",
        )
        Domain.objects.create(
            domain=f"other-{uid2}.localhost",
            tenant=other_tenant,
            is_primary=True,
        )

        # Both tenants start with no workspaces
        ClientWorkspace.objects.filter(tenant=test_tenant).delete()
        ClientWorkspace.objects.filter(tenant=other_tenant).delete()

        try:
            # Run backfill for test_tenant only
            _call_backfill("--tenant", test_tenant.slug)

            test_tenant_count = ClientWorkspace.objects.filter(tenant=test_tenant).count()
            other_tenant_count = ClientWorkspace.objects.filter(tenant=other_tenant).count()

            assert test_tenant_count == 1, (
                f"test_tenant should have 1 workspace after targeted backfill, "
                f"got {test_tenant_count}"
            )
            assert other_tenant_count == 0, (
                f"other_tenant should be untouched when --tenant flag limits scope, "
                f"got {other_tenant_count}"
            )
        finally:
            try:
                other_tenant.delete(force_drop=True)
            except Exception:
                pass
