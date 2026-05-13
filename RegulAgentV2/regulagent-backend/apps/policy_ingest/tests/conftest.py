"""
Conftest for policy_ingest tests.

Sets up a Domain entry mapping 'testserver' (the default HTTP_HOST used by
Django's test client / DRF APIClient) to the public tenant, so that
TenantMainMiddleware routes requests to the public schema instead of
returning 404.
"""

import pytest


@pytest.fixture(autouse=True)
def public_tenant_testserver(db):
    """
    Register 'testserver' as a domain for the public tenant.

    TenantMainMiddleware looks up the incoming Host header in the Domain
    table; if no match is found it returns 404.  The Django test client
    and DRF APIClient both use 'testserver' as the default Host, so we
    need this entry to exist before any API call is made.
    """
    from apps.tenants.models import Tenant, Domain

    # The public tenant is auto-created by migrate_schemas; just fetch it.
    tenant = Tenant.objects.filter(schema_name="public").first()
    if tenant is None:
        # Fallback: get whatever tenant is first (shouldn't normally happen)
        tenant = Tenant.objects.first()

    if tenant is None:
        # No tenant at all — skip domain registration (non-API tests will
        # still pass; API tests may fail due to the underlying DB state).
        return

    # Register 'testserver' domain so APIClient requests resolve to public schema
    Domain.objects.get_or_create(
        domain="testserver",
        defaults={"tenant": tenant, "is_primary": False},
    )
