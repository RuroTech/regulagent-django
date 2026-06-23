"""
Form type constants and mapping utilities for multi-jurisdiction support.

This module provides:
- Constants for form types across jurisdictions (TX, NM, CO)
- Mapping functions to translate between jurisdiction-specific form types
- Utilities for normalizing form references

Jurisdiction Form Mappings:
- TX W-3A (Plugging Plan) ↔ NM C-103 (Sundry Notice)
- TX W-2 (Completion) ↔ NM C-105 (Completion Report)
- TX W-1 (Drilling Permit) ↔ NM C-101 (Application to Drill)
- TX W-1A (Location Plat) ↔ NM C-102 (Well Location Plat)
- TX P-1 (Allowable Request) ↔ NM C-104 (Request for Allowable)
"""

from typing import Optional


# Texas RRC Form Types
TX_W1 = "w1"           # Application to Drill
TX_W1A = "w1a"         # Well Location and Acreage Plat
TX_W2 = "w2"           # Completion Report
TX_W3 = "w3"           # Plugging Report (filed after plugging)
TX_W3A = "w3a"         # Plugging Plan (filed before plugging)
TX_W15 = "w15"         # Recompletion/Workover Report
TX_G1 = "g1"           # Sundry Notice
TX_GAU = "gau"         # Gas Analysis Unit
TX_P1 = "p1"           # Request for Allowable

# New Mexico OCD Form Types
NM_C101 = "c101"       # Application to Drill
NM_C102 = "c102"       # Well Location Plat
NM_C103 = "c103"       # Sundry Notices (used for plugging)
NM_C104 = "c104"       # Request for Allowable
NM_C105 = "c105"       # Completion Report

# Colorado COGCC Form Types (placeholder for future)
CO_2 = "co2"           # Application for Permit to Drill
CO_4 = "co4"           # Sundry Notice
CO_5A = "co5a"         # Completion Report

# ---------- BLM (Federal) Forms ----------
BLM_SUNDRY_3160_5 = "blm_sundry_3160_5"  # Sundry Notices and Reports on Wells

# Generic document types (not jurisdiction-specific)
SCHEMATIC = "schematic"
FORMATION_TOPS = "formation_tops"
PA_PROCEDURE = "pa_procedure"

# All supported form types
ALL_FORM_TYPES = [
    # Texas
    TX_W1, TX_W1A, TX_W2, TX_W3, TX_W3A, TX_W15, TX_G1, TX_GAU, TX_P1,
    # New Mexico
    NM_C101, NM_C102, NM_C103, NM_C104, NM_C105,
    # Generic
    SCHEMATIC, FORMATION_TOPS,
    # Operator packets (always private)
    PA_PROCEDURE,
]

# Public document types (available to all tenants when validated)
PUBLIC_DOC_TYPES = [
    TX_W2, TX_W15, TX_GAU, TX_W3, TX_W3A,  # Texas public docs
    NM_C103, NM_C105,  # New Mexico public docs
]

# Form equivalency mappings between jurisdictions
# Format: (form_type, from_jurisdiction, to_jurisdiction) -> equivalent_form_type
FORM_EQUIVALENCE_MAP = {
    # Texas to New Mexico
    (TX_W1, "TX", "NM"): NM_C101,
    (TX_W1A, "TX", "NM"): NM_C102,
    (TX_W3A, "TX", "NM"): NM_C103,
    (TX_G1, "TX", "NM"): NM_C103,
    (TX_P1, "TX", "NM"): NM_C104,
    (TX_W2, "TX", "NM"): NM_C105,

    # New Mexico to Texas
    (NM_C101, "NM", "TX"): TX_W1,
    (NM_C102, "NM", "TX"): TX_W1A,
    (NM_C103, "NM", "TX"): TX_W3A,  # Primary mapping for plugging
    (NM_C104, "NM", "TX"): TX_P1,
    (NM_C105, "NM", "TX"): TX_W2,
}

# Human-readable form names
FORM_NAMES = {
    # Texas
    TX_W1: "TX W-1 - Application to Drill",
    TX_W1A: "TX W-1A - Well Location Plat",
    TX_W2: "TX W-2 - Completion Report",
    TX_W3: "TX W-3 - Plugging Report",
    TX_W3A: "TX W-3A - Plugging Plan",
    TX_W15: "TX W-15 - Recompletion Report",
    TX_G1: "TX G-1 - Sundry Notice",
    TX_GAU: "TX GAU - Gas Analysis",
    TX_P1: "TX P-1 - Request for Allowable",

    # New Mexico
    NM_C101: "NM C-101 - Application to Drill",
    NM_C102: "NM C-102 - Well Location Plat",
    NM_C103: "NM C-103 - Sundry Notice",
    NM_C104: "NM C-104 - Request for Allowable",
    NM_C105: "NM C-105 - Completion Report",

    # Generic
    SCHEMATIC: "Well Schematic",
    FORMATION_TOPS: "Formation Tops",
    PA_PROCEDURE: "P&A Execution Procedure (Operator Packet)",
}


def normalize_form_type(form_type: str) -> str:
    """
    Normalize form type string to lowercase standard format.

    Examples:
        "W-3A" → "w3a"
        "C-103" → "c103"
        "w2" → "w2"

    Args:
        form_type: Form type string in any case/format

    Returns:
        Normalized lowercase form type without hyphens
    """
    if not form_type:
        return ""

    # Lowercase and remove hyphens/spaces
    normalized = str(form_type).lower().strip()
    normalized = normalized.replace("-", "").replace(" ", "")

    return normalized


def get_equivalent_form(
    form_type: str,
    from_jurisdiction: str,
    to_jurisdiction: str
) -> str:
    """
    Get the equivalent form type in another jurisdiction.

    Example:
        get_equivalent_form("w3a", "TX", "NM") → "c103"
        get_equivalent_form("C-103", "NM", "TX") → "w3a"

    Args:
        form_type: Source form type (e.g., "w3a", "W-3A", "c103")
        from_jurisdiction: Source jurisdiction code ("TX", "NM", "CO")
        to_jurisdiction: Target jurisdiction code ("TX", "NM", "CO")

    Returns:
        Equivalent form type in target jurisdiction, or original if no mapping exists
    """
    # Normalize inputs
    normalized_form = normalize_form_type(form_type)
    from_jur = from_jurisdiction.upper().strip()
    to_jur = to_jurisdiction.upper().strip()

    # Same jurisdiction = no conversion needed
    if from_jur == to_jur:
        return normalized_form

    # Look up in equivalence map
    key = (normalized_form, from_jur, to_jur)
    equivalent = FORM_EQUIVALENCE_MAP.get(key)

    # Return equivalent if found, otherwise return original
    return equivalent if equivalent else normalized_form


def get_form_display_name(form_type: str) -> str:
    """
    Get human-readable display name for a form type.

    Handles both underscore-format (canonical runtime: 'c_105') and
    no-underscore format ('c105') by attempting a second lookup with
    underscores stripped when the first lookup misses.

    Args:
        form_type: Form type code (e.g., "w3a", "c103", "c_105")

    Returns:
        Human-readable form name, or normalized form type if unknown
    """
    normalized = normalize_form_type(form_type)
    hit = FORM_NAMES.get(normalized)
    if hit is not None:
        return hit
    # Retry with underscores stripped (handles canonical 'c_105' → 'c105' key)
    stripped = normalized.replace("_", "")
    hit = FORM_NAMES.get(stripped)
    if hit is not None:
        return hit
    return normalized.upper()


def is_plugging_form(form_type: str, jurisdiction: Optional[str] = None) -> bool:
    """
    Check if a form type is a plugging-related form.

    Args:
        form_type: Form type to check
        jurisdiction: Optional jurisdiction context

    Returns:
        True if form is plugging-related (W-3A, W-3, C-103, etc.)
    """
    normalized = normalize_form_type(form_type)

    plugging_forms = {TX_W3, TX_W3A, NM_C103}

    return normalized in plugging_forms


def is_completion_form(form_type: str, jurisdiction: Optional[str] = None) -> bool:
    """
    Check if a form type is a completion-related form.

    Args:
        form_type: Form type to check
        jurisdiction: Optional jurisdiction context

    Returns:
        True if form is completion-related (W-2, W-15, C-105, etc.)
    """
    normalized = normalize_form_type(form_type)

    completion_forms = {TX_W2, TX_W15, NM_C105}

    return normalized in completion_forms


def get_jurisdiction_from_form(form_type: str) -> Optional[str]:
    """
    Infer jurisdiction from form type.

    Args:
        form_type: Form type code

    Returns:
        Jurisdiction code ("TX", "NM", "CO") or None if unknown
    """
    normalized = normalize_form_type(form_type)

    # Texas forms
    tx_forms = {TX_W1, TX_W1A, TX_W2, TX_W3, TX_W3A, TX_W15, TX_G1, TX_GAU, TX_P1}
    if normalized in tx_forms:
        return "TX"

    # New Mexico forms
    nm_forms = {NM_C101, NM_C102, NM_C103, NM_C104, NM_C105}
    if normalized in nm_forms:
        return "NM"

    # Colorado forms
    co_forms = {CO_2, CO_4, CO_5A}
    if normalized in co_forms:
        return "CO"

    return None
