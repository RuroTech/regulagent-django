"""
FilingSyncer — orchestrates syncing filings from agency portals into FilingStatusRecord.

Consumes the portal_scrapers abstraction layer (BasePortalScraper / get_scraper)
to authenticate, enumerate all filings visible in the portal, then upsert each
one into FilingStatusRecord.

Entry point:
    syncer = FilingSyncer()
    result = await syncer.sync_filings(tenant_id="...", agency="RRC")

Well resolution note
--------------------
When a filing references a well_api that cannot be matched to an existing
WellRegistry row, FilingSyncer now auto-creates a minimal stub WellRegistry
entry (api14 + state derived from the agency) and immediately dispatches the
research/enrichment pipeline via ``start_research_session_task``.  This ensures
the filing can be persisted immediately while the background pipeline populates
documents, components, and metadata asynchronously.

A filing is only skipped (counted under ``errors``) when the portal data
provides no well_api at all — in that case there is no API number to create a
stub for.
"""

import asyncio
import logging
from datetime import date

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

# Maps agency code → two-letter US state abbreviation used in WellRegistry / ResearchSession
AGENCY_STATE_MAP = {
    "RRC": "TX",
    "NMOCD": "NM",
}


class FilingSyncer:
    """
    Syncs filings from an agency portal into FilingStatusRecord.

    Each call to ``sync_filings`` opens a single Playwright browser session,
    authenticates with the portal, scrapes the full filings list, then
    upserts each filing into the database.

    Returns a summary dict with counts of created / updated / unchanged /
    wells_created / error records, suitable for logging or returning in a
    Celery task result.
    """

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def sync_filings(self, tenant_id: str, agency: str = "RRC") -> dict:
        """
        Sync all filings for *tenant_id* from the *agency* portal.

        Algorithm
        ---------
        1. Fetch PortalCredential — return early with error if none found.
        2. Resolve concrete scraper via get_scraper(agency).
        3. Launch headless Playwright browser.
        4. Authenticate → scrape filings list.
        5. Upsert each filing into FilingStatusRecord (created/updated/unchanged).
        6. Close browser.
        7. Return summary dict.

        Parameters
        ----------
        tenant_id:
            UUID string of the tenant to sync for.
        agency:
            Uppercase agency code matching a registered scraper.
            Defaults to ``"RRC"``.

        Returns
        -------
        dict
            ``{status, tenant_id, agency, total_scraped, created,
               updated, unchanged, wells_created, errors}``
        """
        from apps.intelligence.models import PortalCredential
        from apps.intelligence.services.portal_scrapers import get_scraper

        logger.info(
            "FilingSyncer.sync_filings: starting sync tenant=%s agency=%s",
            tenant_id,
            agency,
        )

        # ── 1. Credential lookup ──────────────────────────────────────────
        try:
            credential = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: PortalCredential.objects.get(
                    tenant_id=tenant_id,
                    agency=agency.upper(),
                    is_active=True,
                ),
            )
        except PortalCredential.DoesNotExist:
            logger.warning(
                "FilingSyncer: no active PortalCredential for tenant=%s agency=%s",
                tenant_id,
                agency,
            )
            return {
                "status": "error",
                "error": "no_credentials",
                "tenant_id": tenant_id,
                "agency": agency,
                "total_scraped": 0,
                "created": 0,
                "updated": 0,
                "unchanged": 0,
                "wells_created": 0,
                "errors": 0,
            }

        # ── 1b. Defensive login gate (belt-and-suspenders) ────────────────
        # The task layer should have already checked this, but guard here too
        # so the service is safe when called directly (e.g. management commands).
        if credential.is_login_blocked():
            from apps.intelligence.services.portal_scrapers.exceptions import (
                InvalidCredentialsError,
            )
            logger.warning(
                "FilingSyncer: credential blocked for tenant=%s agency=%s "
                "(auth_state=%s) — refusing to authenticate",
                tenant_id,
                agency,
                credential.auth_state,
            )
            raise InvalidCredentialsError(
                f"Credential for tenant={tenant_id} agency={agency} is blocked "
                f"(auth_state={credential.auth_state}). Update credentials before retrying."
            )

        # ── 2. Resolve scraper ────────────────────────────────────────────
        try:
            scraper = get_scraper(agency)
        except KeyError as exc:
            logger.error("FilingSyncer: unknown agency '%s': %s", agency, exc)
            return {
                "status": "error",
                "error": f"unknown_agency:{agency}",
                "tenant_id": tenant_id,
                "agency": agency,
                "total_scraped": 0,
                "created": 0,
                "updated": 0,
                "unchanged": 0,
                "wells_created": 0,
                "errors": 0,
            }

        created = 0
        updated = 0
        unchanged = 0
        errors = 0
        wells_created = 0
        filings: list[dict] = []

        # ── 3-6. Browser session ──────────────────────────────────────────
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                )

                # 4a. Authenticate
                page = await scraper.authenticate(credential, context)

                # 4b. Scrape filings list
                filings = await scraper.scrape_filings_list(page)

                logger.info(
                    "FilingSyncer: scraped %d filing(s) for tenant=%s agency=%s",
                    len(filings),
                    tenant_id,
                    agency,
                )

                # 5. Upsert each filing
                for filing_data in filings:
                    filing_id = filing_data.get("filing_id", "<unknown>")
                    try:
                        # Resolve well — auto-creates a stub if api is present but unmatched
                        well, well_was_created = await self._resolve_well(
                            filing_data.get("well_api"), tenant_id, agency
                        )

                        if well_was_created:
                            wells_created += 1

                        outcome = await self._upsert_filing(
                            filing_data, tenant_id, agency, well
                        )

                        if outcome == "created":
                            created += 1
                        elif outcome == "updated":
                            updated += 1
                        elif outcome == "unchanged":
                            unchanged += 1
                        else:
                            # "skipped" — logged inside _upsert_filing (no well_api provided)
                            errors += 1

                    except Exception as exc:
                        errors += 1
                        logger.exception(
                            "FilingSyncer: error upserting filing_id=%s tenant=%s: %s",
                            filing_id,
                            tenant_id,
                            exc,
                        )
                        # Continue — one bad filing must not abort the whole sync

            finally:
                await browser.close()

        summary = {
            "status": "success",
            "tenant_id": tenant_id,
            "agency": agency,
            "total_scraped": len(filings),
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "wells_created": wells_created,
            "errors": errors,
        }

        logger.info(
            "FilingSyncer.sync_filings: complete tenant=%s agency=%s "
            "total=%d created=%d updated=%d unchanged=%d wells_created=%d errors=%d",
            tenant_id,
            agency,
            len(filings),
            created,
            updated,
            unchanged,
            wells_created,
            errors,
        )

        return summary

    # ------------------------------------------------------------------
    # Well resolution helper
    # ------------------------------------------------------------------

    async def _resolve_well(
        self,
        well_api: str | None,
        tenant_id: str,
        agency: str,
    ):
        """
        Resolve a well_api string to a WellRegistry row, auto-creating a stub
        if no match is found and the api number is non-blank.

        Parameters
        ----------
        well_api:
            Raw API-14 (or partial) string from the portal.  May be None
            or empty — returns ``(None, False)`` immediately in that case.
        tenant_id:
            UUID of the owning tenant — passed to the ResearchSession so the
            enrichment pipeline runs in the correct tenant context.
        agency:
            Uppercase agency code (e.g. ``"RRC"``).  Used to derive the state
            via ``AGENCY_STATE_MAP`` when creating a stub WellRegistry entry.

        Returns
        -------
        tuple[WellRegistry | None, bool]
            ``(well_instance, was_created)`` where *was_created* is True only
            when a new stub WellRegistry row was inserted by this call.
            Never raises — errors are logged and ``(None, False)`` is returned.
        """
        from django.db import IntegrityError
        from apps.public_core.models import WellRegistry, ResearchSession
        from apps.public_core.tasks_research import start_research_session_task

        if not well_api or not well_api.strip():
            return None, False

        # Normalise: strip dashes/spaces that portals sometimes include
        normalised = well_api.strip().replace("-", "").replace(" ", "")

        try:
            # ── 1. Try to match an existing WellRegistry row ──────────────
            well = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: WellRegistry.objects.filter(
                    api14__contains=normalised
                ).first(),
            )

            if well:
                logger.debug(
                    "FilingSyncer._resolve_well: matched well_api=%s → api14=%s",
                    well_api,
                    well.api14,
                )
                return well, False

            # ── 2. No match — create a minimal stub ───────────────────────
            logger.debug(
                "FilingSyncer._resolve_well: no WellRegistry match for well_api=%s, "
                "creating stub",
                well_api,
            )

            state = AGENCY_STATE_MAP.get(agency.upper(), "TX")

            try:
                well = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: WellRegistry.objects.create(
                        api14=normalised,
                        state=state,
                    ),
                )
                was_created = True
                logger.info(
                    "FilingSyncer._resolve_well: created WellRegistry stub "
                    "api14=%s state=%s tenant=%s",
                    normalised,
                    state,
                    tenant_id,
                )
            except IntegrityError:
                # Race condition — another sync created the same row between our
                # filter() and create() calls.  Fetch the existing row instead.
                logger.debug(
                    "FilingSyncer._resolve_well: IntegrityError creating api14=%s "
                    "(race condition), fetching existing row",
                    normalised,
                )
                well = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: WellRegistry.objects.filter(
                        api14=normalised
                    ).first(),
                )
                was_created = False

            # ── 3. Dispatch research / enrichment pipeline ────────────────
            # Only dispatch for newly created wells — race-condition wells
            # were already created (and presumably researched) by another sync.
            if was_created:
                try:
                    session = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: ResearchSession.objects.create(
                            api_number=normalised,
                            state=state,
                            tenant_id=tenant_id,
                            well=well,
                            status="pending",
                        ),
                    )
                    start_research_session_task.delay(str(session.id))
                    logger.info(
                        "FilingSyncer._resolve_well: dispatched research pipeline "
                        "session=%s api14=%s tenant=%s",
                        session.id,
                        normalised,
                        tenant_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "FilingSyncer._resolve_well: failed to dispatch research pipeline "
                        "for api14=%s tenant=%s: %s",
                        normalised,
                        tenant_id,
                        exc,
                    )
                    # Don't fail the filing — continue with the stub well

            return well, was_created

        except Exception as exc:
            logger.warning(
                "FilingSyncer._resolve_well: error resolving well_api=%s tenant=%s: %s",
                well_api,
                tenant_id,
                exc,
            )
            return None, False

    # ------------------------------------------------------------------
    # Upsert helper
    # ------------------------------------------------------------------

    async def _upsert_filing(
        self,
        filing_data: dict,
        tenant_id: str,
        agency: str,
        well,
    ) -> str:
        """
        Get-or-create a FilingStatusRecord for the supplied filing data.

        Parameters
        ----------
        filing_data:
            Dict conforming to the BasePortalScraper contract — must
            contain at minimum: ``filing_id``, ``status``, ``remarks``.
        tenant_id:
            UUID string of the owning tenant.
        agency:
            Uppercase agency code (e.g. ``"RRC"``).
        well:
            Resolved ``WellRegistry`` instance, or None (only when portal
            provided no well_api at all).

        Returns
        -------
        str
            ``"created"``, ``"updated"``, ``"unchanged"``, or ``"skipped"``
            (when no well_api was provided in the portal data).

        Notes
        -----
        FilingStatusRecord.well is non-nullable.  When *well* is None the
        filing cannot be persisted and is counted as an error by the caller.
        """
        from apps.intelligence.models import FilingStatusRecord

        filing_id: str = filing_data.get("filing_id", "")
        status: str = filing_data.get("status", "under_review")
        remarks: str = filing_data.get("remarks", "")
        reviewer_name: str = filing_data.get("reviewer_name", "")
        portal_url: str = filing_data.get("portal_url", "")
        form_type: str = filing_data.get("form_type", "")
        raw_data: dict = filing_data.get("raw_data", {})

        # Extract geo fields from raw_data (populated by the scraper)
        district: str = raw_data.get("district", "")
        county: str = raw_data.get("county", "")
        state: str = AGENCY_STATE_MAP.get(agency.upper(), "")

        # Parse status_date string → Python date (or None)
        status_date: date | None = self._parse_date(filing_data.get("status_date"))

        # ── Check for existing record ─────────────────────────────────────
        existing: FilingStatusRecord | None = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: FilingStatusRecord.objects.filter(
                filing_id=filing_id,
                agency=agency.upper(),
                tenant_id=tenant_id,
            ).first(),
        )

        if existing:
            # ── UPDATE path ───────────────────────────────────────────────
            changed = (
                existing.status != status
                or existing.agency_remarks != remarks
            )

            if not changed:
                logger.debug(
                    "FilingSyncer._upsert_filing: unchanged filing_id=%s tenant=%s",
                    filing_id,
                    tenant_id,
                )
                return "unchanged"

            # Apply changes
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: FilingStatusRecord.objects.filter(pk=existing.pk).update(
                    status=status,
                    agency_remarks=remarks,
                    reviewer_name=reviewer_name,
                    status_date=status_date,
                    portal_url=portal_url,
                    raw_portal_data=raw_data,
                    state=state,
                    district=district,
                    county=county,
                ),
            )

            old_status = existing.status

            logger.debug(
                "FilingSyncer._upsert_filing: updated filing_id=%s "
                "old_status=%s new_status=%s tenant=%s",
                filing_id,
                old_status,
                status,
                tenant_id,
            )

            # Dispatch remarks fetch if status changed TO a rejection type
            REJECTION_STATUSES = {"rejected", "revision_requested", "deficiency"}
            if status in REJECTION_STATUSES and old_status not in REJECTION_STATUSES:
                try:
                    from apps.intelligence.tasks_polling import fetch_filing_remarks
                    fetch_filing_remarks.delay(str(existing.pk), tenant_id, agency)
                    logger.info(
                        "FilingSyncer: dispatched fetch_filing_remarks for updated filing_id=%s %s->%s",
                        filing_id, old_status, status,
                    )
                except Exception as exc:
                    logger.warning(
                        "FilingSyncer: failed to dispatch fetch_filing_remarks for updated filing_id=%s: %s",
                        filing_id, exc,
                    )

            return "updated"

        # ── CREATE path ───────────────────────────────────────────────────
        if well is None:
            logger.warning(
                "FilingSyncer._upsert_filing: cannot create FilingStatusRecord for "
                "filing_id=%s tenant=%s — no well_api provided in portal data "
                "(well FK is required).",
                filing_id,
                tenant_id,
            )
            return "skipped"

        record = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: FilingStatusRecord.objects.create(
                filing_id=filing_id,
                tenant_id=tenant_id,
                agency=agency.upper(),
                form_type=form_type,
                status=status,
                source="synced",
                well=well,
                agency_remarks=remarks,
                reviewer_name=reviewer_name,
                status_date=status_date,
                portal_url=portal_url,
                raw_portal_data=raw_data,
                state=state,
                district=district,
                county=county,
            ),
        )

        logger.debug(
            "FilingSyncer._upsert_filing: created filing_id=%s status=%s tenant=%s",
            filing_id,
            status,
            tenant_id,
        )

        # For returned/rejected filings: dispatch a separate task to fetch
        # the return reason from the portal detail page, then feed into the
        # rejection pipeline.
        REJECTION_STATUSES = {"rejected", "revision_requested", "deficiency"}
        if status in REJECTION_STATUSES:
            try:
                from apps.intelligence.tasks_polling import fetch_filing_remarks
                fetch_filing_remarks.delay(str(record.id), tenant_id, agency)
                logger.info(
                    "FilingSyncer: dispatched fetch_filing_remarks for filing_id=%s status=%s",
                    filing_id, status,
                )
            except Exception as exc:
                logger.warning(
                    "FilingSyncer: failed to dispatch fetch_filing_remarks for filing_id=%s: %s",
                    filing_id, exc,
                )

        return "created"

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(raw: str | None) -> "date | None":
        """
        Parse an ISO-8601 date string (``YYYY-MM-DD``) into a Python
        ``datetime.date`` object.

        The scrapers already normalise dates to ISO format; this method
        simply converts the string.  Returns ``None`` for blank or
        unparseable inputs without raising.
        """
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except (ValueError, TypeError):
            logger.debug("FilingSyncer._parse_date: could not parse date '%s'", raw)
            return None
