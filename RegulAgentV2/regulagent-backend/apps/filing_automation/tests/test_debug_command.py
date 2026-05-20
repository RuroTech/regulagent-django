"""
Smoke tests for the debug_w3a_filing management command
and unit tests for RRCFormAutomator._step.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.test import override_settings


# Ensure encryption pepper is available for PortalCredential operations.
os.environ.setdefault("ENCRYPTION_PEPPER", "test-pepper-for-debug-cmd-tests")


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def tenant(db, public_tenant):
    from apps.tenants.models import Tenant, Domain

    unique = uuid.uuid4().hex[:8]
    t = Tenant.objects.create(
        name=f"Debug Cmd Tenant {unique}",
        slug=f"debug-cmd-{unique}",
        schema_name=f"debug_cmd_{unique}",
    )
    Domain.objects.create(
        domain=f"debug-cmd-{unique}.localhost",
        tenant=t,
        is_primary=True,
    )
    yield t
    try:
        t.delete(force_drop=True)
    except Exception:
        pass


@pytest.fixture
def well(db):
    from apps.public_core.models import WellRegistry

    return WellRegistry.objects.create(
        api14="42501705750030",
        state="TX",
        county="Andrews",
        district="8A",
        operator_name="Debug Cmd Operator",
        field_name="Debug Field",
        lease_name="Debug Lease",
        well_number="1",
    )


@pytest.fixture
def snapshot(db, well, tenant):
    from apps.public_core.models import PlanSnapshot

    return PlanSnapshot.objects.create(
        well=well,
        plan_id=f"{well.api14}:debug",
        kind=PlanSnapshot.KIND_POST_EDIT,
        status=PlanSnapshot.STATUS_ENGINEER_APPROVED,
        tenant_id=tenant.id,
        payload={"steps": [], "inputs_summary": {"api14": well.api14}},
    )


@pytest.fixture
def filing_job(db, snapshot, tenant):
    from apps.filing_automation.models import FilingJob

    return FilingJob.objects.create(
        plan_snapshot=snapshot,
        tenant_id=tenant.id,
        status="queued",
        celery_task_id="debug-cmd-test-celery-id",
        attestation={
            "submitter_name": "Debug Tester",
            "submitter_title": "P.E.",
            "certification_checked": True,
        },
    )


@pytest.fixture
def completed_job(db, snapshot, tenant):
    from apps.filing_automation.models import FilingJob
    from django.utils import timezone

    return FilingJob.objects.create(
        plan_snapshot=snapshot,
        tenant_id=tenant.id,
        status="succeeded",
        celery_task_id="debug-cmd-completed-celery-id",
        confirmation_number="RRC-ALREADY-DONE",
        finished_at=timezone.now(),
        attestation={},
    )


# ===========================================================================
# Management command smoke tests
# ===========================================================================


class TestDebugCommandDockerRefusal:
    """The command must refuse to run inside a Docker container."""

    def test_refuses_when_dockerenv_exists(self, tmp_path, monkeypatch):
        """If /.dockerenv is present the command raises CommandError."""
        # Patch os.path.exists to simulate /.dockerenv being present.
        dockerenv_path = "/.dockerenv"

        original_exists = os.path.exists

        def patched_exists(p):
            if str(p) == dockerenv_path:
                return True
            return original_exists(p)

        monkeypatch.setattr(os.path, "exists", patched_exists)

        from django.core.management import call_command
        from django.core.management.base import CommandError

        with pytest.raises(CommandError, match="Docker"):
            call_command("debug_w3a_filing", "999")


@pytest.mark.django_db
class TestDebugCommandJobValidation:
    """Job existence and terminal-status checks."""

    def test_raises_for_missing_job(self, monkeypatch):
        """A non-existent job_id raises CommandError."""
        # Ensure we're not in Docker (/.dockerenv check passes).
        monkeypatch.setattr(os.path, "exists", lambda p: False)

        from django.core.management import call_command
        from django.core.management.base import CommandError

        # Use a valid UUID format that doesn't correspond to any real job.
        nonexistent_uuid = "00000000-0000-0000-0000-000000000001"
        with pytest.raises(CommandError, match="does not exist"):
            call_command("debug_w3a_filing", nonexistent_uuid)

    def test_raises_for_completed_job(self, monkeypatch, completed_job):
        """A job in terminal status raises CommandError with a helpful message."""
        monkeypatch.setattr(os.path, "exists", lambda p: False)

        from django.core.management import call_command
        from django.core.management.base import CommandError

        with pytest.raises(CommandError, match="terminal status"):
            call_command("debug_w3a_filing", str(completed_job.id))


# ===========================================================================
# _step unit tests
# ===========================================================================


class TestRRCFormAutomatorStep:
    """Unit tests for RRCFormAutomator._step — the step-boundary hook."""

    def _make_automator(self):
        """Return an RRCFormAutomator with a mocked BrowserContext."""
        from apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc.rrc_form_automator import (
            RRCFormAutomator,
        )

        mock_context = MagicMock()
        mock_page = MagicMock()
        mock_page.pause = AsyncMock()
        mock_context.pages = [mock_page]

        return RRCFormAutomator(context=mock_context, session_id="step-test")

    def test_step_success_emits_start_and_ok(self, caplog):
        """On success, _step should log step_start then step_ok."""
        import logging

        automator = self._make_automator()

        async def _ok_fn():
            return "done"

        with caplog.at_level(logging.INFO, logger="apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc.rrc_form_automator"):
            result = asyncio.run(automator._step("basic_fields", "fill", _ok_fn))

        assert result == "done"

        log_messages = [r.getMessage() for r in caplog.records]
        # Verify start event logged
        assert any("step_start" in str(m) and "basic_fields" in str(m) for m in log_messages), (
            f"Expected step_start/basic_fields in logs. Got: {log_messages}"
        )
        # Verify ok event logged
        assert any("step_ok" in str(m) and "basic_fields" in str(m) for m in log_messages), (
            f"Expected step_ok/basic_fields in logs. Got: {log_messages}"
        )

    def test_step_exception_emits_error_event(self, caplog):
        """On exception, _step should log step_error and re-raise."""
        import logging

        automator = self._make_automator()

        class MyError(Exception):
            pass

        async def _fail_fn():
            raise MyError("selector timed out")

        with caplog.at_level(logging.INFO, logger="apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc.rrc_form_automator"):
            with pytest.raises(MyError):
                asyncio.run(automator._step("location_fields", "fill", _fail_fn))

        log_messages = [r.getMessage() for r in caplog.records]
        assert any("step_error" in str(m) and "location_fields" in str(m) for m in log_messages), (
            f"Expected step_error/location_fields in logs. Got: {log_messages}"
        )

    def test_step_pause_at_triggers_pause(self, monkeypatch):
        """When W3A_PAUSE_AT matches the section, page.pause() is awaited."""
        monkeypatch.setenv("W3A_PAUSE_AT", "file_attachments")

        automator = self._make_automator()
        mock_page = automator.context.pages[0]

        async def _ok_fn():
            return "uploaded"

        asyncio.run(automator._step("file_attachments", "upload", _ok_fn))

        mock_page.pause.assert_awaited_once()

    def test_step_pause_at_does_not_fire_for_other_sections(self, monkeypatch):
        """W3A_PAUSE_AT only pauses for the matching section."""
        monkeypatch.setenv("W3A_PAUSE_AT", "agreement")

        automator = self._make_automator()
        mock_page = automator.context.pages[0]

        async def _ok_fn():
            return "filled"

        asyncio.run(automator._step("basic_fields", "fill", _ok_fn))

        mock_page.pause.assert_not_called()
