# RegulAgent Domain Models
__version__ = "1.0.0"

# Industry-separated domain models for regulatory automation
# Each industry has its own domain models while sharing common base classes

from .shared import BaseRecord, Location, PlanSchema
from .oil_gas import WellRecord, LeaseRecord
# from .construction import ProjectRecord, PropertyRecord  # Future
# from .environmental import SiteRecord, ContaminationRecord  # Future

__all__ = [
    # Shared models
    "BaseRecord",
    "Location", 
    "PlanSchema",
    
    # Oil & Gas models
    "WellRecord",
    "LeaseRecord",
    
    # Future industry models will be added here
]
