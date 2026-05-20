"""Custom exceptions for regulatory form automation."""

class AutomationError(Exception):
    """Base exception for automation errors."""
    
    def __init__(self, message: str, error_code: str = None, context: dict = None):
        super().__init__(message)
        self.error_code = error_code or "AUTOMATION_ERROR"
        self.context = context or {}


class FormSubmissionError(AutomationError):
    """Raised when form submission fails."""
    
    def __init__(self, message: str, form_type: str = None, step: str = None, **kwargs):
        super().__init__(message, error_code="FORM_SUBMISSION_ERROR", **kwargs)
        self.form_type = form_type
        self.step = step


class GISExtractionError(AutomationError):
    """Raised when GIS data extraction fails."""
    
    def __init__(self, message: str, gis_url: str = None, extraction_type: str = None, **kwargs):
        super().__init__(message, error_code="GIS_EXTRACTION_ERROR", **kwargs)
        self.gis_url = gis_url
        self.extraction_type = extraction_type


class AuthenticationError(AutomationError):
    """Raised when authentication fails."""
    
    def __init__(self, message: str, agency: str = None, **kwargs):
        super().__init__(message, error_code="AUTHENTICATION_ERROR", **kwargs)
        self.agency = agency


class SelectorError(AutomationError):
    """Raised when element selectors fail."""
    
    def __init__(self, message: str, selector: str = None, timeout: int = None, **kwargs):
        super().__init__(message, error_code="SELECTOR_ERROR", **kwargs)
        self.selector = selector
        self.timeout = timeout


class MultiTabError(AutomationError):
    """Raised when multi-tab coordination fails."""
    
    def __init__(self, message: str, tab_count: int = None, failed_tab: str = None, **kwargs):
        super().__init__(message, error_code="MULTI_TAB_ERROR", **kwargs)
        self.tab_count = tab_count
        self.failed_tab = failed_tab


class VaultIntegrationError(AutomationError):
    """Raised when vault data retrieval fails."""
    
    def __init__(self, message: str, vault_type: str = None, field: str = None, **kwargs):
        super().__init__(message, error_code="VAULT_INTEGRATION_ERROR", **kwargs)
        self.vault_type = vault_type
        self.field = field
