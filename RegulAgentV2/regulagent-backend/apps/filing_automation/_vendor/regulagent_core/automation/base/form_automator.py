"""Base form automation class with multi-tab workflow support."""

import asyncio
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from playwright.async_api import BrowserContext, Page
import logging

from .data_models import (
    FormData, AuthData, AutomationResult, AutomationStatus, 
    TabConfig, WorkflowStep, SelectorConfig
)
from .tab_manager import BaseTabManager
from .selector_engine import BaseSelectorEngine
from ..exceptions import FormSubmissionError, AuthenticationError


logger = logging.getLogger(__name__)


class BaseFormAutomator(ABC):
    """Base class for regulatory form automation with multi-tab support."""
    
    def __init__(self, context: BrowserContext, session_id: str):
        self.context = context
        self.session_id = session_id
        self.result = AutomationResult(
            session_id=session_id,
            status=AutomationStatus.PENDING,
            form_data=None  # Will be set during execution
        )
        
        # Core components
        self.tab_manager = BaseTabManager(context, self.result)
        self.selector_engine = BaseSelectorEngine(self.result)
        
        # Workflow state
        self._workflow_steps: List[WorkflowStep] = []
        self._current_step: Optional[str] = None
        
        # Configuration (to be defined by subclasses)
        self.agency_config: Dict[str, Any] = {}
        self.form_selectors: Dict[str, SelectorConfig] = {}
        
    # Abstract methods that must be implemented by agency-specific classes
    
    @abstractmethod
    async def authenticate(self, auth_data: AuthData, multi_tab: bool = False) -> bool:
        """Authenticate with the agency system."""
        pass
    
    @abstractmethod
    async def navigate_to_form(self, form_type: str, multi_tab: bool = False) -> Page:
        """Navigate to the specific form interface."""
        pass
    
    @abstractmethod
    async def fill_form_fields(self, form_data: FormData, multi_tab: bool = False) -> bool:
        """Fill form fields with provided data."""
        pass
    
    @abstractmethod
    def get_workflow_steps(self, form_type: str, multi_tab: bool = False) -> List[WorkflowStep]:
        """Get the workflow steps for the specific form type."""
        pass
    
    # Core workflow methods (reusable across agencies)
    
    async def execute_automation(
        self, 
        form_data: FormData, 
        auth_data: AuthData,
        multi_tab: bool = False
    ) -> AutomationResult:
        """Main automation execution workflow."""
        
        self.result.form_data = form_data
        self.result.status = AutomationStatus.PROCESSING
        
        start_time = asyncio.get_event_loop().time()
        
        try:
            logger.info(f"Starting automation for {form_data.form_type} (API: {form_data.api_number})")
            
            # Setup workflow steps
            self._workflow_steps = self.get_workflow_steps(form_data.form_type, multi_tab)
            
            # Execute workflow phases
            if multi_tab:
                await self._setup_multi_tab_workflow(form_data)
            
            await self._execute_authentication(auth_data, multi_tab)
            await self._execute_navigation(form_data.form_type, multi_tab)
            await self._execute_form_filling(form_data, multi_tab)
            
            if not form_data.test_mode:
                await self._execute_submission(multi_tab)
            else:
                await self._save_as_draft()
            
            # Calculate duration and mark success
            duration = int((asyncio.get_event_loop().time() - start_time) * 1000)
            self.result.duration_ms = duration
            self.result.mark_completed()
            
            logger.info(f"Automation completed successfully in {duration}ms")
            
        except Exception as e:
            error_msg = f"Automation failed: {str(e)}"
            logger.error(error_msg)
            self.result.mark_failed(error_msg, getattr(e, 'error_code', None))
            
        finally:
            # Cleanup
            if multi_tab:
                await self.tab_manager.close_tabs()
            
        return self.result
    
    async def execute_post_auth(
        self,
        form_data: FormData,
        multi_tab: bool = False,
    ) -> "AutomationResult":
        """Run the filing pipeline assuming authentication has already completed.

        Identical to ``execute_automation`` but skips the ``_execute_authentication``
        phase.  Call this when the caller has already driven ``authenticate()``
        (e.g. the tracing wrapper in tasks.py that must start tracing AFTER auth).
        """
        self.result.form_data = form_data
        self.result.status = AutomationStatus.PROCESSING

        import asyncio as _asyncio
        start_time = _asyncio.get_event_loop().time()

        try:
            logger.info(
                f"execute_post_auth: starting post-auth phases for "
                f"{form_data.form_type} (API: {form_data.api_number})"
            )

            self._workflow_steps = self.get_workflow_steps(form_data.form_type, multi_tab)

            if multi_tab:
                await self._setup_multi_tab_workflow(form_data)

            await self._execute_navigation(form_data.form_type, multi_tab)
            await self._execute_form_filling(form_data, multi_tab)

            if not form_data.test_mode:
                await self._execute_submission(multi_tab)
            else:
                await self._save_as_draft()

            duration = int((_asyncio.get_event_loop().time() - start_time) * 1000)
            self.result.duration_ms = duration
            self.result.mark_completed()

            logger.info(f"execute_post_auth: completed in {duration}ms")

        except Exception as e:
            error_msg = f"Post-auth automation failed: {str(e)}"
            logger.error(error_msg)
            self.result.mark_failed(error_msg, getattr(e, "error_code", None))

        finally:
            if multi_tab:
                await self.tab_manager.close_tabs()

        return self.result

    async def _setup_multi_tab_workflow(self, form_data: FormData):
        """Setup multiple tabs for complex workflows."""
        
        self.result.add_log_entry("INFO", "Setting up multi-tab workflow", step="setup")
        
        # Get tab configurations from agency-specific implementation
        tab_configs = self.get_tab_configurations(form_data.form_type)
        
        # Register and load tabs
        for config in tab_configs:
            await self.tab_manager.register_tab(config)
            await self.tab_manager.load_tab(config.tab_id)
        
        logger.info(f"Multi-tab setup complete: {len(tab_configs)} tabs")
    
    async def _execute_authentication(self, auth_data: AuthData, multi_tab: bool):
        """Execute authentication workflow step."""
        
        self._current_step = "authentication"
        self.result.add_log_entry("INFO", "Starting authentication", step=self._current_step)
        
        try:
            success = await self.authenticate(auth_data, multi_tab)
            if not success:
                raise AuthenticationError("Authentication failed", agency=self.__class__.__name__)
                
        except Exception as e:
            raise AuthenticationError(f"Authentication error: {str(e)}", agency=self.__class__.__name__)
    
    async def _execute_navigation(self, form_type: str, multi_tab: bool):
        """Execute form navigation workflow step."""
        
        self._current_step = "navigation"
        self.result.add_log_entry("INFO", f"Navigating to {form_type} form", step=self._current_step)
        
        try:
            await self.navigate_to_form(form_type, multi_tab)
        except Exception as e:
            raise FormSubmissionError(f"Navigation failed: {str(e)}", form_type=form_type, step="navigation")
    
    async def _execute_form_filling(self, form_data: FormData, multi_tab: bool):
        """Execute form filling workflow step."""
        
        self._current_step = "form_filling"
        self.result.add_log_entry("INFO", "Filling form fields", step=self._current_step)
        
        try:
            success = await self.fill_form_fields(form_data, multi_tab)
            if not success:
                raise FormSubmissionError("Form filling failed", form_type=form_data.form_type, step="form_filling")
                
        except Exception as e:
            raise FormSubmissionError(f"Form filling error: {str(e)}", form_type=form_data.form_type, step="form_filling")
    
    async def _execute_submission(self, multi_tab: bool):
        """Execute form submission workflow step."""
        
        self._current_step = "submission"
        self.result.add_log_entry("INFO", "Submitting form", step=self._current_step)
        
        try:
            # This is often agency-specific, so provide a default implementation
            await self.submit_form(multi_tab)
        except Exception as e:
            raise FormSubmissionError(f"Form submission failed: {str(e)}", step="submission")
    
    async def _save_as_draft(self):
        """Save form as draft (test mode)."""
        
        self._current_step = "save_draft"
        self.result.add_log_entry("INFO", "Saving form as draft (test mode)", step=self._current_step)
        
        try:
            await self.save_draft()
        except Exception as e:
            logger.warning(f"Draft save failed: {str(e)}")
            # Don't fail the automation for draft save issues
    
    # Helper methods that can be overridden by subclasses
    
    async def submit_form(self, multi_tab: bool = False) -> bool:
        """Default form submission implementation."""
        
        # Look for common submit button selectors
        submit_selectors = [
            'button[type="submit"]',
            'input[type="submit"]', 
            'button:has-text("Submit")',
            '.submit-btn',
            '#submit-button'
        ]
        
        submit_config = SelectorConfig(
            primary=submit_selectors[0],
            fallbacks=submit_selectors[1:],
            description="Submit button"
        )
        
        primary_tab = "primary_form" if multi_tab else None
        page = await self.tab_manager.switch_to_tab(primary_tab) if multi_tab else self.context.pages[0]
        
        return await self.selector_engine.smart_click(page, submit_config)
    
    async def save_draft(self) -> bool:
        """Default draft save implementation."""
        
        # Look for common save/draft button selectors
        save_selectors = [
            'button:has-text("Save")',
            'button:has-text("Draft")',
            '.save-btn',
            '#save-button'
        ]
        
        save_config = SelectorConfig(
            primary=save_selectors[0],
            fallbacks=save_selectors[1:],
            description="Save/Draft button"
        )
        
        page = self.context.pages[0]  # Use primary page for save
        
        try:
            return await self.selector_engine.smart_click(page, save_config)
        except Exception:
            logger.warning("No save/draft button found - form may auto-save")
            return True
    
    async def handle_file_upload(
        self, 
        page: Page, 
        file_selector: SelectorConfig,
        file_path: str
    ) -> bool:
        """Handle file upload with validation."""
        
        try:
            file_input = await self.selector_engine.find_element(page, file_selector)
            await file_input.set_input_files(file_path)
            
            self.result.add_log_entry(
                "INFO",
                f"File uploaded: {file_path}",
                step=self._current_step
            )
            
            return True
            
        except Exception as e:
            error_msg = f"File upload failed: {str(e)}"
            logger.error(error_msg)
            self.result.add_log_entry("ERROR", error_msg, step=self._current_step)
            return False
    
    async def wait_for_page_transition(
        self, 
        page: Page, 
        timeout: int = 30000,
        expected_url_pattern: str = None
    ) -> bool:
        """Wait for page navigation/transition to complete."""
        
        try:
            if expected_url_pattern:
                await page.wait_for_url(expected_url_pattern, timeout=timeout)
            else:
                await page.wait_for_load_state('networkidle', timeout=timeout)
            
            return True
            
        except Exception as e:
            logger.warning(f"Page transition wait failed: {str(e)}")
            return False
    
    # Methods to be implemented by agency-specific subclasses
    
    def get_tab_configurations(self, form_type: str) -> List[TabConfig]:
        """Get tab configurations for multi-tab workflows. Override in subclasses."""
        return []
    
    def get_selector_config(self, field_name: str) -> Optional[SelectorConfig]:
        """Get selector configuration for a field. Override in subclasses."""
        return self.form_selectors.get(field_name)
    
    # Utility properties
    
    @property
    def current_step(self) -> Optional[str]:
        """Get current workflow step."""
        return self._current_step
    
    @property
    def workflow_progress(self) -> Dict[str, Any]:
        """Get workflow progress information."""
        total_steps = len(self._workflow_steps)
        completed_steps = len([log for log in self.result.execution_log if log.get("step")])
        
        return {
            "current_step": self._current_step,
            "completed_steps": completed_steps,
            "total_steps": total_steps,
            "progress_percentage": (completed_steps / total_steps * 100) if total_steps > 0 else 0
        }
