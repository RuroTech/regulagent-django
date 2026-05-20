"""
Universal base record class for all industries.

This provides the foundation that all domain records inherit from,
ensuring consistent structure across oil & gas, construction, environmental, etc.
"""

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Dict, Any, Optional
from .common_types import RecordStatus, ConfidenceLevel


class BaseRecord(BaseModel):
    """Universal base class for all industry records."""
    
    # Universal identifiers
    record_id: str = Field(..., description="Unique identifier for this record")
    record_type: str = Field(..., description="Type of record (well, project, site, etc.)")
    
    # Universal timestamps
    created_at: datetime = Field(default_factory=datetime.now, description="When record was created")
    updated_at: datetime = Field(default_factory=datetime.now, description="When record was last updated")
    
    # Universal status tracking
    status: RecordStatus = Field(default=RecordStatus.DRAFT, description="Current status of the record")
    confidence_level: ConfidenceLevel = Field(default=ConfidenceLevel.MEDIUM, description="Confidence in data accuracy")
    
    # Universal metadata
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Extensible metadata for industry-specific data")
    source_system: Optional[str] = Field(None, description="System that provided this data")
    source_reference: Optional[str] = Field(None, description="Reference ID in source system")
    
    # Universal validation
    validation_errors: list[str] = Field(default_factory=list, description="Any validation errors found")
    validation_warnings: list[str] = Field(default_factory=list, description="Any validation warnings")
    
    class Config:
        """Universal configuration for all records."""
        validate_assignment = True
        use_enum_values = True
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
    
    def update_timestamp(self) -> None:
        """Update the updated_at timestamp."""
        self.updated_at = datetime.now()
    
    def add_validation_error(self, error: str) -> None:
        """Add a validation error."""
        if error not in self.validation_errors:
            self.validation_errors.append(error)
            self.update_timestamp()
    
    def add_validation_warning(self, warning: str) -> None:
        """Add a validation warning."""
        if warning not in self.validation_warnings:
            self.validation_warnings.append(warning)
            self.update_timestamp()
    
    def clear_validation_issues(self) -> None:
        """Clear all validation errors and warnings."""
        self.validation_errors.clear()
        self.validation_warnings.clear()
        self.update_timestamp()
    
    @property
    def is_valid(self) -> bool:
        """Check if record has no validation errors."""
        return len(self.validation_errors) == 0
    
    @property
    def has_warnings(self) -> bool:
        """Check if record has validation warnings."""
        return len(self.validation_warnings) > 0
