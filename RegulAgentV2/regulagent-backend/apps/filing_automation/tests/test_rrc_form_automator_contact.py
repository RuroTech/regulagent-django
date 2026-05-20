"""
TDD (red phase) — contact information must come from calculated_data, not RRC_DEFAULTS.

These tests define the EXPECTED behaviour for Step 1 / A3 of the W-3A field-mapping plan.
All four tests FAIL until the production code is updated.

Contract:
  - _fill_contact_information must read contact_phone, contact_email, and
    cementing_company_name from form_data.calculated_data (populated by the adapter
    from TenantBusinessProfile.rrc.w3a.*), NOT from the hard-coded RRC_DEFAULTS dict.
  - Missing key: the method must raise a KeyError whose message names the missing field,
    so mis-configured profiles fail loudly rather than silently submitting BCM's details.

Design note on the missing-key contract (Test 4):
  We chose RAISE over warn-and-skip because this field populates a regulatory form
  that is submitted to the RRC. Silently filling nothing (or filling a blank) is
  worse than a loud failure that the operator can diagnose and fix before filing.
  The Backend Engineer should let the KeyError propagate out of _fill_contact_information;
  the wrapping try/except in fill_form_fields will log it as a step failure.

Signature ambiguity flagged for Lead (read before implementing):
  Today's call site is:
      await self._fill_contact_information(iframe)
  The method signature is:
      async def _fill_contact_information(self, iframe: Page)
  There is NO form_data argument.  The Backend Engineer must EITHER:
    (a) add a form_data kwarg:
          async def _fill_contact_information(self, iframe, form_data: FormData)
        and update the call site lambda to:
          lambda: self._fill_contact_information(iframe, form_data)
    OR
    (b) access self.result.form_data directly (it is set before fill_form_fields runs).
  Option (b) is lower-friction; option (a) is more testable.
  These tests use option (a) because it is the most explicit contract.
  If the Backend Engineer prefers option (b), the test helper _run_contact_fill
  must be updated accordingly (and the Lead should approve the change).
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure the encryption pepper is set so any PortalCredential operations work.
os.environ.setdefault("ENCRYPTION_PEPPER", "test-pepper-for-contact-tests")

# ---------------------------------------------------------------------------
# Module-level imports of the things under test
# ---------------------------------------------------------------------------

from apps.filing_automation._vendor.regulagent_core.automation.base.data_models import FormData
from apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc.rrc_form_automator import (
    RRCFormAutomator,
)
from apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc.rrc_config import (
    RRC_DEFAULTS,
)

# ---------------------------------------------------------------------------
# BCM sentinel values — what we must NOT see in any fill call
# ---------------------------------------------------------------------------
BCM_PHONE = RRC_DEFAULTS["contact_phone"]          # "432-580-7161"
BCM_EMAIL = RRC_DEFAULTS["contact_email"]          # "operations@bcmandassociates.com"
BCM_COMPANY = RRC_DEFAULTS["cementing_company"]    # "BCM & Associates, Inc; ..."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_automator() -> RRCFormAutomator:
    """Instantiate RRCFormAutomator with a minimal mock BrowserContext.

    We only need the context to satisfy __init__; no actual Playwright calls
    are made because we mock the iframe passed directly to the method.
    """
    mock_context = MagicMock()
    mock_context.pages = [MagicMock()]
    return RRCFormAutomator(context=mock_context, session_id="test-session")


def _make_iframe(fill_calls: list) -> MagicMock:
    """Return a mock Playwright Page (iframe) whose query_selector and locator
    return mock elements that record fill() calls in *fill_calls*.

    Structure mirrors the production code path:
        element = await iframe.query_selector(selector)
        if element:
            await element.fill(value)

    Also supports iframe.evaluate() for the react-datetime native-setter approach:
        await iframe.evaluate(js_string, [selector, date_string])
    """
    async def _fill(value):
        fill_calls.append(value)

    mock_element = MagicMock()
    mock_element.scroll_into_view_if_needed = AsyncMock()
    mock_element.fill = _fill
    mock_element.click = AsyncMock()
    mock_element.count = AsyncMock(return_value=1)

    async def _type(value, **kwargs):
        pass  # no-op in basic iframe

    async def _press(key):
        pass  # no-op in basic iframe

    mock_element.type = _type
    mock_element.press = _press

    async def _query_selector(selector):
        # Return a mock element for every selector so all branches execute.
        return mock_element

    def _locator(selector):
        # Return the same shared element so all locator paths work.
        return mock_element

    mock_iframe = MagicMock()
    mock_iframe.query_selector = _query_selector
    mock_iframe.locator = _locator
    mock_iframe.fill = AsyncMock()
    mock_iframe.evaluate = AsyncMock()  # supports iframe.evaluate(js, args)

    return mock_iframe


def _make_form_data(**calculated_overrides) -> FormData:
    """Return a FormData whose calculated_data contains the given overrides."""
    return FormData(
        api_number="42501705750001",
        form_type="W3A",
        calculated_data=calculated_overrides,
    )


def _run_contact_fill(automator: RRCFormAutomator, iframe, form_data: FormData):
    """Drive _fill_contact_information synchronously.

    NOTE: When the Backend Engineer adds the form_data parameter to the method,
    this helper must be updated to pass it:
        asyncio.run(automator._fill_contact_information(iframe, form_data))

    For now we call the current (no-form_data) signature to confirm the tests
    FAIL before implementation.
    """
    asyncio.run(automator._fill_contact_information(iframe, form_data))


# ===========================================================================
# Test 1 — contact_phone comes from calculated_data, not RRC_DEFAULTS
# ===========================================================================

class TestContactPhoneFromCalculatedData:
    """_fill_contact_information must use calculated_data['contact_phone']."""

    def test_phone_value_from_calculated_data(self):
        """Fill call contains tenant phone, not BCM hardcoded phone."""
        fill_calls: list = []
        automator = _make_automator()
        iframe = _make_iframe(fill_calls)

        form_data = _make_form_data(
            contact_phone="555-0100",
            contact_email="ops@example.com",
            cementing_company_name="Acme Cementing Inc.",
        )

        _run_contact_fill(automator, iframe, form_data)

        assert "555-0100" in fill_calls, (
            f"Expected '555-0100' in fill calls, got: {fill_calls}"
        )

    def test_phone_is_not_bcm_value(self):
        """No fill call should contain the BCM hardcoded phone number."""
        fill_calls: list = []
        automator = _make_automator()
        iframe = _make_iframe(fill_calls)

        form_data = _make_form_data(
            contact_phone="555-0100",
            contact_email="ops@example.com",
            cementing_company_name="Acme Cementing Inc.",
        )

        _run_contact_fill(automator, iframe, form_data)

        assert BCM_PHONE not in fill_calls, (
            f"BCM hardcoded phone '{BCM_PHONE}' must not appear in fill calls. "
            f"Got: {fill_calls}"
        )


# ===========================================================================
# Test 2 — contact_email comes from calculated_data, not RRC_DEFAULTS
# ===========================================================================

class TestContactEmailFromCalculatedData:
    """_fill_contact_information must use calculated_data['contact_email']."""

    def test_email_value_from_calculated_data(self):
        """Fill call contains tenant email, not BCM hardcoded email."""
        fill_calls: list = []
        automator = _make_automator()
        iframe = _make_iframe(fill_calls)

        form_data = _make_form_data(
            contact_phone="555-0100",
            contact_email="ops@example.com",
            cementing_company_name="Acme Cementing Inc.",
        )

        _run_contact_fill(automator, iframe, form_data)

        assert "ops@example.com" in fill_calls, (
            f"Expected 'ops@example.com' in fill calls, got: {fill_calls}"
        )

    def test_email_is_not_bcm_value(self):
        """No fill call should contain the BCM hardcoded email."""
        fill_calls: list = []
        automator = _make_automator()
        iframe = _make_iframe(fill_calls)

        form_data = _make_form_data(
            contact_phone="555-0100",
            contact_email="ops@example.com",
            cementing_company_name="Acme Cementing Inc.",
        )

        _run_contact_fill(automator, iframe, form_data)

        assert BCM_EMAIL not in fill_calls, (
            f"BCM hardcoded email '{BCM_EMAIL}' must not appear in fill calls. "
            f"Got: {fill_calls}"
        )


# ===========================================================================
# Test 3 — cementing_company_name comes from calculated_data, not RRC_DEFAULTS
# ===========================================================================

class TestCementingCompanyFromCalculatedData:
    """_fill_contact_information must use calculated_data['cementing_company_name']."""

    def test_cementing_company_from_calculated_data(self):
        """Fill call contains tenant cementing company, not BCM hardcoded string."""
        fill_calls: list = []
        automator = _make_automator()
        iframe = _make_iframe(fill_calls)

        form_data = _make_form_data(
            contact_phone="555-0100",
            contact_email="ops@example.com",
            cementing_company_name="Acme Cementing Inc.",
        )

        _run_contact_fill(automator, iframe, form_data)

        assert "Acme Cementing Inc." in fill_calls, (
            f"Expected 'Acme Cementing Inc.' in fill calls, got: {fill_calls}"
        )

    def test_cementing_company_is_not_bcm_value(self):
        """No fill call should contain the BCM hardcoded cementing company string."""
        fill_calls: list = []
        automator = _make_automator()
        iframe = _make_iframe(fill_calls)

        form_data = _make_form_data(
            contact_phone="555-0100",
            contact_email="ops@example.com",
            cementing_company_name="Acme Cementing Inc.",
        )

        _run_contact_fill(automator, iframe, form_data)

        # BCM_COMPANY starts with "BCM & Associates"; check the distinctive prefix.
        for call_val in fill_calls:
            assert "BCM & Associates" not in str(call_val), (
                f"BCM hardcoded cementing company must not appear in fill calls. "
                f"Found in: {call_val!r}. All calls: {fill_calls}"
            )


# ===========================================================================
# Test 4 — missing key must raise KeyError (loud failure contract)
# ===========================================================================

class TestMissingContactKeyRaisesError:
    """Missing calculated_data key must raise a KeyError naming the field.

    Contract rationale: silently filing an RRC form with blank/wrong contact
    details is worse than a loud KeyError the operator can diagnose.
    See module docstring for full rationale.
    """

    def test_missing_contact_phone_raises_key_error(self):
        """KeyError is raised when contact_phone is absent from calculated_data."""
        fill_calls: list = []
        automator = _make_automator()
        iframe = _make_iframe(fill_calls)

        # contact_phone deliberately omitted
        form_data = _make_form_data(
            contact_email="ops@example.com",
            cementing_company_name="Acme Cementing Inc.",
        )

        with pytest.raises((KeyError, ValueError)) as exc_info:
            _run_contact_fill(automator, iframe, form_data)

        # The error message must name the missing key so operators can diagnose quickly.
        error_text = str(exc_info.value)
        assert "contact_phone" in error_text, (
            f"KeyError/ValueError must name the missing key 'contact_phone'. "
            f"Got: {error_text!r}"
        )


# ===========================================================================
# Step 2 / A4 — Tests 8-11  (cementing textarea composite + EXT field)
# ===========================================================================
#
# These tests define the EXPECTED behaviour introduced in Step 2 of the
# W-3A field-mapping plan (A4).  All four FAIL until the production code is
# updated.
#
# Cementing textarea composite format (canonical, chosen by QA):
#
#   {cementing_company_name}\n
#   {cementing_company_address}\n
#   P-5: {cementing_company_p5}
#
# Rationale: the RRC textarea is labelled "Cementing Company" and accepts
# free text.  Placing P-5 on its own line (prefixed "P-5:") mirrors how
# operators hand-key the form and lets RRC staff parse each datum separately.
# No trailing newline on the last line.
#
# EXT field strategy:
#   The Backend Engineer may locate the EXT input by any selector:
#     - get_by_label("EXT", exact=True)
#     - query_selector('input[name="ext"]')
#     - locator('label:has-text("EXT") + input')
#   The _make_ext_iframe helper captures ALL fill() calls across ALL
#   locators/query_selector returns, so the Backend has full flexibility.
# ===========================================================================


def _make_ext_iframe(fill_calls: list) -> MagicMock:
    """Return a mock Playwright Page (iframe) that captures fill() calls from
    any locator or query_selector path, AND supports get_by_label().

    Structure mirrors _make_iframe but adds:
      - iframe.get_by_label(label, exact=...) → mock element whose .fill()
        records calls in fill_calls
      - iframe.locator(selector) → mock element whose .fill() records calls

    This gives the Backend Engineer freedom to choose any Playwright selector
    method for the EXT field.
    """
    async def _fill(value):
        fill_calls.append(value)

    def _make_elem():
        elem = MagicMock()
        elem.scroll_into_view_if_needed = AsyncMock()
        elem.fill = _fill
        elem.is_visible = AsyncMock(return_value=True)
        elem.count = AsyncMock(return_value=1)
        return elem

    shared_elem = _make_elem()

    async def _query_selector(selector):
        return shared_elem

    def _get_by_label(label, *, exact=False):
        return shared_elem

    def _locator(selector):
        return shared_elem

    mock_iframe = MagicMock()
    mock_iframe.query_selector = _query_selector
    mock_iframe.get_by_label = _get_by_label
    mock_iframe.locator = _locator
    mock_iframe.fill = AsyncMock()
    mock_iframe.evaluate = AsyncMock()  # supports iframe.evaluate(js, args)

    return mock_iframe


# ---------------------------------------------------------------------------
# Test 8 — cementing textarea filled with composite of name + address + p5
# ---------------------------------------------------------------------------

class TestCementingTextareaComposite:
    """_fill_contact_information must compose the cementing textarea from 3 fields."""

    def test_cementing_textarea_composite_with_all_three_values(self):
        """When name, address, and p5 are all present, textarea value must be
        the exact composite string (newline-separated, P-5: prefix on last line).

        FAILS until _fill_contact_information builds the composite string instead
        of writing only cementing_company_name.

        Expected fill value:
            'Acme Cementing Inc.\\nPO Box 13077, Odessa, TX 79768\\nP-5: 040196'
        """
        fill_calls: list = []
        automator = _make_automator()
        iframe = _make_ext_iframe(fill_calls)

        form_data = _make_form_data(
            contact_phone="555-0100",
            contact_email="ops@example.com",
            cementing_company_name="Acme Cementing Inc.",
            cementing_company_address="PO Box 13077, Odessa, TX 79768",
            cementing_company_p5="040196",
        )

        _run_contact_fill(automator, iframe, form_data)

        expected = "Acme Cementing Inc.\nPO Box 13077, Odessa, TX 79768\nP-5: 040196"
        assert expected in fill_calls, (
            f"Expected cementing textarea to be filled with exact composite:\n"
            f"  {expected!r}\n"
            f"Actual fill calls: {fill_calls}"
        )

    # -----------------------------------------------------------------------
    # Test 9 — textarea falls back to name-only when address and p5 are absent
    # -----------------------------------------------------------------------

    def test_cementing_textarea_falls_back_to_name_only(self):
        """When address and p5 are absent or empty, textarea must be filled with
        EXACTLY the company name — no trailing newlines, no 'P-5:' prefix, no
        extra whitespace.

        FAILS until _fill_contact_information (a) uses a build_cementing_text
        helper AND (b) handles the partial-data case cleanly.

        We anchor this test to the build_cementing_text helper that the Backend
        Engineer must add to rrc_form_automator.py (or its utils module).  If
        the helper does not exist, this test fails with ImportError — the correct
        red state.
        """
        from apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc.rrc_form_automator import (
            build_cementing_text,  # Backend Engineer must expose this helper
        )

        fill_calls: list = []
        automator = _make_automator()
        iframe = _make_ext_iframe(fill_calls)

        form_data = _make_form_data(
            contact_phone="555-0100",
            contact_email="ops@example.com",
            cementing_company_name="Acme Cementing Inc.",
            # address and p5 deliberately absent
        )

        _run_contact_fill(automator, iframe, form_data)

        # The cementing fill call must be EXACTLY the name — no extra content.
        assert "Acme Cementing Inc." in fill_calls, (
            f"'Acme Cementing Inc.' must appear in fill calls. Got: {fill_calls}"
        )
        # No call should be the composite (would imply blank address/p5 were included).
        for val in fill_calls:
            assert not str(val).startswith("Acme Cementing Inc.\n"), (
                f"Textarea must not contain trailing newline when address/p5 absent. "
                f"Got fill value: {val!r}"
            )
            assert "P-5:" not in str(val), (
                f"'P-5:' must not appear in fill value when p5 absent. Got: {val!r}"
            )


# ---------------------------------------------------------------------------
# Test 10 — EXT field filled when contact_ext is present
# ---------------------------------------------------------------------------

class TestExtFieldFilled:
    """_fill_contact_information must fill the EXT input when contact_ext is present."""

    def test_ext_field_filled_when_value_present(self):
        """When calculated_data has contact_ext='1234', at least one .fill('1234')
        call must be awaited.

        FAILS until _fill_contact_information locates and fills the EXT input.

        The Backend Engineer may choose any selector:
          - iframe.get_by_label('EXT', exact=True)
          - iframe.query_selector('input[name="ext"]')
          - iframe.locator('label:has-text("EXT") + input')
        All paths are mocked by _make_ext_iframe.
        """
        fill_calls: list = []
        automator = _make_automator()
        iframe = _make_ext_iframe(fill_calls)

        form_data = _make_form_data(
            contact_phone="555-0100",
            contact_email="ops@example.com",
            cementing_company_name="Acme Cementing Inc.",
            contact_ext="1234",
        )

        _run_contact_fill(automator, iframe, form_data)

        assert "1234" in fill_calls, (
            f"Expected .fill('1234') for EXT field when contact_ext='1234'. "
            f"Actual fill calls: {fill_calls}"
        )


# ---------------------------------------------------------------------------
# Test 11 — EXT field skipped when contact_ext is missing or blank
# ---------------------------------------------------------------------------

class TestExtFieldSkippedWhenBlank:
    """_fill_contact_information must NOT fill EXT when contact_ext is absent or ''."""

    def test_ext_field_skipped_when_value_missing_or_blank(self):
        """When contact_ext is absent OR '' (blank), the EXT input must NOT be filled.

        Phone/email/cementing fills should still happen normally.

        Covers two cases in one test:
          a) contact_ext key entirely absent from calculated_data
          b) contact_ext = "" (blank string)

        FAILS until:
          1. build_cementing_text is exposed (ImportError anchors red state), AND
          2. _fill_contact_information skips the EXT fill for absent/blank values.

        The Backend Engineer must guard: `if ext_value: await ext_field.fill(ext_value)`.
        """
        from apps.filing_automation._vendor.regulagent_core.automation.agencies.rrc.rrc_form_automator import (
            build_cementing_text,  # Backend Engineer must expose this helper
        )

        # --- Case A: contact_ext absent ---
        fill_calls_absent: list = []
        automator_a = _make_automator()
        iframe_a = _make_ext_iframe(fill_calls_absent)

        form_data_absent = _make_form_data(
            contact_phone="555-0100",
            contact_email="ops@example.com",
            cementing_company_name="Acme Cementing Inc.",
            # contact_ext deliberately absent
        )

        try:
            _run_contact_fill(automator_a, iframe_a, form_data_absent)
        except (KeyError, ValueError) as exc:
            pytest.fail(
                f"_fill_contact_information must not raise when contact_ext is absent. "
                f"Got: {exc!r}"
            )

        assert "555-0100" in fill_calls_absent, (
            f"Phone fill must happen when contact_ext absent. Got: {fill_calls_absent}"
        )
        assert "ops@example.com" in fill_calls_absent, (
            f"Email fill must happen when contact_ext absent. Got: {fill_calls_absent}"
        )
        for unexpected in ("1234", "555"):
            assert unexpected not in fill_calls_absent, (
                f"EXT must not be filled with {unexpected!r} when absent. "
                f"Got: {fill_calls_absent}"
            )

        # --- Case B: contact_ext = "" ---
        fill_calls_blank: list = []
        automator_b = _make_automator()
        iframe_b = _make_ext_iframe(fill_calls_blank)

        form_data_blank = _make_form_data(
            contact_phone="555-0100",
            contact_email="ops@example.com",
            cementing_company_name="Acme Cementing Inc.",
            contact_ext="",
        )

        try:
            _run_contact_fill(automator_b, iframe_b, form_data_blank)
        except Exception as exc:
            pytest.fail(
                f"_fill_contact_information must not raise when contact_ext is ''. "
                f"Got: {exc!r}"
            )

        assert "555-0100" in fill_calls_blank, (
            f"Phone fill must happen when contact_ext=''. Got: {fill_calls_blank}"
        )
        # No empty-string fill — the EXT guard `if ext_value` must prevent this.
        assert "" not in fill_calls_blank, (
            f"EXT (or any field) must not be filled with ''. "
            f"Implement: `if ext_value: await ext_field.fill(ext_value)`. "
            f"Got fill_calls: {fill_calls_blank}"
        )


# ===========================================================================
# Test 13 — EXT uses .phone-ext CSS selector, not get_by_label
# (Fix 2: the EXT label is not wired via for/id; direct CSS selector needed)
# ===========================================================================

class TestExtFieldUsesCssSelector:
    """_fill_contact_information must call iframe.locator('.phone-ext input.form-control').

    The DOM has:
        <div class="phone-ext"><label>EXT</label><input class="form-control" value=""/></div>

    The label is NOT associated via for/id, so get_by_label("EXT") does not match.
    The implementation must use iframe.locator('.phone-ext input.form-control').
    """

    def test_phone_ext_locator_called(self):
        """iframe.locator must be called with '.phone-ext input.form-control'."""
        locator_calls: list = []

        async def _fill(value):
            pass

        mock_locator_elem = MagicMock()
        mock_locator_elem.click = AsyncMock()
        mock_locator_elem.fill = AsyncMock(side_effect=_fill)
        mock_locator_elem.type = AsyncMock()
        mock_locator_elem.press = AsyncMock()
        mock_locator_elem.count = AsyncMock(return_value=1)
        mock_locator_elem.scroll_into_view_if_needed = AsyncMock()

        async def _query_selector(selector):
            return mock_locator_elem

        def _locator(selector):
            locator_calls.append(selector)
            return mock_locator_elem

        mock_iframe = MagicMock()
        mock_iframe.query_selector = _query_selector
        mock_iframe.locator = _locator
        mock_iframe.get_by_label = MagicMock(return_value=mock_locator_elem)
        mock_iframe.fill = AsyncMock()
        mock_iframe.evaluate = AsyncMock()  # supports iframe.evaluate(js, args)

        automator = _make_automator()

        form_data = _make_form_data(
            contact_phone="555-0100",
            contact_email="ops@example.com",
            cementing_company_name="Acme Cementing Inc.",
            contact_ext="1234",
        )

        asyncio.run(automator._fill_contact_information(mock_iframe, form_data))

        assert '.phone-ext input.form-control' in locator_calls, (
            f"Expected iframe.locator('.phone-ext input.form-control') to be called. "
            f"Actual locator() calls: {locator_calls!r}"
        )
