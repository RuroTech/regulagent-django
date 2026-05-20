# Regulatory Agent Automation Engine
__version__ = "1.0.0"

from .base import (
    BaseFormAutomator, 
    BaseGISExtractor, 
    BaseTabManager,
    BaseSelectorEngine,
    AutomationResult,
    GISData,
    FormData,
    AuthData
)
from .exceptions import (
    AutomationError, 
    FormSubmissionError, 
    GISExtractionError,
    AuthenticationError,
    SelectorError,
    MultiTabError,
    VaultIntegrationError
)

__all__ = [
    # Base Classes
    "BaseFormAutomator",
    "BaseGISExtractor", 
    "BaseTabManager",
    "BaseSelectorEngine",
    
    # Data Models
    "AutomationResult",
    "GISData", 
    "FormData",
    "AuthData",
    
    # Exceptions
    "AutomationError",
    "FormSubmissionError",
    "GISExtractionError",
    "AuthenticationError", 
    "SelectorError",
    "MultiTabError",
    "VaultIntegrationError"
]
