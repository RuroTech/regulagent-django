"""
Failing tests for the new FilingJob model (apps.filing_automation.models.FilingJob).

These tests intentionally fail until the model is implemented per
plan section 2 of the-next-thing-that-compressed-avalanche.md.
"""
from __future__ import annotations

import uuid

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def well_fixture(db):
    from apps.public_core.models import WellRegistry

    return WellRegistry.objects.create(
        api14="42501705750001",
        state="TX",
        county="Andrews",
        district="8A",
        operator_name="Filing Auto Test Operator",
        field_name="Test Field",
        lease_name="Test Lease",
        well_number="1",
    )


@pytest.fixture
def engineer_approved_snapshot(db, well_fixture):
    from apps.public_core.models import PlanSnapshot

    return PlanSnapshot.objects.create(
        well=well_fixture,
        plan_id=f"{well_fixture.api14}:combined",
        kind=PlanSnapshot.KIND_POST_EDIT,
        status=PlanSnapshot.STATUS_ENGINEER_APPROVED,
        tenant_id=uuid.uuid4(),
        payload={"steps": []},
    )


# ---------------------------------------------------------------------------
# FilingJob — schema / field-level tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFilingJobModelSchema:
    """All field expectations come from plan section 2."""

    def test_model_is_importable(self):
        """FilingJob must exist in apps.filing_automation.models."""
        from apps.filing_automation.models import FilingJob  # noqa: F401

    def test_has_all_required_fields(self):
        """Schema introspection — every plan-section-2 field must be declared."""
        from apps.filing_automation.models import FilingJob

        field_names = {f.name for f in FilingJob._meta.get_fields()}
        expected = {
            "plan_snapshot",
            "workspace",
            "tenant_id",
            "status",
            "celery_task_id",
            "attempt_count",
            "attestation",
            "started_at",
            "finished_at",
            "confirmation_number",
            "screenshot_path",
            "error_class",
            "error_message",
            "traceback_truncated",
            "filing_status",
            "created_at",
            "updated_at",
        }
        missing = expected - field_names
        assert not missing, f"FilingJob is missing required fields: {missing}"

    def test_plan_snapshot_fk_cascade(self):
        from django.db import models
        from apps.filing_automation.models import FilingJob

        field = FilingJob._meta.get_field("plan_snapshot")
        assert isinstance(field, models.ForeignKey)
        assert field.remote_field.on_delete is models.CASCADE

    def test_workspace_fk_set_null(self):
        from django.db import models
        from apps.filing_automation.models import FilingJob

        field = FilingJob._meta.get_field("workspace")
        assert isinstance(field, models.ForeignKey)
        assert field.remote_field.on_delete is models.SET_NULL
        assert field.null is True

    def test_filing_status_fk_set_null(self):
        from django.db import models
        from apps.filing_automation.models import FilingJob

        field = FilingJob._meta.get_field("filing_status")
        assert isinstance(field, models.ForeignKey)
        assert field.remote_field.on_delete is models.SET_NULL
        assert field.null is True

    def test_tenant_id_is_uuid(self):
        from django.db import models
        from apps.filing_automation.models import FilingJob

        field = FilingJob._meta.get_field("tenant_id")
        assert isinstance(field, models.UUIDField)

    def test_status_field_choices(self):
        from apps.filing_automation.models import FilingJob

        field = FilingJob._meta.get_field("status")
        choice_values = {c[0] for c in field.choices}
        expected_values = {"queued", "running", "succeeded", "failed", "retrying"}
        assert expected_values <= choice_values, (
            f"status field must include {expected_values}, has {choice_values}"
        )

    def test_attempt_count_default_zero(self):
        from apps.filing_automation.models import FilingJob

        field = FilingJob._meta.get_field("attempt_count")
        assert field.default == 0

    def test_attestation_is_jsonfield(self):
        from django.db import models
        from apps.filing_automation.models import FilingJob

        field = FilingJob._meta.get_field("attestation")
        assert isinstance(field, models.JSONField)

    def test_started_at_nullable(self):
        from apps.filing_automation.models import FilingJob

        assert FilingJob._meta.get_field("started_at").null is True

    def test_finished_at_nullable(self):
        from apps.filing_automation.models import FilingJob

        assert FilingJob._meta.get_field("finished_at").null is True

    def test_timestamps_auto(self):
        from apps.filing_automation.models import FilingJob

        created = FilingJob._meta.get_field("created_at")
        updated = FilingJob._meta.get_field("updated_at")
        assert getattr(created, "auto_now_add", False)
        assert getattr(updated, "auto_now", False)


# ---------------------------------------------------------------------------
# FilingJob — behavioral tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFilingJobCreation:
    def test_create_with_required_fields(self, engineer_approved_snapshot):
        from apps.filing_automation.models import FilingJob

        job = FilingJob.objects.create(
            plan_snapshot=engineer_approved_snapshot,
            tenant_id=engineer_approved_snapshot.tenant_id,
            attestation={
                "submitter_name": "Jane Doe",
                "submitter_title": "Engineer",
                "certification_checked": True,
            },
        )
        assert job.pk is not None
        assert job.plan_snapshot_id == engineer_approved_snapshot.id
        assert job.tenant_id == engineer_approved_snapshot.tenant_id

    def test_default_status_is_queued(self, engineer_approved_snapshot):
        from apps.filing_automation.models import FilingJob

        job = FilingJob.objects.create(
            plan_snapshot=engineer_approved_snapshot,
            tenant_id=engineer_approved_snapshot.tenant_id,
            attestation={},
        )
        assert job.status == "queued"

    def test_default_attempt_count_is_zero(self, engineer_approved_snapshot):
        from apps.filing_automation.models import FilingJob

        job = FilingJob.objects.create(
            plan_snapshot=engineer_approved_snapshot,
            tenant_id=engineer_approved_snapshot.tenant_id,
            attestation={},
        )
        assert job.attempt_count == 0

    def test_plan_snapshot_required(self, engineer_approved_snapshot):
        """Creating without plan_snapshot raises IntegrityError."""
        from django.db import IntegrityError
        from apps.filing_automation.models import FilingJob

        with pytest.raises((IntegrityError, ValueError, TypeError)):
            FilingJob.objects.create(
                tenant_id=engineer_approved_snapshot.tenant_id,
                attestation={},
            )

    def test_tenant_id_required(self, engineer_approved_snapshot):
        """Creating without tenant_id raises IntegrityError."""
        from django.db import IntegrityError
        from apps.filing_automation.models import FilingJob

        with pytest.raises((IntegrityError, ValueError, TypeError)):
            FilingJob.objects.create(
                plan_snapshot=engineer_approved_snapshot,
                attestation={},
            )
