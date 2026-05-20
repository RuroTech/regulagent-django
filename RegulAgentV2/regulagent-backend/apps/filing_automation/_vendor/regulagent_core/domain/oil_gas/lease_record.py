"""
Oil & Gas Lease Record Models

These models represent lease and property data specific to the oil & gas industry.
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import date
from ..shared.base_record import BaseRecord
from ..shared.location import Location


class LeaseInfo(BaseModel):
    """Oil & gas lease information."""
    
    lease_name: str = Field(..., description="Lease name")
    lease_number: Optional[str] = Field(None, description="Lease number")
    
    # Acreage
    gross_acres: Optional[float] = Field(None, gt=0, description="Gross lease acres")
    net_acres: Optional[float] = Field(None, gt=0, description="Net lease acres")
    
    # Ownership
    lessor: Optional[str] = Field(None, description="Lessor name")
    lessee: Optional[str] = Field(None, description="Lessee name")
    
    # Lease terms
    lease_date: Optional[date] = Field(None, description="Lease execution date")
    primary_term_years: Optional[int] = Field(None, gt=0, description="Primary term in years")
    royalty_rate: Optional[float] = Field(None, ge=0, le=1, description="Royalty rate (decimal)")
    
    # Status
    lease_status: Optional[str] = Field(None, description="Current lease status")
    
    @property
    def working_interest(self) -> Optional[float]:
        """Calculate working interest (1 - royalty_rate)."""
        if self.royalty_rate is not None:
            return 1.0 - self.royalty_rate
        return None


class LeaseRecord(BaseRecord):
    """Complete oil & gas lease record."""
    
    # Lease identification
    lease_info: LeaseInfo = Field(..., description="Basic lease information")
    
    # Location
    location: Location = Field(..., description="Lease location")
    
    # Associated wells
    well_apis: List[str] = Field(default_factory=list, description="API numbers of wells on lease")
    
    # Regulatory information
    district: Optional[int] = Field(None, description="RRC district number")
    field: Optional[str] = Field(None, description="Field name")
    county: Optional[str] = Field(None, description="County name")
    
    # Production allocation
    allocation_factors: Dict[str, float] = Field(
        default_factory=dict, 
        description="Allocation factors by well API"
    )
    
    def __init__(self, **data):
        # Set record_type for oil & gas leases
        data['record_type'] = 'oil_gas_lease'
        super().__init__(**data)
    
    @property
    def well_count(self) -> int:
        """Number of wells on lease."""
        return len(self.well_apis)
    
    def add_well(self, api_number: str, allocation_factor: float = 1.0) -> None:
        """Add a well to the lease."""
        if api_number not in self.well_apis:
            self.well_apis.append(api_number)
            self.allocation_factors[api_number] = allocation_factor
            self.update_timestamp()
    
    def remove_well(self, api_number: str) -> bool:
        """Remove a well from the lease."""
        if api_number in self.well_apis:
            self.well_apis.remove(api_number)
            self.allocation_factors.pop(api_number, None)
            self.update_timestamp()
            return True
        return False
    
    def get_allocation_factor(self, api_number: str) -> Optional[float]:
        """Get allocation factor for a specific well."""
        return self.allocation_factors.get(api_number)
    
    def validate_allocation_factors(self) -> List[str]:
        """Validate allocation factors."""
        issues = []
        
        # Check that all wells have allocation factors
        for api in self.well_apis:
            if api not in self.allocation_factors:
                issues.append(f"Well {api} missing allocation factor")
        
        # Check for orphaned allocation factors
        for api in self.allocation_factors:
            if api not in self.well_apis:
                issues.append(f"Allocation factor exists for well {api} not on lease")
        
        # Check allocation factor sum (should typically be 1.0 for single-well leases)
        total_allocation = sum(self.allocation_factors.values())
        if abs(total_allocation - 1.0) > 0.01:  # Allow small rounding errors
            issues.append(f"Allocation factors sum to {total_allocation:.3f}, expected 1.0")
        
        return issues
