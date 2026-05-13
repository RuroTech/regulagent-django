"""
TDD: Tests for Card 13 — welcome email on new tenant user creation.

Components under test
---------------------
apps/tenants/services/email_service.py  — send_welcome_email(user, temp_password)
apps/tenants/tasks.py                   — send_welcome_email_task(user_id, temp_password)
TenantUserListCreateView.post()         — dispatches task after user creation
"""

import pytest
from unittest.mock import patch, MagicMock
from django.test import override_settings
from django.core import mail
from django_tenants.utils import schema_context, get_public_schema_name

# ---------------------------------------------------------------------------
# These imports confirm the implementation exists.
# ---------------------------------------------------------------------------
from apps.tenants.services.email_service import send_welcome_email  # noqa: F401
from apps.tenants.tasks import send_welcome_email_task  # noqa: F401


# ---------------------------------------------------------------------------
# Helpers (mirrors test_user_management.py patterns)
# ---------------------------------------------------------------------------

def _make_user(tenant, email, password="pass1234!", is_active=True, **kwargs):
    """Create a User inside *tenant*'s schema and add them to the tenant."""
    from apps.tenants.models import User

    with schema_context(tenant.schema_name):
        user = User.objects.create_user(
            email=email,
            password=password,
            is_active=is_active,
            **kwargs,
        )
    tenant.add_user(user, is_superuser=False, is_staff=False)
    return user


def _admin_client(api_client, admin_user):
    """Return an APIClient authenticated as *admin_user* via JWT."""
    from rest_framework_simplejwt.tokens import RefreshToken

    refresh = RefreshToken.for_user(admin_user)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    return api_client


# ---------------------------------------------------------------------------
# Helper: run send_welcome_email with the in-memory backend and return the
# first outgoing message.
# ---------------------------------------------------------------------------

def _send_and_capture(user, temp_password, frontend_url="https://app.example.com"):
    """
    Call send_welcome_email() using Django's locmem backend so we can inspect
    the real EmailMultiAlternatives object that was built and sent.
    Returns the first django.core.mail.outbox entry.
    """
    with override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        FRONTEND_URL=frontend_url,
    ):
        mail.outbox = []  # reset outbox
        send_welcome_email(user, temp_password)
        assert mail.outbox, "No email was sent — check send_welcome_email() for exceptions."
        return mail.outbox[0]


def _make_mock_user(email="newuser@example.com", first_name=""):
    user = MagicMock()
    user.email = email
    user.first_name = first_name
    return user


# ---------------------------------------------------------------------------
# Class 1: Unit tests for the email service directly
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestWelcomeEmailService:
    """Unit tests for send_welcome_email() in apps/tenants/services/email_service.py."""

    def test_send_welcome_email_sends_one_email(self):
        """send_welcome_email must dispatch exactly one email to user.email."""
        user = _make_mock_user()
        msg = _send_and_capture(user, "TmpPass123")
        assert msg.to == [user.email], (
            f"Expected to=[{user.email!r}], got {msg.to!r}"
        )

    def test_welcome_email_subject_contains_regulagent(self):
        """The email subject must contain 'RegulAgent'."""
        user = _make_mock_user()
        msg = _send_and_capture(user, "TmpPass123")
        assert "RegulAgent" in msg.subject, (
            f"Expected 'RegulAgent' in subject, got: {msg.subject!r}"
        )

    def test_welcome_email_subject_contains_welcome(self):
        """The email subject must contain 'Welcome'."""
        user = _make_mock_user()
        msg = _send_and_capture(user, "TmpPass123")
        assert "Welcome" in msg.subject, (
            f"Expected 'Welcome' in subject, got: {msg.subject!r}"
        )

    def test_welcome_email_body_contains_temp_password(self):
        """The plain-text body must contain the temp_password value."""
        user = _make_mock_user()
        temp_password = "TmpPass123"
        msg = _send_and_capture(user, temp_password)
        assert temp_password in msg.body, (
            f"Expected temp_password {temp_password!r} in plain-text body."
        )

    def test_welcome_email_html_contains_temp_password(self):
        """The HTML alternative must also contain the temp_password value."""
        user = _make_mock_user()
        temp_password = "TmpPass123"
        msg = _send_and_capture(user, temp_password)
        html_alternatives = [
            content for content, mime in msg.alternatives if mime == "text/html"
        ]
        assert html_alternatives, "No text/html alternative found."
        assert temp_password in html_alternatives[0], (
            f"Expected temp_password {temp_password!r} in HTML body."
        )

    def test_welcome_email_body_contains_login_url(self):
        """The plain-text body must contain the FRONTEND_URL login link."""
        user = _make_mock_user()
        frontend_url = "https://app.example.com"
        msg = _send_and_capture(user, "TmpPass123", frontend_url=frontend_url)
        assert frontend_url in msg.body, (
            f"Expected FRONTEND_URL {frontend_url!r} in plain-text body."
        )

    def test_welcome_email_body_contains_change_password_prompt(self):
        """The plain-text body must prompt the user to change their password."""
        user = _make_mock_user()
        msg = _send_and_capture(user, "TmpPass123")
        assert "change" in msg.body.lower(), (
            f"Expected 'change' (case-insensitive) in plain-text body."
        )

    def test_welcome_email_has_html_alternative(self):
        """The email must carry a text/html alternative (EmailMultiAlternatives)."""
        user = _make_mock_user()
        msg = _send_and_capture(user, "TmpPass123")
        mime_types = [mime for _content, mime in msg.alternatives]
        assert "text/html" in mime_types, (
            f"Expected a text/html alternative, found: {mime_types!r}"
        )

    def test_welcome_email_html_contains_cta_link(self):
        """The HTML body must contain the login URL for the CTA button."""
        user = _make_mock_user()
        frontend_url = "https://app.example.com"
        msg = _send_and_capture(user, "TmpPass123", frontend_url=frontend_url)
        html = next(c for c, m in msg.alternatives if m == "text/html")
        assert f"{frontend_url}/signin" in html, (
            f"Expected CTA href {frontend_url}/signin in HTML body."
        )

    def test_welcome_email_greeting_uses_first_name(self):
        """When user.first_name is set the HTML greeting should include it."""
        user = _make_mock_user(first_name="Ruben")
        msg = _send_and_capture(user, "TmpPass123")
        html = next(c for c, m in msg.alternatives if m == "text/html")
        assert "Ruben" in html, "Expected first_name in HTML greeting."

    def test_welcome_email_greeting_fallback_when_no_name(self):
        """When user.first_name is blank the greeting should fall back to 'Hi there,'."""
        user = _make_mock_user(first_name="")
        msg = _send_and_capture(user, "TmpPass123")
        assert "Hi there," in msg.body, (
            "Expected 'Hi there,' fallback in plain-text body when no first_name."
        )


# ---------------------------------------------------------------------------
# Class 2: Unit tests for the Celery task
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestWelcomeEmailTask:
    """Unit tests for send_welcome_email_task in apps/tenants/tasks.py."""

    def test_task_calls_send_welcome_email(self, public_tenant):
        """Calling the task directly (not .delay()) must invoke send_welcome_email
        with the correct User instance and temp_password."""
        from apps.tenants.models import User

        temp_password = "TmpPass123"

        with schema_context(get_public_schema_name()):
            user = User.objects.create_user(
                email="tasktest@example.com",
                password="irrelevant_pass",
            )

        patch_target = "apps.tenants.tasks.send_welcome_email"
        with patch(patch_target) as mock_send_welcome_email:
            send_welcome_email_task(user.id, temp_password)

        mock_send_welcome_email.assert_called_once()
        call_args = mock_send_welcome_email.call_args
        called_user, called_password = call_args[0]
        assert called_user.id == user.id, (
            f"Expected user.id={user.id}, got {called_user.id}"
        )
        assert called_password == temp_password, (
            f"Expected temp_password={temp_password!r}, got {called_password!r}"
        )


# ---------------------------------------------------------------------------
# Class 3: Integration test — API dispatch
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCreateUserSendsEmail:
    """Integration test: POST /api/tenant/users/ must dispatch send_welcome_email_task."""

    def test_create_user_dispatches_email_task(
        self, api_client, test_tenant, tenant_admin
    ):
        """After creating a user, send_welcome_email_task.delay must be called with
        the new user's id and the temp_password returned in the response."""
        from django.urls import reverse

        client = _admin_client(api_client, tenant_admin)

        payload = {
            "email": "welcometest@example.com",
            "first_name": "Welcome",
            "last_name": "Tester",
        }

        patch_target = "apps.tenants.views.send_welcome_email_task"
        with patch(patch_target) as mock_task:
            mock_task.delay = MagicMock()

            with schema_context(test_tenant.schema_name):
                url = reverse("tenant-users-list")
                response = client.post(url, payload, format="json")

        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.json()}"
        )

        data = response.json()
        new_user_id = data["id"]
        temp_password = data["temp_password"]

        mock_task.delay.assert_called_once_with(new_user_id, temp_password)
