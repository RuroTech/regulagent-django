"""
RED-PHASE tests for per-user PortalCredential schema (Phase 1).

None of the new model fields/constraints exist yet — every test in this file
MUST FAIL until BE1 implements:
  - PortalCredential.user FK → AUTH_USER_MODEL (nullable=True)
  - PortalCredential.is_default_for_automation BooleanField (default=False)
  - Uniqueness changed: ('user_id', 'tenant_id', 'agency') replaces ('tenant_id', 'agency')
  - Partial unique constraint: at most one is_default_for_automation=True per (tenant_id, agency)
  - Backfill: existing tenant-only creds assigned to tenant owner + marked is_default_for_automation=True

Conventions match test_credential_circuit_breaker.py:
  - pytest (no unittest.TestCase)
  - @pytest.mark.django_db on class
  - monkeypatch.setenv("ENCRYPTION_PEPPER", ...) before any PortalCredential.set_*() call
  - PortalCredential(tenant_id=..., agency=...) → .set_username() / .set_password() / .save()
  - Fixtures from apps/conftest.py and apps/intelligence/tests/conftest.py
"""
import uuid

import pytest
from django.conf import settings
from django.db import IntegrityError

from apps.intelligence.models import PortalCredential


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_credential(monkeypatch, tenant_id, user=None, agency="RRC",
                     username="u@rrc.tx.us", password="P@ss!"):
    """
    Create and save a PortalCredential. Passes `user` if supplied (will fail
    on the DB call until BE1 adds the field).
    """
    monkeypatch.setenv("ENCRYPTION_PEPPER", "test-pepper-for-unit-tests")
    kwargs = dict(tenant_id=tenant_id, agency=agency)
    if user is not None:
        kwargs["user"] = user
    cred = PortalCredential(**kwargs)
    cred.set_username(username)
    cred.set_password(password)
    cred.save()
    return cred


# ===========================================================================
# 1. Per-user storage — two users in the same tenant, same agency
# ===========================================================================


@pytest.mark.django_db
class TestPerUserUniqueness:
    """
    Two different users in the same tenant can each hold a credential for the
    same agency. Current model has unique_together = [('tenant_id', 'agency')],
    which would raise IntegrityError on the second insert.
    """

    def test_two_users_can_each_have_rrc_cred_in_same_tenant(
        self, monkeypatch, test_tenant, public_tenant
    ):
        """
        Two distinct users share the same tenant_id. Both save an RRC
        PortalCredential. After Phase 1 the second save must succeed; right
        now it raises IntegrityError.

        RED reason: unique_together = [('tenant_id', 'agency')] blocks the
        second insert.
        """
        from apps.tenants.models import User

        tenant_uuid = str(uuid.UUID(int=test_tenant.id))

        user_a = User.objects.create_user(
            email="alice@example.com", password="alicepass", is_active=True
        )
        user_b = User.objects.create_user(
            email="bob@example.com", password="bobpass", is_active=True
        )

        # First credential — should always succeed
        _make_credential(
            monkeypatch, tenant_uuid, user=user_a, username="alice@rrc.tx.us"
        )

        # Second credential for the same (tenant, agency) but different user —
        # must succeed after Phase 1; currently raises IntegrityError.
        _make_credential(
            monkeypatch, tenant_uuid, user=user_b, username="bob@rrc.tx.us"
        )

        count = PortalCredential.objects.filter(
            tenant_id=tenant_uuid, agency="RRC"
        ).count()
        assert count == 2, (
            "Expected 2 credentials (one per user) for the same (tenant, agency) pair"
        )

    def test_same_user_cannot_have_duplicate_cred_for_same_agency(
        self, monkeypatch, test_tenant, public_tenant
    ):
        """
        A single user must NOT be able to create two credentials for the same
        (user_id, tenant_id, agency) triple.

        RED reason: `user` field does not exist yet; the IntegrityError comes
        from the old (tenant_id, agency) constraint, not the new one, so the
        right constraint isn't being enforced even when this 'passes'.
        We assert the new constraint is present by triggering the correct path.
        """
        from apps.tenants.models import User

        tenant_uuid = str(uuid.UUID(int=test_tenant.id))
        user_a = User.objects.create_user(
            email="carol@example.com", password="carolpass", is_active=True
        )

        _make_credential(
            monkeypatch, tenant_uuid, user=user_a, username="carol@rrc.tx.us"
        )

        with pytest.raises(IntegrityError):
            _make_credential(
                monkeypatch, tenant_uuid, user=user_a, username="carol2@rrc.tx.us"
            )


# ===========================================================================
# 2. is_default_for_automation field exists and partial unique constraint works
# ===========================================================================


@pytest.mark.django_db
class TestDefaultForAutomation:
    """
    is_default_for_automation BooleanField must exist (default False).
    A partial unique constraint must prevent two creds with
    is_default_for_automation=True for the same (tenant_id, agency).
    """

    def test_is_default_for_automation_field_exists_and_defaults_false(
        self, monkeypatch, tenant_id
    ):
        """
        After saving a credential, is_default_for_automation must be False
        by default.

        RED reason: the field does not exist yet — AttributeError on access,
        or migration missing from DB.
        """
        cred = _make_credential(monkeypatch, tenant_id)
        assert hasattr(cred, "is_default_for_automation"), (
            "PortalCredential must have an is_default_for_automation field"
        )
        assert cred.is_default_for_automation is False

    def test_can_set_is_default_for_automation_true(
        self, monkeypatch, tenant_id, test_user
    ):
        """
        Saving a credential with is_default_for_automation=True must succeed
        when no other credential for (tenant_id, agency) is already True.

        RED reason: field doesn't exist yet.
        """
        monkeypatch.setenv("ENCRYPTION_PEPPER", "test-pepper-for-unit-tests")
        cred = PortalCredential(
            tenant_id=tenant_id,
            agency="RRC",
            user=test_user,
            is_default_for_automation=True,
        )
        cred.set_username("auto@rrc.tx.us")
        cred.set_password("AutoP@ss!")
        cred.save()

        cred.refresh_from_db()
        assert cred.is_default_for_automation is True

    def test_two_defaults_for_same_tenant_agency_raises_integrity_error(
        self, monkeypatch, test_tenant, public_tenant
    ):
        """
        Attempting to mark two credentials is_default_for_automation=True for
        the same (tenant_id, agency) must raise IntegrityError (partial unique
        constraint).

        RED reason: field + constraint do not exist yet.
        """
        from apps.tenants.models import User

        tenant_uuid = str(uuid.UUID(int=test_tenant.id))

        user_a = User.objects.create_user(
            email="dave@example.com", password="davepass", is_active=True
        )
        user_b = User.objects.create_user(
            email="eve@example.com", password="evepass", is_active=True
        )

        monkeypatch.setenv("ENCRYPTION_PEPPER", "test-pepper-for-unit-tests")

        cred_a = PortalCredential(
            tenant_id=tenant_uuid,
            agency="RRC",
            user=user_a,
            is_default_for_automation=True,
        )
        cred_a.set_username("dave@rrc.tx.us")
        cred_a.set_password("DaveP@ss!")
        cred_a.save()

        # A second credential marked as default for the same (tenant, agency)
        # must violate the partial unique constraint.
        with pytest.raises(IntegrityError):
            cred_b = PortalCredential(
                tenant_id=tenant_uuid,
                agency="RRC",
                user=user_b,
                is_default_for_automation=True,
            )
            cred_b.set_username("eve@rrc.tx.us")
            cred_b.set_password("EveP@ss!")
            cred_b.save()

    def test_two_defaults_for_different_agencies_are_allowed(
        self, monkeypatch, tenant_id, test_user
    ):
        """
        One credential per agency can be the default. Two defaults for
        distinct agencies in the same tenant must NOT collide.

        RED reason: field doesn't exist yet; also validates the partial
        constraint is scoped to (tenant_id, agency), not just tenant_id.
        """
        monkeypatch.setenv("ENCRYPTION_PEPPER", "test-pepper-for-unit-tests")

        cred_rrc = PortalCredential(
            tenant_id=tenant_id,
            agency="RRC",
            user=test_user,
            is_default_for_automation=True,
        )
        cred_rrc.set_username("auto@rrc.tx.us")
        cred_rrc.set_password("P1!")
        cred_rrc.save()

        cred_nmocd = PortalCredential(
            tenant_id=tenant_id,
            agency="NMOCD",
            user=test_user,
            is_default_for_automation=True,
        )
        cred_nmocd.set_username("auto@emnrd.nm.gov")
        cred_nmocd.set_password("P2!")
        cred_nmocd.save()

        assert PortalCredential.objects.filter(
            tenant_id=tenant_id, is_default_for_automation=True
        ).count() == 2


# ===========================================================================
# 3. user FK field exists and is nullable in Phase 1
# ===========================================================================


@pytest.mark.django_db
class TestUserFKNullable:
    """
    The new `user` FK must exist on the model, must accept null (nullable=True
    in Phase 1), and must be a FK to AUTH_USER_MODEL.
    """

    def test_user_field_exists_on_model(self):
        """
        PortalCredential must have a `user` field.

        RED reason: field does not exist yet.
        """
        assert hasattr(PortalCredential, "user"), (
            "PortalCredential must have a `user` field (FK to AUTH_USER_MODEL)"
        )

    def test_user_field_is_nullable(self, monkeypatch, tenant_id):
        """
        Creating a PortalCredential WITHOUT setting user must succeed (null=True).

        RED reason: even if the field existed and was NOT nullable, this would
        fail at the DB layer. Also fails now because the field doesn't exist.
        """
        cred = _make_credential(monkeypatch, tenant_id)
        assert cred.user is None, (
            "PortalCredential.user must be nullable in Phase 1"
        )

    def test_user_fk_points_to_auth_user_model(self, monkeypatch, tenant_id, test_user):
        """
        Assigning a real User instance and saving must persist the FK correctly.

        RED reason: field doesn't exist yet.
        """
        cred = _make_credential(monkeypatch, tenant_id, user=test_user)
        cred.refresh_from_db()

        assert cred.user_id == test_user.pk, (
            "PortalCredential.user_id must equal the saved user's PK"
        )

    def test_user_field_references_auth_user_model(self):
        """
        The `user` field's related model must be AUTH_USER_MODEL.

        RED reason: field doesn't exist.
        """
        field = PortalCredential._meta.get_field("user")
        expected_model = settings.AUTH_USER_MODEL
        # Django stores related_model as the actual class; compare via label
        actual_label = (
            f"{field.related_model._meta.app_label}.{field.related_model._meta.model_name}"
        )
        assert actual_label == expected_model.lower(), (
            f"user FK must point to {expected_model}, got {actual_label}"
        )


# ===========================================================================
# 4. Encryption round-trip is unchanged (key remains tenant-derived)
# ===========================================================================


@pytest.mark.django_db
class TestEncryptionUnchangedAfterUserFK:
    """
    Adding the `user` FK must not affect the Fernet key derivation.
    Key is still derived from (tenant_id, ENCRYPTION_PEPPER, key_salt).
    Round-tripping username and password must work correctly.

    These tests are currently GREEN for the model (encryption already works),
    but they MUST stay green after BE1 adds the user field. The red phase
    here is: if BE1 mistakenly folds user_id into the key, these will fail.

    We mark them xfail=False (i.e., expected to pass after implementation),
    but they guard the spec: do NOT change key derivation.
    """

    def test_set_and_get_username_round_trips(self, monkeypatch, tenant_id, test_user):
        """
        set_username / get_username must round-trip correctly when user FK is set.

        Fails NOW because `user` kwarg is rejected (field doesn't exist).
        After BE1: must stay green (encryption key unchanged).
        """
        cred = _make_credential(
            monkeypatch, tenant_id, user=test_user, username="roundtrip@rrc.tx.us"
        )
        cred.refresh_from_db()
        assert cred.get_username() == "roundtrip@rrc.tx.us", (
            "Username must decrypt correctly after saving with user FK set"
        )

    def test_set_and_get_password_round_trips(self, monkeypatch, tenant_id, test_user):
        """
        set_password / get_password must round-trip correctly when user FK is set.

        Fails NOW because `user` kwarg is rejected (field doesn't exist).
        """
        cred = _make_credential(
            monkeypatch, tenant_id, user=test_user, password="S3cr3t!"
        )
        cred.refresh_from_db()
        assert cred.get_password() == "S3cr3t!", (
            "Password must decrypt correctly after saving with user FK set"
        )

    def test_key_is_NOT_derived_from_user_id(self, monkeypatch, tenant_id, test_user):
        """
        The key derivation must NOT include user_id. Verify by creating a
        credential with user set, reading back encrypted bytes, then decrypting
        using only tenant_id + salt — without any user context.

        Fails NOW because `user` kwarg is rejected (field doesn't exist).
        After BE1: must succeed (proves key is still tenant-only).
        """
        import base64
        import hashlib
        from cryptography.fernet import Fernet

        cred = _make_credential(
            monkeypatch, tenant_id, user=test_user, username="keycheck@rrc.tx.us"
        )
        cred.refresh_from_db()

        # Re-derive the key the same way _derive_key() does — WITHOUT user_id
        pepper = "test-pepper-for-unit-tests"
        salt = bytes(cred.key_salt)
        raw_key = hashlib.pbkdf2_hmac(
            "sha256",
            f"{tenant_id}:{pepper}".encode(),
            salt,
            iterations=100_000,
            dklen=32,
        )
        key = base64.urlsafe_b64encode(raw_key)
        f = Fernet(key)
        decrypted = f.decrypt(bytes(cred.encrypted_username)).decode()

        assert decrypted == "keycheck@rrc.tx.us", (
            "Username must be decryptable using ONLY tenant_id + pepper + salt "
            "(user_id must NOT be part of key derivation)"
        )


# ===========================================================================
# 5. Backfill: existing credential gets user assigned + is_default_for_automation=True
# ===========================================================================


@pytest.mark.django_db
class TestBackfillBehavior:
    """
    After the data migration runs, an existing credential that had no `user`
    (null) must be:
      - assigned to the tenant's owner/admin user
      - marked is_default_for_automation=True

    We test the intended end-state rather than invoking a migration runner.
    The test creates a null-user credential (Phase 1 allows null), then
    simulates what the backfill helper should do and asserts the result.

    RED reason: is_default_for_automation field and user FK don't exist yet.
    """

    def test_backfill_assigns_user_and_sets_default_flag(
        self, monkeypatch, test_tenant, public_tenant
    ):
        """
        Simulate the backfill migration logic on a legacy null-user credential:
          1. Create a credential with user=None (legacy row).
          2. Find or create a tenant admin user.
          3. Run the backfill helper (to be implemented as a data migration).
          4. Assert cred.user is set and is_default_for_automation is True.

        Fails NOW because the fields don't exist.
        """
        from apps.tenants.models import User

        tenant_uuid = str(uuid.UUID(int=test_tenant.id))

        # Step 1: Create legacy credential (no user)
        legacy_cred = _make_credential(monkeypatch, tenant_uuid, username="legacy@rrc.tx.us")
        assert legacy_cred.user is None  # legacy row

        # Step 2: Create a tenant admin to be assigned by the backfill
        tenant_admin = User.objects.create_user(
            email="owner@example.com", password="ownerpass", is_active=True
        )
        test_tenant.add_user(tenant_admin, is_superuser=True, is_staff=True)

        # Step 3: Call the backfill helper directly (imported from the migration
        # module once BE1 writes it). We call the expected public symbol.
        from apps.intelligence.migrations import backfill_credential_users  # noqa: F401
        backfill_credential_users(tenant_uuid, legacy_cred, tenant_admin)

        # Step 4: Assertions
        legacy_cred.refresh_from_db()
        assert legacy_cred.user_id is not None, (
            "Backfill must assign a non-null user to the legacy credential"
        )
        assert legacy_cred.user_id == tenant_admin.pk, (
            "Backfill must assign the tenant admin/owner as the credential owner"
        )
        assert legacy_cred.is_default_for_automation is True, (
            "Backfill must set is_default_for_automation=True on the migrated credential"
        )

    def test_backfill_does_not_overwrite_existing_user_assignment(
        self, monkeypatch, test_tenant, public_tenant
    ):
        """
        If a credential already has a user assigned (post-migration row),
        backfill must not overwrite it.

        Fails NOW because the fields don't exist.
        """
        from apps.tenants.models import User

        tenant_uuid = str(uuid.UUID(int=test_tenant.id))

        existing_user = User.objects.create_user(
            email="frank@example.com", password="frankpass", is_active=True
        )
        admin_user = User.objects.create_user(
            email="owner2@example.com", password="ownerpass2", is_active=True
        )
        test_tenant.add_user(admin_user, is_superuser=True, is_staff=True)

        # Credential already has a user set
        already_assigned_cred = _make_credential(
            monkeypatch, tenant_uuid, user=existing_user, username="frank@rrc.tx.us"
        )

        from apps.intelligence.migrations import backfill_credential_users  # noqa: F401
        backfill_credential_users(tenant_uuid, already_assigned_cred, admin_user)

        already_assigned_cred.refresh_from_db()
        assert already_assigned_cred.user_id == existing_user.pk, (
            "Backfill must not overwrite an already-assigned user on a credential"
        )
