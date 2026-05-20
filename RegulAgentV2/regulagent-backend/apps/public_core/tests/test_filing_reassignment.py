"""
TDD: Failing tests for card #61 — move/reassign draft filings between workspaces.

Scope: draft W3FormORM records only. Portal-synced FilingStatusRecord records are
out of scope (they follow their well's workspace).

Expected implementation: a PATCH on the W3FormORM API endpoint
(PATCH /api/w3/forms/<id>/) that accepts a `workspace` field and updates the FK
to a different active workspace within the same tenant, rejecting cross-tenant or
inactive-workspace targets.

RED STATE before implementation:

1. test_w3form_has_workspace_field — PASSES (FK already declared). Structural canary.
2. test_patch_filing_workspace_succeeds — FAILS: serializer ignores `workspace` field.
3. test_patch_filing_workspace_cross_tenant_rejected — FAILS: no cross-tenant guard.
4. test_patch_filing_workspace_to_inactive_workspace_rejected — FAILS: no validation.

Run:
    docker compose -f compose.dev.yml exec web python -m pytest \
        apps/public_core/tests/test_filing_reassignment.py -v
"""

from __future__ import annotations

import uuid
import pytest
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.tenants.models import Tenant, Domain, ClientWorkspace
from apps.public_core.models import W3FormORM, WellRegistry


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _unique() -> str:
    return uuid.uuid4().hex[:8]


def _make_workspace(tenant: Tenant, name: str, is_active: bool = True) -> ClientWorkspace:
    return ClientWorkspace.objects.create(
        tenant=tenant,
        name=name,
        is_active=is_active,
    )


def _make_well() -> WellRegistry:
    unique = str(uuid.uuid4().int)[:10]
    return WellRegistry.objects.create(
        api14=f"4250{unique}",
        state="TX",
        county="Andrews",
        operator_name=f"Test Operator {unique}",
    )


def _make_draft_form(well: WellRegistry, workspace: ClientWorkspace) -> W3FormORM:
    return W3FormORM.objects.create(
        well=well,
        api_number=well.api14,
        status="draft",
        workspace=workspace,
        tenant_id=workspace.tenant.id,
        form_data={},
    )


def _make_user(suffix: str = ""):
    """Create a unique user in the public schema."""
    from apps.tenants.models import User

    with schema_context(get_public_schema_name()):
        return User.objects.create_user(
            email=f"filing-qa-{suffix or _unique()}@example.com",
            password="testpass123",
            is_active=True,
        )


def _authenticated_client(user) -> APIClient:
    client = APIClient()
    refresh = RefreshToken.for_user(user)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    return client


def _make_lightweight_tenant(suffix: str) -> Tenant:
    """
    Create a Tenant record with auto_create_schema=False so no Postgres DDL
    is issued. Safe to create inline in non-transactional db tests.
    """
    with schema_context(get_public_schema_name()):
        t = Tenant(
            name=f"Light Corp {suffix}",
            slug=f"light-corp-{suffix}",
            schema_name=f"light_corp_{suffix}",
        )
        t.auto_create_schema = False
        t.save()
        Domain.objects.create(
            domain=f"light-corp-{suffix}.localhost",
            tenant=t,
            is_primary=True,
        )
    return t


# ---------------------------------------------------------------------------
# Test 1: Structural (no DB needed)
# ---------------------------------------------------------------------------

def test_w3form_has_workspace_field():
    """
    W3FormORM must expose a `workspace` FK to ClientWorkspace.

    PASSES immediately — the FK is already declared on the model.
    Acts as a structural canary: removing the field would break this test.
    """
    from django.db.models import ForeignKey

    field = W3FormORM._meta.get_field("workspace")
    assert isinstance(field, ForeignKey), (
        f"W3FormORM.workspace must be a ForeignKey; got {type(field).__name__}"
    )
    assert field.related_model is ClientWorkspace, (
        f"W3FormORM.workspace FK must reference ClientWorkspace; got {field.related_model}"
    )


# ---------------------------------------------------------------------------
# Test 2: Happy path
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_patch_filing_workspace_succeeds(public_tenant, test_tenant):
    """
    PATCH /api/w3/forms/<id>/ with {"workspace": workspace_b_id} must update
    the filing's workspace when both workspaces belong to the same tenant and
    workspace_b is active.

    FAILS (red) because W3FormORM_CreateUpdateSerializer omits `workspace` from
    its `fields` list — the value is silently ignored and the workspace stays as
    workspace_a.
    """
    user = _make_user()

    workspace_a = _make_workspace(test_tenant, f"Client A {_unique()}")
    workspace_b = _make_workspace(test_tenant, f"Client B {_unique()}")
    well = _make_well()
    form = _make_draft_form(well, workspace_a)

    client = _authenticated_client(user)
    response = client.patch(
        f"/api/w3/forms/{form.id}/",
        {"workspace": workspace_b.id},
        format="json",
    )

    assert response.status_code in (200, 204), (
        f"Expected 200/204 on valid workspace reassignment, "
        f"got {response.status_code}: {getattr(response, 'data', '')}"
    )

    form.refresh_from_db()
    assert form.workspace_id == workspace_b.id, (
        f"Expected form.workspace_id={workspace_b.id} after PATCH, "
        f"still got {form.workspace_id} (workspace_a). "
        "Serializer likely ignores the `workspace` field — add it to "
        "W3FormORM_CreateUpdateSerializer.Meta.fields and add validate_workspace."
    )


# ---------------------------------------------------------------------------
# Test 3: Cross-tenant rejection
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_patch_filing_workspace_cross_tenant_rejected(public_tenant, test_tenant):
    """
    PATCH /api/w3/forms/<id>/ with a workspace belonging to a DIFFERENT tenant
    must be rejected with HTTP 400 or 403.

    FAILS (red) because no cross-tenant validation guard exists.
    """
    user = _make_user()

    other_tenant = _make_lightweight_tenant(_unique())

    workspace_a = _make_workspace(test_tenant, f"Client A {_unique()}")
    workspace_other = _make_workspace(other_tenant, f"Client X {_unique()}")

    well = _make_well()
    form = _make_draft_form(well, workspace_a)
    original_workspace_id = form.workspace_id

    client = _authenticated_client(user)
    response = client.patch(
        f"/api/w3/forms/{form.id}/",
        {"workspace": workspace_other.id},
        format="json",
    )

    assert response.status_code in (400, 403), (
        f"Expected 400 or 403 for cross-tenant workspace reassignment, "
        f"got {response.status_code}"
    )

    form.refresh_from_db()
    assert form.workspace_id == original_workspace_id, (
        "Filing workspace must not change after cross-tenant rejection; "
        f"changed from {original_workspace_id} to {form.workspace_id}"
    )


# ---------------------------------------------------------------------------
# Test 4: Inactive workspace rejection
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_patch_filing_workspace_to_inactive_workspace_rejected(public_tenant, test_tenant):
    """
    PATCH /api/w3/forms/<id>/ with an inactive workspace (is_active=False)
    must be rejected with HTTP 400.

    FAILS (red) because no inactive-workspace validation exists.
    """
    user = _make_user()

    workspace_active = _make_workspace(test_tenant, f"Active WS {_unique()}", is_active=True)
    workspace_inactive = _make_workspace(test_tenant, f"Archived WS {_unique()}", is_active=False)

    well = _make_well()
    form = _make_draft_form(well, workspace_active)
    original_workspace_id = form.workspace_id

    client = _authenticated_client(user)
    response = client.patch(
        f"/api/w3/forms/{form.id}/",
        {"workspace": workspace_inactive.id},
        format="json",
    )

    assert response.status_code == 400, (
        f"Expected 400 when reassigning to an inactive workspace, "
        f"got {response.status_code}"
    )

    form.refresh_from_db()
    assert form.workspace_id == original_workspace_id, (
        "Filing workspace must not change after inactive-workspace rejection; "
        f"changed from {original_workspace_id} to {form.workspace_id}"
    )
