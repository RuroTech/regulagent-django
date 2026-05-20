"""
Failing TDD tests — Playwright trace recording for the W-3A auto-filing pipeline.

These tests define EXPECTED behaviour for the W3A_TRACE_ENABLED / W3A_TRACE_RETENTION
feature. They all FAIL until the production code is implemented.

Requirements pinned:
  R1. tracing.start is called AFTER auth completes (not before, so we don't capture the
      tenant's RRC password in the trace).
  R2. tracing.start args: screenshots=True, snapshots=True, sources=False.
  R3. W3A_TRACE_RETENTION='failure_only' + success  → tracing.stop() with NO path (discard).
  R4. W3A_TRACE_RETENTION='failure_only' + failure  → tracing.stop(path=<trace_path>).
  R5. Trace path uses Django default_storage template
        filing_traces/<tenant_id>/<YYYY-MM-DD>/<job_id>.zip
  R6. W3A_TRACE_ENABLED=False (default) → no tracing calls at all.
  R7. Errors in tracing.stop() must NOT mask the underlying filing error.
"""
from __future__ import annotations

import os
import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from django.test import override_settings


# Ensure the encryption pepper is set so PortalCredential operations work.
os.environ.setdefault("ENCRYPTION_PEPPER", "test-pepper-for-tracing-tests")


# ---------------------------------------------------------------------------
# Fixtures (mirror the task-test fixtures; kept local so this file is
# self-contained and can be run in isolation).
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant(db, public_tenant):
    from apps.tenants.models import Tenant, Domain

    unique = uuid.uuid4().hex[:8]
    t = Tenant.objects.create(
        name=f"Tracing Test Tenant {unique}",
        slug=f"tracing-{unique}",
        schema_name=f"tracing_{unique}",
    )
    Domain.objects.create(
        domain=f"tracing-{unique}.localhost",
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
        api14="42501705750001",
        state="TX",
        county="Andrews",
        district="8A",
        operator_name="Tracing Test Operator",
        field_name="Tracing Field",
        lease_name="Tracing Lease",
        well_number="1",
    )


@pytest.fixture
def snapshot(db, well, tenant):
    from apps.public_core.models import PlanSnapshot

    return PlanSnapshot.objects.create(
        well=well,
        plan_id=f"{well.api14}:tracing",
        kind=PlanSnapshot.KIND_POST_EDIT,
        status=PlanSnapshot.STATUS_ENGINEER_APPROVED,
        tenant_id=tenant.id,
        payload={"steps": [], "inputs_summary": {"api14": well.api14}},
    )


@pytest.fixture
def portal_credential(db, tenant):
    from apps.intelligence.models import PortalCredential

    cred = PortalCredential(
        tenant_id=tenant.id,
        agency="RRC",
        is_active=True,
    )
    cred.set_username("trace-test-user")
    cred.set_password("trace-test-pass")
    cred.save()
    return cred


@pytest.fixture
def filing_job(db, snapshot, tenant):
    from apps.filing_automation.models import FilingJob

    return FilingJob.objects.create(
        plan_snapshot=snapshot,
        tenant_id=tenant.id,
        status="queued",
        celery_task_id="tracing-test-celery-id",
        attestation={
            "submitter_name": "Trace Tester",
            "submitter_title": "P.E.",
            "certification_checked": True,
        },
    )


def _make_success_result():
    """Return a _FilingRunResult-like mock representing a successful filing."""
    r = MagicMock()
    r.success = True
    r.confirmation_number = "RRC-TRACE-12345"
    r.screenshot_path = ""
    r.error = None
    return r


def _make_failure_result():
    """Return a _FilingRunResult-like mock representing a failed filing."""
    r = MagicMock()
    r.success = False
    r.confirmation_number = ""
    r.screenshot_path = ""
    r.error = "RRC portal rejected form"
    return r


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_playwright_mock():
    """
    Return a 3-tuple (mock_async_playwright_ctx_mgr, mock_context, mock_tracing).

    Structure mirrors the real playwright API:
        async with async_playwright() as p:
            browser = await p.chromium.launch(...)
            context = await browser.new_context()
            await context.tracing.start(...)
            ...
            await context.tracing.stop(...)
    """
    mock_tracing = MagicMock()
    mock_tracing.start = AsyncMock()
    mock_tracing.stop = AsyncMock()

    mock_context = MagicMock()
    mock_context.tracing = mock_tracing
    mock_context.new_page = AsyncMock(return_value=MagicMock())

    mock_browser = MagicMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_browser.close = AsyncMock()

    mock_p = MagicMock()
    mock_p.chromium.launch = AsyncMock(return_value=mock_browser)

    mock_playwright_cm = MagicMock()
    mock_playwright_cm.__aenter__ = AsyncMock(return_value=mock_p)
    mock_playwright_cm.__aexit__ = AsyncMock(return_value=False)

    return mock_playwright_cm, mock_context, mock_tracing


def _make_mock_automator():
    """Return a MagicMock automator with awaitable authenticate / execute_*.

    Real production code calls ``authenticate(auth)`` on a real
    ``RRCFormAutomator`` BEFORE tracing.start.  In unit tests we patch
    ``apps.filing_automation.tasks.RRCFormAutomator`` to return this mock so
    the pre-tracing auth call resolves cleanly without needing a real browser.
    """
    auto = MagicMock()
    auto.authenticate = AsyncMock(return_value=True)
    auto.execute_post_auth = AsyncMock(return_value=MagicMock(
        status=MagicMock(name="COMPLETED"),
        agency_confirmation="",
        screenshots=[],
        error_details={},
    ))
    auto.execute_automation = AsyncMock(return_value=MagicMock(
        status=MagicMock(name="COMPLETED"),
        agency_confirmation="",
        screenshots=[],
        error_details={},
    ))
    return auto


# ===========================================================================
# R6 — W3A_TRACE_ENABLED=False (the default) — no tracing calls at all
# ===========================================================================


@pytest.mark.django_db
class TestTracingDisabledByDefault:
    """R6: When W3A_TRACE_ENABLED is False (or absent), _run_filing must not
    touch context.tracing at all."""

    @override_settings(W3A_TRACE_ENABLED=False)
    def test_tracing_not_called_when_disabled(
        self,
        mocker,
        snapshot,
        tenant,
        portal_credential,
        filing_job,
    ):
        """No tracing.start / stop calls when W3A_TRACE_ENABLED=False."""
        mock_playwright_cm, mock_context, mock_tracing = _make_playwright_mock()

        mock_automator = MagicMock()
        mock_result = MagicMock()
        mock_result.status.name = "COMPLETED"
        from unittest.mock import AsyncMock as _AM
        mock_automator.execute_automation = _AM(return_value=mock_result)

        with (
            patch(
                "apps.filing_automation.tasks.async_playwright",
                return_value=mock_playwright_cm,
            ),
            patch(
                "apps.filing_automation.tasks.RRCFormAutomator",
                return_value=mock_automator,
            ),
        ):
            from apps.filing_automation import tasks

            mocker.patch.object(
                tasks,
                "_run_filing",
                return_value=_make_success_result(),
            )

            tasks.submit_w3a_to_rrc(
                snapshot_id=snapshot.id,
                tenant_id=str(tenant.id),
                job_id=filing_job.id,
            )

        mock_tracing.start.assert_not_called()
        mock_tracing.stop.assert_not_called()

    @override_settings()  # no W3A_TRACE_ENABLED key at all
    def test_tracing_not_called_when_setting_absent(
        self,
        mocker,
        snapshot,
        tenant,
        portal_credential,
        filing_job,
    ):
        """W3A_TRACE_ENABLED missing entirely should behave the same as False."""
        from django.conf import settings as _s
        if hasattr(_s, "W3A_TRACE_ENABLED"):
            del _s.W3A_TRACE_ENABLED  # type: ignore[attr-defined]

        mock_playwright_cm, mock_context, mock_tracing = _make_playwright_mock()

        with (
            patch(
                "apps.filing_automation.tasks.async_playwright",
                return_value=mock_playwright_cm,
            ),
            patch(
                "apps.filing_automation.tasks.RRCFormAutomator",
                return_value=_make_mock_automator(),
            ),
        ):
            from apps.filing_automation import tasks

            mocker.patch.object(
                tasks,
                "_run_filing",
                return_value=_make_success_result(),
            )

            tasks.submit_w3a_to_rrc(
                snapshot_id=snapshot.id,
                tenant_id=str(tenant.id),
                job_id=filing_job.id,
            )

        mock_tracing.start.assert_not_called()
        mock_tracing.stop.assert_not_called()


# ===========================================================================
# R2 — tracing.start must be called with the correct arguments
# ===========================================================================


@pytest.mark.django_db
class TestTracingStartArguments:
    """R2: tracing.start(screenshots=True, snapshots=True, sources=False)."""

    @override_settings(
        W3A_TRACE_ENABLED=True,
        W3A_TRACE_RETENTION="failure_only",
    )
    def test_tracing_start_called_with_correct_kwargs(
        self,
        mocker,
        snapshot,
        tenant,
        portal_credential,
        filing_job,
    ):
        mock_playwright_cm, mock_context, mock_tracing = _make_playwright_mock()

        with (
            patch(
                "apps.filing_automation.tasks.async_playwright",
                return_value=mock_playwright_cm,
            ),
            patch(
                "apps.filing_automation.tasks.RRCFormAutomator",
                return_value=_make_mock_automator(),
            ),
        ):
            from apps.filing_automation import tasks

            mocker.patch.object(
                tasks,
                "_run_filing",
                return_value=_make_success_result(),
            )

            tasks.submit_w3a_to_rrc(
                snapshot_id=snapshot.id,
                tenant_id=str(tenant.id),
                job_id=filing_job.id,
            )

        mock_tracing.start.assert_awaited_once()
        _, start_kwargs = mock_tracing.start.call_args
        assert start_kwargs.get("screenshots") is True, (
            "tracing.start must pass screenshots=True"
        )
        assert start_kwargs.get("snapshots") is True, (
            "tracing.start must pass snapshots=True"
        )
        assert start_kwargs.get("sources") is False, (
            "tracing.start must pass sources=False to avoid capturing source code"
        )


# ===========================================================================
# R1 — tracing.start must be called AFTER authenticate completes
# ===========================================================================


@pytest.mark.django_db
class TestTracingStartAfterAuth:
    """R1: tracing.start must be called AFTER authenticate() so the tenant's
    RRC password is never included in the trace."""

    @override_settings(
        W3A_TRACE_ENABLED=True,
        W3A_TRACE_RETENTION="failure_only",
    )
    def test_tracing_start_called_after_authenticate(
        self,
        mocker,
        snapshot,
        tenant,
        portal_credential,
        filing_job,
    ):
        """Verifies call ORDER: authenticate → tracing.start (not before).

        The test instruments both `automator.authenticate` and
        `context.tracing.start`, then checks the call log to confirm
        `authenticate` was invoked first.
        """
        call_order: list[str] = []

        mock_tracing = MagicMock()

        async def _tracing_start(*a, **kw):
            call_order.append("tracing.start")

        async def _tracing_stop(*a, **kw):
            call_order.append("tracing.stop")

        mock_tracing.start = _tracing_start
        mock_tracing.stop = _tracing_stop

        mock_context = MagicMock()
        mock_context.tracing = mock_tracing
        mock_context.new_page = AsyncMock(return_value=MagicMock())

        mock_automator = MagicMock()

        async def _fake_authenticate(auth):
            call_order.append("authenticate")

        async def _fake_execute(form_data, auth):
            # If tracing.start happened before authenticate, fail fast.
            mock_result = MagicMock()
            mock_result.status.name = "COMPLETED"
            mock_result.agency_confirmation = "RRC-TRACE-OK"
            mock_result.screenshots = []
            mock_result.error_details = {}
            return mock_result

        async def _fake_post_auth(form_data):
            # Production calls execute_post_auth (not execute_automation) on
            # the pre-authed context so the tracing run does NOT re-record
            # the password.  Mock both for symmetry — only post-auth is hit
            # by the _pre_auth_context branch in _run_filing.
            mock_result = MagicMock()
            mock_result.status.name = "COMPLETED"
            mock_result.agency_confirmation = "RRC-TRACE-OK"
            mock_result.screenshots = []
            mock_result.error_details = {}
            return mock_result

        mock_automator.authenticate = _fake_authenticate
        mock_automator.execute_automation = AsyncMock(side_effect=_fake_execute)
        mock_automator.execute_post_auth = AsyncMock(side_effect=_fake_post_auth)

        mock_browser = MagicMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_browser.close = AsyncMock()

        mock_p = MagicMock()
        mock_p.chromium.launch = AsyncMock(return_value=mock_browser)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_p)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "apps.filing_automation.tasks.async_playwright",
                return_value=mock_cm,
            ),
            patch(
                "apps.filing_automation.tasks.RRCFormAutomator",
                return_value=mock_automator,
            ),
        ):
            from apps.filing_automation import tasks

            tasks.submit_w3a_to_rrc(
                snapshot_id=snapshot.id,
                tenant_id=str(tenant.id),
                job_id=filing_job.id,
            )

        assert "tracing.start" in call_order, "tracing.start was never called"
        assert "authenticate" in call_order, "authenticate was never called"
        auth_idx = call_order.index("authenticate")
        trace_idx = call_order.index("tracing.start")
        assert auth_idx < trace_idx, (
            f"tracing.start (index {trace_idx}) must come AFTER authenticate "
            f"(index {auth_idx}) to avoid capturing the RRC password. "
            f"Actual call order: {call_order}"
        )


# ===========================================================================
# R3 — failure_only + success → stop with NO path
# ===========================================================================


@pytest.mark.django_db
class TestTracingRetentionFailureOnlySuccess:
    """R3: W3A_TRACE_RETENTION='failure_only', filing succeeds → discard trace
    by calling tracing.stop() WITHOUT a path argument."""

    @override_settings(
        W3A_TRACE_ENABLED=True,
        W3A_TRACE_RETENTION="failure_only",
    )
    def test_stop_called_without_path_on_success(
        self,
        mocker,
        snapshot,
        tenant,
        portal_credential,
        filing_job,
    ):
        mock_playwright_cm, mock_context, mock_tracing = _make_playwright_mock()

        with (
            patch(
                "apps.filing_automation.tasks.async_playwright",
                return_value=mock_playwright_cm,
            ),
            patch(
                "apps.filing_automation.tasks.RRCFormAutomator",
                return_value=_make_mock_automator(),
            ),
        ):
            from apps.filing_automation import tasks

            mocker.patch.object(
                tasks,
                "_run_filing",
                return_value=_make_success_result(),
            )

            tasks.submit_w3a_to_rrc(
                snapshot_id=snapshot.id,
                tenant_id=str(tenant.id),
                job_id=filing_job.id,
            )

        mock_tracing.stop.assert_awaited_once()
        _, stop_kwargs = mock_tracing.stop.call_args
        assert "path" not in stop_kwargs, (
            "On success with failure_only retention, tracing.stop() must be called "
            "WITHOUT a path (discard the trace). Got kwargs: %s" % stop_kwargs
        )


# ===========================================================================
# R4 — failure_only + failure → stop WITH path
# ===========================================================================


@pytest.mark.django_db
class TestTracingRetentionFailureOnlyFailure:
    """R4: W3A_TRACE_RETENTION='failure_only', filing fails → save trace by
    calling tracing.stop(path=<trace_path>)."""

    @override_settings(
        W3A_TRACE_ENABLED=True,
        W3A_TRACE_RETENTION="failure_only",
    )
    def test_stop_called_with_path_on_failure(
        self,
        mocker,
        snapshot,
        tenant,
        portal_credential,
        filing_job,
    ):
        mock_playwright_cm, mock_context, mock_tracing = _make_playwright_mock()

        with (
            patch(
                "apps.filing_automation.tasks.async_playwright",
                return_value=mock_playwright_cm,
            ),
            patch(
                "apps.filing_automation.tasks.RRCFormAutomator",
                return_value=_make_mock_automator(),
            ),
        ):
            from apps.filing_automation import tasks

            # Simulate a filing-level failure (result.success=False)
            mocker.patch.object(
                tasks,
                "_run_filing",
                return_value=_make_failure_result(),
            )

            try:
                tasks.submit_w3a_to_rrc(
                    snapshot_id=snapshot.id,
                    tenant_id=str(tenant.id),
                    job_id=filing_job.id,
                )
            except Exception:
                pass  # We only care about tracing.stop behaviour.

        mock_tracing.stop.assert_awaited_once()
        _, stop_kwargs = mock_tracing.stop.call_args
        assert "path" in stop_kwargs and stop_kwargs["path"], (
            "On failure with failure_only retention, tracing.stop() must be called "
            "WITH a non-empty path argument. Got kwargs: %s" % stop_kwargs
        )

    @override_settings(
        W3A_TRACE_ENABLED=True,
        W3A_TRACE_RETENTION="failure_only",
    )
    def test_stop_called_with_path_on_exception(
        self,
        mocker,
        snapshot,
        tenant,
        portal_credential,
        filing_job,
    ):
        """An exception raised inside _run_filing also counts as failure for trace retention."""
        mock_playwright_cm, mock_context, mock_tracing = _make_playwright_mock()

        with (
            patch(
                "apps.filing_automation.tasks.async_playwright",
                return_value=mock_playwright_cm,
            ),
            patch(
                "apps.filing_automation.tasks.RRCFormAutomator",
                return_value=_make_mock_automator(),
            ),
        ):
            from apps.filing_automation import tasks

            class FormSubmissionError(Exception):
                pass

            mocker.patch.object(
                tasks,
                "_run_filing",
                side_effect=FormSubmissionError("selector timed out"),
            )

            try:
                tasks.submit_w3a_to_rrc(
                    snapshot_id=snapshot.id,
                    tenant_id=str(tenant.id),
                    job_id=filing_job.id,
                )
            except Exception:
                pass

        mock_tracing.stop.assert_awaited_once()
        _, stop_kwargs = mock_tracing.stop.call_args
        assert "path" in stop_kwargs and stop_kwargs["path"], (
            "On exception with failure_only retention, tracing.stop() must save the "
            "trace. Got kwargs: %s" % stop_kwargs
        )


# ===========================================================================
# R4 / R5 — 'all' retention → always save trace with correct path template
# ===========================================================================


@pytest.mark.django_db
class TestTracingRetentionAll:
    """R4 (all-mode): W3A_TRACE_RETENTION='all' → always stop with path.
    R5: Path follows template filing_traces/<tenant_id>/<YYYY-MM-DD>/<job_id>.zip
    """

    @override_settings(
        W3A_TRACE_ENABLED=True,
        W3A_TRACE_RETENTION="all",
    )
    def test_stop_always_called_with_path_on_success(
        self,
        mocker,
        snapshot,
        tenant,
        portal_credential,
        filing_job,
    ):
        mock_playwright_cm, mock_context, mock_tracing = _make_playwright_mock()

        with (
            patch(
                "apps.filing_automation.tasks.async_playwright",
                return_value=mock_playwright_cm,
            ),
            patch(
                "apps.filing_automation.tasks.RRCFormAutomator",
                return_value=_make_mock_automator(),
            ),
        ):
            from apps.filing_automation import tasks

            mocker.patch.object(
                tasks,
                "_run_filing",
                return_value=_make_success_result(),
            )

            tasks.submit_w3a_to_rrc(
                snapshot_id=snapshot.id,
                tenant_id=str(tenant.id),
                job_id=filing_job.id,
            )

        mock_tracing.stop.assert_awaited_once()
        _, stop_kwargs = mock_tracing.stop.call_args
        assert "path" in stop_kwargs and stop_kwargs["path"], (
            "With retention='all', tracing.stop() must always be called WITH a path. "
            "Got kwargs: %s" % stop_kwargs
        )

    @override_settings(
        W3A_TRACE_ENABLED=True,
        W3A_TRACE_RETENTION="all",
    )
    def test_trace_path_matches_template(
        self,
        mocker,
        snapshot,
        tenant,
        portal_credential,
        filing_job,
    ):
        """R5 (corrected contract): tracing.stop is called with SOME local path
        (a temp file ending in .zip), and ``default_storage.save`` is called
        with the storage-key template
        ``filing_traces/<tenant_id>/<YYYY-MM-DD>/<job_id>.zip``.

        The original assertion (``stop_kwargs['path'].startswith('filing_traces/')``)
        was over-specified — Playwright cannot write to a non-existent relative
        path; production must stage the file in a tmp dir first, then upload
        via default_storage.  This test now verifies the real two-step contract.
        """
        mock_playwright_cm, mock_context, mock_tracing = _make_playwright_mock()

        # The mocked tracing.stop must create the temp file so the subsequent
        # open(path,'rb') in _save_trace succeeds and default_storage.save runs.
        async def _stop_creates_file(path=None):
            if path:
                with open(path, "wb") as fh:
                    fh.write(b"")
        mock_tracing.stop = AsyncMock(side_effect=_stop_creates_file)

        with (
            patch(
                "apps.filing_automation.tasks.async_playwright",
                return_value=mock_playwright_cm,
            ),
            patch(
                "apps.filing_automation.tasks.RRCFormAutomator",
                return_value=_make_mock_automator(),
            ),
            patch(
                "django.core.files.storage.default_storage.save"
            ) as mock_storage_save,
        ):
            from apps.filing_automation import tasks

            mocker.patch.object(
                tasks,
                "_run_filing",
                return_value=_make_success_result(),
            )

            tasks.submit_w3a_to_rrc(
                snapshot_id=snapshot.id,
                tenant_id=str(tenant.id),
                job_id=filing_job.id,
            )

        # (a) tracing.stop was called with SOME local path (temp file).
        _, stop_kwargs = mock_tracing.stop.call_args
        stop_path = stop_kwargs.get("path", "")
        assert stop_path, (
            f"tracing.stop() must be called with a non-empty 'path' kwarg "
            f"(temp file). Got kwargs: {stop_kwargs}"
        )
        assert stop_path.endswith(".zip"), (
            f"tracing.stop() path must be a .zip temp file. Got: {stop_path!r}"
        )

        # (b) default_storage.save was called with the storage-key template.
        assert mock_storage_save.called, (
            "default_storage.save must be invoked to persist the trace."
        )
        saved_key = mock_storage_save.call_args[0][0]
        # Re-fetch the job to get the stored tenant_id in its canonical form
        # (the field may coerce to UUID on save even if assigned as int).
        from apps.filing_automation.models import FilingJob as _FJ
        _job = _FJ.objects.get(pk=filing_job.pk)
        today = date.today().strftime("%Y-%m-%d")
        tenant_id_str = str(_job.tenant_id)
        job_id_str = str(_job.id)

        assert saved_key.startswith(f"filing_traces/{tenant_id_str}/"), (
            f"Storage key must start with 'filing_traces/<tenant_id>/'. "
            f"Got: {saved_key!r}"
        )
        assert today in saved_key, (
            f"Storage key must contain today's date ({today}). Got: {saved_key!r}"
        )
        assert job_id_str in saved_key, (
            f"Storage key must contain the job_id ({job_id_str}). Got: {saved_key!r}"
        )
        assert saved_key.endswith(".zip"), (
            f"Storage key must end with .zip. Got: {saved_key!r}"
        )

    @override_settings(
        W3A_TRACE_ENABLED=True,
        W3A_TRACE_RETENTION="all",
    )
    def test_trace_path_uses_default_storage(
        self,
        mocker,
        snapshot,
        tenant,
        portal_credential,
        filing_job,
    ):
        """R5: trace is saved via Django default_storage, not a raw filesystem write."""
        mock_playwright_cm, mock_context, mock_tracing = _make_playwright_mock()

        # Production now writes to a temp file first; the test's mocked
        # tracing.stop must create that file so the subsequent open(path,'rb')
        # call in _save_trace returns bytes (otherwise FileNotFoundError
        # propagates out of _save_trace and short-circuits the upload).
        async def _stop_creates_file(path=None):
            if path:
                with open(path, "wb") as fh:
                    fh.write(b"")  # zero-byte trace is fine for this assertion
        mock_tracing.stop = AsyncMock(side_effect=_stop_creates_file)

        with (
            patch(
                "apps.filing_automation.tasks.async_playwright",
                return_value=mock_playwright_cm,
            ),
            patch(
                "apps.filing_automation.tasks.RRCFormAutomator",
                return_value=_make_mock_automator(),
            ),
            patch(
                "django.core.files.storage.default_storage.save"
            ) as mock_storage_save,
        ):
            from apps.filing_automation import tasks

            mocker.patch.object(
                tasks,
                "_run_filing",
                return_value=_make_success_result(),
            )

            tasks.submit_w3a_to_rrc(
                snapshot_id=snapshot.id,
                tenant_id=str(tenant.id),
                job_id=filing_job.id,
            )

        # default_storage.save must have been called with a path matching the template.
        assert mock_storage_save.called, (
            "Trace file must be saved via django.core.files.storage.default_storage.save()"
        )
        saved_path = mock_storage_save.call_args[0][0]
        assert saved_path.startswith("filing_traces/"), (
            f"default_storage.save path must start with 'filing_traces/'. Got: {saved_path!r}"
        )


# ===========================================================================
# R7 — errors in tracing.stop must NOT mask the underlying filing error
# ===========================================================================


@pytest.mark.django_db
class TestTracingStopErrorIsolation:
    """R7: If tracing.stop() raises, the original filing exception must still
    propagate (or the job must still be marked failed with the correct error),
    not a tracing error."""

    @override_settings(
        W3A_TRACE_ENABLED=True,
        W3A_TRACE_RETENTION="failure_only",
    )
    def test_tracing_stop_error_does_not_mask_filing_error(
        self,
        mocker,
        snapshot,
        tenant,
        portal_credential,
        filing_job,
    ):
        mock_playwright_cm, mock_context, mock_tracing = _make_playwright_mock()
        # Make tracing.stop itself raise.
        mock_tracing.stop = AsyncMock(side_effect=OSError("disk full"))

        with (
            patch(
                "apps.filing_automation.tasks.async_playwright",
                return_value=mock_playwright_cm,
            ),
            patch(
                "apps.filing_automation.tasks.RRCFormAutomator",
                return_value=_make_mock_automator(),
            ),
        ):
            from apps.filing_automation import tasks
            from apps.filing_automation.models import FilingJob

            class FormSubmissionError(Exception):
                pass

            mocker.patch.object(
                tasks,
                "_run_filing",
                side_effect=FormSubmissionError("RRC rejected"),
            )

            try:
                tasks.submit_w3a_to_rrc(
                    snapshot_id=snapshot.id,
                    tenant_id=str(tenant.id),
                    job_id=filing_job.id,
                )
            except Exception as exc:
                # If an exception propagates it should be the filing error, NOT the OSError.
                assert not isinstance(exc, OSError), (
                    "The OSError from tracing.stop() must not propagate; "
                    "only the filing error matters."
                )

        # Job must reflect the FILING failure, not a tracing error.
        job = FilingJob.objects.get(pk=filing_job.pk)
        assert job.status == "failed", (
            "Job must be marked failed due to filing error even when tracing.stop() raises."
        )
        assert "tracing" not in (job.error_class or "").lower() and \
               "OSError" not in (job.error_class or ""), (
            f"job.error_class should reflect the filing error, not the tracing error. "
            f"Got: {job.error_class!r}"
        )

    @override_settings(
        W3A_TRACE_ENABLED=True,
        W3A_TRACE_RETENTION="all",
    )
    def test_tracing_stop_error_on_success_does_not_reraise(
        self,
        mocker,
        snapshot,
        tenant,
        portal_credential,
        filing_job,
    ):
        """If tracing.stop() raises on an otherwise-successful filing, the task
        must still mark the job succeeded (tracing error is non-fatal)."""
        mock_playwright_cm, mock_context, mock_tracing = _make_playwright_mock()
        mock_tracing.stop = AsyncMock(side_effect=OSError("storage unavailable"))

        with (
            patch(
                "apps.filing_automation.tasks.async_playwright",
                return_value=mock_playwright_cm,
            ),
            patch(
                "apps.filing_automation.tasks.RRCFormAutomator",
                return_value=_make_mock_automator(),
            ),
        ):
            from apps.filing_automation import tasks
            from apps.filing_automation.models import FilingJob

            mocker.patch.object(
                tasks,
                "_run_filing",
                return_value=_make_success_result(),
            )

            # Must NOT raise even though tracing.stop fails.
            tasks.submit_w3a_to_rrc(
                snapshot_id=snapshot.id,
                tenant_id=str(tenant.id),
                job_id=filing_job.id,
            )

        job = FilingJob.objects.get(pk=filing_job.pk)
        assert job.status == "succeeded", (
            "Job must be succeeded even when tracing.stop() raises on a successful filing. "
            f"Got status: {job.status!r}"
        )
