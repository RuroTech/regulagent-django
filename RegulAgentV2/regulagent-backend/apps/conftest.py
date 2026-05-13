import pytest
from django.db import connection
from django_tenants.utils import schema_context

@pytest.fixture
def public_tenant(db):
    """Get or create the public tenant."""
    from apps.tenants.models import Tenant, Domain
    tenant = Tenant.objects.filter(schema_name='public').first()
    if not tenant:
        tenant = Tenant.objects.create(
            name='Public',
            slug='public',
            schema_name='public'
        )
        Domain.objects.create(domain='localhost', tenant=tenant, is_primary=True)
    return tenant

@pytest.fixture
def test_tenant(db, public_tenant):
    """
    Create an isolated test tenant with schema.
    Automatically cleans up on teardown.

    Depends on public_tenant to ensure the public schema tenant row exists
    before any user creation occurs (User.objects.create_user internally
    links the new user to the public tenant).
    """
    from apps.tenants.models import Tenant, Domain
    import uuid

    # Use unique schema name to avoid conflicts
    unique_id = str(uuid.uuid4())[:8]
    schema_name = f'test_{unique_id}'

    tenant = Tenant.objects.create(
        name=f'Test Tenant {unique_id}',
        slug=f'test-{unique_id}',
        schema_name=schema_name
    )
    Domain.objects.create(
        domain=f'test-{unique_id}.localhost',
        tenant=tenant,
        is_primary=True
    )

    yield tenant

    # Cleanup: delete tenant and drop schema
    try:
        tenant.delete(force_drop=True)
    except Exception as e:
        # Log but don't fail test on cleanup error
        print(f"Warning: Failed to cleanup tenant {schema_name}: {e}")

@pytest.fixture
def test_user(db, public_tenant):
    """Create a test user in public schema."""
    from apps.tenants.models import User
    user = User.objects.create_user(
        email='test@example.com',
        password='testpass123',
        is_active=True
    )
    return user

@pytest.fixture
def tenant_user(db, test_tenant):
    """Create a user within test tenant context."""
    from apps.tenants.models import User
    with schema_context(test_tenant.schema_name):
        user = User.objects.create_user(
            email='tenant@example.com',
            password='testpass123',
            is_active=True
        )
    test_tenant.add_user(user, is_superuser=False, is_staff=False)
    return user

@pytest.fixture
def tenant_context(test_tenant):
    """Context manager for tenant schema. Use with 'with' statement."""
    with schema_context(test_tenant.schema_name):
        yield test_tenant

@pytest.fixture
def well_with_data(db, public_tenant):
    """
    Create a WellRegistry entry with associated data (plan snapshot, extractions).
    Useful for integration tests.
    """
    from apps.public_core.models import WellRegistry, PlanSnapshot

    well = WellRegistry.objects.create(
        api14='42501705750000',
        state='TX',
        county='Andrews',
        district='08A',
        operator_name='Test Operator Inc',
        field_name='Test Field',
        lease_name='Test Lease',
        well_number='1'
    )

    # Create a plan snapshot for the well
    plan_snapshot = PlanSnapshot.objects.create(
        well=well,
        plan_id=f'{well.api14}:combined',
        kind='baseline',
        status='draft',
        payload={
            'steps': [
                {
                    'step_id': 1,
                    'description': 'Test plug',
                    'top_md': 5000,
                    'bottom_md': 5100
                }
            ],
            'kernel_version': '1.0'
        }
    )

    return {
        'well': well,
        'plan_snapshot': plan_snapshot
    }

@pytest.fixture
def authenticated_tenant_client(api_client, tenant_user, test_tenant):
    """
    Return authenticated APIClient within tenant context.
    Use this for tenant-specific API tests.
    """
    from rest_framework_simplejwt.tokens import RefreshToken
    from django_tenants.utils import schema_context

    with schema_context(test_tenant.schema_name):
        refresh = RefreshToken.for_user(tenant_user)
        api_client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        # Set tenant in request middleware simulation
        api_client.defaults['HTTP_HOST'] = test_tenant.get_primary_domain().domain

    return api_client
