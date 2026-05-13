"""
Tests for the W-3 Wizard plan verification step.

After a successful plan import (via import_wizard_plan task), the session
transitions to STATUS_PLAN_IMPORTED.  The user must review and correct the
extracted plan data via:

  GET  /api/w3-wizard/{id}/plan-verification/  — return 5 payload sections
  PUT  /api/w3-wizard/{id}/plan-verification/  — accept corrections, advance to plan_verified

Parsing (POST /api/w3-wizard/{id}/parse/) is blocked while the session is in
STATUS_PLAN_IMPORTED with a plan_snapshot attached.
"""

from __future__ import annotations

import uuid
import pytest

from rest_framework.test import APIClient
from rest_framework import status


# ---------------------------------------------------------------------------
# Shared plan-snapshot payload used across tests
# ---------------------------------------------------------------------------

SAMPLE_PLAN_PAYLOAD = {
    "well_header": {
        "api_number": "30015288410000",
        "operator": "Test Operator",
        "well_name": "Test Well #1",
        "county": "Eddy",
        "state": "NM",
        "total_depth": "12375",
    },
    "steps": [
        {
            "step_number": 1,
            "step_type": "cement_plug",
            "depth_top_ft": 5000,
            "depth_bottom_ft": 5100,
            "sacks": 50,
            "cement_class": "A",
            "description": "Set cement plug",
            "category": "plug",
        },
    ],
    "formations": [
        {"formation_name": "Wolfcamp", "top_ft": 9080},
        {"formation_name": "Bone Spring", "top_ft": 7500},
    ],
    "casing_record": [
        {
            "string_type": "surface",
            "size_in": 8.625,
            "weight_ppf": 36,
            "grade": "K55",
            "top_ft": 0,
            "bottom_ft": 2500,
            "shoe_depth_ft": 2500,
        },
    ],
    "existing_perforations": [
        {
            "depth_top_ft": 9000,
            "depth_bottom_ft": 9050,
            "formation_name": "Wolfcamp",
            "status": "open",
        },
    ],
    "well_geometry": {
        "casing_strings": [],
        "formation_tops": [{"formation": "Wolfcamp", "top_ft": 9080}],
        "existing_tools": [],
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def public_tenant(db):
    """
    Create the public tenant + Domain so TenantMainMiddleware resolves 'testserver'
    and so tenant_users.create_user() can find the public tenant.

    There is a circular dependency: User.create_user() requires a public Tenant,
    but Tenant requires an owner User (NOT NULL FK).
    We break the cycle by using get_or_create on the User model (which bypasses the
    create_user lookup), inserting the Tenant via raw SQL, then making 'testserver'
    a Domain so the test HTTP client routes correctly.
    """
    from apps.tenants.models import Tenant, Domain
    from django.contrib.auth import get_user_model
    from django.db import connection as db_connection, transaction

    User = get_user_model()

    # Create owner without going through create_user (avoids the public-tenant lookup)
    owner, _ = User.objects.get_or_create(
        email="tenant_owner@test.internal",
        defaults={"is_active": True, "first_name": "", "last_name": ""},
    )

    with transaction.atomic():
        with db_connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenants_tenant (schema_name, name, slug, owner_id, created, modified, created_on)
                VALUES ('public', 'Public', 'public', %s, NOW(), NOW(), NOW())
                ON CONFLICT (schema_name) DO NOTHING
                """,
                [owner.id],
            )

    tenant = Tenant.objects.get(schema_name="public")

    Domain.objects.get_or_create(
        domain="testserver",
        defaults={"tenant": tenant, "is_primary": True},
    )
    return tenant


@pytest.fixture
def well(db):
    from apps.public_core.models import WellRegistry

    return WellRegistry.objects.create(
        api14="30015288410000",
        state="NM",
        county="Eddy",
        operator_name="Test Operator",
        field_name="Test Field",
    )


@pytest.fixture
def plan_snapshot(db, well):
    from apps.public_core.models import PlanSnapshot

    return PlanSnapshot.objects.create(
        well=well,
        plan_id="30015288410000:combined",
        kind="baseline",
        status="draft",
        payload=SAMPLE_PLAN_PAYLOAD,
    )


@pytest.fixture
def test_tenant(db, public_tenant):
    """Create a test tenant used for auth and tenant_id assignment."""
    from apps.tenants.models import Tenant, Domain
    from django.contrib.auth import get_user_model
    from django.db import connection as db_connection

    User = get_user_model()
    unique = str(uuid.uuid4())[:8]

    # Create an owner user — public_tenant already exists so create_user() won't fail
    owner = User.objects.create_user(
        email=f"owner_{unique}@test.com",
        password="testpass123",
    )

    # Use raw SQL to insert the test tenant with the required owner_id
    schema_name = f"test_{unique}"
    with db_connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tenants_tenant (schema_name, name, slug, owner_id, created, modified, created_on)
            VALUES (%s, %s, %s, %s, NOW(), NOW(), NOW())
            """,
            [schema_name, f"Test Tenant {unique}", f"test-{unique}", owner.id],
        )
    tenant = Tenant.objects.get(schema_name=schema_name)

    Domain.objects.create(
        domain=f"test-{unique}.localhost",
        tenant=tenant,
        is_primary=True,
    )
    yield tenant
    try:
        tenant.delete(force_drop=True)
    except Exception:
        pass


@pytest.fixture
def test_user(db, public_tenant, test_tenant):
    """
    Create a user associated with test_tenant.

    tenant_users.create_user() automatically adds every new user to the public
    tenant via add_user(), which also creates a UserTenantPermissions record.
    A second add_user() call (for the workspace tenant) would violate the unique
    constraint on UserTenantPermissions.profile_id.

    The real production flow removes the user from the public tenant before
    adding them to a workspace.  We replicate this here using a direct M2M
    remove (bypassing schema-context checks that are irrelevant in tests) so
    that request.user.tenants.first() reliably resolves to test_tenant rather
    than the public tenant.
    """
    from apps.tenants.models import User

    user = User.objects.create_user(
        email="wizard_verify@example.com",
        password="testpass123",
        is_active=True,
    )
    # Remove from public tenant (plain M2M — avoids schema_required decorator)
    user.tenants.remove(public_tenant)
    # Add to the workspace tenant (plain M2M — UserTenantPermissions already exists)
    user.tenants.add(test_tenant)
    return user


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def authenticated_client(api_client, test_user):
    """APIClient authenticated with a JWT belonging to test_user."""
    from rest_framework_simplejwt.tokens import RefreshToken

    refresh = RefreshToken.for_user(test_user)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    return api_client


@pytest.fixture
def session_plan_imported(db, well, plan_snapshot, test_tenant):
    """A W3WizardSession in STATUS_PLAN_IMPORTED with an attached plan_snapshot."""
    from apps.public_core.models import W3WizardSession

    return W3WizardSession.objects.create(
        api_number="30015288410000",
        well=well,
        plan_snapshot=plan_snapshot,
        tenant_id=test_tenant.id,
        status=W3WizardSession.STATUS_PLAN_IMPORTED,
    )


@pytest.fixture
def session_no_snapshot(db, well, test_tenant):
    """A W3WizardSession in STATUS_PLAN_IMPORTED WITHOUT a plan_snapshot."""
    from apps.public_core.models import W3WizardSession

    return W3WizardSession.objects.create(
        api_number="30015288410000",
        well=well,
        plan_snapshot=None,
        tenant_id=test_tenant.id,
        status=W3WizardSession.STATUS_PLAN_IMPORTED,
    )


@pytest.fixture
def session_plan_verified(db, well, plan_snapshot, test_tenant):
    """A W3WizardSession already at STATUS_PLAN_VERIFIED."""
    from apps.public_core.models import W3WizardSession

    return W3WizardSession.objects.create(
        api_number="30015288410000",
        well=well,
        plan_snapshot=plan_snapshot,
        tenant_id=test_tenant.id,
        status=W3WizardSession.STATUS_PLAN_VERIFIED,
    )


@pytest.fixture
def session_uploading_with_snapshot(db, well, plan_snapshot, test_tenant):
    """A session at STATUS_UPLOADING (not plan_imported) — PUT should be rejected."""
    from apps.public_core.models import W3WizardSession

    return W3WizardSession.objects.create(
        api_number="30015288410000",
        well=well,
        plan_snapshot=plan_snapshot,
        tenant_id=test_tenant.id,
        status=W3WizardSession.STATUS_UPLOADING,
    )


@pytest.fixture
def session_plan_imported_with_docs(db, well, plan_snapshot, test_tenant):
    """
    A session in STATUS_PLAN_IMPORTED with ticket documents uploaded.
    Used to test that parsing is blocked by the plan-verification guard.
    """
    from apps.public_core.models import W3WizardSession

    return W3WizardSession.objects.create(
        api_number="30015288410000",
        well=well,
        plan_snapshot=plan_snapshot,
        tenant_id=test_tenant.id,
        status=W3WizardSession.STATUS_PLAN_IMPORTED,
        uploaded_documents=[
            {
                "file_name": "ticket.pdf",
                "file_type": "pdf",
                "storage_key": "w3_wizard/test/ticket.pdf",
                "category": "tickets",
            }
        ],
    )


@pytest.fixture
def session_plan_verified_with_docs(db, well, plan_snapshot, test_tenant):
    """
    A session in STATUS_PLAN_VERIFIED with ticket documents uploaded.
    Used to test that the parse guard is lifted after verification.
    """
    from apps.public_core.models import W3WizardSession

    return W3WizardSession.objects.create(
        api_number="30015288410000",
        well=well,
        plan_snapshot=plan_snapshot,
        tenant_id=test_tenant.id,
        status=W3WizardSession.STATUS_PLAN_VERIFIED,
        uploaded_documents=[
            {
                "file_name": "ticket.pdf",
                "file_type": "pdf",
                "storage_key": "w3_wizard/test/ticket.pdf",
                "category": "tickets",
            }
        ],
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _plan_verification_url(session_id):
    return f"/api/w3-wizard/{session_id}/plan-verification/"


def _parse_url(session_id):
    return f"/api/w3-wizard/{session_id}/parse/"


# ---------------------------------------------------------------------------
# Test: import_wizard_plan sets status to plan_imported
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestImportPlanSetsPlanImportedStatus:
    """
    After a successful plan import the session status must be plan_imported.
    We test this at the model level because the Celery task calls an external
    service; the view tests cover the API surface.
    """

    def test_import_plan_sets_plan_imported_status(self, db, well, plan_snapshot, test_tenant):
        """
        Manually replicate what import_wizard_plan does on success:
        link a PlanSnapshot and set status → plan_imported.
        """
        from apps.public_core.models import W3WizardSession

        session = W3WizardSession.objects.create(
            api_number="30015288410000",
            well=well,
            tenant_id=test_tenant.id,
            status=W3WizardSession.STATUS_IMPORTING_PLAN,
        )

        # Simulate successful plan import
        session.plan_snapshot = plan_snapshot
        session.status = W3WizardSession.STATUS_PLAN_IMPORTED
        session.save(update_fields=["plan_snapshot", "status", "updated_at"])

        session.refresh_from_db()
        assert session.status == W3WizardSession.STATUS_PLAN_IMPORTED, (
            "Status should advance to plan_imported after a successful plan import"
        )
        assert session.plan_snapshot_id == plan_snapshot.pk, (
            "plan_snapshot foreign key should be wired to the imported snapshot"
        )


# ---------------------------------------------------------------------------
# Test: GET plan-verification
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGetPlanVerification:
    def test_get_plan_verification_returns_sections(
        self, authenticated_client, session_plan_imported
    ):
        """
        GET plan-verification with a valid session + snapshot returns all 5 sections.
        """
        url = _plan_verification_url(session_plan_imported.id)
        response = authenticated_client.get(url)

        assert response.status_code == status.HTTP_200_OK, response.data

        data = response.data
        assert "plan_snapshot_id" in data
        assert "sections" in data
        assert "session_status" in data

        sections = data["sections"]
        for key in ("well_header", "steps", "formations", "casing_record", "existing_perforations"):
            assert key in sections, f"Missing section: {key}"

        # Spot-check that values round-tripped from the payload
        assert sections["well_header"]["operator"] == "Test Operator"
        assert len(sections["steps"]) == 1
        assert len(sections["formations"]) == 2
        assert len(sections["casing_record"]) == 1
        assert len(sections["existing_perforations"]) == 1

    def test_get_plan_verification_no_snapshot_returns_400(
        self, authenticated_client, session_no_snapshot
    ):
        """
        GET plan-verification when no plan_snapshot exists returns HTTP 400.
        """
        url = _plan_verification_url(session_no_snapshot.id)
        response = authenticated_client.get(url)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "error" in response.data

    def test_get_plan_verification_unauthenticated_returns_401(
        self, api_client, session_plan_imported
    ):
        """Unauthenticated requests are rejected."""
        url = _plan_verification_url(session_plan_imported.id)
        response = api_client.get(url)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_get_plan_verification_wrong_tenant_returns_404(
        self, db, public_tenant, well, plan_snapshot
    ):
        """
        A session belonging to tenant A must not be visible to tenant B.
        """
        from apps.public_core.models import W3WizardSession
        from apps.tenants.models import Tenant, Domain, User
        from django.contrib.auth import get_user_model
        from django.db import connection as db_connection
        from rest_framework_simplejwt.tokens import RefreshToken

        UserModel = get_user_model()

        # --- Create other_tenant (needs an owner) ---
        # public_tenant is already set up so create_user() can find it
        unique = str(uuid.uuid4())[:8]
        other_owner = UserModel.objects.create_user(
            email=f"owner_other_{unique}@test.com",
            password="testpass123",
        )
        other_schema = f"other_{unique}"
        with db_connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenants_tenant (schema_name, name, slug, owner_id, created, modified, created_on)
                VALUES (%s, %s, %s, %s, NOW(), NOW(), NOW())
                """,
                [other_schema, f"Other Tenant {unique}", f"other-{unique}", other_owner.id],
            )
        other_tenant = Tenant.objects.get(schema_name=other_schema)
        Domain.objects.create(
            domain=f"other-{unique}.localhost",
            tenant=other_tenant,
            is_primary=True,
        )

        # Session belongs to other_tenant
        session = W3WizardSession.objects.create(
            api_number="30015288410000",
            well=well,
            plan_snapshot=plan_snapshot,
            tenant_id=other_tenant.id,
            status=W3WizardSession.STATUS_PLAN_IMPORTED,
        )

        # --- Create unrelated_tenant (also needs an owner) ---
        unique2 = str(uuid.uuid4())[:8]
        unrelated_owner = UserModel.objects.create_user(
            email=f"owner_unrelated_{unique2}@test.com",
            password="testpass123",
        )
        unrelated_schema = f"unrelated_{unique2}"
        with db_connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenants_tenant (schema_name, name, slug, owner_id, created, modified, created_on)
                VALUES (%s, %s, %s, %s, NOW(), NOW(), NOW())
                """,
                [unrelated_schema, f"Unrelated Tenant {unique2}", f"unrelated-{unique2}", unrelated_owner.id],
            )
        unrelated_tenant = Tenant.objects.get(schema_name=unrelated_schema)
        Domain.objects.create(
            domain=f"unrelated-{unique2}.localhost",
            tenant=unrelated_tenant,
            is_primary=True,
        )

        # Authenticate as a user in unrelated_tenant
        unrelated_user = UserModel.objects.create_user(
            email=f"unrelated_{unique2}@example.com",
            password="testpass123",
            is_active=True,
        )
        unrelated_user.tenants.add(unrelated_tenant)

        client = APIClient()
        refresh = RefreshToken.for_user(unrelated_user)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

        url = _plan_verification_url(session.id)
        response = client.get(url)

        assert response.status_code == status.HTTP_404_NOT_FOUND

        # Cleanup
        try:
            other_tenant.delete(force_drop=True)
            unrelated_tenant.delete(force_drop=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Test: PUT plan-verification
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPutPlanVerification:
    def test_put_plan_verification_persists_corrections(
        self, authenticated_client, session_plan_imported, plan_snapshot
    ):
        """
        PUT with corrected formations and steps:
        - Updates PlanSnapshot.payload in the database
        - Advances session status to plan_verified
        """
        from apps.public_core.models import PlanSnapshot, W3WizardSession

        corrected_formations = [
            {"formation_name": "Wolfcamp", "top_ft": 9100},  # depth corrected
            {"formation_name": "Bone Spring", "top_ft": 7500},
            {"formation_name": "Delaware", "top_ft": 6200},  # newly added
        ]
        corrected_steps = [
            {
                "step_number": 1,
                "step_type": "cement_plug",
                "depth_top_ft": 5050,  # corrected
                "depth_bottom_ft": 5150,
                "sacks": 55,
                "cement_class": "G",
                "description": "Set cement plug (corrected)",
                "category": "plug",
            },
        ]

        payload = {
            "formations": corrected_formations,
            "steps": corrected_steps,
        }

        url = _plan_verification_url(session_plan_imported.id)
        response = authenticated_client.put(url, payload, format="json")

        assert response.status_code == status.HTTP_200_OK, response.data

        # Verify session status advanced
        session_plan_imported.refresh_from_db()
        assert session_plan_imported.status == W3WizardSession.STATUS_PLAN_VERIFIED, (
            "PUT should advance session to plan_verified"
        )

        # Verify PlanSnapshot payload was updated
        plan_snapshot.refresh_from_db()
        saved_formations = plan_snapshot.payload.get("formations", [])
        assert len(saved_formations) == 3, "Three formations should be persisted"

        # Confirm corrected depth was saved
        wolfcamp = next((f for f in saved_formations if f["formation_name"] == "Wolfcamp"), None)
        assert wolfcamp is not None
        assert wolfcamp["top_ft"] == 9100, "Corrected Wolfcamp depth should be saved"

        saved_steps = plan_snapshot.payload.get("steps", [])
        assert saved_steps[0]["depth_top_ft"] == 5050, "Corrected step depth should be saved"

    def test_put_plan_verification_wrong_status_returns_400_when_uploading(
        self, authenticated_client, session_uploading_with_snapshot
    ):
        """
        PUT when session status is STATUS_UPLOADING (not plan_imported) → HTTP 400.
        """
        url = _plan_verification_url(session_uploading_with_snapshot.id)
        response = authenticated_client.put(
            url,
            {"formations": [{"formation_name": "Wolfcamp", "top_ft": 9080}]},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "error" in response.data

    def test_put_plan_verification_wrong_status_returns_400_when_already_verified(
        self, authenticated_client, session_plan_verified
    ):
        """
        PUT when session is already at plan_verified → HTTP 400.
        The endpoint requires exactly STATUS_PLAN_IMPORTED.
        """
        url = _plan_verification_url(session_plan_verified.id)
        response = authenticated_client.put(
            url,
            {"formations": [{"formation_name": "Wolfcamp", "top_ft": 9080}]},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "error" in response.data

    def test_put_plan_verification_syncs_well_geometry_formation_tops(
        self, authenticated_client, session_plan_imported, plan_snapshot
    ):
        """
        PUT with corrected formations syncs well_geometry.formation_tops in the payload:
        each entry should use {formation: ..., top_ft: ...} (not formation_name).
        """
        from apps.public_core.models import PlanSnapshot

        corrected_formations = [
            {"formation_name": "Wolfcamp", "top_ft": 9100},
            {"formation_name": "Delaware", "top_ft": 6200},
        ]

        url = _plan_verification_url(session_plan_imported.id)
        response = authenticated_client.put(
            url,
            {"formations": corrected_formations},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data

        plan_snapshot.refresh_from_db()
        wg = plan_snapshot.payload.get("well_geometry", {})
        formation_tops = wg.get("formation_tops", [])

        assert len(formation_tops) == 2, (
            "well_geometry.formation_tops should be replaced with corrected formations"
        )

        # Keys in formation_tops use "formation", not "formation_name"
        names_in_geom = {ft.get("formation") for ft in formation_tops}
        assert "Wolfcamp" in names_in_geom
        assert "Delaware" in names_in_geom

        wolfcamp_geom = next(ft for ft in formation_tops if ft["formation"] == "Wolfcamp")
        assert wolfcamp_geom["top_ft"] == 9100, (
            "Corrected Wolfcamp depth should be reflected in well_geometry.formation_tops"
        )

    def test_put_plan_verification_partial_payload_leaves_untouched_sections(
        self, authenticated_client, session_plan_imported, plan_snapshot
    ):
        """
        PUT with only 'formations' provided must not clear casing_record or steps.
        Sections not included in the PUT body should be unchanged.
        """
        from apps.public_core.models import PlanSnapshot

        original_casing = plan_snapshot.payload.get("casing_record", [])
        original_steps = plan_snapshot.payload.get("steps", [])

        url = _plan_verification_url(session_plan_imported.id)
        response = authenticated_client.put(
            url,
            {"formations": [{"formation_name": "Wolfcamp", "top_ft": 9080}]},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK

        plan_snapshot.refresh_from_db()
        # casing_record and steps should be unchanged when not provided in PUT
        # (the view only merges sections that are present and non-empty in corrections)
        assert plan_snapshot.payload.get("casing_record") == original_casing, (
            "casing_record should not be overwritten by a PUT that omits it"
        )
        assert plan_snapshot.payload.get("steps") == original_steps, (
            "steps should not be overwritten by a PUT that omits it"
        )

    def test_put_plan_verification_advances_status_in_response(
        self, authenticated_client, session_plan_imported
    ):
        """
        The PUT response should contain the serialized session with status=plan_verified.
        """
        url = _plan_verification_url(session_plan_imported.id)
        response = authenticated_client.put(
            url,
            {"formations": [{"formation_name": "Wolfcamp", "top_ft": 9080}]},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data.get("status") == "plan_verified", (
            "Response body should reflect the new session status"
        )


# ---------------------------------------------------------------------------
# Test: parse blocked / allowed based on status
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestParseGuard:
    def test_parse_blocked_while_plan_imported(
        self, authenticated_client, session_plan_imported_with_docs
    ):
        """
        POST /parse/ when status=plan_imported AND a plan_snapshot exists
        must return HTTP 400.  The user must verify the plan first.
        """
        url = _parse_url(session_plan_imported_with_docs.id)
        response = authenticated_client.post(url)

        assert response.status_code == status.HTTP_400_BAD_REQUEST, (
            "Parse should be blocked when status is plan_imported with a plan_snapshot"
        )
        assert "error" in response.data

    def test_parse_allowed_after_plan_verified(
        self, authenticated_client, session_plan_verified_with_docs
    ):
        """
        POST /parse/ when status=plan_verified should pass the guard and be accepted
        (the Celery task would be queued — we mock .delay() to avoid needing a broker).
        """
        from unittest.mock import patch, MagicMock

        mock_task = MagicMock()
        mock_task.id = str(uuid.uuid4())

        with patch(
            "apps.public_core.tasks_w3_wizard.parse_wizard_tickets"
        ) as mock_parse_task:
            mock_parse_task.delay.return_value = mock_task

            url = _parse_url(session_plan_verified_with_docs.id)
            response = authenticated_client.post(url)

        # Should NOT be blocked — either 202 (queued) or 400 for a different reason
        assert response.status_code != status.HTTP_400_BAD_REQUEST or (
            "plan" not in response.data.get("error", "").lower()
        ), (
            "Parse should not be blocked by the plan-verification guard "
            "when session is plan_verified"
        )

    def test_parse_blocked_even_without_uploaded_tickets(
        self, authenticated_client, session_plan_imported
    ):
        """
        POST /parse/ when status=plan_imported with no uploaded ticket documents
        — the first guard (no uploaded_documents) fires before the plan guard,
        so we still expect a 400.  Ensures the plan guard does not create a
        false pass for sessions with no documents.
        """
        url = _parse_url(session_plan_imported.id)
        response = authenticated_client.post(url)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
