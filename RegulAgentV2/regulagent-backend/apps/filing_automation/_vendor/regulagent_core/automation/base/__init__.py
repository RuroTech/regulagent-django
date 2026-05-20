"""Base automation classes for regulatory form processing."""

from .tab_manager import BaseTabManager
from .form_automator import BaseFormAutomator
from .gis_extractor import BaseGISExtractor
from .selector_engine import BaseSelectorEngine
from .data_models import AutomationResult, GISData, FormData, AuthData

__all__ = [
    "BaseTabManager",
    "BaseFormAutomator", 
    "BaseGISExtractor",
    "BaseSelectorEngine",
    "AutomationResult",
    "GISData",
    "FormData", 
    "AuthData"
]
