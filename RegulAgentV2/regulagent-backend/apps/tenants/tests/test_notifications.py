"""
TDD: Failing tests for the Notification system.

These tests define the expected behaviour BEFORE implementation.
Running this file will produce failures because:
  - apps.tenants.models.Notification does not yet exist
  - GET /api/notifications/ endpoint does not yet exist
  - GET /api/notifications/unread-count/ endpoint does not yet exist
  - POST /api/notifications/{id}/read/ endpoint does not yet exist
  - POST /api/notifications/read-all/ endpoint does not yet exist
  - apps.tenants.tasks.check_token_usage_thresholds does not yet exist

All 9 tests MUST FAIL until the implementation is delivered.

Model spec
----------
Notification fields:
  id          — UUID primary key
  user        — FK to AUTH_USER_MODEL
  tenant_id   — UUIDField
  verb        — CharField(max_length=255)
  message     — TextField(blank=True)
  notif_type  — CharField choices: 'info'/'success'/'warning'/'error', default='info'
  action_url  — CharField(blank=True)
  read        — BooleanField(default=False)
  read_at     — DateTimeField(null=True)
  created_at  — DateTimeField(auto_now_add=True)

API endpoints:
  GET  /api/notifications/              — paginated list for auth user's tenant
  GET  /api/notifications/unread-count/ — {"count": N}
  POST /api/notifications/{id}/read/    — marks one notification read
  POST /api/notifications/read-all/     — marks all notifications read for user
"""

import uuid
from unittest.mock import patch

import pytest
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

# ---------------------------------------------------------------------------
# Guard imports — these MUST fail (ImportError) until implemented.
# The ImportError is the canonical "red" TDD signal.
# ---------------------------------------------------------------------------

from apps.tenants.models import Notification  # noqa: F401 — fails until model exists
from apps.tenants.tasks import check_token_usage_thresholds  # noqa: F401 — fails until task exists


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(suffix: str):
    """Create a unique, active user in the public schema."""
    from apps.tenants.models import User

    with schema_context(get_public_schema_name()):
        return User.objects.create_user(
            email=f"notif-user-{suffix}@example.com",
            password="testpass123",
            is_active=True,
        )


def _auth_client(user) -> APIClient:
    """Return a JWT-authenticated DRF APIClient for *user*."""
    client = APIClient()
    refresh = RefreshToken.for_user(user)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    return client


def _make_notification(user, tenant_id, **kwargs) -> "Notification":
    """Create a Notification with sensible defaults."""
    defaults = dict(
        verb="Test event occurred",
        message="This is a test notification.",
        notif_type="info",
        action_url="",
        read=False,
        read_at=None,
    )
    defaults.update(kwargs)
    return Notification.objects.create(
        user=user,
        tenant_id=tenant_id,
        **defaults,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def notif_user(db, public_tenant, test_tenant):
    """User enrolled in test_tenant, used across notification tests."""
    uid = str(uuid.uuid4())[:8]
    user = _make_user(uid)
    test_tenant.add_user(user, is_superuser=False, is_staff=False)
    return user


@pytest.fixture()
def notif_client(notif_user) -> APIClient:
    """JWT-authenticated APIClient for notif_user."""
    return _auth_client(notif_user)


# ---------------------------------------------------------------------------
# Test 1: Model creation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestNotificationModelCreate:
    """Verify the Notification model persists all fields correctly."""

    def test_notification_model_create(self, db, notif_user, test_tenant):
        """
        Create a Notification and verify every declared field survives a
        round-trip through the ORM.

        FAILS until apps.tenants.models.Notification is defined with the
        correct field set.
        """
        notif = Notification.objects.create(
            user=notif_user,
            tenant_id=test_tenant.id,
            verb="Token budget exceeded",
            message="You have used 85% of your monthly token budget.",
            notif_type="warning",
            action_url="/settings/billing",
            read=False,
            read_at=None,
        )

        # Reload from DB to confirm persistence
        saved = Notification.objects.get(pk=notif.pk)

        assert saved.id is not None, "id must be set (UUID PK)"
        assert saved.user_id == notif_user.pk, "user FK must be stored"
        assert saved.tenant_id == test_tenant.id, "tenant_id must be stored"
        assert saved.verb == "Token budget exceeded"
        assert saved.message == "You have used 85% of your monthly token budget."
        assert saved.notif_type == "warning"
        assert saved.action_url == "/settings/billing"
        assert saved.read is False
        assert saved.read_at is None
        assert saved.created_at is not None, "created_at must be auto-populated"


# ---------------------------------------------------------------------------
# Test 2: Unread-count endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUnreadCountEndpoint:
    """GET /api/notifications/unread-count/ returns the correct count."""

    def test_unread_count_endpoint(self, db, notif_user, notif_client, test_tenant):
        """
        Authenticated GET /api/notifications/unread-count/ must return
        {"count": N} where N equals the number of unread Notification rows
        owned by notif_user within test_tenant.

        FAILS until:
          - Notification model exists
          - The unread-count view is registered at the expected URL
        """
        # Seed 3 unread + 1 already-read notification
        from django.utils import timezone

        _make_notification(notif_user, test_tenant.id, read=False)
        _make_notification(notif_user, test_tenant.id, read=False)
        _make_notification(notif_user, test_tenant.id, read=False)
        _make_notification(
            notif_user,
            test_tenant.id,
            read=True,
            read_at=timezone.now(),
        )

        response = notif_client.get("/api/notifications/unread-count/")

        assert response.status_code == status.HTTP_200_OK, (
            f"Expected 200, got {response.status_code}: {response.data}"
        )
        data = response.json()
        assert "count" in data, "Response body must contain 'count' key"
        assert data["count"] == 3, (
            f"Expected unread count=3, got {data['count']}"
        )


# ---------------------------------------------------------------------------
# Test 3: List endpoint — tenant + user isolation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestNotificationListIsolation:
    """GET /api/notifications/ must enforce per-user and per-tenant isolation."""

    def test_list_filters_by_tenant_and_user(
        self, db, public_tenant, test_tenant, notif_user, notif_client
    ):
        """
        User A's notifications must not be visible to User B, even when they
        share the same tenant.  Cross-tenant notifications must also be hidden.

        FAILS until:
          - Notification model exists
          - GET /api/notifications/ view filters by (user, tenant_id)
        """
        uid_b = str(uuid.uuid4())[:8]
        user_b = _make_user(uid_b)
        test_tenant.add_user(user_b, is_superuser=False, is_staff=False)

        # Tenant A with a different schema (cross-tenant scenario)
        other_tenant_id = uuid.uuid4()

        # notif_user's notification — should be visible
        own_notif = _make_notification(notif_user, test_tenant.id, verb="Own notification")

        # User B's notification in same tenant — must NOT be visible to notif_user
        _make_notification(user_b, test_tenant.id, verb="User B notification")

        # notif_user's notification in a different tenant — must NOT appear
        _make_notification(notif_user, other_tenant_id, verb="Cross-tenant notification")

        response = notif_client.get("/api/notifications/")

        assert response.status_code == status.HTTP_200_OK, (
            f"Expected 200, got {response.status_code}: {response.data}"
        )

        # Handle both paginated and plain list responses
        data = response.json()
        results = data.get("results", data) if isinstance(data, dict) else data

        verbs = [n["verb"] for n in results]

        assert "Own notification" in verbs, (
            "notif_user's own notification must appear in the list"
        )
        assert "User B notification" not in verbs, (
            "User B's notification must not appear in notif_user's list"
        )
        assert "Cross-tenant notification" not in verbs, (
            "Cross-tenant notification must not appear in notif_user's list"
        )


# ---------------------------------------------------------------------------
# Test 4: Mark single notification read
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestMarkRead:
    """POST /api/notifications/{id}/read/ sets read=True and read_at."""

    def test_mark_read_sets_read_at(self, db, notif_user, notif_client, test_tenant):
        """
        Posting to /api/notifications/{id}/read/ must flip read=True and
        populate read_at with a non-null timestamp.

        FAILS until:
          - Notification model exists
          - The read action view is registered at the expected URL
        """
        notif = _make_notification(notif_user, test_tenant.id, read=False, read_at=None)

        response = notif_client.post(f"/api/notifications/{notif.id}/read/")

        assert response.status_code in (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT), (
            f"Expected 200 or 204, got {response.status_code}: {response.data}"
        )

        notif.refresh_from_db()
        assert notif.read is True, "read must be True after POST to /read/"
        assert notif.read_at is not None, "read_at must be set after marking read"


# ---------------------------------------------------------------------------
# Test 5: Mark all notifications read
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestMarkAllRead:
    """POST /api/notifications/read-all/ marks every unread notification read."""

    def test_mark_all_read(self, db, notif_user, notif_client, test_tenant):
        """
        POST /api/notifications/read-all/ must set read=True (and read_at) on
        every unread Notification owned by the authenticated user.

        FAILS until:
          - Notification model exists
          - The read-all action view is registered at the expected URL
        """
        n1 = _make_notification(notif_user, test_tenant.id, verb="Unread 1", read=False)
        n2 = _make_notification(notif_user, test_tenant.id, verb="Unread 2", read=False)
        n3 = _make_notification(notif_user, test_tenant.id, verb="Unread 3", read=False)

        response = notif_client.post("/api/notifications/read-all/")

        assert response.status_code in (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT), (
            f"Expected 200 or 204, got {response.status_code}: {response.data}"
        )

        for notif in [n1, n2, n3]:
            notif.refresh_from_db()
            assert notif.read is True, f"Notification '{notif.verb}' must be marked read"
            assert notif.read_at is not None, (
                f"Notification '{notif.verb}' must have read_at set"
            )


# ---------------------------------------------------------------------------
# Tests 6–9: check_token_usage_thresholds Celery task
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTokenUsageThresholdTask:
    """
    Unit tests for check_token_usage_thresholds().

    The task is expected to:
      - Call get_monthly_token_usage() for each active tenant
      - Create a Notification when usage >= 50%
      - Deduplicate — one notification per threshold crossing per billing cycle
      - Call send_usage_alert_email() when threshold breached
      - Create NO Notification when usage < 50%

    All tests mock get_monthly_token_usage and send_usage_alert_email so
    they don't require real DB token records or SMTP access.

    FAILS until apps.tenants.tasks.check_token_usage_thresholds is implemented.
    """

    # ------------------------------------------------------------------
    # Shared mock payload representing 55% usage
    # ------------------------------------------------------------------
    _USAGE_55_PCT = {
        "percentage": 55,
        "tokens_used": 550_000,
        "monthly_budget": 1_000_000,
        "remaining": 450_000,
    }
    _USAGE_40_PCT = {
        "percentage": 40,
        "tokens_used": 400_000,
        "monthly_budget": 1_000_000,
        "remaining": 600_000,
    }

    def test_threshold_task_creates_notification_at_50pct(
        self, db, public_tenant, test_tenant, notif_user
    ):
        """
        check_token_usage_thresholds() must create exactly one Notification
        for notif_user when mocked usage is 55% (>= 50% threshold).

        FAILS until task is implemented and writes a Notification.
        """
        with (
            patch(
                "apps.tenants.tasks.get_monthly_token_usage",
                return_value=self._USAGE_55_PCT,
            ),
            patch("apps.tenants.tasks.send_usage_alert_email"),
        ):
            check_token_usage_thresholds()

        notifs = Notification.objects.filter(
            user=notif_user,
            tenant_id=test_tenant.id,
            notif_type__in=("warning", "error"),
        )
        assert notifs.exists(), (
            "check_token_usage_thresholds() must create a Notification "
            "when token usage >= 50%"
        )

    def test_threshold_task_dedup(
        self, db, public_tenant, test_tenant, notif_user
    ):
        """
        Running check_token_usage_thresholds() twice with the same 55% usage
        must produce only ONE Notification — not two.

        FAILS until the task implements deduplication logic (e.g. checking
        whether a threshold notification was already created this billing cycle).
        """
        with (
            patch(
                "apps.tenants.tasks.get_monthly_token_usage",
                return_value=self._USAGE_55_PCT,
            ),
            patch("apps.tenants.tasks.send_usage_alert_email"),
        ):
            check_token_usage_thresholds()
            check_token_usage_thresholds()

        notif_count = Notification.objects.filter(
            user=notif_user,
            tenant_id=test_tenant.id,
            notif_type__in=("warning", "error"),
        ).count()

        assert notif_count == 1, (
            f"Deduplication failed: expected 1 threshold notification, "
            f"got {notif_count} after running the task twice"
        )

    def test_threshold_task_sends_email(
        self, db, public_tenant, test_tenant, notif_user
    ):
        """
        When the 50% threshold is breached, send_usage_alert_email must be
        called exactly once.

        FAILS until the task calls send_usage_alert_email on threshold breach.
        """
        with (
            patch(
                "apps.tenants.tasks.get_monthly_token_usage",
                return_value=self._USAGE_55_PCT,
            ),
            patch(
                "apps.tenants.tasks.send_usage_alert_email"
            ) as mock_email,
        ):
            check_token_usage_thresholds()

        mock_email.assert_called_once(), (
            "send_usage_alert_email must be called exactly once when the "
            "usage threshold is breached"
        )

    def test_threshold_below_50pct_no_notification(
        self, db, public_tenant, test_tenant, notif_user
    ):
        """
        When mocked usage is 40% (< 50% threshold), check_token_usage_thresholds()
        must NOT create any Notification.

        FAILS until the task correctly gates on the threshold value.
        """
        with (
            patch(
                "apps.tenants.tasks.get_monthly_token_usage",
                return_value=self._USAGE_40_PCT,
            ),
            patch("apps.tenants.tasks.send_usage_alert_email"),
        ):
            check_token_usage_thresholds()

        notif_count = Notification.objects.filter(
            user=notif_user,
            tenant_id=test_tenant.id,
        ).count()

        assert notif_count == 0, (
            f"No Notification should be created when usage is below 50%; "
            f"found {notif_count}"
        )
