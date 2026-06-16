from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
import logging
import inspect
import pdfplumber
import fitz  # PyMuPDF
import pytesseract
from PIL import Image
import io

from .openai_config import get_openai_client, DEFAULT_CHAT_MODEL, DEFAULT_EMBEDDING_MODEL
from apps.public_core.services.text_processing import json_to_prose, chunk_text

logger = logging.getLogger(__name__)


# Backward-compatible alias for modules that import _openai_client from here
def _openai_client():  # pragma: no cover
    return get_openai_client(operation="document_extraction")


SUPPORTED_TYPES = {
    "gau": {
        "prompt_key": "gau",
        "required_sections": [
            "header",
            "operator_info",
            "well_info",
            "purpose_and_location",
            "recommendation",
            "footnotes",
        ],
    },
    "w2": {
        "prompt_key": "w2",
        "required_sections": [
            "header",
            "operator_info",
            "well_info",
            "filing_info",
            "completion_info",
            "surface_casing_determination",
            "initial_potential_test",
            "casing_record",
            "liner_record",
            "tubing_record",
            "producing_injection_disposal_interval",
            "acid_fracture_operations",
            "formation_record",
            "commingling_and_h2s",
            "remarks",
            "rrc_remarks",
            "operator_certification",
            "revisions",
        ],
    },
    "w15": {
        "prompt_key": "w15",
        "required_sections": [
            "header",
            "operator_info",
            "well_info",
            "cementing_data",
            "cementing_to_squeeze",
            "certifications",
            "instructions_section",
        ],
    },
    "schematic": {
        "prompt_key": "schematic",
        "required_sections": [
            "header",
            "location_info",
            "schematic_data",
        ],
    },
    "formation_tops": {
        "prompt_key": "formation_tops",
        "required_sections": [
            "header",
            "formation_record",
            "h2s_flag",
            "downhole_commingled",
            "remarks",
        ],
    },
    "w3a": {
        "prompt_key": "w3a",
        "required_sections": [
            "header",
            "casing_record",
            "perforations",
            "plugging_proposal",
            "duqw",
        ],
    },
    "c_100": {
        "prompt_key": "c_100",
        "required_sections": [
            "header",
            "operator_info",
            "well_info",
        ],
    },
    "c_101": {
        "prompt_key": "c_101",
        "required_sections": [
            "header",
            "operator_info",
            "well_info",
            "proposed_work",
            "casing_record",
            "cement_record",
            "remarks",
        ],
    },
    "c_102": {
        "prompt_key": "c_102",
        "required_sections": [
            "header",
            "operator_info",
            "well_info",
            "completion_data",
            "casing_record",
            "remarks",
        ],
    },
    "c_103": {
        "prompt_key": "c_103",
        "required_sections": [
            "header",
            "operator_info",
            "well_info",
            "notice_type",
            "proposed_work",
            "casing_program",
            "cement_program",
            "plugging_procedure",
            "remarks",
        ],
    },
    "c_104": {
        "prompt_key": "c_104",
        "required_sections": [
            "header",
            "operator_info",
            "well_info",
            "subsequent_report",
            "remarks",
        ],
    },
    "c_105": {
        "prompt_key": "c_105",
        "required_sections": [
            "header",
            "operator_info",
            "well_info",
            "completion_data",
            "casing_record",
            "perforation_record",
            "production_test",
            "cement_record",
            "remarks",
        ],
    },
    "sundry": {
        "prompt_key": "sundry",
        "required_sections": [
            "header",
            "operator_info",
            "well_info",
            "notice_type",
            "description",
            "remarks",
        ],
    },
    "pa_procedure": {
        "prompt_key": "pa_procedure",
        "required_sections": [
            "well_header",
            "pa_procedure_steps",
            "formation_data",
            "operational_comments",
            "contacts",
            "notice_info",
            "existing_wellbore_condition",
            "existing_perforations",
            "casing_record",
        ],
    },
    "w1": {
        "prompt_key": "w1",
        "required_sections": [
            "header",
            "operator_info",
            "well_info",
            "permit_info",
            "location",
            "proposed_work",
            "surface_casing",
            "drilling_contractor",
            "rule_37",
            "attachments",
        ],
    },
    "w3": {
        "prompt_key": "w3",
        "required_sections": [
            "header",
            "operator_info",
            "well_info",
            "plugging_summary",
            "plug_record",
            "casing_record",
            "casing_disposition",
            "mud_data",
            "surface_restoration",
            "certifications",
            "remarks",
        ],
    },
    "g1": {
        "prompt_key": "g1",
        "required_sections": [
            "header",
            "operator_info",
            "well_info",
            "well_status",
            "test_data",
            "deliverability",
            "production_data",
            "remarks",
        ],
    },
    "w12": {
        "prompt_key": "w12",
        "required_sections": ["header", "operator_info", "well_info", "gas_test_data", "remarks"],
    },
    "l1": {
        "prompt_key": "l1",
        "required_sections": ["header", "operator_info", "well_info", "lease_info", "remarks"],
    },
    "p14": {
        "prompt_key": "p14",
        "required_sections": ["header", "operator_info", "well_info", "pressure_test_data", "remarks"],
    },
    "swr10": {
        "prompt_key": "swr10",
        "required_sections": ["header", "operator_info", "well_info", "exception_info", "remarks"],
    },
    "swr13": {
        "prompt_key": "swr13",
        "required_sections": ["header", "operator_info", "well_info", "exception_info", "remarks"],
    },
}

# Aliases — normalize document types stored with/without underscores
SUPPORTED_TYPES["c100"] = SUPPORTED_TYPES["c_100"]
SUPPORTED_TYPES["c101"] = SUPPORTED_TYPES["c_101"]
SUPPORTED_TYPES["c102"] = SUPPORTED_TYPES["c_102"]
SUPPORTED_TYPES["c103"] = SUPPORTED_TYPES["c_103"]
SUPPORTED_TYPES["c104"] = SUPPORTED_TYPES["c_104"]
SUPPORTED_TYPES["c105"] = SUPPORTED_TYPES["c_105"]


# OpenAI Models - using latest available models with best performance
# Updated 2025-11-02: Use gpt-4o for extraction (structured outputs support)
MODEL_CLASSIFIER = os.getenv("OPENAI_CLASSIFIER_MODEL", "gpt-4o-mini")
MODEL_PRIMARY = os.getenv("OPENAI_EXTRACTION_MODEL", "gpt-4o")  # Updated: best for structured outputs
MODEL_BATCH = os.getenv("OPENAI_EXTRACTION_BATCH_MODEL", "gpt-4o")  # 50% cost savings for async
MODEL_EMBEDDING = DEFAULT_EMBEDDING_MODEL


@dataclass
class ExtractionResult:
    document_type: str
    json_data: Dict[str, Any]
    model_tag: str
    errors: List[str]
    raw_text: str = ""
    tokens_used: int = 0


def _extract_pdf_text(file_path: Path, max_chars: int = 20000) -> str:
    """Best-effort text extraction for context. Truncates to max_chars."""
    text_parts: List[str] = []
    try:
        with pdfplumber.open(str(file_path)) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t:
                    text_parts.append(t)
                if sum(len(x) for x in text_parts) >= max_chars:
                    break
    except Exception:
        pass
    # Fallback to PyMuPDF
    if not text_parts:
        try:
            doc = fitz.open(str(file_path))
            for i, page in enumerate(doc):
                t = page.get_text() or ""
                if t:
                    text_parts.append(t)
                if sum(len(x) for x in text_parts) >= max_chars:
                    break
        except Exception:
            pass
    text = "\n\n".join(text_parts).strip()
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


def _json_schema_for(doc_type: str) -> Dict[str, Any]:
    """
    Build JSON schema with structured outputs (strict=True).
    
    Structured outputs guarantee:
    - 100% reliable JSON parsing
    - No hallucinated fields
    - Schema-compliant responses
    
    Updated 2025-11-02: Using OpenAI structured outputs best practice
    """
    req = SUPPORTED_TYPES[doc_type]["required_sections"]
    properties: Dict[str, Any] = {}
    for key in req:
        if key in ("casing_record", "tubing_record", "formation_record", "schematic_data", "formation_data", "existing_perforations", "pa_procedure_steps"):
            properties[key] = {"type": "array"}
        elif key in ("h2s_flag", "downhole_commingled", "remarks"):
            properties[key] = {"type": ["string", "object", "null"]}
        else:
            properties[key] = {"type": ["object", "string", "null"]}
    schema = {
        "name": f"regulagent_{doc_type}_schema",
        "schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": properties,
            "required": req,
        },
        "strict": True,  # ← Structured outputs: 100% reliable
    }
    return schema


def classify_document(file_path: Path, candidate_types: list[str] | None = None) -> str:
    """Classify document type using a lightweight model. Returns one of SUPPORTED_TYPES keys or 'unknown'."""
    client = get_openai_client(operation="document_extraction")
    logger.info("classify_document: start file=%s", file_path)
    
    # Check if it's an image file - always classify as schematic
    image_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff'}
    if file_path.suffix.lower() in image_extensions:
        logger.info(f"classify_document: Image file detected ({file_path.suffix}), classifying as schematic")
        return "schematic"
    
    # Minimal heuristic by filename as fallback
    name = file_path.name.lower()
    if re.search(r'\bw-?12\b', name): return "w12"
    if "w-2" in name or "w_2" in name or "w2" in name:
        return "w2"
    if "w-15" in name or "w_15" in name or "w15" in name or "cement" in name:
        return "w15"
    if "gau" in name:
        return "gau"
    if "schematic" in name or "diagram" in name or "wbd" in name:
        return "schematic"
    if "formation" in name and "top" in name:
        return "formation_tops"
    if "c-101" in name or "c101" in name or "c_101" in name:
        return "c_101"
    if "c-103" in name or "c103" in name or "c_103" in name:
        return "c_103"
    if "c-105" in name or "c105" in name or "c_105" in name:
        return "c_105"
    if "sundry" in name:
        return "sundry"
    # w-3a must be checked BEFORE w-3 to avoid false match
    if "w-3a" in name or "w_3a" in name or "w3a" in name:
        return "w3a"
    if "w-1" in name or "w_1" in name or ("w1" in name and "w15" not in name):
        return "w1"
    if "w-3" in name or "w_3" in name or "w3" in name:
        return "w3"
    if "g-1" in name or "g_1" in name or "g1" in name:
        return "g1"
    if re.search(r'\bl-?1\b', name): return "l1"
    if re.search(r'\bp-?14\b', name): return "p14"
    if re.search(r'\bswr[\s-]*10\b', name): return "swr10"
    if re.search(r'\bswr[\s-]*13\b', name): return "swr13"

    # Extract first page text so the LLM has real content (not just the filename)
    first_page_text = ""
    try:  # pragma: no cover
        import pdfplumber
        with pdfplumber.open(str(file_path)) as pdf:
            if pdf.pages:
                first_page_text = (pdf.pages[0].extract_text() or "")[:2000]
    except Exception:
        pass  # PDF reading failed — proceed with filename only

    # If pdfplumber found no text, try OCR (handles scanned/image PDFs)
    if not first_page_text.strip():
        try:
            from pdf2image import convert_from_path
            import pytesseract
            images = convert_from_path(str(file_path), first_page=1, last_page=1, dpi=150)
            if images:
                first_page_text = (pytesseract.image_to_string(images[0]) or "")[:2000]
        except Exception:
            pass  # OCR failed, proceed with filename only

    # Ask the LLM classifier using filename + first-page content
    try:  # pragma: no cover
        type_list = candidate_types if candidate_types else list(SUPPORTED_TYPES.keys())
        type_str = ", ".join(type_list)

        # Build form descriptions so the LLM can distinguish types
        form_descriptions = {
            # NM OCD forms
            "c_100": "C-100: NM OCD well location/record form",
            "c_101": "C-101: NM OCD Application for Permit to Drill (APD)",
            "c_102": "C-102: NM OCD Completion or Workover Report",
            "c_103": "C-103: NM OCD Application to Plug and Abandon",
            "c_104": "C-104: NM OCD Subsequent/Sundry Report — Request for Allowable and Authorization to Transport, production data, monthly reports",
            "c_105": "C-105: NM OCD Sundry Notice and Report on Wells — miscellaneous well operations",
            "sundry": "Sundry Notice: general well operation notice (NM or TX)",
            "apd": "Application for Permit to Drill — federal BLM Form 3160-3 or state APD",
            # TX RRC forms
            "w1": "W-1: TX RRC Drilling Permit",
            "w2": "W-2: TX RRC Completion Report — ONLY for Texas wells",
            "w3": "W-3: TX RRC Plugging Record — ONLY for Texas wells",
            "w3a": "W-3A: TX RRC Application to Plug and Abandon — ONLY for Texas wells",
            "w15": "W-15: TX RRC Completion/Recompletion — ONLY for Texas wells",
            "w12": "W-12: TX RRC Cementing Report — ONLY for Texas wells",
            "gau": "GAU: TX Gas Allowable Update — ONLY for Texas wells",
            "g1": "G-1: TX RRC Organizational Report",
            "schematic": "Well schematic or wellbore diagram",
            "formation_tops": "Formation tops log or chart",
        }
        desc_lines = [f"  {k}: {form_descriptions.get(k, k)}" for k in type_list if k in form_descriptions]
        descriptions_block = "\n".join(desc_lines)

        system_msg = (
            f"You are a regulatory document classifier for oil and gas wells. "
            f"Classify the document into exactly one of these types:\n{descriptions_block}\n  unknown: document does not match any type above\n\n"
            f"IMPORTANT: Look for the form number printed on the document (e.g., 'Form C-104', 'Form 3160-3', 'W-2'). "
            f"Match the form number, not just keywords. If the document says 'C-104' classify as c_104. "
            f"If the form is from a different state than the candidate types suggest, return 'unknown'. "
            f"Federal BLM forms (3160-3, 3160-4, 3160-5) should be classified as 'apd' if they are drilling permits, otherwise 'unknown'. "
            f"Return ONLY the key (e.g., c_103), nothing else."
        )

        content = f"Filename: {file_path.name}"
        if first_page_text:
            content += f"\n\nFirst page text:\n{first_page_text}"
        content += f"\n\nClassify this document. Return only the type key."
        resp = client.chat.completions.create(
            model=MODEL_CLASSIFIER,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": content},
            ],
            temperature=0,
        )
        label = (resp.choices[0].message.content or "").strip().lower()
        ok = label if label in (candidate_types or SUPPORTED_TYPES) else "unknown"
        logger.info("classify_document: label=%s resolved=%s", label, ok)
        return ok
    except Exception:
        logger.exception("classify_document: failed remote classification; falling back to 'unknown'")
        return "unknown"


def _load_prompt(prompt_key: str, tags: Optional[List[str]] = None) -> str:
    # Prompts instruct models to return normalized JSON for downstream planning.
    # Conventions:
    # - Use snake_case keys
    # - Return numeric values as numbers (no units);
    #   depths in feet, sizes in inches
    # - Prefer structured arrays of records for any tabular data
    # - If a requested field cannot be found, include the key with null value
    base = {
        "gau": (
            "Extract GAU (Groundwater Advisory Unit) data. Return JSON with: "
            "operator_info{name,address,operator_number}; "
            "well_info{api,district,county,field,lease,well_no,location{lat,lon}}; "
            "header{date}; purpose_and_location; recommendation; footnotes; "
            "surface_casing_determination{gau_groundwater_protection_determination_depth}. "
            "Operator name: Extract the full operator/company name from the operator_info section, header, or attention line. "
            "Coordinates: If latitude/longitude are present anywhere (maps, headers, footers, or body), output decimal degrees in well_info.location.lat and .lon. "
            "Accept both decimal and DMS formats (e.g., 32°45'30\" N, 102°00'00\" W) and convert to signed decimal (N/E positive, S/W negative). "
            "Use keys lat/lon (not latitude/longitude). Output up to 6 decimal places (e.g., 32.242052, -102.282218). Do not round to whole degrees. "
            "If coordinates cannot be found, set both to null. "
            "Rules: numbers only (feet for depths), snake_case keys, no units in numeric values. If a requested field is missing, set it to null."
        ),
        "w2": (
            "Extract W-2 (Oil/Gas Well Completion) data. Return JSON with: "
            "header{tracking_no}; operator_info{name,address,operator_number}; well_info{api,district,county,field,lease,well_no,location{lat,lon}}; filing_info; completion_info; "
            "surface_casing_determination{gau_groundwater_protection_determination_depth,surface_shoe_depth_ft}; "
            "casing_record:[{string:'surface|intermediate|production|liner', size_in, weight_per_ft, hole_size_in, top_ft, bottom_ft, shoe_depth_ft, cement_top_ft}]; "
            "liner_record:[{size_in, top_ft, bottom_ft, cement_top_ft}]; "
            "tubing_record:[{size_in, top_ft, bottom_ft}]; "
            "producing_injection_disposal_interval:[{from_ft, to_ft, open_hole:true|false}] (CRITICAL - PRODUCTION PERFORATIONS: Find actual perforation/completion intervals in one of these locations: "
            "(1) MODERN FORMS: Table titled 'PRODUCING/INJECTION/DISPOSAL INTERVAL' with columns for From/To depths - extract ALL rows. "
            "(2) LEGACY FORMS (1970s-1990s): Section/Field #47 labeled 'Producing Interval' or 'Indicate Depth of Perforations or Open Hole' - look for text patterns like '7250' To 8129' (12 holes)', 'From: 8556 To: 8822', etc. Extract ALL intervals listed. "
            "VALIDATION RULES: "
            "- These are ACTUAL PERFORATIONS where oil/gas enters the wellbore, NOT liner depths, NOT tubing depths, NOT treatment intervals "
            "- Perforations must be within or just below production casing shoe depth (verify against casing_record) "
            "- Cross-check with acid_fracture_operations: stimulation treatments usually occur at perforation depths "
            "- If you see 'Open hole' or 'open hole completion' marked, set open_hole=true, otherwise false "
            "- DO NOT use liner top/bottom as perforations (liner is a casing string, not a perforation) "
            "- DO NOT use depths from cementing operations "
            "EXTRACTION: Each row/line should extract From/To depths as numbers. Return array of all intervals found. If NO perforations found anywhere on form, set to null (not empty array)); "
            "acid_fracture_operations:[{operation_type, amount_and_kind_of_material_used, from_ft, to_ft, open_hole, notes}] (Extract all rows from the table titled 'ACID, FRACTURE, CEMENT SQUEEZE, CAST IRON BRIDGE PLUG, RETAINER, ETC.' Return array of objects. operation_type: classify as 'mechanical_plug'(CIBP/BRIDGE PLUG/RETAINER), 'acid'(HCL/acid), 'cement_squeeze'(squeeze), 'fracture'(sand+water), or raw text. amount_and_kind_of_material_used: extract full raw string preserving capitalization/units. from_ft/to_ft: numeric depths; if single depth set both equal. open_hole: true if marked open hole. notes: any descriptive text not captured (e.g. 'set CIBP @ 10490'), null if none. If table missing/blank, return empty array []); "
            "kop:{kop_md_ft,kop_tvd_ft} (Kick-Off Point - look in remarks section for 'KOP' followed by MD and TV/TVD depths); "
            "commingling_and_h2s; remarks; rrc_remarks; operator_certification; "
            "revisions:{revising_tracking_number, revision_reason, other_changes}. "
            "TRACKING NO EXTRACTION: Extract 'Tracking No.' from the header/top section of the form (not ticket number). Format is typically 'Tracking No. XXXX' or similar. "
            "REVISION DETECTION: If remarks indicate this is a revision/correction filing of a previous submission, extract: "
            "  - revising_tracking_number: The tracking number of the previous W-2 being revised/corrected "
            "  - revision_reason: What was being revised (e.g., 'Incorrect CIBP size (4.5\" to 5.5\")', 'Cement quantity correction', 'Perforation depth revision') "
            "  - other_changes: true if there are additional changes beyond the revision noted in remarks, false if this is ONLY a correction filing "
            "If remarks do NOT indicate a revision, set revisions to null. "
            "Example: If remarks say 'This document is to revise the incorrect spec of 4.5 cibp, a 5.5 cibp was used and tracking no was 1572' "
            "Then extract: revisions:{revising_tracking_number:'1572', revision_reason:'Incorrect CIBP size (4.5 to 5.5 inch)', other_changes:false} "
            "Cement tops: For each casing string, extract cement_top_ft (the depth where cement reaches in the annulus). "
            "Look for phrases like 'cemented to surface', 'cement returns', 'cement top at X ft', 'cemented from X to Y ft'. "
            "If cemented to surface, set cement_top_ft to 0. If no cement data is found for a string, set cement_top_ft to null. "
            "Operator name: Extract the full operator/company name from the operator_info section, header, or certification area. "
            "Coordinates: If latitude/longitude are present anywhere (maps, headers, footers, or body), output decimal degrees in well_info.location.lat and .lon. "
            "Accept both decimal and DMS formats (e.g., 32°45'30\" N, 102°00'00\" W) and convert to signed decimal (N/E positive, S/W negative). "
            "Use keys lat/lon (not latitude/longitude). Output up to 6 decimal places. Do not round to whole degrees. If coordinates cannot be found, set both to null. "
            "Rules: numbers only (feet/inches), snake_case keys, no units in values. If a field is missing, set it to null."
        ),
        "w15": (
            "Extract W-15 (Cementing Report) data. Return JSON with: "
            "header; operator_info{name,address,operator_number}; well_info{api,district,county,field,lease,well_no,location{lat,lon}}; "
            "cementing_data:[{job:'surface|intermediate|production|plug|squeeze', interval_top_ft, interval_bottom_ft, cement_top_ft, "
            "sacks, slurries:[{slurry_no, sacks, cement_class, additives:[], slurry_density_ppg, yield_ft3_per_sk, volume_cuft, height_ft}] }]; "
            "mechanical_equipment:[{equipment_type:'CIBP|bridge_plug|packer|retainer', size_in, depth_ft, cement_top_ft, sacks, slurry_weight_ppg, cement_class, date, notes}]; "
            "cement_tops_per_string:[{string:'surface|intermediate|production', cement_top_ft, cement_returns:'full|partial|none'}]; "
            "cementing_to_squeeze:[{top_ft,bottom_ft,method}]; certifications; instructions_section. "
            "FORM STRUCTURE - W-15 has specific sections, read them carefully: "
            "1. OPERATOR INFORMATION (top of form): Extract operator name, cementer name, P-S numbers "
            "2. WELL INFORMATION (top of form): Extract API No., District No., County, Lease Name, Well No., Field Name - these fields are clearly labeled "
            "3. CASING CEMENTING DATA (Sections I, II, III): Extract casing cement jobs - Type of casing (Surface/Intermediate/Production), drilled hole size, setting depth shoe, calculated top of cement, slurry data table "
            "4. CEMENT PLUG DATA (Section IV, V, or VI): CRITICAL section for plug/CIBP extraction - has fields like 'Date', 'Size of hole or pipe (in.)', 'CIBP setting depth (ft.)', 'Cement retainer setting depth (ft.)', 'Sacks of cement used', 'Calculated top of plug (ft.)', 'Measured top of plug, if tagged (ft.)', 'Slurry weight (lbs/gal)', 'Class/type of cement' "
            "WELL INFO EXTRACTION: "
            "- Look in 'WELL INFORMATION' box at top of form for clearly labeled fields: 'API No.:', 'District No.:', 'County:', 'Lease Name:', 'Well No.:', 'Field Name:' "
            "- API format is typically 'XX-XXX-XXXXX' (e.g., '42-317-31973') "
            "- Extract exactly as shown, preserve leading zeros "
            "OPERATOR INFO EXTRACTION: "
            "- Look in 'OPERATOR INFORMATION' box at top of form for 'Operator Name:', 'Operator P-S No.:' "
            "- Also extract 'Cementer Name:' and 'Cementer P-S No.:' if present "
            "CASING CEMENTING DATA (Sections I/II/III): "
            "- Each section has checkboxes for casing type (Conductor, Surface, Intermediate, Production, Liner) "
            "- Extract: drilled hole size, depth of drilled hole, size of casing OD, casing weight/grade, setting depth shoe (or top of liner for liners), calculated top of cement, cementing date "
            "- Extract slurry table: Slurry No., No. of Sacks, Class, Additives, Volume (cu. ft.), Height (ft.) "
            "IMPORTANT: Each casing section may have MULTIPLE slurry rows (Slurry 1, Slurry 2, Slurry 3, etc.). "
            "Capture EACH slurry row as a separate entry in the slurries array. "
            "Set the top-level sacks field to the SUM of all slurry sacks for that section. "
            "- Multiple sections can be filled for different casing strings "
            "CRITICAL: Do NOT confuse 'Waiting on Cement (WOC) hours' with 'Calculated top of cement (ft.)'. "
            "WOC is a TIME value in hours (commonly 8, 12, 24), NOT a depth. "
            "cement_top_ft must be a DEPTH in feet, typically hundreds or thousands of feet. "
            "If a value appears suspiciously small (less than 50 ft) for cement_top_ft, verify it is not WOC hours. "
            "Also look for 'DV tool depth' or 'Measured/tagged cement top' as alternative depth sources. "
            "CEMENT PLUG DATA (Section IV/V/VI) - CRITICAL FOR CIBP/PLUG EXTRACTION: "
            "- This section has a table with rows for plug operations "
            "- Each row may have: Date, Size of hole/pipe, Depth to bottom of tubing, Cement retainer setting depth, CIBP setting depth, Amount of cement on top, Sacks used, Slurry volume, Calculated top of plug, Measured/tagged top, Slurry weight, Cement class/type, Perforate and squeeze Y/N "
            "- For mechanical_equipment array, create one entry per plug/CIBP: "
            "  * equipment_type: 'CIBP' if 'CIBP setting depth' has value, 'retainer' if 'Cement retainer setting depth' has value, 'bridge_plug' for bridge plugs "
            "  * size_in: from 'Size of hole or pipe (in.)' "
            "  * depth_ft: from 'CIBP setting depth (ft.)' or 'Cement retainer setting depth (ft.)' "
            "  * cement_top_ft: from 'Calculated top of plug (ft.)' or 'Measured top of plug, if tagged (ft.)' (prefer measured if available) "
            "  * sacks: from 'Sacks of cement used' "
            "  * slurry_weight_ppg: from 'Slurry weight (lbs/gal)' "
            "  * cement_class: from 'Class/type of cement' "
            "  * date: from 'Date' field "
            "  * notes: any additional text from 'Remarks' or special notations "
            "Cement tops: Extract cement_top_ft for each cementing job - the depth where cement circulated to or stopped. "
            "Also extract cement_tops_per_string for each casing string showing final cement top depth after all jobs. "
            "Look for 'cement returns', 'cement to surface', 'cement circulated to X ft', 'cement left at X ft'. "
            "If returns to surface, set cement_top_ft to 0. If no returns or unknown, set to null. "
            "Coordinates: If latitude/longitude are present anywhere (maps, headers, footers, or body), output decimal degrees in well_info.location.lat and .lon. "
            "Accept both decimal and DMS formats (e.g., 32°45'30\" N, 102°00'00\" W) and convert to signed decimal (N/E positive, S/W negative). "
            "Use keys lat/lon (not latitude/longitude). Output up to 6 decimal places. Do not round to whole degrees. If coordinates cannot be found, set both to null. "
            "Rules: numbers only (feet/inches/ppg), snake_case keys, no units in values. If a field is missing, set it to null. "
            "CRITICAL: The form has labeled sections - read the section headers and field labels carefully. Do NOT return all nulls if data is present on the form."
        ),
        "schematic": (
            "Extract schematic data. Return JSON with: header; location_info; "
            "schematic_data:{surface_shoe_ft, intermediate_shoe_ft, production_shoe_ft, production_top_ft, production_bottom_ft, "
            "casing:[{string:'surface|intermediate|production', size_in, shoe_ft, top_ft, bottom_ft}], "
            "tubing:[{size_in, top_ft, bottom_ft}] }. "
            "Rules: numbers only, snake_case keys. If a field is missing, set it to null."
        ),
        "formation_tops": (
            "Extract Formation Record. Return JSON with: header; formation_record:[{formation, top_ft, base_ft}]; "
            "h2s_flag; downhole_commingled; remarks. Rules: numbers only (ft), snake_case keys. If a field is missing, set it to null."
        ),
        "c_101": (
            "Extract NM OCD C-101 (Application for Permit to Drill) data. Return JSON with: "
            "header{date, permit_number, api_number}; "
            "operator_info{name, address, operator_number, contact_name, phone}; "
            "well_info{api, county, township, range, section, quarter, location_description, field, lease, well_no, latitude, longitude}; "
            "proposed_work{well_type, proposed_total_depth_ft, surface_location, bottom_hole_location, "
            "formation_target, purpose, spud_date}; "
            "casing_record:[{string_type:'surface|intermediate|production|conductor', size_in, weight_ppf, "
            "setting_depth_ft, hole_size_in, cement_top_ft}]; "
            "cement_record:[{string_type, sacks, slurry_weight_ppg, cement_class, additives, "
            "interval_top_ft, interval_bottom_ft}]; "
            "remarks. "
            "Coordinates: If latitude/longitude are present anywhere, output decimal degrees in well_info.latitude and .longitude. "
            "Accept both decimal and DMS formats and convert to signed decimal (N/E positive, S/W negative). "
            "Output up to 6 decimal places. If coordinates cannot be found, set both to null. "
            "Rules: numbers only (feet for depths, inches for sizes), snake_case keys, no units in numeric values. "
            "If a requested field is missing, set it to null."
        ),
        "c_103": (
            "Extract NM OCD C-103 (Notice of Intention to Plug and Abandon / Workover) data. Return JSON with: "
            "header{date, api_number, permit_number}; "
            "operator_info{name, address, operator_number, contact_name, phone}; "
            "well_info{api, county, township, range, section, quarter, field, lease, well_no, total_depth_ft}; "
            "notice_type{type:'plug_and_abandon|workover|recompletion', description}; "
            "proposed_work{description, formation_target, proposed_depth_ft, start_date}; "
            "casing_program:[{string_type:'surface|intermediate|production|conductor|liner', size_in, "
            "weight_ppf, setting_depth_ft, hole_size_in}]; "
            "cement_program:[{string_type, sacks, slurry_weight_ppg, cement_class, additives, "
            "interval_top_ft, interval_bottom_ft}]; "
            "plugging_procedure:[{step_order, plug_type:'cement_plug|bridge_plug|mechanical_plug|squeeze', "
            "depth_top_ft, depth_bottom_ft, sacks, cement_class, notes}]; "
            "remarks. "
            "Rules: numbers only (feet for depths, inches for sizes), snake_case keys, no units in numeric values. "
            "If a requested field is missing, set it to null."
        ),
        "c_105": (
            "Extract NM OCD C-105 (Completion/Recompletion Report) data. Return JSON with: "
            "header{date, api_number, permit_number, completion_date}; "
            "operator_info{name, address, operator_number}; "
            "well_info{api, county, township, range, section, quarter, field, lease, well_no, "
            "total_depth_ft, location{lat, lon}}; "
            "completion_data{completion_type:'oil|gas|dry_hole|injection|disposal|recompletion', "
            "formation_completed, well_type, elevation_ft, datum:'KB|GL|DF'}; "
            "casing_record:[{string_type:'surface|intermediate|production|conductor|liner', size_in, "
            "weight_ppf, hole_size_in, top_ft, bottom_ft, shoe_depth_ft, cement_top_ft}]; "
            "perforation_record:[{interval_top_ft, interval_bottom_ft, formation, shots_per_ft, "
            "gun_size_in, perforation_date, status:'open|squeezed|plugged'}]; "
            "production_test{test_date, duration_hours, oil_bbls_per_day, gas_mcf_per_day, "
            "water_bbls_per_day, tubing_pressure_psi, casing_pressure_psi, choke_size_in, "
            "gas_oil_ratio, formation_tested}; "
            "cement_record:[{string_type, sacks, slurry_weight_ppg, cement_class, additives, "
            "interval_top_ft, interval_bottom_ft, cement_top_ft}]; "
            "remarks. "
            "Coordinates: If latitude/longitude are present anywhere, output decimal degrees in well_info.location.lat and .lon. "
            "Accept both decimal and DMS formats and convert to signed decimal (N/E positive, S/W negative). "
            "Output up to 6 decimal places. If coordinates cannot be found, set both to null. "
            "Rules: numbers only (feet/inches/ppg/psi), snake_case keys, no units in numeric values. "
            "If a requested field is missing, set it to null."
        ),
        "sundry": (
            "Extract NM OCD Sundry Notice (Miscellaneous Filing) data. Return JSON with: "
            "header{date, api_number, sundry_number}; "
            "operator_info{name, address, operator_number, contact_name, phone}; "
            "well_info{api, county, township, range, section, quarter, field, lease, well_no}; "
            "notice_type{type, description}; "
            "description{work_description, purpose, requested_action, justification}; "
            "remarks. "
            "notice_type.type: classify as one of: 'workover', 'recompletion', 'plug_and_abandon', "
            "'change_of_operator', 'change_of_well_status', 'injection_disposal', 'surface_equipment', "
            "'administrative', 'other' based on the filing content. "
            "CRITICAL EXTRACTION RULES FOR description.work_description: "
            "1. Copy the ENTIRE narrative text VERBATIM from the 'Description of Proposed Work' or "
            "'Description of Work Performed' or 'Description of Work' field on the form. "
            "2. Include ALL depth values (measured depth, true vertical depth), formation names, "
            "casing sizes, cement volumes, packer depths, perforation intervals, and plug depths "
            "mentioned anywhere in the narrative. "
            "3. Do NOT summarize or paraphrase — copy the exact text as written on the form. "
            "4. null is ONLY acceptable when the field is genuinely blank/empty on the form. "
            "5. Also check 'Remarks', 'Additional Information', and 'Justification' sections "
            "for supplementary narrative text and append it to work_description. "
            "description.requested_action: specific approval or action being sought from the OCD. "
            "Rules: snake_case keys. If a requested field is missing, set it to null."
        ),
        "pa_procedure": (
            "Extract an Operator P&A Execution Packet (approved plugging and abandonment procedure). "
            "This document type includes a C-103F NOI form, wellbore diagrams (CURRENT and PROPOSED), "
            "and attached Conditions of Approval. Read ALL pages, including wellbore diagram tables. "
            "Return JSON with: "

            "well_header{api_number, well_name, operator, county, state, field, well_number, "
            "lease_name, section, township, range, elevation_ft, elevation_datum, "
            "surface_coordinates{lat, lon}}; "

            "notice_info{form_type, received_date, approved_date, approved_by, "
            "must_plug_by_date, ogrid_number, pool_name, lease_type}; "

            "existing_wellbore_condition{"
            "tubing_size_in, tubing_depth_ft, "
            "rod_depth_ft, "
            "rbp_depth_ft, "
            "cement_retainer_depth_ft, "
            "existing_cibp_depths:[{depth_ft, notes}], "
            "cbl_on_file{on_file, date, toc_ft}, "
            "td_ft, tvd_ft"
            "}; "

            "existing_perforations:[{depth_top_ft, depth_bottom_ft, formation_name, status}]; "

            "formation_data:[{formation_name, top_ft}]; "

            "casing_record:[{string_type:'surface|intermediate|production|liner|conductor', "
            "od_in, weight_ppf, grade, thd, top_ft, bottom_ft, num_joints, "
            "bit_size_in, sx_cmt, top_cmt_ft, comment}]; "

            "pa_procedure_steps:[{"
            "step_number, "
            "operation:'miru|pooh_tubing_rods|remove_rbp|cleanout|run_cbl|pressure_test|"
            "set_cibp|spot_plug|perf_and_squeeze|cement_retainer|casing_pull|"
            "surface_plug|topoff|cut_wellhead|set_marker|other', "
            "depth_top_ft, depth_bottom_ft, "
            "perf_depth_ft, "
            "sacks, cement_class, "
            "woc_required, "
            "pressure_test_psi, pressure_test_duration_min, "
            "formations_referenced:[{formation_name, depth_ft, reference_type:'top|shoe|perf|tol'}], "
            "description, contingency_notes"
            "}]; "

            "operational_comments:[{comment_text, category}]; "
            "contacts:[{name, role, phone, email}]. "

            "WELLBORE DIAGRAM EXTRACTION: "
            "Pages labeled 'WELLBORE DIAGRAM - CURRENT' and 'WELLBORE DIAGRAM - PROPOSED' contain tables. "
            "Extract the Casing Record table (Surface, Intermediate, Production casing rows) into casing_record. "
            "Extract the Formation table (Formation / Top columns) into formation_data. "
            "Extract tubing size and depth, rod depth, RBP depth, and CIBP depths into existing_wellbore_condition. "
            "Extract any labeled perforations (e.g., 'Perf: 8642-8691', 'Perf: 13837-14035') into existing_perforations. "

            "EXISTING WELLBORE CONDITION RULES: "
            "rbp_depth_ft: look for 'RBP @ XXXX' in early procedure steps (removal of retrievable bridge plug). "
            "cement_retainer_depth_ft: look for 'Cement Retainer @ XXXX' in cleanout steps. "
            "cbl_on_file: if a step says 'CBP on file' or 'RCBL on file', set on_file=true and extract date and TOC depth. "
            "existing_cibp_depths: list any CIBPs shown in the CURRENT wellbore diagram or referenced as pre-existing. "

            "EXISTING PERFORATIONS: "
            "Extract perforation intervals shown on the CURRENT wellbore diagram AND those referenced "
            "parenthetically in procedure steps (e.g., '(Perfs @ 13837, Morrow @ 13619)' → depth_top_ft=13837, "
            "formation_name='Morrow'). Status should be 'open' unless noted as squeezed or plugged. "

            "FORMATION DATA: "
            "Extract ALL formation tops from: (1) the Formation/Top table in the wellbore diagrams, "
            "(2) parenthetical references inside procedure step descriptions such as '(Wolfcamp @ 11616)', "
            "'(Delaware @ 5268, Shoe @ 5235)', '(Morrow @ 13619)', '(Atoka @ 12603, Strawn @ 12324)'. "
            "Each parenthetical reference is a formation top depth — do not skip them. "

            "PROCEDURE STEPS OPERATION TYPES: "
            "miru: mobilize/rig up P&A unit. "
            "pooh_tubing_rods: pull out of hole tubing, rods, pumps. "
            "remove_rbp: retrieve retrievable bridge plug. "
            "cleanout: circulate/wash/clean wellbore to a specified depth. "
            "run_cbl: run cement bond log or RCBL; capture toc_ft from 'TOC @ XXXX' in description. "
            "pressure_test: pressure test casing; capture pressure_test_psi and pressure_test_duration_min. "
            "set_cibp: set cast iron bridge plug; depth_top_ft is the CIBP set depth. "
            "spot_plug: spot cement plug; depth_top_ft and depth_bottom_ft are the plug interval. "
            "perf_and_squeeze: perforate and squeeze; perf_depth_ft is the perforation depth, "
            "depth_top_ft/depth_bottom_ft are the squeeze interval. "
            "cement_retainer: set a cement retainer tool. "
            "casing_pull: retrieve casing string. "
            "surface_plug: set final surface cement plug. "
            "topoff: top-off cement operation above an existing plug. "
            "cut_wellhead: cut and remove wellhead. "
            "set_marker: install dry hole marker. "
            "other: any operation not fitting the above. "

            "FORMATIONS REFERENCED IN STEPS: "
            "For each step, extract formations_referenced as an array. Scan the step description AND any "
            "parenthetical text for formation names with depths. "
            "reference_type: 'top'=formation top depth, 'shoe'=casing shoe at that depth, "
            "'perf'=perforation at that depth, 'tol'=top of liner. "
            "Example: 'Spot 25 sx Cl H from 13787-13550. WOC & Tag. (Perfs @ 13837, Morrow @ 13619)' → "
            "formations_referenced: [{formation_name:'Morrow', depth_ft:13619, reference_type:'top'}, "
            "{formation_name:null, depth_ft:13837, reference_type:'perf'}]. "

            "CEMENT CLASS EXTRACTION: "
            "Look for 'Cl H', 'Cl C', 'Cl A', 'Cl G', 'Class H', 'Class C' in each step. "
            "Return only the uppercase letter. 'sx Cl H' = sacks Class H. null if not mentioned for that step. "

            "COORDINATES: "
            "If lat/lon present anywhere, convert to signed decimal degrees (N/E positive, S/W negative), "
            "6 decimal places. Null if absent. "

            "Rules: numbers only (feet for depths, inches for sizes, sacks for cement), "
            "snake_case keys, no units in numeric values. If a field is missing, set to null."
        ),
        "w3a": (
            "Extract W-3A (Plugging Responsibility and Plugging Proposal) data. Return JSON with: "
            "header{api_number, well_name, operator, county, rrc_district, field, total_depth_ft}; "
            "casing_record:[{string_type:'surface|intermediate|production|liner', size_in, weight_ppf, hole_size_in, top_ft, bottom_ft, shoe_depth_ft, cement_top_ft, removed_to_depth_ft}]; "
            "perforations:[{interval_top_ft, interval_bottom_ft, formation, status:'open|perforated|squeezed|plugged', perforation_date}]; "
            "plugging_proposal:[{plug_number, depth_top_ft, depth_bottom_ft, type:'cement_plug|bridge_plug|mechanicalplug|squeeze', cement_class, sacks, volume_bbl, remarks}]; "
            "operational_steps:[{step_order, step_type, plug_number, depth_ft, wait_hours, description}]; "
            "duqw{depth_ft, formation, determination_method}; "
            "remarks. "
            "CASING RECORD CRITICAL RULES: "
            "1. Read the casing table row-by-row from 'Casing Record' section. "
            "2. For SURFACE casing: top_ft=0 (surface), bottom_ft=shoe depth (from table). "
            "3. For INTERMEDIATE casing: top_ft=0 (or shoe of previous string), bottom_ft=shoe depth from table. "
            "4. For PRODUCTION casing: top_ft=0 (or shoe of previous string), bottom_ft=shoe depth (usually TD if not deeper). "
            "5. For LINER (critical): top_ft must be 'Top of Liner' or 'Tool Setting Depth' from table (NOT 0), bottom_ft=liner shoe depth. "
            "    Example: If table shows 'Top of Liner: 6997 ft' and 'Shoe Depth: 11200 ft', then {top_ft: 6997, bottom_ft: 11200}. "
            "6. hole_size_in comes from 'Hole Size' column. "
            "7. cement_top_ft is 'Top of Cement' depth for each string. "
            "8. If liner depth is shown in a separate column, use that for top_ft - do NOT default to 0. "
            "OPERATIONAL STEPS (CRITICAL): "
            "The plugging proposal table has operational steps in TWO places: "
            "1. FIRST ROW (standalone): Contains pre-plug operational steps like 'Tag top of plug' "
            "2. PLUG ROWS: Each plug row (starting with 'Cement Plug' or 'Cement Surface Plug') has associated requirements "
            "READ THE TABLE TOP-TO-BOTTOM: "
            "- First row often shows 'Additional requirements 3 - Tag top of plug' (no plug data on that row) -> Step 1: tag_toc "
            "- Second row shows 'Cement Plug Set at 7990 to 7890...' with 'Additional requirements 6 - None' -> Step 2: plug #1 "
            "- Third row shows 'Cement Plug Set at 7047 to 6947...' with 'Additional requirements 6 - None' -> Step 3: plug #2 "
            "- When a plug row has requirements like '2 - Perforate and Squeeze, 4 - Wait X hours', create multiple steps for that plug "
            "Step numbering: Increment step_order for EACH distinct operational requirement, in table order. "
            "For plugs: plug_number corresponds to the sequence of actual plug rows (first plug row = plug #1). "
            "Step type mapping: "
            "- 'Tag top of plug' or 'Tag TOC' = step_type:'tag_toc' "
            "- 'Perforate and Circulate' = step_type:'perforate_and_circulate' "
            "- 'Perforate and Squeeze' = step_type:'perforate_and_squeeze' "
            "- 'Wait X hours and tag' = step_type:'wait_on_cement' with wait_hours:X "
            "CRITICAL: The first operational step is often 'Tag top of plug' which is step_order:1, and does NOT have a plug_number "
            "Then the first plug row creates step_order:2 with plug_number:1, and so on. "
            "Perforations: Extract from 'Record of Perforated Intervals' or 'Open Hole Intervals' showing top/bottom depths, formation name, and current status. "
            "Plugging Proposal: Extract from 'Plugging Proposal' section showing plug numbers, depths, type (cement plug vs bridge plug), cement class, and sack quantities. "
            "DUQW: Extract 'Deepest Usable Quality Water' information - the depth, formation, and how it was determined. "
            "Rules: numbers only (feet/inches/sacks), snake_case keys, no units in numeric values. If a field is missing, set it to null."
        ),
        "w1": (
            "Extract W-1 (Drilling Permit Application) data. Return JSON with: "
            "header{tracking_number, date_filed, rrc_district}; "
            "operator_info{name, address, operator_number, contact_name, phone}; "
            "well_info{api, well_no, lease, field, county, total_depth_ft, "
            "well_type:'new_drill|re_entry|field_transfer|deepening'}; "
            "permit_info{permit_number, permit_date, expiration_date, approved_depth_ft}; "
            "location{section, block, survey, abstract, latitude, longitude, "
            "distance_from_nearest_lease_line_ft, distance_from_nearest_well_ft, "
            "field_rules{spacing_acres, density_acres}}; "
            "proposed_work{proposed_total_depth_ft, proposed_formation, spud_date, estimated_completion_date}; "
            "surface_casing{surface_casing_depth_ft, gau_determination_depth_ft, cement_to_surface:bool}; "
            "drilling_contractor{name, address}; "
            "rule_37{exception_requested:bool, exception_type, justification}; "
            "attachments:[{type:'plat|gau|other', description}]. "
            "FORM STRUCTURE — TX RRC Form W-1 has these labeled sections: "
            "1. HEADER (top of form): 'RRC District No.', 'Tracking No.', 'Date Filed' — these are typically printed or stamped at the top. "
            "   The tracking number format varies: may be numeric (e.g., '006025'), alphanumeric (e.g., '8-15874'), or prefixed with '#'. Extract exactly as printed. "
            "2. OPERATOR INFORMATION: 'Operator Name', 'Operator No.' (P-5 number), address, contact info. "
            "   CRITICAL: Copy the operator name EXACTLY as printed. Old forms (pre-2000) may show historical operators like 'Stranlead Oil & Gas', 'Amoco Production Co.', etc. — extract the actual text, do NOT substitute modern operator names. "
            "3. WELL INFORMATION: 'API No.' (format XX-XXX-XXXXX), 'Lease Name', 'Well No.', 'Field Name', 'County'. "
            "   API EXTRACTION: The API number is typically in a labeled box near the top. On older forms it may be handwritten. "
            "   If you see a number matching the pattern XX-XXX-XXXXX (2 digits, dash, 3 digits, dash, 5 digits), that is the API. "
            "   On pre-1990 forms the API may be absent — set to null if not found. "
            "   well_no: Extract the well number from the 'Well No.' field. This is a number or alphanumeric (e.g., '688', 'A-1', '1-H'). "
            "4. PERMIT INFO: 'Permit No.', dates. May be stamped by RRC after approval. "
            "5. LOCATION: Section, Block, Survey/Abstract, Township/Range (if applicable). "
            "   Coordinates may appear as lat/lon in decimal or DMS format — convert to signed decimal degrees (N positive, W negative), 6 decimal places. "
            "   Spacing distances: 'Distance from nearest lease line' and 'Distance from nearest well' in feet. "
            "6. PROPOSED WORK: Total depth, target formation, spud date, estimated completion. "
            "7. SURFACE CASING: GAU determination depth, proposed surface casing depth, cement to surface requirement. "
            "8. DRILLING CONTRACTOR: Name and address of contractor. "
            "9. RULE 37: Exception request information (if applicable). "
            "LEGACY FORM VARIATIONS (pre-2000): "
            "- Forms from 1950s-1980s have different layouts — fields may be in different positions but are still labeled. "
            "- Older forms may not have API numbers (API system started ~1970). Set api to null if absent. "
            "- Handwritten entries are common — extract what you can clearly read, null for illegible values. "
            "- Some forms have the tracking number as 'Permit No.' instead of 'Tracking No.' "
            "Coordinates: decimal degrees (N/E positive, S/W negative), 6 decimal places. "
            "Rules: numbers only (feet for depths), snake_case keys, no units in numeric values. "
            "If a requested field is missing or illegible, set it to null."
        ),
        "w3": (
            "Extract W-3 (Plugging Record / Post-Plugging Report) data. Return JSON with: "
            "header{tracking_number, date_filed, rrc_district}; "
            "operator_info{name, address, operator_number, contact_name, phone}; "
            "well_info{api, well_no, lease, field, county, total_depth_ft, "
            "surface_elevation_ft, ground_elevation_ft}; "
            "plugging_summary{plug_date, plugging_commenced_date, plugging_completed_date, "
            "service_company, rig_type, freshwater_protection_depth_ft}; "
            "plug_record:[{plug_number, depth_top_ft, depth_bottom_ft, sacks, cement_class, "
            "slurry_weight_ppg, method:'dump_bail|squeeze|pump_and_plug|balanced_plug', "
            "plug_type:'cement_plug|bridge_plug|mechanical_plug|CIBP', "
            "annulus:'surface_casing|production_casing|open_hole|tubing_casing_annulus|between_strings', "
            "pipe_description, "
            "date_set, wait_on_cement_hours, tagged_top_ft}]; "
            "casing_record:[{string_type:'surface|intermediate|production|liner|conductor', "
            "size_in, weight_ppf, hole_size_in, top_ft, bottom_ft, shoe_depth_ft, cement_top_ft}]; "
            "casing_disposition{casing_left_in_hole:bool, casing_cut_depth_ft, "
            "casing_pulled:[{string_type, pulled_from_ft, pulled_to_ft}], explanation}; "
            "mud_data{mud_weight_ppg, mud_type, fluid_level_ft, circulated:bool}; "
            "surface_restoration{surface_plug_depth_top_ft, surface_plug_depth_bottom_ft, "
            "surface_plug_sacks, cut_and_cap_depth_ft, plate_welded:bool, "
            "cellar_filled:bool, pits_closed:bool}; "
            "certifications{operator_signature:bool, service_company_signature:bool, notarized:bool}; "
            "remarks. "
            "CRITICAL: Extract EVERY plug in the plug record — wells may have 6-12+ plugs. "
            "Track plug_number sequencing. If the form shows 'Plug #1', 'Plug #2', etc., preserve numbering. "
            "If method is not explicitly stated, infer from context (dump bail for open hole, squeeze for perfs). "
            "Coordinates: decimal degrees, 6 decimal places. If coordinates cannot be found, set to null. "
            "DEPTH CONVENTION: depth_top_ft is always the SHALLOWER depth (smaller number, closer to surface). "
            "depth_bottom_ft is always the DEEPER depth (larger number, further into earth). "
            "If a plug is squeezed from 4051 ft up to 2655 ft, then depth_top_ft=2655, depth_bottom_ft=4051. "
            "'Surface' as a depth means 0 ft. "
            "ANNULUS: For each plug, identify WHERE the cement was placed — which annular space or hole section. "
            "Look for references like 'between surface and production casing', 'in open hole below shoe', "
            "'tubing-casing annulus', 'surface casing annulus', etc. "
            "Set annulus to the best match. pipe_description is a free-text field for specifics like '5.5 x 10.75 ann'. "
            "Rules: numbers only (feet/ppg/sacks), snake_case keys. If a field is missing, set it to null."
        ),
        "g1": (
            "Extract G-1 (Gas Well Back Pressure Test / Status Report) data. Return JSON with: "
            "header{date_filed, rrc_district, g1_type:'initial|annual|special'}; "
            "operator_info{name, operator_number}; "
            "well_info{api, well_no, lease, field, county, total_depth_ft, formation_name}; "
            "well_status{status:'producing|shut_in|ta|injection', gas_well_classification}; "
            "test_data{test_date, test_type:'multipoint|singlepoint|calculated', "
            "shut_in_pressure_psi, flow_periods:[{"
            "duration_hours, choke_size_64ths, tubing_pressure_psi, casing_pressure_psi, "
            "gas_rate_mcfd, condensate_rate_bpd, water_rate_bpd}], "
            "bottom_hole_temperature_f, datum_depth_ft}; "
            "deliverability{aof_mcfd, four_point_c, four_point_n, "
            "authorized_rate_mcfd, effective_date}; "
            "production_data{last_month_gas_mcf, last_month_condensate_bbl, last_month_water_bbl, "
            "gor, cumulative_gas_mcf}; "
            "remarks. "
            "FORM STRUCTURE — TX RRC Form G-1 has these labeled sections: "
            "1. HEADER (top): 'RRC District No.', 'Date', type of test (Initial/Annual/Special). "
            "2. OPERATOR & WELL INFO: 'Operator', 'Operator No.', 'API No.' (format XX-XXX-XXXXX), "
            "   'Lease Name', 'Well No.', 'Field Name', 'County', 'Total Depth', 'Producing Formation'. "
            "   API EXTRACTION: Look for the labeled 'API No.' field. Format XX-XXX-XXXXX. Extract exactly as printed. "
            "3. WELL STATUS: Current well status (producing, shut-in, TA, injection), gas well classification. "
            "4. TEST DATA TABLE: This is the main data table with flow period measurements. "
            "   CRITICAL — READ THE TABLE COLUMNS CAREFULLY: "
            "   - Each row is one flow period at a specific choke size "
            "   - Columns typically: Duration (hrs), Choke Size (64ths in.), Tubing Pressure (psi), "
            "     Casing Pressure (psi), Gas Rate (Mcf/D), Condensate (BPD), Water (BPD) "
            "   - Do NOT confuse pressure columns (tubing vs casing) — they are separate columns "
            "   - Gas rate is in Mcf/D (thousand cubic feet per day), NOT cf/D "
            "   - Choke size is in 64ths of an inch (e.g., '16' means 16/64 = 1/4 inch) "
            "   - The first row may be 'shut-in' (zero flow rate, maximum pressure) — this gives shut_in_pressure_psi "
            "   - Extract ALL flow period rows as separate entries in the flow_periods array "
            "5. DELIVERABILITY: Calculated values — AOF (Absolute Open Flow), C and n coefficients, authorized rate. "
            "   These are typically computed from the test data and may appear below the test table. "
            "6. PRODUCTION DATA: Recent production volumes — monthly gas, condensate, water, GOR, cumulative. "
            "LEGACY FORM VARIATIONS: "
            "- Older forms may have handwritten entries in the test data table — read carefully. "
            "- Some forms combine multiple test types on one page. "
            "- If test data table is blank or illegible, set test_data fields to null. "
            "Rules: numbers only (psi/mcfd/bpd/ft), snake_case keys. If a field is missing or illegible, set it to null."
        ),
        "w12": (
            "Extract structured data from this Texas Railroad Commission W-12 "
            "(Completion or Recompletion Report — Gas Well) form.\n\n"
            "Return JSON with these sections:\n"
            "- header: form_type, filing_date, rrc_district, api_number\n"
            "- operator_info: operator_name, operator_number, address\n"
            "- well_info: well_number, lease_name, field_name, county, total_depth\n"
            "- gas_test_data: test_date, gas_rate_mcf, tubing_pressure, casing_pressure, "
            "choke_size, gor, btu_content, h2s_ppm, co2_percent\n"
            "- remarks: text (any additional notes or remarks)\n\n"
        ),
        "l1": (
            "Extract structured data from this Texas Railroad Commission L-1 "
            "(Notification of Lease/Lease Term) form.\n\n"
            "Return JSON with these sections:\n"
            "- header: form_type, filing_date, rrc_district, api_number\n"
            "- operator_info: operator_name, operator_number, address\n"
            "- well_info: well_number, lease_name, field_name, county\n"
            "- lease_info: lease_number, lease_date, lease_term, lessor, lessee, "
            "acreage, legal_description\n"
            "- remarks: text (any additional notes or remarks)\n\n"
        ),
        "p14": (
            "Extract structured data from this Texas Railroad Commission P-14 "
            "(Plugging Record) form.\n\n"
            "Return JSON with these sections:\n"
            "- header: form_type, filing_date, rrc_district, api_number\n"
            "- operator_info: operator_name, operator_number, address\n"
            "- well_info: well_number, lease_name, field_name, county, total_depth\n"
            "- pressure_test_data: test_date, test_type, initial_pressure_psi, "
            "final_pressure_psi, duration_minutes, result\n"
            "- remarks: text (any additional notes or remarks)\n\n"
        ),
        "swr10": (
            "Extract structured data from this Texas Railroad Commission SWR-10 "
            "(Statewide Rule 10 — Well Spacing Exception) form.\n\n"
            "Return JSON with these sections:\n"
            "- header: form_type, filing_date, rrc_district, api_number\n"
            "- operator_info: operator_name, operator_number, address\n"
            "- well_info: well_number, lease_name, field_name, county\n"
            "- exception_info: exception_type, requested_spacing, standard_spacing, "
            "justification, affected_leases, hearing_date\n"
            "- remarks: text (any additional notes or remarks)\n\n"
        ),
        "swr13": (
            "Extract structured data from this Texas Railroad Commission SWR-13 "
            "(Statewide Rule 13 — Casing, Cementing, Drilling, and Completion "
            "Requirements Exception) form.\n\n"
            "Return JSON with these sections:\n"
            "- header: form_type, filing_date, rrc_district, api_number\n"
            "- operator_info: operator_name, operator_number, address\n"
            "- well_info: well_number, lease_name, field_name, county\n"
            "- exception_info: exception_type, rule_section, current_requirement, "
            "requested_exception, justification, well_conditions\n"
            "- remarks: text (any additional notes or remarks)\n\n"
        ),
    }
    preamble = (
        "ACCURACY RULES (apply to ALL fields): "
        "1. If you cannot clearly read a value from the scanned form, return null. NEVER guess or fabricate values. "
        "2. API number format on TX RRC forms is XX-XXX-XXXXX (e.g., 42-003-05770). Extract exactly as printed, preserving dashes and leading zeros. If the API is illegible or missing, set to null. "
        "3. Operator names must be copied EXACTLY as printed on the form — do not invent or guess operator names. "
        "4. Numeric field validation: weight_ppf is typically 1-100, size_in is typically 2-20, sacks is typically 1-500 for a single plug. If a value seems implausible, re-read the column headers to confirm you are reading the correct column. "
        "5. Do NOT confuse table columns — depths, weights, and sizes are in separate columns. If a table has multiple columns, read the column header for each value. "
        "6. Dates should be in YYYY-MM-DD format when possible. If only partial date is readable, include what you can read. "
    )
    prompt = preamble + base[prompt_key]
    if tags:
        focus = ", ".join(tags)
        prompt += (
            f" FOCUS AREAS: Pay special attention to extracting data related to: {focus}. "
            "These are the key data categories expected in this document."
        )
    return prompt


def _ensure_sections(doc_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    req = SUPPORTED_TYPES[doc_type]["required_sections"]
    out = dict(data)
    for key in req:
        out.setdefault(key, {} if key not in ("casing_record", "tubing_record", "formation_record", "schematic_data") else [])
    return out


def extract_json_from_pdf(file_path: Path, doc_type: str, retries: int = 2, w2_data: Optional[Dict] = None, tags: Optional[List[str]] = None) -> ExtractionResult:
    """
    Send PDF to OpenAI and retrieve structured JSON per schema; retry on malformed JSON up to retries.
    
    For schematic documents or image files, uses Vision API instead of text extraction.
    """
    # Check if this is an image file (schematic/wellbore diagram)
    image_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff'}
    is_image = file_path.suffix.lower() in image_extensions
    
    # Route images or explicit schematic doc types to Vision API
    if doc_type == 'schematic' or doc_type == 'wellbore_schematic' or is_image:
        try:
            from .schematic_extraction import extract_schematic_from_image
            # Force doc_type to schematic for images
            actual_doc_type = 'schematic' if is_image else doc_type
            logger.info(f"Routing image file {file_path.name} to Vision API for schematic extraction")
            data = extract_schematic_from_image(file_path, w2_data=w2_data)
            return ExtractionResult(
                document_type=actual_doc_type,
                json_data=data,
                model_tag=DEFAULT_CHAT_MODEL,
                errors=[]
            )
        except Exception as e:
            logger.error(f"Vision API extraction failed for {file_path.name}: {str(e)}")
            return ExtractionResult(
                document_type=doc_type,
                json_data={},
                model_tag=DEFAULT_CHAT_MODEL,
                errors=[str(e)]
            )
    
    # --- Quota cooldown pre-check ------------------------------------------------
    # If a previous call hit insufficient_quota, fail fast without calling OpenAI.
    from apps.public_core.services.openai_config import (
        OpenAIQuotaExceededError,
        is_quota_exceeded,
        set_quota_exceeded,
    )
    from math import ceil as _ceil
    _quota_active, _quota_remaining = is_quota_exceeded()
    if _quota_active:
        raise OpenAIQuotaExceededError(
            f"OpenAI quota exceeded; retry in ~{_ceil(_quota_remaining / 60)} min"
        )
    # --------------------------------------------------------------------------

    # Standard text-based extraction
    client = get_openai_client(operation="document_extraction")
    model = MODEL_PRIMARY
    prompt = _load_prompt(SUPPORTED_TYPES[doc_type]["prompt_key"], tags=tags) + " Return only valid JSON."
    last_err = None

    # Pre-extract textual context to aid model grounding
    context_text = _extract_pdf_text(file_path, max_chars=20000)

    for attempt in range(retries + 1):
        logger.info("extract_json_from_pdf: attempt=%d file=%s type=%s model=%s", attempt + 1, file_path, doc_type, model)
        try:
            # Upload the PDF and call Responses API with file input (supports input_file_id)
            fobj = client.files.create(file=open(str(file_path), "rb"), purpose="assistants")
            logger.info("extract_json_from_pdf: uploaded file_id=%s size=%s", getattr(fobj, 'id', ''), os.path.getsize(file_path))
            # Debug: log SDK version/path and Responses signature in the same process
            try:
                import openai  # type: ignore
                logger.warning("openai runtime: version=%s path=%s", getattr(openai, "__version__", "?"), getattr(openai, "__file__", "?"))
                try:
                    from openai.resources.responses import Responses as _Responses
                    logger.warning("responses.create signature=%s", inspect.signature(_Responses.create))
                    try:
                        logger.warning("responses.create varnames=%s", getattr(_Responses.create, "__code__", None) and _Responses.create.__code__.co_varnames)
                    except Exception:
                        pass
                except Exception as e_sig:
                    logger.warning("responses.create signature introspection failed: %s", e_sig)
            except Exception:
                pass

            # Request JSON output using Responses API (SDK >= 1.6.0)
            inputs = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        # Provide extracted text to improve retrieval of numeric values
                        *( [{"type": "input_text", "text": context_text[:20000]}] if context_text else [] ),
                        {"type": "input_file", "file_id": getattr(fobj, "id", "")},
                    ],
                }
            ]
            resp = client.responses.create(
                model=model,
                input=inputs,
                text={"format": {"type": "json_object"}},
                max_output_tokens=4000,
                temperature=0,
            )
            tokens_used = getattr(getattr(resp, 'usage', None), 'total_tokens', 0) or 0

            # Robustly extract text from Responses API
            # Extract text from Responses API output
            # Extract text from Responses API output (preferred shape in SDK 1.x)
            content = ""
            if hasattr(resp, "output") and resp.output:
                try:
                    content = "".join(
                        (
                            block.get("text", "")
                            if isinstance(block, dict)
                            else (getattr(block, "text", "") or "")
                        )
                        for item in resp.output
                        for block in (
                            item.get("content", []) if isinstance(item, dict) else (getattr(item, "content", []) or [])
                        )
                        if (
                            block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
                        ) == "output_text"
                    )
                except Exception as parse_e:
                    logger.warning("extract_json_from_pdf: failed to parse Responses output blocks: %s", parse_e)
            if not content and hasattr(resp, "output_text"):
                content = resp.output_text or ""

            # Debug: log a compact raw view once per attempt
            try:
                dump = None
                if hasattr(resp, "model_dump_json"):
                    dump = resp.model_dump_json(indent=2)  # type: ignore[attr-defined]
                elif hasattr(resp, "model_dump"):
                    dump = json.dumps(getattr(resp, "model_dump")(), indent=2)  # type: ignore[misc]
                else:
                    dump = repr(resp)
                if dump:
                    logger.debug("extract_json_from_pdf: raw_response_snippet=%s", str(dump)[:2000])
            except Exception:
                pass
            if not content or content.strip() in ("{}", "[]", "null", "None", ""):
                raise ValueError("EMPTY_JSON_RESPONSE")
            logger.info("extract_json_from_pdf: received json length=%d", len(content))
            try:
                logger.debug("extract_json_from_pdf: content_snippet=%s", content[:500])
            except Exception:
                pass
            data = json.loads(content)
            data = _ensure_sections(doc_type, data)
            # Post-extraction validation
            from apps.public_core.services.extraction_validator import validate_extracted_data
            data = validate_extracted_data(doc_type, data)
            # Post-process GAU: if lat/lon missing, parse from context text (decimal or DMS) and inject
            if doc_type == "gau":
                try:
                    wi = data.setdefault("well_info", {})
                    loc = wi.setdefault("location", {})
                    lat = loc.get("lat") or loc.get("latitude")
                    lon = loc.get("lon") or loc.get("longitude")
                    def _parse_from_text(txt: str) -> Tuple[Optional[float], Optional[float]]:
                        import re, math
                        # 1) Try decimal degrees: lat, lon
                        dec = re.findall(r"([+-]?\d{1,2}\.\d{3,})\s*,?\s*([+-]?\d{3}\.\d{3,})", txt)
                        for a,b in dec:
                            try:
                                la = float(a); lo = float(b)
                                if -90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0:
                                    return round(la, 6), round(lo, 6)
                            except Exception:
                                pass
                        # 2) Try DMS with N/S and E/W
                        dms_lat = re.search(r"(\d{1,2})[°\s](\d{1,2})['’\s](\d{1,2}(?:\.\d+)?)\s*([NSns])", txt)
                        dms_lon = re.search(r"(\d{1,3})[°\s](\d{1,2})['’\s](\d{1,2}(?:\.\d+)?)\s*([EWew])", txt)
                        def to_dec(d, m, s, hemi):
                            val = float(d) + float(m)/60.0 + float(s)/3600.0
                            if hemi.upper() in ("S","W"):
                                val = -val
                            return round(val, 6)
                        if dms_lat and dms_lon:
                            la = to_dec(dms_lat.group(1), dms_lat.group(2), dms_lat.group(3), dms_lat.group(4))
                            lo = to_dec(dms_lon.group(1), dms_lon.group(2), dms_lon.group(3), dms_lon.group(4))
                            if -90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0:
                                return la, lo
                        return None, None
                    if (lat is None or lon is None) and context_text:
                        la, lo = _parse_from_text(context_text)
                        if la is not None and lo is not None:
                            loc["lat"], loc["lon"] = la, lo
                except Exception:
                    logger.exception("GAU post-processing for lat/lon failed")
            # Save extracted JSON to tmp/extractions for inspection
            try:
                tmp_dir = Path(settings.BASE_DIR) / 'tmp' / 'extractions'
                tmp_dir.mkdir(parents=True, exist_ok=True)
                out_name = f"{file_path.stem}_{doc_type}.json"
                out_path = tmp_dir / out_name
                with open(out_path, 'w', encoding='utf-8') as f_out:
                    json.dump(data, f_out, ensure_ascii=False, indent=2)
                logger.info("extract_json_from_pdf: saved output -> %s", out_path)
            except Exception:
                logger.exception("extract_json_from_pdf: failed to save output JSON")
            return ExtractionResult(document_type=doc_type, json_data=data, model_tag=model, errors=[], raw_text=context_text, tokens_used=tokens_used)
        except Exception as e:  # pragma: no cover
            # Propagate quota errors immediately — never retry, never sleep.
            if isinstance(e, OpenAIQuotaExceededError):
                raise

            # Detect insufficient_quota from OpenAI's RateLimitError.
            import openai as _openai_module
            if isinstance(e, _openai_module.RateLimitError):
                _is_quota = (
                    getattr(e, "code", None) == "insufficient_quota"
                    or "insufficient_quota" in str(getattr(e, "body", "") or "")
                    or "insufficient_quota" in str(e)
                )
                if _is_quota:
                    set_quota_exceeded()
                    raise OpenAIQuotaExceededError(
                        f"OpenAI quota exceeded; retry in ~5 min"
                    ) from e

            last_err = str(e)
            logger.warning("extract_json_from_pdf: error attempt=%d err=%s", attempt + 1, last_err)
            time.sleep(0.5 if attempt == 0 else 2.0)
            continue

    logger.error("extract_json_from_pdf: failed after retries err=%s", last_err)
    return ExtractionResult(document_type=doc_type, json_data={}, model_tag=model, errors=[last_err or "unknown_error"], raw_text=context_text)



# Sections that are near-identical across filings for the same well and
# pollute vector search results with noise.  Skip these for sundry documents.
_SUNDRY_SKIP_SECTIONS = frozenset({"header", "operator_info", "well_info"})


def iter_json_sections_for_embedding(doc_type: str, data: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Yield (section_name, section_text) pairs for vectorization.

    Skips sections that are None, empty, or contain only null values
    to avoid creating junk vectors that pollute search results.

    For sundry documents, also skips header/operator_info/well_info sections
    which are nearly identical across filings and displace useful content
    in top-k retrieval.
    """
    sections = SUPPORTED_TYPES.get(doc_type, {}).get("required_sections", [])
    is_sundry = doc_type == "sundry"
    out: List[Tuple[str, str]] = []
    for sec in sections:
        # Skip noise sections for sundry documents
        if is_sundry and sec in _SUNDRY_SKIP_SECTIONS:
            continue
        val = data.get(sec)
        if val is None:
            continue
        if isinstance(val, (dict, list)):
            # Skip dicts/lists where all values are None
            if isinstance(val, dict) and all(v is None for v in val.values()):
                continue
            if isinstance(val, list) and len(val) == 0:
                continue
            text = json.dumps(val, ensure_ascii=False)
        else:
            text = str(val)
        # Skip trivial text
        if not text or text in ("None", "null", "", "{}", "[]"):
            continue
        # Convert JSON blobs to readable prose before embedding
        if text.startswith("{") or text.startswith("["):
            text = json_to_prose(sec, text)
        # Chunk long sections so each vector covers a focused slice
        if len(text) > 500:
            for i, chunk in enumerate(chunk_text(text, 500, 100)):
                out.append((f"{sec}__chunk_{i}", chunk))
        else:
            out.append((sec, text))
    # Emit raw PDF text as fallback vector for retrieval
    raw_text = data.get("_raw_text")
    if raw_text and isinstance(raw_text, str) and len(raw_text.strip()) > 50:
        if len(raw_text) > 800:
            for i, chunk in enumerate(chunk_text(raw_text, 800, 200)):
                out.append((f"_raw_text__chunk_{i}", chunk))
        else:
            out.append(("_raw_text", raw_text))
    return out


# --- Vectorization helpers ---
def _embed_texts(texts: List[str]) -> List[List[float]]:  # pragma: no cover
    if not texts:
        return []
    client = get_openai_client(operation="document_extraction")
    resp = client.embeddings.create(model=MODEL_EMBEDDING, input=texts)
    # SDK returns data list with .embedding per item
    vectors: List[List[float]] = []
    try:
        for item in getattr(resp, "data", []) or []:
            vec = getattr(item, "embedding", None)
            if vec:
                vectors.append(list(vec))
    except Exception:
        logger.exception("_embed_texts: failed to parse embeddings response")
    return vectors


def vectorize_extracted_document(ed_obj) -> int:  # pragma: no cover
    """Create DocumentVector rows for an ExtractedDocument.
    Returns number of vectors created.
    """
    try:
        from apps.public_core.models.document_vector import DocumentVector
    except Exception as e:
        logger.exception("vectorize_extracted_document: import failed")
        return 0
    try:
        doc_type = getattr(ed_obj, "document_type", None) or ""
        data = getattr(ed_obj, "json_data", None) or {}
        if not isinstance(data, dict):
            return 0
        sections = iter_json_sections_for_embedding(doc_type, data)
        if not sections:
            return 0
        texts = [s for _, s in sections]
        embeddings = _embed_texts(texts)
        
        # Get well for enriched metadata (if available)
        well = getattr(ed_obj, "well", None)
        
        # Extract district from JSON (commonly in well_info section)
        well_info = data.get("well_info", {}) if isinstance(data, dict) else {}
        district = well_info.get("district") or well_info.get("rrc_district")
        
        # Get tenant attribution (Phase 1: uploaded_by_tenant)
        uploaded_by_tenant = getattr(ed_obj, "uploaded_by_tenant", None)
        tenant_id_str = str(uploaded_by_tenant) if uploaded_by_tenant else None
        
        created = 0
        for (section_name, section_text), emb in zip(sections, embeddings):
            try:
                DocumentVector.objects.create(
                    well=well,
                    file_name=(getattr(ed_obj, "source_path", None) or ""),
                    document_type=doc_type,
                    section_name=section_name,
                    section_text=section_text,
                    embedding=emb,
                    metadata={
                        # Existing fields
                        "ed_id": str(getattr(ed_obj, "id", "")),
                        "api_number": getattr(ed_obj, "api_number", ""),
                        "model_tag": getattr(ed_obj, "model_tag", ""),
                        
                        # Roadmap-aligned fields (from Consolidated-AI-Roadmap.md line 46)
                        # Tenant attribution (populated from ExtractedDocument.uploaded_by_tenant)
                        "tenant_id": tenant_id_str,  # None for RRC-sourced, UUID string for tenant uploads
                        
                        # Well context for retrieval filtering
                        "operator": getattr(well, "operator_name", None) if well else None,
                        "district": district,
                        "county": getattr(well, "county", None) if well else None,
                        "field": getattr(well, "field_name", None) if well else None,
                        "lat": float(well.lat) if (well and well.lat) else None,
                        "lon": float(well.lon) if (well and well.lon) else None,
                        
                        # Plan-level metadata (populated later when plans are generated)
                        "step_types": None,  # Future: list of step types from plan
                        "materials": None,  # Future: materials summary from plan
                        "approval_status": None,  # Future: approved/rejected/pending
                        "overlay_id": None,  # Future: canonical facts overlay ID
                        "kernel_version": None,  # Future: kernel version used
                    },
                )
                created += 1
            except Exception:
                logger.exception("vectorize_extracted_document: failed to create vector row")
        return created
    except Exception:
        logger.exception("vectorize_extracted_document: failure")
        return 0

