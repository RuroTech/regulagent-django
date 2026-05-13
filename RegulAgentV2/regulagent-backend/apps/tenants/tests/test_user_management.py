"""
TDD: Failing tests for user management endpoints.

These tests define the expected behaviour of three endpoints that have NOT
been implemented yet.  Running this file should produce collection errors or
ImportErrors because the referenced views and serializers do not exist.

Endpoints under test
--------------------
GET  /api/tenant/users/          — list tenant users + seat summary
POST /api/tenant/users/          — create a new user (seat-limit enforced)
PATCH /api/tenant/users/<id>/deactivate/ — deactivate a user
"""

import pytest
from django.urls import reverse
from rest_framework import status
from django_tenants.utils import schema_context

# ---------------------------------------------------------------------------
# These imports MUST fail until the implementation is delivered.
# The ImportError is the "red" signal that keeps tests failing correctly.
# ---------------------------------------------------------------------------
from apps.tenants.views import TenantUserListCreateView, TenantUserDeactivateView  # noqa: F401
from apps.tenants.serializers import UserListSerializer, UserCreateSerializer  # noqa: F401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(tenant, email, password="pass1234!", is_active=True, **kwargs):
    """Create a User inside *tenant*'s schema and add them to the tenant."""
    from apps.tenants.models import User

    with schema_context(tenant.schema_name):
        user = User.objects.create_user(
            email=email,
            password=password,
            is_active=is_active,
            **kwargs,
        )
    tenant.add_user(user, is_superuser=False, is_staff=False)
    return user


def _admin_client(api_client, admin_user):
    """Return an APIClient authenticated as *admin_user* via JWT."""
    from rest_framework_simplejwt.tokens import RefreshToken

    refresh = RefreshToken.for_user(admin_user)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    return api_client


# ---------------------------------------------------------------------------
# GET /api/tenant/users/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestListTenantUsers:
    """GET /api/tenant/users/ — list users and seat summary."""

    def test_list_returns_200_with_users_and_seats(
        self, api_client, test_tenant, tenant_admin
    ):
        """Happy path: authenticated admin gets 200 with expected shape."""
        client = _admin_client(api_client, tenant_admin)

        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-users-list")
            response = client.get(url)

        assert response.status_code == status.HTTP_200_OK

        data = response.json()
        assert "users" in data, "Response must contain a 'users' key"
        assert "seats" in data, "Response must contain a 'seats' key"

        seats = data["seats"]
        assert "used" in seats
        assert "limit" in seats
        assert "available" in seats

    def test_list_returns_correct_user_fields(
        self, api_client, test_tenant, tenant_admin
    ):
        """Each user entry must expose the documented field set."""
        _make_user(
            test_tenant,
            email="alice@example.com",
            first_name="Alice",
            last_name="Smith",
        )

        client = _admin_client(api_client, tenant_admin)

        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-users-list")
            response = client.get(url)

        assert response.status_code == status.HTTP_200_OK

        users = response.json()["users"]
        assert len(users) >= 1

        user_entry = next(
            (u for u in users if u["email"] == "alice@example.com"), None
        )
        assert user_entry is not None, "alice@example.com must appear in the list"

        for field in ("id", "email", "first_name", "last_name", "title", "is_active"):
            assert field in user_entry, f"Missing field '{field}' in user entry"

    def test_list_requires_authentication(self, api_client, test_tenant):
        """Unauthenticated requests must receive 401."""
        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-users-list")
            response = api_client.get(url)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_only_returns_users_in_same_tenant(
        self, api_client, test_tenant, tenant_admin
    ):
        """Users belonging to a *different* tenant must not appear."""
        from apps.tenants.models import Tenant, Domain
        import uuid

        uid = str(uuid.uuid4())[:8]
        other_tenant = Tenant.objects.create(
            name=f"Other Tenant {uid}",
            slug=f"other-{uid}",
            schema_name=f"other_{uid}",
        )
        Domain.objects.create(
            domain=f"other-{uid}.localhost",
            tenant=other_tenant,
            is_primary=True,
        )

        try:
            _make_user(other_tenant, email="outsider@other.com")

            client = _admin_client(api_client, tenant_admin)

            with schema_context(test_tenant.schema_name):
                url = reverse("tenant-users-list")
                response = client.get(url)

            assert response.status_code == status.HTTP_200_OK

            emails = [u["email"] for u in response.json()["users"]]
            assert "outsider@other.com" not in emails
        finally:
            try:
                other_tenant.delete(force_drop=True)
            except Exception:
                pass

    def test_seat_summary_counts_reflect_active_users(
        self, api_client, test_tenant, tenant_admin
    ):
        """seats.used must equal the number of active users in the tenant."""
        from apps.tenants.models import TenantPlan
        from plans.models import Plan

        plan, _ = Plan.objects.get_or_create(
            name="Test Plan", defaults={"slug": "test-plan"}
        )
        TenantPlan.objects.update_or_create(
            tenant=test_tenant,
            defaults={"plan": plan, "user_limit": 10},
        )

        _make_user(test_tenant, email="active1@example.com", is_active=True)
        _make_user(test_tenant, email="inactive1@example.com", is_active=False)

        client = _admin_client(api_client, tenant_admin)

        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-users-list")
            response = client.get(url)

        assert response.status_code == status.HTTP_200_OK

        seats = response.json()["seats"]
        # tenant_admin (created in conftest) + active1 = 2 active users
        assert seats["used"] == 2
        assert seats["limit"] == 10
        assert seats["available"] == 8


# ---------------------------------------------------------------------------
# POST /api/tenant/users/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCreateTenantUser:
    """POST /api/tenant/users/ — create a new user."""

    def test_create_user_returns_201_with_temp_password(
        self, api_client, test_tenant, tenant_admin
    ):
        """Happy path: valid payload returns 201 and includes temp_password."""
        client = _admin_client(api_client, tenant_admin)

        payload = {
            "email": "newuser@example.com",
            "first_name": "New",
            "last_name": "User",
            "title": "Engineer",
        }

        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-users-list")
            response = client.post(url, payload, format="json")

        assert response.status_code == status.HTTP_201_CREATED

        data = response.json()
        assert data["email"] == "newuser@example.com"
        assert data["first_name"] == "New"
        assert data["last_name"] == "User"
        assert "temp_password" in data, "Response must include a temp_password field"
        assert data["temp_password"], "temp_password must be non-empty"

    def test_create_user_persists_to_database(
        self, api_client, test_tenant, tenant_admin
    ):
        """The new user must exist in the database after creation."""
        from apps.tenants.models import User

        client = _admin_client(api_client, tenant_admin)

        payload = {
            "email": "persisted@example.com",
            "first_name": "Persist",
            "last_name": "Check",
        }

        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-users-list")
            client.post(url, payload, format="json")

            assert User.objects.filter(email="persisted@example.com").exists()

    def test_create_user_fails_when_seat_limit_reached(
        self, api_client, test_tenant, tenant_admin
    ):
        """Returns 400 when active-user count has already reached user_limit."""
        from apps.tenants.models import TenantPlan
        from plans.models import Plan

        plan, _ = Plan.objects.get_or_create(
            name="Seat Limit Plan", defaults={"slug": "seat-limit-plan"}
        )
        # tenant_admin already counts as 1 active user; limit to 1 to block new users
        TenantPlan.objects.update_or_create(
            tenant=test_tenant,
            defaults={"plan": plan, "user_limit": 1},
        )

        client = _admin_client(api_client, tenant_admin)

        payload = {
            "email": "overflow@example.com",
            "first_name": "Over",
            "last_name": "Flow",
        }

        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-users-list")
            response = client.post(url, payload, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_create_user_fails_on_duplicate_email(
        self, api_client, test_tenant, tenant_admin
    ):
        """Returns 400 when the email address already exists."""
        _make_user(test_tenant, email="duplicate@example.com")

        client = _admin_client(api_client, tenant_admin)

        payload = {
            "email": "duplicate@example.com",
            "first_name": "Dup",
            "last_name": "User",
        }

        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-users-list")
            response = client.post(url, payload, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_create_user_requires_authentication(self, api_client, test_tenant):
        """Unauthenticated requests must receive 401."""
        payload = {
            "email": "anon@example.com",
            "first_name": "Anon",
            "last_name": "User",
        }

        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-users-list")
            response = api_client.post(url, payload, format="json")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_create_user_requires_email_field(
        self, api_client, test_tenant, tenant_admin
    ):
        """Missing email must produce a 400 validation error."""
        client = _admin_client(api_client, tenant_admin)

        payload = {"first_name": "No", "last_name": "Email"}

        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-users-list")
            response = client.post(url, payload, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# PATCH /api/tenant/users/<id>/deactivate/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestDeactivateTenantUser:
    """PATCH /api/tenant/users/<id>/deactivate/ — deactivate a user."""

    def test_deactivate_sets_is_active_false(
        self, api_client, test_tenant, tenant_admin
    ):
        """Happy path: target user is set to is_active=False."""
        from apps.tenants.models import User

        target = _make_user(test_tenant, email="target@example.com", is_active=True)

        client = _admin_client(api_client, tenant_admin)

        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-user-deactivate", kwargs={"id": target.id})
            response = client.patch(url)

        assert response.status_code == status.HTTP_200_OK

        target.refresh_from_db()
        assert target.is_active is False

    def test_deactivate_returns_updated_user(
        self, api_client, test_tenant, tenant_admin
    ):
        """Response body must include the updated user with is_active=False."""
        target = _make_user(test_tenant, email="updatecheck@example.com", is_active=True)

        client = _admin_client(api_client, tenant_admin)

        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-user-deactivate", kwargs={"id": target.id})
            response = client.patch(url)

        assert response.status_code == status.HTTP_200_OK

        data = response.json()
        assert data["id"] == target.id
        assert data["is_active"] is False

    def test_deactivate_returns_404_for_user_not_in_tenant(
        self, api_client, test_tenant, tenant_admin
    ):
        """Users outside the current tenant must return 404."""
        from apps.tenants.models import Tenant, Domain, User
        import uuid

        uid = str(uuid.uuid4())[:8]
        foreign_tenant = Tenant.objects.create(
            name=f"Foreign {uid}",
            slug=f"foreign-{uid}",
            schema_name=f"foreign_{uid}",
        )
        Domain.objects.create(
            domain=f"foreign-{uid}.localhost",
            tenant=foreign_tenant,
            is_primary=True,
        )

        try:
            foreign_user = _make_user(foreign_tenant, email="foreign@other.com")

            client = _admin_client(api_client, tenant_admin)

            with schema_context(test_tenant.schema_name):
                url = reverse(
                    "tenant-user-deactivate", kwargs={"id": foreign_user.id}
                )
                response = client.patch(url)

            assert response.status_code == status.HTTP_404_NOT_FOUND
        finally:
            try:
                foreign_tenant.delete(force_drop=True)
            except Exception:
                pass

    def test_deactivate_cannot_deactivate_self(
        self, api_client, test_tenant, tenant_admin
    ):
        """An admin must not be able to deactivate their own account."""
        client = _admin_client(api_client, tenant_admin)

        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-user-deactivate", kwargs={"id": tenant_admin.id})
            response = client.patch(url)

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_deactivate_requires_authentication(self, api_client, test_tenant):
        """Unauthenticated requests must receive 401."""
        target = _make_user(test_tenant, email="nonauthdeactivate@example.com")

        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-user-deactivate", kwargs={"id": target.id})
            response = api_client.patch(url)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
