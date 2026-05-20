"""
Learning Integration - Connects the learning engine to automation framework.

This module provides decorators and mixins to automatically capture execution data
and feed it into the learning system without modifying core automation logic.
"""

import asyncio
import functools
from typing import Dict, Any, Optional, Callable
from datetime import datetime
import logging

from ..base.data_models import AutomationResult, SelectorConfig
from ..base.selector_engine import BaseSelectorEngine
from ..base.form_automator import BaseFormAutomator
from ..base.gis_extractor import BaseGISExtractor
from .learning_engine import AutomationLearningEngine

logger = logging.getLogger(__name__)


class LearningIntegratedSelectorEngine(BaseSelectorEngine):
    """
    Enhanced selector engine that automatically captures learning data.
    
    This extends the base selector engine to feed success/failure data
    into the learning system for continuous improvement.
    """
    
    def __init__(self, result: AutomationResult, learning_engine: AutomationLearningEngine = None):
        super().__init__(result)
        self.learning_engine = learning_engine or AutomationLearningEngine()
        self.current_page_data = {}
        self.learned_selectors_cache = {}  # Cache for learned selectors
    
    async def find_element(self, page, config: SelectorConfig, wait_strategy: str = "default"):
        """Override to automatically use learned selectors and capture learning data."""
        
        start_time = datetime.now()
        
        # 🧠 AUTOMATIC LEARNING INTEGRATION: Merge learned selectors with original
        enhanced_config = await self._enhance_config_with_learned_selectors(config, page.url)
        
        attempted_selectors = enhanced_config.all_selectors.copy()
        successful_selector = None
        success = False
        error_info = None
        
        try:
            # Capture page data for learning
            await self._capture_page_context(page)
            
            # 🎯 KEY: Use enhanced config with learned selectors automatically!
            element = await super().find_element(page, enhanced_config, wait_strategy)
            
            # Determine which selector succeeded
            cache_key = f"{enhanced_config.description}_{page.url}"
            if cache_key in self.selector_success_cache:
                successful_selector = self.selector_success_cache[cache_key]
            else:
                successful_selector = enhanced_config.primary  # Assumption if not cached
            
            # Update confidence for successful selector
            await self._update_selector_confidence(successful_selector, success=True)
            
            success = True
            return element
            
        except Exception as e:
            success = False
            error_info = {
                "message": str(e),
                "type": type(e).__name__
            }
            
            # Update confidence for all failed selectors
            for selector in attempted_selectors:
                await self._update_selector_confidence(selector, success=False)
            
            raise
            
        finally:
            # Capture execution data for learning
            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            
            try:
                await self.learning_engine.capture_execution(
                    session_id=self.result.session_id,
                    form_type=getattr(self.result.form_data, 'form_type', 'unknown') if self.result.form_data else 'unknown',
                    agency=self._extract_agency_from_url(page.url),
                    step_name=f"selector_{config.description.replace(' ', '_')}",
                    success=success,
                    duration_ms=duration_ms,
                    page_data=self.current_page_data,
                    selector_data={
                        "attempted_selectors": attempted_selectors,
                        "successful_selector": successful_selector,
                        "config_description": config.description,
                        "used_learned_selectors": enhanced_config != config  # Flag if learning was applied
                    },
                    error_info=error_info
                )
            except Exception as learning_error:
                logger.warning(f"Learning capture failed: {learning_error}")
    
    async def _capture_page_context(self, page):
        """Capture current page context for learning."""
        
        try:
            # Get page metadata
            self.current_page_data = {
                "url": page.url,
                "title": await page.title(),
                "viewport_size": await page.evaluate("() => [window.innerWidth, window.innerHeight]"),
                "user_agent": await page.evaluate("() => navigator.userAgent"),
                "timestamp": datetime.now().isoformat()
            }
            
            # Capture HTML content for analysis (truncated for storage)
            html_content = await page.content()
            self.current_page_data["html_content"] = html_content
            
            # Basic performance metrics
            performance = await page.evaluate("""
                () => {
                    const perf = performance.getEntriesByType('navigation')[0];
                    return {
                        page_load_time_ms: perf ? perf.loadEventEnd - perf.fetchStart : 0,
                        dom_content_loaded_ms: perf ? perf.domContentLoadedEventEnd - perf.fetchStart : 0
                    };
                }
            """)
            self.current_page_data.update(performance)
            
        except Exception as e:
            logger.debug(f"Failed to capture page context: {e}")
            self.current_page_data = {"url": page.url, "error": str(e)}
    
    def _extract_agency_from_url(self, url: str) -> str:
        """Extract agency name from URL."""
        if "rrc.texas.gov" in url:
            return "RRC"
        elif "epa.gov" in url:
            return "EPA"
        else:
            return "unknown"
    
    async def _enhance_config_with_learned_selectors(self, original_config: SelectorConfig, page_url: str) -> SelectorConfig:
        """
        🎯 THE KEY METHOD: Automatically merge learned selectors with original configuration.
        
        This is how improved selectors get used automatically in the next automation run!
        """
        
        # Create cache key for this selector
        cache_key = f"{original_config.description}_{self._extract_agency_from_url(page_url)}"
        
        # Check cache first
        if cache_key in self.learned_selectors_cache:
            logger.debug(f"Using cached enhanced config for '{original_config.description}'")
            return self.learned_selectors_cache[cache_key]
        
        try:
            # Get learned selectors from the learning engine
            learned_data = await self.learning_engine.get_learned_selectors_for_step(
                step_name=f"selector_{original_config.description.replace(' ', '_')}",
                agency=self._extract_agency_from_url(page_url)
            )
            
            if not learned_data:
                # No learned data available, use original config
                self.learned_selectors_cache[cache_key] = original_config
                return original_config
            
            # 🧠 MERGE LEARNED SELECTORS WITH ORIGINAL
            enhanced_selectors = []
            
            # 1. Add learned selectors FIRST (highest priority, highest confidence)
            learned_selectors = learned_data.get('suggested_selectors', [])
            confidence_scores = learned_data.get('confidence_scores', [])
            
            for i, selector in enumerate(learned_selectors):
                confidence = confidence_scores[i] if i < len(confidence_scores) else 0.8
                enhanced_selectors.append({
                    'selector': selector,
                    'confidence': confidence,
                    'source': 'learned'
                })
            
            # 2. Add original selectors as fallbacks (lower priority)
            original_selectors = [original_config.primary] + (original_config.fallbacks or [])
            for i, selector in enumerate(original_selectors):
                if selector:  # Skip None values
                    # Reduce confidence of original selectors if learned ones exist
                    base_confidence = 0.7 - (i * 0.05)  # Primary: 0.7, fallbacks: 0.65, 0.6, etc.
                    if learned_selectors:  # If we have learned selectors, reduce original confidence more
                        base_confidence = max(0.3, base_confidence - 0.2)
                    
                    enhanced_selectors.append({
                        'selector': selector,
                        'confidence': base_confidence,
                        'source': 'original'
                    })
            
            # 3. Sort by confidence (highest first) - THIS IS KEY!
            enhanced_selectors.sort(key=lambda x: x['confidence'], reverse=True)
            
            # 4. Create enhanced SelectorConfig
            if enhanced_selectors:
                primary_selector = enhanced_selectors[0]['selector']
                fallback_selectors = [item['selector'] for item in enhanced_selectors[1:]]
                
                enhanced_config = SelectorConfig(
                    primary=primary_selector,
                    fallbacks=fallback_selectors,
                    description=f"{original_config.description} (learning-enhanced)",
                    wait_timeout=original_config.wait_timeout,
                    wait_strategy=original_config.wait_strategy
                )
                
                logger.info(f"🧠 Enhanced '{original_config.description}' with {len(learned_selectors)} learned selectors")
                logger.debug(f"   Primary: {primary_selector[:50]}... (confidence: {enhanced_selectors[0]['confidence']:.2f})")
                
            else:
                enhanced_config = original_config
            
            # Cache the enhanced config
            self.learned_selectors_cache[cache_key] = enhanced_config
            return enhanced_config
            
        except Exception as e:
            logger.warning(f"Failed to enhance config with learned selectors: {e}")
            # Fall back to original config
            self.learned_selectors_cache[cache_key] = original_config
            return original_config
    
    async def _update_selector_confidence(self, selector: str, success: bool):
        """
        Update confidence scores for selectors based on success/failure.
        
        This creates a feedback loop that improves selector prioritization over time.
        """
        
        try:
            if success:
                # Boost confidence for successful selectors
                await self.learning_engine.update_selector_confidence(selector, boost=0.05)
                logger.debug(f"🎯 Boosted confidence for successful selector: {selector[:50]}...")
            else:
                # Reduce confidence for failed selectors  
                await self.learning_engine.update_selector_confidence(selector, boost=-0.1)
                logger.debug(f"📉 Reduced confidence for failed selector: {selector[:50]}...")
                
        except Exception as e:
            logger.debug(f"Failed to update selector confidence: {e}")
    
    def get_learning_stats(self) -> Dict[str, Any]:
        """Get statistics about learning integration."""
        
        return {
            "enhanced_configs_cached": len(self.learned_selectors_cache),
            "learning_engine_status": "active" if self.learning_engine else "inactive",
            "total_page_captures": len(getattr(self, 'page_capture_history', [])),
            "cached_configs": list(self.learned_selectors_cache.keys())
        }


class LearningIntegratedFormAutomator(BaseFormAutomator):
    """
    Enhanced form automator that captures learning data automatically.
    
    This extends the base form automator to integrate with the learning system
    without changing the core automation logic.
    """
    
    def __init__(self, context, session_id: str, learning_engine: AutomationLearningEngine = None):
        super().__init__(context, session_id)
        
        # Replace selector engine with learning-integrated version
        self.learning_engine = learning_engine or AutomationLearningEngine()
        self.selector_engine = LearningIntegratedSelectorEngine(self.result, self.learning_engine)
    
    async def execute_automation(self, form_data, auth_data, multi_tab: bool = False):
        """Override to capture high-level automation learning data."""
        
        start_time = datetime.now()
        
        try:
            # Execute parent automation
            result = await super().execute_automation(form_data, auth_data, multi_tab)
            
            # Capture successful automation
            await self._capture_automation_success(result, start_time)
            
            return result
            
        except Exception as e:
            # Capture failed automation
            await self._capture_automation_failure(e, start_time, form_data)
            raise
    
    async def _capture_automation_success(self, result: AutomationResult, start_time: datetime):
        """Capture successful automation for learning."""
        
        try:
            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            
            # Get current page data if available
            page_data = {}
            if self.context.pages:
                page = self.context.pages[0]
                page_data = {
                    "url": page.url,
                    "title": await page.title(),
                    "html_content": await page.content()
                }
            
            await self.learning_engine.capture_execution(
                session_id=result.session_id,
                form_type=result.form_data.form_type if result.form_data else 'unknown',
                agency=self.agency_config.get("code", "unknown"),
                step_name="complete_automation",
                success=True,
                duration_ms=duration_ms,
                page_data=page_data
            )
            
        except Exception as e:
            logger.warning(f"Failed to capture automation success: {e}")
    
    async def _capture_automation_failure(self, error: Exception, start_time: datetime, form_data):
        """Capture failed automation for learning."""
        
        try:
            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            
            # Get current page data if available
            page_data = {}
            if self.context.pages:
                page = self.context.pages[0]
                try:
                    page_data = {
                        "url": page.url,
                        "title": await page.title(),
                        "html_content": await page.content()
                    }
                except:
                    page_data = {"url": page.url, "error": "Could not capture page data"}
            
            await self.learning_engine.capture_execution(
                session_id=self.session_id,
                form_type=form_data.form_type if form_data else 'unknown',
                agency=self.agency_config.get("code", "unknown"),
                step_name="complete_automation",
                success=False,
                duration_ms=duration_ms,
                page_data=page_data,
                error_info={
                    "message": str(error),
                    "type": type(error).__name__
                }
            )
            
        except Exception as e:
            logger.warning(f"Failed to capture automation failure: {e}")


class LearningIntegratedGISExtractor(BaseGISExtractor):
    """
    Enhanced GIS extractor that captures learning data for location extraction.
    """
    
    def __init__(self, result: AutomationResult, learning_engine: AutomationLearningEngine = None):
        super().__init__(result)
        self.learning_engine = learning_engine or AutomationLearningEngine()
        
        # Replace selector engine with learning version
        self.selector_engine = LearningIntegratedSelectorEngine(result, learning_engine)
    
    async def extract_gis_data(self, page, identifier: str, extraction_types: list = None):
        """Override to capture GIS extraction learning data."""
        
        start_time = datetime.now()
        
        try:
            # Execute parent GIS extraction
            gis_data = await super().extract_gis_data(page, identifier, extraction_types)
            
            # Capture GIS extraction success/failure
            success = gis_data.confidence_score > 0.5  # Consider >50% confidence as success
            
            await self._capture_gis_extraction(
                page, identifier, gis_data, success, start_time
            )
            
            return gis_data
            
        except Exception as e:
            # Capture GIS extraction failure
            await self._capture_gis_extraction(
                page, identifier, None, False, start_time, error=e
            )
            raise
    
    async def _capture_gis_extraction(
        self, 
        page, 
        identifier: str, 
        gis_data, 
        success: bool, 
        start_time: datetime,
        error: Exception = None
    ):
        """Capture GIS extraction data for learning."""
        
        try:
            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            
            # Capture page data
            page_data = {
                "url": page.url,
                "html_content": await page.content(),
                "identifier_searched": identifier
            }
            
            # Add GIS-specific context
            if gis_data:
                page_data["extracted_data"] = {
                    "distance": gis_data.distance,
                    "direction": gis_data.direction,
                    "town": gis_data.town,
                    "confidence_score": gis_data.confidence_score,
                    "extraction_method": gis_data.extraction_method
                }
            
            error_info = None
            if error:
                error_info = {
                    "message": str(error),
                    "type": type(error).__name__
                }
            
            await self.learning_engine.capture_execution(
                session_id=self.result.session_id,
                form_type="GIS_EXTRACTION",
                agency="RRC",  # Assuming RRC GIS for now
                step_name=f"gis_extract_{identifier}",
                success=success,
                duration_ms=duration_ms,
                page_data=page_data,
                error_info=error_info
            )
            
        except Exception as e:
            logger.warning(f"Failed to capture GIS extraction learning data: {e}")


# Decorator for automatic learning integration
def capture_learning_data(learning_engine: AutomationLearningEngine = None):
    """
    Decorator to automatically capture learning data from any automation function.
    
    Usage:
    @capture_learning_data()
    async def my_automation_step(page, config):
        # ... automation logic ...
        return result
    """
    
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            engine = learning_engine or AutomationLearningEngine()
            start_time = datetime.now()
            
            try:
                result = await func(*args, **kwargs)
                
                # Capture success
                # This is a simplified version - real implementation would need more context
                logger.debug(f"Learning capture: {func.__name__} succeeded")
                
                return result
                
            except Exception as e:
                # Capture failure
                logger.debug(f"Learning capture: {func.__name__} failed - {str(e)}")
                raise
        
        return wrapper
    return decorator


# Factory function to create learning-integrated automation components
def create_learning_integrated_automator(automator_class, context, session_id: str):
    """
    Factory function to create learning-integrated version of any automator.
    
    This automatically wraps the automator with learning capabilities.
    """
    
    class LearningWrapper(automator_class):
        def __init__(self, context, session_id: str):
            super().__init__(context, session_id)
            
            # Replace components with learning versions
            self.learning_engine = AutomationLearningEngine()
            self.selector_engine = LearningIntegratedSelectorEngine(self.result, self.learning_engine)
        
        async def execute_automation(self, form_data, auth_data, multi_tab: bool = False):
            """Add learning capture to any automator."""
            start_time = datetime.now()
            
            try:
                result = await super().execute_automation(form_data, auth_data, multi_tab)
                
                # Capture success
                await self._capture_learning_success(result, start_time)
                return result
                
            except Exception as e:
                # Capture failure
                await self._capture_learning_failure(e, start_time, form_data)
                raise
        
        async def _capture_learning_success(self, result, start_time):
            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            logger.info(f"Learning: {self.__class__.__name__} succeeded in {duration_ms}ms")
        
        async def _capture_learning_failure(self, error, start_time, form_data):
            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            logger.error(f"Learning: {self.__class__.__name__} failed in {duration_ms}ms - {str(error)}")
    
    return LearningWrapper(context, session_id)


# Convenience function for RRC specifically
def create_learning_rrc_automator(context, session_id: str):
    """Create learning-integrated RRC automator."""
    from ..agencies.rrc.rrc_form_automator import RRCFormAutomator
    return create_learning_integrated_automator(RRCFormAutomator, context, session_id)
