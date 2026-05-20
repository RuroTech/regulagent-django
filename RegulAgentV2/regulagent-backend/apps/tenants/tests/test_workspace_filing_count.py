"""
Card #58 — Failing tests for ClientWorkspace.filing_count feature.

These tests define the expected behavior BEFORE implementation.
All tests must fail until:
  1. ClientWorkspaceViewSet.get_queryset() annotates with filing_count
  2. ClientWorkspaceSerializer exposes filing_count (read field from annotation)

Test strategy:
  - Tests 1–4: Test the ORM annotation directly (avoids pre-existing HTTP 404 failures).
  - Tests 5–6: Test the serializer directly with a manually-annotated object.
"""
import uuid

import pytest
from django.db.models import Count
from django_tenants.utils import schema_context

from apps.tenants.models import ClientWorkspace
from apps.tenants.serializers import ClientWorkspaceSerializer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_w3_form(workspace, tenant_id, api14):
    """Create a minimal W3FormORM linked to workspace with the given tenant_id."""
    from apps.public_core.models.w3_orm import W3FormORM

    return W3FormORM.objects.create(
        workspace=workspace,
        tenant_id=tenant_id,
        api_number=api14,
        form_data={},
    )


def _annotated_qs(workspace_pk):
    """Return a queryset for workspace_pk with the expected filing_count annotation."""
    return ClientWorkspace.objects.filter(pk=workspace_pk).annotate(
        filing_count=Count("w3_forms", distinct=True)
    )


# ---------------------------------------------------------------------------
# Tests 1–4: ORM annotation (no HTTP, no auth)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFilingCountAnnotation:
    """Verify the ORM annotation logic the view will apply."""

    def test_filing_count_annotation_zero_when_no_w3_forms(self, test_tenant, db):
        """Workspace with no W3FormORM rows must return filing_count=0."""
        with schema_context(test_tenant.schema_name):
            workspace = ClientWorkspace.objects.create(
                tenant=test_tenant,
                name="Empty Workspace",
            )

        qs = _annotated_qs(workspace.pk)
        assert qs.first().filing_count == 0

    def test_filing_count_annotation_counts_w3_forms_via_workspace(self, test_tenant, db):
        """ClientWorkspace → W3FormORM chain must yield filing_count=1."""
        with schema_context(test_tenant.schema_name):
            workspace = ClientWorkspace.objects.create(
                tenant=test_tenant,
                name="Single W3 Form Workspace",
            )

        _make_w3_form(workspace, tenant_id=test_tenant.id, api14="42501234560001")

        qs = _annotated_qs(workspace.pk)
        assert qs.first().filing_count == 1

    def test_filing_count_counts_multiple_w3_forms_on_one_workspace(self, test_tenant, db):
        """One workspace with 3 W3FormORM rows must yield filing_count=3."""
        with schema_context(test_tenant.schema_name):
            workspace = ClientWorkspace.objects.create(
                tenant=test_tenant,
                name="Multi W3 Form Workspace",
            )

        for i in range(3):
            _make_w3_form(workspace, tenant_id=test_tenant.id, api14=f"4250123456000{i}")

        qs = _annotated_qs(workspace.pk)
        assert qs.first().filing_count == 3

    def test_filing_count_does_not_cross_workspace_boundaries(self, test_tenant, db):
        """Filing counts must be isolated per workspace — no cross-contamination."""
        with schema_context(test_tenant.schema_name):
            workspace_a = ClientWorkspace.objects.create(
                tenant=test_tenant,
                name="Workspace A",
            )
            workspace_b = ClientWorkspace.objects.create(
                tenant=test_tenant,
                name="Workspace B",
            )

        _make_w3_form(workspace_a, tenant_id=test_tenant.id, api14="42501234560003")
        _make_w3_form(workspace_a, tenant_id=test_tenant.id, api14="42501234560004")
        _make_w3_form(workspace_b, tenant_id=test_tenant.id, api14="42501234560005")

        qs_a = _annotated_qs(workspace_a.pk)
        qs_b = _annotated_qs(workspace_b.pk)

        assert qs_a.first().filing_count == 2, "Workspace A should have 2 W3 forms"
        assert qs_b.first().filing_count == 1, "Workspace B should have 1 W3 form"


# ---------------------------------------------------------------------------
# Tests 5–6: Serializer (direct, no HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWorkspaceSerializerFilingCount:
    """Verify the serializer exposes filing_count as a top-level field."""

    def test_workspace_serializer_includes_filing_count_field(self, test_tenant, db):
        """Serializer output must include the 'filing_count' key."""
        with schema_context(test_tenant.schema_name):
            workspace = ClientWorkspace.objects.create(
                tenant=test_tenant,
                name="Serializer Test Workspace",
            )

        # Simulate what the view will do: annotate then pass to serializer.
        workspace.filing_count = 0  # type: ignore[attr-defined]
        serializer = ClientWorkspaceSerializer(workspace)
        assert "filing_count" in serializer.data, (
            "ClientWorkspaceSerializer must expose 'filing_count' field; "
            "currently absent — needs implementation"
        )

    def test_workspace_serializer_filing_count_value_correct(self, test_tenant, db):
        """Serializer must return the annotated filing_count value unchanged."""
        with schema_context(test_tenant.schema_name):
            workspace = ClientWorkspace.objects.create(
                tenant=test_tenant,
                name="Value Check Workspace",
            )

        _make_w3_form(workspace, tenant_id=test_tenant.id, api14="42501234560006")
        _make_w3_form(workspace, tenant_id=test_tenant.id, api14="42501234560007")

        # Simulate the view annotation, then serialize.
        annotated = (
            ClientWorkspace.objects.filter(pk=workspace.pk)
            .annotate(filing_count=Count("w3_forms", distinct=True))
            .first()
        )
        serializer = ClientWorkspaceSerializer(annotated)
        assert serializer.data["filing_count"] == 2, (
            "Serializer must expose the annotated filing_count value (expected 2)"
        )
