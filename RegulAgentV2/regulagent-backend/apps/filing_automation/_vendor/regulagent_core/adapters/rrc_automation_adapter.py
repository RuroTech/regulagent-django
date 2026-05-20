"""
RRC Automation Data Adapter

Converts data from the existing RRC automation system into our standardized
domain models (WellRecord, etc.) for use with policy packs.

This bridges the gap between the proven RRC automation and our new
deterministic policy engine.
"""

import re
from typing import Dict, Any, List, Optional
from datetime import datetime

# Import domain models
from ..domain.oil_gas.well_record import WellRecord, Casing, Perforation, ExistingTool
from ..domain.oil_gas.regulatory_data import RegulatoryData, GAUData
from ..domain.shared.location import Location, Coordinates, DistanceFromTown, Address

# Import RRC automation data models
from ..automation.base.data_models import GISData, FormData


class RRCAutomationAdapter:
    """
    Adapter to convert RRC automation output into domain models.
    
    This handles the data transformation from the existing automation
    system into our standardized WellRecord format.
    """
    
    def __init__(self):
        """Initialize the adapter."""
        self.casing_size_patterns = {
            r'(\d+\.?\d*)\s*3/8': lambda m: float(m.group(1)) + 0.375,  # 13 3/8" -> 13.375
            r'(\d+\.?\d*)\s*5/8': lambda m: float(m.group(1)) + 0.625,  # 9 5/8" -> 9.625
            r'(\d+\.?\d*)\s*1/2': lambda m: float(m.group(1)) + 0.5,    # 5 1/2" -> 5.5
            r'(\d+\.?\d*)[\s"]*inch': lambda m: float(m.group(1)),      # 7 inch -> 7.0
            r'(\d+\.?\d*)': lambda m: float(m.group(1))                 # Fallback
        }
    
    def convert_to_well_record(self, 
                             api_number: str,
                             gis_data: Optional[GISData] = None,
                             form_data: Optional[FormData] = None,
                             completion_data: Optional[Dict[str, Any]] = None,
                             gau_data: Optional[Dict[str, Any]] = None,
                             additional_data: Optional[Dict[str, Any]] = None) -> WellRecord:
        """
        Convert RRC automation data into a WellRecord domain model.
        
        Args:
            api_number: Well API number
            gis_data: GIS data from RRC automation
            form_data: Form data from RRC automation
            completion_data: Well completion data
            gau_data: GAU allocation data
            additional_data: Any additional extracted data
        
        Returns:
            WellRecord: Standardized well record for policy pack processing
        """
        
        # Create location from GIS data
        location = self._convert_location(gis_data, additional_data)
        
        # Extract well header information
        header_info = self._extract_header_info(form_data, additional_data)
        
        # Convert casing program
        casing_program = self._convert_casing_program(completion_data, additional_data)
        
        # Convert perforations
        perforations = self._convert_perforations(completion_data, additional_data)
        
        # Convert existing tools
        existing_tools = self._convert_existing_tools(completion_data, additional_data)
        
        # Create regulatory data
        regulatory_data = self._convert_regulatory_data(gau_data, additional_data)
        
        # Create WellRecord
        well_record = WellRecord(
            record_id=f"rrc_automation_{api_number}_{int(datetime.now().timestamp())}",
            api_number=api_number,
            well_name=header_info.get("well_name"),
            location=location,
            total_depth_ft=self._extract_total_depth(completion_data, additional_data),
            well_status=header_info.get("well_status", "P&A"),
            well_type=self._extract_well_type(gis_data, additional_data),
            operator=header_info.get("operator"),
            district=self._extract_district(gis_data, additional_data),
            field=header_info.get("field"),
            lease_name=header_info.get("lease_name"),
            casing_program=casing_program,
            perforations=perforations,
            existing_tools=existing_tools,
            regulatory_data=regulatory_data
        )
        
        return well_record
    
    def _convert_location(self, gis_data: Optional[GISData], additional_data: Optional[Dict[str, Any]]) -> Optional[Location]:
        """Convert GIS data to Location domain model."""
        if not gis_data:
            return None
        
        # Create coordinates
        coordinates = None
        if gis_data.coordinates:
            coordinates = Coordinates(
                latitude=gis_data.coordinates.get("latitude"),
                longitude=gis_data.coordinates.get("longitude"),
                elevation_ft=gis_data.coordinates.get("elevation")
            )
        
        # Create distance from town
        distance_from_town = None
        if gis_data.distance and gis_data.direction and gis_data.town:
            # Parse distance (remove "miles" and convert to float)
            distance_str = gis_data.distance.replace("miles", "").strip()
            try:
                distance = float(distance_str)
                distance_from_town = DistanceFromTown(
                    distance=distance,
                    direction=gis_data.direction,
                    town_name=gis_data.town
                )
            except ValueError:
                pass
        
        # Create address (if available in additional data)
        address = None
        if additional_data:
            county = additional_data.get("county")
            if county:
                address = Address(
                    county=county,
                    state="Texas"
                )
        
        return Location(
            coordinates=coordinates,
            distance_from_town=distance_from_town,
            address=address,
            description=gis_data.location_string if gis_data else None
        )
    
    def _extract_header_info(self, form_data: Optional[FormData], additional_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Extract well header information."""
        header = {}
        
        # Extract from form data
        if form_data:
            # This would extract operator, lease name, etc. from form data
            # For now, use placeholder logic
            pass
        
        # Extract from additional data
        if additional_data:
            header.update({
                "operator": additional_data.get("operator"),
                "lease_name": additional_data.get("lease_name"),
                "field": additional_data.get("field_name"),
                "well_name": additional_data.get("well_name"),
                "well_status": additional_data.get("well_status", "P&A")
            })
        # Fallback to completion_data if not present in additional_data
        try:
            if not header.get("operator") and hasattr(self, "_last_completion_data"):
                header["operator"] = self._last_completion_data.get("operator")
            if not header.get("field") and hasattr(self, "_last_completion_data"):
                header["field"] = self._last_completion_data.get("field_name")
            if not header.get("lease_name") and hasattr(self, "_last_completion_data"):
                header["lease_name"] = self._last_completion_data.get("lease_name")
            if not header.get("well_name") and hasattr(self, "_last_completion_data"):
                header["well_name"] = self._last_completion_data.get("well_name")
        except Exception:
            pass
        
        return header
    
    def _convert_casing_program(self, completion_data: Optional[Dict[str, Any]], additional_data: Optional[Dict[str, Any]]) -> List[Casing]:
        """Convert casing records to Casing domain models."""
        casing_program = []
        
        if not completion_data:
            return casing_program
        
        # Extract casing records
        casing_records = completion_data.get("casing_records", [])
        # Keep a link for header fallback
        try:
            self._last_completion_data = completion_data
        except Exception:
            pass
        
        for i, record in enumerate(casing_records):
            # Determine casing name based on depth and sequence
            if i == 0:
                name = "surface"
            elif i == len(casing_records) - 1:
                name = "production"
            else:
                name = "intermediate"
            
            # Parse casing size
            size_str = record.get("size", "")
            size_in = self._parse_casing_size(size_str)
            
            # Extract depth
            depth = record.get("depth", 0)
            if isinstance(depth, str):
                depth = float(re.sub(r'[^\d.]', '', depth))
            
            # Create casing
            casing = Casing(
                name=name,
                size_in=size_in,
                shoe_ft=float(depth),
                toc_ft=0,  # Assume TOC to surface unless specified
                cement_class="H" if depth > 6500 else "C",  # Standard rule
                weight_lb_ft=self._parse_weight(record.get("weight", "")),
                grade=record.get("grade", "N-80")
            )
            
            casing_program.append(casing)
        
        return casing_program
    
    def _convert_perforations(self, completion_data: Optional[Dict[str, Any]], additional_data: Optional[Dict[str, Any]]) -> List[Perforation]:
        """Convert perforation records to Perforation domain models."""
        perforations = []
        
        if not completion_data:
            return perforations
        
        perf_records = completion_data.get("perforations", [])
        
        for record in perf_records:
            # Handle both field name variations (top/top_ft, bottom/bottom_ft)
            top_ft = record.get("top_ft") or record.get("top", 0)
            bottom_ft = record.get("bottom_ft") or record.get("bottom", 0)
            
            perf = Perforation(
                top_ft=float(top_ft),
                bottom_ft=float(bottom_ft),
                formation=record.get("formation") or record.get("description", "Unknown Formation"),
                active=record.get("active", False)
            )
            perforations.append(perf)
        
        return perforations
    
    def _convert_existing_tools(self, completion_data: Optional[Dict[str, Any]], additional_data: Optional[Dict[str, Any]]) -> List[ExistingTool]:
        """Convert existing tool records to ExistingTool domain models."""
        existing_tools = []
        
        # Check additional data for CIBP information
        if additional_data:
            cibp_depth = additional_data.get("cibp_depth")
            if cibp_depth:
                tool = ExistingTool(
                    tool_type="CIBP",
                    md_ft=float(cibp_depth),
                    verified=True,
                    description=additional_data.get("cibp_description", "Existing CIBP")
                )
                existing_tools.append(tool)
        
        # Extract from completion data if available
        if completion_data:
            tools = completion_data.get("existing_tools", [])
            for tool_data in tools:
                # Accept both legacy (type/depth) and new (tool_type/md_ft) schemas
                raw_type = tool_data.get("tool_type") or tool_data.get("type")
                raw_depth = tool_data.get("md_ft") or tool_data.get("depth")
                if raw_type is None or raw_depth in (None, ""):
                    continue
                # Normalize tool type to allowed literals
                t = str(raw_type).strip().lower()
                mapped = None
                if "cibp" in t or "bridge" in t:
                    mapped = "CIBP"
                elif "packer" in t:
                    mapped = "packer"
                elif t == "dv" or "dv" in t:
                    mapped = "DV"
                elif "retainer" in t:
                    mapped = "retainer"
                elif "plug" in t:
                    mapped = "plug"
                # Skip unknown types
                if not mapped:
                    continue
                try:
                    md_ft_val = float(raw_depth)
                except (TypeError, ValueError):
                    continue
                if md_ft_val <= 0:
                    continue
                tool = ExistingTool(
                    tool_type=mapped,
                    md_ft=md_ft_val,
                    verified=tool_data.get("verified", True),
                    description=tool_data.get("description")
                )
                existing_tools.append(tool)
        
        return existing_tools
    
    def _convert_regulatory_data(self, gau_data: Optional[Dict[str, Any]], additional_data: Optional[Dict[str, Any]]) -> Optional[RegulatoryData]:
        """Convert GAU and regulatory data."""
        if not gau_data and not additional_data:
            return None
        
        # Create GAU data
        gau_info = None
        if gau_data:
            gau_info = GAUData(
                duqw_ft=gau_data.get("duqw_ft"),
                unit_name=gau_data.get("unit_name"),
                working_interest=gau_data.get("working_interest", 1.0),
                net_revenue_interest=gau_data.get("net_revenue_interest", 0.875)
            )
        
        return RegulatoryData(
            gau_data=gau_info
        )
    
    def _parse_casing_size(self, size_str: str) -> float:
        """Parse casing size string to float."""
        if not size_str:
            return 7.0  # Default
        
        # Try each pattern
        for pattern, converter in self.casing_size_patterns.items():
            match = re.search(pattern, size_str, re.IGNORECASE)
            if match:
                return converter(match)
        
        # Fallback: extract first number
        numbers = re.findall(r'\d+\.?\d*', size_str)
        if numbers:
            return float(numbers[0])
        
        return 7.0  # Default fallback
    
    def _parse_weight(self, weight_str: str) -> Optional[float]:
        """Parse casing weight string to float."""
        if not weight_str:
            return None
        
        # Extract number before "lb/ft"
        match = re.search(r'(\d+\.?\d*)', weight_str)
        if match:
            return float(match.group(1))
        
        return None
    
    def _extract_total_depth(self, completion_data: Optional[Dict[str, Any]], additional_data: Optional[Dict[str, Any]]) -> Optional[float]:
        """Extract total depth from various sources."""
        # Try completion data first
        if completion_data:
            td = completion_data.get("total_depth")
            if td:
                return float(td)
        
        # Try additional data
        if additional_data:
            td = additional_data.get("total_depth")
            if td:
                return float(td)
        
        # Fallback: use deepest casing depth
        if completion_data and completion_data.get("casing_records"):
            depths = [record.get("depth", 0) for record in completion_data["casing_records"]]
            if depths:
                return float(max(depths))
        
        return None
    
    def _extract_well_type(self, gis_data: Optional[GISData], additional_data: Optional[Dict[str, Any]]) -> Optional[str]:
        """Extract well type from GIS or additional data."""
        # Try GIS data first
        if gis_data and gis_data.well_type:
            return gis_data.well_type
        
        # Try additional data
        if additional_data:
            well_type = additional_data.get("well_type")
            if well_type:
                return well_type
        
        return "Oil"  # Default assumption
    
    def _extract_district(self, gis_data: Optional[GISData], additional_data: Optional[Dict[str, Any]]) -> Optional[int]:
        """Extract RRC district number."""
        # Try additional data first
        if additional_data:
            district = additional_data.get("rrc_district")
            if district:
                # Extract number from district string
                if isinstance(district, str):
                    numbers = re.findall(r'\d+', district)
                    if numbers:
                        return int(numbers[0])
                elif isinstance(district, (int, float)):
                    return int(district)
        
        # Default to District 8 (common for West Texas)
        return 8
    
    def validate_conversion(self, well_record: WellRecord) -> List[str]:
        """Validate the converted well record and return any issues."""
        issues = []
        
        # Check required fields
        if not well_record.api_number:
            issues.append("API number is missing")
        
        if not well_record.casing_program:
            issues.append("No casing program found")
        
        if not well_record.total_depth_ft:
            issues.append("Total depth is missing")
        
        # Check casing program validity
        if well_record.casing_program:
            depths = [c.shoe_ft for c in well_record.casing_program]
            if depths != sorted(depths):
                issues.append("Casing depths are not in ascending order")
        
        # Check for surface casing
        surface_casing = any(c.name == "surface" for c in well_record.casing_program)
        if not surface_casing:
            issues.append("No surface casing found")
        
        return issues
    
    def get_conversion_summary(self, well_record: WellRecord) -> Dict[str, Any]:
        """Get a summary of the conversion results."""
        return {
            "api_number": well_record.api_number,
            "total_depth_ft": well_record.total_depth_ft,
            "casing_strings": len(well_record.casing_program),
            "perforations": len(well_record.perforations),
            "existing_tools": len(well_record.existing_tools),
            "has_location": well_record.location is not None,
            "has_regulatory_data": well_record.regulatory_data is not None,
            "district": well_record.district,
            "operator": well_record.operator,
            "conversion_timestamp": datetime.now().isoformat()
        }
