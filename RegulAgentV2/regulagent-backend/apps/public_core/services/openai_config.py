"""
Centralized OpenAI configuration for RegulAgent.

Best practices implemented (2025-11-02):
- Structured outputs (strict=True) for reliability
- Prompt caching for cost savings
- Latest models with function calling support
- Consistent temperature settings
- Proper error handling

All OpenAI integrations should import from this module for consistency.
"""

import os
import time
import threading
import logging
from typing import Optional
from openai import OpenAI

logger = logging.getLogger(__name__)

# =============================================================================
# QUOTA COOLDOWN
# =============================================================================

_QUOTA_CACHE_KEY = "openai_quota_exceeded"


class OpenAIQuotaExceededError(Exception):
    """Raised when OpenAI reports insufficient_quota. Signals a global cooldown."""
    pass


def set_quota_exceeded(ttl: int = 300) -> None:
    """Record a quota-exceeded cooldown in the Django cache.

    Stores the expiry epoch so is_quota_exceeded() can compute remaining seconds
    without relying on backend-specific TTL introspection.

    Args:
        ttl: Cooldown duration in seconds (default 300 = 5 minutes).
    """
    from django.core.cache import cache
    expiry_epoch = time.time() + ttl
    cache.set(_QUOTA_CACHE_KEY, expiry_epoch, ttl)
    logger.warning(
        "[OpenAI] Quota exceeded — cooldown set for %ds (until epoch %.0f)",
        ttl,
        expiry_epoch,
    )


def is_quota_exceeded() -> tuple[bool, int]:
    """Check whether the global OpenAI quota cooldown is currently active.

    Returns:
        (active, seconds_remaining) — (False, 0) when no cooldown is set or it
        has expired.
    """
    from django.core.cache import cache
    expiry_epoch = cache.get(_QUOTA_CACHE_KEY)
    if expiry_epoch is None:
        return False, 0
    remaining = expiry_epoch - time.time()
    if remaining <= 0:
        return False, 0
    return True, int(remaining)

# =============================================================================
# MODEL SELECTION
# =============================================================================

# Chat/Assistant Models (with function calling)
DEFAULT_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o")
DEFAULT_REASONING_MODEL = os.getenv("OPENAI_REASONING_MODEL", "o1")  # For complex compliance

# Document Processing Models
DEFAULT_EXTRACTION_MODEL = os.getenv("OPENAI_EXTRACTION_MODEL", "gpt-4o")
DEFAULT_CLASSIFIER_MODEL = os.getenv("OPENAI_CLASSIFIER_MODEL", "gpt-4o-mini")

# Batch Processing (50% cost savings)
DEFAULT_BATCH_MODEL = os.getenv("OPENAI_BATCH_MODEL", "gpt-4o")

# Embeddings
DEFAULT_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
DEFAULT_EMBEDDING_DIMENSIONS = int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", "3072"))

# =============================================================================
# TEMPERATURE SETTINGS
# =============================================================================

# Low temperature for factual, deterministic responses
TEMPERATURE_FACTUAL = 0.0  # Document extraction, compliance checks
TEMPERATURE_LOW = 0.1  # Chat responses, plan modifications
TEMPERATURE_BALANCED = 0.5  # General conversation
TEMPERATURE_CREATIVE = 0.8  # Suggestions, explanations

# =============================================================================
# MODEL PRICING (per 1M tokens, USD)
# =============================================================================

MODEL_PRICING = {
    "gpt-4o": {"prompt": 2.50, "completion": 10.00},
    "gpt-4o-2024-11-20": {"prompt": 2.50, "completion": 10.00},
    "gpt-4o-2024-08-06": {"prompt": 2.50, "completion": 10.00},
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.60},
    "gpt-4o-mini-2024-07-18": {"prompt": 0.15, "completion": 0.60},
    "o1": {"prompt": 15.00, "completion": 60.00},
    "o1-2024-12-17": {"prompt": 15.00, "completion": 60.00},
    "text-embedding-3-large": {"prompt": 0.13, "completion": 0.0},
    "text-embedding-3-small": {"prompt": 0.02, "completion": 0.0},
}


def calculate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate estimated cost in USD for an OpenAI API call."""
    pricing = MODEL_PRICING.get(model, MODEL_PRICING.get("gpt-4o", {"prompt": 2.50, "completion": 10.00}))
    return (prompt_tokens * pricing["prompt"] + completion_tokens * pricing["completion"]) / 1_000_000


# =============================================================================
# RATE LIMITING - Token Per Minute (TPM) aware throttling
# =============================================================================

class TokenRateLimiter:
    """
    Prevents hitting OpenAI's Token Per Minute (TPM) limit by tracking usage.
    
    GPT-4o limits:
    - 30,000 TPM (tokens per minute)
    - 500 RPM (requests per minute)
    
    When concurrent requests are made, TPM can be exhausted quickly.
    This throttler ensures we stay under the TPM limit.
    """
    
    def __init__(self, tokens_per_minute: int = 30000, window_seconds: int = 60):
        self.tokens_per_minute = tokens_per_minute
        self.window_seconds = window_seconds
        self.tokens_used = 0
        self.window_start = time.time()
        self.lock = threading.Lock()
    
    def add_tokens(self, tokens: int):
        """Record tokens used"""
        with self.lock:
            now = time.time()
            elapsed = now - self.window_start
            
            # Reset window if expired
            if elapsed >= self.window_seconds:
                self.tokens_used = 0
                self.window_start = now
            
            self.tokens_used += tokens
    
    def should_throttle(self, estimated_tokens: int) -> tuple[bool, float]:
        """
        Check if a request with estimated_tokens would exceed TPM limit.
        
        Returns:
            (should_throttle: bool, wait_seconds: float)
        """
        with self.lock:
            now = time.time()
            elapsed = now - self.window_start
            
            # Reset window if expired
            if elapsed >= self.window_seconds:
                self.tokens_used = 0
                self.window_start = now
                return False, 0.0
            
            # Check if adding new tokens would exceed limit
            if self.tokens_used + estimated_tokens > self.tokens_per_minute:
                # Calculate how long to wait
                tokens_over = self.tokens_used + estimated_tokens - self.tokens_per_minute
                wait_time = (tokens_over / self.tokens_per_minute) * self.window_seconds
                return True, wait_time
            
            return False, 0.0


# Global rate limiter instance
_token_limiter = TokenRateLimiter(
    tokens_per_minute=int(os.getenv("OPENAI_TPM_LIMIT", "30000")),
    window_seconds=60
)


# =============================================================================
# TRACKED OPENAI CLIENT WRAPPER
# =============================================================================

class _TrackedCompletions:
    """Proxy for chat.completions that records usage after each create() call."""

    def __init__(self, completions, operation: str):
        self._completions = completions
        self._operation = operation

    def create(self, **kwargs):
        response = self._completions.create(**kwargs)
        self._record_usage(response, kwargs.get("model", "unknown"))
        return response

    def _record_usage(self, response, requested_model: str):
        try:
            usage = getattr(response, 'usage', None)
            if not usage:
                return

            model = getattr(response, 'model', requested_model) or requested_model
            prompt_tokens = getattr(usage, 'prompt_tokens', 0) or 0
            completion_tokens = getattr(usage, 'completion_tokens', 0) or 0
            total_tokens = getattr(usage, 'total_tokens', 0) or 0

            # Update rate limiter
            _token_limiter.add_tokens(total_tokens)

            # Record to database if tenant context is available
            from apps.tenants.context import get_current_tenant
            tenant = get_current_tenant()
            if tenant:
                from apps.tenants.services.usage_tracker import track_usage
                from apps.tenants.models import UsageRecord
                cost_usd = calculate_cost_usd(model, prompt_tokens, completion_tokens)
                track_usage(
                    tenant=tenant,
                    event_type=UsageRecord.EVENT_API_CALL,
                    resource_type='openai',
                    tokens_used=total_tokens,
                    metadata={
                        'model': model,
                        'prompt_tokens': prompt_tokens,
                        'completion_tokens': completion_tokens,
                        'cost_usd': round(cost_usd, 6),
                        'operation': self._operation,
                    },
                )
            else:
                logger.debug(
                    f"OpenAI usage [{self._operation}]: model={model} "
                    f"tokens={total_tokens} (no tenant context, skipping DB write)"
                )
        except Exception:
            logger.exception("Failed to record OpenAI usage (non-fatal)")

    def __getattr__(self, name):
        return getattr(self._completions, name)


class _TrackedChat:
    """Proxy for client.chat that wraps completions."""

    def __init__(self, chat, operation: str):
        self._chat = chat
        self._operation = operation

    @property
    def completions(self):
        return _TrackedCompletions(self._chat.completions, self._operation)

    def __getattr__(self, name):
        return getattr(self._chat, name)


class _TrackedResponses:
    """Proxy for client.responses that intercepts create() to record usage."""

    def __init__(self, responses, operation: str):
        self._responses = responses
        self._operation = operation

    def create(self, **kwargs):
        response = self._responses.create(**kwargs)
        self._record_usage(response, kwargs.get("model", "unknown"))
        return response

    def _record_usage(self, response, requested_model: str):
        try:
            usage = getattr(response, 'usage', None)
            if not usage:
                return

            model = getattr(response, 'model', requested_model) or requested_model
            input_tokens = getattr(usage, 'input_tokens', 0) or 0
            output_tokens = getattr(usage, 'output_tokens', 0) or 0
            total_tokens = getattr(usage, 'total_tokens', 0) or (input_tokens + output_tokens)

            # Update rate limiter
            _token_limiter.add_tokens(total_tokens)

            # Record to database if tenant context is available
            from apps.tenants.context import get_current_tenant
            tenant = get_current_tenant()
            if tenant:
                from apps.tenants.services.usage_tracker import track_usage
                from apps.tenants.models import UsageRecord
                cost_usd = calculate_cost_usd(model, input_tokens, output_tokens)
                track_usage(
                    tenant=tenant,
                    event_type=UsageRecord.EVENT_API_CALL,
                    resource_type='openai',
                    tokens_used=total_tokens,
                    metadata={
                        'model': model,
                        'input_tokens': input_tokens,
                        'output_tokens': output_tokens,
                        'cost_usd': round(cost_usd, 6),
                        'operation': self._operation,
                    },
                )
            else:
                logger.debug(
                    f"OpenAI usage [{self._operation}]: model={model} "
                    f"tokens={total_tokens} (no tenant context, skipping DB write)"
                )
        except Exception:
            logger.exception("Failed to record OpenAI responses usage (non-fatal)")

    def __getattr__(self, name):
        return getattr(self._responses, name)


class TrackedOpenAI:
    """
    Wraps an OpenAI client to automatically record token usage per tenant.

    All chat.completions.create() calls are intercepted to:
    1. Record tokens and cost in UsageRecord (if tenant context is set)
    2. Update the in-memory rate limiter
    3. Log usage for debugging

    Other API methods (embeddings, beta, etc.) are proxied unchanged.
    """

    def __init__(self, client: OpenAI, operation: str = "unknown"):
        self._client = client
        self._operation = operation

    @property
    def chat(self):
        return _TrackedChat(self._client.chat, self._operation)

    @property
    def responses(self):
        return _TrackedResponses(self._client.responses, self._operation)

    def __getattr__(self, name):
        return getattr(self._client, name)


# =============================================================================
# API CONFIGURATION
# =============================================================================

def get_openai_client(api_key: Optional[str] = None, operation: str = "unknown") -> OpenAI:
    """
    Get OpenAI client instance with proper configuration and usage tracking.

    Args:
        api_key: Optional API key override. If None, uses OPENAI_API_KEY env var.
        operation: Label for this operation (e.g., "dwr_parse", "chat_assistant").
                   Used in usage records to identify what triggered the API call.

    Returns:
        Configured OpenAI client (TrackedOpenAI wrapper if tracking is enabled)

    Raises:
        RuntimeError: If API key not configured
    """
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY not configured. "
            "Set it in .env or pass api_key parameter."
        )

    client = OpenAI(
        api_key=key,
        max_retries=5,
        timeout=120.0,
    )

    if os.getenv("TRACK_OPENAI_USAGE", "true").lower() == "true":
        return TrackedOpenAI(client, operation=operation)
    return client


# =============================================================================
# STRUCTURED OUTPUTS HELPERS
# =============================================================================

def create_json_schema(
    name: str,
    properties: dict,
    required: list,
    strict: bool = True,
    additional_properties: bool = False
) -> dict:
    """
    Create JSON schema for structured outputs.
    
    Args:
        name: Schema name
        properties: Property definitions
        required: List of required field names
        strict: Use strict mode (100% reliable, recommended)
        additional_properties: Allow fields not in schema
    
    Returns:
        OpenAI-compatible JSON schema
    
    Example:
        >>> schema = create_json_schema(
        ...     name="well_data",
        ...     properties={
        ...         "api": {"type": "string"},
        ...         "depth": {"type": "number"}
        ...     },
        ...     required=["api"]
        ... )
    """
    return {
        "name": name,
        "schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": additional_properties,
        },
        "strict": strict,
    }


# =============================================================================
# PROMPT CACHING PATTERNS
# =============================================================================

def build_cached_messages(
    system_prompt: str,
    context: str,
    user_message: str,
    history: Optional[list] = None
) -> list:
    """
    Build message array optimized for prompt caching.
    
    Caching strategy:
    1. System prompt (cached - reused across requests)
    2. Context (cached - reused when unchanged)
    3. History (varies per conversation)
    4. New user message (always fresh)
    
    Args:
        system_prompt: System instructions (will be cached)
        context: Static context like plan data (will be cached)
        user_message: New user input
        history: Previous messages in conversation
    
    Returns:
        Message list optimized for caching
    
    Savings:
        ~50% cost reduction on cached tokens (system + context)
    """
    messages = [
        {"role": "system", "content": system_prompt},
    ]
    
    if context:
        messages.append({
            "role": "system",
            "content": f"**Context (cached):**\n{context}"
        })
    
    if history:
        messages.extend(history)
    
    messages.append({
        "role": "user",
        "content": user_message
    })
    
    return messages


# =============================================================================
# USAGE TRACKING (Optional)
# =============================================================================

def check_rate_limit(estimated_tokens: int = 15000) -> None:
    """
    Check and apply rate limiting before making OpenAI API calls.
    
    Prevents hitting the TPM (Tokens Per Minute) limit by throttling requests.
    
    Args:
        estimated_tokens: Estimated tokens for the request (default: 15000 for chat)
    
    Raises:
        None - will sleep if needed
    
    Example:
        >>> check_rate_limit(estimated_tokens=12000)
        >>> response = client.chat.completions.create(...)
    """
    should_throttle, wait_time = _token_limiter.should_throttle(estimated_tokens)
    
    if should_throttle:
        logger.warning(
            f"[Rate Limiter] TPM limit approaching. "
            f"Waiting {wait_time:.1f}s before next request. "
            f"Estimated tokens for next request: {estimated_tokens}"
        )
        time.sleep(wait_time)


def log_openai_usage(response, operation: str):
    """
    Log OpenAI API usage for cost tracking and update rate limiter.

    Note: This is a legacy function that only logs to Python logger.
    For DB-backed per-tenant tracking, use TrackedOpenAI wrapper via
    get_openai_client(operation="...") instead.

    Args:
        response: OpenAI API response
        operation: Operation name (e.g., "document_extraction", "chat")

    Example:
        >>> response = client.chat.completions.create(...)
        >>> log_openai_usage(response, "chat_message")
    """
    try:
        usage = getattr(response, 'usage', None)
        if usage:
            # Track tokens for rate limiting
            _token_limiter.add_tokens(usage.total_tokens)

            logger.info(
                f"OpenAI Usage [{operation}]: "
                f"prompt={usage.prompt_tokens} "
                f"completion={usage.completion_tokens} "
                f"total={usage.total_tokens}"
            )
    except Exception:
        pass  # Don't fail on logging errors


# =============================================================================
# RECOMMENDED SETTINGS BY USE CASE
# =============================================================================

SETTINGS_BY_USE_CASE = {
    "document_extraction": {
        "model": DEFAULT_EXTRACTION_MODEL,
        "temperature": TEMPERATURE_FACTUAL,
        "response_format": {"type": "json_object"},
        "max_tokens": 4000,
    },
    "chat_assistant": {
        "model": DEFAULT_CHAT_MODEL,
        "temperature": TEMPERATURE_LOW,
        "tools_enabled": True,
    },
    "compliance_check": {
        "model": DEFAULT_REASONING_MODEL,  # Use reasoning for complex logic
        "temperature": TEMPERATURE_FACTUAL,
    },
    "embeddings": {
        "model": DEFAULT_EMBEDDING_MODEL,
        "dimensions": DEFAULT_EMBEDDING_DIMENSIONS,
        "description": "Text embeddings for semantic search (3072-dim)",
    },
    "batch_processing": {
        "model": DEFAULT_BATCH_MODEL,
        "temperature": TEMPERATURE_FACTUAL,
        "note": "50% cost savings vs sync"
    }
}


def get_recommended_settings(use_case: str) -> dict:
    """
    Get recommended OpenAI settings for a specific use case.
    
    Args:
        use_case: One of: document_extraction, chat_assistant, 
                  compliance_check, embeddings, batch_processing
    
    Returns:
        Dict of recommended settings
    
    Example:
        >>> settings = get_recommended_settings("chat_assistant")
        >>> response = client.chat.completions.create(**settings, messages=...)
    """
    return SETTINGS_BY_USE_CASE.get(use_case, {
        "model": DEFAULT_CHAT_MODEL,
        "temperature": TEMPERATURE_BALANCED
    })

