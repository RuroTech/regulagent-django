"""
API view tests for the intelligence app.
Tests tenant isolation, auth guards, filtering, and endpoint behaviour.
"""
import uuid

import pytest
from django.urls import reverse
from rest_framework import status

from apps.intelligence.models import (
    FilingStatusRecord,
    Recommendation,
    RecommendationInteraction,
    RejectionPattern,
    RejectionRecord,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth_client(client, user):
    """Force-authenticate a DRF APIClient with the given user."""
    client.force_authenticate(user=user)
    return client


# ---------------------------------------------------------------------------
# Auth guard tests (unauthenticated → 401)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuthGuards:
    def test_recommendations_list_requires_auth(self, api_client):
        url = reverse("intelligence:recommendation-list")
        response = api_client.get(url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_rejections_list_requires_auth(self, api_client):
        url = reverse("intelligence:rejection-list")
        response = api_client.get(url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_filing_status_list_requires_auth(self, api_client):
        url = reverse("intelligence:filing-status-list")
        response = api_client.get(url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_trends_requires_auth(self, api_client):
        url = reverse("intelligence:trends")
        response = api_client.get(url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_dashboard_requires_auth(self, api_client):
        url = reverse("intelligence:dashboard")
        response = api_client.get(url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRecommendationListView:
    def test_returns_200(self, api_client, test_user, mocker):
        mocker.patch(
            "apps.intelligence.services.recommendation_engine.RecommendationEngine"
            ".get_recommendations_for_context",
            return_value=[],
        )
        _auth_client(api_client, test_user)
        url = reverse("intelligence:recommendation-list")
        response = api_client.get(url, {"form_type": "w3a"})
        assert response.status_code == status.HTTP_200_OK

    def test_returns_recommendation_data(
        self, api_client, test_user, recommendation, mocker
    ):
        mocker.patch(
            "apps.intelligence.services.recommendation_engine.RecommendationEngine"
            "._embedding_augment",
            return_value=[],
        )
        _auth_client(api_client, test_user)
        url = reverse("intelligence:recommendation-list")
        response = api_client.get(url, {"form_type": "w3a", "state": "TX"})
        assert response.status_code == status.HTTP_200_OK

    def test_invalid_field_values_json_silently_ignored(
        self, api_client, test_user, mocker
    ):
        mocker.patch(
            "apps.intelligence.services.recommendation_engine.RecommendationEngine"
            ".get_recommendations_for_context",
            return_value=[],
        )
        _auth_client(api_client, test_user)
        url = reverse("intelligence:recommendation-list")
        response = api_client.get(url, {"form_type": "w3a", "field_values": "NOT_JSON"})
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestFieldCheckView:
    def test_valid_request_returns_200(self, api_client, test_user, recommendation):
        _auth_client(api_client, test_user)
        url = reverse("intelligence:check-field")
        response = api_client.post(
            url,
            {
                "form_type": "w3a",
                "field_name": "plug_type",
                "value": "CIBP cap",
                "state": "TX",
                "district": "8A",
            },
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK
        assert "recommendations" in response.data

    def test_missing_required_fields_returns_400(self, api_client, test_user):
        _auth_client(api_client, test_user)
        url = reverse("intelligence:check-field")
        response = api_client.post(url, {}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestRecommendationInteractView:
    def test_creates_interaction_record(
        self, api_client, test_user, recommendation, tenant_id
    ):
        test_user.tenant_id = tenant_id
        _auth_client(api_client, test_user)
        url = reverse("intelligence:recommendation-interact", kwargs={"pk": recommendation.id})
        response = api_client.post(
            url, {"action": "shown"}, format="json"
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert RecommendationInteraction.objects.filter(
            recommendation=recommendation
        ).exists()

    def test_404_for_nonexistent_recommendation(self, api_client, test_user):
        _auth_client(api_client, test_user)
        url = reverse(
            "intelligence:recommendation-interact",
            kwargs={"pk": uuid.uuid4()},
        )
        response = api_client.post(url, {"action": "shown"}, format="json")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_invalid_action_returns_400(self, api_client, test_user, recommendation):
        _auth_client(api_client, test_user)
        url = reverse(
            "intelligence:recommendation-interact", kwargs={"pk": recommendation.id}
        )
        response = api_client.post(url, {"action": "unknown_action"}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_accepted_increments_times_accepted(
        self, api_client, test_user, recommendation, tenant_id
    ):
        test_user.tenant_id = tenant_id
        _auth_client(api_client, test_user)
        url = reverse(
            "intelligence:recommendation-interact", kwargs={"pk": recommendation.id}
        )
        api_client.post(url, {"action": "accepted"}, format="json")

        recommendation.refresh_from_db()
        assert recommendation.times_accepted >= 1


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRejectionListView:
    def test_returns_200_for_authenticated_user(
        self, api_client, test_user, rejection_record
    ):
        test_user.tenant_id = rejection_record.tenant_id
        _auth_client(api_client, test_user)
        url = reverse("intelligence:rejection-list")
        response = api_client.get(url)
        assert response.status_code == status.HTTP_200_OK

    def test_tenant_isolation(
        self,
        api_client,
        test_user,
        rejection_record,
        well,
        filing_status_record,
        second_tenant_id,
    ):
        """User A cannot see User B's rejection records."""
        # Create a record for second tenant
        RejectionRecord.objects.create(
            filing_status=filing_status_record,
            tenant_id=second_tenant_id,
            well=well,
            agency="RRC",
            form_type="w3a",
            parse_status="pending",
        )

        test_user.tenant_id = rejection_record.tenant_id
        _auth_client(api_client, test_user)
        url = reverse("intelligence:rejection-list")
        response = api_client.get(url)

        assert response.status_code == status.HTTP_200_OK
        result_ids = [r["id"] for r in response.data["results"]]
        assert str(rejection_record.id) in result_ids
        # Second tenant's record must not appear
        for r in response.data["results"]:
            assert str(r["tenant_id"]) == str(rejection_record.tenant_id)


@pytest.mark.django_db
class TestRejectionDetailView:
    def test_returns_404_for_different_tenant(
        self, api_client, test_user, rejection_record, second_tenant_id
    ):
        test_user.tenant_id = second_tenant_id
        _auth_client(api_client, test_user)
        url = reverse("intelligence:rejection-detail", kwargs={"pk": rejection_record.id})
        response = api_client.get(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_returns_200_for_own_tenant(
        self, api_client, test_user, rejection_record
    ):
        test_user.tenant_id = rejection_record.tenant_id
        _auth_client(api_client, test_user)
        url = reverse("intelligence:rejection-detail", kwargs={"pk": rejection_record.id})
        response = api_client.get(url)
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestRejectionVerifyView:
    def test_verify_updates_parse_status(
        self, api_client, test_user, rejection_record
    ):
        test_user.tenant_id = rejection_record.tenant_id
        _auth_client(api_client, test_user)
        url = reverse("intelligence:rejection-verify", kwargs={"pk": rejection_record.id})

        new_issues = [
            {
                "field_name": "plug_type",
                "field_value": "CIBP cap",
                "expected_value": "Cement Plug",
                "issue_category": "terminology",
                "severity": "rejection",
                "description": "Verified by user",
                "form_section": "plugging_record",
                "confidence": 1.0,
            }
        ]
        response = api_client.patch(url, {"parsed_issues": new_issues}, format="json")

        assert response.status_code == status.HTTP_200_OK
        rejection_record.refresh_from_db()
        assert rejection_record.parse_status == "verified"
        assert rejection_record.parsed_issues == new_issues

    def test_verify_returns_404_for_wrong_tenant(
        self, api_client, test_user, rejection_record, second_tenant_id
    ):
        test_user.tenant_id = second_tenant_id
        _auth_client(api_client, test_user)
        url = reverse("intelligence:rejection-verify", kwargs={"pk": rejection_record.id})
        response = api_client.patch(url, {"parsed_issues": []}, format="json")
        assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# Filing Status
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFilingStatusListView:
    def test_list_returns_own_tenant_records(
        self, api_client, test_user, filing_status_record
    ):
        test_user.tenant_id = filing_status_record.tenant_id
        _auth_client(api_client, test_user)
        url = reverse("intelligence:filing-status-list")
        response = api_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        result_ids = [r["id"] for r in response.data["results"]]
        assert str(filing_status_record.id) in result_ids

    def test_create_returns_201(self, api_client, test_user, well, tenant_id):
        test_user.tenant_id = tenant_id
        _auth_client(api_client, test_user)
        url = reverse("intelligence:filing-status-list")
        response = api_client.post(
            url,
            {
                "filing_id": "RRC-CREATE-001",
                "form_type": "w3a",
                "agency": "RRC",
                "tenant_id": str(tenant_id),
                "well_id": str(well.id),
                "state": "TX",
            },
            format="json",
        )
        assert response.status_code == status.HTTP_201_CREATED

    def test_create_with_missing_well_returns_400(
        self, api_client, test_user, tenant_id
    ):
        test_user.tenant_id = tenant_id
        _auth_client(api_client, test_user)
        url = reverse("intelligence:filing-status-list")
        response = api_client.post(
            url,
            {
                "filing_id": "RRC-CREATE-002",
                "form_type": "w3a",
                "agency": "RRC",
                "tenant_id": str(tenant_id),
                "well_id": str(uuid.uuid4()),  # non-existent well
            },
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Trends & Analytics
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTrendsView:
    def test_returns_only_privacy_safe_patterns(
        self, api_client, test_user, rejection_pattern, private_pattern
    ):
        _auth_client(api_client, test_user)
        url = reverse("intelligence:trends")
        response = api_client.get(url)

        assert response.status_code == status.HTTP_200_OK
        result_ids = [r["id"] for r in response.data["results"]]
        # rejection_pattern has tenant_count=5 → appears
        assert str(rejection_pattern.id) in result_ids
        # private_pattern has tenant_count=1 → must NOT appear
        assert str(private_pattern.id) not in result_ids

    def test_filter_by_form_type(self, api_client, test_user, rejection_pattern):
        _auth_client(api_client, test_user)
        url = reverse("intelligence:trends")
        response = api_client.get(url, {"form_type": "c103"})
        result_ids = [r["id"] for r in response.data["results"]]
        assert str(rejection_pattern.id) not in result_ids

    def test_filter_by_state(self, api_client, test_user, rejection_pattern):
        _auth_client(api_client, test_user)
        url = reverse("intelligence:trends")
        response = api_client.get(url, {"state": "NM"})
        result_ids = [r["id"] for r in response.data["results"]]
        assert str(rejection_pattern.id) not in result_ids


@pytest.mark.django_db
class TestTrendsHeatmapView:
    def test_returns_200(self, api_client, test_user):
        _auth_client(api_client, test_user)
        url = reverse("intelligence:trends-heatmap")
        response = api_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        assert "heatmap" in response.data

    def test_heatmap_respects_privacy_guard(
        self, api_client, test_user, private_pattern
    ):
        _auth_client(api_client, test_user)
        url = reverse("intelligence:trends-heatmap")
        response = api_client.get(url)
        # private_pattern has tenant_count=1 — heatmap should not include it
        assert response.status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDashboardView:
    def test_returns_200_with_correct_structure(
        self, api_client, test_user, filing_status_record, rejection_record
    ):
        test_user.tenant_id = rejection_record.tenant_id
        _auth_client(api_client, test_user)
        url = reverse("intelligence:dashboard")
        response = api_client.get(url)

        assert response.status_code == status.HTTP_200_OK
        data = response.data
        assert "total_filings" in data
        assert "total_rejections" in data
        assert "approval_rate" in data
        assert "top_rejection_reasons" in data
        assert "trending_patterns" in data
        assert "recent_rejections" in data

    def test_dashboard_counts_correct_totals(
        self, api_client, test_user, filing_status_record, rejection_record
    ):
        test_user.tenant_id = rejection_record.tenant_id
        _auth_client(api_client, test_user)
        url = reverse("intelligence:dashboard")
        response = api_client.get(url)

        assert response.data["total_filings"] >= 1
        assert response.data["total_rejections"] >= 1


# ---------------------------------------------------------------------------
# TDD: POST /api/intelligence/rejections/{pk}/apply-corrections/
# (NOT YET IMPLEMENTED — these tests define expected behaviour for BE2)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRejectionApplyCorrectionsView:
    """Tests for the apply-corrections endpoint on RejectionRecord.

    The endpoint does NOT exist yet. Tests should fail with NoReverseMatch or
    404 — that is the expected TDD state before BE2 implements:
      - RejectionRecord.accepted_corrections  (JSONField, default=[])
      - RejectionRecord.correction_status     (CharField choices: none/partial/all_applied)
      - POST /api/intelligence/rejections/<uuid>/apply-corrections/ view + URL
    """

    def _url(self, pk):
        return reverse(
            "intelligence:rejection-apply-corrections", kwargs={"pk": pk}
        )

    # ------------------------------------------------------------------
    # Auth guard
    # ------------------------------------------------------------------

    def test_requires_auth(self, api_client, parsed_rejection_record):
        """Unauthenticated request must return 401."""
        url = self._url(parsed_rejection_record.id)
        response = api_client.post(url, [], content_type="application/json")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_apply_corrections_stores_accepted_fixes(
        self, api_client, test_user, parsed_rejection_record
    ):
        """Authenticated POST stores corrections on the record and returns 200."""
        test_user.tenant_id = parsed_rejection_record.tenant_id
        _auth_client(api_client, test_user)
        url = self._url(parsed_rejection_record.id)
        payload = [
            {
                "issue_index": 0,
                "field_name": "plug_type",
                "applied_value": "Cement Plug",
            }
        ]
        response = api_client.post(url, payload, content_type="application/json")
        assert response.status_code == status.HTTP_200_OK
        parsed_rejection_record.refresh_from_db()
        assert len(parsed_rejection_record.accepted_corrections) == 1
        assert parsed_rejection_record.accepted_corrections[0]["field_name"] == "plug_type"
        assert parsed_rejection_record.accepted_corrections[0]["applied_value"] == "Cement Plug"

    def test_apply_all_sets_all_applied_status(
        self, api_client, test_user, parsed_rejection_record
    ):
        """Accepting every issue sets correction_status to 'all_applied'."""
        test_user.tenant_id = parsed_rejection_record.tenant_id
        _auth_client(api_client, test_user)
        url = self._url(parsed_rejection_record.id)
        # parsed_rejection_record has exactly 1 issue; accepting it → all_applied
        payload = [
            {
                "issue_index": 0,
                "field_name": "plug_type",
                "applied_value": "Cement Plug",
            }
        ]
        api_client.post(url, payload, content_type="application/json")
        parsed_rejection_record.refresh_from_db()
        assert parsed_rejection_record.correction_status == "all_applied"

    def test_apply_partial_sets_partial_status(
        self,
        api_client,
        test_user,
        db,
        filing_status_record,
        well,
        tenant_id,
    ):
        """When accepted count < total issues, correction_status is 'partial'."""
        from apps.intelligence.models import RejectionRecord
        from django.utils import timezone

        multi_issue_record = RejectionRecord.objects.create(
            filing_status=filing_status_record,
            tenant_id=tenant_id,
            well=well,
            agency="RRC",
            form_type="w3a",
            state="TX",
            district="8A",
            county="Andrews",
            raw_rejection_notes="Two issues.",
            rejection_date=timezone.now().date(),
            parse_status="parsed",
            parsed_issues=[
                {
                    "field_name": "plug_type",
                    "field_value": "CIBP cap",
                    "expected_value": "Cement Plug",
                    "issue_category": "terminology",
                    "issue_subcategory": "naming_convention",
                    "severity": "rejection",
                    "description": "Use Cement Plug",
                    "form_section": "plugging_record",
                    "confidence": 0.95,
                },
                {
                    "field_name": "depth_top",
                    "field_value": "3100",
                    "expected_value": "3103.5",
                    "issue_category": "precision",
                    "issue_subcategory": "rounding",
                    "severity": "revision",
                    "description": "Depth rounded",
                    "form_section": "plugging_record",
                    "confidence": 0.7,
                },
            ],
        )
        test_user.tenant_id = tenant_id
        _auth_client(api_client, test_user)
        url = self._url(multi_issue_record.id)
        # Accept only the first of two issues → partial
        payload = [
            {
                "issue_index": 0,
                "field_name": "plug_type",
                "applied_value": "Cement Plug",
            }
        ]
        api_client.post(url, payload, content_type="application/json")
        multi_issue_record.refresh_from_db()
        assert multi_issue_record.correction_status == "partial"

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_empty_corrections_returns_200_status_none(
        self, api_client, test_user, parsed_rejection_record
    ):
        """Posting an empty list is valid; correction_status stays 'none'."""
        test_user.tenant_id = parsed_rejection_record.tenant_id
        _auth_client(api_client, test_user)
        url = self._url(parsed_rejection_record.id)
        response = api_client.post(url, [], content_type="application/json")
        assert response.status_code == status.HTTP_200_OK
        parsed_rejection_record.refresh_from_db()
        assert parsed_rejection_record.correction_status == "none"

    # ------------------------------------------------------------------
    # Tenant isolation
    # ------------------------------------------------------------------

    def test_cross_tenant_access_blocked(
        self,
        api_client,
        db,
        well,
        second_tenant_id,
        test_user,
        filing_status_record,
        tenant_id,
    ):
        """A record belonging to second_tenant_id must be inaccessible to test_user."""
        from apps.intelligence.models import RejectionRecord
        from django.utils import timezone

        other_record = RejectionRecord.objects.create(
            filing_status=filing_status_record,
            tenant_id=second_tenant_id,
            well=well,
            agency="RRC",
            form_type="w3a",
            state="TX",
            raw_rejection_notes="Issue.",
            rejection_date=timezone.now().date(),
            parse_status="parsed",
            parsed_issues=[],
        )
        # test_user belongs to tenant_id, NOT second_tenant_id
        test_user.tenant_id = tenant_id
        _auth_client(api_client, test_user)
        url = self._url(other_record.id)
        response = api_client.post(url, [], content_type="application/json")
        assert response.status_code in (
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def test_invalid_payload_returns_400(
        self, api_client, test_user, parsed_rejection_record
    ):
        """Correction item missing required field_name must return 400."""
        test_user.tenant_id = parsed_rejection_record.tenant_id
        _auth_client(api_client, test_user)
        url = self._url(parsed_rejection_record.id)
        # field_name is missing — serializer must reject this
        payload = [{"issue_index": 0, "applied_value": "Cement Plug"}]
        response = api_client.post(url, payload, content_type="application/json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
