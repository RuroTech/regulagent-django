"""
RRC (Texas Railroad Commission) portal scraper.

Implements BasePortalScraper for the RRC Well Plugging portal at
webapps.rrc.texas.gov/DW3P/. The W3A filings table lives inside a
cross-origin KnowledgeMill SPA served from kmprodwebapp.rrc.texas.gov
and embedded via an <iframe id="receiver">. All DOM interactions inside
the app must use Playwright's frame_locator API — direct page.query_selector()
calls will find nothing.

Auth flow:
  1. Login at /security/login.do (JS credential injection + form submit).
  2. Verify success via nav dropdown (select[name="go"]).
  3. Navigate to /DW3P/?app=w3a and wait for the iframe SPA to boot.
  4. Return the authenticated Page already positioned on go.jsp.

Scraping flow:
  1. Access iframe via page.frame_locator('iframe#receiver').
  2. Wait for the "My W3As" table to render.
  3. Parse all rows by column index (no pagination needed).
  4. For "Returned for Rework" filings, click into each detail page to
     capture the red/orange return-reason banner.
"""

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from django.utils import timezone
from playwright.async_api import BrowserContext, Page

from apps.intelligence.services.portal_scrapers.base import BasePortalScraper
from apps.intelligence.services.portal_scrapers.exceptions import (
    CredentialLockedError,
    InvalidCredentialsError,
)

if TYPE_CHECKING:
    from apps.intelligence.models import PortalCredential

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

RRC_LOGIN_URL = "https://webapps.rrc.texas.gov/security/login.do"
RRC_WELL_PLUGGING_URL = "https://webapps.rrc.texas.gov/DW3P/?app=w3a"
RRC_BASE_URL = "https://webapps.rrc.texas.gov"

# Column indices in the "My W3As" table (0-based).
# Confirmed by scrolling the full table width on the live portal.
COL_STAGE = 0
COL_API_NUMBER = 1
COL_DISTRICT = 2
COL_OPERATOR = 3
COL_OPERATOR_NUMBER = 4
COL_LEASE_NAME = 5
COL_LEASE_OR_GAS_ID = 6
COL_WELL_NUMBER = 7
COL_COUNTY = 8
COL_SUBMITTER = 9
COL_TRACKING_NUMBER = 10
COL_LAST_MODIFIED = 11
COL_DRILLING_PERMIT = 12
COL_RULE37_CASE = 13

# Maps portal "Stage" values (lowercased) to internal FilingStatusRecord keys.
RRC_STATUS_MAP = {
    "district approved": "approved",
    "returned for rework": "revision_requested",
    "deficiency notice": "deficiency",
    "revision requested": "revision_requested",
    "under review": "under_review",
    "pending": "pending",
    "approved": "approved",
    "rejected": "rejected",
    "deficiency": "deficiency",
}

# Maps RRC form-type label strings (lowercased) to internal FORM_TYPE_CHOICES keys.
RRC_FORM_TYPE_MAP = {
    "w-3": "w3",
    "w3": "w3",
    "w-3a": "w3a",
    "w3a": "w3a",
}

# Keywords (lowercase) that indicate the portal has locked the account rather
# than a simple bad-password rejection.  Any of these in the page body text
# means the user must visit the RRC portal to unlock before retrying.
_LOCKOUT_KEYWORDS = ("locked", "reset your password", "maximum number", "too many")


def _classify_login_failure(body_text: str) -> str:
    """
    Classify a portal login failure as 'locked' or 'invalid'.

    Inspects the page body text returned after a failed login attempt to
    determine whether the RRC portal has locked the account (too many
    failed attempts / reset-password required) or whether this is a
    simple bad-credentials rejection.

    Parameters
    ----------
    body_text:
        Raw text content of the login page body after a failed attempt.
        May be empty or whitespace-only.

    Returns
    -------
    str
        ``'locked'`` when any lockout keyword is found (case-insensitive).
        ``'invalid'`` for all other cases (wrong password, empty text, etc.).
    """
    lower = body_text.lower()
    for keyword in _LOCKOUT_KEYWORDS:
        if keyword in lower:
            return "locked"
    return "invalid"


class RRCPortalScraper(BasePortalScraper):
    """
    Concrete portal scraper for the Texas Railroad Commission (RRC).

    Covers the RRC W3A Well Plugging portal at webapps.rrc.texas.gov/DW3P/.
    The filings table lives inside a cross-origin iframe (kmprodwebapp.rrc.texas.gov)
    and must be accessed via Playwright's frame_locator API.

    Usage
    -----
    scraper = RRCPortalScraper()
    page    = await scraper.authenticate(credential, context)
    filings = await scraper.scrape_filings_list(page)
    status  = await scraper.check_filing_status(page, filing_id)
    """

    agency = "RRC"

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(
        self,
        credential: "PortalCredential",
        context: BrowserContext,
    ) -> Page:
        """
        Log into the RRC portal and navigate to the Well Plugging app.

        Uses JavaScript value injection and synthetic input events to satisfy
        the portal's dynamic form validation before submitting. Verifies success
        by checking for the post-login nav dropdown (select[name="go"]).

        After successful login, navigates to /DW3P/?app=w3a and waits for the
        KnowledgeMill iframe SPA to boot. Returns the Page already positioned
        on go.jsp with the iframe loaded.

        Raises RuntimeError if authentication fails.
        """
        # Decrypt credentials via executor so we don't block the event loop.
        username = await asyncio.get_event_loop().run_in_executor(
            None, credential.get_username
        )
        password = await asyncio.get_event_loop().run_in_executor(
            None, credential.get_password
        )

        page = await context.new_page()
        await page.goto(RRC_LOGIN_URL)
        await page.wait_for_load_state("networkidle")

        logger.info("RRC portal: starting authentication for credential id=%s", credential.id)

        # Replicate the exact working login flow from the legacy RegulAgent
        # test_scripts/rrc_w3a_phase6_comprehensive.py — JS injection in two
        # separate evaluate calls, then specific submit button selector.
        await page.wait_for_timeout(2_000)

        await page.evaluate(
            f"""
            document.querySelector('input[name="login"]').value = '{username}';
            document.querySelector('input[name="password"]').value = '{password}';
            """
        )
        await page.evaluate(
            """
            document.querySelector('input[name="login"]').dispatchEvent(new Event('input', { bubbles: true }));
            document.querySelector('input[name="password"]').dispatchEvent(new Event('input', { bubbles: true }));
            """
        )

        await page.click('input[type="submit"][value="Submit"]')
        await page.wait_for_load_state("networkidle", timeout=10_000)
        await page.wait_for_timeout(3_000)

        # Verify: after successful login the page redirects away from login.do
        current_url = page.url
        logger.info("RRC portal: post-login URL=%s", current_url)

        if "login.do" in current_url:
            body_text = (await page.inner_text('body'))[:300]
            kind = _classify_login_failure(body_text)
            msg = (
                f"RRC portal authentication failed for credential id={credential.id}. "
                f"Still on login page."
            )
            if kind == "locked":
                raise CredentialLockedError(msg)
            raise InvalidCredentialsError(msg)

        logger.info("RRC portal: authentication successful for credential id=%s", credential.id)

        # Persist last_successful_login without blocking the event loop.
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: type(credential).objects.filter(pk=credential.pk).update(
                last_successful_login=timezone.now()
            ),
        )

        # Navigate to Well Plugging via the nav dropdown — exact legacy flow.
        logger.info("RRC portal: navigating to Well Plugging via nav dropdown")
        await page.select_option('select[name="go"]', "/DW3P/?app=w3a")
        await page.click('input[value="Go"]')
        await page.wait_for_load_state("networkidle", timeout=20_000)

        # Wait for the KnowledgeMill SPA iframe to fully boot and render its UI.
        # The outer page title will read "Login Success" — this is expected.
        await page.wait_for_timeout(5_000)

        logger.info("RRC portal: positioned on DW3P go.jsp, iframe SPA should be booting")
        return page

    # ------------------------------------------------------------------
    # Filing list enumeration
    # ------------------------------------------------------------------

    async def scrape_filings_list(self, page: Page) -> list[dict]:
        """
        Scrape all W3A filings from the "My W3As" table inside the DW3P iframe.

        The page must already be authenticated and on /DW3P/go.jsp (done by
        authenticate()). The filings table lives inside a cross-origin iframe
        (kmprodwebapp.rrc.texas.gov) — all DOM access goes through frame_locator.

        No pagination is needed: the table loads all visible rows at once.
        For "Returned for Rework" filings, this method also clicks into the
        detail page to capture the red/orange return-reason banner.

        Returns a list of filing dicts conforming to BasePortalScraper contract.
        """
        filings: list[dict] = []

        try:
            # Access the iframe using the legacy proven approach:
            # query_selector + content_frame(), NOT frame_locator().
            logger.info("RRC portal: outer page URL=%s", page.url)

            iframe_el = await page.query_selector('#receiver')
            if not iframe_el:
                # Try waiting a bit more — SPA may still be loading
                await page.wait_for_timeout(5_000)
                iframe_el = await page.query_selector('#receiver')

            if not iframe_el:
                all_iframes = await page.query_selector_all('iframe')
                logger.error(
                    "RRC portal: iframe#receiver NOT found. Total iframes: %d", len(all_iframes)
                )
                for idx, f in enumerate(all_iframes):
                    f_id = await f.get_attribute('id') or ''
                    f_src = await f.get_attribute('src') or ''
                    logger.error("RRC portal: iframe[%d] id=%s src=%s", idx, f_id, f_src)
                return filings

            iframe = await iframe_el.content_frame()
            await iframe.wait_for_load_state("networkidle", timeout=15_000)
            # Extra wait for KnowledgeMill SPA to fully render
            await page.wait_for_timeout(5_000)

            logger.info("RRC portal: iframe loaded, opening W3A solution")

            # The KnowledgeMill SPA lands on a "Solutions" view. We need to
            # open the W3A solution to see the filings table. This matches
            # the legacy flow (rrc_w3a_phase6_comprehensive.py lines 100-107).
            try:
                # Click the W3A settings dropdown
                settings_btn = await iframe.wait_for_selector(
                    '.solution-list-item:has-text("W3A") .dropdown-toggle',
                    timeout=15_000,
                )
                await settings_btn.click()
                await page.wait_for_timeout(1_000)

                # Click "Open" in the dropdown
                open_link = await iframe.query_selector(
                    '.solution-list-item:has-text("W3A") a[role="menuitem"]:has-text("Open")'
                )
                if open_link:
                    await open_link.click()
                    await iframe.wait_for_load_state("networkidle", timeout=15_000)
                    await page.wait_for_timeout(8_000)
                    logger.info("RRC portal: W3A solution opened, waiting for table")
                else:
                    logger.warning("RRC portal: 'Open' link not found in W3A dropdown")
            except Exception as open_exc:
                logger.warning("RRC portal: could not open W3A solution: %s", open_exc)

            # Wait for the filings grid/table to appear.
            # KnowledgeMill SPA may use <table>, div[role="grid"], or a custom
            # grid class — try multiple selectors.
            grid_selectors = [
                'table',
                '[role="grid"]',
                '.react-grid-Grid',
                '.data-grid',
                '[class*="grid"]',
                '[class*="Grid"]',
                '[class*="table"]',
                '[class*="Table"]',
                '[class*="list-view"]',
                '[class*="ListView"]',
            ]

            grid_found = False
            for sel in grid_selectors:
                try:
                    await iframe.wait_for_selector(sel, timeout=5_000)
                    logger.info("RRC portal: found grid/table element with selector '%s'", sel)
                    grid_found = True
                    break
                except Exception:
                    continue

            if not grid_found:
                # Debug: dump the full iframe HTML structure to find the right selector
                try:
                    body_html = await iframe.inner_html('body')
                    # Log first 2000 chars of HTML for debugging
                    logger.error(
                        "RRC portal: no grid/table found. Iframe HTML (first 2000 chars): %s",
                        body_html[:2000],
                    )
                except Exception:
                    try:
                        body_text = await iframe.inner_text('body')
                        logger.error(
                            "RRC portal: no grid/table found. Iframe body text (first 1000 chars): %s",
                            body_text[:1000],
                        )
                    except Exception:
                        logger.error("RRC portal: could not read iframe content at all")
                return filings

            logger.info("RRC portal: W3A filings grid loaded")

            # Debug: count ALL row elements in DOM and check total data size
            all_rows_debug = await iframe.query_selector_all('.react-grid-Row')
            logger.info("RRC portal: total .react-grid-Row elements in DOM: %d", len(all_rows_debug))

            # Check the grid's total row count via the scrollHeight / row height
            canvas = await iframe.query_selector('.react-grid-Canvas')
            if canvas:
                dims = await canvas.evaluate('''el => ({
                    scrollHeight: el.scrollHeight,
                    clientHeight: el.clientHeight,
                    children: el.children.length,
                    firstChildTag: el.children[0] ? el.children[0].className : "none",
                    rowCount: el.querySelectorAll("[class*='react-grid-Row']").length,
                })''')
                logger.info("RRC portal: canvas dims: %s", dims)

            logger.info("RRC portal: using default 'My W3As' view")

            # React Data Grid virtualizes rendering — only rows visible in
            # the viewport exist in the DOM. We must scroll the grid container
            # incrementally, collecting row data at each position, until no
            # new rows appear.
            #
            # We also filter to filings from the past 18 months.
            from datetime import datetime, timedelta
            cutoff_date = datetime.now() - timedelta(days=548)  # ~18 months
            logger.info("RRC portal: filtering to filings after %s", cutoff_date.strftime("%Y-%m-%d"))

            seen_tracking_numbers: set[str] = set()
            scroll_attempts = 0
            max_scroll_attempts = 50  # Safety limit

            # Find the scrollable grid container.
            # In react-data-grid, the Canvas is the scrollable viewport,
            # NOT the outer Grid wrapper.
            grid_container = await iframe.query_selector('.react-grid-Canvas')
            if not grid_container:
                grid_container = await iframe.query_selector('.react-grid-Grid')
            logger.info("RRC portal: scroll container found: %s",
                         await grid_container.evaluate('el => el.className') if grid_container else "NONE")

            while scroll_attempts < max_scroll_attempts:
                # Grab currently rendered rows
                rows = await iframe.query_selector_all('.react-grid-Row')
                if not rows:
                    rows = await iframe.query_selector_all('table tr')
                    if rows:
                        rows = rows[1:]

                new_rows_found = 0

                # Log what we're about to parse on this pass
                if scroll_attempts > 0:
                    logger.info("RRC portal: pass %d — %d rows in DOM, %d already seen",
                                scroll_attempts + 1, len(rows), len(seen_tracking_numbers))

                for row in rows:
                    try:
                        cell_elements = await row.query_selector_all('.react-grid-Cell')
                        if not cell_elements:
                            cell_elements = await row.query_selector_all('td')
                        cells = [await c.inner_text() for c in cell_elements]

                        if len(cells) < 11:
                            continue

                        raw_tracking = cells[COL_TRACKING_NUMBER].strip() if len(cells) > COL_TRACKING_NUMBER else ""
                        if not raw_tracking or raw_tracking in seen_tracking_numbers:
                            continue

                        raw_date = cells[COL_LAST_MODIFIED].strip() if len(cells) > COL_LAST_MODIFIED else ""
                        status_date = self._parse_portal_date(raw_date)

                        # 18-month filter
                        if status_date:
                            try:
                                from datetime import date as _date
                                filing_date = _date.fromisoformat(status_date)
                                if filing_date < cutoff_date.date():
                                    continue
                            except (ValueError, TypeError):
                                pass

                        seen_tracking_numbers.add(raw_tracking)
                        new_rows_found += 1

                        raw_stage        = cells[COL_STAGE].strip()            if len(cells) > COL_STAGE            else ""
                        raw_api          = cells[COL_API_NUMBER].strip()       if len(cells) > COL_API_NUMBER        else ""
                        raw_district     = cells[COL_DISTRICT].strip()         if len(cells) > COL_DISTRICT           else ""
                        raw_operator     = cells[COL_OPERATOR].strip()         if len(cells) > COL_OPERATOR           else ""
                        raw_operator_num = cells[COL_OPERATOR_NUMBER].strip()  if len(cells) > COL_OPERATOR_NUMBER    else ""
                        raw_lease        = cells[COL_LEASE_NAME].strip()       if len(cells) > COL_LEASE_NAME         else ""
                        raw_lease_id     = cells[COL_LEASE_OR_GAS_ID].strip()  if len(cells) > COL_LEASE_OR_GAS_ID    else ""
                        raw_well_num     = cells[COL_WELL_NUMBER].strip()      if len(cells) > COL_WELL_NUMBER         else ""
                        raw_county       = cells[COL_COUNTY].strip()           if len(cells) > COL_COUNTY              else ""
                        raw_submitter    = cells[COL_SUBMITTER].strip()        if len(cells) > COL_SUBMITTER           else ""
                        raw_permit       = cells[COL_DRILLING_PERMIT].strip()  if len(cells) > COL_DRILLING_PERMIT     else ""
                        raw_rule37       = cells[COL_RULE37_CASE].strip()      if len(cells) > COL_RULE37_CASE         else ""

                        filings.append({
                            "filing_id": raw_tracking,
                            "form_type": "w3a",
                            "status": self._parse_status(raw_stage),
                            "portal_url": "",
                            "status_date": status_date,
                            "remarks": "",
                            "reviewer_name": raw_submitter,
                            "well_api": raw_api,
                            "raw_data": {
                                "stage": raw_stage,
                                "api_number": raw_api,
                                "district": raw_district,
                                "operator": raw_operator,
                                "operator_number": raw_operator_num,
                                "lease_name": raw_lease,
                                "lease_or_gas_id": raw_lease_id,
                                "well_number": raw_well_num,
                                "county": raw_county,
                                "submitter": raw_submitter,
                                "tracking_number": raw_tracking,
                                "last_modified": raw_date,
                                "drilling_permit": raw_permit,
                                "rule_37_case": raw_rule37,
                            },
                        })

                    except Exception as exc:
                        logger.warning("RRC portal: error parsing row: %s", exc)
                        continue

                # End of row loop for this scroll position

                if new_rows_found == 0:
                    # No new rows found at this scroll position — we've reached the end
                    break

                # Scroll down to render more virtualized rows
                scroll_attempts += 1
                logger.info(
                    "RRC portal: scroll %d — %d new rows this pass, %d total so far",
                    scroll_attempts, new_rows_found, len(filings),
                )

                if grid_container:
                    # Dispatch scroll event after setting scrollTop, so
                    # React's virtualizer re-renders rows.
                    scroll_info = await grid_container.evaluate('''el => {
                        const before = el.scrollTop;
                        el.scrollTop += el.clientHeight;
                        el.dispatchEvent(new Event('scroll', { bubbles: true }));
                        return {
                            before: before,
                            after: el.scrollTop,
                            scrollHeight: el.scrollHeight,
                            clientHeight: el.clientHeight
                        };
                    }''')
                    logger.info(
                        "RRC portal: scroll — before=%s after=%s total=%s",
                        scroll_info.get('before'), scroll_info.get('after'),
                        scroll_info.get('scrollHeight'),
                    )
                else:
                    await iframe.evaluate('''
                        const el = document.querySelector(".react-grid-Canvas");
                        el.scrollTop += 500;
                        el.dispatchEvent(new Event('scroll', { bubbles: true }));
                    ''')

                # Wait for React to re-render
                await page.wait_for_timeout(2_000)

            logger.info(
                "RRC portal: scrolling complete — %d unique filings collected (%d scroll passes)",
                len(filings), scroll_attempts,
            )

            # Remarks are NOT fetched during the sync — it takes too long and
            # causes Celery timeouts. Instead, the FilingSyncer dispatches a
            # separate fetch_filing_remarks task for each returned filing after
            # upserting. This keeps the sync fast and reliable.
            returned_count = sum(1 for f in filings if f["status"] == "revision_requested")
            if returned_count:
                logger.info("RRC portal: %d returned filings — remarks will be fetched separately", returned_count)

            logger.info("RRC portal: scrape_filings_list complete — %d filing(s) total", len(filings))

        except Exception as exc:
            logger.error("RRC portal: scrape_filings_list failed: %s", exc)
            # Return whatever we have so far rather than raising.

        return filings

    # ------------------------------------------------------------------
    # Single-filing remarks scraping
    # ------------------------------------------------------------------

    async def scrape_single_filing_remarks(
        self,
        page: Page,
        tracking_number: str,
        api_number: str = "",
    ) -> str:
        """
        Fetch the return reason for a single filing.

        Assumes the page is already authenticated and on the DW3P app.
        Uses the search box to find the filing (tries API number first,
        then tracking number), clicks into detail, scrapes the banner,
        and navigates back.

        Returns the remarks text, or empty string if not found.
        """
        iframe_el = await page.query_selector('#receiver')
        if not iframe_el:
            logger.warning("RRC portal: iframe#receiver not found for remarks scrape")
            return ""
        iframe = await iframe_el.content_frame()
        await iframe.wait_for_load_state("networkidle", timeout=15_000)
        await page.wait_for_timeout(3_000)

        # Open W3A solution if we're on the Solutions landing page
        try:
            settings_btn = await iframe.wait_for_selector(
                '.solution-list-item:has-text("W3A") .dropdown-toggle',
                timeout=5_000,
            )
            if settings_btn:
                await settings_btn.click()
                await page.wait_for_timeout(1_000)
                open_link = await iframe.query_selector(
                    '.solution-list-item:has-text("W3A") a[role="menuitem"]:has-text("Open")'
                )
                if open_link:
                    await open_link.click()
                    await iframe.wait_for_load_state("networkidle", timeout=15_000)
                    await page.wait_for_timeout(5_000)
        except Exception:
            pass  # Already on W3A grid, no solutions page

        try:
            # Find the search input
            search_input = None
            for sel in ['input[type="search"]', 'input[placeholder*="Search" i]', 'input[placeholder*="search" i]']:
                search_input = await iframe.query_selector(sel)
                if search_input:
                    break
            if not search_input:
                all_inputs = await iframe.query_selector_all('input[type="text"]')
                for inp in all_inputs:
                    ph = await inp.get_attribute('placeholder') or ''
                    if 'search' in ph.lower():
                        search_input = inp
                        break

            if not search_input:
                logger.warning("RRC portal: search input not found for remarks")
                try:
                    await page.screenshot(path=f"/app/debug_remarks_{tracking_number}.png")
                    logger.info("RRC portal: saved debug screenshot for %s", tracking_number)
                except Exception:
                    pass
                return ""

            # Try API number first (search box likely filters by visible columns
            # like API Number, not Tracking Number).
            search_terms = []
            if api_number:
                search_terms.append(api_number)
            search_terms.append(tracking_number)

            rows = []
            for term in search_terms:
                await search_input.click()
                await search_input.fill('')
                await page.wait_for_timeout(500)
                await search_input.fill(term)
                await page.wait_for_timeout(2_000)
                rows = await iframe.query_selector_all('.react-grid-Row')
                if rows:
                    logger.info("RRC portal: search '%s' returned %d rows", term, len(rows))
                    break

            if not rows:
                logger.warning("RRC portal: no rows after search for %s (api=%s)", tracking_number, api_number)
                await search_input.fill('')
                await page.wait_for_timeout(1_000)
                return ""

            # Find the correct row — must be "Returned for Rework" stage.
            # Multiple filings can exist for the same API number.
            target_row = None
            for r in rows:
                try:
                    cell_els = await r.query_selector_all('.react-grid-Cell')
                    if cell_els and len(cell_els) > COL_STAGE:
                        stage_text = (await cell_els[COL_STAGE].inner_text()).strip().lower()
                        if 'returned' in stage_text or 'rework' in stage_text:
                            target_row = r
                            break
                except Exception:
                    continue

            if not target_row:
                logger.warning("RRC portal: no 'Returned for Rework' row found for %s", tracking_number)
                await search_input.fill('')
                await page.wait_for_timeout(1_000)
                return ""

            await target_row.click()
            await page.wait_for_timeout(3_000)

            # Screenshot the detail page for debugging
            try:
                await page.screenshot(path=f"/app/debug_detail_{tracking_number}.png")
                logger.info("RRC portal: saved detail screenshot for %s", tracking_number)
            except Exception:
                pass

            # Debug: dump the first few elements at the top of the detail page
            # to find the correct selector for the return reason banner
            try:
                top_els = await iframe.query_selector_all('div, p, span')
                for idx, el in enumerate(top_els[:20]):
                    cls = await el.evaluate('el => el.className') or ''
                    text = (await el.inner_text()).strip()[:80]
                    if text and ('correct' in text.lower() or 'submit' in text.lower() or 'return' in text.lower() or 'rework' in text.lower()):
                        tag = await el.evaluate('el => el.tagName')
                        style = await el.evaluate('el => el.getAttribute("style")') or ''
                        logger.info("RRC portal: potential banner el <%s class='%s' style='%s'> text='%s'", tag, cls[:60], style[:60], text)
            except Exception:
                pass

            # Look for the return reason banner
            remarks = ""
            for bsel in ['[class*="alert"]', '[class*="notification"]', '[class*="banner"]',
                         '[class*="rework"]', '[class*="warning"]', '[role="alert"]',
                         '[class*="error"]', '[class*="return"]', '[class*="reject"]',
                         '[class*="message"]', '[class*="info"]',
                         'div[style*="background"]', 'div[style*="red"]', 'div[style*="color"]']:
                try:
                    banner = await iframe.query_selector(bsel)
                    if banner:
                        text = (await banner.inner_text()).strip()
                        if text and len(text) > 3:
                            remarks = text
                            break
                except Exception:
                    continue

            if not remarks:
                # The banner uses class 'container-fluid no-padding' — try that directly
                try:
                    banner = await iframe.query_selector('.container-fluid.no-padding')
                    if banner:
                        text = (await banner.inner_text()).strip()
                        # Filter out navigation/header text
                        if text and len(text) > 5 and len(text) < 500:
                            remarks = text
                            logger.info("RRC portal: remarks (container-fluid) for %s: %s", tracking_number, text[:100])
                except Exception:
                    pass

            if not remarks:
                # Fallback: scan body text for return-reason phrases
                try:
                    body_text = await iframe.inner_text('body')
                    for phrase in ['Submit a', 'Please provide', 'Missing', 'Required', 'Incorrect',
                                   'Need', 'Correct', 'Current operator', 'operator of well',
                                   'add all', 'operator info', 'lease #']:
                        idx = body_text.find(phrase)
                        if idx >= 0:
                            remarks = body_text[idx:idx + 300].split('\n')[0].strip()
                            if remarks:
                                logger.info("RRC portal: remarks (text scan) for %s: %s", tracking_number, remarks[:100])
                                break
                except Exception:
                    pass

            # Navigate back and clear search
            for bk in ['a:has-text("Home")', '[aria-label="Home"]', '.breadcrumb a']:
                try:
                    btn = await iframe.query_selector(bk)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(2_000)
                        break
                except Exception:
                    continue

            try:
                search_input = await iframe.query_selector('input[type="search"], input[placeholder*="Search" i]')
                if search_input:
                    await search_input.fill('')
                    await page.wait_for_timeout(1_000)
            except Exception:
                pass

            if remarks:
                logger.info("RRC portal: remarks for %s: %s", tracking_number, remarks[:100])
            return remarks

        except Exception as exc:
            logger.warning("RRC portal: scrape_single_filing_remarks failed for %s: %s", tracking_number, exc)
            return ""

    # ------------------------------------------------------------------
    # Single-filing status check
    # ------------------------------------------------------------------

    async def check_filing_status(self, page: Page, filing_id: str) -> dict:
        """
        Check status of a specific filing by its tracking number.

        Since the DW3P app is an SPA with no per-filing URLs, navigates to the
        W3A filings list (if not already there) and finds the matching row by
        tracking number.

        Returns a dict with keys: new_status, remarks, reviewer_name,
        status_date, raw_data.
        """
        logger.debug("RRC portal: checking status for filing_id=%s", filing_id)

        # Ensure we're on the DW3P page.
        if "/DW3P/" not in page.url:
            logger.info("RRC portal: not on DW3P — navigating to %s", RRC_WELL_PLUGGING_URL)
            await page.goto(RRC_WELL_PLUGGING_URL)
            await page.wait_for_load_state("networkidle", timeout=20_000)
            await page.wait_for_timeout(5_000)

        iframe_el = await page.query_selector('#receiver')
        if not iframe_el:
            return {"new_status": "under_review", "remarks": "", "reviewer_name": "",
                    "status_date": None, "raw_data": {"error": "iframe#receiver not found"}}
        iframe = await iframe_el.content_frame()
        await iframe.wait_for_selector('table', timeout=20_000)

        rows = await iframe.query_selector_all('.react-grid-Row')
        if not rows:
            rows = await iframe.query_selector_all('table tr')
            if rows:
                rows = rows[1:]  # skip header for native tables

        for row in rows:
            try:
                cell_els = await row.query_selector_all('.react-grid-Cell')
                if not cell_els:
                    cell_els = await row.query_selector_all('td')
                cells = [await c.inner_text() for c in cell_els]
            except Exception:
                continue

            if (
                len(cells) > COL_TRACKING_NUMBER
                and cells[COL_TRACKING_NUMBER].strip() == filing_id
            ):
                raw_stage     = cells[COL_STAGE].strip()          if len(cells) > COL_STAGE          else ""
                raw_date      = cells[COL_LAST_MODIFIED].strip()  if len(cells) > COL_LAST_MODIFIED  else ""
                raw_submitter = cells[COL_SUBMITTER].strip()      if len(cells) > COL_SUBMITTER      else ""

                logger.debug(
                    "RRC portal: found filing %s (stage=%s)", filing_id, raw_stage
                )

                return {
                    "new_status": self._parse_status(raw_stage),
                    "remarks": "",
                    "reviewer_name": raw_submitter,
                    "status_date": self._parse_portal_date(raw_date),
                    "raw_data": {
                        "stage": raw_stage,
                        "cells": [c.strip() for c in cells],
                    },
                }

        logger.warning("RRC portal: filing %s not found in W3A table", filing_id)
        return {
            "new_status": "under_review",
            "remarks": "",
            "reviewer_name": "",
            "status_date": None,
            "raw_data": {"error": f"Filing {filing_id} not found in W3A table"},
        }

    # ------------------------------------------------------------------
    # Static parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_status(raw_stage: str) -> str:
        """
        Map portal 'Stage' value to internal FilingStatusRecord status key.

        Falls back to 'under_review' when the stage is blank or unrecognised.
        """
        if not raw_stage:
            return "under_review"
        return RRC_STATUS_MAP.get(raw_stage.strip().lower(), "under_review")

    @staticmethod
    def _parse_portal_date(raw: str) -> str | None:
        """
        Parse RRC portal date strings to ISO 'YYYY-MM-DD'.

        The portal's primary format is 'February 25 2026 10:29:53'.
        Also handles abbreviated month names, dates without a time component,
        and legacy 'MM/DD/YYYY' or 'YYYY-MM-DD' formats as fallbacks.

        Returns None when the input is blank or unparseable.
        """
        if not raw or not raw.strip():
            return None

        raw = raw.strip()

        formats = [
            "%B %d %Y %H:%M:%S",  # February 25 2026 10:29:53
            "%B %d %Y",            # February 25 2026
            "%b %d %Y %H:%M:%S",  # Feb 25 2026 10:29:53
            "%b %d %Y",            # Feb 25 2026
            "%m/%d/%Y",            # 03/15/2026
            "%Y-%m-%d",            # 2026-03-15
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

        logger.debug("RRC portal: could not parse date '%s'", raw)
        return None
