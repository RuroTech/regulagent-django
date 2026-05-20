"""
Oil & Gas Completion and Production Data Models

These models represent completion reports, production data, and well performance
specific to the oil & gas industry.
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import date
from ..shared.common_types import VolumeUnit, PressureUnit


class CompletionData(BaseModel):
    """Oil & gas well completion data."""
    
    # Completion identification
    completion_date: date = Field(..., description="Date of completion")
    completion_type: str = Field(..., description="Type of completion")
    
    # Formation information
    producing_formation: Optional[str] = Field(None, description="Primary producing formation")
    formations: List[str] = Field(default_factory=list, description="All completed formations")
    
    # Completion specifications
    completion_method: Optional[str] = Field(None, description="Completion method (e.g., perforated, open hole)")
    stimulation_type: Optional[str] = Field(None, description="Stimulation type (e.g., fracture, acid)")
    
    # Initial test data
    initial_oil_rate_bopd: Optional[float] = Field(None, ge=0, description="Initial oil rate in BOPD")
    initial_gas_rate_mcfd: Optional[float] = Field(None, ge=0, description="Initial gas rate in MCFD")
    initial_water_rate_bwpd: Optional[float] = Field(None, ge=0, description="Initial water rate in BWPD")
    
    # Pressure data
    initial_flowing_pressure_psi: Optional[float] = Field(None, ge=0, description="Initial flowing pressure")
    initial_shut_in_pressure_psi: Optional[float] = Field(None, ge=0, description="Initial shut-in pressure")
    
    # Test conditions
    test_period_hours: Optional[float] = Field(None, gt=0, description="Test period in hours")
    choke_size: Optional[str] = Field(None, description="Choke size during test")
    
    # Completion equipment
    tubing_size_in: Optional[float] = Field(None, gt=0, description="Production tubing size")
    packer_depth_ft: Optional[float] = Field(None, gt=0, description="Packer depth")
    
    @property
    def initial_gor(self) -> Optional[float]:
        """Calculate initial gas-oil ratio."""
        if self.initial_oil_rate_bopd and self.initial_gas_rate_mcfd:
            if self.initial_oil_rate_bopd > 0:
                return self.initial_gas_rate_mcfd / self.initial_oil_rate_bopd
        return None
    
    @property
    def initial_wor(self) -> Optional[float]:
        """Calculate initial water-oil ratio."""
        if self.initial_oil_rate_bopd and self.initial_water_rate_bwpd:
            if self.initial_oil_rate_bopd > 0:
                return self.initial_water_rate_bwpd / self.initial_oil_rate_bopd
        return None


class ProductionData(BaseModel):
    """Oil & gas production data for a specific period."""
    
    # Time period
    production_date: date = Field(..., description="Production date or period end")
    period_days: int = Field(default=1, gt=0, description="Number of days in period")
    
    # Production volumes
    oil_bbls: Optional[float] = Field(None, ge=0, description="Oil production in barrels")
    gas_mcf: Optional[float] = Field(None, ge=0, description="Gas production in MCF")
    water_bbls: Optional[float] = Field(None, ge=0, description="Water production in barrels")
    
    # Condensate (for gas wells)
    condensate_bbls: Optional[float] = Field(None, ge=0, description="Condensate production in barrels")
    
    # Operating conditions
    flowing_pressure_psi: Optional[float] = Field(None, ge=0, description="Flowing pressure")
    shut_in_pressure_psi: Optional[float] = Field(None, ge=0, description="Shut-in pressure")
    
    # Well status
    days_on_production: Optional[int] = Field(None, ge=0, le=31, description="Days well was producing")
    downtime_reason: Optional[str] = Field(None, description="Reason for downtime if applicable")
    
    @property
    def oil_rate_bopd(self) -> Optional[float]:
        """Calculate oil rate in BOPD."""
        if self.oil_bbls is not None and self.period_days > 0:
            return self.oil_bbls / self.period_days
        return None
    
    @property
    def gas_rate_mcfd(self) -> Optional[float]:
        """Calculate gas rate in MCFD."""
        if self.gas_mcf is not None and self.period_days > 0:
            return self.gas_mcf / self.period_days
        return None
    
    @property
    def water_rate_bwpd(self) -> Optional[float]:
        """Calculate water rate in BWPD."""
        if self.water_bbls is not None and self.period_days > 0:
            return self.water_bbls / self.period_days
        return None
    
    @property
    def gor(self) -> Optional[float]:
        """Calculate gas-oil ratio."""
        oil_rate = self.oil_rate_bopd
        gas_rate = self.gas_rate_mcfd
        if oil_rate and gas_rate and oil_rate > 0:
            return gas_rate / oil_rate
        return None
    
    @property
    def wor(self) -> Optional[float]:
        """Calculate water-oil ratio."""
        oil_rate = self.oil_rate_bopd
        water_rate = self.water_rate_bwpd
        if oil_rate and water_rate and oil_rate > 0:
            return water_rate / oil_rate
        return None
    
    @property
    def total_liquids_bpd(self) -> Optional[float]:
        """Calculate total liquids rate."""
        rates = []
        if self.oil_rate_bopd is not None:
            rates.append(self.oil_rate_bopd)
        if self.water_rate_bwpd is not None:
            rates.append(self.water_rate_bwpd)
        if self.condensate_bbls is not None and self.period_days > 0:
            rates.append(self.condensate_bbls / self.period_days)
        
        return sum(rates) if rates else None
