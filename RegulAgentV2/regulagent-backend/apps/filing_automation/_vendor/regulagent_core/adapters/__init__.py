# RegulAgent Data Adapters
__version__ = "1.0.0"

# Data adapters convert external data sources into domain models
# Each adapter handles a specific data source format

from .rrc_automation_adapter import RRCAutomationAdapter

__all__ = [
    "RRCAutomationAdapter"
]
