"""
W3A Form Data Model with proper PII classification.

This defines how W3A form data should be handled based on privacy requirements.
"""

from datetime import datetime
from typing import Dict, Optional
from pydantic import BaseModel, Field


class W3APIIData(BaseModel):
    """🔐 PII data that must come from client vault."""
    
    contact_phone: str = Field(..., description="Individual contact phone number")
    contact_email: str = Field(..., description="Individual contact email address")
    
    class Config:
        # This data should NEVER be stored in backend
        json_encoders = {
            str: lambda v: "***REDACTED***" if len(v) > 0 else v
        }


class W3ABusinessData(BaseModel):
    """🏢 Business data that can be stored in user profile."""
    
    cementing_company_name: str = Field(..., description="Name of cementing company")
    cementing_company_address: str = Field(..., description="Full address of cementing company")
    cementing_company_phone: Optional[str] = Field(None, description="Business phone number")
    cementing_company_contact: Optional[str] = Field(None, description="Business contact name")
    
    # Additional business fields that are commonly reused
    operator_name: Optional[str] = Field(None, description="Operating company name")
    operator_address: Optional[str] = Field(None, description="Operating company address")


class W3AOperationalData(BaseModel):
    """📊 Operational data specific to each form submission."""
    
    anticipated_plugging_date: datetime = Field(..., description="Anticipated plugging date")
    well_api_number: str = Field(..., description="API number of well to be plugged")
    
    # Technical specifications
    total_depth: Optional[float] = Field(None, description="Total depth of well (feet)")
    casing_depth: Optional[float] = Field(None, description="Casing depth (feet)")
    plug_method: Optional[str] = Field(None, description="Plugging method")
    
    # Regulatory details
    permit_number: Optional[str] = Field(None, description="Drilling permit number")
    lease_name: Optional[str] = Field(None, description="Lease name")
    field_name: Optional[str] = Field(None, description="Field name")


class W3AFormData(BaseModel):
    """Complete W3A form data combining all sources."""
    
    # Core identifiers
    api_number: str
    form_type: str = "W3A"
    
    # Data classification markers
    requires_pii: bool = True
    requires_business_profile: bool = True
    
    # Operational data (passed directly)
    operational_data: W3AOperationalData
    
    # Business data (loaded from user profile)
    business_data: Optional[W3ABusinessData] = None
    
    # PII data (retrieved from vault - never stored)
    pii_data: Optional[W3APIIData] = None
    
    # Automation settings
    test_mode: bool = True
    multi_tab: bool = True


class VaultRequest(BaseModel):
    """Request structure for retrieving PII from client vault."""
    
    session_id: str
    required_fields: list[str] = [
        "contact_phone",
        "contact_email"
    ]
    vault_provider: str = Field(..., description="1Password, LastPass, etc.")
    expiry_minutes: int = Field(default=30, description="How long PII can be held in memory")


class W3ASubmissionRequest(BaseModel):
    """API request for W3A form submission."""
    
    # Required operational data
    operational_data: W3AOperationalData
    
    # Vault configuration for PII retrieval
    vault_request: VaultRequest
    
    # Optional overrides for business data
    business_data_overrides: Optional[Dict[str, str]] = None
    
    # Automation settings
    test_mode: bool = True


class W3ASubmissionResult(BaseModel):
    """Result of W3A form submission."""
    
    session_id: str
    status: str  # "success", "failed", "pending"
    
    # Form submission details
    rrc_confirmation: Optional[str] = None
    submission_timestamp: Optional[datetime] = None
    
    # Learning system insights
    learning_data: Dict = Field(default_factory=dict)
    confidence_score: float = 0.0
    
    # Automation performance
    duration_ms: int
    steps_completed: int
    
    # Any errors or warnings
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# Example usage in API endpoint
EXAMPLE_W3A_SUBMISSION = {
    "operational_data": {
        "anticipated_plugging_date": "2024-03-15T10:00:00Z",
        "well_api_number": "42-00333756",
        "total_depth": 5700.0,
        "casing_depth": 4800.0,
        "plug_method": "Cement Plug",
        "permit_number": "749238",
        "lease_name": "A.M. MURPHY",
        "field_name": "SCHLEICHER"
    },
    "vault_request": {
        "session_id": "w3a_submission_001",
        "required_fields": ["contact_phone", "contact_email"],
        "vault_provider": "1Password",
        "expiry_minutes": 30
    },
    "test_mode": True
}

# Business data stored in user profile (separate from PII)
EXAMPLE_BUSINESS_PROFILE = {
    "cementing_company_name": "ACME Cementing Services",
    "cementing_company_address": "123 Industrial Blvd, Houston, TX 77001",
    "cementing_company_phone": "(713) 555-0123",  # Business phone, not PII
    "cementing_company_contact": "Operations Department",  # Generic contact, not individual
    "operator_name": "JMR Energy Partners",
    "operator_address": "456 Energy Plaza, Midland, TX 79701"
}
