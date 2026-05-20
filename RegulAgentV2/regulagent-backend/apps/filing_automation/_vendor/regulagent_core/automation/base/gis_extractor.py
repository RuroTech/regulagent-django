"""Base GIS data extraction class with multi-source support."""

import asyncio
import re
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any, Pattern
from playwright.async_api import Page
import logging

from .data_models import GISData, AutomationResult, SelectorConfig
from .selector_engine import BaseSelectorEngine
from ..exceptions import GISExtractionError


logger = logging.getLogger(__name__)


class BaseGISExtractor(ABC):
    """Base class for GIS data extraction with multi-source support."""
    
    def __init__(self, result: AutomationResult):
        self.result = result
        self.selector_engine = BaseSelectorEngine(result)
        
        # Common extraction patterns (can be overridden by subclasses)
        self.extraction_patterns = {
            "distance_direction": [
                r'(\d+\.?\d*)\s*miles?\s*(north|south|east|west|N|S|E|W|NE|NW|SE|SW|northeast|northwest|southeast|southwest)\s*of\s*([A-Z][A-Z\s]+)',
                r'Distance.*?(\d+\.?\d*)\s*miles?\s*(north|south|east|west|N|S|E|W|NE|NW|SE|SW).*?([A-Z][A-Z\s]+)',
                r'(\d+\.?\d*)\s*(miles?|mi)\s*(north|south|east|west|N|S|E|W|NE|NW|SE|SW)\s*of\s*([A-Z][A-Z\s]+)'
            ],
            "coordinates": [
                r'Lat(?:itude)?[:\s]*(-?\d+\.?\d*)[,\s]*Lon(?:gitude)?[:\s]*(-?\d+\.?\d*)',
                r'(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)'
            ],
            "well_type": [
                r'(oil|gas|water|injection)\s*well',
                r'Type[:\s]*(Oil|Gas|Water|Injection)',
                r'Production[:\s]*Type[:\s]*(Oil|Gas)'
            ]
        }
        
        # Fallback data for when extraction fails
        self.fallback_data = GISData(
            distance="4",
            direction="northwest", 
            town="Unknown",
            confidence_score=0.1
        )
    
    # Abstract methods to be implemented by agency-specific classes
    
    @abstractmethod
    async def setup_gis_session(self, page: Page, search_params: Dict[str, Any]) -> bool:
        """Setup GIS session and navigate to data source."""
        pass
    
    @abstractmethod
    async def search_location(self, page: Page, identifier: str) -> bool:
        """Search for location using identifier (API number, coordinates, etc.)."""
        pass
    
    @abstractmethod
    def get_extraction_workflow(self) -> List[Dict[str, Any]]:
        """Get the extraction workflow steps specific to the GIS system."""
        pass
    
    # Core extraction methods (reusable across GIS systems)
    
    async def extract_gis_data(
        self, 
        page: Page, 
        identifier: str,
        extraction_types: List[str] = None
    ) -> GISData:
        """Main GIS data extraction workflow."""
        
        extraction_types = extraction_types or ["distance_direction", "well_type", "coordinates"]
        
        try:
            logger.info(f"Starting GIS extraction for identifier: {identifier}")
            self.result.add_log_entry("INFO", f"GIS extraction started: {identifier}", step="gis_extraction")
            
            # Setup GIS session
            setup_params = {"identifier": identifier}
            setup_success = await self.setup_gis_session(page, setup_params)
            if not setup_success:
                raise GISExtractionError("GIS session setup failed")
            
            # Search for location
            search_success = await self.search_location(page, identifier)
            if not search_success:
                logger.warning("GIS location search failed, using fallback data")
                return self._create_fallback_data(identifier)
            
            # Extract data using workflow
            gis_data = await self._execute_extraction_workflow(page, extraction_types)
            
            # Validate extracted data
            if not self._validate_extracted_data(gis_data):
                logger.warning("GIS data validation failed, using fallback")
                return self._create_fallback_data(identifier)
            
            self.result.add_log_entry(
                "INFO", 
                f"GIS extraction completed: {gis_data.location_string}",
                step="gis_extraction"
            )
            
            return gis_data
            
        except Exception as e:
            error_msg = f"GIS extraction failed: {str(e)}"
            logger.error(error_msg)
            self.result.add_log_entry("ERROR", error_msg, step="gis_extraction")
            
            # Return fallback data instead of raising
            return self._create_fallback_data(identifier)
    
    async def _execute_extraction_workflow(
        self, 
        page: Page, 
        extraction_types: List[str]
    ) -> GISData:
        """Execute the GIS extraction workflow steps."""
        
        workflow = self.get_extraction_workflow()
        extracted_data = GISData()
        
        for step in workflow:
            step_name = step["name"]
            step_type = step["type"]
            
            try:
                logger.debug(f"Executing GIS extraction step: {step_name}")
                
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
                    
                else:
                    logger.warning(f"Unknown extraction step type: {step_type}")
                
                # Wait between steps
                await asyncio.sleep(step.get("wait_after", 1))
                
            except Exception as e:
                logger.warning(f"GIS extraction step '{step_name}' failed: {str(e)}")
                if step.get("required", False):
                    raise GISExtractionError(f"Required step failed: {step_name}")
                continue
        
        return extracted_data
    
    async def _click_gis_tool(self, page: Page, config: Dict[str, Any]):
        """Click GIS tool (info tool, select tool, etc.)."""
        
        tool_selectors = config.get("selectors", [])
        tool_config = SelectorConfig(
            primary=tool_selectors[0] if tool_selectors else 'button[title*="Identify" i]',
            fallbacks=tool_selectors[1:] if len(tool_selectors) > 1 else [
                'button[aria-label*="Identify" i]',
                '.identify-tool',
                'button:has-text("i")'
            ],
            description=config.get("description", "GIS tool")
        )
        
        await self.selector_engine.smart_click(page, tool_config)
    
    async def _select_gis_layer(self, page: Page, config: Dict[str, Any]):
        """Select GIS layer or dataset."""
        
        layer_name = config.get("layer_name", "wells")
        layer_selectors = [
            f'select option[value*="{layer_name}" i]',
            f'select option:has-text("{layer_name.title()}")',
            f'option:has-text("{layer_name}")',
            f'[role="option"]:has-text("{layer_name.title()}")'
        ]
        
        layer_config = SelectorConfig(
            primary=layer_selectors[0],
            fallbacks=layer_selectors[1:],
            description=f"{layer_name} layer"
        )
        
        await self.selector_engine.smart_click(page, layer_config)
    
    async def _click_location_marker(self, page: Page, config: Dict[str, Any]):
        """Click on location marker/dot in GIS interface."""
        
        # Try different marker selectors
        marker_selectors = config.get("selectors", [
            '.well-marker',
            '.esri-graphic', 
            '[class*="well"]',
            'circle[fill*="blue"]',
            'circle[fill*="green"]'
        ])
        
        for selector in marker_selectors:
            try:
                markers = await page.query_selector_all(selector)
                if markers:
                    # Try clicking the first few markers
                    for i, marker in enumerate(markers[:3]):
                        try:
                            await marker.click()
                            await asyncio.sleep(2)
                            
                            # Check if popup appeared
                            popup_check = await page.query_selector('[class*="identity" i], [title*="identity" i], .popup-content')
                            if popup_check:
                                logger.info("GIS popup appeared after marker click")
                                return
                        except:
                            continue
            except:
                continue
        
        # Fallback: Click at estimated center of map
        try:
            map_element = await page.query_selector('#map, .map-container, .esri-view')
            if map_element:
                bbox = await map_element.bounding_box()
                center_x = bbox['x'] + bbox['width'] / 2
                center_y = bbox['y'] + bbox['height'] / 2
                await page.mouse.click(center_x, center_y)
                await asyncio.sleep(3)
        except Exception as e:
            logger.warning(f"Map center click fallback failed: {e}")
    
    async def _extract_from_popup(self, page: Page, config: Dict[str, Any]) -> Dict[str, Any]:
        """Extract data from GIS popup/info window."""
        
        popup_data = {}
        
        # Wait for popup to appear
        popup_selectors = [
            '[class*="identity" i]',
            '[title*="identity" i]', 
            '.popup-content',
            '.info-window',
            '.esri-popup'
        ]
        
        popup_element = None
        for selector in popup_selectors:
            try:
                popup_element = await page.wait_for_selector(selector, timeout=5000)
                if popup_element:
                    break
            except:
                continue
        
        if popup_element:
            popup_text = await popup_element.inner_text()
            popup_data["popup_content"] = popup_text
            
            # Save popup HTML for debugging
            popup_html = await popup_element.inner_html()
            popup_data["popup_html"] = popup_html
            
            logger.debug(f"Extracted popup content: {len(popup_text)} characters")
        
        return popup_data
    
    async def _extract_from_table(self, page: Page, config: Dict[str, Any]) -> Dict[str, Any]:
        """Extract data from GIS data table."""
        
        table_data = {}
        
        # Look for tables
        table_selectors = ['table', '.table', '[class*="grid"]']
        
        for selector in table_selectors:
            try:
                tables = await page.query_selector_all(selector)
                for i, table in enumerate(tables[:3]):  # Check first 3 tables
                    table_text = await table.inner_text()
                    if any(keyword in table_text.lower() for keyword in ['distance', 'town', 'location']):
                        table_data[f"table_{i}_content"] = table_text
                        logger.debug(f"Found relevant table {i} with location data")
            except:
                continue
        
        return table_data
    
    async def _extract_from_page_content(self, page: Page, extraction_types: List[str]) -> Dict[str, Any]:
        """Extract data from full page content using regex patterns."""
        
        try:
            page_content = await page.content()
            extracted = {}
            
            for extraction_type in extraction_types:
                patterns = self.extraction_patterns.get(extraction_type, [])
                
                for pattern in patterns:
                    matches = re.search(pattern, page_content, re.IGNORECASE)
                    if matches:
                        extracted[f"{extraction_type}_matches"] = matches.groups()
                        logger.debug(f"Found {extraction_type} pattern: {matches.groups()}")
                        break
            
            extracted["page_content_length"] = len(page_content)
            return extracted
            
        except Exception as e:
            logger.warning(f"Page content extraction failed: {e}")
            return {}
    
    def _merge_extraction_data(
        self, 
        gis_data: GISData, 
        raw_data: Dict[str, Any], 
        extraction_types: List[str]
    ):
        """Merge extracted raw data into structured GIS data."""
        
        # Store raw data
        gis_data.raw_data.update(raw_data)
        
        # Extract structured data using patterns
        for extraction_type in extraction_types:
            if extraction_type == "distance_direction":
                self._extract_distance_direction(gis_data, raw_data)
            elif extraction_type == "well_type":
                self._extract_well_type(gis_data, raw_data)
            elif extraction_type == "coordinates":
                self._extract_coordinates(gis_data, raw_data)
    
    def _extract_distance_direction(self, gis_data: GISData, raw_data: Dict[str, Any]):
        """Extract distance, direction, and town from raw data."""
        
        # Combine all text content for pattern matching
        all_text = " ".join(str(value) for value in raw_data.values() if isinstance(value, str))
        
        for pattern in self.extraction_patterns["distance_direction"]:
            match = re.search(pattern, all_text, re.IGNORECASE)
            if match:
                groups = match.groups()
                if len(groups) >= 3:
                    gis_data.distance = groups[0]
                    gis_data.direction = groups[-2].upper()  # Direction usually second-to-last
                    gis_data.town = groups[-1].strip().title()  # Town usually last
                    gis_data.extraction_method = "regex_pattern"
                    gis_data.confidence_score = 0.8
                    logger.info(f"Extracted location: {gis_data.location_string}")
                    break
    
    def _extract_well_type(self, gis_data: GISData, raw_data: Dict[str, Any]):
        """Extract well type from raw data."""
        
        all_text = " ".join(str(value) for value in raw_data.values() if isinstance(value, str)).lower()
        
        if 'oil' in all_text:
            gis_data.well_type = 'Oil'
        elif 'gas' in all_text:
            gis_data.well_type = 'Gas'
        elif 'water' in all_text:
            gis_data.well_type = 'Water'
        elif 'injection' in all_text:
            gis_data.well_type = 'Injection'
        
        if gis_data.well_type:
            logger.debug(f"Extracted well type: {gis_data.well_type}")
    
    def _extract_coordinates(self, gis_data: GISData, raw_data: Dict[str, Any]):
        """Extract coordinates from raw data."""
        
        all_text = " ".join(str(value) for value in raw_data.values() if isinstance(value, str))
        
        for pattern in self.extraction_patterns["coordinates"]:
            match = re.search(pattern, all_text)
            if match:
                try:
                    lat, lon = float(match.group(1)), float(match.group(2))
                    gis_data.coordinates = {"latitude": lat, "longitude": lon}
                    logger.debug(f"Extracted coordinates: {lat}, {lon}")
                    break
                except ValueError:
                    continue
    
    def _validate_extracted_data(self, gis_data: GISData) -> bool:
        """Validate that extracted GIS data is reasonable."""
        
        # Check if we have essential location data
        if not gis_data.is_complete:
            return False
        
        # Validate distance is reasonable (0-100 miles typically)
        try:
            distance_val = float(gis_data.distance)
            if distance_val < 0 or distance_val > 100:
                logger.warning(f"Suspicious distance value: {distance_val}")
                return False
        except (ValueError, TypeError):
            return False
        
        # Validate direction
        valid_directions = ['N', 'S', 'E', 'W', 'NE', 'NW', 'SE', 'SW', 'NORTH', 'SOUTH', 'EAST', 'WEST']
        if gis_data.direction.upper() not in valid_directions:
            logger.warning(f"Invalid direction: {gis_data.direction}")
            return False
        
        # Validate town name (should be reasonable length and characters)
        if not gis_data.town or len(gis_data.town) < 2 or len(gis_data.town) > 50:
            logger.warning(f"Suspicious town name: {gis_data.town}")
            return False
        
        gis_data.confidence_score = 0.9  # High confidence for validated data
        return True
    
    def _create_fallback_data(self, identifier: str) -> GISData:
        """Create fallback GIS data when extraction fails."""
        
        fallback = GISData(
            distance=self.fallback_data.distance,
            direction=self.fallback_data.direction,
            town=self.fallback_data.town,
            extraction_method="fallback",
            confidence_score=0.1
        )
        
        logger.warning(f"Using fallback GIS data for {identifier}: {fallback.location_string}")
        self.result.add_log_entry(
            "WARNING", 
            f"Using fallback GIS data: {fallback.location_string}",
            step="gis_extraction"
        )
        
        return fallback
    
    # Utility methods
    
    def set_fallback_data(self, distance: str, direction: str, town: str):
        """Set custom fallback data for when extraction fails."""
        self.fallback_data = GISData(
            distance=distance,
            direction=direction,
            town=town,
            confidence_score=0.1
        )
    
    def add_extraction_pattern(self, pattern_type: str, regex_pattern: str):
        """Add custom extraction pattern."""
        if pattern_type not in self.extraction_patterns:
            self.extraction_patterns[pattern_type] = []
        self.extraction_patterns[pattern_type].append(regex_pattern)
