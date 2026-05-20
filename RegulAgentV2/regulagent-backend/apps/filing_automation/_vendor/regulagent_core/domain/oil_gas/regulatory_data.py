"""
Oil & Gas Regulatory Data Models

These models represent regulatory permits, compliance data, and agency-specific
information for the oil & gas industry.
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import date
from ..shared.common_types import RecordStatus


class PermitInfo(BaseModel):
    """Oil & gas permit information."""
    
    permit_number: str = Field(..., description="Permit number")
    permit_type: str = Field(..., description="Type of permit (drilling, completion, etc.)")
    
    # Permit details
    issue_date: Optional[date] = Field(None, description="Date permit was issued")
    expiry_date: Optional[date] = Field(None, description="Date permit expires")
    effective_date: Optional[date] = Field(None, description="Date permit becomes effective")
    
    # Issuing authority
    issuing_agency: str = Field(..., description="Agency that issued permit")
    district: Optional[int] = Field(None, description="District or region number")
    
    # Permit status
    status: RecordStatus = Field(default=RecordStatus.APPROVED, description="Current permit status")
    
    # Associated data
    conditions: List[str] = Field(default_factory=list, description="Permit conditions")
    attachments: List[str] = Field(default_factory=list, description="Associated documents")
    
    @property
    def is_expired(self) -> bool:
        """Check if permit is expired."""
        if self.expiry_date:
            from datetime import date
            return date.today() > self.expiry_date
        return False
    
    @property
    def days_until_expiry(self) -> Optional[int]:
        """Calculate days until permit expires."""
        if self.expiry_date:
            from datetime import date
            delta = self.expiry_date - date.today()
            return delta.days
        return None


class GAUData(BaseModel):
    """Gas Allocation Unit (GAU) data."""
    
    # GAU identification
    gau_number: Optional[str] = Field(None, description="GAU number")
    unit_name: Optional[str] = Field(None, description="Unit name")
    
    # Allocation information
    working_interest: Optional[float] = Field(None, ge=0, le=1, description="Working interest decimal")
    net_revenue_interest: Optional[float] = Field(None, ge=0, le=1, description="Net revenue interest decimal")
    
    # DUQW (Deepest Usable Quality Water)
    duqw_ft: Optional[float] = Field(None, gt=0, description="DUQW depth in feet")
    duqw_source: Optional[str] = Field(None, description="Source of DUQW determination")
    duqw_date: Optional[date] = Field(None, description="Date DUQW was determined")
    
    # Unit details
    unit_acres: Optional[float] = Field(None, gt=0, description="Total unit acres")
    well_count: Optional[int] = Field(None, ge=0, description="Number of wells in unit")
    
    # Operator information
    unit_operator: Optional[str] = Field(None, description="Unit operator")
    operator_number: Optional[str] = Field(None, description="Operator number")
    
    # Status
    unit_status: Optional[str] = Field(None, description="Current unit status")
    effective_date: Optional[date] = Field(None, description="Unit effective date")
    
    # Notes and documentation
    notes: List[str] = Field(default_factory=list, description="GAU-related notes")
    supporting_documents: List[str] = Field(default_factory=list, description="Supporting documentation")
    
    @property
    def royalty_interest(self) -> Optional[float]:
        """Calculate royalty interest (1 - working_interest)."""
        if self.working_interest is not None:
            return 1.0 - self.working_interest
        return None


class RegulatoryData(BaseModel):
    """Complete regulatory data for an oil & gas well."""
    
    # Permits
    permits: List[PermitInfo] = Field(default_factory=list, description="Associated permits")
    
    # GAU information
    gau_data: Optional[GAUData] = Field(None, description="GAU allocation data")
    
    # Regulatory compliance
    compliance_status: RecordStatus = Field(default=RecordStatus.PENDING_REVIEW, description="Overall compliance status")
    last_inspection_date: Optional[date] = Field(None, description="Date of last regulatory inspection")
    next_inspection_due: Optional[date] = Field(None, description="Date next inspection is due")
    
    # Violations and enforcement
    violations: List[Dict[str, Any]] = Field(default_factory=list, description="Regulatory violations")
    enforcement_actions: List[Dict[str, Any]] = Field(default_factory=list, description="Enforcement actions")
    
    # Reporting requirements
    required_reports: List[str] = Field(default_factory=list, description="Required regulatory reports")
    submitted_reports: List[Dict[str, Any]] = Field(default_factory=list, description="Submitted reports")
    
    # Bonds and financial assurance
    bond_amount: Optional[float] = Field(None, ge=0, description="Required bond amount")
    bond_status: Optional[str] = Field(None, description="Bond status")
    financial_assurance: Optional[Dict[str, Any]] = Field(None, description="Financial assurance details")
    
    def get_permit_by_type(self, permit_type: str) -> Optional[PermitInfo]:
        """Get permit by type."""
        for permit in self.permits:
            if permit.permit_type.lower() == permit_type.lower():
                return permit
        return None
    
    def get_active_permits(self) -> List[PermitInfo]:
        """Get list of active (non-expired) permits."""
        return [permit for permit in self.permits if not permit.is_expired]
    
    def get_expiring_permits(self, days: int = 30) -> List[PermitInfo]:
        """Get permits expiring within specified days."""
        expiring = []
        for permit in self.permits:
            if permit.days_until_expiry is not None and permit.days_until_expiry <= days:
                expiring.append(permit)
        return expiring
    
    def add_permit(self, permit: PermitInfo) -> None:
        """Add a permit to the regulatory data."""
        self.permits.append(permit)
    
    def add_violation(self, violation_date: date, violation_type: str, description: str, 
                     severity: str = "medium", resolved: bool = False) -> None:
        """Add a regulatory violation."""
        violation = {
            "date": violation_date.isoformat(),
            "type": violation_type,
            "description": description,
            "severity": severity,
            "resolved": resolved,
            "resolution_date": None
        }
        self.violations.append(violation)
    
    def resolve_violation(self, violation_index: int, resolution_date: date, 
                         resolution_notes: str = "") -> bool:
        """Mark a violation as resolved."""
        if 0 <= violation_index < len(self.violations):
            self.violations[violation_index]["resolved"] = True
            self.violations[violation_index]["resolution_date"] = resolution_date.isoformat()
            self.violations[violation_index]["resolution_notes"] = resolution_notes
            return True
        return False
    
    @property
    def has_active_violations(self) -> bool:
        """Check if there are any unresolved violations."""
        return any(not violation.get("resolved", False) for violation in self.violations)
    
    @property
    def compliance_score(self) -> float:
        """Calculate a compliance score (0-1) based on violations and permit status."""
        score = 1.0
        
        # Deduct for active violations
        active_violations = sum(1 for v in self.violations if not v.get("resolved", False))
        score -= min(active_violations * 0.1, 0.5)  # Max 50% deduction for violations
        
        # Deduct for expired permits
        expired_permits = sum(1 for p in self.permits if p.is_expired)
        score -= min(expired_permits * 0.2, 0.3)  # Max 30% deduction for expired permits
        
        return max(score, 0.0)  # Ensure score doesn't go below 0
