"""
Automation Learning Engine - Learns from successes and failures to improve over time.

This system captures execution data, analyzes patterns, and uses LLM intelligence
to understand why automations succeed or fail, then updates strategies accordingly.
"""

import asyncio
import json
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from pathlib import Path
import logging

from ..base.data_models import AutomationResult, SelectorConfig, GISData
from ..exceptions import AutomationError


logger = logging.getLogger(__name__)


@dataclass
class ExecutionCapture:
    """Captures detailed execution data for learning."""
    
    # Execution metadata
    session_id: str
    timestamp: datetime
    form_type: str
    agency: str
    step_name: str
    
    # Success/failure data
    success: bool
    duration_ms: int
    error_message: Optional[str] = None
    error_type: Optional[str] = None
    
    # HTML and selector data
    html_snapshot: str = ""
    html_hash: str = ""
    attempted_selectors: List[str] = None
    successful_selector: Optional[str] = None
    
    # Context data
    url: str = ""
    viewport_size: Tuple[int, int] = (1920, 1080)
    user_agent: str = ""
    
    # Performance data
    network_requests: int = 0
    page_load_time_ms: int = 0
    
    def __post_init__(self):
        if self.attempted_selectors is None:
            self.attempted_selectors = []
        
        # Generate HTML hash for comparison
        if self.html_snapshot:
            self.html_hash = hashlib.md5(self.html_snapshot.encode()).hexdigest()


@dataclass
class SelectorPerformance:
    """Tracks selector performance over time."""
    
    selector: str
    total_attempts: int = 0
    successful_attempts: int = 0
    avg_response_time_ms: float = 0.0
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    confidence_score: float = 0.5
    
    # Context tracking
    successful_contexts: List[str] = None  # HTML hashes where it worked
    failed_contexts: List[str] = None      # HTML hashes where it failed
    
    def __post_init__(self):
        if self.successful_contexts is None:
            self.successful_contexts = []
        if self.failed_contexts is None:
            self.failed_contexts = []
    
    @property
    def success_rate(self) -> float:
        return self.successful_attempts / self.total_attempts if self.total_attempts > 0 else 0.0
    
    def update_success(self, response_time_ms: float, html_hash: str):
        """Record a successful selector usage."""
        self.total_attempts += 1
        self.successful_attempts += 1
        self.last_success = datetime.now()
        
        # Update average response time
        if self.avg_response_time_ms == 0:
            self.avg_response_time_ms = response_time_ms
        else:
            self.avg_response_time_ms = (self.avg_response_time_ms * 0.7 + response_time_ms * 0.3)
        
        # Update confidence score
        self.confidence_score = min(0.95, self.success_rate * 0.8 + 0.2)
        
        # Track successful context
        if html_hash and html_hash not in self.successful_contexts:
            self.successful_contexts.append(html_hash)
    
    def update_failure(self, html_hash: str):
        """Record a failed selector usage."""
        self.total_attempts += 1
        self.last_failure = datetime.now()
        
        # Decrease confidence score
        self.confidence_score = max(0.05, self.confidence_score * 0.9)
        
        # Track failed context
        if html_hash and html_hash not in self.failed_contexts:
            self.failed_contexts.append(html_hash)


class AutomationLearningEngine:
    """
    Core learning engine that captures execution data, analyzes patterns,
    and uses LLM intelligence to improve automation strategies.
    """
    
    def __init__(self, storage_path: str = "automation_intelligence"):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(exist_ok=True)
        
        # Execution data storage
        self.executions_path = self.storage_path / "executions"
        self.executions_path.mkdir(exist_ok=True)
        
        # HTML snapshots storage
        self.html_snapshots_path = self.storage_path / "html_snapshots"
        self.html_snapshots_path.mkdir(exist_ok=True)
        
        # Selector performance tracking
        self.selector_performance: Dict[str, SelectorPerformance] = {}
        self.performance_file = self.storage_path / "selector_performance.json"
        
        # Load existing performance data
        self._load_selector_performance()
        
        # LLM client for analysis (will be initialized when needed)
        self.llm_client = None
    
    def _load_selector_performance(self):
        """Load existing selector performance data."""
        try:
            if self.performance_file.exists():
                with open(self.performance_file, 'r') as f:
                    data = json.load(f)
                    
                for selector, perf_data in data.items():
                    # Convert datetime strings back to datetime objects
                    if perf_data.get('last_success'):
                        perf_data['last_success'] = datetime.fromisoformat(perf_data['last_success'])
                    if perf_data.get('last_failure'):
                        perf_data['last_failure'] = datetime.fromisoformat(perf_data['last_failure'])
                    
                    self.selector_performance[selector] = SelectorPerformance(**perf_data)
                    
                logger.info(f"Loaded performance data for {len(self.selector_performance)} selectors")
                
        except Exception as e:
            logger.warning(f"Failed to load selector performance: {e}")
    
    def _save_selector_performance(self):
        """Save selector performance data."""
        try:
            # Convert to JSON-serializable format
            data = {}
            for selector, performance in self.selector_performance.items():
                perf_dict = asdict(performance)
                
                # Convert datetime objects to ISO strings
                if perf_dict.get('last_success'):
                    perf_dict['last_success'] = perf_dict['last_success'].isoformat()
                if perf_dict.get('last_failure'):
                    perf_dict['last_failure'] = perf_dict['last_failure'].isoformat()
                
                data[selector] = perf_dict
            
            with open(self.performance_file, 'w') as f:
                json.dump(data, f, indent=2)
                
        except Exception as e:
            logger.error(f"Failed to save selector performance: {e}")
    
    async def capture_execution(
        self,
        session_id: str,
        form_type: str,
        agency: str,
        step_name: str,
        success: bool,
        duration_ms: int,
        page_data: Dict[str, Any],
        selector_data: Optional[Dict[str, Any]] = None,
        error_info: Optional[Dict[str, Any]] = None
    ) -> ExecutionCapture:
        """
        Capture detailed execution data for learning.
        
        This is called after every significant automation step.
        """
        
        try:
            # Create execution capture
            capture = ExecutionCapture(
                session_id=session_id,
                timestamp=datetime.now(),
                form_type=form_type,
                agency=agency,
                step_name=step_name,
                success=success,
                duration_ms=duration_ms,
                url=page_data.get('url', ''),
                viewport_size=page_data.get('viewport_size', (1920, 1080)),
                user_agent=page_data.get('user_agent', ''),
                network_requests=page_data.get('network_requests', 0),
                page_load_time_ms=page_data.get('page_load_time_ms', 0)
            )
            
            # Capture HTML snapshot
            if 'html_content' in page_data:
                capture.html_snapshot = page_data['html_content']
                
                # Save HTML to file for detailed analysis
                html_file = self.html_snapshots_path / f"{capture.html_hash}_{step_name}.html"
                if not html_file.exists():  # Don't duplicate identical HTML
                    with open(html_file, 'w', encoding='utf-8') as f:
                        f.write(capture.html_snapshot)
            
            # Capture selector data
            if selector_data:
                capture.attempted_selectors = selector_data.get('attempted_selectors', [])
                capture.successful_selector = selector_data.get('successful_selector')
                
                # Update selector performance tracking
                self._update_selector_performance(
                    capture.attempted_selectors,
                    capture.successful_selector,
                    success,
                    duration_ms,
                    capture.html_hash
                )
            
            # Capture error information
            if error_info:
                capture.error_message = error_info.get('message', '')
                capture.error_type = error_info.get('type', '')
            
            # Save execution data
            execution_file = self.executions_path / f"{session_id}_{step_name}_{int(datetime.now().timestamp())}.json"
            with open(execution_file, 'w') as f:
                # Convert to dict and handle datetime serialization
                capture_dict = asdict(capture)
                capture_dict['timestamp'] = capture.timestamp.isoformat()
                json.dump(capture_dict, f, indent=2)
            
            logger.info(f"Captured execution data for {agency} {form_type} - {step_name}: {'SUCCESS' if success else 'FAILURE'}")
            
            # Trigger learning analysis if this was a failure
            if not success and capture.html_snapshot:
                await self._analyze_failure(capture)
            
            return capture
            
        except Exception as e:
            logger.error(f"Failed to capture execution data: {e}")
            raise
    
    def _update_selector_performance(
        self,
        attempted_selectors: List[str],
        successful_selector: Optional[str],
        success: bool,
        duration_ms: int,
        html_hash: str
    ):
        """Update selector performance metrics."""
        
        for selector in attempted_selectors:
            if selector not in self.selector_performance:
                self.selector_performance[selector] = SelectorPerformance(selector=selector)
            
            perf = self.selector_performance[selector]
            
            if success and selector == successful_selector:
                perf.update_success(duration_ms, html_hash)
            else:
                perf.update_failure(html_hash)
        
        # Save updated performance data
        self._save_selector_performance()
    
    async def _analyze_failure(self, failure_capture: ExecutionCapture):
        """
        Analyze a failure using LLM intelligence to understand why it happened.
        
        This is where we compare failed HTML against successful HTML to find patterns.
        """
        
        try:
            # Find similar successful executions for comparison
            similar_successes = await self._find_similar_executions(
                failure_capture.form_type,
                failure_capture.agency,
                failure_capture.step_name,
                success_only=True,
                limit=3
            )
            
            if not similar_successes:
                logger.warning(f"No similar successful executions found for comparison")
                return
            
            # Analyze the failure using LLM
            analysis = await self._llm_analyze_failure(failure_capture, similar_successes)
            
            if analysis:
                # Store analysis results
                analysis_file = self.storage_path / f"failure_analysis_{failure_capture.session_id}_{failure_capture.step_name}.json"
                with open(analysis_file, 'w') as f:
                    json.dump(analysis, f, indent=2)
                
                # Apply learnings if LLM suggests improvements
                if analysis.get('suggested_selectors'):
                    await self._apply_selector_improvements(
                        failure_capture.agency,
                        failure_capture.form_type, 
                        failure_capture.step_name,
                        analysis['suggested_selectors']
                    )
            
        except Exception as e:
            logger.error(f"Failure analysis failed: {e}")
    
    async def _find_similar_executions(
        self,
        form_type: str,
        agency: str,
        step_name: str,
        success_only: bool = False,
        limit: int = 5,
        days_back: int = 30
    ) -> List[ExecutionCapture]:
        """Find similar executions for comparison."""
        
        similar_executions = []
        cutoff_date = datetime.now() - timedelta(days=days_back)
        
        try:
            # Search through execution files
            for execution_file in self.executions_path.glob("*.json"):
                try:
                    with open(execution_file, 'r') as f:
                        data = json.load(f)
                    
                    # Check if this execution matches criteria
                    if (data['form_type'] == form_type and 
                        data['agency'] == agency and
                        data['step_name'] == step_name and
                        datetime.fromisoformat(data['timestamp']) > cutoff_date):
                        
                        if success_only and not data['success']:
                            continue
                        
                        # Convert back to ExecutionCapture object
                        data['timestamp'] = datetime.fromisoformat(data['timestamp'])
                        execution = ExecutionCapture(**data)
                        similar_executions.append(execution)
                        
                        if len(similar_executions) >= limit:
                            break
                            
                except Exception as e:
                    logger.debug(f"Error processing execution file {execution_file}: {e}")
                    continue
            
            # Sort by timestamp (most recent first)
            similar_executions.sort(key=lambda x: x.timestamp, reverse=True)
            
            return similar_executions[:limit]
            
        except Exception as e:
            logger.error(f"Error finding similar executions: {e}")
            return []
    
    async def _llm_analyze_failure(
        self,
        failure: ExecutionCapture,
        similar_successes: List[ExecutionCapture]
    ) -> Optional[Dict[str, Any]]:
        """
        Use LLM to analyze failure against successful executions.
        
        This is where the magic happens - AI understands why automation failed.
        """
        
        try:
            if not self.llm_client:
                # Initialize LLM client (OpenAI/Claude)
                # This would be configured based on your preference
                from openai import AsyncOpenAI
                self.llm_client = AsyncOpenAI()  # Requires API key in environment
            
            # Prepare analysis prompt
            analysis_prompt = self._build_failure_analysis_prompt(failure, similar_successes)
            
            # Call LLM for analysis
            response = await self.llm_client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are an expert web automation analyst. Analyze HTML differences to understand why selectors fail and suggest improvements."},
                    {"role": "user", "content": analysis_prompt}
                ],
                response_format={"type": "json_object"}
            )
            
            analysis = json.loads(response.choices[0].message.content)
            
            logger.info(f"LLM analysis completed for {failure.step_name}: {analysis.get('root_cause', 'Unknown')}")
            
            return analysis
            
        except Exception as e:
            logger.error(f"LLM failure analysis failed: {e}")
            return None
    
    def _build_failure_analysis_prompt(
        self,
        failure: ExecutionCapture,
        successes: List[ExecutionCapture]
    ) -> str:
        """Build comprehensive prompt for LLM failure analysis."""
        
        prompt = f"""
AUTOMATION FAILURE ANALYSIS

FAILED EXECUTION:
- Step: {failure.step_name}
- Agency: {failure.agency}
- Form: {failure.form_type}
- Error: {failure.error_message}
- Attempted Selectors: {failure.attempted_selectors}
- Duration: {failure.duration_ms}ms
- URL: {failure.url}

FAILED HTML SNIPPET (first 2000 chars):
{failure.html_snapshot[:2000]}

SIMILAR SUCCESSFUL EXECUTIONS:
"""
        
        for i, success in enumerate(successes):
            prompt += f"""
SUCCESS #{i+1}:
- Successful Selector: {success.successful_selector}
- Duration: {success.duration_ms}ms
- HTML Hash: {success.html_hash}
- HTML Snippet (first 1000 chars):
{success.html_snapshot[:1000]}
"""
        
        prompt += """

ANALYSIS REQUIRED:
Please analyze the failed execution against the successful ones and provide:

1. ROOT_CAUSE: Why did the selectors fail? (e.g., HTML structure changed, timing issue, new elements)
2. HTML_DIFFERENCES: Key differences between failed and successful HTML
3. SUGGESTED_SELECTORS: New selectors that would work with the failed HTML
4. CONFIDENCE_LEVEL: How confident are you in the analysis (0.0-1.0)
5. PATTERN_CHANGES: Any patterns indicating website structure changes

Respond in JSON format:
{
  "root_cause": "explanation",
  "html_differences": ["difference1", "difference2"],
  "suggested_selectors": ["selector1", "selector2"],
  "confidence_level": 0.85,
  "pattern_changes": "description of changes",
  "recommended_actions": ["action1", "action2"]
}
"""
        
        return prompt
    
    async def _apply_selector_improvements(
        self,
        agency: str,
        form_type: str,
        step_name: str,
        suggested_selectors: List[str]
    ):
        """Apply LLM-suggested selector improvements to configuration."""
        
        try:
            # This would update the actual configuration files
            # For now, log the suggestions for manual review
            
            logger.info(f"LLM suggests new selectors for {agency} {form_type} {step_name}:")
            for i, selector in enumerate(suggested_selectors):
                logger.info(f"  Suggestion {i+1}: {selector}")
            
            # In a full implementation, this would:
            # 1. Update the agency configuration files
            # 2. Test the new selectors
            # 3. Update selector performance tracking
            # 4. Notify administrators of changes
            
        except Exception as e:
            logger.error(f"Failed to apply selector improvements: {e}")
    
    def get_selector_insights(self, agency: str = None, form_type: str = None) -> Dict[str, Any]:
        """Get insights about selector performance."""
        
        insights = {
            "total_selectors": len(self.selector_performance),
            "high_confidence_selectors": [],
            "low_confidence_selectors": [],
            "recently_failed_selectors": [],
            "performance_summary": {}
        }
        
        cutoff_date = datetime.now() - timedelta(days=7)
        
        for selector, perf in self.selector_performance.items():
            # High confidence selectors (>80% success rate)
            if perf.confidence_score > 0.8:
                insights["high_confidence_selectors"].append({
                    "selector": selector,
                    "confidence": perf.confidence_score,
                    "success_rate": perf.success_rate
                })
            
            # Low confidence selectors (<30% success rate)
            if perf.confidence_score < 0.3:
                insights["low_confidence_selectors"].append({
                    "selector": selector,
                    "confidence": perf.confidence_score,
                    "success_rate": perf.success_rate
                })
            
            # Recently failed selectors
            if perf.last_failure and perf.last_failure > cutoff_date:
                insights["recently_failed_selectors"].append({
                    "selector": selector,
                    "last_failure": perf.last_failure.isoformat(),
                    "confidence": perf.confidence_score
                })
        
        # Performance summary
        if self.selector_performance:
            confidences = [p.confidence_score for p in self.selector_performance.values()]
            insights["performance_summary"] = {
                "avg_confidence": sum(confidences) / len(confidences),
                "selectors_above_80_percent": len([c for c in confidences if c > 0.8]),
                "selectors_below_30_percent": len([c for c in confidences if c < 0.3])
            }
        
        return insights
    
    async def generate_learning_report(self) -> Dict[str, Any]:
        """Generate comprehensive learning report."""
        
        report = {
            "generated_at": datetime.now().isoformat(),
            "summary": {
                "total_executions": len(list(self.executions_path.glob("*.json"))),
                "unique_html_snapshots": len(list(self.html_snapshots_path.glob("*.html"))),
                "tracked_selectors": len(self.selector_performance)
            },
            "selector_insights": self.get_selector_insights(),
            "recent_failures": [],
            "learning_trends": {}
        }
        
        # Add recent failure analysis
        cutoff_date = datetime.now() - timedelta(days=7)
        recent_failures = await self._find_similar_executions(
            form_type="",  # Any form type
            agency="",     # Any agency  
            step_name="",  # Any step
            success_only=False,
            limit=10
        )
        
        failed_executions = [f for f in recent_failures if not f.success and f.timestamp > cutoff_date]
        
        for failure in failed_executions:
            report["recent_failures"].append({
                "session_id": failure.session_id,
                "step_name": failure.step_name,
                "error": failure.error_message,
                "timestamp": failure.timestamp.isoformat()
            })
        
        return report
    
    async def get_learned_selectors_for_step(self, step_name: str, agency: str) -> Optional[Dict[str, Any]]:
        """Get learned selectors for a specific automation step."""
        
        try:
            # Look for learned selector data in analysis files
            analysis_files = list(self.intelligence_dir.glob(f"failure_analysis_*_{agency}_{step_name}.json"))
            
            if not analysis_files:
                # Try without agency filter
                analysis_files = list(self.intelligence_dir.glob(f"failure_analysis_*{step_name}*.json"))
            
            if not analysis_files:
                return None
            
            # Get the most recent analysis
            latest_analysis = max(analysis_files, key=lambda x: x.stat().st_mtime)
            
            with open(latest_analysis, 'r') as f:
                analysis_data = json.load(f)
            
            return {
                "suggested_selectors": analysis_data.get("suggested_selectors", []),
                "confidence_scores": analysis_data.get("confidence_scores", []),
                "analysis_timestamp": analysis_data.get("analysis_timestamp"),
                "learned_from": analysis_data.get("session_id")
            }
            
        except Exception as e:
            logger.debug(f"No learned selectors found for {step_name}: {e}")
            return None

    async def update_selector_confidence(self, selector: str, boost: float):
        """Update confidence score for a selector based on success/failure."""
        
        try:
            # Update in-memory performance tracking
            if selector in self.selector_performance:
                current_confidence = self.selector_performance[selector].confidence_score
                new_confidence = max(0.1, min(0.99, current_confidence + boost))
                
                # Update the performance entry
                self.selector_performance[selector].confidence_score = new_confidence
                self.selector_performance[selector].last_updated = datetime.now()
                
                if boost > 0:
                    self.selector_performance[selector].successful_attempts += 1
                    self.selector_performance[selector].last_success = datetime.now()
                else:
                    self.selector_performance[selector].last_failure = datetime.now()
                
                # Update total attempts
                self.selector_performance[selector].total_attempts += 1
                
                # Recalculate success rate  
                total = self.selector_performance[selector].total_attempts
                successful = self.selector_performance[selector].successful_attempts
                self.selector_performance[selector].success_rate = successful / max(1, total)
                
                logger.debug(f"Updated selector '{selector[:50]}...' confidence: {current_confidence:.2f} -> {new_confidence:.2f}")
            else:
                # Create new entry
                confidence = max(0.1, min(0.99, 0.5 + boost))
                self.selector_performance[selector] = SelectorPerformance(
                    selector=selector,
                    confidence_score=confidence,
                    total_attempts=1,
                    successful_attempts=1 if boost > 0 else 0,
                    success_rate=1.0 if boost > 0 else 0.0,
                    last_success=datetime.now() if boost > 0 else None,
                    last_failure=datetime.now() if boost < 0 else None
                )
                
                logger.debug(f"Created new selector '{selector[:50]}...' with confidence: {confidence:.2f}")
                
        except Exception as e:
            logger.error(f"Failed to update selector confidence: {e}")
