"""RRC form automation implementation."""

import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from playwright.async_api import BrowserContext, Page
import logging

from ...base.form_automator import BaseFormAutomator
from ...base.data_models import (
    FormData, AuthData, AutomationResult, WorkflowStep,
    TabConfig, SelectorConfig, GISData
)
from ...exceptions import FormSubmissionError, AuthenticationError
from ...base.step_by_step_controller import StepByStepMixin
from .rrc_config import RRC_SELECTORS, RRC_FORM_CONFIGS, RRC_DEFAULTS, RRC_TAB_CONFIGS
from .rrc_gis_extractor import RRCGISExtractor
from apps.intelligence.services.customer_documents import get_gau_letter


logger = logging.getLogger(__name__)


def build_cementing_text(name: str, address: str = "", p5: str = "") -> str:
    """Compose the W-3A cementing textarea value from its three components.

    Rules:
    - Lines are joined by ``\\n`` (no trailing newline).
    - The P-5 number is prefixed with ``"P-5: "``.
    - Absent / blank components are omitted — only ``name`` is guaranteed.

    Examples::

        build_cementing_text("Acme Inc.")
        # → 'Acme Inc.'

        build_cementing_text("Acme Inc.", "PO Box 1", "040196")
        # → 'Acme Inc.\\nPO Box 1\\nP-5: 040196'

        build_cementing_text("Acme Inc.", "", "040196")
        # → 'Acme Inc.\\nP-5: 040196'
    """
    parts = [name]
    if address:
        parts.append(address)
    if p5:
        parts.append(f"P-5: {p5}")
    return "\n".join(parts)


class RRCFormAutomator(StepByStepMixin, BaseFormAutomator):
    """Texas Railroad Commission form automation implementation."""
    
    def __init__(self, context: BrowserContext, session_id: str):
        super().__init__(context, session_id)
        
        # RRC-specific configuration
        self.agency_config = {
            "name": "Texas Railroad Commission",
            "code": "RRC",
            "base_url": "https://webapps.rrc.texas.gov"
        }
        
        # Load RRC form selectors
        self.form_selectors = RRC_SELECTORS
        
        # Initialize GIS extractor for multi-tab workflows
        self.gis_extractor: Optional[RRCGISExtractor] = None
        
    # Abstract method implementations
    
    async def authenticate(self, auth_data: AuthData, multi_tab: bool = False) -> bool:
        """Authenticate with RRC system."""
        
        try:
            # Get the RRC form tab (should be loaded already if multi-tab)
            if multi_tab:
                page = await self.tab_manager.switch_to_tab("rrc_form")
            else:
                page = self.context.pages[0]
                await page.goto("https://webapps.rrc.texas.gov/security/login.do")
                await page.wait_for_load_state('networkidle')
            
            logger.info("Starting RRC authentication")
            
            # Fill credentials using JavaScript (mimicking original approach)
            await page.evaluate(f"""
                document.querySelector('input[name="login"]').value = '{auth_data.username}';
                document.querySelector('input[name="password"]').value = '{auth_data.password}';
            """)
            
            # Trigger input events for dynamic forms
            await page.evaluate("""
                document.querySelector('input[name="login"]').dispatchEvent(new Event('input', { bubbles: true }));
                document.querySelector('input[name="password"]').dispatchEvent(new Event('input', { bubbles: true }));
            """)
            
            # Submit login form
            submit_config = self.form_selectors["login_submit"]
            await self.selector_engine.smart_click(page, submit_config)
            
            # Wait for login to complete
            await page.wait_for_load_state('networkidle', timeout=10000)
            await asyncio.sleep(3)
            
            # Verify login success (check for common post-login elements)
            try:
                # Look for navigation dropdown which indicates successful login
                nav_dropdown = await page.query_selector('select[name="go"]')
                if nav_dropdown:
                    logger.info("RRC authentication successful")
                    self.result.add_log_entry("INFO", "RRC authentication completed", step="authentication")
                    return True
                else:
                    raise AuthenticationError("Login verification failed - navigation dropdown not found")
            except Exception:
                # Check for error messages
                error_indicators = await page.query_selector('.error, .alert-danger, [class*="error"]')
                if error_indicators:
                    error_text = await error_indicators.inner_text()
                    raise AuthenticationError(f"Login failed: {error_text}")
                else:
                    raise AuthenticationError("Login verification failed - unknown error")
            
        except Exception as e:
            if isinstance(e, AuthenticationError):
                raise
            raise AuthenticationError(f"RRC authentication error: {str(e)}", agency="RRC")
    
    async def navigate_to_form(self, form_type: str, multi_tab: bool = False) -> Page:
        """Navigate to RRC form interface."""
        
        try:
            # Get the appropriate page
            if multi_tab:
                page = await self.tab_manager.switch_to_tab("rrc_form")
            else:
                page = self.context.pages[0]
            
            logger.info(f"Navigating to RRC {form_type} form")
            
            if form_type.upper() == "W3A":
                await self._navigate_to_w3a(page)
            else:
                raise FormSubmissionError(f"Unsupported form type: {form_type}")
            
            self.result.add_log_entry("INFO", f"Successfully navigated to {form_type} form", step="navigation")
            return page
            
        except Exception as e:
            raise FormSubmissionError(f"Navigation to {form_type} failed: {str(e)}", form_type=form_type, step="navigation")
    
    async def _navigate_to_w3a(self, page: Page):
        """Navigate specifically to W-3A form interface."""
        
        # Navigate to Well Plugging
        nav_dropdown_config = self.form_selectors["nav_dropdown"]
        nav_dropdown = await self.selector_engine.find_element(page, nav_dropdown_config)
        await nav_dropdown.select_option("/DW3P/?app=w3a")
        
        go_button_config = self.form_selectors["nav_go_button"]
        await self.selector_engine.smart_click(page, go_button_config)
        await page.wait_for_load_state('networkidle')
        await asyncio.sleep(5)
        
        # Access W3A application iframe
        iframe_config = self.form_selectors["iframe_container"]
        iframe_element = await self.selector_engine.find_element(page, iframe_config)
        iframe = await iframe_element.content_frame()
        await iframe.wait_for_load_state('networkidle', timeout=15000)
        await asyncio.sleep(5)
        
        # Open W3A application
        settings_config = self.form_selectors["w3a_settings_button"]
        settings_button = await iframe.query_selector(settings_config.primary)
        if settings_button:
            await settings_button.click()
            await asyncio.sleep(1)
            
            open_config = self.form_selectors["w3a_open_link"]
            open_link = await iframe.query_selector(open_config.primary)
            if open_link:
                await open_link.click()
                await iframe.wait_for_load_state('networkidle', timeout=15000)
                await asyncio.sleep(8)
        
        # Create new W3A entry
        create_config = self.form_selectors["w3a_create_button"]
        create_button = await iframe.query_selector(create_config.primary)
        if create_button:
            await create_button.click()
            await iframe.wait_for_load_state('networkidle', timeout=15000)
            await asyncio.sleep(3)
        
        logger.info("W3A form interface accessed successfully")
    
    async def _step(self, section: str, action: str, fn):
        """Step-boundary hook for structured logging and optional Playwright Inspector pause.

        Wraps each ``_fill_*`` call inside ``fill_form_fields``.  The
        ``W3A_PAUSE_AT`` environment variable (dev-only) can be set to a
        section name; when the current section matches, ``page.pause()`` is
        awaited via the first available page so the Playwright Inspector
        freezes execution at that point.

        Log format (JSON-serialisable dict via logger.info)::

            {
                "event": "step_start" | "step_ok" | "step_error",
                "section": "<section>",
                "action": "<action>",
                "error": "<message>",   # only on step_error
            }
        """
        pause_at = os.environ.get("W3A_PAUSE_AT", "")
        logger.info({"event": "step_start", "section": section, "action": action})
        if pause_at and pause_at == section:
            try:
                page = self.context.pages[0]
                await page.pause()
            except Exception:
                pass  # Non-fatal — dev helper only
        try:
            result = await fn()
            logger.info({"event": "step_ok", "section": section, "action": action})
            return result
        except Exception as exc:
            logger.info({"event": "step_error", "section": section, "action": action, "error": str(exc)})
            raise

    async def fill_form_fields(self, form_data: FormData, multi_tab: bool = False) -> bool:
        """Fill W-3A form fields with provided data."""
        
        try:
            # Get the appropriate page and iframe
            if multi_tab:
                page = await self.tab_manager.switch_to_tab("rrc_form")
            else:
                page = self.context.pages[0]
            
            # Get iframe for W3A form
            iframe_element = await page.query_selector('#receiver')
            iframe = await iframe_element.content_frame()
            
            logger.info("🚀 Starting W3A form field completion")
            
            # Extract GIS data if multi-tab workflow AND District 7C
            gis_data = None
            if multi_tab:
                logger.info("📋 PHASE 1: Data Collection (Multi-tab mode)")
                
                # Step 1.1: Fill basic fields first to populate RRC District
                if not self.wait_for_step(
                    "Fill Basic Form Fields",
                    f"About to enter API number {form_data.api_number} and select lease to populate RRC District",
                    "Enter API number, select lease, and extract RRC District"
                ):
                    return
                
                logger.info("📝 Step 1.1: Filling basic fields to populate form...")
                await self._fill_basic_fields(iframe, form_data, None)
                self.log_step_action("Basic form fields filled", True, f"API {form_data.api_number} entered and lease selected")
                
                # Step 1.2: Extract RRC District from populated form
                if not self.wait_for_step(
                    "Extract RRC District",
                    "Form is now populated with basic information",
                    "Read the RRC District field to determine if GIS workflow is needed"
                ):
                    return
                
                logger.info("🔍 Step 1.2: Extracting RRC District from populated form...")
                rrc_district = await self._extract_rrc_district(iframe)
                self.log_step_action("RRC District extracted", True, f"District: {rrc_district}")
                
                # Step 1.3: Conditional GIS workflow
                if rrc_district == '7C':
                    if not self.wait_for_step(
                        "GIS Data Collection (District 7C)",
                        f"District {rrc_district} requires GIS surface location data",
                        "Open GIS Viewer, find well location, extract surface coordinates"
                    ):
                        return
                    
                    logger.info("🗺️  Step 1.3: District 7C detected - running GIS workflow...")
                    gis_data = await self._extract_gis_data(form_data.api_number)
                    if gis_data:
                        self.log_step_action("GIS data collection completed", True, f"Surface location: {gis_data.surface_location}")
                    else:
                        self.log_step_action("GIS data collection failed", False, "Could not extract surface location data")
                else:
                    if not self.wait_for_step(
                        "Skip GIS Workflow",
                        f"District {rrc_district} does not require GIS data collection",
                        "Continue to completions data collection"
                    ):
                        return
                    
                    logger.info(f"⏭️  Step 1.3: District {rrc_district} detected - skipping GIS workflow")
                    gis_data = None
                    self.log_step_action("GIS workflow skipped", True, f"Not required for District {rrc_district}")
                
                # Step 1.4: Collect completions data (always runs)
                if not self.wait_for_step(
                    "Collect Completions Data",
                    "About to search RRC Completions database for W-2, GAU Letter, and W-15 documents",
                    "Navigate to completions page, search by API, download latest documents"
                ):
                    return
                
                logger.info("📄 Step 1.4: Collecting RRC Completions data (W-2, GAU, W-15)...")
                completions_data = await self._collect_completions_data(form_data.api_number)
                if completions_data:
                    self.log_step_action("Completions data collection completed", True, f"{len(completions_data)} files downloaded")
                else:
                    self.log_step_action("Completions data collection failed", False, "Could not download completion documents")
            
            logger.info("📋 PHASE 2: Form Field Completion")
            
            # Step 2.1: Fill basic form fields (already done above if multi_tab, do it here for single tab)
            if not multi_tab:
                if not self.wait_for_step(
                    "Fill Basic Form Fields",
                    f"About to enter API number {form_data.api_number} and basic well information",
                    "Fill API number, lease selection, and basic form fields"
                ):
                    return
                
                logger.info("📝 Step 2.1: Filling basic form fields...")
                await self._step("basic_fields", "fill", lambda: self._fill_basic_fields(iframe, form_data, gis_data))
                self.log_step_action("Basic fields completed", True, "API and lease information entered")
            else:
                self.log_step_info("Step 2.1: Basic fields already completed in Phase 1")
            
            # Step 2.2: Fill location and well information
            if not self.wait_for_step(
                "Fill Location & Well Information",
                "About to fill location coordinates, well details, and operator information",
                "Enter surface location, well number, operator details, and field information"
            ):
                return
            
            logger.info("📍 Step 2.2: Filling location and well information...")
            await self._step("location_fields", "fill", lambda: self._fill_location_fields(iframe, form_data, gis_data))
            self.log_step_action("Location fields completed", True, "Surface location and well details entered")
            
            # Step 2.3: Configure well type and completion
            if not self.wait_for_step(
                "Configure Well Type & Completion",
                "About to set well type, completion method, and technical specifications",
                "Select well type, completion details, and technical parameters"
            ):
                return
            
            logger.info("🛢️  Step 2.3: Configuring well type and completion...")
            await self._step("well_type_fields", "fill", lambda: self._fill_well_type_fields(iframe, form_data, gis_data))
            self.log_step_action("Well type fields completed", True, "Well type and completion configured")

            # Step 2.3b: Casing Record (iframe-route grid — clear, then repopulate)
            logger.info("🧱 Step 2.3b: Filling Casing Record...")
            await self._step("casing_record", "fill", lambda: self._fill_casing_record(iframe, form_data))
            self.log_step_action("Casing record completed", True, "Casing rows filled")

            # Step 2.3c: Perforation Record (RECON STUB — dumps DOM and halts)
            logger.info("🔎 Step 2.3c: Perforation Record RECON...")
            await self._step("perforation_record", "fill", lambda: self._fill_perforation_record(iframe, form_data))
            self.log_step_action("Perforation record completed", True, "Perforation rows filled")

            # Step 2.3d: Plugging Proposal (RECON STUB — dumps DOM and halts)
            logger.info("🔎 Step 2.3d: Plugging Proposal RECON...")
            await self._step("plugging_proposal", "fill", lambda: self._fill_plugging_proposal(iframe, form_data))
            self.log_step_action("Plugging Proposal completed", True, "Plugging Proposal rows filled")

            # Step 2.4: Handle file attachments (GAU upload)
            if not self.wait_for_step(
                "Upload GAU Letter",
                "About to upload the downloaded GAU Letter document to the form",
                "Find GAU upload section and attach the downloaded GAU_LETTER file"
            ):
                return
            
            logger.info("📎 Step 2.4: Handling file attachments...")
            await self._step("file_attachments", "upload", lambda: self._handle_file_attachments(iframe, form_data.file_attachments or []))
            self.log_step_action("File attachments completed", True, "GAU Letter uploaded")
            
            # Step 2.5: Configure area review
            if not self.wait_for_step(
                "Configure Area Review",
                "About to configure area review sections with depth values based on RRC District",
                "Add area review entries with depth 0 for non-7C districts, or skip for 7C"
            ):
                return
            
            # DISABLED 2026-05-19: legacy _configure_area_review iterates all 5
            # itemizer-grids and pollutes Casing/Perforation/Plug with depth=0.
            # Times out at celery 300s hard limit. Rewrite tracked in task #8
            # (proper scoping to the 2 Area Review field-IDs). Until then,
            # Area Review stays unfilled and RRC may reject the draft.
            #
            # await self._configure_area_review(iframe)
            logger.info("⏭️  Step 2.5: Area Review SKIPPED (legacy implementation disabled — see task #8)")
            self.log_step_action("Area review skipped", True, "Disabled pending rewrite")
            
            # Fill contact and company information
            logger.info("🏢 Step 2.6: Filling contact and company information...")
            await self._step("contact_information", "fill", lambda: self._fill_contact_information(iframe, form_data))
            logger.info("✅ Contact information completed")

            # Handle agreement section
            logger.info("📋 Step 2.7: Handling agreement section...")
            await self._step("agreement", "handle", lambda: self._handle_agreement_section(iframe))
            logger.info("✅ Agreement section completed")
            
            self.result.add_log_entry("INFO", "W3A form fields completed successfully", step="form_filling")
            return True
            
        except Exception as e:
            error_msg = f"W3A form filling failed: {str(e)}"
            logger.error(error_msg)
            self.result.add_log_entry("ERROR", error_msg, step="form_filling")
            return False
    
    async def _extract_rrc_district(self, iframe: Page) -> str:
        """Extract RRC District from populated W-3A form."""
        try:
            # Try multiple selectors for RRC District
            district_selectors = [
                'input[name*="district" i]',
                'select[name*="district" i]',
                'input[id*="district" i]',
                'select[id*="district" i]'
            ]
            
            for selector in district_selectors:
                try:
                    elements = await iframe.locator(selector).all()
                    for element in elements:
                        if await element.is_visible():
                            value = await element.input_value()
                            if value and value.strip():
                                district = value.strip()
                                logger.info(f"Found RRC District using {selector}: {district}")
                                return district
                except:
                    continue
            
            # Fallback: search iframe text
            iframe_text = await iframe.text_content('body')
            import re
            district_patterns = [
                r'District\s*:?\s*([0-9A-Z]+)',
                r'RRC\s*District\s*:?\s*([0-9A-Z]+)'
            ]
            
            for pattern in district_patterns:
                match = re.search(pattern, iframe_text, re.IGNORECASE)
                if match:
                    district = match.group(1).strip()
                    logger.info(f"Found RRC District in text: {district}")
                    return district
            
            logger.warning("RRC District not found - defaulting to UNKNOWN")
            return "UNKNOWN"
            
        except Exception as e:
            logger.error(f"District extraction error: {e}")
            return "UNKNOWN"
    
    async def _extract_gis_data(self, api_number: str) -> Optional[GISData]:
        """Extract GIS data using dual-tab workflow."""
        
        try:
            if not self.gis_extractor:
                self.gis_extractor = RRCGISExtractor(self.result)
            
            gis_page = await self.tab_manager.switch_to_tab("gis_viewer")
            gis_data = await self.gis_extractor.extract_gis_data(gis_page, api_number)
            
            logger.info(f"GIS extraction completed: {gis_data.location_string}")
            self.result.gis_data = gis_data
            
            return gis_data
            
        except Exception as e:
            logger.warning(f"GIS extraction failed, using fallback: {str(e)}")
            # Return fallback data
            fallback = RRC_DEFAULTS["fallback_gis_data"]
            return GISData(
                distance=fallback["distance"],
                direction=fallback["direction"],
                town=fallback["town"],
                confidence_score=0.1,
                extraction_method="fallback"
            )
    
    async def _collect_completions_data(self, api_number: str) -> dict:
        """Collect RRC Completions data (W-2 & P-15) from completions database."""
        
        try:
            logger.info(f"🔍 COMPLETIONS WORKFLOW - Starting for API {api_number}")
            logger.info(f"📊 Initial browser pages: {len(self.context.pages)}")
            
            # Create new tab for completions search
            completions_page = await self.context.new_page()
            logger.info(f"✅ Created new completions tab: {completions_page.url}")
            logger.info(f"📊 Total browser pages: {len(self.context.pages)}")
            
            try:
                # Step 1: Navigate to completions search
                completions_url = "https://webapps.rrc.texas.gov/CMPL/publicSearchAction.do?formData.methodHndlr.inputValue=init&formData.headerTabSelected=home&formData.pageForwardHndlr.inputValue=home"
                logger.info(f"🌐 Navigating to: {completions_url}")
                await completions_page.goto(completions_url)
                await completions_page.wait_for_load_state('networkidle')
                logger.info(f"✅ Loaded completions search page: {completions_page.url}")
                
                # Step 2: Enter API number in search field
                api_input_selector = 'input[name="searchArgs.apiNoHndlr.inputValue"]'
                await completions_page.fill(api_input_selector, api_number)
                logger.info(f"✅ Entered API number: {api_number} in page: {completions_page.url}")
                
                # Step 3: Click search button
                search_button_selector = 'input[type="button"][value="Search"][onclick="doSearch();"]'
                await completions_page.click(search_button_selector)
                await completions_page.wait_for_load_state('networkidle')
                logger.info(f"✅ Clicked search button, now at: {completions_page.url}")
                logger.info(f"📊 Total browser pages after search: {len(self.context.pages)}")
                
                # Step 4: Find the row with the greatest submit date
                latest_row = await self._find_latest_completion_row(completions_page)
                
                if not latest_row:
                    logger.warning("❌ No completion records found")
                    return {"status": "no_records", "api_number": api_number}
                
                # Step 5: Click on the latest completion record
                tracking_link = await latest_row.query_selector('td:first-child a')
                if tracking_link:
                    href = await tracking_link.get_attribute('href')
                    logger.info(f"🔗 Found tracking link: {href}")
                    await tracking_link.click()
                    await completions_page.wait_for_load_state('networkidle')
                    logger.info(f"✅ Clicked tracking link, now at: {completions_page.url}")
                    logger.info(f"📊 Total browser pages after tracking click: {len(self.context.pages)}")
                    
                    # Step 6: Download all available files
                    logger.info(f"📁 Starting file downloads from page: {completions_page.url}")
                    downloaded_files = await self._download_completion_files(completions_page, api_number)
                    
                    logger.info(f"Completions data collection completed: {len(downloaded_files)} files downloaded")
                    return {
                        "status": "success",
                        "api_number": api_number,
                        "files_downloaded": downloaded_files,
                        "source": "rrc_completions"
                    }
                else:
                    logger.error("Could not find tracking link in latest row")
                    return {"status": "error", "error": "tracking_link_not_found"}
                    
            finally:
                await completions_page.close()
                
        except Exception as e:
            logger.error(f"Completions data collection failed: {e}")
            return {"status": "error", "error": str(e), "api_number": api_number}
    
    async def _find_latest_completion_row(self, page: Page):
        """Find the table row with the greatest submit date."""
        
        try:
            # Look for the data table
            table_selector = 'table.DataGrid'
            table = await page.query_selector(table_selector)
            
            if not table:
                logger.warning("Data table not found")
                return None
            
            # Get all data rows (skip header row)
            rows = await table.query_selector_all('tr')
            data_rows = rows[2:]  # Skip header and pagination rows
            
            if not data_rows:
                logger.warning("No data rows found in table")
                return None
            
            latest_row = None
            latest_date = None
            
            for row in data_rows:
                # Get submit date from 7th column (index 6)
                date_cell = await row.query_selector('td:nth-child(7)')
                if date_cell:
                    date_text = await date_cell.text_content()
                    if date_text and date_text.strip():
                        try:
                            # Parse date (MM/DD/YYYY format)
                            from datetime import datetime
                            parsed_date = datetime.strptime(date_text.strip(), '%m/%d/%Y')
                            
                            if latest_date is None or parsed_date > latest_date:
                                latest_date = parsed_date
                                latest_row = row
                                
                        except ValueError:
                            logger.warning(f"Could not parse date: {date_text}")
                            continue
            
            if latest_row:
                logger.info(f"Found latest completion record with date: {latest_date.strftime('%m/%d/%Y')}")
                return latest_row
            else:
                logger.warning("No valid completion records found")
                return None
                
        except Exception as e:
            logger.error(f"Error finding latest completion row: {e}")
            return None
    
    async def _download_completion_files(self, page: Page, api_number: str) -> list:
        """Download all completion files using direct URL method."""
        
        downloaded_files = []
        
        try:
            logger.info(f"📁 DOWNLOAD WORKFLOW - Starting from page: {page.url}")
            
            # Create processed_wells directory for this API
            from pathlib import Path
            import requests
            import re
            
            processed_dir = Path("processed_wells") / api_number
            processed_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"📂 Created directory: {processed_dir}")
            
            # Target documents we want to download
            target_documents = ['W-2', 'G-1', 'GAU LETTER', 'W-15']
            logger.info(f"🎯 Target documents: {target_documents}")
            
            # Find the Form/Attachment table
            tables = await page.query_selector_all('table')
            documents_table = None
            
            for table in tables:
                # Look for table with Form/Attachment header
                header_cells = await table.query_selector_all('th, td')
                for cell in header_cells:
                    text = await cell.inner_text()
                    if 'Form/Attachment' in text and 'View Form/Attachment' in text:
                        documents_table = table
                        logger.info("✅ Found Form/Attachment table")
                        break
                if documents_table:
                    break
            
            if not documents_table:
                logger.warning("❌ Form/Attachment table not found")
                return downloaded_files
            
            # Parse table rows to find target documents
            rows = await documents_table.query_selector_all('tr')
            logger.info(f"📊 Found {len(rows)} rows in documents table")
            
            found_documents = []
            
            for i, row in enumerate(rows):
                cells = await row.query_selector_all('td, th')
                if len(cells) >= 3:  # Should have Form/Attachment, Status, View columns
                    
                    # Get cell texts
                    cell_texts = []
                    for cell in cells:
                        text = await cell.inner_text()
                        cell_texts.append(text.strip())
                    
                    # Check if this row contains a target document
                    form_attachment = cell_texts[0] if len(cell_texts) > 0 else ""
                    status = cell_texts[1] if len(cell_texts) > 1 else ""
                    
                    # Debug: Log all found documents
                    if form_attachment and len(form_attachment) < 50:  # Avoid merged cells
                        logger.info(f"📋 Row {i}: '{form_attachment}' (Status: '{status}')")
                    
                    # Check if this is one of our target documents
                    matched_target = None
                    for target_doc in target_documents:
                        if target_doc in form_attachment and len(form_attachment) < 50:  # Avoid merged cells
                            matched_target = target_doc
                            logger.info(f"🎯 Row {i}: Found target document '{target_doc}' in '{form_attachment}'")
                            break
                    
                    if matched_target:
                        # Find the View link in this row
                        view_links = await row.query_selector_all('a')
                        logger.info(f"   🔗 Row {i}: Found {len(view_links)} links")
                        
                        for j, view_link in enumerate(view_links):
                            href = await view_link.get_attribute('href')
                            text = await view_link.inner_text()
                            logger.info(f"      Link {j}: '{text}' -> {href}")
                            
                            # Skip navigation links and focus on document links
                            if href and ('viewPdfReportFormAction.do' in href or 'dpimages/r/' in href):
                                logger.info(f"   ✅ Row {i}: Valid document link found for '{matched_target}'")
                                
                                found_documents.append({
                                    'name': matched_target,
                                    'form_attachment': form_attachment,
                                    'status': status,
                                    'href': href,
                                    'row_index': i,
                                    'link_index': j
                                })
                                break  # Only take first valid link per document type per row
                        else:
                            logger.warning(f"   ❌ Row {i}: No valid document links found for '{matched_target}'")
            
            # Remove duplicates based on href AND document type
            unique_documents = []
            seen_hrefs = set()
            seen_doc_types = set()
            
            logger.info(f"📊 Processing {len(found_documents)} found documents for deduplication...")
            
            for doc in found_documents:
                doc_key = f"{doc['name']}:{doc['href']}"
                if doc['href'] not in seen_hrefs and doc['name'] not in seen_doc_types:
                    unique_documents.append(doc)
                    seen_hrefs.add(doc['href'])
                    seen_doc_types.add(doc['name'])
                    logger.info(f"   ✅ Added unique: {doc['name']} -> {doc['form_attachment']}")
                else:
                    logger.info(f"   ⏭️  Skipping duplicate: {doc['name']} -> {doc['form_attachment']}")
            
            logger.info(f"📊 Found {len(unique_documents)} unique target documents to download")
            
            if len(unique_documents) == 0:
                logger.warning("❌ No target documents found")
                return downloaded_files
            
            for doc_info in unique_documents:
                try:
                    logger.info(f"🔄 Downloading {doc_info['name']}...")
                    
                    # Get the href for the download
                    href = doc_info['href']
                    if href:
                        # Construct full URL
                        if href.startswith('/'):
                            download_url = f"https://webapps.rrc.texas.gov{href}"
                        elif href.startswith('http'):
                            download_url = href
                        else:
                            download_url = f"https://webapps.rrc.texas.gov/{href}"
                        
                        # Generate filename based on document name
                        doc_name = doc_info['name'].replace(' ', '_').replace('-', '_')
                        filename = f"{doc_name}_{api_number}.pdf"
                        file_path = processed_dir / filename
                        
                        # Use proven direct download method
                        logger.info(f"🌐 Downloading from URL: {download_url}")
                        
                        try:
                            # Direct download using requests
                            response = requests.get(download_url, timeout=30)
                            
                            logger.info(f"📊 Response status: {response.status_code}")
                            logger.info(f"📊 Content-Type: {response.headers.get('content-type', 'Unknown')}")
                            logger.info(f"📊 Content size: {len(response.content)} bytes")
                            
                            if response.status_code == 200:
                                # Save the file
                                with open(file_path, 'wb') as f:
                                    f.write(response.content)
                                
                                # Verify the file
                                if file_path.exists():
                                    file_size = file_path.stat().st_size
                                    
                                    # Check if it's a PDF
                                    with open(file_path, 'rb') as f:
                                        header = f.read(4)
                                        is_pdf = header == b'%PDF'
                                    
                                    if is_pdf:
                                        logger.info(f"✅ SUCCESS: {filename} ({file_size} bytes) - Valid PDF")
                                    else:
                                        logger.info(f"✅ SUCCESS: {filename} ({file_size} bytes) - Document")
                                    
                                    downloaded_files.append(str(file_path))
                                else:
                                    logger.error(f"❌ File not saved: {file_path}")
                            else:
                                logger.error(f"❌ Download failed: HTTP {response.status_code}")
                                
                        except Exception as download_e:
                            logger.error(f"❌ Download failed: {download_e}")
                        
                except Exception as e:
                    logger.warning(f"Failed to download {doc_info['name']}: {e}")
                    continue
            
            logger.info(f"Downloaded {len(downloaded_files)} completion files")
            return downloaded_files
            
        except Exception as e:
            logger.error(f"Error downloading completion files: {e}")
            return downloaded_files
    
    async def _analyze_pdf_viewer_html(self, html_content: str) -> Optional[str]:
        """Use OpenAI to analyze Chrome PDF viewer HTML and find download button."""
        
        try:
            # Check if OpenAI is available
            import openai
            api_key = os.getenv('OPENAI_API_KEY')
            if not api_key:
                logger.warning("OpenAI API key not found, skipping HTML analysis")
                return None
            
            client = openai.OpenAI(api_key=api_key)
            
            # Truncate HTML if too large (OpenAI has token limits)
            if len(html_content) > 50000:
                html_content = html_content[:50000] + "... [truncated]"
            
            # Create prompt for OpenAI
            prompt = f"""
            Analyze this Chrome PDF viewer HTML and find the download button selector.
            
            I need to click the download button to download the PDF file. The download button is typically:
            - A div with id="icon" containing a cr-icon
            - A button with download-related attributes
            - An element with download, save, or similar functionality
            
            HTML:
            {html_content}
            
            Return ONLY the CSS selector that should be clicked to download the PDF.
            Examples of good selectors:
            - #icon
            - button[title="Download"]
            - cr-icon
            - [role="button"][aria-label*="Download"]
            
            Return just the selector, nothing else.
            """
            
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are an expert at analyzing HTML and finding the correct CSS selectors for web automation. Return only the CSS selector, no explanation."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=100
            )
            
            selector = response.choices[0].message.content.strip()
            
            # Clean up the response (remove quotes, extra text)
            if selector.startswith('"') and selector.endswith('"'):
                selector = selector[1:-1]
            if selector.startswith("'") and selector.endswith("'"):
                selector = selector[1:-1]
            
            # Validate it looks like a CSS selector
            valid_selector_patterns = [
                selector.startswith('#'),  # ID selector
                selector.startswith('.'),  # Class selector  
                selector.startswith('['),  # Attribute selector
                'button' in selector.lower(),  # Button element
                'icon' in selector.lower(),  # Icon element
                'a[' in selector.lower(),  # Link with attributes
                selector.startswith('a'),  # Link element
                'div' in selector.lower(),  # Div element
                'span' in selector.lower(),  # Span element
                'input' in selector.lower()  # Input element
            ]
            
            if selector and any(valid_selector_patterns):
                logger.info(f"OpenAI suggested selector: {selector}")
                return selector
            else:
                logger.warning(f"OpenAI returned invalid selector: {selector}")
                return None
                
        except Exception as e:
            logger.error(f"OpenAI HTML analysis failed: {e}")
            return None
    
    async def _fill_basic_fields(self, iframe: Page, form_data: FormData, gis_data: Optional[GISData]):
        """Fill basic form fields (API number, lease selection)."""
        
        logger.info("📝 Entering API number field...")
        # Fill API number
        api_config = self.form_selectors["api_number_field"]
        await iframe.fill(api_config.primary, form_data.api_number)
        logger.info(f"✅ API number entered: {form_data.api_number}")
        await asyncio.sleep(2)
        
        logger.info("🔽 Opening lease dropdown...")
        # Select lease
        lease_dropdown_config = self.form_selectors["lease_dropdown"]
        lease_dropdown = await iframe.query_selector(lease_dropdown_config.primary)
        if lease_dropdown:
            await lease_dropdown.click()
            logger.info("✅ Lease dropdown opened")
            await asyncio.sleep(3)
            
            logger.info("🎯 Selecting lease option...")
            lease_option_config = self.form_selectors["lease_option"]
            lease_option = await iframe.query_selector(lease_option_config.primary)
            if lease_option:
                await lease_option.click()
                logger.info("✅ Lease option selected")
                await asyncio.sleep(5)
                logger.info("⏳ Waiting for form to populate with lease data...")
            else:
                logger.warning("⚠️  Lease option not found")
        else:
            logger.warning("⚠️  Lease dropdown not found")
        
        logger.debug("Basic fields completed")
    
    async def _fill_location_fields(self, iframe: Page, form_data: FormData, gis_data: Optional[GISData]):
        """Fill location and distance fields."""
        
        if gis_data and gis_data.is_complete:
            logger.info("📍 Using GIS data for location fields...")
            # Format location string: "4 miles northwest of Kermit"
            location_text = f"{gis_data.distance} miles {gis_data.direction} of {gis_data.town}"
            logger.info(f"📍 Formatted location: {location_text}")
            
            logger.info("📝 Entering location into distance/direction field...")
            distance_config = self.form_selectors["distance_direction_field"]
            distance_field = await iframe.query_selector(distance_config.primary)
            if distance_field:
                await distance_field.fill(location_text)
                logger.info(f"✅ Location entered: {location_text}")
                await asyncio.sleep(1)
            else:
                logger.warning("⚠️  Distance/direction field not found")
        else:
            logger.info("⏭️  No GIS data available - skipping location fields")
    
    async def _fill_well_type_fields(self, iframe: Page, form_data: FormData, gis_data: Optional[GISData]):
        """Fill well type and completion fields."""
        
        # Well type selection: prefer extraction-derived value from calculated_data,
        # then fall back to GIS data, then hard default to "Oil".
        well_type = (
            form_data.calculated_data.get("well_type")
            or (gis_data.well_type if gis_data else None)
            or "Oil"
        )
        logger.info(
            f"🛢️  Determining well type: {well_type} "
            f"(source: {'extraction' if form_data.calculated_data.get('well_type') else 'gis' if gis_data and gis_data.well_type else 'default'})"
        )
        
        logger.info("🔽 Opening well type dropdown...")
        well_type_dropdown_config = self.form_selectors["well_type_dropdown"]
        well_type_dropdown = await iframe.query_selector(well_type_dropdown_config.primary)
        if well_type_dropdown:
            await well_type_dropdown.click()
            logger.info("✅ Well type dropdown opened")
            await asyncio.sleep(2)
            
            logger.info(f"🎯 Searching for well type option: {well_type}")
            # Select the appropriate option
            options = await iframe.query_selector_all('.Select-option')
            for option in options:
                try:
                    text = await option.inner_text()
                    if well_type.lower() in text.strip().lower():
                        logger.info(f"✅ Selecting well type: {text}")
                        await option.click()
                        await asyncio.sleep(2)
                        break
                except:
                    continue
            else:
                logger.warning(f"⚠️  Well type option '{well_type}' not found")
        else:
            logger.warning("⚠️  Well type dropdown not found")
        
        # Completion type - select "Single"
        completion_section_config = self.form_selectors["completion_type_section"]
        completion_section = await iframe.query_selector(completion_section_config.primary)
        if completion_section:
            await completion_section.scroll_into_view_if_needed()
            await asyncio.sleep(1)
            
            single_radio_config = self.form_selectors["completion_single_radio"]
            single_radio = await iframe.query_selector(single_radio_config.primary)
            if single_radio:
                try:
                    await single_radio.click()
                except:
                    await iframe.evaluate('element => element.click()', single_radio)
                logger.info("Selected completion type: Single")
                await asyncio.sleep(1)
        
        # Previous notice question - select "No"
        notice_section_config = self.form_selectors["previous_notice_section"]
        notice_section = await iframe.query_selector(notice_section_config.primary)
        if notice_section:
            await notice_section.scroll_into_view_if_needed()
            await asyncio.sleep(1)
            
            no_radio_config = self.form_selectors["previous_notice_no"]
            no_radio = await iframe.query_selector(no_radio_config.primary)
            if no_radio:
                try:
                    await no_radio.click()
                except:
                    await iframe.evaluate('element => element.click()', no_radio)
                logger.info("Selected: No previous notice filed")
                await asyncio.sleep(1)
    
    async def _handle_file_attachments(self, iframe: Page, file_paths: List[str]):
        """Handle file attachments — fetch GAU PDF via MEDIA_ROOT and upload it.

        Replaces the legacy ``processed_wells/<api>/GAU_LETTER_*.pdf`` lookup.
        GAU PDFs are stored at:
            MEDIA_ROOT/rrc/completions/<api_digits>/GAU_<api_digits>_NNN.pdf
        and are retrieved via ``get_gau_letter`` (apps.intelligence.services.customer_documents).

        Behaviour:
          - If ``get_gau_letter`` returns bytes: write a temp .pdf, upload via
            ``set_input_files``, delete the temp file in a ``finally`` block.
          - If ``get_gau_letter`` returns None: log a warning and return silently
            (GAU is optional per RRC filing rules).
          - If ``set_input_files`` raises: temp file is still deleted; exception
            is re-raised so the caller can handle it.
        """
        logger.info("📎 Starting file attachments process...")

        client_md = self.result.form_data.client_metadata or {}
        tenant_id = client_md.get("tenant_id", "")
        # Prefer api14_full (10/14-digit form set by the adapter) for filesystem
        # lookups; api_number is the 8-digit RRC form value and would miss the
        # MEDIA_ROOT/rrc/completions/<api_digits>/ directory.
        api14 = (
            client_md.get("api14_full")
            or getattr(self.result.form_data, "api_number", None)
            or ""
        )

        bytes_ = get_gau_letter(tenant_id, api14)
        if not bytes_:
            logger.warning(
                "GAU letter not found — skipping attachment. "
                "tenant_id=%r api14=%r",
                tenant_id,
                api14,
            )
            return

        # Write bytes to a temp file so Playwright can set_input_files by path.
        fd, tmp_path = tempfile.mkstemp(suffix=".pdf", prefix="gau_")
        os.close(fd)
        try:
            with open(tmp_path, "wb") as fh:
                fh.write(bytes_)

            logger.info("📎 Locating GAU upload section in iframe...")
            # Scroll to ensure all sections are rendered.
            try:
                await iframe.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1)
            except Exception:
                pass

            # Locate the Add button inside the GAU Attachment section using the
            # field's stable DOM id.  RRC's React widget requires clicking the
            # visible "Add" button to open the OS file chooser — calling
            # set_input_files directly on the hidden <input type="file"> never
            # registers with the React event system.
            #
            # IMPORTANT: expect_file_chooser lives on Page, NOT on Frame.
            # We get the outer Page from the BrowserContext and use page-level
            # file-chooser interception while clicking inside the iframe.
            gau_field_id = "field-ab03a8eb-152d-421f-8646-4fb66c805607"
            gau_add_button = iframe.locator(f'#{gau_field_id} button:has-text("Add")')

            # Get the outer Page from the BrowserContext.
            page = self.context.pages[0]

            logger.info("📎 Clicking GAU Attachment Add button to open file chooser...")
            try:
                async with page.expect_file_chooser() as fc_info:
                    await gau_add_button.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(tmp_path)
                logger.info("✅ GAU file submitted to file chooser")
                await asyncio.sleep(1)
            except Exception as exc:
                # GAU is optional — if the Add button isn't found or the file
                # chooser times out, log a warning and skip rather than failing
                # the whole submission.
                logger.warning(
                    "GAU attachment skipped — Add button or file chooser not available: %s",
                    exc,
                )
                return

        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    
    async def _configure_area_review(self, iframe: Page):
        """Configure area review settings based on RRC District."""
        
        try:
            logger.info("👥 Starting area review configuration...")
            
            # Get RRC District to determine logic
            rrc_district = await self._extract_rrc_district(iframe)
            logger.info(f"🔍 RRC District for area review: {rrc_district}")
            
            if rrc_district == '7C':
                logger.info("⏭️  District 7C detected - skipping area review (GIS data used)")
                return
            
            logger.info(f"👥 District {rrc_district} detected - configuring area review with depth 0...")
            
            # Scroll to ensure all sections are loaded
            await iframe.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
            
            # Count itemizer-grid sections using a Locator so the count reflects
            # the current DOM (not a stale snapshot captured before any Saves).
            grids_locator = iframe.locator('div.itemizer-grid')
            total_grids = await grids_locator.count()
            logger.info(f"📊 Found {total_grids} itemizer grid sections")

            # Also log area-review text sections for diagnostics (read-only, no stale risk).
            area_review_count = await iframe.locator('div:has-text("Area Review"), div:has-text("Depth of Zone")').count()
            logger.info(f"📊 Found {area_review_count} sections with area review text")

            sections_processed = 0

            # Iterate by index so each iteration re-resolves the grid Locator
            # against the CURRENT DOM.  RRC re-renders the entire grid after each
            # per-section Save, which invalidates any ElementHandle captured before
            # the click.  Locators never go stale — nth(i) is resolved on demand.
            for grid_index in range(total_grids):
                try:
                    logger.info(f"👥 Processing itemizer grid section {grid_index + 1}...")

                    # Re-resolve this grid against the current DOM on every iteration.
                    grid = grids_locator.nth(grid_index)

                    # Scroll section into view
                    await grid.scroll_into_view_if_needed()
                    await asyncio.sleep(1)

                    # Find Add button scoped within this grid (Locator, not ElementHandle)
                    add_button = grid.locator('button:has-text("Add")')
                    add_count = await add_button.count()
                    if add_count > 0:
                        # Check if button is visible and enabled
                        is_visible = await add_button.first.is_visible()
                        is_enabled = await add_button.first.is_enabled()

                        logger.info(f"🖱️  Add button found in section {grid_index + 1} (visible: {is_visible}, enabled: {is_enabled})")

                        if is_visible and is_enabled:
                            await add_button.first.click()
                            await asyncio.sleep(3)
                            logger.info(f"✅ Add button clicked in section {grid_index + 1}")

                            # Look for depth input field that appears after clicking Add
                            # Wait a bit for the UI to update
                            await asyncio.sleep(2)

                            # Search iframe-wide for depth inputs.  After clicking Add,
                            # RRC injects the new edit row's number input somewhere in the
                            # iframe DOM — it is NOT necessarily inside div.itemizer-grid.
                            # Reverse-iterating picks the most-recently appended empty
                            # input, which is the just-Added row.
                            depth_inputs_locator = iframe.locator('input[type="number"]')
                            depth_input_count = await depth_inputs_locator.count()
                            logger.info(f"📊 Found {depth_input_count} number input fields in section {grid_index + 1}")

                            depth_filled = False
                            # Iterate from last to first to prefer the most-recently added row
                            for di in range(depth_input_count - 1, -1, -1):
                                try:
                                    depth_input = depth_inputs_locator.nth(di)
                                    current_value = await depth_input.get_attribute('value')
                                    is_visible = await depth_input.is_visible()

                                    if is_visible and (not current_value or current_value == ''):
                                        logger.info(f"📝 Found empty depth input - entering value: 0")
                                        await depth_input.fill('0')
                                        logger.info("✅ Entered depth: 0")
                                        await asyncio.sleep(1)
                                        depth_filled = True
                                        break
                                except Exception as depth_e:
                                    logger.warning(f"Depth input error: {depth_e}")
                                    continue

                            if not depth_filled:
                                logger.warning(f"⚠️  Could not find empty depth input for section {grid_index + 1}")

                            # Search iframe-wide for the Save button.  The per-row Save
                            # that appears after clicking Add is not necessarily inside
                            # div.itemizer-grid.  The first visible+enabled Save is the
                            # per-row Save — the global form-level Save is not
                            # visible/enabled until form completion.
                            save_button_locator = iframe.locator('button:has-text("Save")')
                            save_button_count = await save_button_locator.count()
                            logger.info(f"📊 Found {save_button_count} Save buttons in section {grid_index + 1}")

                            save_clicked = False
                            for si in range(save_button_count):
                                try:
                                    save_button = save_button_locator.nth(si)
                                    is_visible = await save_button.is_visible()
                                    is_enabled = await save_button.is_enabled()

                                    if is_visible and is_enabled:
                                        logger.info(f"💾 Clicking Save button for section {grid_index + 1}...")
                                        await save_button.click()
                                        logger.info(f"✅ Save button clicked for section {grid_index + 1}")
                                        # Allow RRC to re-render the grid DOM before the
                                        # next iteration resolves nth(grid_index + 1).
                                        await asyncio.sleep(3)
                                        save_clicked = True
                                        sections_processed += 1
                                        break
                                except Exception as save_e:
                                    logger.warning(f"Save button error: {save_e}")
                                    continue

                            if not save_clicked:
                                logger.warning(f"⚠️  Could not click Save button for section {grid_index + 1}")
                        else:
                            logger.info(f"⏭️  Add button not clickable in section {grid_index + 1}")
                    else:
                        logger.info(f"⏭️  No Add button found in section {grid_index + 1}")

                except Exception as e:
                    logger.error(f"❌ Area review section {grid_index + 1} error: {e}")
                    continue
            
            logger.info(f"✅ Area review configuration completed - processed {sections_processed} sections")
                        
        except Exception as e:
            logger.error(f"❌ Area review configuration failed: {str(e)}")
    
    async def _fill_contact_information(self, iframe: Page, form_data: FormData):
        """Fill contact information and dates.

        contact_phone, contact_email, and cementing_company_name are read from
        form_data.calculated_data (populated by the adapter from
        TenantBusinessProfile.rrc.w3a.*).  A missing key raises KeyError so
        mis-configured profiles fail loudly instead of silently filing BCM's
        contact details.

        The cementing textarea is composed from three optional components:
        cementing_company_name (required), cementing_company_address (optional),
        and cementing_company_p5 (optional).  All three are joined via
        build_cementing_text().

        EXT field: filled only when contact_ext is present AND non-empty.
        If the EXT selector is not found but a value was configured, a WARNING
        is logged (never raises).
        """
        # Extract tenant-specific values BEFORE the try block so KeyError propagates.
        cementing_company = form_data.calculated_data["cementing_company_name"]
        contact_phone = form_data.calculated_data["contact_phone"]
        contact_email = form_data.calculated_data["contact_email"]

        # Optional fields — absent key means not configured; treat as "".
        cementing_address = form_data.calculated_data.get("cementing_company_address", "")
        cementing_p5 = form_data.calculated_data.get("cementing_company_p5", "")
        contact_ext = form_data.calculated_data.get("contact_ext", None)

        # Build composite cementing textarea value.
        cementing_text = build_cementing_text(
            cementing_company,
            address=cementing_address or "",
            p5=cementing_p5 or "",
        )

        try:
            defaults = RRC_DEFAULTS

            # Cementing company textarea (composite: name + address + P-5 number).
            cementing_config = self.form_selectors["cementing_field"]
            cementing_field = await iframe.query_selector(cementing_config.primary)
            if cementing_field:
                await cementing_field.scroll_into_view_if_needed()
                await cementing_field.fill(cementing_text)
                logger.info("Filled cementing company info")
                await asyncio.sleep(1)

            # Anticipated plugging date (1 month ahead)
            today = datetime.now()
            try:
                target_date = today.replace(month=today.month + 1)
            except ValueError:  # Handle December -> January
                target_date = today.replace(year=today.year + 1, month=1)

            # react-datetime is configured with dateFormat="MMM D YYYY" (e.g. "May 16 2026").
            # Using %m/%d/%Y silently fails — the widget's parser rejects it and clears state on save.
            date_string = f"{target_date.strftime('%b')} {target_date.day} {target_date.year}"

            # Anticipated plugging date — react-datetime uses Moment.js internal state
            # and ignores DOM-set values via native setter or Playwright keystrokes.
            # Canonical workaround: drive the calendar picker directly by clicking
            # the input to open the picker, navigating to the target month, then
            # clicking the target day cell.
            date_selector_root = '#field-31007eb0-7ec1-4f25-ab78-9b2309d154c2'
            date_input_sel = f'{date_selector_root} input.form-control'
            picker_sel = f'{date_selector_root} .rdtPicker'

            # 1. Click the input to focus and open the picker
            await iframe.locator(date_input_sel).first.click()
            await asyncio.sleep(0.5)

            # 2. Navigate to target month — read .rdtSwitch text, click .rdtNext until match
            target_month_str = target_date.strftime("%B %Y")   # e.g. "June 2026"
            for _ in range(24):  # safety bound: never loop more than 24 months
                switch_locator = iframe.locator(f'{picker_sel} .rdtSwitch').first
                current = (await switch_locator.text_content() or "").strip()
                if current == target_month_str:
                    break
                await iframe.locator(f'{picker_sel} .rdtNext').first.click()
                await asyncio.sleep(0.25)
            else:
                logger.warning(f"Could not navigate to target month {target_month_str}; current={current}")

            # 3. Click the day cell (filter out adjacent-month days)
            day_str = str(target_date.day)
            day_locator = iframe.locator(
                f'{picker_sel} td.rdtDay:not(.rdtOld):not(.rdtNew)',
                has_text=day_str,
            ).first
            await day_locator.click()
            await asyncio.sleep(0.5)
            logger.info(f"Set plugging date via calendar click: {target_date.strftime('%m/%d/%Y')}")

            # Contact title
            title_config = self.form_selectors["title_field"]
            title_field = await iframe.query_selector(title_config.primary)
            if title_field:
                await title_field.fill(defaults["contact_title"])
                logger.info("Filled contact title")
                await asyncio.sleep(1)

            # Phone number
            phone_config = self.form_selectors["phone_field"]
            phone_field = await iframe.query_selector(phone_config.primary)
            if phone_field:
                await phone_field.fill(contact_phone)
                logger.info("Filled phone number")
                await asyncio.sleep(1)

            # Email
            email_config = self.form_selectors["email_field"]
            email_field = await iframe.query_selector(email_config.primary)
            if email_field:
                await email_field.fill(contact_email)
                logger.info("Filled email address")
                await asyncio.sleep(1)

            # EXT field — only when contact_ext is present and non-empty.
            # The DOM has <div class="phone-ext"><label>EXT</label><input class="form-control" .../>
            # The label is NOT wired via for/id, so get_by_label("EXT") does not match.
            # Use the direct CSS selector that anchors to the .phone-ext wrapper.
            if contact_ext:
                ext_filled = False
                try:
                    ext_input = iframe.locator('.phone-ext input.form-control')
                    count = await ext_input.count()
                    if count > 0:
                        await ext_input.fill(contact_ext)
                        logger.info(f"Filled EXT field: {contact_ext!r}")
                        ext_filled = True
                except Exception:
                    pass

                if not ext_filled:
                    logger.warning(
                        f"EXT field not found on form — contact_ext value {contact_ext!r} "
                        "was not filled. Check selector or form layout."
                    )

        except Exception as e:
            logger.warning(f"Contact information filling failed: {str(e)}")
    
    # Parent grid container for the Casing Record table on the W-3A form.
    _CASING_GRID_FIELD_ID = "field-6dc56f71-575c-40ac-8c82-d4db2074f2a6"

    # Row-edit form field IDs (captured from popup DOM 2026-05-18).
    _CASING_FIELD_TYPE = "field-b4bc0b18-eaf7-4124-8338-9d66ec129d43"
    _CASING_FIELD_SUBTYPE = "field-85b8f890-f7da-47c8-ba9a-c39398d9539f"
    _CASING_FIELD_SIZE = "field-214fdbac-28f5-44f3-a612-21a39a30d037"
    _CASING_FIELD_HOLE_SIZE = "field-3a856cad-cea2-456c-9691-8aac4f9ff709"
    _CASING_FIELD_DEPTH = "field-478d7da8-f81e-440d-978b-84491c74617f"
    _CASING_FIELD_CEMENT_SACKS = "field-faba1696-5cd6-4bf5-9328-1f64f5f92ae2"
    _CASING_FIELD_TOC = "field-60aeb29b-573f-4b15-a61b-35600e5dcef3"
    _CASING_FIELD_TOC_METHOD = "field-f39d3957-4e44-4898-928d-ed507c473901"
    _CASING_FIELD_RECOVERY = "field-2d5839f7-faf8-453a-895d-0260904d1e41"
    _CASING_FIELD_ADDITIVES = "field-1c30efa4-50cc-4085-b59d-f8c4fc542da1"

    # Sentinel that signals the row-edit form is mounted in the iframe.
    _CASING_ROW_SENTINEL = "input[name='b4bc0b18-eaf7-4124-8338-9d66ec129d43']"

    # Parent grid container for the Perforation Record table on the W-3A form.
    # Field-IDs for the row-edit form are unknown until DOM recon runs.
    _PERFORATION_GRID_FIELD_ID = "field-02cd5751-47e1-4fa1-aa5a-cf641e0628c0"

    # Parent grid container for the Plugging Proposal table on the W-3A form.
    _PLUGGING_PROPOSAL_GRID_FIELD_ID = "field-cf2c9c26-1e6a-402e-8697-adec56130b49"
    # Row-edit form field IDs (captured from popup DOM recon 2026-05-18).
    _PP_TYPE_FIELD_ID = "field-abe41737-4ede-4e2b-9bfc-2f25f7af5559"
    _PP_SATA_FIELD_ID = "field-266d73fd-c79e-46ae-a283-0828a2f0f13f"
    _PP_BOTTOM_FT_FIELD_ID = "field-2d5b6f37-9460-4733-af08-ba48c36c690c"
    _PP_TOP_FT_FIELD_ID = "field-618c1fd7-3a1e-45e4-b781-3fa04a1db5fd"
    _PP_SACKS_FIELD_ID = "field-b3d00304-7d8b-47e6-b001-5a2ce26b92a4"

    # Kernel step.type / step.plug_type → RRC W-3A Plugging Proposal Type
    # dropdown option mapping. User-confirmed 2026-05-18.
    # NOTE: kernel doesn't emit CIBP / TTBP / Cement Retainer / Other today.
    # Top plugs map to "Cement Surface Plug"; everything else maps to
    # "Cement Plug" (the default).
    _PP_RRC_TYPE_CEMENT_PLUG = "Cement Plug"
    _PP_RRC_TYPE_CEMENT_SURFACE_PLUG = "Cement Surface Plug"

    # SATA (Select-All-That-Apply) option labels (verbatim — note option 3
    # has a missing space after the hyphen).
    _PP_SATA_PERF_CIRCULATE = "1 - Perforate and Circulate"
    _PP_SATA_PERF_SQUEEZE = "2 - Perforate and Squeeze"
    _PP_SATA_TAG_TOP = "3 -Tag top of plug"
    _PP_SATA_WAIT_TAG = "4 - Wait 4 hours and tag top of plug"
    _PP_SATA_PRESSURE_TEST = "5 - Pressure Test"
    _PP_SATA_NONE = "6 - None"

    # Kernel plug_type → SATA option to add.
    _PP_PLUG_TYPE_TO_SATA = {
        "perf_and_squeeze_plug": _PP_SATA_PERF_SQUEEZE,
        "perf_and_circulate_plug": _PP_SATA_PERF_CIRCULATE,
        "spot_plug": _PP_SATA_NONE,
        "dumpbail_plug": _PP_SATA_NONE,
    }

    # Kernel step.type → SATA option to add (in addition to plug_type mapping).
    _PP_TYPE_TO_SATA = {
        "perforate_and_squeeze_plug": _PP_SATA_PERF_SQUEEZE,
        "perf_and_circulate_to_surface": _PP_SATA_PERF_CIRCULATE,
    }

    async def _fill_casing_record(self, iframe: Page, form_data: FormData):
        """Fill the W-3A Casing Record grid (clear, then repopulate).

        The "Add" control opens a row-edit SPA route inside the same iframe
        (NOT a new tab / popup). Save commits + navigates back to the parent
        W-3A form; Back abandons. We always clear existing rows first, then
        append the rows in ``form_data.calculated_data["casings"]``.

        Reuse note: Perforation Record + Plugging Proposal use the same
        iframe-route pattern. The Add-row plumbing (``_add_casing_row``) is
        intentionally inlined here while there's only one caller — when the
        second pattern lands, refactor into a generic helper that takes the
        parent grid ID + a per-row fill callback.
        """
        rows = (form_data.calculated_data or {}).get("casings") or []
        if not isinstance(rows, list):
            logger.warning(
                "Casing record: calculated_data['casings'] is %s, expected list — skipping",
                type(rows).__name__,
            )
            return

        logger.info("🧱 Casing record: %d row(s) to fill", len(rows))

        grid_selector = f"#{self._CASING_GRID_FIELD_ID}"
        grid = iframe.locator(grid_selector)

        # Scroll the grid into view so the Add/Remove buttons are clickable.
        try:
            await grid.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            # Non-fatal — visibility is enforced per-action below.
            pass
        await asyncio.sleep(0.5)

        # --- CLEAR existing rows ---------------------------------------
        # On a fresh draft the grid is empty and this is a no-op. On
        # re-dispatch the grid has rows from a prior run; we always nuke
        # and repopulate (see project_w3a_table_idempotency memory:
        # never diff, never skip-if-populated).
        #
        # Fail-fast: clear failures MUST bubble up so we don't proceed to
        # Add on top of a populated grid (bug #2). The parent step
        # orchestration will mark the section failed.
        await self._clear_casing_rows(iframe, grid_selector)

        try:
            # --- ADD each row ---------------------------------------------
            for idx, row in enumerate(rows):
                if not isinstance(row, dict):
                    logger.warning("Casing record: row %d is not a dict, skipping", idx)
                    continue
                logger.info(
                    "🧱 Casing row %d/%d: type=%s size=%s depth=%s",
                    idx + 1,
                    len(rows),
                    row.get("type"),
                    row.get("casing_size"),
                    row.get("depth"),
                )
                await self._add_casing_row(iframe, grid_selector, row)

            logger.info("✅ Casing record: %d row(s) filled", len(rows))

        except Exception as e:
            # Add/Save failures: log + record so the job still produces a
            # draft the user can inspect. Clear failures are NOT caught
            # here — they bubble up above.
            logger.error(f"Casing record filling failed: {e}", exc_info=True)
            try:
                self.result.add_log_entry("ERROR", f"Casing record failed: {e}", step="casing_record")
            except Exception:
                pass

    async def _fill_perforation_record(self, iframe, form_data):
        """
        Intentional no-op. RRC prepopulates Perforation Record from well
        production history, and perforations don't change during a plug job —
        so RRC's prepop is authoritative.

        This function stays as a hook point for future work on:
        - Setting Plugged/Not Plugged status (post-plug W-3 form)
        - Historic Plug Information subform

        The ``_PERFORATION_GRID_FIELD_ID`` constant captures the parent grid
        anchor for whoever picks this up next. Row-edit form-shape (captured
        via DOM recon 2026-05-18):
          - field-a3d438c0-...  Type radio (Perforations / Open Hole)
          - field-520821d9-...  "to ##### feet (shallower)" number
          - field-22bc64bc-...  "From ##### feet (deeper)" number
          - field-d29845b8-...  Plugged or Not Plugged radio
          - field-ff06cacb-...  Record of Perforation ID (system-assigned)

        See memory: project_w3a_perforation_skip.md
        """
        logger.info("⏭️  Perforation Record: skipped (RRC prepop is authoritative)")

    def _resolve_pp_rrc_type(self, row: Dict[str, Any]) -> str:
        """Map a kernel plug step to the RRC Plugging Proposal Type dropdown
        option label. User decision 2026-05-18:

            kernel type == 'top_plug'  → 'Cement Surface Plug'
            everything else            → 'Cement Plug'

        Kernel doesn't emit CIBP / TTBP / Cement Retainer / Other today; if
        it ever does, this resolver will need extending.
        """
        kernel_type = (row.get("type") or "").strip()
        if kernel_type == "top_plug":
            return self._PP_RRC_TYPE_CEMENT_SURFACE_PLUG
        # Default: every cement/formation/perf/dumpbail plug gets the regular
        # "Cement Plug" RRC option.
        return self._PP_RRC_TYPE_CEMENT_PLUG

    def _resolve_pp_sata_options(self, row: Dict[str, Any]) -> List[str]:
        """Return the deduped list of SATA option labels to select for a row.

        Maps from kernel ``plug_type`` (primary) AND kernel ``type`` (secondary,
        for perforate-and-squeeze / perf-and-circulate top types). Unknown /
        missing methods default to ``6 - None`` so the multi-select is never
        empty (RRC requires at least one selection).
        """
        plug_type = (row.get("plug_type") or "").strip()
        kernel_type = (row.get("type") or "").strip()

        opts: List[str] = []

        plug_type_opt = self._PP_PLUG_TYPE_TO_SATA.get(plug_type)
        if plug_type_opt is not None:
            opts.append(plug_type_opt)

        type_opt = self._PP_TYPE_TO_SATA.get(kernel_type)
        if type_opt is not None and type_opt not in opts:
            opts.append(type_opt)

        # Default fallback when neither mapping matched.
        if not opts:
            opts.append(self._PP_SATA_NONE)

        return opts

    async def _fill_plugging_proposal(self, iframe: Page, form_data: FormData):
        """Fill the W-3A Plugging Proposal grid (clear, then repopulate).

        Mirrors the Casing-record pattern: clear existing rows, then iterate
        ``form_data.calculated_data["plug_rows"]`` and Add each one. Unlike
        Casing, the Plugging Proposal grid is NOT prepopulated by RRC — a
        fresh draft has an empty grid, so the clear is a no-op on first run
        and only kicks in on re-dispatch (idempotency: see memory
        project_w3a_table_idempotency.md).

        Each row needs:
          1. Type dropdown (react-select v1) — driven by kernel step.type.
          2. Bottom Ft / Top Ft / Sacks number inputs (conditional fields
             that mount after Type is selected).
          3. SATA multi-select (react-select v1 multi) — driven by kernel
             plug_type + type.

        FAIL-FAST: a Cement Plug / Cement Surface Plug row with sacks <= 0
        raises RuntimeError immediately so we don't file an invalid form.
        Tracked by Trello card #95 (kernel must emit non-zero sacks).
        """
        rows = (form_data.calculated_data or {}).get("plug_rows") or []
        if not isinstance(rows, list):
            logger.warning(
                "Plugging Proposal: calculated_data['plug_rows'] is %s, "
                "expected list — skipping",
                type(rows).__name__,
            )
            return

        if not rows:
            logger.warning("⏭️  Plugging Proposal: no plug_rows in form_data — skipping")
            return

        logger.info("🪛 Plugging Proposal: %d row(s) to fill", len(rows))

        grid_selector = f"#{self._PLUGGING_PROPOSAL_GRID_FIELD_ID}"
        grid = iframe.locator(grid_selector)

        try:
            await grid.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(0.5)

        # --- CLEAR existing rows ----------------------------------------
        # No-op on a fresh draft (the PP grid starts empty per recon). On
        # re-dispatch we always nuke + repopulate. Fail-fast on clear errors
        # so we don't pile Adds onto unremoved rows.
        await self._clear_plugging_proposal_rows(iframe, grid_selector)

        try:
            # --- ADD each row -------------------------------------------
            for idx, row in enumerate(rows):
                if not isinstance(row, dict):
                    logger.warning(
                        "Plugging Proposal: row %d is not a dict, skipping", idx
                    )
                    continue
                logger.info(
                    "🪛 PP row %d/%d: type=%s plug_type=%s top=%s bottom=%s sacks=%s",
                    idx + 1,
                    len(rows),
                    row.get("type"),
                    row.get("plug_type"),
                    row.get("top_ft"),
                    row.get("bottom_ft"),
                    row.get("sacks"),
                )
                await self._add_plugging_proposal_row(iframe, grid_selector, row, idx + 1)

            logger.info("✅ Plugging Proposal: %d row(s) filled", len(rows))

        except Exception as e:
            # Add/Save failures: log + record so the job still produces a
            # draft the user can inspect. Clear failures and fail-fast
            # RuntimeErrors above are NOT caught here — they bubble up so
            # the parent step orchestrator marks the section failed.
            if isinstance(e, RuntimeError):
                raise
            logger.error(f"Plugging Proposal filling failed: {e}", exc_info=True)
            try:
                self.result.add_log_entry(
                    "ERROR",
                    f"Plugging Proposal failed: {e}",
                    step="plugging_proposal",
                )
            except Exception:
                pass

    async def _clear_plugging_proposal_rows(self, iframe: Page, grid_selector: str):
        """Remove all existing rows from the Plugging Proposal grid.

        Mirrors ``_clear_casing_rows`` — same react-data-grid library, same
        select-all + Remove header-button pattern, same confirmation modal.
        The PP grid IS empty on a fresh draft (no prepop), so this is a
        no-op on first dispatch and only matters on re-dispatch.

        IMPORTANT: scope all locators to ``grid_selector``; the iframe
        contains 5 itemizer grids that share ``#select-all-checkbox`` and
        ``checkbox0..N`` IDs (memory: project_w3a_table_idempotency.md).
        """
        grid = iframe.locator(grid_selector)

        try:
            await grid.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass

        row_locator = grid.locator(".react-grid-Row")
        try:
            initial_count = await row_locator.count()
        except Exception as e:
            logger.warning(
                f"PP clear: could not count rows ({e}) — assuming empty"
            )
            return

        if initial_count == 0:
            logger.info("🧹 PP clear: 0 existing rows (fresh draft, no-op)")
            return

        logger.warning("🧹 PP clear: %d existing row(s) to remove", initial_count)

        # --- Phase 1: select all rows --------------------------------------
        select_all_strategy = None
        try:
            select_all_input = grid.locator("input#select-all-checkbox").first
            await select_all_input.check(force=True, timeout=5000)
            select_all_strategy = "A: input.check(force=True)"
            logger.info("🧹 PP clear: select-all strategy A succeeded")
        except Exception as e_a:
            logger.warning(f"🧹 PP clear: strategy A failed: {e_a}")
            try:
                ok = await grid.evaluate(
                    """
                    (gridEl) => {
                        const cb = gridEl.querySelector('#select-all-checkbox');
                        if (!cb) return false;
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'checked'
                        ).set;
                        setter.call(cb, true);
                        cb.dispatchEvent(new Event('click', {bubbles: true}));
                        cb.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                    """
                )
                if ok:
                    select_all_strategy = "B: JS setter + dispatchEvent"
                    logger.info("🧹 PP clear: select-all strategy B succeeded")
                else:
                    raise RuntimeError(
                        "strategy B: #select-all-checkbox not found in grid"
                    )
            except Exception as e_b:
                logger.warning(f"🧹 PP clear: strategy B failed: {e_b}")
                try:
                    clicked_any = False
                    for i in range(initial_count):
                        try:
                            await grid.locator(
                                f"label[for='checkbox{i}']"
                            ).first.click(force=True, timeout=5000)
                            clicked_any = True
                        except Exception as e_row:
                            logger.warning(
                                f"🧹 PP clear: strategy C row {i} click failed: {e_row}"
                            )
                    if not clicked_any:
                        raise RuntimeError("strategy C: no per-row labels clicked")
                    select_all_strategy = "C: per-row label clicks"
                    logger.info("🧹 PP clear: select-all strategy C succeeded")
                except Exception as e_c:
                    logger.warning(f"🧹 PP clear: strategy C failed: {e_c}")
                    raise RuntimeError(
                        f"PP clear: all select-all strategies failed "
                        f"(A: {e_a}; B: {e_b}; C: {e_c})"
                    )

        await asyncio.sleep(0.5)

        # --- Phase 2: click the grid-level Remove button -------------------
        remove_button = grid.locator(
            "div.btn-group button:has(i.fa-minus-circle)"
        ).first
        if await remove_button.count() == 0:
            remove_button = grid.locator("button:has(i.fa-minus-circle)").first
        if await remove_button.count() == 0:
            raise RuntimeError(
                "PP clear: Remove button not found inside grid"
            )

        disabled_attr = await remove_button.get_attribute("disabled")
        for _ in range(10):
            if disabled_attr is None:
                break
            await asyncio.sleep(0.5)
            disabled_attr = await remove_button.get_attribute("disabled")
        if disabled_attr is not None:
            raise RuntimeError(
                f"PP clear: Remove button still disabled after select-all "
                f"(strategy={select_all_strategy})"
            )

        try:
            await remove_button.click()
            logger.info("🧹 PP clear: clicked Remove button")
        except Exception as e:
            raise RuntimeError(f"PP clear: Remove click failed: {e}") from e

        # --- Phase 3: handle optional confirmation modal -------------------
        confirm_selectors = [
            ".modal.in button.btn-primary",
            ".modal.in button:has-text('Yes')",
            ".modal.in button:has-text('OK')",
            ".modal.in button:has-text('Confirm')",
            ".modal.in button:has-text('Remove')",
            ".modal-dialog button.btn-primary",
            "div[role='dialog'] button.btn-primary",
        ]
        await asyncio.sleep(0.5)
        for sel in confirm_selectors:
            try:
                btn = iframe.locator(sel).first
                if await btn.count() == 0:
                    continue
                if not await btn.is_visible():
                    continue
                await btn.click()
                logger.info(
                    "🧹 PP clear: confirmed removal via modal '%s'", sel
                )
                break
            except Exception as e:
                logger.debug(f"PP clear: confirm selector '{sel}' failed: {e}")
                continue

        # --- Phase 4: wait for row count to reach 0 ------------------------
        for _ in range(20):
            try:
                current_count = await row_locator.count()
            except Exception:
                current_count = 0
            if current_count == 0:
                break
            await asyncio.sleep(0.5)

        try:
            final_count = await row_locator.count()
        except Exception:
            final_count = -1
        if final_count > 0:
            raise RuntimeError(
                f"PP clear failed: {final_count} rows still present "
                f"(started with {initial_count}, strategy={select_all_strategy})"
            )
        logger.info(
            "✅ PP clear: removed all rows (started with %d, strategy=%s)",
            initial_count,
            select_all_strategy,
        )

    async def _pp_select_react_select_option(
        self,
        iframe: Page,
        field_id: str,
        option_label: str,
        *,
        timeout_ms: int = 5000,
    ) -> None:
        """Open a react-select v1 widget and click the option matching
        ``option_label`` (exact text). Works for both single (Type) and
        multi (SATA) variants — the menu stays open after a click in multi
        mode, which is what we want when selecting several options.

        Raises RuntimeError on failure so the caller can fail-fast.
        """
        control = iframe.locator(f"#{field_id} .Select-control")
        menu = iframe.locator(f"#{field_id} .Select-menu-outer")
        try:
            await control.click(timeout=timeout_ms)
            await menu.wait_for(state="visible", timeout=timeout_ms)
        except Exception as e:
            raise RuntimeError(
                f"react-select open failed for {field_id}: {e}"
            ) from e

        # Exact-text match to avoid substring collisions (e.g. "Cement Plug"
        # is a substring of "Cement Surface Plug").
        option_locator = iframe.locator(
            f"#{field_id} .Select-option"
        ).filter(has_text=option_label).first
        if await option_locator.count() == 0:
            raise RuntimeError(
                f"react-select option {option_label!r} not found in {field_id}"
            )
        try:
            await option_locator.click(timeout=timeout_ms)
        except Exception as e:
            raise RuntimeError(
                f"react-select option click failed ({option_label!r} in {field_id}): {e}"
            ) from e

    async def _add_plugging_proposal_row(
        self,
        iframe: Page,
        grid_selector: str,
        row: Dict[str, Any],
        row_idx: int,
    ) -> None:
        """Click Add on the Plugging Proposal grid, fill the row-edit form,
        click Save. Scoped strictly to ``grid_selector`` (iframe-wide
        locators trigger strict-mode violations + cross-grid picks).
        """
        grid = iframe.locator(grid_selector)

        rrc_type = self._resolve_pp_rrc_type(row)

        # FAIL-FAST: kernel must emit non-zero sacks for cement plugs.
        # Trello card #95 tracks the kernel-side fix. We refuse to file an
        # invalid draft with 0-sack plugs.
        if rrc_type in (
            self._PP_RRC_TYPE_CEMENT_PLUG,
            self._PP_RRC_TYPE_CEMENT_SURFACE_PLUG,
        ):
            sacks_val = row.get("sacks")
            try:
                sacks_int = int(sacks_val) if sacks_val is not None else 0
            except (TypeError, ValueError):
                sacks_int = 0
            if sacks_int <= 0:
                raise RuntimeError(
                    f"🚨 Plug step has no sacks — failing fast. Kernel must "
                    f"emit sacks (Trello card #95). plug_row={row}"
                )

        # We currently only support the two cement-plug variants. CIBP /
        # TTBP / Cement Retainer / Other have different field sets and
        # different sack requirements (see dispatch notes) — defer until
        # the kernel emits them.
        if rrc_type not in (
            self._PP_RRC_TYPE_CEMENT_PLUG,
            self._PP_RRC_TYPE_CEMENT_SURFACE_PLUG,
        ):
            raise NotImplementedError(
                f"PP row idx={row_idx} resolved to RRC Type {rrc_type!r}; "
                f"only Cement Plug / Cement Surface Plug are supported today. "
                f"row={row}"
            )

        # --- Snapshot 1: row count BEFORE clicking Add ---
        pre_add_count = await grid.locator(".react-grid-Row").count()
        logger.info(f"📸 PP pre-Add row count (row {row_idx}): {pre_add_count}")

        # --- Click Add (scoped to this grid) ---
        add_button = grid.locator(
            "div.btn-group button:has(i.fa-plus-circle)"
        ).first
        if await add_button.count() == 0:
            add_button = grid.locator("button:has-text('Add')").first
        await add_button.scroll_into_view_if_needed()
        await add_button.click(timeout=15000)
        logger.info("➕ PP add: clicked Add (row %d)", row_idx)

        # --- Wait for row-edit form to mount (Type dropdown sentinel) ---
        type_sentinel = iframe.locator(f"#{self._PP_TYPE_FIELD_ID}").first
        try:
            await type_sentinel.wait_for(state="attached", timeout=10000)
        except Exception as e:
            raise RuntimeError(
                f"PP row-edit form did not appear within 10s: {e}"
            )
        # Brief settle for React hydration.
        await asyncio.sleep(1.0)

        # --- Select the Type dropdown option ---
        logger.info("🪛 PP row %d: selecting Type=%r", row_idx, rrc_type)
        await self._pp_select_react_select_option(
            iframe, self._PP_TYPE_FIELD_ID, rrc_type
        )

        # Wait ~1s for the conditional Bottom/Top/Sacks fields to mount.
        await asyncio.sleep(1.2)

        # --- Fill conditional number fields (native-setter) ---
        # Cement Plug & Cement Surface Plug both expose Bottom Ft, Top Ft,
        # Sacks per recon. Bottom/Top map directly from kernel bottom_ft /
        # top_ft. Sacks already validated > 0 above.
        bottom_ft = row.get("bottom_ft")
        if bottom_ft is None:
            raise RuntimeError(
                f"PP row {row_idx}: bottom_ft missing — kernel must emit it. row={row}"
            )
        await self._set_input_via_native_setter(
            iframe, f"#{self._PP_BOTTOM_FT_FIELD_ID}", bottom_ft
        )

        top_ft = row.get("top_ft")
        if top_ft is None:
            raise RuntimeError(
                f"PP row {row_idx}: top_ft missing — kernel must emit it. row={row}"
            )
        await self._set_input_via_native_setter(
            iframe, f"#{self._PP_TOP_FT_FIELD_ID}", top_ft
        )

        sacks = row.get("sacks")
        await self._set_input_via_native_setter(
            iframe, f"#{self._PP_SACKS_FIELD_ID}", sacks
        )

        # --- SATA multi-select ---
        sata_options = self._resolve_pp_sata_options(row)
        logger.info(
            "🪛 PP row %d: selecting SATA options %r", row_idx, sata_options
        )
        for opt in sata_options:
            await self._pp_select_react_select_option(
                iframe, self._PP_SATA_FIELD_ID, opt
            )
            await asyncio.sleep(0.2)
        # Close the SATA menu so it doesn't intercept the Save click.
        try:
            await iframe.keyboard.press("Escape")
        except Exception:
            pass
        await asyncio.sleep(0.3)

        # --- Pre-Save diagnostic — form groups dump (validator binding) ---
        # Copied from _add_casing_row. Surfaces which field has a non-empty
        # help-block error BEFORE Save, so Save failures are debuggable.
        try:
            groups_state = await iframe.evaluate(
                """
                () => {
                    const form = document.querySelector('form.custom-form');
                    if (!form) return {error: 'no-form'};
                    const groups = [];
                    form.querySelectorAll('.form-group').forEach(grp => {
                        const labelEl = grp.querySelector('.control-label span');
                        const helpEl = grp.querySelector('.help-block');
                        const input = grp.querySelector('input, textarea');
                        groups.push({
                            fieldId: grp.id,
                            label: labelEl ? labelEl.textContent.trim() : null,
                            inputType: input ? input.type : null,
                            inputValue: input
                                ? ((input.type === 'checkbox' || input.type === 'radio')
                                    ? input.checked
                                    : input.value)
                                : null,
                            helpText: helpEl ? helpEl.textContent.trim() : null,
                        });
                    });
                    return {groups};
                }
                """
            )
            logger.warning("🔬 PP row-edit form groups pre-Save: %s", groups_state)
        except Exception as e:
            logger.warning(f"🔬 PP form groups dump failed: {e}")

        # --- Click Save ---
        save_button = iframe.locator(
            "div[role='toolbar'].btn-toolbar button.btn-primary"
        ).first
        try:
            await save_button.click(timeout=15000)
            logger.info("💾 PP add: Save clicked (row %d)", row_idx)
        except Exception as e:
            raise RuntimeError(f"PP Save click failed (row {row_idx}): {e}") from e

        # --- Snapshot 3: AFTER Save — row-count delta is authoritative ---
        await asyncio.sleep(1.5)
        post_add_count = await grid.locator(".react-grid-Row").count()
        row_edit_still_open = (
            await iframe.locator(f"#{self._PP_TYPE_FIELD_ID}").count() > 0
        )

        new_row_data = None
        if post_add_count > pre_add_count:
            last_row = grid.locator(".react-grid-Row").nth(post_add_count - 1)
            try:
                new_row_data = await last_row.evaluate(
                    """
                    (row) => {
                        const cells = row.querySelectorAll('.react-grid-Cell');
                        return Array.from(cells).map(c => {
                            const v = c.getAttribute('value');
                            return v !== null ? v : (c.textContent || '').trim();
                        });
                    }
                    """
                )
            except Exception as e:
                logger.debug(f"PP new-row cell capture failed: {e}")

        logger.info(
            f"📸 PP post-Save (row {row_idx}): pre={pre_add_count} "
            f"post={post_add_count} delta={post_add_count - pre_add_count} "
            f"row_edit_still_open={row_edit_still_open} new_row={new_row_data}"
        )

        if post_add_count == pre_add_count + 1:
            logger.info(f"✅ PP add: row {row_idx} landed in grid ({new_row_data})")
        elif post_add_count > pre_add_count + 1:
            raise RuntimeError(
                f"PP add row {row_idx}: post-count {post_add_count} > expected "
                f"{pre_add_count + 1} (RRC added more rows than requested)"
            )
        else:
            raise RuntimeError(
                f"PP add row {row_idx}: row did not land. "
                f"pre={pre_add_count} post={post_add_count} "
                f"row_edit_still_open={row_edit_still_open}"
            )

        await asyncio.sleep(0.5)  # small settle for parent grid re-render

    async def _clear_casing_rows(self, iframe: Page, grid_selector: str):
        """Remove all existing rows from the Casing Record grid.

        The fresh-draft DOM doesn't show populated rows so the exact Remove
        control selector is unknown until runtime. Strategy:

        1. Detect data rows inside the grid via tbody>tr (common itemizer-grid
           pattern).
        2. On the first row, log the row's outer HTML so we can refine the
           selector if needed.
        3. Try a list of candidate selectors for the Remove control
           (``i.fa-trash``, ``button[title*=Remove]``, etc.) — first one that
           matches wins.
        4. Loop-remove until rows count reaches 0, with a timeout.

        First run on a clean draft = no-op. Subsequent runs exercise this path.
        """
        grid = iframe.locator(grid_selector)

        # --- DOM recon dump ---------------------------------------------
        # The user portal-verified that RRC prepopulates 4 Casing rows once
        # an API number is entered, but our prior tbody>tr selector saw 0.
        # Dump the parent grid's inner HTML so we can identify the actual
        # row structure + the Remove control (likely a row-checkbox + a
        # grid-level "Remove" button next to "+ Add"). Logged at WARNING
        # so it survives any INFO-level filtering in dev.
        try:
            await grid.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        try:
            inner = await grid.inner_html(timeout=5000)
            # Chunked dump — logger formatters / Docker line-length sometimes
            # silently truncate; emit in 4k slices so the full HTML is captured.
            chunk_size = 4000
            total_chunks = (len(inner) + chunk_size - 1) // chunk_size
            logger.warning(
                "🔎 Casing grid DOM dump (selector=%s, len=%d, chunks=%d)",
                grid_selector,
                len(inner),
                total_chunks,
            )
            for i in range(total_chunks):
                logger.warning(
                    "🔎 Casing grid DOM chunk %d/%d:\n%s",
                    i + 1,
                    total_chunks,
                    inner[i * chunk_size:(i + 1) * chunk_size],
                )
        except Exception as e:
            logger.warning(f"🔎 Casing grid DOM dump failed: {e}")

        # Row selector confirmed from the DOM dump: RRC's itemizer-grid uses
        # the react-data-grid library — rows are <div class="react-grid-Row">,
        # NOT tbody>tr. Each row has a leading react-grid-checkbox at the
        # frozen first cell.
        row_locator = grid.locator(".react-grid-Row")

        try:
            initial_count = await row_locator.count()
        except Exception as e:
            logger.warning(f"Casing clear: could not count rows ({e}) — assuming empty")
            return

        if initial_count == 0:
            logger.info("🧹 Casing clear: 0 existing rows (fresh draft, no-op)")
            return

        logger.warning("🧹 Casing clear: %d existing row(s) to remove", initial_count)

        # --- Phase 1: select all rows --------------------------------------
        # Prior attempt clicked the <label for="select-all-checkbox"> directly,
        # but the label is zero-dimension (empty text, no padding) so
        # Playwright's actionability check times out at 30s. Strategies below
        # are attempted in order; the first that yields an enabled Remove
        # button wins.
        #
        #   A. Direct check on the hidden <input> via Playwright's check()
        #      with force=True (bypasses the zero-dimension actionability
        #      check but still drives the React onChange path through the
        #      synthetic event Playwright dispatches).
        #   B. JS evaluate — set the input's `checked` via the prototype
        #      setter (so React's value tracker sees the change) and
        #      dispatch click + change events.
        #   C. Per-row label clicks for checkbox0..checkboxN-1.
        # CRITICAL: RRC reuses `#select-all-checkbox` and `checkbox0..N` IDs
        # across all 5 itemizer-grids on the W-3A form (Casing, Perforation,
        # Plugging Proposal, Area Review Q1, Area Review Q2). All strategies
        # below MUST scope to the casing `grid` locator — iframe-wide
        # selectors trigger strict-mode violations and (worse) JS
        # `document.querySelector` silently picks the FIRST grid, which is
        # almost never the casing grid.
        select_all_strategy = None
        try:
            select_all_input = grid.locator("input#select-all-checkbox").first
            await select_all_input.check(force=True, timeout=5000)
            select_all_strategy = "A: input.check(force=True)"
            logger.info("🧹 Casing clear: select-all strategy A succeeded (input.check force)")
        except Exception as e_a:
            logger.warning(f"🧹 Casing clear: strategy A failed: {e_a}")
            try:
                # Pass the grid element into JS so querySelector is scoped to
                # this grid, not the whole iframe.
                ok = await grid.evaluate(
                    """
                    (gridEl) => {
                        const cb = gridEl.querySelector('#select-all-checkbox');
                        if (!cb) return false;
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'checked'
                        ).set;
                        setter.call(cb, true);
                        cb.dispatchEvent(new Event('click', {bubbles: true}));
                        cb.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                    """
                )
                if ok:
                    select_all_strategy = "B: JS setter + dispatchEvent"
                    logger.info("🧹 Casing clear: select-all strategy B succeeded (JS setter)")
                else:
                    raise RuntimeError("strategy B: #select-all-checkbox not found in grid")
            except Exception as e_b:
                logger.warning(f"🧹 Casing clear: strategy B failed: {e_b}")
                try:
                    clicked_any = False
                    for i in range(initial_count):
                        try:
                            await grid.locator(
                                f"label[for='checkbox{i}']"
                            ).first.click(force=True, timeout=5000)
                            clicked_any = True
                        except Exception as e_row:
                            logger.warning(
                                f"🧹 Casing clear: strategy C row {i} click failed: {e_row}"
                            )
                    if not clicked_any:
                        raise RuntimeError("strategy C: no per-row labels clicked")
                    select_all_strategy = "C: per-row label clicks"
                    logger.info("🧹 Casing clear: select-all strategy C succeeded (per-row labels)")
                except Exception as e_c:
                    logger.warning(f"🧹 Casing clear: strategy C failed: {e_c}")
                    raise RuntimeError(
                        f"Casing clear: all select-all strategies failed "
                        f"(A: {e_a}; B: {e_b}; C: {e_c})"
                    )

        # Brief settle so React enables the Remove button after the
        # select-all state propagates.
        await asyncio.sleep(0.5)

        # --- Phase 2: click the grid-level Remove button -------------------
        # Header DOM (from the dump):
        #   <div class="btn-group">
        #     <button ...><i class="fa fa-plus-circle"/> Add </button>
        #     <button ...><i class="fa fa-minus-circle"/> Remove </button>
        #   </div>
        # The Remove button is initially `disabled=""`; selecting rows
        # enables it. We scope strictly to the grid (NOT iframe-wide) to
        # avoid hitting any other form-level Remove button.
        remove_button = grid.locator(
            "div.btn-group button:has(i.fa-minus-circle)"
        ).first
        if await remove_button.count() == 0:
            # Looser fallback: any button containing the fa-minus-circle icon.
            remove_button = grid.locator("button:has(i.fa-minus-circle)").first
        if await remove_button.count() == 0:
            raise RuntimeError(
                "Casing clear: Remove button not found inside grid"
            )

        # Verify Remove button has lost its `disabled` attribute. Poll up to
        # ~5s (10 * 0.5s) — React re-renders the button when the select-all
        # state propagates. If still disabled at the end, the selection
        # didn't register and we MUST raise so the caller doesn't proceed
        # to Add on top of a still-populated grid.
        disabled_attr = await remove_button.get_attribute("disabled")
        for _ in range(10):
            if disabled_attr is None:
                break
            await asyncio.sleep(0.5)
            disabled_attr = await remove_button.get_attribute("disabled")
        if disabled_attr is not None:
            raise RuntimeError(
                f"Casing clear: Remove button still disabled after "
                f"select-all (strategy={select_all_strategy})"
            )

        try:
            await remove_button.click()
            logger.info("🧹 Casing clear: clicked Remove button")
        except Exception as e:
            raise RuntimeError(f"Casing clear: Remove click failed: {e}") from e

        # --- Phase 3: handle optional confirmation modal -------------------
        # RRC SPA forms often pop a Bootstrap confirm dialog before bulk
        # destructive actions. Try a short list of common "confirm/yes/ok"
        # selectors; first-match wins. If no modal appears, this is a no-op.
        confirm_selectors = [
            ".modal.in button.btn-primary",
            ".modal.in button:has-text('Yes')",
            ".modal.in button:has-text('OK')",
            ".modal.in button:has-text('Confirm')",
            ".modal.in button:has-text('Remove')",
            ".modal-dialog button.btn-primary",
            "div[role='dialog'] button.btn-primary",
        ]
        await asyncio.sleep(0.5)  # let modal mount if one is coming
        for sel in confirm_selectors:
            try:
                btn = iframe.locator(sel).first
                if await btn.count() == 0:
                    continue
                if not await btn.is_visible():
                    continue
                await btn.click()
                logger.info(
                    "🧹 Casing clear: confirmed removal via modal '%s'", sel
                )
                break
            except Exception as e:
                logger.debug(f"Casing clear: confirm selector '{sel}' failed: {e}")
                continue

        # --- Phase 4: wait for row count to reach 0 ------------------------
        deadline_loops = 20  # ~10s at 0.5s cadence
        for _ in range(deadline_loops):
            try:
                current_count = await row_locator.count()
            except Exception:
                current_count = 0
            if current_count == 0:
                break
            await asyncio.sleep(0.5)

        # Final assertion — must reach 0 or we raise so the caller does NOT
        # proceed to Add on top of a still-populated grid (bug #2).
        try:
            final_count = await row_locator.count()
        except Exception:
            final_count = -1
        if final_count > 0:
            raise RuntimeError(
                f"Casing clear failed: {final_count} rows still present "
                f"after remove attempts (started with {initial_count}, "
                f"strategy={select_all_strategy})"
            )
        logger.info(
            "✅ Casing clear: successfully removed all rows (started with %d, strategy=%s)",
            initial_count,
            select_all_strategy,
        )

    async def _set_input_via_native_setter(
        self, iframe: Page, wrapper_selector: str, value
    ) -> None:
        """Set value on a React-controlled <input>/<textarea> by calling the
        native property setter and dispatching the synthetic events React
        listens for (input/change/blur).

        Background: RRC's row-edit form uses React-controlled inputs whose
        ``onChange`` only fires when the DOM property descriptor's setter is
        invoked AND a bubbling event is dispatched. Playwright's ``.fill()``
        sets ``value`` directly, which React's value-tracker ignores — the
        text appears in the box but never enters React state, so Save runs
        validation against an empty model and refuses to detach the row-edit
        route. Same root cause as the select-all bug.

        Raises RuntimeError on failure so the caller can surface it.
        """
        result = await iframe.evaluate(
            """
            ({selector, value}) => {
                const wrapper = document.querySelector(selector);
                if (!wrapper) return {ok: false, reason: 'wrapper-not-found'};
                const input = wrapper.querySelector('input, textarea');
                if (!input) return {ok: false, reason: 'input-not-found'};
                const proto = input.tagName === 'TEXTAREA'
                    ? window.HTMLTextAreaElement.prototype
                    : window.HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(input, value);
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
                input.dispatchEvent(new Event('blur', {bubbles: true}));
                return {ok: true};
            }
            """,
            {"selector": wrapper_selector, "value": str(value)},
        )
        if not result.get("ok"):
            raise RuntimeError(
                f"native-setter failed on {wrapper_selector}: {result.get('reason')}"
            )

    async def _add_casing_row(self, iframe: Page, grid_selector: str, row: Dict[str, Any]):
        """Click Add on the Casing grid, fill the row-edit form, click Save.

        Scoped strictly to ``grid_selector`` to avoid the Area Review bug
        (don't iterate ``div.itemizer-grid`` form-wide).
        """
        grid = iframe.locator(grid_selector)

        # === EXPERIMENTAL PROBE — REMOVE AFTER DIAGNOSIS ===
        # Test hypothesis: RRC silently-requires hole_size, cement_sacks,
        # top_of_cement, anticipated_recovery on every Casing row, and
        # Save's onClick handler no-ops when any are empty without
        # surfacing a help-block error. Probes ONLY fire when the field
        # is None, so once the kernel/adapter populate them properly,
        # the probes auto-disable. Safe to leave in until real values land.
        probe_applied = []
        if row.get("hole_size") is None:
            row["hole_size"] = "17 1/2"  # plausible 17-1/2" surface hole, 6 chars
            probe_applied.append("hole_size=17 1/2")
        if row.get("cement_sacks") is None:
            row["cement_sacks"] = 100
            probe_applied.append("cement_sacks=100")
        if row.get("top_of_cement") is None:
            row["top_of_cement"] = 0
            probe_applied.append("top_of_cement=0")
        if row.get("anticipated_recovery") is None:
            row["anticipated_recovery"] = 0
            probe_applied.append("anticipated_recovery=0")
        if probe_applied:
            logger.warning(
                f"🧪 PROBE: filled None fields with hardcoded probes: "
                f"{', '.join(probe_applied)}"
            )
        # === END PROBE ===

        # --- Snapshot 1: row count BEFORE clicking Add ---
        # The authoritative success signal for an Add attempt is the
        # row-count delta in this grid: post == pre + 1 means the row
        # landed, regardless of what the sentinel-detach signal says.
        # (We've seen the sentinel lie in both directions.)
        pre_add_count = await grid.locator(".react-grid-Row").count()
        logger.info(f"📸 Casing pre-Add row count: {pre_add_count}")

        # --- Click Add (scoped to this grid) ---
        add_button = grid.locator("button:has-text('Add')").first
        await add_button.scroll_into_view_if_needed()
        await add_button.click()
        logger.info("➕ Casing add: clicked Add")

        # --- Wait for row-edit form sentinel ---
        # RRC wraps real <input type="radio"> elements in custom widgets where
        # the underlying input is visually hidden (display:none / opacity:0)
        # and a sibling <span> renders the UI. So we wait for state="attached"
        # not "visible" — the input being present in the DOM means the
        # row-edit route has mounted.
        sentinel = iframe.locator(self._CASING_ROW_SENTINEL).first
        try:
            await sentinel.wait_for(state="attached", timeout=10000)
        except Exception as e:
            raise RuntimeError(f"Casing row-edit form did not appear within 10s: {e}")
        # Brief settle for React to wire up onChange handlers and complete
        # hydration. Without this, fast Playwright .check()/.fill() calls can
        # land before React attaches listeners, leaving the form's internal
        # state empty and triggering save-side validation rejection.
        await asyncio.sleep(1.0)

        # --- Fill fields (in DOM order) ---
        # Type (radio): always required.
        # RRC's custom radio/checkbox widgets visually hide the native <input>
        # and render a sibling <span> + <div class="radio-label">. Clicking
        # the hidden input directly (even with force=True) doesn't propagate
        # the React onChange, so we click the parent <label> instead — same
        # event chain a human triggers.
        type_value = row.get("type") or "Casing"
        type_label = iframe.locator(
            f"#{self._CASING_FIELD_TYPE} label:has(input[type='radio'][value={type_value!r}])"
        ).first
        if await type_label.count():
            await type_label.click()
        else:
            logger.warning("Casing row: Type label for value=%r not found", type_value)

        # Sub Type (checkbox DV Tool): only when explicitly requested.
        if row.get("sub_type") == "DV Tool":
            sub_label = iframe.locator(
                f"#{self._CASING_FIELD_SUBTYPE} label:has(input[type='checkbox'])"
            ).first
            if await sub_label.count():
                await sub_label.click()

        # All non-radio/checkbox fields below are filled via the native-setter
        # helper. Playwright's `.fill()` sets the value DOM property directly,
        # which React's value-tracker ignores — the field looks populated but
        # React state stays empty, so Save runs validation against an empty
        # model and the row-edit route never detaches. See
        # `_set_input_via_native_setter` docstring.

        # Casing Size (text). Empty string is acceptable if not provided.
        casing_size = row.get("casing_size")
        if casing_size not in (None, ""):
            await self._set_input_via_native_setter(
                iframe, f"#{self._CASING_FIELD_SIZE}", casing_size
            )

        # Hole Size (text) — skip if None.
        hole_size = row.get("hole_size")
        if hole_size is not None:
            await self._set_input_via_native_setter(
                iframe, f"#{self._CASING_FIELD_HOLE_SIZE}", hole_size
            )

        # Depth (number) — always required.
        depth = row.get("depth")
        if depth is not None:
            await self._set_input_via_native_setter(
                iframe, f"#{self._CASING_FIELD_DEPTH}", depth
            )

        # Cement sacks (number) — skip if None (kernel may not emit yet).
        sacks = row.get("cement_sacks")
        if sacks is not None:
            await self._set_input_via_native_setter(
                iframe, f"#{self._CASING_FIELD_CEMENT_SACKS}", sacks
            )

        # Top of Cement (number) — skip if None.
        toc = row.get("top_of_cement")
        if toc is not None:
            await self._set_input_via_native_setter(
                iframe, f"#{self._CASING_FIELD_TOC}", toc
            )

        # Top of Cement Determined By (radio) — always "Calculated".
        toc_method = row.get("top_of_cement_method") or "Calculated"
        toc_label = iframe.locator(
            f"#{self._CASING_FIELD_TOC_METHOD} label:has(input[type='radio'][value={toc_method!r}])"
        ).first
        if await toc_label.count():
            await toc_label.click()

        # Anticipated Casing Recovery (number) — skip if None.
        recovery = row.get("anticipated_recovery")
        if recovery is not None:
            await self._set_input_via_native_setter(
                iframe, f"#{self._CASING_FIELD_RECOVERY}", recovery
            )

        # Additives (textarea) — skip if None.
        additives = row.get("additives")
        if additives is not None:
            await self._set_input_via_native_setter(
                iframe, f"#{self._CASING_FIELD_ADDITIVES}", additives
            )

        # --- Pre-Save diagnostic: dump row-edit form state ---
        # Confirms the native-setter fills landed in React state (the
        # value attribute reflects React state for controlled inputs) and
        # surfaces any RRC help-block validation errors BEFORE we click
        # Save. Without this, Save failures are blind — we can't tell
        # whether the form was empty, had wrong types, or had stale state.
        try:
            form_state = await iframe.evaluate(
                """
                () => {
                    const form = document.querySelector('form.custom-form');
                    if (!form) return {error: 'no-form'};
                    const fields = {};
                    form.querySelectorAll('input, textarea').forEach(el => {
                        const id = el.id || el.name;
                        if (id) {
                            fields[id] = (el.type === 'checkbox' || el.type === 'radio')
                                ? el.checked
                                : el.value;
                        }
                    });
                    const errors = [];
                    form.querySelectorAll('.help-block').forEach(el => {
                        const t = (el.textContent || '').trim();
                        if (t) errors.push(t);
                    });
                    return {fields, errors};
                }
                """
            )
            logger.warning("🔬 Row-edit form state pre-Save: %s", form_state)
        except Exception as e:
            logger.warning(f"🔬 Row-edit form state dump failed: {e}")

        # Richer validator-binding dump: walks `.form-group` elements and
        # pairs each field's label + input value + help-block text so we
        # can identify exactly WHICH field has a non-empty validation
        # error. The previous dump collected help-block text form-wide,
        # making it impossible to bind errors to specific fields.
        try:
            groups_state = await iframe.evaluate(
                """
                () => {
                    const form = document.querySelector('form.custom-form');
                    if (!form) return {error: 'no-form'};
                    const groups = [];
                    form.querySelectorAll('.form-group').forEach(grp => {
                        const labelEl = grp.querySelector('.control-label span');
                        const helpEl = grp.querySelector('.help-block');
                        const input = grp.querySelector('input, textarea');
                        groups.push({
                            fieldId: grp.id,
                            label: labelEl ? labelEl.textContent.trim() : null,
                            inputType: input ? input.type : null,
                            inputValue: input
                                ? ((input.type === 'checkbox' || input.type === 'radio')
                                    ? input.checked
                                    : input.value)
                                : null,
                            helpText: helpEl ? helpEl.textContent.trim() : null,
                        });
                    });
                    return {groups};
                }
                """
            )
            logger.warning("🔬 Row-edit form groups pre-Save: %s", groups_state)
        except Exception as e:
            logger.warning(f"🔬 Row-edit form groups dump failed: {e}")

        # --- Click Save ---
        # Use the simpler selector that earlier dispatches confirmed
        # resolves. The previous `:has(> form.custom-form)` traversal
        # gave Playwright nothing to compute a bounding box from. The
        # toolbar selector is unique inside the row-edit route's iframe.
        save_button = iframe.locator(
            "div[role='toolbar'].btn-toolbar button.btn-primary"
        ).first
        save_locator = save_button  # alias for downstream code

        # --- React 15 fiber dump (pre-click) -------------------------
        # RRC is React 15 / pre-Fiber — keys are `__reactInternalInstance$...`.
        # Props live at `instance._currentElement.props`, NOT on the
        # instance itself. We dump BEFORE the click so we can see the
        # Save button's onClick binding + the Casing Size input's React
        # `value` prop regardless of whether the click succeeds.
        try:
            fiber_pre = await iframe.evaluate(
                """
                () => {
                    function getReact15Props(el) {
                        if (!el) return null;
                        const key = Object.keys(el).find(k =>
                            k.startsWith('__reactInternalInstance$')
                        );
                        if (!key) return {error: 'no-react-15-key'};
                        const instance = el[key];
                        if (!instance) return {error: 'no-instance', key};
                        const elt = instance._currentElement;
                        if (!elt) return {
                            error: 'no-currentElement',
                            instanceKeys: Object.keys(instance).slice(0,15),
                        };
                        const props = elt.props;
                        if (!props) return {error: 'no-props', eltKeys: Object.keys(elt)};
                        const out = {};
                        for (const k in props) {
                            const v = props[k];
                            if (typeof v === 'function') out[k] = '<function>';
                            else if (v === null || v === undefined) out[k] = v;
                            else if (['string','number','boolean'].includes(typeof v)) out[k] = v;
                            else if (Array.isArray(v)) out[k] = `<array len=${v.length}>`;
                            else if (typeof v === 'object') out[k] = `<object keys=${Object.keys(v).slice(0,8).join(',')}>`;
                        }
                        return out;
                    }
                    const toolbar = document.querySelector("div[role='toolbar'].btn-toolbar");
                    const saveBtn = toolbar ? toolbar.querySelector('button.btn-primary') : null;
                    const sizeWrap = document.querySelector('#field-214fdbac-28f5-44f3-a612-21a39a30d037');
                    const sizeInput = sizeWrap ? sizeWrap.querySelector('input') : null;
                    return {
                        saveBtn: saveBtn ? getReact15Props(saveBtn) : 'not-found',
                        saveBtnOuterHTML: saveBtn ? saveBtn.outerHTML.slice(0, 250) : null,
                        sizeInputReactProps: sizeInput ? getReact15Props(sizeInput) : 'not-found',
                        sizeInputDomValue: sizeInput ? sizeInput.value : null,
                    };
                }
                """
            )
            logger.warning(f"🔬 React 15 fiber dump (pre-click): {fiber_pre}")
        except Exception as e:
            logger.warning(f"🔬 React 15 fiber dump (pre-click) failed: {e}")

        # --- onClick source + parent component owner chain ----------
        # The Save button's React onClick is bound (confirmed prior run),
        # but Save no-ops silently. Dump the handler's source (truncated)
        # AND walk the React 15 `_owner` chain to find which parent
        # component the handler likely reads state from. The owner chain
        # gives us the names of the parent components and their state
        # slot keys — points us at where the silent-required check lives.
        try:
            onclick_source = await iframe.evaluate(
                """
                () => {
                    const saveBtn = document.querySelector(
                        "div[role='toolbar'].btn-toolbar button.btn-primary"
                    );
                    if (!saveBtn) return {error: 'no-save-btn'};
                    const key = Object.keys(saveBtn).find(k =>
                        k.startsWith('__reactInternalInstance$')
                    );
                    if (!key) return {error: 'no-react-key'};
                    const instance = saveBtn[key];
                    if (!instance) return {error: 'no-instance'};

                    const elt = instance._currentElement;
                    const onClickStr = (elt && elt.props && elt.props.onClick)
                        ? elt.props.onClick.toString()
                        : null;

                    // Walk _owner chain — the component that *rendered*
                    // this element (parent in JSX tree, not DOM tree).
                    let owner = elt && elt._owner;
                    const ownerChain = [];
                    let depth = 0;
                    while (owner && depth < 8) {
                        const ownerType = owner._currentElement && owner._currentElement.type;
                        const ownerName = ownerType
                            ? (typeof ownerType === 'string'
                                ? ownerType
                                : ownerType.displayName || ownerType.name || '<anon>')
                            : '<unknown>';
                        const ownerState = owner._instance && owner._instance.state
                            ? Object.keys(owner._instance.state)
                            : null;
                        ownerChain.push({depth, name: ownerName, stateKeys: ownerState});
                        owner = owner._currentElement && owner._currentElement._owner;
                        depth++;
                    }

                    return {
                        onClickSource: onClickStr ? onClickStr.slice(0, 1200) : null,
                        ownerChain,
                    };
                }
                """
            )
            logger.warning(f"🔬 Save onClick + owner chain: {onclick_source}")
        except Exception as e:
            logger.warning(f"🔬 onClick + owner-chain dump failed: {e}")

        # Log locator count for diagnostic clarity. Earlier dispatches
        # showed multiple `btn-primary` candidates iframe-wide (one per
        # itemizer-grid route); `.first` picks the visible row-edit one.
        try:
            save_count = await save_button.count()
            logger.info(f"💾 Casing add: save button locator resolved to {save_count} element(s)")
        except Exception as e:
            logger.warning(f"💾 Casing add: save button count failed: {e}")

        strategies_tried: list[str] = []

        # Strategy A: Playwright's standard `.click()` — real mouse
        # simulation (move → press → release) via CDP. Produces
        # isTrusted=true events. The earlier `force=True` bypassed
        # actionability checks AND skipped the real mouse simulation;
        # this version uses both.
        try:
            await save_button.click(timeout=15000)
            strategies_tried.append("A-real-click")
            logger.info("💾 Casing add: strategy A (Playwright real click) completed")
        except Exception as e:
            logger.warning(f"💾 Casing add: strategy A real click failed: {e}")

        # Strategy B-jsclick: in-page btn.click() from JS context — bypasses
        # Playwright's synthetic event layer; React picks up the click as
        # if a real user fired it.
        try:
            clicked = await iframe.evaluate(
                """
                () => {
                    const form = document.querySelector('form.custom-form');
                    if (!form) return {ok: false, reason: 'no-form'};
                    const wrapper = form.parentElement;
                    const toolbar = wrapper.querySelector("div[role='toolbar'].btn-toolbar");
                    if (!toolbar) return {ok: false, reason: 'no-toolbar'};
                    const btn = toolbar.querySelector('button.btn-primary');
                    if (!btn) return {ok: false, reason: 'no-save-btn'};
                    btn.click();
                    return {ok: true};
                }
                """
            )
            if clicked.get("ok"):
                strategies_tried.append("B-jsclick")
                logger.info("💾 Casing add: strategy B-jsclick (JS click) fired")
            else:
                logger.warning(
                    f"💾 Casing add: strategy B-jsclick failed: {clicked.get('reason')}"
                )
        except Exception as e:
            logger.warning(f"💾 Casing add: strategy B-jsclick evaluate failed: {e}")

        # Strategy C-mouseevent: manual MouseEvent dispatch (mousedown +
        # mouseup + click). Some React handlers listen to mousedown rather
        # than click.
        try:
            clicked = await iframe.evaluate(
                """
                () => {
                    const form = document.querySelector('form.custom-form');
                    if (!form) return {ok: false, reason: 'no-form'};
                    const wrapper = form.parentElement;
                    const toolbar = wrapper.querySelector("div[role='toolbar'].btn-toolbar");
                    if (!toolbar) return {ok: false, reason: 'no-toolbar'};
                    const btn = toolbar.querySelector('button.btn-primary');
                    if (!btn) return {ok: false, reason: 'no-save-btn'};
                    const opts = {bubbles: true, cancelable: true, view: window};
                    btn.dispatchEvent(new MouseEvent('mousedown', opts));
                    btn.dispatchEvent(new MouseEvent('mouseup', opts));
                    btn.dispatchEvent(new MouseEvent('click', opts));
                    return {ok: true};
                }
                """
            )
            if clicked.get("ok"):
                strategies_tried.append("C-mouseevent")
                logger.info("💾 Casing add: strategy C-mouseevent (MouseEvent dispatch) fired")
            else:
                logger.warning(
                    f"💾 Casing add: strategy C-mouseevent dispatch failed: {clicked.get('reason')}"
                )
        except Exception as e:
            logger.warning(f"💾 Casing add: strategy C-mouseevent evaluate failed: {e}")

        # --- Snapshot 3: AFTER Save attempts ---
        # Let RRC settle — either the row-edit form closes and parent grid
        # re-renders, OR validation kept the form open and nothing landed.
        await asyncio.sleep(1.5)

        post_add_count = await grid.locator(".react-grid-Row").count()
        sentinel_still_attached = (
            await iframe.locator(self._CASING_ROW_SENTINEL).count() > 0
        )

        # If a row landed, capture its visible cell values for log-side
        # verification (eliminates the portal-verify step).
        new_row_data = None
        if post_add_count > pre_add_count:
            last_row = grid.locator(".react-grid-Row").nth(post_add_count - 1)
            try:
                new_row_data = await last_row.evaluate(
                    """
                    (row) => {
                        const cells = row.querySelectorAll('.react-grid-Cell');
                        return Array.from(cells).map(c => {
                            const v = c.getAttribute('value');
                            return v !== null ? v : (c.textContent || '').trim();
                        });
                    }
                    """
                )
            except Exception as e:
                logger.debug(f"new-row cell capture failed: {e}")

        logger.info(
            f"📸 Casing post-Save: pre={pre_add_count} post={post_add_count} "
            f"delta={post_add_count - pre_add_count} "
            f"row_edit_still_open={sentinel_still_attached} "
            f"strategies_tried={strategies_tried} "
            f"new_row={new_row_data}"
        )

        # React fiber forensic — only when no row landed. Captures the
        # Save button's onClick prop binding + the Casing Size and Depth
        # input React props, so we can confirm (a) whether React even has
        # a click handler on the button or delegates from a parent, and
        # (b) whether our native-setter actually pushed values into
        # React's controlled-component state vs only the DOM.
        if post_add_count == pre_add_count:
            try:
                fiber_dump = await iframe.evaluate(
                    """
                    () => {
                        function getReactProps(el) {
                            if (!el) return null;
                            const key = Object.keys(el).find(k =>
                                k.startsWith('__reactProps$') ||
                                k.startsWith('__reactInternalInstance$')
                            );
                            if (!key) return {error: 'no-react-key', keys: Object.keys(el).slice(0,10)};
                            const props = el[key];
                            if (!props) return {error: 'no-props', key};
                            const out = {key};
                            for (const k in props) {
                                const v = props[k];
                                if (typeof v === 'function') out[k] = '<function>';
                                else if (v === null || ['string','number','boolean','undefined'].includes(typeof v)) out[k] = v;
                                else if (Array.isArray(v)) out[k] = `<array len=${v.length}>`;
                                else if (typeof v === 'object') out[k] = `<object keys=${Object.keys(v).join(',')}>`;
                            }
                            return out;
                        }
                        const form = document.querySelector('form.custom-form');
                        const wrapper = form ? form.parentElement : null;
                        const toolbar = wrapper ? wrapper.querySelector("div[role='toolbar'].btn-toolbar") : null;
                        const saveBtn = toolbar ? toolbar.querySelector('button.btn-primary') : null;
                        const sizeInputWrapper = document.querySelector('#field-214fdbac-28f5-44f3-a612-21a39a30d037');
                        const sizeInput = sizeInputWrapper ? sizeInputWrapper.querySelector('input') : null;
                        const depthInputWrapper = document.querySelector('#field-478d7da8-f81e-440d-978b-84491c74617f');
                        const depthInput = depthInputWrapper ? depthInputWrapper.querySelector('input') : null;
                        return {
                            saveBtn: saveBtn ? getReactProps(saveBtn) : 'not-found',
                            saveBtnDomHtml: saveBtn ? saveBtn.outerHTML.slice(0,200) : null,
                            sizeInputProps: sizeInput ? getReactProps(sizeInput) : 'not-found',
                            sizeInputDomValue: sizeInput ? sizeInput.value : null,
                            depthInputProps: depthInput ? getReactProps(depthInput) : 'not-found',
                            depthInputDomValue: depthInput ? depthInput.value : null,
                        };
                    }
                    """
                )
                logger.warning(f"🔬 React fiber forensic: {fiber_dump}")
            except Exception as e:
                logger.warning(f"🔬 React fiber forensic dump failed: {e}")

        # Authoritative pass/fail: row-count delta.
        if post_add_count == pre_add_count + 1:
            logger.info(f"✅ Casing add: row landed in grid ({new_row_data})")
        elif post_add_count > pre_add_count + 1:
            raise RuntimeError(
                f"Casing add: post-count {post_add_count} > expected "
                f"{pre_add_count + 1} (RRC added more rows than requested)"
            )
        else:
            raise RuntimeError(
                f"Casing add: row did not land. "
                f"pre={pre_add_count} post={post_add_count} "
                f"row_edit_still_open={sentinel_still_attached} "
                f"strategies_tried={strategies_tried}"
            )

        await asyncio.sleep(0.5)  # small settle for parent grid re-render

    async def _handle_agreement_section(self, iframe: Page):
        """Handle agreement checkbox (critical for form validity)."""
        
        try:
            agreement_config = self.form_selectors["agreement_section"]
            agreement_section = await iframe.query_selector(agreement_config.primary)
            if agreement_section:
                await agreement_section.scroll_into_view_if_needed()
                await asyncio.sleep(1)
                
                checkbox_config = self.form_selectors["agreement_checkbox"]
                agree_checkbox = await agreement_section.query_selector(checkbox_config.primary)
                if agree_checkbox:
                    try:
                        await agree_checkbox.click()
                    except:
                        await iframe.evaluate('element => element.click()', agree_checkbox)
                    
                    logger.info("Agreement checkbox checked (REQUIRED)")
                    self.result.add_log_entry("INFO", "Agreement accepted", step="form_filling")
                    await asyncio.sleep(1)
                else:
                    logger.error("Agreement checkbox not found - CRITICAL!")
                    raise FormSubmissionError("Agreement checkbox not found", step="agreement")
        except Exception as e:
            logger.error(f"Agreement section error: {str(e)}")
            raise FormSubmissionError(f"Agreement handling failed: {str(e)}", step="agreement")
    
    async def submit_form(self, multi_tab: bool = False) -> bool:
        """Submit W3A form or save as draft."""
        
        try:
            # Get the appropriate page and iframe
            if multi_tab:
                page = await self.tab_manager.switch_to_tab("rrc_form")
            else:
                page = self.context.pages[0]
            
            iframe_element = await page.query_selector('#receiver')
            iframe = await iframe_element.content_frame()
            
            # Check if this is test mode
            if self.result.form_data.test_mode:
                logger.info("Test mode: Form will auto-save when navigating away")
                return True  # Form auto-saves, no manual save needed
            else:
                return await self._submit_form(iframe)
                
        except Exception as e:
            error_msg = f"Form submission/save failed: {str(e)}"
            logger.error(error_msg)
            raise FormSubmissionError(error_msg, step="submission")
    
    async def _click_save_draft(self) -> bool:
        """Resolve the RRC iframe and click the Save button.

        Shared implementation used by both save_draft() and _save_as_draft().
        Raises RuntimeError if no visible+enabled Save button is found so that
        operators are alerted immediately rather than discovering blank RRC drafts.
        """
        # Resolve page via tab_manager when the rrc_form tab is open, otherwise
        # fall back to the first page in the browser context.
        if hasattr(self.tab_manager, 'switch_to_tab') and "rrc_form" in self.tab_manager.tabs:
            page = await self.tab_manager.switch_to_tab("rrc_form")
        else:
            page = self.context.pages[0]

        # Resolve the #receiver iframe; fall back to using the page directly if
        # the iframe element is absent (e.g. in unit-test environments).
        iframe_element = await page.query_selector('#receiver')
        if iframe_element is not None:
            iframe = await iframe_element.content_frame()
        else:
            iframe = page

        save_button_config = self.form_selectors["save_button"]
        selectors_to_try = [save_button_config.primary] + list(save_button_config.fallbacks)

        for selector in selectors_to_try:
            candidates = await iframe.query_selector_all(selector)
            for btn in candidates:
                try:
                    if await btn.is_visible() and await btn.is_enabled():
                        logger.info("Clicking RRC W3A Save button…")
                        await btn.click()
                        logger.info("RRC W3A Save button clicked — waiting for page to settle")
                        await iframe.wait_for_load_state('networkidle', timeout=15000)
                        return True
                except Exception as btn_e:
                    logger.warning(f"Save button candidate error: {btn_e}")
                    continue

        raise RuntimeError(
            "RRC W3A Save button not found — check selector or page state"
        )

    async def save_draft(self) -> bool:
        """Override base class save_draft to click the RRC Save button."""
        return await self._click_save_draft()

    async def _save_as_draft(self) -> bool:
        """Override base class _save_as_draft to click the RRC Save button."""
        return await self._click_save_draft()
    
    async def _submit_form(self, iframe: Page) -> bool:
        """Submit form to agency."""
        
        try:
            submit_config = self.form_selectors["submit_button"]
            submit_button = await iframe.query_selector(submit_config.primary)
            if submit_button:
                await submit_button.click()
                logger.info("Form submitted to RRC")
                await asyncio.sleep(5)
                await iframe.wait_for_load_state('networkidle', timeout=15000)
                
                # Look for confirmation or success indicators
                # This would need to be implemented based on RRC's response patterns
                
                return True
            else:
                raise FormSubmissionError("Submit button not found")
                
        except Exception as e:
            raise FormSubmissionError(f"Form submission failed: {str(e)}")
    
    # Override base class methods for RRC-specific behavior
    
    def get_workflow_steps(self, form_type: str, multi_tab: bool = False) -> List[WorkflowStep]:
        """Get RRC-specific workflow steps."""
        
        if form_type.upper() == "W3A":
            return RRC_FORM_CONFIGS["W3A"]["workflow"]
        else:
            return []
    
    def get_tab_configurations(self, form_type: str) -> List[TabConfig]:
        """Get RRC tab configurations for multi-tab workflows."""
        
        if form_type.upper() == "W3A":
            return RRC_FORM_CONFIGS["W3A"]["tab_config"]
        else:
            return []
    
    def get_selector_config(self, field_name: str) -> Optional[SelectorConfig]:
        """Get RRC selector configuration for a field."""
        
        return self.form_selectors.get(field_name)
    
    # Utility methods
    
    def supports_form_type(self, form_type: str) -> bool:
        """Check if this automator supports the given form type."""
        return form_type.upper() in RRC_FORM_CONFIGS
    
    def get_supported_forms(self) -> List[str]:
        """Get list of supported form types."""
        return list(RRC_FORM_CONFIGS.keys())
    
    def get_form_requirements(self, form_type: str) -> Dict[str, Any]:
        """Get requirements for a specific form type."""
        return RRC_FORM_CONFIGS.get(form_type.upper(), {})
