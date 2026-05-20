"""Smart element selection engine with fallback strategies."""

import asyncio
from typing import Dict, List, Optional, Any, Union
from playwright.async_api import Page, ElementHandle
import logging

from .data_models import SelectorConfig, AutomationResult
from ..exceptions import SelectorError


logger = logging.getLogger(__name__)


class BaseSelectorEngine:
    """Smart element selector with fallback strategies and learning capabilities."""
    
    def __init__(self, result: AutomationResult):
        self.result = result
        self.selector_success_cache: Dict[str, str] = {}  # Cache successful selectors
        self.selector_performance: Dict[str, float] = {}  # Track selector performance
    
    async def find_element(
        self, 
        page: Page, 
        config: SelectorConfig,
        wait_strategy: str = "default"
    ) -> ElementHandle:
        """Find element using smart selector strategy with fallbacks."""
        
        start_time = asyncio.get_event_loop().time()
        
        # Try cached successful selector first
        cache_key = f"{config.description}_{page.url}"
        if cache_key in self.selector_success_cache:
            cached_selector = self.selector_success_cache[cache_key]
            try:
                element = await self._try_selector(page, cached_selector, config.timeout, wait_strategy)
                if element:
                    logger.debug(f"Found element using cached selector: {cached_selector}")
                    self._update_performance_metrics(cached_selector, start_time)
                    return element
            except Exception:
                # Cache miss, remove from cache and continue with normal flow
                del self.selector_success_cache[cache_key]
        
        # Try selectors in order: primary, then fallbacks
        last_error = None
        for i, selector in enumerate(config.all_selectors):
            try:
                logger.debug(f"Trying selector {i+1}/{len(config.all_selectors)}: {selector}")
                
                element = await self._try_selector(page, selector, config.timeout, wait_strategy)
                if element:
                    # Cache successful selector for future use
                    self.selector_success_cache[cache_key] = selector
                    self._update_performance_metrics(selector, start_time)
                    
                    self.result.add_log_entry(
                        "INFO",
                        f"Element found: {config.description} using selector {i+1}",
                        step="element_selection",
                        selector=selector,
                        attempt=i+1
                    )
                    
                    return element
                    
            except Exception as e:
                last_error = e
                logger.debug(f"Selector failed: {selector} - {str(e)}")
                continue
        
        # All selectors failed
        error_msg = f"All selectors failed for '{config.description}': {last_error}"
        logger.error(error_msg)
        
        self.result.add_log_entry(
            "ERROR",
            error_msg,
            step="element_selection",
            selectors_tried=config.all_selectors
        )
        
        raise SelectorError(
            error_msg,
            selector=config.primary,
            timeout=config.timeout
        )
    
    async def _try_selector(
        self, 
        page: Page, 
        selector: str, 
        timeout: int,
        wait_strategy: str
    ) -> Optional[ElementHandle]:
        """Try a single selector with specified wait strategy."""
        
        try:
            if wait_strategy == "visible":
                return await page.wait_for_selector(selector, timeout=timeout, state="visible")
            elif wait_strategy == "attached":
                return await page.wait_for_selector(selector, timeout=timeout, state="attached") 
            elif wait_strategy == "hidden":
                return await page.wait_for_selector(selector, timeout=timeout, state="hidden")
            elif wait_strategy == "immediate":
                return await page.query_selector(selector)
            else:  # default
                return await page.wait_for_selector(selector, timeout=timeout)
                
        except Exception:
            return None
    
    async def find_multiple_elements(
        self,
        page: Page,
        config: SelectorConfig,
        min_count: int = 1
    ) -> List[ElementHandle]:
        """Find multiple elements matching selector criteria."""
        
        for selector in config.all_selectors:
            try:
                elements = await page.query_selector_all(selector)
                if len(elements) >= min_count:
                    logger.debug(f"Found {len(elements)} elements with selector: {selector}")
                    return elements
            except Exception as e:
                logger.debug(f"Multi-element selector failed: {selector} - {str(e)}")
                continue
        
        raise SelectorError(
            f"Could not find {min_count}+ elements for '{config.description}'",
            selector=config.primary
        )
    
    async def smart_click(
        self,
        page: Page,
        config: SelectorConfig,
        click_options: Dict[str, Any] = None
    ) -> bool:
        """Smart click with element validation and retry logic."""
        
        click_options = click_options or {}
        element = await self.find_element(page, config)
        
        try:
            # Scroll element into view if needed
            await element.scroll_into_view_if_needed()
            
            # Wait for element to be actionable
            await element.wait_for_element_state("visible")
            await element.wait_for_element_state("stable")
            
            # Attempt click
            await element.click(**click_options)
            
            # Small delay to allow for page changes
            await asyncio.sleep(0.5)
            
            self.result.add_log_entry(
                "INFO",
                f"Successfully clicked: {config.description}",
                step="element_interaction"
            )
            
            return True
            
        except Exception as e:
            # Try JavaScript click as fallback
            try:
                await element.evaluate("element => element.click()")
                logger.info(f"JavaScript click succeeded for: {config.description}")
                return True
            except Exception:
                error_msg = f"Click failed for '{config.description}': {str(e)}"
                logger.error(error_msg)
                raise SelectorError(error_msg, selector=config.primary)
    
    async def smart_fill(
        self,
        page: Page,
        config: SelectorConfig,
        value: str,
        clear_first: bool = True
    ) -> bool:
        """Smart form filling with validation."""
        
        element = await self.find_element(page, config)
        
        try:
            # Scroll into view
            await element.scroll_into_view_if_needed()
            
            # Clear existing value if requested
            if clear_first:
                await element.clear()
            
            # Fill value
            await element.fill(value)
            
            # Trigger input events for dynamic forms
            await element.dispatch_event("input")
            await element.dispatch_event("change")
            
            # Verify value was set (for critical fields)
            actual_value = await element.get_attribute("value")
            if actual_value != value:
                logger.warning(f"Fill verification failed. Expected: {value}, Got: {actual_value}")
            
            self.result.add_log_entry(
                "INFO",
                f"Successfully filled: {config.description} = '{value[:50]}{'...' if len(value) > 50 else ''}'",
                step="form_filling"
            )
            
            return True
            
        except Exception as e:
            error_msg = f"Fill failed for '{config.description}': {str(e)}"
            logger.error(error_msg)
            raise SelectorError(error_msg, selector=config.primary)
    
    async def smart_select(
        self,
        page: Page,
        config: SelectorConfig,
        value: str,
        select_by: str = "value"  # value, label, index
    ) -> bool:
        """Smart dropdown/select element handling."""
        
        element = await self.find_element(page, config)
        
        try:
            if select_by == "value":
                await element.select_option(value=value)
            elif select_by == "label":
                await element.select_option(label=value)
            elif select_by == "index":
                await element.select_option(index=int(value))
            else:
                raise ValueError(f"Invalid select_by option: {select_by}")
            
            self.result.add_log_entry(
                "INFO",
                f"Successfully selected: {config.description} = '{value}'",
                step="form_selection"
            )
            
            return True
            
        except Exception as e:
            error_msg = f"Select failed for '{config.description}': {str(e)}"
            logger.error(error_msg)
            raise SelectorError(error_msg, selector=config.primary)
    
    async def extract_text(
        self,
        page: Page,
        config: SelectorConfig,
        attribute: str = "text"  # text, value, href, etc.
    ) -> Optional[str]:
        """Extract text or attribute from element."""
        
        try:
            element = await self.find_element(page, config)
            
            if attribute == "text":
                text = await element.inner_text()
            else:
                text = await element.get_attribute(attribute)
            
            logger.debug(f"Extracted {attribute} from '{config.description}': {text[:100] if text else 'None'}")
            return text
            
        except SelectorError:
            # Element not found, return None instead of raising
            logger.warning(f"Could not extract {attribute} from '{config.description}' - element not found")
            return None
        except Exception as e:
            logger.error(f"Text extraction failed for '{config.description}': {str(e)}")
            return None
    
    def _update_performance_metrics(self, selector: str, start_time: float):
        """Update performance metrics for selector optimization."""
        duration = asyncio.get_event_loop().time() - start_time
        
        if selector in self.selector_performance:
            # Moving average of performance
            self.selector_performance[selector] = (
                self.selector_performance[selector] * 0.7 + duration * 0.3
            )
        else:
            self.selector_performance[selector] = duration
    
    def get_selector_stats(self) -> Dict[str, Any]:
        """Get selector performance statistics."""
        return {
            "cached_selectors": len(self.selector_success_cache),
            "performance_data": self.selector_performance.copy(),
            "fastest_selectors": sorted(
                self.selector_performance.items(), 
                key=lambda x: x[1]
            )[:5]
        }
