import pytest
from django_tenants.utils import schema_context


@pytest.fixture
def tenant_admin(db, public_tenant, test_tenant):
    """
    Create an admin user and add them to test_tenant.

    django-tenant-users requires:
      1. A public-schema Tenant row to exist (public_tenant fixture).
      2. User.objects.create_user() to be called while the active DB schema
         is 'public' — it internally creates a UserTenantPermissions row in
         the public schema.

    We depend on public_tenant so it is guaranteed to exist, and wrap the
    create_user call in a public-schema context to satisfy (2).
    """
    from django_tenants.utils import get_public_schema_name
    from apps.tenants.models import User

    with schema_context(get_public_schema_name()):
        user = User.objects.create_user(
            email='admin@example.com',
            password='adminpass123',
            is_active=True,
        )
    test_tenant.add_user(user, is_superuser=True, is_staff=True)
    return user
