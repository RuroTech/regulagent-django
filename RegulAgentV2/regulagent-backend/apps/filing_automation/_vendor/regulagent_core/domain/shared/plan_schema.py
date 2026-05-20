"""
Universal plan schema that works across all industries.

This provides the canonical Plan JSON structure that policy packs use
to generate regulatory compliance plans, whether for oil & gas plugging,
construction permits, environmental remediation, etc.
"""

from pydantic import BaseModel, Field
from datetime import datetime
from typing import List, Dict, Any, Optional, Literal
from .common_types import PriorityLevel, ConfidenceLevel


class PlanRow(BaseModel):
    """Individual step in a regulatory compliance plan."""
    
    # Universal step identification
    step_id: str = Field(..., description="Unique identifier for this step")
    step_type: str = Field(..., description="Type of step (industry-specific)")
    sequence: int = Field(..., description="Order of execution")
    
    # Universal step details
    title: str = Field(..., description="Human-readable step title")
    description: str = Field(..., description="Detailed step description")
    special_instructions: Optional[str] = Field(None, description="Special instructions for this step")
    
    # Universal requirements
    required: bool = Field(default=True, description="Whether this step is mandatory")
    priority: PriorityLevel = Field(default=PriorityLevel.NORMAL, description="Step priority")
    
    # Universal timing
    estimated_duration_hours: Optional[float] = Field(None, description="Estimated time to complete")
    wait_time_hours: Optional[float] = Field(None, description="Required wait time after completion")
    
    # Universal cost
    estimated_cost: Optional[float] = Field(None, description="Estimated cost in USD")
    cost_breakdown: Dict[str, float] = Field(default_factory=dict, description="Detailed cost breakdown")
    
    # Universal compliance
    regulatory_basis: List[str] = Field(default_factory=list, description="Regulations that require this step")
    compliance_notes: List[str] = Field(default_factory=list, description="Compliance-related notes")
    
    # Universal quality control
    verification_required: bool = Field(default=False, description="Whether verification is required")
    inspection_required: bool = Field(default=False, description="Whether inspection is required")
    documentation_required: List[str] = Field(default_factory=list, description="Required documentation")
    
    # Universal metadata
    origin: Literal["rule", "heuristic", "user_edit", "inferred", "ai_generated"] = Field(
        default="rule", description="How this step was generated"
    )
    confidence: ConfidenceLevel = Field(default=ConfidenceLevel.HIGH, description="Confidence in this step")
    
    # Industry-specific data (extensible)
    industry_data: Dict[str, Any] = Field(default_factory=dict, description="Industry-specific step data")
    
    class Config:
        validate_assignment = True


class PlanResult(BaseModel):
    """Complete regulatory compliance plan result."""
    
    # Universal plan identification
    plan_id: str = Field(..., description="Unique plan identifier")
    policy_id: str = Field(..., description="Policy pack that generated this plan")
    policy_version: str = Field(..., description="Version of policy pack used")
    
    # Universal plan metadata
    created_at: datetime = Field(default_factory=datetime.now, description="When plan was created")
    created_by: Optional[str] = Field(None, description="Who/what created the plan")
    
    # Universal plan content
    title: str = Field(..., description="Plan title")
    description: str = Field(..., description="Plan description")
    steps: List[PlanRow] = Field(..., description="Ordered list of plan steps")
    
    # Universal plan summary
    total_estimated_cost: Optional[float] = Field(None, description="Total estimated cost")
    total_estimated_duration_hours: Optional[float] = Field(None, description="Total estimated duration")
    required_permits: List[str] = Field(default_factory=list, description="Required permits/approvals")
    
    # Universal compliance
    regulatory_requirements: List[str] = Field(default_factory=list, description="Applicable regulations")
    compliance_notes: List[str] = Field(default_factory=list, description="General compliance notes")
    
    # Universal validation
    validation_errors: List[str] = Field(default_factory=list, description="Plan validation errors")
    validation_warnings: List[str] = Field(default_factory=list, description="Plan validation warnings")
    
    # Universal quality indicators
    overall_confidence: ConfidenceLevel = Field(default=ConfidenceLevel.MEDIUM, description="Overall plan confidence")
    completeness_score: float = Field(default=0.0, ge=0.0, le=1.0, description="Plan completeness (0-1)")
    
    # Industry-specific data (extensible)
    industry_data: Dict[str, Any] = Field(default_factory=dict, description="Industry-specific plan data")
    
    class Config:
        validate_assignment = True
    
    def __init__(self, **data):
        super().__init__(**data)
        self._recalculate_totals()
    
    def add_step(self, step: PlanRow) -> None:
        """Add a step to the plan."""
        self.steps.append(step)
        self._recalculate_totals()
    
    def remove_step(self, step_id: str) -> bool:
        """Remove a step from the plan by ID."""
        original_length = len(self.steps)
        self.steps = [step for step in self.steps if step.step_id != step_id]
        if len(self.steps) < original_length:
            self._recalculate_totals()
            return True
        return False
    
    def get_step(self, step_id: str) -> Optional[PlanRow]:
        """Get a step by ID."""
        return next((step for step in self.steps if step.step_id == step_id), None)
    
    def reorder_steps(self) -> None:
        """Reorder steps by sequence number."""
        self.steps.sort(key=lambda x: x.sequence)
    
    def _recalculate_totals(self) -> None:
        """Recalculate total cost and duration."""
        total_cost = 0.0
        total_duration = 0.0
        
        for step in self.steps:
            if step.estimated_cost:
                total_cost += step.estimated_cost
            if step.estimated_duration_hours:
                total_duration += step.estimated_duration_hours
            if step.wait_time_hours:
                total_duration += step.wait_time_hours
        
        self.total_estimated_cost = total_cost if total_cost > 0 else None
        self.total_estimated_duration_hours = total_duration if total_duration > 0 else None
    
    @property
    def is_valid(self) -> bool:
        """Check if plan has no validation errors."""
        return len(self.validation_errors) == 0
    
    @property
    def has_warnings(self) -> bool:
        """Check if plan has validation warnings."""
        return len(self.validation_warnings) > 0
    
    @property
    def required_steps_count(self) -> int:
        """Count of required steps."""
        return sum(1 for step in self.steps if step.required)
    
    @property
    def optional_steps_count(self) -> int:
        """Count of optional steps."""
        return sum(1 for step in self.steps if not step.required)


class PlanSchema(BaseModel):
    """Schema definition for plan validation and documentation."""
    
    schema_id: str = Field(..., description="Schema identifier")
    schema_version: str = Field(..., description="Schema version")
    industry: str = Field(..., description="Target industry")
    agency: str = Field(..., description="Target regulatory agency")
    
    # Schema validation rules
    required_step_types: List[str] = Field(default_factory=list, description="Required step types")
    allowed_step_types: List[str] = Field(default_factory=list, description="Allowed step types")
    step_sequence_rules: Dict[str, Any] = Field(default_factory=dict, description="Step sequencing rules")
    
    # Schema metadata
    description: str = Field(..., description="Schema description")
    created_at: datetime = Field(default_factory=datetime.now)
    
    def validate_plan(self, plan: PlanResult) -> List[str]:
        """Validate a plan against this schema."""
        errors = []
        
        # Check required step types
        plan_step_types = {step.step_type for step in plan.steps}
        for required_type in self.required_step_types:
            if required_type not in plan_step_types:
                errors.append(f"Missing required step type: {required_type}")
        
        # Check allowed step types
        if self.allowed_step_types:
            for step_type in plan_step_types:
                if step_type not in self.allowed_step_types:
                    errors.append(f"Invalid step type: {step_type}")
        
        return errors
