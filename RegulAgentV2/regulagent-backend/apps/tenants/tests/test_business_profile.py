"""
TDD: Failing tests for the new TenantBusinessProfile model and its endpoints.

These tests define the expected behaviour of the model + endpoints described
in `the-next-thing-that-compressed-avalanche.md` step "0. New TenantBusinessProfile
model (one migration, schema-less)" plus the field registry / adapter exception
referenced in step 5.

Running this file BEFORE implementation should produce ImportError /
AttributeError / NoReverseMatch failures.  That is the "red" signal that keeps
the suite failing correctly until backend engineers ship:

  - apps/tenants/models.py:TenantBusinessProfile  (model + dotted-path helpers)
  - apps/tenants/views.py:TenantBusinessProfileView           (GET/PUT)
  - apps/tenants/views.py:TenantBusinessProfileSchemaView     (GET ?agency=&form=)
  - apps/filing_automation/services/profile_schema.py
      RRC_W3A_REQUIRED, RRC_W3A_OPTIONAL, get_schema(agency, form)
  - apps/filing_automation/services/adapter.py
      BusinessProfileIncomplete(field=...), assert_profile_complete(profile, required)
  - URL routes named "tenant-business-profile" and "tenant-business-profile-schema"

The QA agent has intentionally NOT implemented any of the above — this file
exists purely to define behaviour up front, per the RegulAgent TDD workflow.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from django_tenants.utils import schema_context
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_client(api_client, admin_user):
    """JWT-authenticate an APIClient as *admin_user*."""
    refresh = RefreshToken.for_user(admin_user)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    return api_client


def _make_non_admin_user(tenant, email="member@example.com"):
    """Create a regular (non-admin) user inside *tenant*'s schema."""
    from apps.tenants.models import User

    with schema_context(tenant.schema_name):
        user = User.objects.create_user(
            email=email,
            password="pass1234!",
            is_active=True,
        )
    tenant.add_user(user, is_superuser=False, is_staff=False)
    return user


# ---------------------------------------------------------------------------
# 1. Model basics
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTenantBusinessProfileModel:
    """Direct ORM tests on the new model."""

    def test_can_create_profile_for_tenant(self, test_tenant):
        from apps.tenants.models import TenantBusinessProfile

        with schema_context(test_tenant.schema_name):
            profile = TenantBusinessProfile.objects.create(tenant=test_tenant)

        assert profile.pk is not None
        assert profile.tenant_id == test_tenant.id
        assert profile.data == {}  # JSONField default=dict
        assert profile.created_at is not None
        assert profile.updated_at is not None

    def test_one_to_one_uniqueness_enforced(self, test_tenant):
        """A second profile for the same tenant must violate uniqueness."""
        from django.db import IntegrityError

        from apps.tenants.models import TenantBusinessProfile

        with schema_context(test_tenant.schema_name):
            TenantBusinessProfile.objects.create(tenant=test_tenant)
            with pytest.raises(IntegrityError):
                TenantBusinessProfile.objects.create(tenant=test_tenant)

    def test_deleting_tenant_cascades(self, db, public_tenant):
        """Deleting the parent Tenant should cascade-delete its profile."""
        from apps.tenants.models import Tenant, TenantBusinessProfile

        tenant = Tenant.objects.create(
            name="Cascade Test", slug="cascade-test", schema_name="cascade_test"
        )
        try:
            with schema_context(tenant.schema_name):
                profile = TenantBusinessProfile.objects.create(tenant=tenant)
                profile_pk = profile.pk

            tenant.delete(force_drop=True)

            assert not TenantBusinessProfile.objects.filter(pk=profile_pk).exists()
        finally:
            # Defensive cleanup if delete failed mid-way
            try:
                tenant.delete(force_drop=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 2. Dotted-path JSON helpers
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDottedPathHelpers:
    """profile.get(), profile.set(), profile.merge() — schema-less navigation."""

    def _make_profile(self, tenant):
        from apps.tenants.models import TenantBusinessProfile

        with schema_context(tenant.schema_name):
            return TenantBusinessProfile.objects.create(tenant=tenant)

    def test_get_returns_nested_value(self, test_tenant):
        profile = self._make_profile(test_tenant)
        profile.data = {"rrc": {"w3a": {"cementing_company_name": "Halliburton"}}}
        profile.save()

        assert (
            profile.get("rrc.w3a.cementing_company_name") == "Halliburton"
        )

    def test_get_missing_path_returns_none(self, test_tenant):
        profile = self._make_profile(test_tenant)
        assert profile.get("rrc.w3a.no_such_field") is None

    def test_get_missing_path_returns_supplied_default(self, test_tenant):
        profile = self._make_profile(test_tenant)
        assert profile.get("rrc.w3a.contact_phone", default="555-0000") == "555-0000"

    def test_set_creates_intermediate_dicts(self, test_tenant):
        profile = self._make_profile(test_tenant)
        profile.set("rrc.w3a.contact_phone", "555-1212")
        # Intermediate dicts must be built
        assert profile.data == {"rrc": {"w3a": {"contact_phone": "555-1212"}}}

    def test_set_then_get_round_trip(self, test_tenant):
        profile = self._make_profile(test_tenant)
        profile.set("rrc.w3a.contact_email", "ops@example.com")
        assert profile.get("rrc.w3a.contact_email") == "ops@example.com"

    def test_set_persists_after_save_and_refresh(self, test_tenant):
        from apps.tenants.models import TenantBusinessProfile

        profile = self._make_profile(test_tenant)
        profile.set("rrc.w3a.submitter_default_name", "Jane Doe")
        profile.save()

        with schema_context(test_tenant.schema_name):
            reloaded = TenantBusinessProfile.objects.get(pk=profile.pk)
        assert reloaded.get("rrc.w3a.submitter_default_name") == "Jane Doe"

    def test_merge_is_deep_not_replace(self, test_tenant):
        """merge({rrc:{w3a:{b:2}}}) into {rrc:{w3a:{a:1}}} must keep both keys."""
        profile = self._make_profile(test_tenant)
        profile.data = {"rrc": {"w3a": {"contact_phone": "555-1111"}}}
        profile.save()

        profile.merge({"rrc": {"w3a": {"contact_email": "ops@example.com"}}})

        assert profile.get("rrc.w3a.contact_phone") == "555-1111"
        assert profile.get("rrc.w3a.contact_email") == "ops@example.com"

    def test_merge_overwrites_scalar_leaves(self, test_tenant):
        profile = self._make_profile(test_tenant)
        profile.set("rrc.w3a.contact_phone", "555-1111")
        profile.merge({"rrc": {"w3a": {"contact_phone": "555-9999"}}})
        assert profile.get("rrc.w3a.contact_phone") == "555-9999"


# ---------------------------------------------------------------------------
# 3. GET /api/tenant/business-profile/
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGetBusinessProfileEndpoint:
    """GET /api/tenant/business-profile/ — admin returns the JSON profile."""

    def test_unauthenticated_returns_401(self, api_client, test_tenant):
        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-business-profile")
            response = api_client.get(url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_non_admin_returns_403(self, api_client, test_tenant):
        member = _make_non_admin_user(test_tenant, email="member@example.com")
        client = _admin_client(api_client, member)

        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-business-profile")
            response = client.get(url)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_admin_returns_profile_json(
        self, api_client, test_tenant, tenant_admin
    ):
        from apps.tenants.models import TenantBusinessProfile

        with schema_context(test_tenant.schema_name):
            profile = TenantBusinessProfile.objects.create(tenant=test_tenant)
            profile.data = {"rrc": {"w3a": {"contact_phone": "555-1212"}}}
            profile.save()

        client = _admin_client(api_client, tenant_admin)
        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-business-profile")
            response = client.get(url)

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert "data" in body
        assert body["data"]["rrc"]["w3a"]["contact_phone"] == "555-1212"

    def test_admin_get_auto_creates_or_returns_empty(
        self, api_client, test_tenant, tenant_admin
    ):
        """
        Per plan: GET should auto-create-on-read so the frontend always has a
        profile to render.  Either 200 with empty data OR 404 is acceptable per
        plan wording — we assert one of the two.  If the impl picks 404, the
        frontend must POST/PUT first; if 200, an empty profile is created.
        """
        client = _admin_client(api_client, tenant_admin)
        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-business-profile")
            response = client.get(url)

        assert response.status_code in (
            status.HTTP_200_OK,
            status.HTTP_404_NOT_FOUND,
        )
        if response.status_code == status.HTTP_200_OK:
            assert response.json().get("data", None) in ({}, None) or isinstance(
                response.json()["data"], dict
            )


# ---------------------------------------------------------------------------
# 4. PUT /api/tenant/business-profile/
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPutBusinessProfileEndpoint:
    """PUT must MERGE (not replace), enforce admin-only, and respect tenants."""

    def test_non_admin_put_returns_403(self, api_client, test_tenant):
        member = _make_non_admin_user(test_tenant, email="member2@example.com")
        client = _admin_client(api_client, member)

        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-business-profile")
            response = client.put(
                url,
                {"rrc": {"w3a": {"contact_phone": "555-1212"}}},
                format="json",
            )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_put_merges_does_not_replace(
        self, api_client, test_tenant, tenant_admin
    ):
        """Set one field via direct ORM, then PUT a DIFFERENT field — both must remain."""
        from apps.tenants.models import TenantBusinessProfile

        with schema_context(test_tenant.schema_name):
            profile = TenantBusinessProfile.objects.create(tenant=test_tenant)
            profile.data = {
                "rrc": {"w3a": {"cementing_company_name": "Halliburton"}}
            }
            profile.save()

        client = _admin_client(api_client, tenant_admin)
        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-business-profile")
            put_response = client.put(
                url,
                {"rrc": {"w3a": {"contact_phone": "555-1212"}}},
                format="json",
            )

        assert put_response.status_code in (
            status.HTTP_200_OK,
            status.HTTP_202_ACCEPTED,
        )

        # Re-GET and confirm BOTH fields present
        with schema_context(test_tenant.schema_name):
            get_response = client.get(url)
            assert get_response.status_code == status.HTTP_200_OK
            data = get_response.json()["data"]
            assert data["rrc"]["w3a"]["cementing_company_name"] == "Halliburton"
            assert data["rrc"]["w3a"]["contact_phone"] == "555-1212"

    def test_put_is_tenant_isolated(
        self, api_client, public_tenant, db
    ):
        """
        Two tenants each with their own admin and own profile.  A PUT inside
        tenant A's schema must not affect tenant B's profile.
        """
        from apps.tenants.models import Tenant, Domain, TenantBusinessProfile, User

        # ---- create tenant A
        tenant_a = Tenant.objects.create(
            name="Iso A", slug="iso-a", schema_name="iso_a"
        )
        Domain.objects.create(domain="iso-a.localhost", tenant=tenant_a, is_primary=True)

        # ---- create tenant B
        tenant_b = Tenant.objects.create(
            name="Iso B", slug="iso-b", schema_name="iso_b"
        )
        Domain.objects.create(domain="iso-b.localhost", tenant=tenant_b, is_primary=True)

        try:
            # ---- admin user in each tenant
            from django_tenants.utils import get_public_schema_name
            with schema_context(get_public_schema_name()):
                admin_a = User.objects.create_user(
                    email="adminA@example.com", password="passA123!", is_active=True
                )
                admin_b = User.objects.create_user(
                    email="adminB@example.com", password="passB123!", is_active=True
                )
            tenant_a.add_user(admin_a, is_superuser=True, is_staff=True)
            tenant_b.add_user(admin_b, is_superuser=True, is_staff=True)

            # ---- pre-seed a value in tenant B's profile we expect to remain UNTOUCHED
            with schema_context(tenant_b.schema_name):
                profile_b = TenantBusinessProfile.objects.create(tenant=tenant_b)
                profile_b.data = {
                    "rrc": {"w3a": {"cementing_company_name": "B-Cementing-Co"}}
                }
                profile_b.save()

            # ---- PUT into tenant A's schema as admin A
            from rest_framework.test import APIClient

            client = APIClient()
            refresh = RefreshToken.for_user(admin_a)
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

            with schema_context(tenant_a.schema_name):
                url = reverse("tenant-business-profile")
                resp = client.put(
                    url,
                    {"rrc": {"w3a": {"cementing_company_name": "A-Cementing-Co"}}},
                    format="json",
                )
                assert resp.status_code in (
                    status.HTTP_200_OK,
                    status.HTTP_202_ACCEPTED,
                )

            # ---- tenant B's profile must be untouched
            with schema_context(tenant_b.schema_name):
                profile_b.refresh_from_db()
                assert (
                    profile_b.data["rrc"]["w3a"]["cementing_company_name"]
                    == "B-Cementing-Co"
                )
        finally:
            for t in (tenant_a, tenant_b):
                try:
                    t.delete(force_drop=True)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# 5. GET /api/tenant/business-profile/schema/?agency=rrc&form=w3a
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBusinessProfileSchemaEndpoint:
    """Field registry exposed for the frontend settings page to render dynamically."""

    def test_unknown_agency_returns_400(self, api_client, test_tenant, tenant_admin):
        client = _admin_client(api_client, tenant_admin)
        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-business-profile-schema")
            response = client.get(url, {"agency": "xxxxx", "form": "w3a"})
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_unknown_form_returns_400(self, api_client, test_tenant, tenant_admin):
        client = _admin_client(api_client, tenant_admin)
        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-business-profile-schema")
            response = client.get(url, {"agency": "rrc", "form": "not-real"})
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_rrc_w3a_returns_required_and_optional(
        self, api_client, test_tenant, tenant_admin
    ):
        client = _admin_client(api_client, tenant_admin)
        with schema_context(test_tenant.schema_name):
            url = reverse("tenant-business-profile-schema")
            response = client.get(url, {"agency": "rrc", "form": "w3a"})

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert "required" in body
        assert "optional" in body

        # The registry constants per the plan
        expected_required = {
            "rrc.w3a.cementing_company_name",
            "rrc.w3a.contact_phone",
            "rrc.w3a.contact_email",
            "rrc.w3a.submitter_default_name",
            "rrc.w3a.submitter_default_title",
        }
        expected_optional = {
            "rrc.w3a.default_plugging_date_offset_days",
        }
        assert expected_required.issubset(set(body["required"]))
        assert expected_optional.issubset(set(body["optional"]))


# ---------------------------------------------------------------------------
# 6. Profile-completeness exception (adapter helper)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBusinessProfileIncompleteException:
    """
    `BusinessProfileIncomplete` is raised by the adapter when a required dotted
    path is missing from the profile.  The exception's `.field` attribute
    carries the missing path so the submit endpoint can surface it to the user.
    """

    def test_assert_profile_complete_raises_for_missing_field(self, test_tenant):
        from apps.filing_automation.services.adapter import (
            BusinessProfileIncomplete,
            assert_profile_complete,
        )
        from apps.filing_automation.services.profile_schema import RRC_W3A_REQUIRED
        from apps.tenants.models import TenantBusinessProfile

        with schema_context(test_tenant.schema_name):
            profile = TenantBusinessProfile.objects.create(tenant=test_tenant)
            # Empty data — every required key is missing
            with pytest.raises(BusinessProfileIncomplete) as exc_info:
                assert_profile_complete(profile, RRC_W3A_REQUIRED)

        # The first missing key in the canonical order should be flagged
        assert exc_info.value.field == "rrc.w3a.cementing_company_name"

    def test_assert_profile_complete_passes_when_all_required_present(
        self, test_tenant
    ):
        from apps.filing_automation.services.adapter import (
            assert_profile_complete,
        )
        from apps.filing_automation.services.profile_schema import RRC_W3A_REQUIRED
        from apps.tenants.models import TenantBusinessProfile

        with schema_context(test_tenant.schema_name):
            profile = TenantBusinessProfile.objects.create(tenant=test_tenant)
            for key in RRC_W3A_REQUIRED:
                profile.set(key, "filled")
            profile.save()

            # Should not raise
            assert_profile_complete(profile, RRC_W3A_REQUIRED)

    def test_exception_field_attribute_is_dotted_path(self):
        """Standalone — does not require ORM."""
        from apps.filing_automation.services.adapter import BusinessProfileIncomplete

        exc = BusinessProfileIncomplete(field="rrc.w3a.contact_email")
        assert exc.field == "rrc.w3a.contact_email"
        # Also expect a useful str() representation
        assert "rrc.w3a.contact_email" in str(exc)
