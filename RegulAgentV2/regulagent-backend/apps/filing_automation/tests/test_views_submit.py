"""
Failing tests for the W3A submit + polling endpoints.

POST /api/w3a/{snapshot_id}/submit/
GET  /api/w3a/jobs/{job_id}/

These tests intentionally fail until the views and the FilingJob model exist,
per plan section 3.
"""
from __future__ import annotations

import uuid

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def well_a(db):
    from apps.public_core.models import WellRegistry

    return WellRegistry.objects.create(
        api14="42501705750010",
        state="TX",
        county="Andrews",
        district="8A",
        operator_name="Operator A",
        field_name="Field A",
        lease_name="Lease A",
        well_number="1",
    )


@pytest.fixture
def well_b(db):
    from apps.public_core.models import WellRegistry

    return WellRegistry.objects.create(
        api14="42501705750011",
        state="TX",
        county="Andrews",
        district="8A",
        operator_name="Operator B",
        field_name="Field B",
        lease_name="Lease B",
        well_number="2",
    )


@pytest.fixture
def tenant_a_id():
    return uuid.uuid4()


@pytest.fixture
def tenant_b_id():
    return uuid.uuid4()


@pytest.fixture
def tenant_a_user(db, tenant_a_id):
    """A test user pinned to tenant A via tenant_id attribute (pattern used elsewhere in suite)."""
    from apps.tenants.models import User

    user = User.objects.create_user(
        email=f"tenant-a-{uuid.uuid4().hex[:6]}@example.com",
        password="testpass123",
        is_active=True,
    )
    # Pattern used in intelligence tests — attach tenant_id to user for view-layer scoping.
    user.tenant_id = tenant_a_id
    user.save(update_fields=["tenant_id"]) if hasattr(user, "tenant_id") and "tenant_id" in [
        f.name for f in user._meta.get_fields()
    ] else None
    return user


@pytest.fixture
def tenant_b_user(db, tenant_b_id):
    from apps.tenants.models import User

    user = User.objects.create_user(
        email=f"tenant-b-{uuid.uuid4().hex[:6]}@example.com",
        password="testpass123",
        is_active=True,
    )
    user.tenant_id = tenant_b_id
    if "tenant_id" in [f.name for f in user._meta.get_fields()]:
        user.save(update_fields=["tenant_id"])
    return user


def _make_snapshot(well, tenant_id, status_value):
    from apps.public_core.models import PlanSnapshot

    return PlanSnapshot.objects.create(
        well=well,
        plan_id=f"{well.api14}:combined:{uuid.uuid4().hex[:6]}",
        kind=PlanSnapshot.KIND_POST_EDIT,
        status=status_value,
        tenant_id=tenant_id,
        payload={"steps": [], "inputs_summary": {"api14": well.api14}},
    )


@pytest.fixture
def snapshot_engineer_approved(db, well_a, tenant_a_id):
    from apps.public_core.models import PlanSnapshot

    return _make_snapshot(well_a, tenant_a_id, PlanSnapshot.STATUS_ENGINEER_APPROVED)


@pytest.fixture
def snapshot_revision_requested(db, well_a, tenant_a_id):
    from apps.public_core.models import PlanSnapshot

    return _make_snapshot(well_a, tenant_a_id, PlanSnapshot.STATUS_REVISION_REQUESTED)


@pytest.fixture
def snapshot_draft(db, well_a, tenant_a_id):
    from apps.public_core.models import PlanSnapshot

    return _make_snapshot(well_a, tenant_a_id, PlanSnapshot.STATUS_DRAFT)


@pytest.fixture
def snapshot_filed(db, well_a, tenant_a_id):
    from apps.public_core.models import PlanSnapshot

    return _make_snapshot(well_a, tenant_a_id, PlanSnapshot.STATUS_FILED)


@pytest.fixture
def snapshot_for_tenant_b(db, well_b, tenant_b_id):
    from apps.public_core.models import PlanSnapshot

    return _make_snapshot(well_b, tenant_b_id, PlanSnapshot.STATUS_ENGINEER_APPROVED)


@pytest.fixture
def valid_attestation():
    return {
        "submitter_name": "Jane Engineer",
        "submitter_title": "Licensed P.E.",
        "certification_checked": True,
    }


@pytest.fixture
def business_profile_for_tenant_a(db, tenant_a_id):
    """
    Stub a TenantBusinessProfile with the required RRC W3A keys
    so the BusinessProfileIncomplete path doesn't fire in unrelated tests.
    """
    from apps.tenants.models import TenantBusinessProfile  # implementation will provide this

    profile, _ = TenantBusinessProfile.objects.get_or_create(
        tenant_id=tenant_a_id,
        defaults={
            "data": {
                "rrc": {
                    "w3a": {
                        "cementing_company_name": "Acme Cement",
                        "contact_phone": "555-0100",
                        "contact_email": "ops@acme.test",
                        "submitter_default_name": "Jane Engineer",
                        "submitter_default_title": "Licensed P.E.",
                    }
                }
            }
        },
    )
    return profile


@pytest.fixture
def auth_client_tenant_a(api, tenant_a_user, mocker):
    """
    Authenticate as tenant A user AND set the tenant context that the submit view
    will use to scope the snapshot lookup. The implementation is expected to read
    tenant from request (via TenantContextMiddleware / set_current_tenant); for
    tests we force-authenticate and patch get_current_tenant if necessary.
    """
    api.force_authenticate(user=tenant_a_user)
    return api


@pytest.fixture
def auth_client_tenant_b(api, tenant_b_user):
    client = APIClient()
    client.force_authenticate(user=tenant_b_user)
    return client


@pytest.fixture
def mock_celery_apply_async(mocker):
    """Mock the Celery task's apply_async so tests don't actually queue work."""
    mock = mocker.patch(
        "apps.filing_automation.tasks.submit_w3a_to_rrc.apply_async",
        return_value=mocker.MagicMock(id="celery-task-id-test-12345"),
    )
    return mock


# ---------------------------------------------------------------------------
# URL helpers — URLs may not exist yet; we attempt reverse but fall back to
# the literal path so the test failure is "404 because view not wired",
# not "NoReverseMatch in fixture setup".
# ---------------------------------------------------------------------------


def submit_url(snapshot_id) -> str:
    return f"/api/w3a/{snapshot_id}/submit/"


def job_url(job_id) -> str:
    return f"/api/w3a/jobs/{job_id}/"


# ---------------------------------------------------------------------------
# POST /api/w3a/{snapshot_id}/submit/
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSubmitEndpointHappyPath:
    def test_engineer_approved_returns_202(
        self,
        auth_client_tenant_a,
        snapshot_engineer_approved,
        valid_attestation,
        business_profile_for_tenant_a,
        mock_celery_apply_async,
    ):
        resp = auth_client_tenant_a.post(
            submit_url(snapshot_engineer_approved.id),
            valid_attestation,
            format="json",
        )
        assert resp.status_code == status.HTTP_202_ACCEPTED
        assert "job_id" in resp.data
        assert "poll_url" in resp.data

    def test_revision_requested_returns_202(
        self,
        auth_client_tenant_a,
        snapshot_revision_requested,
        valid_attestation,
        business_profile_for_tenant_a,
        mock_celery_apply_async,
    ):
        resp = auth_client_tenant_a.post(
            submit_url(snapshot_revision_requested.id),
            valid_attestation,
            format="json",
        )
        assert resp.status_code == status.HTTP_202_ACCEPTED

    def test_creates_filing_job_with_queued_status_and_task_id(
        self,
        auth_client_tenant_a,
        snapshot_engineer_approved,
        valid_attestation,
        business_profile_for_tenant_a,
        mock_celery_apply_async,
    ):
        from apps.filing_automation.models import FilingJob

        resp = auth_client_tenant_a.post(
            submit_url(snapshot_engineer_approved.id),
            valid_attestation,
            format="json",
        )
        assert resp.status_code == status.HTTP_202_ACCEPTED

        job = FilingJob.objects.get(plan_snapshot=snapshot_engineer_approved)
        assert job.status == "queued"
        assert job.celery_task_id  # non-empty


@pytest.mark.django_db
class TestSubmitEndpointBadStatus:
    def test_draft_returns_409(
        self,
        auth_client_tenant_a,
        snapshot_draft,
        valid_attestation,
        business_profile_for_tenant_a,
        mock_celery_apply_async,
    ):
        resp = auth_client_tenant_a.post(
            submit_url(snapshot_draft.id),
            valid_attestation,
            format="json",
        )
        assert resp.status_code == status.HTTP_409_CONFLICT

    def test_already_filed_returns_409(
        self,
        auth_client_tenant_a,
        snapshot_filed,
        valid_attestation,
        business_profile_for_tenant_a,
        mock_celery_apply_async,
    ):
        resp = auth_client_tenant_a.post(
            submit_url(snapshot_filed.id),
            valid_attestation,
            format="json",
        )
        assert resp.status_code == status.HTTP_409_CONFLICT

    def test_active_job_blocks_resubmit(
        self,
        auth_client_tenant_a,
        snapshot_engineer_approved,
        valid_attestation,
        business_profile_for_tenant_a,
        mock_celery_apply_async,
    ):
        """If a FilingJob queued|running exists, second submit returns 409."""
        from apps.filing_automation.models import FilingJob

        FilingJob.objects.create(
            plan_snapshot=snapshot_engineer_approved,
            tenant_id=snapshot_engineer_approved.tenant_id,
            status="queued",
            attestation=valid_attestation,
        )
        resp = auth_client_tenant_a.post(
            submit_url(snapshot_engineer_approved.id),
            valid_attestation,
            format="json",
        )
        assert resp.status_code == status.HTTP_409_CONFLICT


@pytest.mark.django_db
class TestSubmitEndpointAttestationValidation:
    def test_missing_certification_returns_400(
        self,
        auth_client_tenant_a,
        snapshot_engineer_approved,
        business_profile_for_tenant_a,
        mock_celery_apply_async,
    ):
        resp = auth_client_tenant_a.post(
            submit_url(snapshot_engineer_approved.id),
            {
                "submitter_name": "Jane",
                "submitter_title": "P.E.",
                "certification_checked": False,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_missing_submitter_name_returns_400(
        self,
        auth_client_tenant_a,
        snapshot_engineer_approved,
        business_profile_for_tenant_a,
        mock_celery_apply_async,
    ):
        resp = auth_client_tenant_a.post(
            submit_url(snapshot_engineer_approved.id),
            {"submitter_title": "P.E.", "certification_checked": True},
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_missing_submitter_title_returns_400(
        self,
        auth_client_tenant_a,
        snapshot_engineer_approved,
        business_profile_for_tenant_a,
        mock_celery_apply_async,
    ):
        resp = auth_client_tenant_a.post(
            submit_url(snapshot_engineer_approved.id),
            {"submitter_name": "Jane", "certification_checked": True},
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestSubmitEndpointBusinessProfile:
    def test_missing_business_profile_returns_400_with_field(
        self,
        auth_client_tenant_a,
        snapshot_engineer_approved,
        valid_attestation,
        mock_celery_apply_async,
    ):
        """
        TODO: Coupling to BusinessProfileIncomplete is loose. We assert response
        body identifies the missing field. No TenantBusinessProfile fixture is
        used here — so the adapter should raise BusinessProfileIncomplete and the
        view should translate it to a 400 with the missing field name.
        """
        resp = auth_client_tenant_a.post(
            submit_url(snapshot_engineer_approved.id),
            valid_attestation,
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        body_text = str(resp.data).lower() if resp.data is not None else ""
        # The response should mention at least one of the missing required keys.
        assert (
            "cementing_company_name" in body_text
            or "contact_phone" in body_text
            or "contact_email" in body_text
            or "business_profile" in body_text
            or "rrc.w3a" in body_text
        ), f"Response should identify the missing business-profile field: {resp.data}"


@pytest.mark.django_db
class TestSubmitEndpointDoubleClick:
    def test_double_click_protection(
        self,
        auth_client_tenant_a,
        snapshot_engineer_approved,
        valid_attestation,
        business_profile_for_tenant_a,
        mock_celery_apply_async,
    ):
        """Two rapid POSTs — second must get 409."""
        from apps.filing_automation.models import FilingJob

        r1 = auth_client_tenant_a.post(
            submit_url(snapshot_engineer_approved.id),
            valid_attestation,
            format="json",
        )
        r2 = auth_client_tenant_a.post(
            submit_url(snapshot_engineer_approved.id),
            valid_attestation,
            format="json",
        )
        assert r1.status_code == status.HTTP_202_ACCEPTED
        assert r2.status_code == status.HTTP_409_CONFLICT
        assert FilingJob.objects.filter(plan_snapshot=snapshot_engineer_approved).count() == 1


@pytest.mark.django_db
class TestSubmitEndpointTenantIsolation:
    def test_tenant_a_cannot_submit_tenant_b_snapshot(
        self,
        auth_client_tenant_a,
        snapshot_for_tenant_b,
        valid_attestation,
        mock_celery_apply_async,
    ):
        """Cross-tenant attempt returns 404 (don't leak existence)."""
        resp = auth_client_tenant_a.post(
            submit_url(snapshot_for_tenant_b.id),
            valid_attestation,
            format="json",
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# GET /api/w3a/jobs/{job_id}/
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestJobPollingEndpoint:
    def test_returns_job_payload(
        self,
        auth_client_tenant_a,
        snapshot_engineer_approved,
        tenant_a_id,
    ):
        from apps.filing_automation.models import FilingJob

        job = FilingJob.objects.create(
            plan_snapshot=snapshot_engineer_approved,
            tenant_id=tenant_a_id,
            status="running",
            celery_task_id="abc",
            attestation={},
            started_at=timezone.now(),
        )
        resp = auth_client_tenant_a.get(job_url(job.pk))
        assert resp.status_code == status.HTTP_200_OK
        for key in (
            "status",
            "confirmation_number",
            "error_message",
            "started_at",
            "finished_at",
            "plan_snapshot_id",
        ):
            assert key in resp.data, f"polling response missing {key}: {resp.data}"

    def test_cross_tenant_returns_404(
        self,
        auth_client_tenant_a,
        snapshot_for_tenant_b,
        tenant_b_id,
    ):
        """Tenant A asking for tenant B's job must get 404."""
        from apps.filing_automation.models import FilingJob

        job = FilingJob.objects.create(
            plan_snapshot=snapshot_for_tenant_b,
            tenant_id=tenant_b_id,
            status="succeeded",
            attestation={},
        )
        resp = auth_client_tenant_a.get(job_url(job.pk))
        assert resp.status_code == status.HTTP_404_NOT_FOUND
