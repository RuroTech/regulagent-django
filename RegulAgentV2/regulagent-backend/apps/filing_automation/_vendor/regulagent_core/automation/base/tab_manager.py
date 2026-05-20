"""Multi-tab browser management for complex workflows."""

import asyncio
from typing import Dict, List, Optional, Any
from playwright.async_api import BrowserContext, Page
import logging

from .data_models import TabConfig, TabType, AutomationResult
from ..exceptions import MultiTabError, SelectorError


logger = logging.getLogger(__name__)


class BaseTabManager:
    """Manages multiple browser tabs for complex automation workflows."""
    
    def __init__(self, context: BrowserContext, result: AutomationResult):
        self.context = context
        self.result = result
        self.tabs: Dict[str, Page] = {}
        self.tab_configs: Dict[str, TabConfig] = {}
        self._active_tab: Optional[str] = None
    
    async def register_tab(self, config: TabConfig) -> Page:
        """Register and create a new tab."""
        try:
            page = await self.context.new_page()
            self.tabs[config.tab_id] = page
            self.tab_configs[config.tab_id] = config
            
            logger.info(f"Registered tab '{config.tab_id}' for {config.tab_type.value}")
            self.result.add_log_entry(
                "INFO", 
                f"Tab registered: {config.tab_id} ({config.tab_type.value})",
                step="tab_setup"
            )
            
            return page
            
        except Exception as e:
            error_msg = f"Failed to register tab '{config.tab_id}': {str(e)}"
            logger.error(error_msg)
            raise MultiTabError(error_msg, failed_tab=config.tab_id)
    
    async def load_tab(self, tab_id: str, custom_url: str = None) -> Page:
        """Load content in specified tab."""
        if tab_id not in self.tabs:
            raise MultiTabError(f"Tab '{tab_id}' not registered")
        
        config = self.tab_configs[tab_id]
        page = self.tabs[tab_id]
        url = custom_url or config.url
        
        try:
            logger.info(f"Loading {url} in tab '{tab_id}'")
            await page.goto(url)
            
            if config.wait_for_load:
                await page.wait_for_load_state('networkidle', timeout=config.load_timeout)
                
            self.result.add_log_entry(
                "INFO",
                f"Tab loaded: {tab_id} -> {url}",
                step="tab_loading"
            )
            
            return page
            
        except Exception as e:
            error_msg = f"Failed to load tab '{tab_id}' with URL {url}: {str(e)}"
            logger.error(error_msg)
            
            if config.required:
                raise MultiTabError(error_msg, failed_tab=tab_id)
            else:
                logger.warning(f"Non-required tab failed, continuing: {error_msg}")
                return page
    
    async def switch_to_tab(self, tab_id: str) -> Page:
        """Switch focus to specified tab."""
        if tab_id not in self.tabs:
            raise MultiTabError(f"Tab '{tab_id}' not registered")
        
        page = self.tabs[tab_id]
        await page.bring_to_front()
        self._active_tab = tab_id
        
        logger.debug(f"Switched to tab '{tab_id}'")
        return page
    
    async def execute_in_tab(self, tab_id: str, operation: str, **kwargs) -> Any:
        """Execute operation in specific tab context."""
        page = await self.switch_to_tab(tab_id)
        
        try:
            # Common tab operations
            if operation == "fill_form_field":
                selector = kwargs.get("selector")
                value = kwargs.get("value")
                await page.fill(selector, value)
                return True
                
            elif operation == "click_element":
                selector = kwargs.get("selector")
                await page.click(selector)
                return True
                
            elif operation == "wait_for_selector":
                selector = kwargs.get("selector")
                timeout = kwargs.get("timeout", 15000)
                element = await page.wait_for_selector(selector, timeout=timeout)
                return element
                
            elif operation == "extract_text":
                selector = kwargs.get("selector")
                element = await page.query_selector(selector)
                return await element.inner_text() if element else None
                
            elif operation == "get_page_content":
                return await page.content()
                
            elif operation == "screenshot":
                path = kwargs.get("path", f"tab_{tab_id}_screenshot.png")
                await page.screenshot(path=path)
                return path
                
            else:
                raise ValueError(f"Unknown operation: {operation}")
                
        except Exception as e:
            error_msg = f"Operation '{operation}' failed in tab '{tab_id}': {str(e)}"
            logger.error(error_msg)
            raise MultiTabError(error_msg, failed_tab=tab_id)
    
    async def coordinate_tabs(self, workflow_steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Coordinate operations across multiple tabs."""
        results = {}
        
        for step in workflow_steps:
            step_id = step["step_id"]
            tab_operations = step.get("tab_operations", [])
            
            logger.info(f"Executing workflow step: {step_id}")
            step_results = {}
            
            try:
                # Execute operations across tabs
                for operation in tab_operations:
                    tab_id = operation["tab_id"]
                    op_type = operation["operation"]
                    op_params = operation.get("parameters", {})
                    
                    result = await self.execute_in_tab(tab_id, op_type, **op_params)
                    step_results[f"{tab_id}_{op_type}"] = result
                
                # Handle inter-tab data transfer
                if "data_transfer" in step:
                    transfer_config = step["data_transfer"]
                    source_tab = transfer_config["source_tab"]
                    target_tab = transfer_config["target_tab"]
                    data_key = transfer_config["data_key"]
                    
                    # Get data from source tab
                    source_data = step_results.get(f"{source_tab}_extract_text")
                    if source_data:
                        # Use data in target tab
                        await self.execute_in_tab(
                            target_tab, 
                            "fill_form_field",
                            selector=transfer_config["target_selector"],
                            value=source_data
                        )
                
                results[step_id] = step_results
                self.result.add_log_entry(
                    "INFO",
                    f"Workflow step completed: {step_id}",
                    step=step_id
                )
                
            except Exception as e:
                error_msg = f"Workflow step '{step_id}' failed: {str(e)}"
                logger.error(error_msg)
                self.result.add_log_entry("ERROR", error_msg, step=step_id)
                
                if step.get("required", True):
                    raise MultiTabError(error_msg)
                else:
                    results[step_id] = {"error": error_msg}
        
        return results
    
    async def close_tabs(self, tab_ids: List[str] = None):
        """Close specified tabs or all tabs."""
        tabs_to_close = tab_ids or list(self.tabs.keys())
        
        for tab_id in tabs_to_close:
            if tab_id in self.tabs:
                try:
                    await self.tabs[tab_id].close()
                    del self.tabs[tab_id]
                    del self.tab_configs[tab_id]
                    logger.info(f"Closed tab: {tab_id}")
                except Exception as e:
                    logger.warning(f"Failed to close tab '{tab_id}': {e}")
    
    def get_tab_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all registered tabs."""
        status = {}
        for tab_id, config in self.tab_configs.items():
            status[tab_id] = {
                "type": config.tab_type.value,
                "url": config.url,
                "required": config.required,
                "active": tab_id == self._active_tab,
                "loaded": tab_id in self.tabs
            }
        return status
    
    @property
    def active_tab(self) -> Optional[str]:
        """Get currently active tab ID."""
        return self._active_tab
    
    @property
    def tab_count(self) -> int:
        """Get total number of registered tabs."""
        return len(self.tabs)
