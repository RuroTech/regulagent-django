"""
Tests for FilingSyncer — upsert logic, credential lookup, and well matching.

Strategy
--------
- ``test_sync_no_credentials`` — pure mock, no DB needed.
- ``test_sync_creates_new_filings``, ``test_sync_updates_changed_status``,
  ``test_sync_skips_unchanged``, ``test_sync_handles_no_well_match`` — use
  the real ORM (``@pytest.mark.django_db``) for FilingStatusRecord and
  WellRegistry.  Only Playwright, portal scraping, and the credential lookup
  are mocked.

This project has no ``pytest-asyncio``, so the coroutine under test is driven
synchronously via ``asyncio.get_event_loop().run_until_complete(...)`` — the
same idiom used by ``test_filing_syncer_well_stub.py``.  DB-backed tests use
``@pytest.mark.django_db(transaction=True)`` so writes made on the
``sync_to_async`` worker thread are committed and visible to the main thread.
ORM calls go through ``_run_db`` (``sync_to_async``-based helper); the
credential lookup is mocked by patching
``apps.intelligence.services.filing_syncer._run_db`` for the first call only,
then delegating to the real ``_run_db`` for all subsequent ORM operations.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.intelligence.services.filing_syncer import FilingSyncer
from apps.intelligence.services import filing_syncer as _fs_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_filing_data(
    filing_id: str = "RRC-2026-001",
    status: str = "pending",
    well_api: str | None = "42501705750000",
    remarks: str = "",
    form_type: str = "w3a",
) -> dict:
    """Build a minimal filing dict matching the BasePortalScraper contract."""
    return {
        "filing_id": filing_id,
        "form_type": form_type,
        "status": status,
        "portal_url": f"https://webapps.rrc.texas.gov/EWA/ewastatus.do?filingId={filing_id}",
        "status_date": "2026-03-15",
        "remarks": remarks,
        "reviewer_name": "Jane Smith",
        "well_api": well_api,
        "raw_data": {
            "filing_id": filing_id,
            "status": status,
        },
    }


def _make_mock_scraper(filings: list[dict]) -> MagicMock:
    """Return a mock scraper whose async methods return the given filings list."""
    scraper = MagicMock()
    scraper.authenticate = AsyncMock(return_value=MagicMock())
    scraper.scrape_filings_list = AsyncMock(return_value=filings)
    scraper.check_filing_status = AsyncMock()
    return scraper


def _make_mock_playwright_cm(browser: MagicMock) -> MagicMock:
    """Return a mock async context manager that yields a playwright-like object."""
    pw_mock = MagicMock()
    pw_mock.chromium.launch = AsyncMock(return_value=browser)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=pw_mock)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_mock_browser() -> MagicMock:
    context = MagicMock()
    context.new_page = AsyncMock(return_value=MagicMock())

    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    return browser


# ---------------------------------------------------------------------------
# 1. No credentials — early exit without touching the browser
# ---------------------------------------------------------------------------


def test_sync_no_credentials():
    """
    When PortalCredential.DoesNotExist is raised the syncer returns an error
    dict with error='no_credentials' and all counts set to zero.
    """
    from apps.intelligence.models import PortalCredential

    syncer = FilingSyncer()
    tenant_id = str(uuid.uuid4())

    # _run_db is the async helper that wraps all ORM calls; patch it to raise
    # DoesNotExist so sync_filings returns the no-credentials error path.
    async def _fake_run_db(fn):
        raise PortalCredential.DoesNotExist()

    with patch(
        "apps.intelligence.services.filing_syncer.async_playwright"
    ) as mock_pw, patch.object(_fs_module, "_run_db", side_effect=_fake_run_db):
        result = asyncio.get_event_loop().run_until_complete(
            syncer.sync_filings(tenant_id=tenant_id, agency="RRC")
        )

    assert result["status"] == "error"
    assert result["error"] == "no_credentials"
    assert result["created"] == 0
    assert result["updated"] == 0
    assert result["unchanged"] == 0
    assert result["errors"] == 0
    # Browser must not have been opened
    mock_pw.assert_not_called()


# ---------------------------------------------------------------------------
# 2. New filings created (happy path)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_sync_creates_new_filings():
    """
    When the scraper returns 2 filings and neither exists in the DB yet,
    both should be created with source='synced' and summary shows created=2.
    """
    from apps.intelligence.models import FilingStatusRecord, PortalCredential
    from apps.public_core.models import WellRegistry

    tenant_id = uuid.uuid4()

    # Pre-create the well so FK can be satisfied
    well = WellRegistry.objects.create(
        api14="42501705750001",
        state="TX",
        county="Andrews",
        district="8A",
        operator_name="Test Op",
        field_name="Field A",
        lease_name="Lease A",
        well_number="1",
    )

    filings = [
        _make_filing_data(filing_id="RRC-NEW-001", well_api=well.api14),
        _make_filing_data(filing_id="RRC-NEW-002", well_api=well.api14),
    ]
    scraper = _make_mock_scraper(filings)
    browser = _make_mock_browser()

    credential = MagicMock(spec=PortalCredential)
    credential.id = uuid.uuid4()
    # Credential circuit breaker (is_login_blocked) must report unblocked,
    # otherwise sync_filings raises InvalidCredentialsError before scraping.
    credential.is_login_blocked.return_value = False

    syncer = FilingSyncer()

    with patch(
        "apps.intelligence.services.filing_syncer.async_playwright",
        return_value=_make_mock_playwright_cm(browser),
    ), patch(
        "apps.intelligence.services.portal_scrapers.get_scraper",
        return_value=scraper,
    ):
        # Intercept only the first _run_db call (credential lookup); let all
        # subsequent ORM calls pass through to the real _run_db implementation.
        real_run_db = _fs_module._run_db
        call_count = 0

        async def selective_run_db(fn):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return credential
            return await real_run_db(fn)

        with patch.object(_fs_module, "_run_db", side_effect=selective_run_db):
            result = asyncio.get_event_loop().run_until_complete(
                syncer.sync_filings(tenant_id=str(tenant_id), agency="RRC")
            )

    assert result["status"] == "success"
    assert result["created"] == 2
    assert result["updated"] == 0
    assert result["unchanged"] == 0
    assert result["errors"] == 0

    records = list(
        FilingStatusRecord.objects.filter(tenant_id=tenant_id).order_by("filing_id")
    )
    assert len(records) == 2
    assert all(r.source == "synced" for r in records)
    filing_ids = {r.filing_id for r in records}
    assert filing_ids == {"RRC-NEW-001", "RRC-NEW-002"}

    # Cleanup
    FilingStatusRecord.objects.filter(tenant_id=tenant_id).delete()
    well.delete()


# ---------------------------------------------------------------------------
# 3. Existing filing with changed status → updated
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_sync_updates_changed_status():
    """
    When the scraper returns a filing whose status differs from the existing
    DB record, the record should be updated and summary shows updated=1.
    """
    from apps.intelligence.models import FilingStatusRecord, PortalCredential
    from apps.public_core.models import WellRegistry

    tenant_id = uuid.uuid4()

    well = WellRegistry.objects.create(
        api14="42501705750002",
        state="TX",
        county="Andrews",
        district="8A",
        operator_name="Test Op",
        field_name="Field B",
        lease_name="Lease B",
        well_number="2",
    )

    # Pre-create existing record with 'pending' status
    existing = FilingStatusRecord.objects.create(
        filing_id="RRC-UPD-001",
        tenant_id=tenant_id,
        well=well,
        agency="RRC",
        form_type="w3a",
        status="pending",
        source="synced",
    )

    # Scraper returns the same filing but with status 'approved'
    filings = [_make_filing_data(filing_id="RRC-UPD-001", status="approved", well_api=well.api14)]
    scraper = _make_mock_scraper(filings)
    browser = _make_mock_browser()

    credential = MagicMock(spec=PortalCredential)
    credential.id = uuid.uuid4()
    # Credential circuit breaker (is_login_blocked) must report unblocked,
    # otherwise sync_filings raises InvalidCredentialsError before scraping.
    credential.is_login_blocked.return_value = False

    syncer = FilingSyncer()

    with patch(
        "apps.intelligence.services.filing_syncer.async_playwright",
        return_value=_make_mock_playwright_cm(browser),
    ), patch(
        "apps.intelligence.services.portal_scrapers.get_scraper",
        return_value=scraper,
    ):
        real_run_db = _fs_module._run_db
        call_count = 0

        async def selective_run_db(fn):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return credential
            return await real_run_db(fn)

        with patch.object(_fs_module, "_run_db", side_effect=selective_run_db):
            result = asyncio.get_event_loop().run_until_complete(
                syncer.sync_filings(tenant_id=str(tenant_id), agency="RRC")
            )

    assert result["status"] == "success"
    assert result["updated"] == 1
    assert result["created"] == 0
    assert result["unchanged"] == 0

    existing.refresh_from_db()
    assert existing.status == "approved"

    # Cleanup
    existing.delete()
    well.delete()


# ---------------------------------------------------------------------------
# 4. Unchanged — same status, same remarks → no DB write
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_sync_skips_unchanged():
    """
    When the scraper returns a filing whose status and remarks match the
    existing record exactly, summary should show unchanged=1 and the record
    must not be mutated.
    """
    from apps.intelligence.models import FilingStatusRecord, PortalCredential
    from apps.public_core.models import WellRegistry

    tenant_id = uuid.uuid4()

    well = WellRegistry.objects.create(
        api14="42501705750003",
        state="TX",
        county="Andrews",
        district="8A",
        operator_name="Test Op",
        field_name="Field C",
        lease_name="Lease C",
        well_number="3",
    )

    existing = FilingStatusRecord.objects.create(
        filing_id="RRC-SAME-001",
        tenant_id=tenant_id,
        well=well,
        agency="RRC",
        form_type="w3a",
        status="approved",
        agency_remarks="",
        source="synced",
    )
    original_updated_at = existing.updated_at

    # Scraper returns same status and empty remarks
    filings = [_make_filing_data(filing_id="RRC-SAME-001", status="approved", remarks="", well_api=well.api14)]
    scraper = _make_mock_scraper(filings)
    browser = _make_mock_browser()

    credential = MagicMock(spec=PortalCredential)
    credential.id = uuid.uuid4()
    # Credential circuit breaker (is_login_blocked) must report unblocked,
    # otherwise sync_filings raises InvalidCredentialsError before scraping.
    credential.is_login_blocked.return_value = False

    syncer = FilingSyncer()

    with patch(
        "apps.intelligence.services.filing_syncer.async_playwright",
        return_value=_make_mock_playwright_cm(browser),
    ), patch(
        "apps.intelligence.services.portal_scrapers.get_scraper",
        return_value=scraper,
    ):
        real_run_db = _fs_module._run_db
        call_count = 0

        async def selective_run_db(fn):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return credential
            return await real_run_db(fn)

        with patch.object(_fs_module, "_run_db", side_effect=selective_run_db):
            result = asyncio.get_event_loop().run_until_complete(
                syncer.sync_filings(tenant_id=str(tenant_id), agency="RRC")
            )

    assert result["status"] == "success"
    assert result["unchanged"] == 1
    assert result["updated"] == 0
    assert result["created"] == 0

    # Cleanup
    existing.delete()
    well.delete()


# ---------------------------------------------------------------------------
# 5. No well match → filing skipped (counted as error)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_sync_handles_missing_well_api():
    """
    When a filing provides NO well_api at all, it cannot be tied to a well
    (FilingStatusRecord.well is required) so it is skipped and counted under
    errors, and no record is created.

    Note: an unmatched-but-valid API does NOT error — it auto-creates a stub
    WellRegistry row (covered by test_filing_syncer_well_stub.py).  The error
    path only triggers when no API number is present.
    """
    from apps.intelligence.models import FilingStatusRecord, PortalCredential

    tenant_id = uuid.uuid4()

    # No well_api at all → cannot create a stub → filing is skipped/errored
    filings = [_make_filing_data(filing_id="RRC-NOWL-001", well_api=None)]
    scraper = _make_mock_scraper(filings)
    browser = _make_mock_browser()

    credential = MagicMock(spec=PortalCredential)
    credential.id = uuid.uuid4()
    # Credential circuit breaker (is_login_blocked) must report unblocked,
    # otherwise sync_filings raises InvalidCredentialsError before scraping.
    credential.is_login_blocked.return_value = False

    syncer = FilingSyncer()

    with patch(
        "apps.intelligence.services.filing_syncer.async_playwright",
        return_value=_make_mock_playwright_cm(browser),
    ), patch(
        "apps.intelligence.services.portal_scrapers.get_scraper",
        return_value=scraper,
    ):
        real_run_db = _fs_module._run_db
        call_count = 0

        async def selective_run_db(fn):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return credential
            return await real_run_db(fn)

        with patch.object(_fs_module, "_run_db", side_effect=selective_run_db):
            result = asyncio.get_event_loop().run_until_complete(
                syncer.sync_filings(tenant_id=str(tenant_id), agency="RRC")
            )

    assert result["status"] == "success"
    assert result["errors"] == 1
    assert result["created"] == 0
    assert result["updated"] == 0

    # No record should have been created
    count = FilingStatusRecord.objects.filter(
        filing_id="RRC-NOWL-001", tenant_id=tenant_id
    ).count()
    assert count == 0


# ---------------------------------------------------------------------------
# FilingSyncer._parse_date (static helper, no DB needed)
# ---------------------------------------------------------------------------


class TestFilingSyncerParseDate:
    def test_parse_valid_iso_date(self):
        from datetime import date

        result = FilingSyncer._parse_date("2026-03-15")
        assert result == date(2026, 3, 15)

    def test_parse_none_returns_none(self):
        assert FilingSyncer._parse_date(None) is None

    def test_parse_empty_string_returns_none(self):
        assert FilingSyncer._parse_date("") is None

    def test_parse_invalid_string_returns_none(self):
        assert FilingSyncer._parse_date("not-a-date") is None

    def test_parse_mm_dd_yyyy_returns_none(self):
        """The syncer's _parse_date only handles ISO strings; MM/DD/YYYY is the scraper's job."""
        result = FilingSyncer._parse_date("03/15/2026")
        assert result is None  # fromisoformat raises ValueError for this format
