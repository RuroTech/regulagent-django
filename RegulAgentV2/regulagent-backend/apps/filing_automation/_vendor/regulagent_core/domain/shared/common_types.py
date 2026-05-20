"""
Common types and enums used across all industries.

These are truly universal concepts that apply to any regulatory domain.
"""

from enum import Enum
from typing import Literal


class RecordStatus(str, Enum):
    """Universal record status - applies to any industry."""
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    PROCESSING = "processing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ConfidenceLevel(str, Enum):
    """Confidence level in data accuracy - universal across industries."""
    VERY_LOW = "very_low"      # 0-20% confidence
    LOW = "low"                # 20-40% confidence  
    MEDIUM = "medium"          # 40-70% confidence
    HIGH = "high"              # 70-90% confidence
    VERY_HIGH = "very_high"    # 90-100% confidence


class PriorityLevel(str, Enum):
    """Priority level for processing - universal concept."""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"
    CRITICAL = "critical"


class DataSource(str, Enum):
    """Common data sources across industries."""
    MANUAL_ENTRY = "manual_entry"
    API_IMPORT = "api_import"
    FILE_UPLOAD = "file_upload"
    WEB_SCRAPING = "web_scraping"
    DATABASE_SYNC = "database_sync"
    THIRD_PARTY = "third_party"


# Type aliases for common concepts
Currency = Literal["USD", "CAD", "EUR", "GBP"]
DistanceUnit = Literal["ft", "m", "km", "mi"]
VolumeUnit = Literal["bbl", "gal", "L", "m3", "ft3"]
PressureUnit = Literal["psi", "bar", "kPa", "MPa"]
TemperatureUnit = Literal["F", "C", "K"]
