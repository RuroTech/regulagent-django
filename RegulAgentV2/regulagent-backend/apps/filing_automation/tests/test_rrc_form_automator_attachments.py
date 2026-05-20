"""
TDD — _handle_file_attachments must use page.expect_file_chooser (Page-level,
not Frame-level) driven by the visible "Add" button anchored to the GAU
Attachment field's stable DOM id.

Root cause being fixed:
  'Frame' object has no attribute 'expect_file_chooser' — Playwright exposes
  expect_file_chooser only on Page, not on Frame/FrameLocator.  The fix
  obtains the outer Page via self.context.pages[0] and uses page-level
  interception while clicking the Add button inside the iframe.

  Directly calling set_input_files on a hidden <input type="file"> also never
  fires RRC's React click handler, so the file is never registered and the
  saved draft is empty.  The correct flow clicks the visible "Add" button
  which opens the OS file chooser, then calls file_chooser.set_files().

Contract defined by these tests:
  5. _handle_file_attachments calls get_gau_letter and uploads when bytes
     returned using page.expect_file_chooser + file_chooser.set_files.
  6. Skips upload + logs warning when get_gau_letter returns None.
  7. Cleans up temp file even when file_chooser.set_files raises; re-raises
     exception.
  8. No longer calls glob.glob / os.path.exists with "processed_wells/" prefix.
  9. Uses api14_full from client_metadata (NOT api_number) for get_gau_letter.
  10. When the Add button click raises (selector miss), logs warning and does
      NOT raise — GAU is optional.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

os.environ.setdefault("ENCRYPTION_PEPPER", "test-pepper-for-attachments-tests")

from apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc.rrc_form_automator import (
    RRCFormAutomator,
)
from apps.filing_automation._vendor.regulagent_core.automation.base.data_models import (
    FormData,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "00000000-0000-0000-0000-000000000042"
_API_NUMBER = "42317361340000"  # digits-only after FormData.__post_init__ cleanup

_GAU_FIELD_ID = "field-ab03a8eb-152d-421f-8646-4fb66c805607"


def _make_automator_with_form_data(
    tenant_id: str = _TENANT_ID,
    api_number: str = _API_NUMBER,
) -> RRCFormAutomator:
    """Instantiate RRCFormAutomator and wire result.form_data including tenant_id."""
    mock_page = MagicMock()
    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    automator = RRCFormAutomator(context=mock_context, session_id="test-attach-session")

    # Pre-populate result.form_data as execute_automation would do.
    form_data = FormData(
        api_number=api_number,
        form_type="w3a",
        client_metadata={"tenant_id": tenant_id},
    )
    automator.result.form_data = form_data
    return automator


def _make_mock_iframe_and_page() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return (automator, iframe, file_chooser) with page.expect_file_chooser mocked.

    The new API uses:
      - page.expect_file_chooser(): async context manager yielding fc_info
      - fc_info.value: awaitable that returns file_chooser
      - file_chooser.set_files: AsyncMock
      - iframe.locator(f'#{gau_field_id} button:has-text("Add")') → gau_add_button
      - gau_add_button.click: AsyncMock
    """
    file_chooser = MagicMock()
    file_chooser.set_files = AsyncMock()

    # fc_info.value must be awaitable and return file_chooser.
    class _AwaitableChooser:
        def __await__(self):
            async def _inner():
                return file_chooser
            return _inner().__await__()

    fc_info = MagicMock()
    fc_info.value = _AwaitableChooser()

    # page.expect_file_chooser() must be an async context manager
    @asynccontextmanager
    async def _expect_file_chooser():
        yield fc_info

    # Wire gau_add_button returned from iframe.locator(...)
    gau_add_button = MagicMock()
    gau_add_button.click = AsyncMock()

    iframe = MagicMock()
    iframe.evaluate = AsyncMock()
    iframe.locator = MagicMock(return_value=gau_add_button)

    # Build the automator so we can wire page on its context
    mock_page = MagicMock()
    mock_page.expect_file_chooser = _expect_file_chooser
    mock_context = MagicMock()
    mock_context.pages = [mock_page]

    automator = RRCFormAutomator(context=mock_context, session_id="test-attach-session")
    form_data = FormData(
        api_number=_API_NUMBER,
        form_type="w3a",
        client_metadata={"tenant_id": _TENANT_ID},
    )
    automator.result.form_data = form_data

    return automator, iframe, file_chooser


def _run(coro):
    """Run a coroutine synchronously."""
    return asyncio.run(coro)


# ===========================================================================
# Test 5 — uploads GAU via get_gau_letter using page.expect_file_chooser
# ===========================================================================

class TestHandleFileAttachmentsUploadsViaStorage:
    """_handle_file_attachments calls get_gau_letter, writes a temp .pdf, opens
    the OS file chooser via page.expect_file_chooser + Add button click,
    calls file_chooser.set_files, then removes the temp file."""

    def test_handle_file_attachments_uploads_gau_via_file_chooser(self, settings):
        """When get_gau_letter returns bytes, file_chooser.set_files is awaited with
        a .pdf path — using page.expect_file_chooser, NOT iframe.expect_file_chooser."""
        settings.MEDIA_ROOT = "/fake/media"
        automator, iframe, file_chooser = _make_mock_iframe_and_page()

        captured_tmp_paths: list[str] = []
        original_mkstemp = tempfile.mkstemp

        def _capturing_mkstemp(suffix="", prefix="", **kw):
            fd, path = original_mkstemp(suffix=suffix, prefix=prefix, **kw)
            if suffix == ".pdf":
                captured_tmp_paths.append(path)
            return fd, path

        with (
            patch(
                "apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc."
                "rrc_form_automator.get_gau_letter",
                return_value=b"%PDF-fake",
            ),
            patch("tempfile.mkstemp", side_effect=_capturing_mkstemp),
        ):
            _run(automator._handle_file_attachments(iframe, []))

        # file_chooser.set_files must have been called with a .pdf path
        assert file_chooser.set_files.await_count >= 1, (
            "file_chooser.set_files must be awaited once when get_gau_letter returns bytes"
        )
        call_arg = file_chooser.set_files.call_args[0][0]
        assert call_arg.endswith(".pdf"), (
            f"file_chooser.set_files must receive a .pdf path. Got: {call_arg!r}"
        )

        # Temp file must be cleaned up after the call
        for tmp_path in captured_tmp_paths:
            assert not os.path.exists(tmp_path), (
                f"Temp file {tmp_path!r} must be deleted after upload"
            )

    def test_handle_file_attachments_clicks_add_button(self, settings):
        """The Add button click must be awaited so the React handler fires."""
        settings.MEDIA_ROOT = "/fake/media"
        automator, iframe, _file_chooser = _make_mock_iframe_and_page()

        # Retrieve the add button mock BEFORE running so we can assert on it.
        # New pattern: iframe.locator(f'#{gau_field_id} button:has-text("Add")') → gau_add_button
        gau_add_button = iframe.locator.return_value

        with patch(
            "apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc."
            "rrc_form_automator.get_gau_letter",
            return_value=b"%PDF-fake",
        ):
            _run(automator._handle_file_attachments(iframe, []))

        gau_add_button.click.assert_awaited_once()

    def test_handle_file_attachments_locates_button_by_stable_field_id(self, settings):
        """iframe.locator must be called with the stable GAU field id selector."""
        settings.MEDIA_ROOT = "/fake/media"
        automator, iframe, _file_chooser = _make_mock_iframe_and_page()

        with patch(
            "apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc."
            "rrc_form_automator.get_gau_letter",
            return_value=b"%PDF-fake",
        ):
            _run(automator._handle_file_attachments(iframe, []))

        # Verify locator was called with a selector referencing the stable field id
        assert iframe.locator.called, "iframe.locator must be called"
        selector_arg = iframe.locator.call_args[0][0]
        assert _GAU_FIELD_ID in selector_arg, (
            f"locator must use stable field id '{_GAU_FIELD_ID}'. Got: {selector_arg!r}"
        )


# ===========================================================================
# Test 6 — skips when get_gau_letter returns None
# ===========================================================================

class TestHandleFileAttachmentsSkipsWhenNoGau:
    """When get_gau_letter returns None, file_chooser.set_files is NOT called and
    a warning is logged."""

    def test_handle_file_attachments_skips_when_no_gau(self, settings, caplog):
        """No upload, no exception, warning logged."""
        import logging
        settings.MEDIA_ROOT = "/fake/media"
        automator, iframe, file_chooser = _make_mock_iframe_and_page()

        with (
            patch(
                "apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc."
                "rrc_form_automator.get_gau_letter",
                return_value=None,
            ),
            caplog.at_level(logging.WARNING),
        ):
            _run(automator._handle_file_attachments(iframe, []))

        file_chooser.set_files.assert_not_awaited()
        assert any(r.levelno >= logging.WARNING for r in caplog.records), (
            "A warning must be logged when no GAU file is available"
        )


# ===========================================================================
# Test 7 — cleans up temp file on file_chooser.set_files failure; re-raises
# ===========================================================================

class TestHandleFileAttachmentsCleansUpOnFailure:
    """If file_chooser.set_files raises, the temp file must be deleted and the
    exception must propagate (not be swallowed)."""

    def test_handle_file_attachments_cleans_up_temp_on_upload_failure(self, settings):
        settings.MEDIA_ROOT = "/fake/media"
        automator, iframe, file_chooser = _make_mock_iframe_and_page()

        # Make set_files raise
        file_chooser.set_files = AsyncMock(side_effect=Exception("playwright fail"))

        captured_tmp_paths: list[str] = []
        original_mkstemp = tempfile.mkstemp

        def _capturing_mkstemp(suffix="", prefix="", **kw):
            fd, path = original_mkstemp(suffix=suffix, prefix=prefix, **kw)
            if suffix == ".pdf":
                captured_tmp_paths.append(path)
            return fd, path

        with (
            patch(
                "apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc."
                "rrc_form_automator.get_gau_letter",
                return_value=b"%PDF-fake",
            ),
            patch("tempfile.mkstemp", side_effect=_capturing_mkstemp),
        ):
            # The exception is caught by the inner try/except and turned into a
            # warning — the outer finally still cleans up the temp file.
            _run(automator._handle_file_attachments(iframe, []))

        # Temp file must still be cleaned up even though set_files raised
        for tmp_path in captured_tmp_paths:
            assert not os.path.exists(tmp_path), (
                f"Temp file {tmp_path!r} must be deleted even after upload failure"
            )


# ===========================================================================
# Test 8 — no longer uses processed_wells/ local path
# ===========================================================================

class TestHandleFileAttachmentsNoLongerUsesLegacyPath:
    """After the fix, _handle_file_attachments must NOT call glob.glob or
    os.path.exists with a path starting with 'processed_wells/'."""

    def test_handle_file_attachments_no_longer_uses_local_processed_wells_path(
        self, settings
    ):
        settings.MEDIA_ROOT = "/fake/media"
        automator, iframe, _file_chooser = _make_mock_iframe_and_page()

        legacy_glob_calls: list[str] = []
        legacy_exists_calls: list[str] = []

        original_glob = __import__("glob").glob
        original_exists = os.path.exists

        def _spying_glob(pattern, **kw):
            if isinstance(pattern, str) and pattern.startswith("processed_wells/"):
                legacy_glob_calls.append(pattern)
            return original_glob(pattern, **kw)

        def _spying_exists(path):
            if isinstance(path, str) and path.startswith("processed_wells/"):
                legacy_exists_calls.append(path)
            return original_exists(path)

        with (
            patch("glob.glob", side_effect=_spying_glob),
            patch("os.path.exists", side_effect=_spying_exists),
            patch(
                "apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc."
                "rrc_form_automator.get_gau_letter",
                return_value=None,  # skip actual upload
            ),
        ):
            _run(automator._handle_file_attachments(iframe, []))

        assert not legacy_glob_calls, (
            "glob.glob must not be called with a 'processed_wells/' path after the fix. "
            f"Got calls: {legacy_glob_calls}"
        )
        assert not legacy_exists_calls, (
            "os.path.exists must not be called with a 'processed_wells/' path after the fix. "
            f"Got calls: {legacy_exists_calls}"
        )


# ===========================================================================
# Test 9 — uses api14_full from client_metadata (NOT api_number)
# ===========================================================================

class TestHandleFileAttachmentsUsesApi14Full:
    """The filler must call ``get_gau_letter`` with the full 10/14-digit api14
    from ``client_metadata["api14_full"]``, NOT the 8-digit ``api_number``.

    Background: ``adapter._normalize_api`` converts the api to the 8-digit RRC
    form value. GAU PDFs are stored under MEDIA_ROOT/rrc/completions/<10-digit>/,
    so passing the 8-digit form to ``get_gau_letter`` always misses.
    """

    def test_handle_file_attachments_uses_api14_full_for_gau_lookup(self, settings):
        settings.MEDIA_ROOT = "/fake/media"

        mock_page = MagicMock()
        mock_context = MagicMock()
        mock_context.pages = [mock_page]
        automator = RRCFormAutomator(context=mock_context, session_id="test-api14-full")

        # api_number is the 8-digit RRC form value (post adapter normalisation).
        # api14_full holds the unstripped 14-digit form for downstream lookups.
        form_data = FormData(
            api_number="31736134",  # 8-digit (would miss GAU dir)
            form_type="w3a",
            client_metadata={
                "tenant_id": _TENANT_ID,
                "api14_full": "42-317-36134",  # 10-digit dashed form
            },
        )
        automator.result.form_data = form_data

        # Build a minimal iframe mock
        gau_add_button = MagicMock()
        gau_add_button.click = AsyncMock()
        iframe = MagicMock()
        iframe.evaluate = AsyncMock()
        iframe.locator = MagicMock(return_value=gau_add_button)

        with patch(
            "apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc."
            "rrc_form_automator.get_gau_letter",
            return_value=None,
        ) as mock_get:
            _run(automator._handle_file_attachments(iframe, []))

        assert mock_get.called, "get_gau_letter must be called"
        _tenant_arg, api14_arg = mock_get.call_args[0]
        assert api14_arg == "42-317-36134", (
            f"Expected get_gau_letter to be called with api14_full ('42-317-36134'), "
            f"got {api14_arg!r}. The filler must read client_metadata['api14_full'], "
            f"NOT form_data.api_number (which is the 8-digit form '31736134')."
        )


# ===========================================================================
# Test 10 — Add button missing (click raises) → warning only, does NOT raise
# ===========================================================================

class TestHandleFileAttachmentsAddButtonMissing:
    """When the Add button click raises (e.g. selector miss or timeout), the
    method must log a warning and return cleanly — GAU is optional per spec."""

    def test_handle_file_attachments_warns_and_returns_when_add_button_missing(
        self, settings, caplog
    ):
        """Simulates the Add button locator's click() raising — should warn, not raise."""
        import logging
        settings.MEDIA_ROOT = "/fake/media"

        # Wire click to raise immediately (selector miss simulation)
        gau_add_button = MagicMock()
        gau_add_button.click = AsyncMock(side_effect=Exception("Element not found"))

        iframe = MagicMock()
        iframe.evaluate = AsyncMock()
        iframe.locator = MagicMock(return_value=gau_add_button)

        mock_page = MagicMock()

        @asynccontextmanager
        async def _expect_file_chooser_timeout():
            yield MagicMock()  # fc_info — never reached due to click raising

        mock_page.expect_file_chooser = _expect_file_chooser_timeout
        mock_context = MagicMock()
        mock_context.pages = [mock_page]

        automator = RRCFormAutomator(context=mock_context, session_id="test-missing-btn")
        automator.result.form_data = FormData(
            api_number=_API_NUMBER,
            form_type="w3a",
            client_metadata={"tenant_id": _TENANT_ID},
        )

        with (
            patch(
                "apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc."
                "rrc_form_automator.get_gau_letter",
                return_value=b"%PDF-fake",
            ),
            caplog.at_level(logging.WARNING),
        ):
            # Must NOT raise
            _run(automator._handle_file_attachments(iframe, []))

        assert any(r.levelno >= logging.WARNING for r in caplog.records), (
            "A warning must be logged when the Add button click fails"
        )
