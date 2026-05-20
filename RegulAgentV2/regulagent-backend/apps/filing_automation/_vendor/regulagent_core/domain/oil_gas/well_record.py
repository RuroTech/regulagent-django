"""
Oil & Gas Well Record Models

These models represent well data specific to the oil & gas industry,
including casing programs, perforations, completions, and downhole tools.
"""

from pydantic import BaseModel, Field, validator
from typing import List, Optional, Literal, TYPE_CHECKING, Any
from datetime import date
from ..shared.base_record import BaseRecord
from ..shared.location import Location
from ..shared.common_types import DistanceUnit, VolumeUnit, PressureUnit

if TYPE_CHECKING:
    from .regulatory_data import RegulatoryData


class Casing(BaseModel):
    """Oil & gas well casing string."""
    
    name: Literal["surface", "intermediate", "production", "liner"] = Field(..., description="Casing string name")
    size_in: float = Field(..., gt=0, description="Casing outer diameter in inches")
    weight_lb_ft: Optional[float] = Field(None, gt=0, description="Casing weight in lb/ft")
    grade: Optional[str] = Field(None, description="Casing grade (e.g., N-80, P-110)")
    
    # Depths
    shoe_ft: float = Field(..., gt=0, description="Casing shoe depth in feet")
    toc_ft: Optional[float] = Field(None, description="Top of cement in feet")
    
    # Specifications
    id_in: Optional[float] = Field(None, gt=0, description="Casing inner diameter in inches")
    wall_thickness_in: Optional[float] = Field(None, gt=0, description="Wall thickness in inches")
    
    # Installation details
    set_date: Optional[date] = Field(None, description="Date casing was set")
    cement_class: Optional[Literal["A", "B", "C", "G", "H"]] = Field(None, description="Cement class used")
    
    @validator('toc_ft')
    def validate_toc(cls, v, values):
        if v is not None and 'shoe_ft' in values and v > values['shoe_ft']:
            raise ValueError('Top of cement cannot be deeper than casing shoe')
        return v
    
    @property
    def cement_coverage_ft(self) -> Optional[float]:
        """Calculate cement coverage above shoe."""
        if self.toc_ft is not None:
            return self.shoe_ft - self.toc_ft
        return None


class Perforation(BaseModel):
    """Oil & gas well perforation interval."""
    
    top_ft: float = Field(..., gt=0, description="Top of perforation interval in feet")
    bottom_ft: float = Field(..., gt=0, description="Bottom of perforation interval in feet")
    
    # Formation information
    formation: Optional[str] = Field(None, description="Formation name")
    zone: Optional[str] = Field(None, description="Production zone designation")
    
    # Perforation specifications
    shot_density: Optional[float] = Field(None, gt=0, description="Shots per foot")
    gun_type: Optional[str] = Field(None, description="Perforation gun type")
    charge_type: Optional[str] = Field(None, description="Charge type used")
    
    # Status
    active: bool = Field(default=True, description="Whether perforation is currently active")
    plugged: bool = Field(default=False, description="Whether perforation has been plugged")
    
    @validator('bottom_ft')
    def validate_interval(cls, v, values):
        if 'top_ft' in values and v <= values['top_ft']:
            raise ValueError('Bottom depth must be greater than top depth')
        return v
    
    @property
    def interval_length_ft(self) -> float:
        """Calculate perforation interval length."""
        return self.bottom_ft - self.top_ft


class ExistingTool(BaseModel):
    """Existing downhole tool or equipment."""
    
    tool_type: Literal["CIBP", "retainer", "DV", "packer", "plug", "bridge_plug"] = Field(
        ..., description="Type of downhole tool"
    )
    md_ft: float = Field(..., gt=0, description="Measured depth of tool in feet")
    
    # Tool specifications
    size_in: Optional[float] = Field(None, gt=0, description="Tool size in inches")
    manufacturer: Optional[str] = Field(None, description="Tool manufacturer")
    model: Optional[str] = Field(None, description="Tool model/part number")
    
    # Installation details
    set_date: Optional[date] = Field(None, description="Date tool was set")
    set_by: Optional[str] = Field(None, description="Company that set the tool")
    
    # Status
    verified: bool = Field(default=False, description="Whether tool presence is verified")
    accessible: bool = Field(default=True, description="Whether tool is accessible")
    
    # Additional depths (for tools that span intervals)
    top_ft: Optional[float] = Field(None, description="Top of tool (if applicable)")
    bottom_ft: Optional[float] = Field(None, description="Bottom of tool (if applicable)")
    
    @validator('top_ft', 'bottom_ft')
    def validate_tool_depths(cls, v, values):
        if v is not None and 'md_ft' in values:
            # For most tools, md_ft should be within the tool span
            pass  # Add specific validation logic as needed
        return v


class WellRecord(BaseRecord):
    """Complete oil & gas well record."""
    
    # Well identification
    api_number: str = Field(..., description="API well number")
    well_name: Optional[str] = Field(None, description="Well name")
    well_number: Optional[str] = Field(None, description="Well number on lease")
    
    # Location
    location: Location = Field(..., description="Well location")
    
    # Well specifications
    total_depth_ft: float = Field(..., gt=0, description="Total depth in feet")
    measured_depth_ft: Optional[float] = Field(None, gt=0, description="Measured depth if different from TD")
    
    # Well construction
    casing_program: List[Casing] = Field(default_factory=list, description="Casing strings")
    perforations: List[Perforation] = Field(default_factory=list, description="Perforation intervals")
    existing_tools: List[ExistingTool] = Field(default_factory=list, description="Existing downhole tools")
    
    # Tubing information
    tubing_present: bool = Field(default=False, description="Whether tubing is present")
    tubing_size_in: Optional[float] = Field(None, description="Tubing size in inches")
    tubing_depth_ft: Optional[float] = Field(None, description="Tubing depth in feet")
    
    # Well status
    well_status: Literal["Producing", "Shut-In", "TA", "P&A", "Drilling", "Completed"] = Field(
        ..., description="Current well status"
    )
    well_type: Literal["Oil", "Gas", "Water", "Injection", "Disposal"] = Field(
        ..., description="Well type"
    )
    
    # Operator information
    operator: Optional[str] = Field(None, description="Current operator")
    operator_number: Optional[str] = Field(None, description="Operator number")
    
    # Regulatory information
    district: Optional[int] = Field(None, description="RRC district number")
    field: Optional[str] = Field(None, description="Field name")
    lease_name: Optional[str] = Field(None, description="Lease name")
    
    # Dates
    spud_date: Optional[date] = Field(None, description="Spud date")
    completion_date: Optional[date] = Field(None, description="Completion date")
    first_production_date: Optional[date] = Field(None, description="First production date")
    
    # Regulatory data
    regulatory_data: Optional[Any] = Field(None, description="Regulatory and compliance data")
    
    def __init__(self, **data):
        # Set record_type for oil & gas wells
        data['record_type'] = 'oil_gas_well'
        super().__init__(**data)
    
    @validator('measured_depth_ft')
    def validate_measured_depth(cls, v, values):
        if v is not None and 'total_depth_ft' in values and v < values['total_depth_ft']:
            raise ValueError('Measured depth cannot be less than total depth')
        return v
    
    @validator('tubing_depth_ft')
    def validate_tubing_depth(cls, v, values):
        if v is not None and 'total_depth_ft' in values and v > values['total_depth_ft']:
            raise ValueError('Tubing depth cannot exceed total depth')
        return v
    
    @property
    def deepest_casing_ft(self) -> Optional[float]:
        """Get depth of deepest casing shoe."""
        if not self.casing_program:
            return None
        return max(casing.shoe_ft for casing in self.casing_program)
    
    @property
    def surface_casing_ft(self) -> Optional[float]:
        """Get depth of surface casing shoe."""
        surface_casings = [c for c in self.casing_program if c.name == "surface"]
        if not surface_casings:
            return None
        return surface_casings[0].shoe_ft
    
    @property
    def production_casing_ft(self) -> Optional[float]:
        """Get depth of production casing shoe."""
        prod_casings = [c for c in self.casing_program if c.name == "production"]
        if not prod_casings:
            return None
        return prod_casings[0].shoe_ft
    
    @property
    def deepest_cibp_ft(self) -> Optional[float]:
        """Get depth of deepest CIBP."""
        cibps = [tool for tool in self.existing_tools if tool.tool_type == "CIBP"]
        if not cibps:
            return None
        return max(cibp.md_ft for cibp in cibps)
    
    @property
    def active_perforations(self) -> List[Perforation]:
        """Get list of active (non-plugged) perforations."""
        return [perf for perf in self.perforations if perf.active and not perf.plugged]
    
    @property
    def total_perforated_interval_ft(self) -> float:
        """Calculate total length of active perforated intervals."""
        return sum(perf.interval_length_ft for perf in self.active_perforations)
    
    def get_casing_at_depth(self, depth_ft: float) -> Optional[Casing]:
        """Get the casing string at a given depth."""
        applicable_casings = [c for c in self.casing_program if c.shoe_ft >= depth_ft]
        if not applicable_casings:
            return None
        # Return the shallowest casing that extends to this depth
        return min(applicable_casings, key=lambda x: x.shoe_ft)
    
    def validate_well_construction(self) -> List[str]:
        """Validate well construction for common issues."""
        issues = []
        
        # Check casing program
        if not self.casing_program:
            issues.append("No casing program specified")
        else:
            # Check for surface casing
            if not any(c.name == "surface" for c in self.casing_program):
                issues.append("No surface casing specified")
            
            # Check casing depth progression
            casing_depths = [(c.name, c.shoe_ft) for c in self.casing_program]
            casing_depths.sort(key=lambda x: x[1])  # Sort by depth
            
            for i in range(1, len(casing_depths)):
                if casing_depths[i][1] <= casing_depths[i-1][1]:
                    issues.append(f"Casing depth progression issue: {casing_depths[i][0]} not deeper than {casing_depths[i-1][0]}")
        
        # Check perforations vs casing
        for perf in self.perforations:
            casing_at_perf = self.get_casing_at_depth(perf.top_ft)
            if not casing_at_perf:
                issues.append(f"Perforation at {perf.top_ft}-{perf.bottom_ft} ft has no casing protection")
        
        # Check tools vs total depth
        for tool in self.existing_tools:
            if tool.md_ft > self.total_depth_ft:
                issues.append(f"{tool.tool_type} at {tool.md_ft} ft exceeds total depth")
        
        return issues
