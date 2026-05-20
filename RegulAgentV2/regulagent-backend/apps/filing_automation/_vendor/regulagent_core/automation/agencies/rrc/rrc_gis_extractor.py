"""RRC GIS data extraction implementation."""

import asyncio
import re
from typing import Dict, List, Any
from playwright.async_api import Page
import logging

from ...base.gis_extractor import BaseGISExtractor
from ...base.data_models import GISData, AutomationResult
from ...exceptions import GISExtractionError
from .rrc_config import RRC_SELECTORS, GIS_EXTRACTION_PATTERNS, RRC_DEFAULTS


logger = logging.getLogger(__name__)


class RRCGISExtractor(BaseGISExtractor):
    """RRC-specific GIS data extraction from Texas RRC GIS Viewer."""
    
    def __init__(self, result: AutomationResult):
        super().__init__(result)
        
        # Override extraction patterns with RRC-specific patterns
        self.extraction_patterns.update(GIS_EXTRACTION_PATTERNS)
        
        # Set RRC-specific fallback data
        fallback_data = RRC_DEFAULTS["fallback_gis_data"]
        self.set_fallback_data(
            distance=fallback_data["distance"],
            direction=fallback_data["direction"],
            town=fallback_data["town"]
        )
    
    async def setup_gis_session(self, page: Page, search_params: Dict[str, Any]) -> bool:
        """Setup RRC GIS session and navigate to viewer."""
        
        try:
            logger.info("Setting up RRC GIS session")
            
            # GIS page should already be loaded by tab manager
            # Wait for page to be fully loaded
            await page.wait_for_load_state('networkidle', timeout=30000)
            await asyncio.sleep(5)  # Additional wait for GIS viewer to initialize
            
            self.result.add_log_entry(
                "INFO", 
                "RRC GIS viewer loaded successfully",
                step="gis_setup"
            )
            
            return True
            
        except Exception as e:
            error_msg = f"RRC GIS setup failed: {str(e)}"
            logger.error(error_msg)
            self.result.add_log_entry("ERROR", error_msg, step="gis_setup")
            return False
    
    async def search_location(self, page: Page, identifier: str) -> bool:
        """Search for well location using API number in RRC GIS."""
        
        try:
            logger.info(f"Searching for API {identifier} in RRC GIS")
            
            # Clean API number (remove 42- prefix if present)
            clean_api = identifier.replace("42-", "").replace("-", "")
            
            # Find search input using configured selector
            search_config = RRC_SELECTORS["gis_search_input"]
            search_element = await self.selector_engine.find_element(page, search_config)
            
            # Perform search
            await search_element.fill(clean_api)
            await search_element.press('Enter')
            await asyncio.sleep(5)  # Wait for search results
            
            self.result.add_log_entry(
                "INFO",
                f"GIS search completed for API: {clean_api}",
                step="gis_search"
            )
            
            return True
            
        except Exception as e:
            error_msg = f"RRC GIS search failed: {str(e)}"
            logger.warning(error_msg)
            self.result.add_log_entry("WARNING", error_msg, step="gis_search")
            return False
    
    def get_extraction_workflow(self) -> List[Dict[str, Any]]:
        """Get RRC GIS extraction workflow steps."""
        
        return [
            {
                "name": "activate_identify_tool",
                "type": "click_tool",
                "config": {
                    "selectors": [
                        'button[title*="Identify" i]',
                        'button[aria-label*="Identify" i]',
                        '.identify-tool',
                        'button:has-text("i")',
                        '[class*="identify"]',
                        'button[title*="Info" i]'
                    ],
                    "description": "GIS identify tool"
                },
                "wait_after": 2,
                "required": False
            },
            {
                "name": "select_wells_layer", 
                "type": "select_layer",
                "config": {
                    "layer_name": "wells",
                    "description": "Wells layer selection"
                },
                "wait_after": 2,
                "required": False
            },
            {
                "name": "click_well_marker",
                "type": "click_location",
                "config": {
                    "selectors": [
                        '.well-marker',
                        '.esri-graphic',
                        '[class*="well"]', 
                        'circle[fill*="blue"]',
                        'circle[fill*="green"]'
                    ],
                    "description": "Well location marker"
                },
                "wait_after": 3,
                "required": False
            },
            {
                "name": "extract_identity_popup",
                "type": "extract_from_popup",
                "config": {
                    "popup_selectors": [
                        '[class*="identity" i]',
                        '[title*="identity" i]',
                        '.popup-content',
                        '.info-window',
                        '.esri-popup'
                    ],
                    "description": "GIS Identity Results popup"
                },
                "wait_after": 2,
                "required": False
            },
            {
                "name": "click_drilling_permits",
                "type": "click_drilling_permits",
                "config": {
                    "selectors": [
                        'a:has-text("Drilling Permits")',
                        'button:has-text("Drilling Permits")',
                        'span:has-text("Drilling Permits")',
                        '[title*="Drilling Permits" i]',
                        'td:has-text("Drilling Permits")',
                        'div:has-text("Drilling Permits")'
                    ],
                    "description": "Drilling Permits link"
                },
                "wait_after": 5,
                "required": False
            },
            {
                "name": "click_lease_name",
                "type": "click_lease_name",
                "config": {
                    "selectors": [
                        'a[style*="color" i]',  # Highlighted links
                        'span[style*="color" i]',
                        'td > a',  # Links in table cells
                        'a:has-text("UNIVERSITY")',  # Known lease names
                        '.highlighted',
                        '.selected'
                    ],
                    "description": "Highlighted lease name link"
                },
                "wait_after": 5,
                "required": False
            },
            {
                "name": "extract_surface_location",
                "type": "extract_from_page",
                "config": {
                    "description": "Surface location information extraction"
                },
                "wait_after": 3,
                "required": True
            },
            {
                "name": "extract_from_tables",
                "type": "extract_from_table",
                "config": {
                    "table_keywords": ["distance", "town", "location", "surface"],
                    "description": "Extract from data tables"
                },
                "wait_after": 1,
                "required": False
            }
        ]
    
    # Custom extraction step implementations
    
    async def _click_drilling_permits(self, page: Page, config: Dict[str, Any]):
        """Click drilling permits link in GIS Identity Results."""
        
        selectors = config.get("selectors", [])
        drilling_permits_clicked = False
        
        for selector in selectors:
            try:
                drilling_permits = await page.query_selector(selector)
                if drilling_permits:
                    await drilling_permits.click()
                    logger.info("Clicked 'Drilling Permits' in GIS Identity Results")
                    self.result.add_log_entry(
                        "INFO",
                        "Clicked Drilling Permits link",
                        step="gis_extraction"
                    )
                    drilling_permits_clicked = True
                    break
            except Exception as e:
                logger.debug(f"Drilling permits selector failed: {selector} - {str(e)}")
                continue
        
        if not drilling_permits_clicked:
            logger.warning("'Drilling Permits' link not found, continuing with available data")
    
    async def _click_lease_name(self, page: Page, config: Dict[str, Any]):
        """Click highlighted lease name link."""
        
        selectors = config.get("selectors", [])
        
        for selector in selectors:
            try:
                lease_link = await page.query_selector(selector)
                if lease_link:
                    lease_text = await lease_link.inner_text()
                    if len(lease_text) > 5:  # Meaningful lease name
                        await lease_link.click()
                        logger.info(f"Clicked lease name: {lease_text}")
                        self.result.add_log_entry(
                            "INFO",
                            f"Clicked lease name: {lease_text}",
                            step="gis_extraction"
                        )
                        break
            except Exception as e:
                logger.debug(f"Lease name selector failed: {selector} - {str(e)}")
                continue
    
    def _merge_extraction_data(
        self,
        gis_data: GISData,
        raw_data: Dict[str, Any],
        extraction_types: List[str]
    ):
        """Override parent method with RRC-specific extraction logic."""
        
        # Store raw data
        gis_data.raw_data.update(raw_data)
        
        # Extract structured data using RRC patterns
        for extraction_type in extraction_types:
            if extraction_type == "distance_direction":
                self._extract_rrc_distance_direction(gis_data, raw_data)
            elif extraction_type == "well_type":
                self._extract_rrc_well_type(gis_data, raw_data)
            elif extraction_type == "coordinates":
                self._extract_coordinates(gis_data, raw_data)
        
        # Save extraction data for debugging
        self._save_extraction_debug_data(raw_data)
    
    def _extract_rrc_distance_direction(self, gis_data: GISData, raw_data: Dict[str, Any]):
        """Extract distance, direction, and town using RRC-specific patterns."""
        
        # Combine all text content
        all_text = " ".join(str(value) for value in raw_data.values() if isinstance(value, str))
        
        # Try comprehensive patterns first
        patterns = self.extraction_patterns["distance_direction"]
        for pattern in patterns:
            match = re.search(pattern, all_text, re.IGNORECASE)
            if match:
                groups = match.groups()
                if len(groups) >= 3:
                    gis_data.distance = groups[0]
                    gis_data.direction = groups[-2].upper()  # Direction is usually second-to-last
                    gis_data.town = groups[-1].strip().title()  # Town is usually last
                    gis_data.extraction_method = "regex_pattern_comprehensive"
                    gis_data.confidence_score = 0.8
                    
                    logger.info(f"Extracted complete location: {gis_data.location_string}")
                    self.result.add_log_entry(
                        "INFO",
                        f"GIS extraction successful: {gis_data.location_string}",
                        step="gis_extraction"
                    )
                    return
        
        # Try separate extraction if comprehensive failed
        if not gis_data.distance:
            logger.info("Trying separate distance/direction/town extraction")
            
            # Extract distance separately
            distance_patterns = self.extraction_patterns["distance_only"]
            for pattern in distance_patterns:
                match = re.search(pattern, all_text, re.IGNORECASE)
                if match:
                    gis_data.distance = match.group(1)
                    logger.debug(f"Extracted distance: {gis_data.distance} miles")
                    break
            
            # Extract direction separately  
            direction_patterns = self.extraction_patterns["direction_only"]
            for pattern in direction_patterns:
                match = re.search(pattern, all_text, re.IGNORECASE)
                if match:
                    gis_data.direction = match.group(1).upper()
                    logger.debug(f"Extracted direction: {gis_data.direction}")
                    break
            
            # Extract town separately
            town_patterns = self.extraction_patterns["town_patterns"]
            for pattern in town_patterns:
                match = re.search(pattern, all_text, re.IGNORECASE)
                if match:
                    gis_data.town = match.group(1).strip().title()
                    logger.debug(f"Extracted town: {gis_data.town}")
                    break
            
            # If we got some data via separate extraction
            if gis_data.distance or gis_data.direction or gis_data.town:
                gis_data.extraction_method = "regex_pattern_separate"
                gis_data.confidence_score = 0.6
                
                logger.info(f"Partial extraction: {gis_data.distance} miles {gis_data.direction} of {gis_data.town}")
    
    def _extract_rrc_well_type(self, gis_data: GISData, raw_data: Dict[str, Any]):
        """Extract well type using RRC-specific logic."""
        
        all_text = " ".join(str(value) for value in raw_data.values() if isinstance(value, str)).lower()
        
        # Use RRC well type patterns
        patterns = self.extraction_patterns["well_type"]
        for pattern in patterns:
            match = re.search(pattern, all_text, re.IGNORECASE)
            if match:
                well_type = match.group(1).title()
                gis_data.well_type = well_type
                logger.debug(f"Extracted well type: {well_type}")
                return
        
        # Fallback to simple keyword matching
        if 'oil' in all_text:
            gis_data.well_type = 'Oil'
        elif 'gas' in all_text:
            gis_data.well_type = 'Gas'
        elif 'water' in all_text:
            gis_data.well_type = 'Water'
        elif 'injection' in all_text:
            gis_data.well_type = 'Injection'
        
        if gis_data.well_type:
            logger.debug(f"Extracted well type (fallback): {gis_data.well_type}")
    
    def _save_extraction_debug_data(self, raw_data: Dict[str, Any]):
        """Save extraction data for debugging (mimics original script behavior)."""
        
        try:
            # Save key content for debugging if available
            if "page_content" in raw_data:
                with open("gis_data_extraction.html", "w", encoding="utf-8") as f:
                    f.write(raw_data["page_content"])
                logger.debug("Saved GIS extraction content for debugging")
            
            if "popup_html" in raw_data:
                with open("gis_surface_location_data.html", "w", encoding="utf-8") as f:
                    f.write(raw_data["popup_html"])
                logger.debug("Saved GIS surface location data for debugging")
                
        except Exception as e:
            logger.warning(f"Failed to save debug data: {e}")
    
    # RRC-Specific GIS Circle and Modal Handling Methods
    
    async def find_and_click_well_circle(self, page: Page, api_number: str) -> bool:
        """
        🎯 NEW METHOD: Find and click the GIS circle for a specific well.
        
        This handles the dynamic SVG circle selection workflow you described:
        1. Find potential well marker circles (cyan/blue colored)
        2. Click each circle to test popup content
        3. Verify popup contains the target API number
        4. Use learning system to remember successful patterns
        """
        
        try:
            logger.info(f"🔍 Searching for well circle for API: {api_number}")
            
            # Multiple strategies for finding well circles
            circle_selectors = [
                # Based on your SVG example - cyan filled circles
                'circle[fill*="rgb(25, 255, 255)"]',  # Cyan fill from your example
                'circle[stroke*="rgb(0, 255, 255)"]', # Cyan stroke 
                'circle[fill*="cyan"]',
                'circle[r="5"]',  # 5px radius from your example
                'circle[fill-opacity="1"]',
                # Fallback to any circles
                'circle'
            ]
            
            circles_found = []
            
            # Try each selector strategy
            for selector in circle_selectors:
                try:
                    circles = await page.query_selector_all(selector)
                    if circles:
                        logger.debug(f"Found {len(circles)} circles with selector: {selector}")
                        circles_found.extend(circles)
                        break  # Use first successful selector
                except Exception as e:
                    logger.debug(f"Selector {selector} failed: {e}")
                    continue
            
            if not circles_found:
                logger.warning("No well marker circles found on page")
                return False
            
            logger.info(f"Testing {len(circles_found)} potential well circles...")
            
            # Test each circle by clicking and checking popup
            for i, circle in enumerate(circles_found):
                try:
                    # Get circle coordinates for learning context
                    circle_info = await circle.evaluate('''
                        (element) => ({
                            cx: element.getAttribute('cx') || 'unknown',
                            cy: element.getAttribute('cy') || 'unknown',
                            fill: element.getAttribute('fill') || 'unknown',
                            r: element.getAttribute('r') || 'unknown'
                        })
                    ''')
                    
                    logger.debug(f"Testing circle {i+1}/{len(circles_found)}: cx={circle_info['cx']}, cy={circle_info['cy']}")
                    
                    # Click the circle
                    await circle.click()
                    await page.wait_for_timeout(1500)  # Wait for popup/modal
                    
                    # Check if popup contains our API number
                    if await self._verify_api_in_popup(page, api_number):
                        logger.success(f"✅ Found matching well circle! Coordinates: cx={circle_info['cx']}, cy={circle_info['cy']}")
                        return True  # Success - popup is open for the right well
                    
                    # Close popup/modal if it doesn't match
                    await self._close_popup(page)
                    
                except Exception as e:
                    logger.debug(f"Failed to test circle {i}: {e}")
                    continue
            
            logger.warning(f"❌ No matching well circle found for API {api_number}")
            return False
            
        except Exception as e:
            logger.error(f"Error finding well circle: {e}")
            return False
    
    async def _verify_api_in_popup(self, page: Page, api_number: str) -> bool:
        """Verify if the current popup/modal contains the target API number."""
        
        try:
            # Clean API number variants
            clean_api = api_number.replace('42-', '').replace('-', '')  # 00333756
            full_api = api_number  # 42-00333756 or 00333756
            formatted_api = f"413-{clean_api}"  # 413-32865 format from image
            
            api_variants = [clean_api, full_api, formatted_api]
            
            # Check popup/modal selectors
            popup_selectors = [
                '[role="dialog"]', '.popup', '.modal', '.tooltip',
                'div[style*="position: absolute"]', 'div[style*="z-index"]',
                # Check for any visible overlays/popups
                '*:has-text("API")', '*:has-text("Well")', '*:has-text("GIS")'
            ]
            
            page_content = await page.content()
            
            # First check page content for API number
            for api_variant in api_variants:
                if api_variant in page_content:
                    logger.debug(f"Found API variant '{api_variant}' in page content")
                    return True
            
            # Then check specific popup elements
            for selector in popup_selectors:
                try:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        element_text = await element.inner_text()
                        for api_variant in api_variants:
                            if api_variant in element_text:
                                logger.debug(f"Found API '{api_variant}' in popup element")
                                return True
                except:
                    continue
                    
            return False
            
        except Exception as e:
            logger.debug(f"Error verifying API in popup: {e}")
            return False
    
    async def _close_popup(self, page: Page):
        """Close any open popup/modal."""
        
        try:
            # Try multiple strategies to close popup
            close_strategies = [
                lambda: page.keyboard.press('Escape'),
                lambda: page.click('body', position={'x': 10, 'y': 10}),  # Click outside
                lambda: page.click('[aria-label="Close"]'),
                lambda: page.click('.close, .modal-close, [role="button"]:has-text("×")')
            ]
            
            for strategy in close_strategies:
                try:
                    await strategy()
                    await page.wait_for_timeout(500)
                    break  # If no error, strategy worked
                except:
                    continue
                    
        except Exception as e:
            logger.debug(f"Error closing popup: {e}")
    
    async def extract_drilling_permits_data(self, page: Page, api_number: str) -> Dict[str, Any]:
        """
        🎯 NEW METHOD: Handle the drilling permits workflow you described.
        
        This handles:
        1. Click 'Drilling Permits' link in modal
        2. Switch to new tab
        3. Click 'Lease Name' hyperlink
        4. Extract surface location information
        """
        
        logger.info("🔗 Starting drilling permits workflow...")
        surface_data = {}
        new_tab = None
        
        try:
            # Step 1: Find and click 'Drilling Permits' link
            drilling_permits_selectors = [
                'a:has-text("Drilling Permits")',
                'a[href*="permit"]', 'a[href*="drilling"]',
                '*:has-text("Drilling Permits") a',
                'text="Drilling Permits"'
            ]
            
            drilling_permits_link = None
            for selector in drilling_permits_selectors:
                try:
                    drilling_permits_link = await page.query_selector(selector)
                    if drilling_permits_link:
                        logger.debug(f"Found 'Drilling Permits' link: {selector}")
                        break
                except:
                    continue
            
            if not drilling_permits_link:
                logger.warning("❌ 'Drilling Permits' link not found")
                return {"error": "Drilling permits link not found", "confidence_score": 0.0}
            
            # Step 2: Click link and handle new tab
            logger.debug("Clicking 'Drilling Permits' link...")
            
            # Listen for new page/tab
            async with page.context.expect_page() as new_tab_info:
                await drilling_permits_link.click()
            
            new_tab = await new_tab_info.value
            await new_tab.wait_for_load_state('networkidle', timeout=10000)
            logger.info("✅ New drilling permits tab opened")
            
            # Step 3: Click 'Lease Name' hyperlink
            lease_name_selectors = [
                'a:has-text("Lease Name")',
                'a:has-text("A.M. MURPHY")',  # From your image example
                'a:has-text("MURPHY")',
                'td a[href]',  # Table cell links
                'a[href*="lease"]'
            ]
            
            lease_link = None
            for selector in lease_name_selectors:
                try:
                    lease_link = await new_tab.query_selector(selector)
                    if lease_link:
                        logger.debug(f"Found 'Lease Name' link: {selector}")
                        break
                except:
                    continue
            
            if lease_link:
                logger.debug("Clicking 'Lease Name' link...")
                await lease_link.click()
                await new_tab.wait_for_load_state('networkidle', timeout=10000)
                logger.info("✅ Navigated to lease details page")
            else:
                logger.warning("⚠️ 'Lease Name' link not found - extracting from current page")
            
            # Step 4: Extract surface location information
            surface_data = await self._extract_surface_location_info(new_tab, api_number)
            
            return surface_data
            
        except Exception as e:
            logger.error(f"❌ Drilling permits workflow failed: {e}")
            return {"error": f"Workflow failed: {str(e)}", "confidence_score": 0.0}
            
        finally:
            # Always close the new tab
            if new_tab:
                try:
                    await new_tab.close()
                    logger.debug("Closed drilling permits tab")
                except:
                    pass
    
    async def _extract_surface_location_info(self, page: Page, api_number: str) -> Dict[str, Any]:
        """
        Extract surface location information from the final page.
        
        Based on your image, looking for:
        - Distance from Nearest Town (8.0 miles)
        - Direction from Nearest Town (NW) 
        - Nearest Town (ELDORADO)
        - Surface Location Type (Land)
        """
        
        try:
            surface_data = {
                "api_number": api_number,
                "distance": None,
                "direction": None,
                "town": None, 
                "location_type": None,
                "extraction_method": "drilling_permits_workflow"
            }
            
            # Get all page text for analysis
            page_text = await page.inner_text()
            page_html = await page.content()
            
            logger.debug("Extracting surface location data...")
            
            # Extract Distance (e.g., "8.0 miles")
            distance_patterns = [
                r'(\d+\.?\d*)\s*miles?',
                r'Distance[^:]*:\s*([^,\n]+)',
                r'(\d+\.?\d*)\s*(mi\.?|mile)'
            ]
            
            for pattern in distance_patterns:
                matches = re.findall(pattern, page_text, re.IGNORECASE)
                if matches:
                    if isinstance(matches[0], tuple):
                        surface_data["distance"] = f"{matches[0][0]} miles"
                    else:
                        surface_data["distance"] = matches[0]
                    logger.debug(f"Extracted distance: {surface_data['distance']}")
                    break
            
            # Extract Direction (e.g., "NW", "Northwest")
            direction_patterns = [
                r'\b(N|S|E|W|NE|NW|SE|SW)\b',
                r'\b(North|South|East|West|Northeast|Northwest|Southeast|Southwest)\b',
                r'Direction[^:]*:\s*([^,\n]+)'
            ]
            
            for pattern in direction_patterns:
                matches = re.findall(pattern, page_text, re.IGNORECASE)
                if matches:
                    surface_data["direction"] = matches[0].upper()
                    logger.debug(f"Extracted direction: {surface_data['direction']}")
                    break
            
            # Extract Town (e.g., "ELDORADO", "KERMIT")
            town_patterns = [
                r'Town[^:]*:\s*([^,\n]+)',
                r'\b(ELDORADO|KERMIT|MIDLAND|ODESSA)\b',
                r'of\s+([A-Z][A-Z\s]{2,15})',
                r'near\s+([A-Z][A-Z\s]{2,15})'
            ]
            
            for pattern in town_patterns:
                matches = re.findall(pattern, page_text, re.IGNORECASE)
                if matches:
                    surface_data["town"] = matches[0].strip().upper()
                    logger.debug(f"Extracted town: {surface_data['town']}")
                    break
            
            # Extract Location Type (e.g., "Land", "Water")
            location_type_patterns = [
                r'Location Type[^:]*:\s*([^,\n]+)',
                r'\b(Land|Water|Offshore)\b'
            ]
            
            for pattern in location_type_patterns:
                matches = re.findall(pattern, page_text, re.IGNORECASE)
                if matches:
                    surface_data["location_type"] = matches[0].strip().title()
                    logger.debug(f"Extracted location type: {surface_data['location_type']}")
                    break
            
            # Calculate confidence score
            confidence = 0.0
            if surface_data["distance"]: confidence += 0.3
            if surface_data["direction"]: confidence += 0.3
            if surface_data["town"]: confidence += 0.3
            if surface_data["location_type"]: confidence += 0.1
            
            surface_data["confidence_score"] = confidence
            
            # Store raw data for learning system
            surface_data["raw_text"] = page_text[:1000]  # First 1000 chars
            surface_data["page_url"] = page.url
            
            logger.info(f"✅ Surface location extraction completed with {confidence:.1%} confidence")
            return surface_data
            
        except Exception as e:
            logger.error(f"❌ Surface location extraction failed: {e}")
            return {
                "api_number": api_number,
                "error": str(e),
                "confidence_score": 0.0,
                "extraction_method": "drilling_permits_workflow"
            }
    
    # Override workflow execution to handle RRC-specific steps
    
    async def _execute_extraction_workflow(
        self, 
        page: Page, 
        extraction_types: List[str]
    ) -> GISData:
        """Execute RRC-specific GIS extraction workflow."""
        
        workflow = self.get_extraction_workflow()
        extracted_data = GISData()
        
        # Track extraction progress
        successful_steps = 0
        
        for step in workflow:
            step_name = step["name"]
            step_type = step["type"]
            
            try:
                logger.debug(f"Executing RRC GIS step: {step_name}")
                
                if step_type == "click_drilling_permits":
                    await self._click_drilling_permits(page, step["config"])
                elif step_type == "click_lease_name":
                    await self._click_lease_name(page, step["config"])
                else:
                    # Use parent class methods for standard steps
                    if step_type == "click_tool":
                        await self._click_gis_tool(page, step["config"])
                    elif step_type == "select_layer":
                        await self._select_gis_layer(page, step["config"])
                    elif step_type == "click_location":
                        await self._click_location_marker(page, step["config"])
                    elif step_type == "extract_from_popup":
                        popup_data = await self._extract_from_popup(page, step["config"])
                        self._merge_extraction_data(extracted_data, popup_data, extraction_types)
                    elif step_type == "extract_from_table":
                        table_data = await self._extract_from_table(page, step["config"])
                        self._merge_extraction_data(extracted_data, table_data, extraction_types)
                    elif step_type == "extract_from_page":
                        page_data = await self._extract_from_page_content(page, extraction_types)
                        self._merge_extraction_data(extracted_data, page_data, extraction_types)
                
                # Wait between steps
                await asyncio.sleep(step.get("wait_after", 1))
                successful_steps += 1
                
            except Exception as e:
                logger.warning(f"RRC GIS step '{step_name}' failed: {str(e)}")
                if step.get("required", False):
                    raise GISExtractionError(f"Required RRC GIS step failed: {step_name}")
                continue
        
        # Calculate confidence based on successful steps
        total_steps = len(workflow)
        step_success_rate = successful_steps / total_steps if total_steps > 0 else 0
        
        if extracted_data.confidence_score == 0.0:
            extracted_data.confidence_score = step_success_rate * 0.5  # Base confidence on workflow success
        
        logger.info(f"RRC GIS extraction completed: {successful_steps}/{total_steps} steps successful")
        
        return extracted_data
    
    def get_debug_info(self) -> Dict[str, Any]:
        """Get RRC GIS extractor debug information."""
        
        return {
            "extractor_type": "RRC_GIS",
            "patterns_loaded": len(self.extraction_patterns),
            "fallback_data": self.fallback_data.__dict__,
            "workflow_steps": len(self.get_extraction_workflow())
        }
