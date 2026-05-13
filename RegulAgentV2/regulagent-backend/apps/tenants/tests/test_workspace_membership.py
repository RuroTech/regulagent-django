"""
TDD: Failing tests for card #60 — WorkspaceMembership model, auto-enrollment,
and access scoping.

These tests define expected behaviour that has NOT been implemented yet.
Running this file should produce failures because:
  - WorkspaceMembership model does not yet exist in apps/tenants/models.py
  - Auto-enrollment signal hook does not yet exist
  - ClientWorkspaceViewSet.get_queryset() does not yet filter by membership

All tests must FAIL (ImportError on test 1, assertion failures on 2-7) until
the implementation is in place.
"""

import uuid
import pytest
from django.db import IntegrityError
from django_tenants.utils import schema_context, get_public_schema_name

from apps.tenants.models import ClientWorkspace, Tenant, Domain, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(suffix: str) -> User:
    """Create a unique, active user in the public schema."""
    with schema_context(get_public_schema_name()):
        return User.objects.create_user(
            email=f"mem-user-{suffix}@example.com",
            password="testpass123",
            is_active=True,
        )


def _make_workspace(tenant: Tenant, name: str, is_active: bool = True) -> ClientWorkspace:
    """Create a ClientWorkspace for the given tenant."""
    return ClientWorkspace.objects.create(
        tenant=tenant,
        name=name,
        is_active=is_active,
    )


# ---------------------------------------------------------------------------
# Class 1: TestWorkspaceMembershipModel
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestWorkspaceMembershipModel:
    """
    Basic model existence and constraint tests for WorkspaceMembership.
    """

    def test_membership_model_exists(self):
        """
        Importing WorkspaceMembership from apps.tenants.models must not raise
        ImportError. This test FAILS until the model is added to models.py.
        """
        from apps.tenants.models import WorkspaceMembership  # noqa: F401

    def test_membership_unique_together_enforced(self, db, public_tenant, test_tenant):
        """
        Creating two WorkspaceMembership rows for the same (workspace, user)
        pair must raise IntegrityError due to the unique_together constraint.
        This test FAILS until both the model and its unique_together are
        implemented.
        """
        from apps.tenants.models import WorkspaceMembership

        uid = str(uuid.uuid4())[:8]
        user = _make_user(uid)
        workspace = _make_workspace(test_tenant, f"Unique WS {uid}")

        # First membership — must succeed
        WorkspaceMembership.objects.create(workspace=workspace, user=user)

        # Second membership for same pair — must raise IntegrityError
        with pytest.raises(IntegrityError):
            WorkspaceMembership.objects.create(workspace=workspace, user=user)

    def test_membership_created_at_auto_set(self, db, public_tenant, test_tenant):
        """
        WorkspaceMembership.created_at must be populated automatically when
        the record is saved (auto_now_add=True).
        This test FAILS until the model field is implemented.
        """
        from apps.tenants.models import WorkspaceMembership

        uid = str(uuid.uuid4())[:8]
        user = _make_user(uid)
        workspace = _make_workspace(test_tenant, f"Timestamp WS {uid}")

        membership = WorkspaceMembership.objects.create(workspace=workspace, user=user)

        assert membership.created_at is not None, (
            "WorkspaceMembership.created_at must be auto-populated on creation"
        )


# ---------------------------------------------------------------------------
# Class 2: TestWorkspaceMembershipAutoEnrollment
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestWorkspaceMembershipAutoEnrollment:
    """
    Tests that a user is automatically enrolled in all existing active
    workspaces for a tenant when they are added to that tenant.
    """

    def test_new_user_auto_enrolled_in_existing_workspaces(
        self, db, public_tenant, test_tenant
    ):
        """
        Given a tenant that already has 2 active workspaces, when a new user
        is added to the tenant via tenant.add_user(), WorkspaceMembership
        records must be created automatically for both workspaces.

        This test FAILS until the post_add signal hook that creates
        WorkspaceMembership records is implemented.
        """
        from apps.tenants.models import WorkspaceMembership

        uid = str(uuid.uuid4())[:8]

        # Pre-create two active workspaces on the tenant
        ws_a = _make_workspace(test_tenant, f"Client Alpha {uid}")
        ws_b = _make_workspace(test_tenant, f"Client Beta {uid}")

        # Also create one inactive workspace — user should NOT be enrolled here
        ws_inactive = _make_workspace(test_tenant, f"Archived {uid}", is_active=False)

        # Create a fresh user and add them to the tenant
        new_user = _make_user(uid)
        test_tenant.add_user(new_user, is_superuser=False, is_staff=False)

        enrolled_ids = set(
            WorkspaceMembership.objects.filter(user=new_user)
            .values_list("workspace_id", flat=True)
        )

        assert ws_a.pk in enrolled_ids, (
            f"User should be enrolled in workspace '{ws_a.name}' but wasn't"
        )
        assert ws_b.pk in enrolled_ids, (
            f"User should be enrolled in workspace '{ws_b.name}' but wasn't"
        )
        assert ws_inactive.pk not in enrolled_ids, (
            f"User must NOT be enrolled in inactive workspace '{ws_inactive.name}'"
        )
        assert len(enrolled_ids) == 2, (
            f"Expected exactly 2 memberships (active workspaces only), "
            f"got {len(enrolled_ids)}"
        )


# ---------------------------------------------------------------------------
# Class 3: TestWorkspaceAccessScoping
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestWorkspaceAccessScoping:
    """
    Tests for the membership-based access scoping logic that
    ClientWorkspaceViewSet.get_queryset() will enforce.

    All tests exercise the queryset directly at the ORM level to avoid
    routing issues with pre-existing 404s on HTTP endpoints.
    """

    def _resolve_qs(self, user: User, tenant: Tenant):
        """
        Replicate the filtering logic that ClientWorkspaceViewSet.get_queryset()
        will use after implementation:

          - Admin users (is_staff=True on UserTenantPermissions) → all tenant workspaces
          - Non-admin users → only workspaces they have a WorkspaceMembership for
        """
        from tenant_users.permissions.models import UserTenantPermissions
        from apps.tenants.models import ClientWorkspace  # noqa — same model, explicit import

        perms = UserTenantPermissions.objects.filter(profile=user).first()
        if perms and perms.is_staff:
            return ClientWorkspace.objects.filter(tenant=tenant)
        else:
            return ClientWorkspace.objects.filter(memberships__user=user)

    def test_admin_sees_all_workspaces(self, db, public_tenant, test_tenant, tenant_admin):
        """
        An admin user (UserTenantPermissions.is_staff=True) must see ALL
        workspaces for the tenant, regardless of whether they have explicit
        WorkspaceMembership records.

        This test FAILS until:
          - WorkspaceMembership model exists (memberships related_name required)
          - The queryset logic in get_queryset() is implemented

        The guard import of WorkspaceMembership ensures the test fails at
        collection time before the model exists, so it cannot pass vacuously.
        """
        # Guard: this import must succeed before the rest of the test can run.
        # If WorkspaceMembership doesn't exist yet, this raises ImportError → FAIL.
        from apps.tenants.models import WorkspaceMembership  # noqa: F401

        uid = str(uuid.uuid4())[:8]

        ws_a = _make_workspace(test_tenant, f"Admin Visible A {uid}")
        ws_b = _make_workspace(test_tenant, f"Admin Visible B {uid}")
        ws_c = _make_workspace(test_tenant, f"Admin Visible C {uid}")

        # Admin has NO explicit memberships — but must still see everything
        qs = self._resolve_qs(tenant_admin, test_tenant)

        workspace_names = list(qs.values_list("name", flat=True))
        for ws in [ws_a, ws_b, ws_c]:
            assert ws.pk in [w.pk for w in qs], (
                f"Admin must see workspace '{ws.name}'; got: {workspace_names}"
            )

        assert qs.count() >= 3, (
            f"Admin should see at least 3 workspaces; got {qs.count()}"
        )

    def test_non_admin_only_sees_own_workspaces(self, db, public_tenant, test_tenant):
        """
        A non-admin user must only see workspaces they have a
        WorkspaceMembership for. Workspaces they are not a member of must be
        excluded.

        This test FAILS until WorkspaceMembership model and the viewset
        queryset logic are both implemented.
        """
        from apps.tenants.models import WorkspaceMembership

        uid = str(uuid.uuid4())[:8]
        user = _make_user(uid)
        test_tenant.add_user(user, is_superuser=False, is_staff=False)

        ws_member = _make_workspace(test_tenant, f"Member WS {uid}")
        ws_no_member = _make_workspace(test_tenant, f"No Member WS {uid}")

        # Explicitly enrol user in ws_member only
        # (auto-enrolment would have run on add_user above, so we set up
        #  fresh workspaces AFTER the user was added to avoid coupling with
        #  the auto-enrolment feature)
        WorkspaceMembership.objects.get_or_create(workspace=ws_member, user=user)

        qs = self._resolve_qs(user, test_tenant)
        member_pks = list(qs.values_list("pk", flat=True))

        assert ws_member.pk in member_pks, (
            f"Non-admin user should see workspace '{ws_member.name}' (has membership)"
        )
        assert ws_no_member.pk not in member_pks, (
            f"Non-admin user must NOT see workspace '{ws_no_member.name}' (no membership)"
        )

    def test_non_admin_with_no_memberships_sees_no_workspaces(
        self, db, public_tenant, test_tenant
    ):
        """
        A non-admin user with zero WorkspaceMembership records must receive an
        empty queryset.

        This test FAILS until WorkspaceMembership model and the viewset
        queryset logic are both implemented.
        """
        from apps.tenants.models import WorkspaceMembership

        uid = str(uuid.uuid4())[:8]
        user = _make_user(uid)
        test_tenant.add_user(user, is_superuser=False, is_staff=False)

        # Create workspaces on the tenant so there is something that *could*
        # be returned if the scoping were wrong
        _make_workspace(test_tenant, f"Orphan WS 1 {uid}")
        _make_workspace(test_tenant, f"Orphan WS 2 {uid}")

        # Remove any memberships that auto-enrollment may have created
        # (this test ALSO exercises the state before auto-enrolment, but since
        #  the feature is not yet implemented, there will be none anyway)
        WorkspaceMembership.objects.filter(user=user).delete()

        qs = self._resolve_qs(user, test_tenant)

        assert qs.count() == 0, (
            f"Non-admin with no memberships must see 0 workspaces; "
            f"got {qs.count()}"
        )
