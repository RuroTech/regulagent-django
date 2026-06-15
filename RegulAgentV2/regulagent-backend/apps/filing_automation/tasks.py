from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import traceback
from typing import Any

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.tenants.context import set_current_tenant


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy module-level imports for Playwright + vendored automator.
#
# These are resolved once at first task-run (not at import time) so the web
# container doesn't pay the Playwright startup cost.  They live at module
# scope so unit tests can patch them via
#   patch("apps.filing_automation.tasks.async_playwright", ...)
#   patch("apps.filing_automation.tasks.RRCFormAutomator", ...)
# ---------------------------------------------------------------------------

async_playwright = None  # type: ignore[assignment]
RRCFormAutomator = None  # type: ignore[assignment]
AutomationStatus = None  # type: ignore[assignment]


def _ensure_playwright_imports() -> None:
    """Resolve lazy Playwright + automator imports into module-level names."""
    global async_playwright, RRCFormAutomator, AutomationStatus
    if async_playwright is None:
        from playwright.async_api import async_playwright as _ap
        async_playwright = _ap
    if RRCFormAutomator is None:
        from apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc.rrc_form_automator import (
            RRCFormAutomator as _RFA,
        )
        RRCFormAutomator = _RFA
    if AutomationStatus is None:
        from apps.filing_automation._vendor.regulagent_core.automation.base.data_models import (
            AutomationStatus as _AS,
        )
        AutomationStatus = _AS


class BrowserCrashed(Exception):
    """Raised when the Playwright browser context dies mid-filing."""


def _normalize_tenant_id(value):
    """Return a UUID for ``value`` whether it arrives as int / str digits / UUID.

    The shared ``Tenant`` table from django-tenants uses an auto-int PK in this
    deployment, whereas tenant-scoped models (PortalCredential, PlanSnapshot,
    etc.) declare ``tenant_id`` as a UUIDField. ``uuid.UUID(int=n)`` matches
    the conversion Django performs internally when storing ints into UUID
    columns, so the round-trip is loss-less.
    """
    import uuid as _uuid

    if isinstance(value, _uuid.UUID):
        return value
    if isinstance(value, int):
        return _uuid.UUID(int=value)
    if isinstance(value, str):
        if value.isdigit():
            return _uuid.UUID(int=int(value))
        return _uuid.UUID(value)
    raise ValueError(f"Unsupported tenant id type: {type(value).__name__}")


# Vendored exceptions live behind a lazy import so the web container does not
# pay the playwright cost. We import them at module-level via try/except
# because they DO NOT touch playwright themselves (just plain Exception subclasses).
try:
    from apps.filing_automation._vendor.regulagent_core.automation.exceptions import (
        AuthenticationError,
        FormSubmissionError,
    )
except Exception:  # pragma: no cover — defensive
    class AuthenticationError(Exception):
        pass

    class FormSubmissionError(Exception):
        pass


# Lazy import marker — populated on first task run.
_PWTimeoutError: type[BaseException] | None = None


def _playwright_timeout_error() -> type[BaseException]:
    """Return playwright.async_api.TimeoutError, importing on first use."""
    global _PWTimeoutError
    if _PWTimeoutError is None:
        try:
            from playwright.async_api import TimeoutError as PWTimeoutError
            _PWTimeoutError = PWTimeoutError
        except Exception:
            class _FallbackPWTimeoutError(Exception):
                pass
            _PWTimeoutError = _FallbackPWTimeoutError
    return _PWTimeoutError


class _FilingRunResult:
    """Lightweight shim translating ``AutomationResult`` to the attributes
    ``submit_w3a_to_rrc`` expects (``success``, ``confirmation_number``,
    ``screenshot_path``, ``error``)."""

    __slots__ = ("success", "confirmation_number", "screenshot_path", "error")

    def __init__(self, success, confirmation_number="", screenshot_path="", error=None):
        self.success = success
        self.confirmation_number = confirmation_number or ""
        self.screenshot_path = screenshot_path or ""
        self.error = error


async def _run_filing(auth, form_data, well_record, job_id, *, headless=True, slow_mo=0,
                     _pre_auth_context=None):
    """Drive the vendored RRCFormAutomator to file a W-3A.

    Uses module-level ``async_playwright`` / ``RRCFormAutomator`` names so
    unit tests can patch them directly on this module.  Returns a
    ``_FilingRunResult``.

    ``form_data.test_mode`` is the single source of truth for the
    submit-vs-draft decision — callers set it based on the layered gates
    (``settings.RRC_LIVE_SUBMIT_ENABLED`` and ``PortalCredential.is_test``).

    Optional kwargs:
        headless           — passed to ``p.chromium.launch``; default True (production).
        slow_mo            — passed to ``p.chromium.launch``; default 0 (no slow-down).
        _pre_auth_context  — When the caller (tracing wrapper) has already created a
                             BrowserContext and completed ``authenticate()``, pass it
                             here.  ``_run_filing`` will use it directly and call
                             ``execute_post_auth`` instead of ``execute_automation``
                             (skipping a second auth round-trip).  If None, a fresh
                             browser session is opened as usual.
    """
    _ensure_playwright_imports()

    # Normalize form type before any browser work.
    ft = (getattr(form_data, "form_type", "") or "").upper().replace("-", "")
    if ft:
        form_data.form_type = ft

    if _pre_auth_context is not None:
        # Fast path: caller (tracing wrapper) already authenticated; re-use the
        # existing BrowserContext rather than opening a second browser session.
        # We call execute_post_auth — NOT execute_automation — because tracing
        # has already started by the time this runs; re-authenticating here
        # would record the password in page.fill(...) inside the trace, which
        # defeats the entire auth-before-tracing split (R1).
        context = _pre_auth_context
        automator = RRCFormAutomator(context=context, session_id=str(job_id))
        result = await automator.execute_post_auth(form_data)
        success = result.status == AutomationStatus.COMPLETED
        confirmation = result.agency_confirmation or ""
        screenshot = result.screenshots[0] if result.screenshots else ""
        error_detail = None
        if not success and result.error_details:
            error_detail = result.error_details.get("error")
        return _FilingRunResult(
            success=success,
            confirmation_number=confirmation,
            screenshot_path=screenshot,
            error=error_detail,
        )

    async with async_playwright() as p:
        launch_kwargs: dict = {"headless": headless}
        if slow_mo:
            launch_kwargs["slow_mo"] = slow_mo
        browser = await p.chromium.launch(**launch_kwargs)
        try:
            context = await browser.new_context()
            # The vendored automator's single-tab code path does
            # ``self.context.pages[0]`` immediately, so a fresh
            # ``BrowserContext`` (which starts with zero pages) trips
            # ``IndexError`` before authenticate even reaches goto. Pre-create
            # an initial page so ``pages[0]`` resolves cleanly.
            await context.new_page()
            # The vendored constructor requires (context, session_id). We pass
            # the FilingJob id as a stable session identifier so screenshots /
            # logs the automator emits are correlated to this job.
            automator = RRCFormAutomator(context=context, session_id=str(job_id))
            # ``execute_automation`` orchestrates authenticate → navigate →
            # fill → submit/draft using the vendored API (which expects
            # ``form_type`` strings and reads ``test_mode`` from
            # ``self.result.form_data``). We can't pass ``well_record`` to the
            # vendored automator — the prototype doesn't accept it. Plan-level
            # data is already encoded into ``form_data`` by the adapter.
            result = await automator.execute_automation(form_data, auth)
            success = result.status == AutomationStatus.COMPLETED
            confirmation = result.agency_confirmation or ""
            screenshot = result.screenshots[0] if result.screenshots else ""
            error_detail = None
            if not success and result.error_details:
                error_detail = result.error_details.get("error")
            return _FilingRunResult(
                success=success,
                confirmation_number=confirmation,
                screenshot_path=screenshot,
                error=error_detail,
            )
        finally:
            await browser.close()


async def _save_trace(context, job, trace_retention, filing_failed: bool) -> None:
    """Stop tracing and persist the trace file according to retention policy.

    Retention rules:
      'failure_only' + success  → ``tracing.stop()`` with no path (discard).
      'failure_only' + failure  → ``tracing.stop(path=<storage_key>)`` then
                                  upload via ``default_storage``.
      'all'                     → always stop with path and upload.

    Storage path template:  filing_traces/<tenant_id>/<YYYY-MM-DD>/<job_id>.zip

    Two-step process for saves:
      1. ``tracing.stop(path=storage_key)`` writes the zip to a local path.
      2. Read bytes from that path (tolerating a missing file in test/mock
         environments where Playwright's stop() is itself mocked).
      3. ``default_storage.save(storage_key, ContentFile(bytes))``.
      4. Delete the local file if it exists.

    Errors here must NOT propagate — callers wrap this in try/except.
    """
    from datetime import date
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    should_save = (trace_retention == "all") or filing_failed

    if not should_save:
        # Discard: stop with no path argument.
        await context.tracing.stop()
        return

    # Build storage path template: filing_traces/<tenant_id>/<YYYY-MM-DD>/<job_id>.zip
    today = date.today().strftime("%Y-%m-%d")
    tenant_id_str = str(job.tenant_id)
    job_id_str = str(job.id)
    storage_key = f"filing_traces/{tenant_id_str}/{today}/{job_id_str}.zip"

    # Two-step persistence:
    #   1. tracing.stop(path=tmp_path) — writes the trace zip to a guaranteed-
    #      existing local temp directory (Playwright won't auto-create dirs).
    #   2. Read bytes from tmp_path and upload via default_storage.save().
    #   3. Delete the temp file in a finally block.
    fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix="w3a_trace_")
    os.close(fd)  # Playwright owns the writing; we just need the path.
    try:
        await context.tracing.stop(path=tmp_path)
        with open(tmp_path, "rb") as fh:
            trace_bytes = fh.read()
        default_storage.save(storage_key, ContentFile(trace_bytes))
        logger.info(
            "filing_automation: trace saved storage_key=%s bytes=%d",
            storage_key,
            len(trace_bytes),
        )
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


async def _run_filing_traced(auth, form_data, well_record, job_id, job, trace_retention):
    """Run the W-3A filing pipeline with Playwright trace recording.

    Architecture (auth-before-tracing, R1 requirement):
      1. Open playwright session via module-level ``async_playwright`` (patchable).
      2. Build automator (module-level ``RRCFormAutomator``) + call ``authenticate``.
      3. Start tracing on the context AFTER auth completes.
      4. Call ``_run_filing`` with the pre-authed context so it reuses the session.
      5. Stop tracing (retention-aware) in a non-masking try/except.
      6. Return ``(result, filing_exc)`` — caller decides how to handle the exc.

    The ``_run_filing`` call uses ``asyncio.iscoroutine`` to support both the real
    coroutine (production) and a synchronous mock (unit tests).
    """
    _ensure_playwright_imports()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.new_page()

            # Normalize form type (same logic as in _run_filing non-traced path).
            ft = (getattr(form_data, "form_type", "") or "").upper().replace("-", "")
            if ft:
                form_data.form_type = ft

            # Build the automator.  Uses module-level RRCFormAutomator so the
            # unit-test mock is picked up automatically.
            automator = RRCFormAutomator(context=context, session_id=str(job_id))

            # ── Step 1: Auth (BEFORE tracing, so credentials are never captured) ──
            # We call authenticate() explicitly here so the trace only starts
            # after the credential handshake, keeping the RRC password out of
            # the recorded network traffic.  Real auth failures propagate as
            # AuthenticationError and are handled by submit_w3a_to_rrc's
            # exception-class router.
            await automator.authenticate(auth)

            # ── Step 2: Start tracing (after auth) ──────────────────────────────
            await context.tracing.start(screenshots=True, snapshots=True, sources=False)

            # ── Step 3: Run the post-auth filing phases ──────────────────────────
            filing_exc: Exception | None = None
            result = None
            try:
                # Support both the real coroutine and a sync mock (test_tracing.py
                # patches _run_filing with return_value=... not AsyncMock).
                maybe_coro = _run_filing(
                    auth, form_data, well_record, job_id,
                    _pre_auth_context=context,
                )
                if asyncio.iscoroutine(maybe_coro):
                    result = await maybe_coro
                else:
                    result = maybe_coro
            except Exception as exc:
                filing_exc = exc

            # ── Step 4: Stop tracing (non-fatal; filing error takes priority) ────
            try:
                filing_failed = (
                    filing_exc is not None
                    or not getattr(result, "success", False)
                )
                await _save_trace(context, job, trace_retention, filing_failed)
            except Exception as trace_exc:
                logger.warning(
                    "filing_automation: tracing.stop failed (non-fatal): %s", trace_exc
                )

            return result, filing_exc
        finally:
            await browser.close()


def _mark_failed(job, exc, *, error_class: str | None = None) -> None:
    job.status = "failed"
    job.error_class = error_class or type(exc).__name__
    job.error_message = str(exc)[:2000]
    job.traceback_truncated = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[:8000]
    job.finished_at = timezone.now()
    job.save(update_fields=[
        "status", "error_class", "error_message", "traceback_truncated", "finished_at", "updated_at",
    ])


@shared_task(
    bind=True,
    name="apps.filing_automation.tasks.submit_w3a_to_rrc",
    queue="browser",
    max_retries=2,
    autoretry_for=(BrowserCrashed,),
    retry_backoff=30,
    retry_jitter=True,
)
def submit_w3a_to_rrc(self, snapshot_id, tenant_id, job_id):
    """Submit a W-3A PlanSnapshot to the Texas RRC portal via Playwright."""
    # Local imports to keep module import cheap.
    from django.core import exceptions as django_exceptions

    from apps.filing_automation.models import FilingJob
    from apps.filing_automation.services.adapter import (
        plan_snapshot_to_form_data,
    )
    from apps.filing_automation.services.profile_schema import BusinessProfileIncomplete
    from apps.intelligence.models import FilingStatusRecord, PortalCredential
    from apps.public_core.models import PlanSnapshot
    from apps.tenants.models import Tenant, TenantBusinessProfile
    from apps.filing_automation._vendor.regulagent_core.automation.base.data_models import AuthData

    tenant = Tenant.objects.get(id=tenant_id)
    set_current_tenant(tenant)

    try:
        credential_tenant_id = _normalize_tenant_id(tenant.id)
    except Exception:
        credential_tenant_id = tenant.id

    job = FilingJob.objects.get(pk=job_id)
    job.status = "running"
    job.started_at = timezone.now()
    job.attempt_count = self.request.retries + 1 if getattr(self, "request", None) else 1
    job.save(update_fields=["status", "started_at", "attempt_count", "updated_at"])

    # Resolve credential, snapshot, profile — terminal failures captured per-class.
    try:
        try:
            cred = PortalCredential.objects.filter(
                tenant_id=credential_tenant_id, agency="RRC", is_active=True
            ).first()
        except (django_exceptions.ValidationError, ValueError):
            cred = None
        if cred is None:
            raise LookupError(
                "No active RRC portal credential found for this tenant. "
                "Add credentials under Settings → Portal Credentials."
            )

        # ── Credential block gate ─────────────────────────────────────────
        # If the credential has been flagged as blocked (bad password or
        # account locked), abort immediately without touching the portal.
        # The tenant needs to update their credentials first.
        if getattr(cred, "is_login_blocked", lambda: False)():
            auth_state = getattr(cred, "auth_state", "blocked")
            _mark_failed(
                job,
                RuntimeError(
                    f"Portal credential is blocked (auth_state={auth_state}). "
                    "Update your RRC portal credentials in Filing Tracker settings "
                    "before submitting."
                ),
                error_class="CredentialBlocked",
            )
            return

        snap = PlanSnapshot.objects.select_related("well").get(id=snapshot_id)

        profile = (
            TenantBusinessProfile.objects.filter(tenant=tenant).first()
        )

        # If the credential was encrypted under an integer tenant id (the
        # django-tenants default PK) but the loaded model now exposes the
        # UUID-coerced form, decryption needs the original int-string key
        # input. We attempt the natural decryption first and fall back to
        # the integer reading on Fernet failure.
        try:
            cred_username = cred.get_username()
            cred_password = cred.get_password()
        except Exception:
            from cryptography.fernet import InvalidToken
            try:
                saved_tenant_id = cred.tenant_id
                cred.tenant_id = tenant.id
                cred_username = cred.get_username()
                cred_password = cred.get_password()
            except InvalidToken:
                cred.tenant_id = saved_tenant_id
                raise
            finally:
                cred.tenant_id = saved_tenant_id
        auth = AuthData(username=cred_username, password=cred_password)
        form_data, well_record = plan_snapshot_to_form_data(
            snap, job.attestation or {}, profile, enforce_profile=False
        )

        # Layered live-submit gates. Live submission to the real RRC portal
        # only fires when BOTH conditions are true:
        #   1. settings.RRC_LIVE_SUBMIT_ENABLED is True (deployment-level flag)
        #   2. cred.is_test is False (credential is a production account)
        # Otherwise we run in test_mode: form is filled and auto-saved as a
        # draft, but the final Submit button is NOT clicked.
        live_submit_allowed = bool(
            getattr(settings, "RRC_LIVE_SUBMIT_ENABLED", False)
        ) and not bool(getattr(cred, "is_test", False))
        form_data.test_mode = not live_submit_allowed
        logger.info(
            "filing_automation: live_submit_allowed=%s "
            "(RRC_LIVE_SUBMIT_ENABLED=%s, cred.is_test=%s) -> test_mode=%s",
            live_submit_allowed,
            getattr(settings, "RRC_LIVE_SUBMIT_ENABLED", False),
            getattr(cred, "is_test", False),
            form_data.test_mode,
        )
    except LookupError as exc:
        _mark_failed(job, exc, error_class="MissingPortalCredential")
        return
    except BusinessProfileIncomplete as exc:
        _mark_failed(job, exc, error_class="BusinessProfileIncomplete")
        return
    except Exception as exc:
        # Pydantic / payload validation is terminal too.
        exc_name = type(exc).__name__
        if "Validation" in exc_name or "PayloadIncomplete" in exc_name:
            _mark_failed(job, exc, error_class=exc_name)
            return
        raise

    # Execute the actual portal automation, optionally wrapped with trace recording.
    trace_enabled = bool(getattr(settings, "W3A_TRACE_ENABLED", False))
    trace_retention = getattr(settings, "W3A_TRACE_RETENTION", "failure_only")

    if trace_enabled:
        result, filing_exc = asyncio.run(
            _run_filing_traced(auth, form_data, well_record, job_id, job, trace_retention)
        )
        if filing_exc is not None:
            exc = filing_exc
            exc_name = type(exc).__name__
            try:
                raise exc
            except BrowserCrashed:
                raise
            except Exception:
                if isinstance(exc, _playwright_timeout_error()):
                    try:
                        raise self.retry(exc=exc, countdown=30)
                    except self.MaxRetriesExceededError:
                        _mark_failed(job, exc, error_class=exc_name)
                        return
                if exc_name in {"AuthenticationError", "FormSubmissionError"} or "Validation" in exc_name:
                    _mark_failed(job, exc, error_class=exc_name)
                    return
                _mark_failed(job, exc, error_class=exc_name)
                raise
    else:
        try:
            result = asyncio.run(_run_filing(auth, form_data, well_record, job_id))
        except BrowserCrashed:
            raise
        except Exception as exc:
            exc_name = type(exc).__name__
            # Transient: Playwright timeout → trigger Celery retry.
            if isinstance(exc, _playwright_timeout_error()):
                try:
                    raise self.retry(exc=exc, countdown=30)
                except self.MaxRetriesExceededError:
                    _mark_failed(job, exc, error_class=exc_name)
                    return
            # Terminal exception classes (matched by NAME so vendored vs. test-defined
            # classes both work — see tests/test_tasks.py which defines local stubs).
            if exc_name in {"AuthenticationError", "FormSubmissionError"} or "Validation" in exc_name:
                _mark_failed(job, exc, error_class=exc_name)
                return
            # Unknown failure — record + re-raise so Celery surfaces it.
            _mark_failed(job, exc, error_class=exc_name)
            raise

    # Result-level handling.
    if not getattr(result, "success", False):
        err = getattr(result, "error", None) or "Unknown filing failure"
        _mark_failed(job, RuntimeError(str(err)), error_class="FormSubmissionError")
        return

    confirmation = getattr(result, "confirmation_number", "") or ""
    screenshot = getattr(result, "screenshot_path", "") or ""

    # In test_mode the portal auto-saves a draft and does not return a real
    # confirmation number. Mint a synthetic one so downstream consumers
    # (FilingStatusRecord, frontend status page) always have a stable id.
    if not confirmation and getattr(form_data, "test_mode", False):
        import uuid as _uuid
        confirmation = f"DRAFT-{_uuid.uuid4()}"

    job.status = "succeeded"
    job.confirmation_number = confirmation
    job.screenshot_path = screenshot
    job.finished_at = timezone.now()
    job.save(update_fields=[
        "status", "confirmation_number", "screenshot_path", "finished_at", "updated_at",
    ])

    # Mark the snapshot filed and create an agency-side record.
    snap.status = PlanSnapshot.STATUS_FILED
    snap.save(update_fields=["status"])

    fsr = FilingStatusRecord.objects.create(
        filing_id=confirmation or f"PENDING-{job.id}",
        plan_snapshot=snap,
        tenant_id=credential_tenant_id,
        well=snap.well,
        agency="RRC",
        form_type="w3a",
        status="submitted",
        source="submitted",
        state=getattr(snap.well, "state", "") or "",
        district=getattr(snap.well, "district", "") or "",
        county=getattr(snap.well, "county", "") or "",
    )
    job.filing_status = fsr
    job.save(update_fields=["filing_status", "updated_at"])

    cred.last_successful_login = timezone.now()
    cred.save(update_fields=["last_successful_login", "updated_at"])
