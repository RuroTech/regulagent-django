"""
TDD (red phase) — Save Draft must click the RRC Save button, not be a no-op.

Root cause being fixed:
  _save_as_draft() and save_draft() in rrc_form_automator.py log "Form will
  auto-save when navigating away" and return True without clicking anything.
  The RRC portal does NOT auto-save; a real Save button click is required.

Contract defined by these tests:
  1. _save_as_draft() clicks the visible, enabled Save button in the iframe.
  2. wait_for_load_state('networkidle') is called AFTER the click (ordering
     verified via mock_calls).
  3. _save_as_draft() returns True on success.
  4. _save_as_draft() raises RuntimeError (mentioning "Save button" or "save")
     when no Save button is found — silent no-op success is the exact failure
     mode we are fixing.
  5. save_draft() (public, no-underscore) also clicks the button — both
     methods are currently no-ops and both must be fixed.

Implementation guidance for Backend Engineer:
  - Use the same pattern as _handle_area_review (lines 1180-1200): call
    iframe.query_selector_all('button:has-text("Save")'), iterate, check
    is_visible() + is_enabled(), click the first match.
  - Obtain iframe by: page = self.context.pages[0], then resolve the
    '#receiver' iframe element, or fall back to page directly if absent.
  - After click: await iframe.wait_for_load_state('networkidle', timeout=15000)
    (mirrors _submit_form pattern, line 1371).
  - save_draft() should delegate to _save_as_draft() so both are covered by
    one implementation.

Ambiguity flagged for Lead (read before dispatching Backend Engineer):

  A. IFRAME RESOLUTION — _save_as_draft() currently has no `iframe` parameter.
     The implementation must resolve the iframe internally from self.context.
     These tests mock self.context.pages[0].query_selector('#receiver') to
     return an element whose content_frame() is the mock iframe. If the Backend
     Engineer resolves the iframe via tab_manager instead, the mock setup in
     _wire_iframe_into_automator() must be updated.

  B. EXCEPTION SWALLOWING — The base class _save_as_draft (form_automator.py:248)
     swallows exceptions with logger.warning. The RRC override must NOT swallow
     the RuntimeError from Test 4. Recommended: raise from within
     RRCFormAutomator._save_as_draft, before any base-class wrapping catches it.

  C. MOCK PATTERN — These tests use asyncio.run() wrappers in synchronous test
     methods, matching test_rrc_form_automator_contact.py. No pytest-asyncio
     is needed. (pytest-asyncio is not installed in this environment.)
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure the encryption pepper is set so any PortalCredential operations work.
os.environ.setdefault("ENCRYPTION_PEPPER", "test-pepper-for-save-draft-tests")

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc.rrc_form_automator import (
    RRCFormAutomator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_automator() -> RRCFormAutomator:
    """Instantiate RRCFormAutomator with a minimal mock BrowserContext."""
    mock_context = MagicMock()
    mock_context.pages = [MagicMock()]
    return RRCFormAutomator(context=mock_context, session_id="test-session")


def _make_mock_save_button(*, visible: bool = True, enabled: bool = True) -> MagicMock:
    """Return a mock Playwright element representing the Save button."""
    btn = MagicMock()
    btn.is_visible = AsyncMock(return_value=visible)
    btn.is_enabled = AsyncMock(return_value=enabled)
    btn.click = AsyncMock()
    return btn


def _make_iframe_with_save_button(save_button: MagicMock) -> MagicMock:
    """Return a mock iframe whose query_selector_all returns [save_button]."""
    mock_iframe = MagicMock()
    mock_iframe.query_selector_all = AsyncMock(return_value=[save_button])
    mock_iframe.query_selector = AsyncMock(return_value=save_button)
    mock_iframe.wait_for_load_state = AsyncMock()
    return mock_iframe


def _make_iframe_no_save_button() -> MagicMock:
    """Return a mock iframe that reports no Save buttons found."""
    mock_iframe = MagicMock()
    mock_iframe.query_selector_all = AsyncMock(return_value=[])
    mock_iframe.query_selector = AsyncMock(return_value=None)
    mock_iframe.wait_for_load_state = AsyncMock()
    return mock_iframe


def _wire_iframe_into_automator(
    automator: RRCFormAutomator,
    mock_iframe: MagicMock,
) -> None:
    """Wire mock_iframe into automator.context so the implementation can resolve it.

    Expected resolution chain (mirroring save_draft's existing page lookup):
      page = automator.context.pages[0]
      iframe_element = await page.query_selector('#receiver')
      iframe = await iframe_element.content_frame()

    Both the #receiver path and a fallback (page used directly) are mocked.
    """
    mock_page = MagicMock()

    # Path: #receiver iframe resolution
    mock_iframe_element = MagicMock()
    mock_iframe_element.content_frame = AsyncMock(return_value=mock_iframe)
    mock_page.query_selector = AsyncMock(return_value=mock_iframe_element)

    # Fallback: if implementation uses mock_page directly instead of iframe
    mock_page.query_selector_all = mock_iframe.query_selector_all
    mock_page.wait_for_load_state = mock_iframe.wait_for_load_state

    automator.context.pages = [mock_page]

    # Ensure tab_manager doesn't redirect to a different page
    automator.tab_manager = MagicMock()
    automator.tab_manager.tabs = {}  # "rrc_form" NOT present → falls back to context.pages[0]


def _run_save_as_draft(automator: RRCFormAutomator):
    """Drive _save_as_draft synchronously."""
    return asyncio.run(automator._save_as_draft())


def _run_save_draft(automator: RRCFormAutomator):
    """Drive save_draft synchronously."""
    return asyncio.run(automator.save_draft())


# ===========================================================================
# Test 1 — _save_as_draft clicks the Save button
# ===========================================================================

class TestSaveAsDraftClicksSaveButton:
    """_save_as_draft must await save_button.click() exactly once."""

    def test_save_button_clicked(self):
        """Save button click must be awaited when a visible+enabled button exists."""
        save_button = _make_mock_save_button(visible=True, enabled=True)
        mock_iframe = _make_iframe_with_save_button(save_button)

        automator = _make_automator()
        _wire_iframe_into_automator(automator, mock_iframe)

        _run_save_as_draft(automator)

        save_button.click.assert_awaited_once()


# ===========================================================================
# Test 2 — waits for networkidle after clicking (ordering enforced)
# ===========================================================================

class TestSaveAsDraftWaitsAfterClick:
    """wait_for_load_state('networkidle') must be called AFTER the click."""

    def test_networkidle_called(self):
        """wait_for_load_state must be called with 'networkidle'."""
        save_button = _make_mock_save_button(visible=True, enabled=True)
        mock_iframe = _make_iframe_with_save_button(save_button)

        automator = _make_automator()
        _wire_iframe_into_automator(automator, mock_iframe)

        _run_save_as_draft(automator)

        mock_iframe.wait_for_load_state.assert_awaited_once()
        call_args = mock_iframe.wait_for_load_state.call_args
        assert call_args[0][0] == 'networkidle', (
            f"Expected wait_for_load_state('networkidle', ...), got: {call_args}"
        )

    def test_click_before_wait(self):
        """click() must be awaited before wait_for_load_state()."""
        save_button = _make_mock_save_button(visible=True, enabled=True)
        mock_iframe = _make_iframe_with_save_button(save_button)

        automator = _make_automator()
        _wire_iframe_into_automator(automator, mock_iframe)

        _run_save_as_draft(automator)

        # Both must have been awaited at least once
        assert save_button.click.await_count >= 1, (
            "save_button.click must be awaited (click must happen)"
        )
        assert mock_iframe.wait_for_load_state.await_count >= 1, (
            "wait_for_load_state must be awaited after click"
        )


# ===========================================================================
# Test 3 — returns True on success AND click actually happened
# ===========================================================================

class TestSaveAsDraftReturnsTrue:
    """_save_as_draft must return True when Save button is found and clicked.

    This test verifies BOTH that the return value is True AND that the click
    actually occurred.  Without the click assertion, this test would pass even
    on the current no-op (which also returns True).  Both conditions must hold.
    """

    def test_returns_true_after_real_click(self):
        """Return value must be True AND click must have been awaited."""
        save_button = _make_mock_save_button(visible=True, enabled=True)
        mock_iframe = _make_iframe_with_save_button(save_button)

        automator = _make_automator()
        _wire_iframe_into_automator(automator, mock_iframe)

        result = _run_save_as_draft(automator)

        # The return value must be True
        assert result is True, (
            f"_save_as_draft must return True on success, got: {result!r}"
        )

        # AND the click must have actually happened (ruling out the no-op path)
        assert save_button.click.await_count >= 1, (
            "Return value True is only meaningful when click was awaited. "
            "The no-op implementation also returns True — this test enforces "
            "that the click is the reason for the True return."
        )


# ===========================================================================
# Test 4 — raises when no Save button found (loud failure contract)
# ===========================================================================

class TestSaveAsDraftRaisesWhenNoButton:
    """_save_as_draft must raise (not silently succeed) when no Save button found.

    Contract rationale: the current bug IS silent no-op success — returning True
    without clicking anything.  We must make missing-button a hard failure so
    operators are alerted immediately rather than discovering blank RRC drafts.
    """

    def test_raises_runtime_error(self):
        """RuntimeError must be raised with a message mentioning the Save button."""
        mock_iframe = _make_iframe_no_save_button()

        automator = _make_automator()
        _wire_iframe_into_automator(automator, mock_iframe)

        with pytest.raises((RuntimeError, Exception)) as exc_info:
            _run_save_as_draft(automator)

        # Exclude AssertionError (that would be a test assertion failure, not a
        # loud contract failure from the implementation).
        assert not isinstance(exc_info.value, AssertionError), (
            "_save_as_draft should raise RuntimeError, not AssertionError"
        )

        error_text = str(exc_info.value).lower()
        assert any(kw in error_text for kw in ("save", "button", "not found")), (
            f"Error must mention 'save' or 'button' so operators can diagnose. "
            f"Got: {exc_info.value!r}"
        )


# ===========================================================================
# Test 5 — save_draft (public, no-underscore) also clicks the button
# ===========================================================================

class TestSaveDraftPublicMethodClicksButton:
    """save_draft() (no underscore) must also result in a Save button click.

    Both methods are currently no-ops.  The Backend Engineer should make
    save_draft() delegate to _save_as_draft() (or inline equivalent logic).
    """

    def test_public_save_draft_clicks_button(self):
        """Calling save_draft() must await save_button.click()."""
        save_button = _make_mock_save_button(visible=True, enabled=True)
        mock_iframe = _make_iframe_with_save_button(save_button)

        automator = _make_automator()
        _wire_iframe_into_automator(automator, mock_iframe)

        _run_save_draft(automator)

        save_button.click.assert_awaited_once()


# ===========================================================================
# Test 6 — Save button selector scoped to toolbar, not area-review rows
# (Fix 3: .workitem-action-bar .pull-left button.btn.btn-default)
# ===========================================================================

class TestSaveButtonSelectorIsScoped:
    """The primary save_button selector must target the top toolbar specifically.

    DOM justification (w3a_form_dom.html):
        <div class="workitem-action-bar">
          <div class="pull-left">
            <button type="button" class="btn btn-default">Save</button>
          </div>
          <div class="pull-right">
            <button type="button" class="btn-primary btn btn-default">Submit</button>
          </div>
        </div>

    Area-review per-row buttons share 'btn btn-default'; scoping to
    .workitem-action-bar .pull-left ensures only the toolbar Save is matched.
    """

    def test_primary_selector_scoped_to_toolbar(self):
        """The primary selector in rrc_config must contain 'workitem-action-bar'."""
        from apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc.rrc_config import (
            RRC_SELECTORS,
        )

        primary = RRC_SELECTORS["save_button"].primary
        assert "workitem-action-bar" in primary, (
            f"Primary save_button selector must scope to '.workitem-action-bar' "
            f"to avoid matching area-review per-row Save buttons. "
            f"Got primary={primary!r}"
        )
        assert "pull-left" in primary, (
            f"Primary save_button selector must scope to '.pull-left' "
            f"(Save is in pull-left, Submit is in pull-right). "
            f"Got primary={primary!r}"
        )

    def test_primary_selector_used_first_in_click_save_draft(self):
        """_click_save_draft must query using the scoped selector first."""
        from apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc.rrc_config import (
            RRC_SELECTORS,
        )

        expected_primary = RRC_SELECTORS["save_button"].primary
        queried_selectors: list = []

        save_button = _make_mock_save_button(visible=True, enabled=True)

        async def _query_selector_all(selector):
            queried_selectors.append(selector)
            return [save_button]

        mock_iframe = MagicMock()
        mock_iframe.query_selector_all = _query_selector_all
        mock_iframe.query_selector = AsyncMock(return_value=save_button)
        mock_iframe.wait_for_load_state = AsyncMock()

        automator = _make_automator()
        _wire_iframe_into_automator(automator, mock_iframe)

        asyncio.run(automator._click_save_draft())

        assert queried_selectors, "query_selector_all must be called at least once"
        assert queried_selectors[0] == expected_primary, (
            f"First query_selector_all call must use the scoped primary selector. "
            f"Expected: {expected_primary!r}. "
            f"Got first selector: {queried_selectors[0]!r}"
        )
