"""
File validation service for tenant-uploaded documents.

Performs security scanning and API number verification before marking
documents as validated.

Phase 1 Implementation:
1. OpenAI security scan for prompt injections and malicious content
2. API number extraction and verification
3. Validation result with errors for rejection tracking
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from apps.public_core.services.openai_config import get_openai_client

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of file validation."""
    is_valid: bool
    errors: List[str]
    warnings: List[str] = None
    warning_code: Optional[str] = None
    extracted_api: Optional[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


def normalize_api(api_str: str) -> str:
    """
    Normalize API number to 14-digit format: XXYYYZZZZZCCSS
    Handles various input formats (10-digit, 12-digit, 14-digit, with/without dashes).
    
    Format: XX-YYY-ZZZZZ-CC-SS
    - XX: State (42 for Texas)
    - YYY: County
    - ZZZZZ: Well number
    - CC: Completion number
    - SS: Sidetrack number
    """
    if not api_str:
        return ""
    
    # Remove all non-digits
    digits = ''.join(c for c in str(api_str) if c.isdigit())
    
    # Handle different lengths
    if len(digits) == 10:
        # 10-digit can be:
        # - XXYYYZZZZZ (state + county + well, missing completion/sidetrack)
        # - YYYZZZZZZC (county + well + completion, missing state/sidetrack)
        if digits.startswith('42'):
            # Already has state code (42 for Texas)
            # 4212345678 → 42-123-45678-00-00
            digits = digits + "0000"  # Pad completion and sidetrack
        else:
            # Missing state code, assume Texas
            # 0001234500 → 42-000-12345-00-00
            digits = "42" + digits + "00"
    elif len(digits) == 12:
        # 12-digit: XXYYYZZZZZCC
        # Check if starts with valid state code (42 for Texas)
        if digits.startswith('42'):
            # Has state code, just pad sidetrack
            digits = digits + "00"
        else:
            # Assume first 2 digits are not state, prepend 42
            # But this is ambiguous - for now just pad
            digits = digits + "00"
    elif len(digits) == 14:
        # Already 14 digits
        pass
    else:
        # Return as-is for unexpected formats
        return digits
    
    # Ensure exactly 14 digits
    if len(digits) == 14:
        return digits
    
    return digits


def api_matches(extracted_api: str, expected_api: str, fuzzy: bool = True) -> bool:
    """
    Check if extracted API matches expected API.
    
    Args:
        extracted_api: API from document extraction
        expected_api: API provided by user
        fuzzy: If True, match on last 8 digits (well number + completion)
               If False, require exact 14-digit match
    
    Returns:
        True if APIs match
    """
    norm_extracted = normalize_api(extracted_api)
    norm_expected = normalize_api(expected_api)
    
    if not norm_extracted or not norm_expected:
        return False
    
    if fuzzy:
        # Match on last 8 digits (well number + completion suffix)
        return norm_extracted[-8:] == norm_expected[-8:]
    else:
        # Exact 14-digit match
        return norm_extracted == norm_expected


ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc"}
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
}


def validate_file_extension(file_path: Path, content_type: str = "") -> ValidationResult:
    """
    Check that the file has an allowed extension and, when provided, an
    allowed MIME type.

    Allowed formats: .pdf, .docx, .doc

    Args:
        file_path:    Path to the file on disk.
        content_type: Optional MIME type string from the HTTP upload.

    Returns:
        ValidationResult with is_valid=True when the file passes all checks.
    """
    suffix = file_path.suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return ValidationResult(
            is_valid=False,
            errors=[
                f"File type '{suffix}' is not allowed. "
                f"Accepted formats: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            ],
        )

    if content_type:
        # Strip parameters (e.g. "application/pdf; charset=utf-8")
        mime = content_type.split(";")[0].strip().lower()
        if mime and mime not in ALLOWED_MIME_TYPES:
            return ValidationResult(
                is_valid=False,
                errors=[
                    f"MIME type '{mime}' is not allowed for extension '{suffix}'."
                ],
            )

    return ValidationResult(is_valid=True, errors=[])


def _extract_text_for_scan(file_path: Path) -> str:
    """
    Extract plain text from a PDF or Word document for security scanning.
    Returns empty string on failure.
    """
    suffix = file_path.suffix.lower()
    if suffix in (".docx", ".doc"):
        try:
            from apps.public_core.services.docx_extraction import extract_text_from_docx
            text, _ = extract_text_from_docx(str(file_path))
            return text
        except Exception as e:
            logger.warning("_extract_text_for_scan: docx extraction failed: %s", e)
            return ""
    # Default: PDF
    try:
        from apps.public_core.services.openai_extraction import _extract_pdf_text
        return _extract_pdf_text(file_path, max_chars=50000)
    except Exception as e:
        logger.warning("_extract_text_for_scan: pdf extraction failed: %s", e)
        return ""


def openai_security_scan(file_path: Path, document_type: str = "unknown") -> ValidationResult:
    """
    Scan document for security issues using OpenAI moderation.

    Supports .pdf and .docx/.doc files. Returns a validation error for any
    other extension.

    Checks for:
    - Prompt injection attempts
    - Malicious content
    - Unsafe instructions

    Args:
        file_path: Path to the uploaded file (.pdf or .docx/.doc)
        document_type: Type of document (w2, gau, pa_procedure, etc.)

    Returns:
        ValidationResult with is_valid=True if safe
    """
    try:
        suffix = Path(file_path).suffix.lower()

        # --- Text extraction: branch by file type ---
        text = ""
        try:
            if suffix in (".docx", ".doc"):
                from apps.public_core.services.docx_extraction import extract_text_from_docx
                text, _ = extract_text_from_docx(str(file_path))
            elif suffix == ".pdf":
                from apps.public_core.services.openai_extraction import _extract_pdf_text
                text = _extract_pdf_text(Path(file_path), max_chars=50000)
            else:
                return ValidationResult(
                    is_valid=False,
                    errors=[
                        f"Unsupported file type '{suffix}' for security scan. "
                        f"Accepted: .pdf, .docx, .doc"
                    ],
                )
        except Exception as e:
            logger.exception("openai_security_scan: failed to extract file text")
            return ValidationResult(
                is_valid=False,
                errors=[f"Failed to read file: {str(e)}"]
            )

        if not text or len(text.strip()) < 50:
            return ValidationResult(
                is_valid=False,
                errors=["File appears empty or unreadable"]
            )
        
        # OpenAI Moderation API check
        client = get_openai_client(operation="file_validation")
        
        try:
            moderation_response = client.moderations.create(input=text[:32000])  # API limit
            
            # Check if flagged
            result = moderation_response.results[0]
            if result.flagged:
                # Get flagged categories
                flagged_categories = [
                    cat for cat, flagged in result.categories.__dict__.items()
                    if flagged
                ]
                
                return ValidationResult(
                    is_valid=False,
                    errors=[
                        f"Security scan failed: content flagged for {', '.join(flagged_categories)}"
                    ]
                )
            
        except Exception as e:
            logger.exception("openai_security_scan: moderation API call failed")
            return ValidationResult(
                is_valid=False,
                errors=[f"Security scan failed: {str(e)}"]
            )
        
        # Additional heuristics for prompt injection
        prompt_injection_patterns = [
            "ignore previous instructions",
            "ignore all previous",
            "disregard all previous",
            "new instructions:",
            "system message:",
            "override instructions",
            "jailbreak",
            "act as if you are",
            "pretend you are",
        ]
        
        text_lower = text.lower()
        detected_patterns = [
            pattern for pattern in prompt_injection_patterns
            if pattern in text_lower
        ]
        
        if detected_patterns:
            return ValidationResult(
                is_valid=False,
                errors=[
                    f"Potential prompt injection detected: {', '.join(detected_patterns[:3])}"
                ],
                warnings=["Document contains suspicious instruction-like patterns"]
            )
        
        # Passed all checks
        return ValidationResult(is_valid=True, errors=[])
        
    except Exception as e:
        logger.exception("openai_security_scan: unexpected error")
        return ValidationResult(
            is_valid=False,
            errors=[f"Validation system error: {str(e)}"]
        )


def verify_api_number(
    json_data: dict,
    expected_api: str,
    fuzzy_match: bool = True
) -> ValidationResult:
    """
    Verify that extracted JSON contains the expected API number.

    Args:
        json_data: Already-extracted document JSON (from extract_json_from_pdf)
        expected_api: API number provided by user
        fuzzy_match: If True, match on last 8 digits

    Returns:
        ValidationResult with is_valid=True if API matches
    """
    try:
        # Try common API field locations
        extracted_api = None
        if "well_info" in json_data:
            extracted_api = json_data["well_info"].get("api") or json_data["well_info"].get("api_number")

        if not extracted_api and "header" in json_data:
            extracted_api = json_data["header"].get("api") or json_data["header"].get("api_number")

        if not extracted_api:
            # Fallback: search all top-level fields
            for key in ["api", "api_number", "api14", "well_api"]:
                if key in json_data:
                    extracted_api = json_data[key]
                    break

        if not extracted_api:
            return ValidationResult(
                is_valid=False,
                errors=["Could not extract API number from document"],
                warning_code="api_not_found",
                extracted_api=None,
            )

        # Verify API match
        if api_matches(extracted_api, expected_api, fuzzy=fuzzy_match):
            return ValidationResult(
                is_valid=True,
                errors=[],
                warnings=[
                    f"Matched API: {extracted_api} (expected: {expected_api})"
                ]
            )
        else:
            return ValidationResult(
                is_valid=False,
                errors=[
                    f"API mismatch: document contains '{extracted_api}', expected '{expected_api}'"
                ],
                warning_code="api_mismatch",
                extracted_api=extracted_api,
            )

    except Exception as e:
        logger.exception("verify_api_number: unexpected error")
        return ValidationResult(
            is_valid=False,
            errors=[f"API verification system error: {str(e)}"]
        )


def validate_uploaded_file(
    file_path: Path,
    document_type: str,
    expected_api: str,
    skip_security_scan: bool = False,
    fuzzy_api_match: bool = True,
    json_data: dict = None,
    content_type: str = "",
) -> ValidationResult:
    """
    Complete validation pipeline for tenant-uploaded files.

    Validation steps:
    0. File extension / MIME type check (.pdf, .docx, .doc)
    1. Security scan (OpenAI moderation + prompt injection detection)
    2. API number verification against pre-extracted JSON

    Args:
        file_path: Path to uploaded file (PDF or Word document)
        document_type: Document type (w2, gau, w15, pa_procedure, etc.)
        expected_api: API number provided by user
        skip_security_scan: Skip security checks (for testing only)
        fuzzy_api_match: Match on last 8 digits vs exact 14-digit
        json_data: Already-extracted document JSON. When provided, API
                   verification runs against this data instead of
                   triggering a separate extraction call.
        content_type: Optional MIME type from the HTTP upload header.

    Returns:
        ValidationResult with is_valid=True if all checks pass
    """
    all_errors = []
    all_warnings = []

    # Step 0: Extension and MIME type check
    ext_result = validate_file_extension(file_path, content_type=content_type)
    if not ext_result.is_valid:
        return ValidationResult(
            is_valid=False,
            errors=ext_result.errors,
            warnings=all_warnings,
        )

    # Step 1: Security scan (uses lightweight text extraction, not full OpenAI call)
    if not skip_security_scan:
        logger.info(f"validate_uploaded_file: running security scan for {file_path}")
        security_result = openai_security_scan(file_path, document_type)

        if not security_result.is_valid:
            all_errors.extend(security_result.errors)
            return ValidationResult(
                is_valid=False,
                errors=all_errors,
                warnings=all_warnings
            )

        all_warnings.extend(security_result.warnings)

    # Step 2: API verification (requires pre-extracted JSON)
    if json_data is not None:
        logger.info(f"validate_uploaded_file: verifying API number for {file_path}")
        api_result = verify_api_number(
            json_data,
            expected_api,
            fuzzy_match=fuzzy_api_match,
        )

        if not api_result.is_valid:
            all_errors.extend(api_result.errors)
            return ValidationResult(
                is_valid=False,
                errors=all_errors,
                warnings=all_warnings,
                warning_code=api_result.warning_code,
                extracted_api=api_result.extracted_api,
            )

        all_warnings.extend(api_result.warnings)

    # All checks passed
    logger.info(f"validate_uploaded_file: validation PASSED for {file_path}")
    return ValidationResult(
        is_valid=True,
        errors=[],
        warnings=all_warnings
    )

