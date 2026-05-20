"""Data models for automation workflows."""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Union
from datetime import datetime
from enum import Enum


class AutomationStatus(Enum):
    """Status of automation execution."""
    PENDING = "pending"
    PROCESSING = "processing" 
    COMPLETED = "completed"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"


class TabType(Enum):
    """Type of browser tab for multi-tab workflows."""
    PRIMARY_FORM = "primary_form"
    GIS_VIEWER = "gis_viewer"
    DATA_SOURCE = "data_source"
    VERIFICATION = "verification"
    SUPPORT = "support"


@dataclass
class AuthData:
    """Authentication data structure."""
    username: str
    password: str
    additional_fields: Dict[str, str] = field(default_factory=dict)
    vault_token: Optional[str] = None
    
    def __post_init__(self):
        """Validate auth data."""
        if not self.username or not self.password:
            raise ValueError("Username and password are required")


@dataclass
class GISData:
    """GIS extracted data structure."""
    distance: Optional[str] = None
    direction: Optional[str] = None
    town: Optional[str] = None
    well_type: Optional[str] = None
    section: Optional[str] = None
    block: Optional[str] = None
    survey: Optional[str] = None
    coordinates: Optional[Dict[str, float]] = None
    extraction_method: Optional[str] = None
    confidence_score: float = 0.0
    raw_data: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def location_string(self) -> str:
        """Format location as human-readable string."""
        if self.distance and self.direction and self.town:
            return f"{self.distance} miles {self.direction} of {self.town}"
        return "Location data incomplete"
    
    @property
    def is_complete(self) -> bool:
        """Check if essential GIS data is present."""
        return bool(self.distance and self.direction and self.town)


@dataclass
class FormData:
    """Form data structure for automation."""
    api_number: str
    form_type: str
    test_mode: bool = False
    vault_data: Dict[str, Any] = field(default_factory=dict)
    calculated_data: Dict[str, Any] = field(default_factory=dict)
    file_attachments: List[str] = field(default_factory=list)
    priority: str = "normal"  # normal, high, urgent
    client_metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate form data."""
        if not self.api_number or not self.form_type:
            raise ValueError("API number and form type are required")
        
        # Clean API number (remove prefixes)
        self.api_number = self.api_number.replace("42-", "").replace("-", "")


@dataclass
class SelectorConfig:
    """Configuration for element selectors."""
    primary: str
    fallbacks: List[str] = field(default_factory=list)
    timeout: int = 15000
    description: str = ""
    
    @property
    def all_selectors(self) -> List[str]:
        """Get all selectors including primary and fallbacks."""
        return [self.primary] + self.fallbacks


@dataclass
class TabConfig:
    """Configuration for browser tab."""
    tab_id: str
    tab_type: TabType
    url: str
    wait_for_load: bool = True
    load_timeout: int = 30000
    required: bool = True
    
    
@dataclass
class WorkflowStep:
    """Individual step in automation workflow."""
    step_id: str
    name: str
    description: str
    required_tabs: List[str] = field(default_factory=list)
    timeout: int = 30000
    retry_count: int = 2
    
    
@dataclass
class AutomationResult:
    """Result of automation execution."""
    session_id: str
    status: AutomationStatus
    form_data: FormData
    gis_data: Optional[GISData] = None
    execution_log: List[Dict[str, Any]] = field(default_factory=list)
    error_details: Optional[Dict[str, Any]] = None
    duration_ms: Optional[int] = None
    agency_confirmation: Optional[str] = None
    screenshots: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    def add_log_entry(self, level: str, message: str, step: str = None, **kwargs):
        """Add entry to execution log."""
        self.execution_log.append({
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "message": message,
            "step": step,
            **kwargs
        })
        self.updated_at = datetime.now()
    
    def mark_completed(self, confirmation: str = None):
        """Mark automation as completed."""
        self.status = AutomationStatus.COMPLETED
        self.agency_confirmation = confirmation
        self.updated_at = datetime.now()
        self.add_log_entry("INFO", "Automation completed successfully")
    
    def mark_failed(self, error: str, error_code: str = None):
        """Mark automation as failed."""
        self.status = AutomationStatus.FAILED
        self.error_details = {
            "error": error,
            "error_code": error_code,
            "timestamp": datetime.now().isoformat()
        }
        self.updated_at = datetime.now()
        self.add_log_entry("ERROR", f"Automation failed: {error}")
    
    @property
    def success_rate(self) -> float:
        """Calculate success rate based on completed steps."""
        if not self.execution_log:
            return 0.0
        
        total_steps = len([log for log in self.execution_log if log.get("step")])
        success_steps = len([log for log in self.execution_log 
                           if log.get("level") == "INFO" and log.get("step")])
        
        return success_steps / total_steps if total_steps > 0 else 0.0
