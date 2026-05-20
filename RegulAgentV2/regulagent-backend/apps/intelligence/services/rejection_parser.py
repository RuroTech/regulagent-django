"""
RejectionParser: AI service that parses raw agency rejection remarks into
structured field-level issues using OpenAI structured outputs.
"""

import json
import logging
from typing import Any

from apps.intelligence.constants import ISSUE_CATEGORIES
from apps.intelligence.models import RejectionRecord
from apps.public_core.services.openai_config import (
    DEFAULT_CHAT_MODEL,
    TEMPERATURE_FACTUAL,
    check_rate_limit,
    create_json_schema,
    get_openai_client,
)

logger = logging.getLogger(__name__)

# W-3A field names (Texas RRC plugging form)
W3A_FIELDS = [
    "plug_type",
    "depth_top",
    "depth_bottom",
    "cement_volume",
    "cement_class",
    "woc_time",
    "retainer_type",
    "retainer_depth",
    "squeeze_pressure",
    "formation_name",
    "casing_od",
    "casing_weight",
    "casing_depth",
    "perforations_top",
    "perforations_bottom",
    "tbg_od",
    "tbg_weight",
    "tbg_depth",
    "operator_name",
    "api_number",
    "well_number",
    "lease_name",
    "county",
    "district",
    "field_name",
    "plugging_date",
    "contractor_name",
    "remarks",
]

# W-3 field names (same base as W-3A, superset)
W3_FIELDS = W3A_FIELDS + [
    "completion_date",
    "production_method",
    "surface_casing_depth",
    "intermediate_casing_depth",
]

# C-103 field names (New Mexico NMOCD)
C103_FIELDS = [
    "plug_type",
    "formation",
    "cement_volume",
    "woc_time",
    "depth_top",
    "depth_bottom",
    "cement_class",
    "retainer_type",
    "retainer_depth",
    "api_number",
    "lease_name",
    "well_number",
    "region",
    "sub_area",
    "lease_type",
    "coa_figure",
    "operator_name",
    "plugging_date",
    "contractor_name",
    "surface_casing_od",
    "intermediate_casing_od",
    "production_casing_od",
    "remarks",
]

# C-104 / C-105 share same base as C-103
C104_FIELDS = C103_FIELDS
C105_FIELDS = C103_FIELDS

FIELD_MAP: dict[str, list[str]] = {
    "w3": W3_FIELDS,
    "w3a": W3A_FIELDS,
    "c103": C103_FIELDS,
    "c104": C104_FIELDS,
    "c105": C105_FIELDS,
}

REJECTION_STATUSES_REQUIRING_NOTES = {
    "rejected",
    "revision_requested",
    "deficiency",
}


class RejectionParser:
    """
    Parses raw agency rejection remarks into structured field-level issues using AI.

    Uses OpenAI structured outputs (gpt-4o, temperature 0.0) to identify specific
    field-level problems in a submitted regulatory form based on the agency's
    rejection notes and the original submitted form snapshot.
    """

    def __init__(self) -> None:
        self._client = get_openai_client(operation="rejection_parser")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_rejection(self, rejection_record: RejectionRecord) -> list[dict]:
        """
        Parse raw_rejection_notes + submitted_form_snapshot into parsed_issues.

        Returns a list of issue dicts matching the parsed_issues JSONField schema:
        [
            {
                "field_name": "plug_type",
                "field_value": "CIBP cap",
                "expected_value": "Cement Plug",
                "issue_category": "terminology",
                "issue_subcategory": "naming_convention",
                "severity": "rejection",
                "description": "RRC requires 'Cement Plug' not 'CIBP cap'",
                "form_section": "plugging_record",
                "confidence": 0.92
            }
        ]
        """
        if not rejection_record.raw_rejection_notes.strip():
            logger.warning(
                "[RejectionParser] RejectionRecord %s has no raw_rejection_notes — skipping parse.",
                rejection_record.id,
            )
            return []

        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(rejection_record)
        schema = self._build_json_schema()

        check_rate_limit(estimated_tokens=8000)

        try:
            response = self._client.chat.completions.create(
                model=DEFAULT_CHAT_MODEL,
                temperature=TEMPERATURE_FACTUAL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": schema,
                },
            )

            raw_content = response.choices[0].message.content
            parsed = json.loads(raw_content)
            issues: list[dict] = parsed.get("issues", [])

            for issue in issues:
                issue.setdefault("policy_references", [])

            logger.info(
                "[RejectionParser] Parsed %d issue(s) for RejectionRecord %s.",
                len(issues),
                rejection_record.id,
            )
            return issues

        except Exception:
            logger.exception(
                "[RejectionParser] Failed to parse rejection notes for record %s.",
                rejection_record.id,
            )
            return []

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        categories_text = self._format_categories()
        return f"""You are an expert regulatory compliance analyst specializing in oil and gas plugging regulations.

Your task is to analyze agency rejection remarks for a submitted regulatory form and identify specific field-level issues.

## Instructions

1. **Read** the rejection remarks carefully — they may be explicit ("plug type must be 'Cement Plug'") or vague ("form needs corrections").
2. **Cross-reference** each remark against the submitted form snapshot to identify which field(s) are problematic and what the submitted value was.
3. **Categorize** each issue using the provided category taxonomy.
4. **Assign confidence** (0.0–1.0):
   - 0.9–1.0: Remark explicitly names the field and required value.
   - 0.6–0.9: Remark implies the field; submitted value clearly wrong.
   - 0.3–0.6: Remark is vague; best-guess at affected field.
   - < 0.3: Cannot determine specific field — still report with low confidence.
5. **Do not hallucinate** field names. Use only the valid field names provided in the user prompt.
6. If the remark is entirely unactionable (e.g., "please resubmit"), return an empty issues list.
7. **Cite policy references**: For each issue, list any specific regulatory rule numbers, RRC statewide rules, NMOCD regulations, or form instruction sections that the submitted value violates. If the rejection remark explicitly cites a rule (e.g., "per Rule 37", "16 TAC §3.14"), include it. If you can infer a relevant rule from domain knowledge (e.g., plug type must comply with Statewide Rule 14), include it. If no specific rule applies, return an empty list.

## Issue Categories
{categories_text}

## Severity Values
- `rejection`: The field caused a hard rejection (must be corrected).
- `deficiency`: A deficiency notice — must address before approval.
- `revision`: A revision request — recommended correction.
- `warning`: A warning that may not block approval.

## Output Format
Return valid JSON matching the provided schema. Do not include any prose outside the JSON."""

    def _build_user_prompt(self, rejection_record: RejectionRecord) -> str:
        valid_fields = self._get_valid_field_names(rejection_record.form_type)
        snapshot = rejection_record.submitted_form_snapshot or {}

        snapshot_text = json.dumps(snapshot, indent=2) if snapshot else "(not available)"

        geo_parts = []
        if rejection_record.state:
            geo_parts.append(f"State: {rejection_record.state}")
        if rejection_record.district:
            geo_parts.append(f"District: {rejection_record.district}")
        if rejection_record.county:
            geo_parts.append(f"County: {rejection_record.county}")
        geo_context = ", ".join(geo_parts) if geo_parts else "Unknown"

        agency_label = dict(
            [("RRC", "Texas Railroad Commission"), ("NMOCD", "New Mexico Oil Conservation Division")]
        ).get(rejection_record.agency, rejection_record.agency)

        return f"""## Agency Rejection Details

**Agency:** {agency_label} ({rejection_record.agency})
**Form Type:** {rejection_record.form_type.upper()}
**Geographic Context:** {geo_context}
**Rejection Date:** {rejection_record.rejection_date or "Unknown"}
**Reviewer:** {rejection_record.reviewer_name or "Unknown"}

## Raw Rejection Remarks

{rejection_record.raw_rejection_notes}

## Submitted Form Snapshot

```json
{snapshot_text}
```

## Valid Field Names for {rejection_record.form_type.upper()}

{json.dumps(valid_fields, indent=2)}

## Task

Analyze the rejection remarks above and identify every field-level issue.
Map each issue to one of the valid field names listed.
Return your analysis as JSON matching the schema."""

    # ------------------------------------------------------------------
    # Schema + field name helpers
    # ------------------------------------------------------------------

    def _get_valid_field_names(self, form_type: str) -> list[str]:
        """Return valid field names for the given form type."""
        return FIELD_MAP.get(form_type.lower(), W3A_FIELDS)

    def _build_json_schema(self) -> dict:
        """Build the OpenAI JSON schema for structured output."""
        issue_properties: dict[str, Any] = {
            "field_name": {
                "type": "string",
                "description": "The specific form field with the issue (use one of the valid field names).",
            },
            "field_value": {
                "type": "string",
                "description": "The value that was submitted for this field (empty string if unknown).",
            },
            "expected_value": {
                "type": "string",
                "description": "The correct or expected value per agency requirements (empty string if not determinable).",
            },
            "issue_category": {
                "type": "string",
                "enum": list(ISSUE_CATEGORIES.keys()),
                "description": "Top-level issue category.",
            },
            "issue_subcategory": {
                "type": "string",
                "description": "Subcategory within the issue category (empty string if not applicable).",
            },
            "severity": {
                "type": "string",
                "enum": ["rejection", "deficiency", "revision", "warning"],
                "description": "How severely this issue affects the filing.",
            },
            "description": {
                "type": "string",
                "description": "Human-readable explanation of the issue and what must be corrected.",
            },
            "form_section": {
                "type": "string",
                "description": "Section of the form where this field appears (e.g., 'plugging_record', 'header', 'casing_record').",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence score 0.0–1.0 that this is a real issue in this field.",
            },
            "policy_references": {
                "type": "array",
                "description": "List of specific regulatory rule citations that justify this correction (e.g., '16 TAC §3.14(b)(2)'). Empty list [] if no specific rule citation is found in the rejection remarks.",
                "items": {"type": "string"},
            },
        }

        issue_required = [
            "field_name",
            "field_value",
            "expected_value",
            "issue_category",
            "issue_subcategory",
            "severity",
            "description",
            "form_section",
            "confidence",
            "policy_references",
        ]

        return create_json_schema(
            name="rejection_parse_result",
            properties={
                "issues": {
                    "type": "array",
                    "description": "List of field-level issues found in the rejection.",
                    "items": {
                        "type": "object",
                        "properties": issue_properties,
                        "required": issue_required,
                        "additionalProperties": False,
                    },
                }
            },
            required=["issues"],
            strict=True,
            additional_properties=False,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _format_categories(self) -> str:
        lines = []
        for cat_key, cat_data in ISSUE_CATEGORIES.items():
            lines.append(f"- **{cat_key}** ({cat_data['label']})")
            for sub_key, sub_label in cat_data["subcategories"].items():
                lines.append(f"  - `{sub_key}`: {sub_label}")
        return "\n".join(lines)
