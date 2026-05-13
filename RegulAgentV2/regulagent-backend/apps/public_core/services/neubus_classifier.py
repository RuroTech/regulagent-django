"""
Neubus Document Page Classifier (Pass 1).

Classifies each page of a Neubus PDF into RRC form types using OpenAI Vision.
Groups consecutive pages into form segments for extraction.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF
from PIL import Image

from apps.public_core.services.openai_config import get_openai_client

logger = logging.getLogger(__name__)

# Use cheap model for classification
CLASSIFIER_MODEL = "gpt-4o-mini"

CLASSIFICATION_PROMPT = """Identify the RRC (Railroad Commission of Texas) form on this page. Return JSON only:
{
  "form_type": "W-1" | "W-2" | "W-3" | "W-3a" | "W-15" | "G-1" | "Other",
  "is_continuation": true/false,
  "confidence": "high" | "medium" | "low",
  "evidence": "brief explanation of classification"
}

Rules:
- A new form has a printed header with form number and "Railroad Commission of Texas"
- A continuation page has no header, says "Page 2 of 2", or continues a data table from the previous page
- W-3 is the Plugging Record (post-plugging filing). W-3a is the Plugging Proposal (pre-plugging).
- W-3 and W-3a are SEPARATE forms — do not confuse them
- If unsure, set confidence to "low" and explain in evidence
- Return ONLY the JSON object, no other text"""


@dataclass
class PageClassification:
    """Classification result for a single page."""
    page: int
    form_type: str
    is_continuation: bool
    confidence: str
    evidence: str


@dataclass
class FormGroup:
    """A group of consecutive pages that form a single RRC document."""
    form_type: str
    pages: List[int] = field(default_factory=list)
    confidence: str = "high"

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def first_page(self) -> int:
        return self.pages[0] if self.pages else 0


def _page_to_image_bytes(pdf_path: Path, page_num: int, dpi: int = 150) -> bytes:
    """Convert a PDF page to PNG bytes for vision API."""
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[page_num]
        # Render at specified DPI
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        return pix.tobytes("png")
    finally:
        doc.close()


def _classify_single_page(
    image_bytes: bytes,
    page_num: int,
    client=None,
) -> PageClassification:
    """Classify a single page using OpenAI Vision."""
    import base64
    import json

    if client is None:
        client = get_openai_client(operation="neubus_classification")

    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    try:
        resp = client.chat.completions.create(
            model=CLASSIFIER_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": CLASSIFICATION_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{b64_image}",
                                "detail": "low",  # cheaper, sufficient for form headers
                            },
                        },
                    ],
                }
            ],
            temperature=0,
            max_tokens=200,
        )

        raw = (resp.choices[0].message.content or "").strip()
        # Parse JSON from response (handle markdown code blocks)
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        data = json.loads(raw)

        # Normalize form_type
        form_type = _normalize_form_type(data.get("form_type", "Other"))

        return PageClassification(
            page=page_num,
            form_type=form_type,
            is_continuation=data.get("is_continuation", False),
            confidence=data.get("confidence", "low"),
            evidence=data.get("evidence", ""),
        )

    except Exception as e:
        logger.warning(f"Classification failed for page {page_num}: {e}")
        return PageClassification(
            page=page_num,
            form_type="Other",
            is_continuation=False,
            confidence="low",
            evidence=f"Classification error: {e}",
        )


def _normalize_form_type(raw: str) -> str:
    """Normalize form type strings to consistent keys."""
    raw = raw.strip().upper().replace(" ", "").replace("-", "")
    mapping = {
        "W1": "W-1",
        "W2": "W-2",
        "W3": "W-3",
        "W3A": "W-3a",
        "W15": "W-15",
        "G1": "G-1",
    }
    return mapping.get(raw, raw if raw != "OTHER" else "Other")


def classify_document_pages(
    pdf_path: Path,
    neubus_doc=None,
) -> List[PageClassification]:
    """
    Classify all pages of a Neubus PDF document.

    .. deprecated:: Use classify_document_pages_v2() for text-first classification
       with ~80% fewer Vision API calls.

    Args:
        pdf_path: Path to the PDF file
        neubus_doc: Optional NeubusDocument model instance to update

    Returns:
        List of PageClassification objects, one per page
    """
    logger.warning(
        "classify_document_pages() is deprecated. "
        "Use classify_document_pages_v2() for text-first classification."
    )
    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    doc.close()

    if total_pages == 0:
        logger.warning(f"Empty PDF: {pdf_path}")
        return []

    logger.info(f"Classifying {total_pages} pages in {pdf_path.name}")

    # Update status if model instance provided
    if neubus_doc:
        neubus_doc.classification_status = "processing"
        neubus_doc.pages = total_pages
        neubus_doc.save(update_fields=["classification_status", "pages"])

    client = get_openai_client(operation="neubus_classification")
    classifications = []

    for page_num in range(total_pages):
        image_bytes = _page_to_image_bytes(pdf_path, page_num)
        classification = _classify_single_page(image_bytes, page_num, client=client)
        classifications.append(classification)
        logger.debug(
            f"  Page {page_num + 1}/{total_pages}: {classification.form_type} "
            f"(continuation={classification.is_continuation}, "
            f"confidence={classification.confidence})"
        )

    # Update NeubusDocument with classification results
    if neubus_doc:
        form_types_by_page = {}
        for c in classifications:
            if c.form_type != "Other":
                form_types_by_page.setdefault(c.form_type, []).append(c.page + 1)  # 1-indexed

        neubus_doc.form_types_by_page = form_types_by_page
        neubus_doc.classification_status = "complete"
        neubus_doc.save(update_fields=["form_types_by_page", "classification_status"])

    return classifications


def group_pages_into_forms(classifications: List[PageClassification]) -> List[FormGroup]:
    """
    Group classified pages into form segments.

    Rules:
    - A new form starts when is_continuation=False
    - Continuation pages are appended to the current form group
    - W-3 and W-3a are INDEPENDENT form groups — never merged
    - Multiple forms of the same type create separate groups (e.g., two W-3 plugging events)
    - "Other" pages are skipped

    Returns:
        List of FormGroup objects in page order
    """
    groups: List[FormGroup] = []
    current_group: Optional[FormGroup] = None

    for c in classifications:
        if c.form_type == "Other":
            # End current group if any
            current_group = None
            continue

        if c.is_continuation and current_group is not None:
            # Continuation of current form — verify type matches
            if c.form_type == current_group.form_type:
                current_group.pages.append(c.page)
                # Downgrade confidence if continuation has lower confidence
                if c.confidence == "low":
                    current_group.confidence = "low"
                continue
            else:
                # Type mismatch on continuation — start new group
                logger.warning(
                    f"Continuation type mismatch on page {c.page}: "
                    f"expected {current_group.form_type}, got {c.form_type}. Starting new group."
                )

        # Start a new form group
        current_group = FormGroup(
            form_type=c.form_type,
            pages=[c.page],
            confidence=c.confidence,
        )
        groups.append(current_group)

    logger.info(
        f"Grouped {len(classifications)} pages into {len(groups)} form groups: "
        f"{[(g.form_type, g.pages) for g in groups]}"
    )

    return groups


# ──────────────────────────────────────────────────────────────
# Triage: cheap first-2-page scan to extract API number per PDF
# ──────────────────────────────────────────────────────────────

TRIAGE_PROMPT = """You are analyzing a page from a Texas Railroad Commission (RRC) document to find the API well number.

IMPORTANT: If you cannot clearly read an API number on this page, return null for api_number. Do NOT guess or hallucinate a number.

Where API numbers appear on RRC forms:
- Top header area, labeled "API No.", "API Number", or "API #"
- Format: XX-XXX-XXXXX (state-county-unique), e.g., 42-003-35663
- Sometimes shown without dashes: 4200335663
- Sometimes includes a 2-digit suffix: 42-003-35663-00
- The state code for Texas is always "42"

Common misidentifications to AVOID:
- Lease numbers (usually 5-6 digits, no state prefix)
- Permit numbers (labeled "Permit No.")
- Tracking/filing numbers
- District numbers (1-2 characters like "7C" or "8A")

Return JSON only:
{
  "api_number": "42-003-35663" or null,
  "well_number": "1" or null,
  "confidence": "high" | "medium" | "low" | "unknown",
  "api_format_note": "brief note on where/how the API was found, or why null"
}

Return ONLY the JSON object, no other text."""


def triage_document(pdf_path: Path, max_pages: int = 10) -> dict:
    """
    Iteratively scan pages of a PDF to extract API number with confidence.

    Scans up to max_pages. Early-exits on high confidence or convergence
    (2+ pages agree on the same API).

    Returns:
        {"api_number": str|None, "well_number": str|None, "confidence": str,
         "pages_scanned": int, "api_format_note": str}
    """
    import base64
    import json

    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    doc.close()

    if total_pages == 0:
        return {"api_number": None, "well_number": None, "confidence": "low",
                "pages_scanned": 0, "api_format_note": "empty PDF"}

    client = get_openai_client(operation="neubus_triage")
    pages_to_scan = min(max_pages, total_pages)

    candidates = []  # list of dicts: {"api_number": str, "well_number": str|None, "confidence": str, "api_format_note": str}

    for page_num in range(pages_to_scan):
        image_bytes = _page_to_image_bytes(pdf_path, page_num)
        b64_image = base64.b64encode(image_bytes).decode("utf-8")

        try:
            resp = client.chat.completions.create(
                model=CLASSIFIER_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": TRIAGE_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{b64_image}",
                                    "detail": "low",
                                },
                            },
                        ],
                    }
                ],
                temperature=0,
                max_tokens=200,
            )

            raw = (resp.choices[0].message.content or "").strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            data = json.loads(raw)
            api_num = data.get("api_number")

            if api_num:
                # Normalize: strip dashes/spaces, ensure digits
                clean = api_num.replace("-", "").replace(" ", "")
                if len(clean) >= 5:  # At minimum county+unique portion
                    confidence = data.get("confidence", "medium")
                    candidate = {
                        "api_number": clean,
                        "well_number": data.get("well_number"),
                        "confidence": confidence,
                        "pages_scanned": page_num + 1,
                        "api_format_note": data.get("api_format_note", ""),
                    }

                    # Early exit on high confidence
                    if confidence == "high":
                        logger.info(
                            f"[Triage] {pdf_path.name}: high confidence API {clean} on page {page_num + 1}"
                        )
                        return candidate

                    candidates.append(candidate)

                    # Convergence check: 2+ pages agree on same API
                    api_counts = {}
                    for c in candidates:
                        api_counts[c["api_number"]] = api_counts.get(c["api_number"], 0) + 1

                    for api_val, count in api_counts.items():
                        if count >= 2:
                            logger.info(
                                f"[Triage] {pdf_path.name}: convergence on API {api_val} "
                                f"({count} pages agree, scanned {page_num + 1} pages)"
                            )
                            # Return the first candidate with this API, upgraded to high confidence
                            for c in candidates:
                                if c["api_number"] == api_val:
                                    c["confidence"] = "high"
                                    c["pages_scanned"] = page_num + 1
                                    return c

        except Exception as e:
            logger.warning(f"Triage failed for {pdf_path.name} page {page_num}: {e}")

    # Exhausted all pages: return best candidate or None
    if candidates:
        # Prefer medium over low confidence
        candidates.sort(key=lambda c: {"high": 0, "medium": 1, "low": 2}.get(c["confidence"], 3))
        best = candidates[0]
        best["pages_scanned"] = pages_to_scan
        logger.info(
            f"[Triage] {pdf_path.name}: best candidate API {best['api_number']} "
            f"(confidence={best['confidence']}, scanned {pages_to_scan} pages)"
        )
        return best

    return {
        "api_number": None,
        "well_number": None,
        "confidence": "low",
        "pages_scanned": pages_to_scan,
        "api_format_note": f"No API found after scanning {pages_to_scan} pages",
    }


def triage_lease_documents(lease) -> Dict[str, list]:
    """
    Triage all un-triaged documents in a lease using ThreadPoolExecutor.

    - Reads first 2 pages of each PDF to extract API number
    - Creates/updates WellRegistry for each unique API discovered
    - Sets NeubusDocument.api and WellRegistry.lease_id

    Args:
        lease: NeubusLease instance

    Returns:
        Dict mapping api_number → list of NeubusDocument instances.
        Documents with no API found are under key "unknown".
    """
    import re
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from apps.public_core.models import WellRegistry
    from apps.public_core.models.neubus_lease import NeubusDocument

    docs = list(lease.documents.filter(api="").exclude(triage_confidence="unidentified"))  # Only un-triaged docs
    if not docs:
        # All docs already triaged — rebuild mapping from DB
        all_docs = list(lease.documents.all())
        result = {}
        for d in all_docs:
            key = d.api or "unknown"
            result.setdefault(key, []).append(d)
        return result

    logger.info(f"[Triage] Starting triage for {len(docs)} docs in lease {lease.lease_id}")

    # Run triage in parallel
    triage_results = {}  # doc.id → triage dict

    def _triage_one(doc):
        pdf_path = Path(doc.local_path)
        if not pdf_path.exists():
            return doc.id, {"api_number": None, "well_number": None, "confidence": "low"}
        return doc.id, triage_document(pdf_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_triage_one, doc): doc for doc in docs}
        for future in as_completed(futures):
            try:
                doc_id, result = future.result()
                triage_results[doc_id] = result
            except Exception as e:
                doc = futures[future]
                logger.warning(f"[Triage] Failed for {doc.neubus_filename}: {e}")
                triage_results[doc.id] = {"api_number": None, "well_number": None, "confidence": "low"}

    # Group by API number and update DB records
    api_to_docs: Dict[str, list] = {}

    for doc in docs:
        triage = triage_results.get(doc.id, {})
        api_num = triage.get("api_number")
        well_num = triage.get("well_number")

        if api_num:
            # Normalize to 14-digit API if we have enough info
            clean = re.sub(r"\D+", "", api_num)
            if len(clean) < 10:
                # Pad with TX state code prefix if missing
                clean = "42" + clean.zfill(8)
            if len(clean) < 14:
                clean = clean.ljust(14, "0")

            doc.api = clean
            doc.well_number = well_num or ""
            doc.triage_confidence = triage.get("confidence", "medium")
            doc.triage_pages_scanned = triage.get("pages_scanned", 0)
            doc.save(update_fields=["api", "well_number", "triage_confidence", "triage_pages_scanned"])

            # Create/update WellRegistry
            well, created = WellRegistry.objects.get_or_create(
                api14=clean,
                defaults={"state": "TX", "lease_id": lease.lease_id, "data_status": "cold_storage"},
            )
            if created:
                logger.info(
                    f"[Triage] Created cold-storage WellRegistry for API {clean} "
                    f"(lease {lease.lease_id})"
                )
            # Always ensure lease_id is set
            if not well.lease_id:
                well.lease_id = lease.lease_id
                well.save(update_fields=["lease_id"])

            api_to_docs.setdefault(clean, []).append(doc)
        else:
            doc.triage_confidence = "unidentified"
            doc.triage_pages_scanned = triage.get("pages_scanned", 0)
            doc.save(update_fields=["triage_confidence", "triage_pages_scanned"])
            api_to_docs.setdefault("unknown", []).append(doc)

    # Also include already-triaged docs in the mapping
    already_triaged = lease.documents.exclude(api="")
    for d in already_triaged:
        if d.id not in triage_results:  # Don't double-count
            api_to_docs.setdefault(d.api, []).append(d)

    # Log per-API breakdown
    api_breakdown = ", ".join(
        f"API {k} → {len(v)} docs" for k, v in sorted(api_to_docs.items())
    )
    logger.info(f"[Triage] Lease {lease.lease_id}: {api_breakdown}")

    return api_to_docs


def classify_document_pages_v2(
    pdf_path: Path,
    state: str = "TX",
    neubus_doc=None,
    client=None,
) -> List[FormGroup]:
    """
    Text-first classification with Vision fallback.
    Delegates to document_segmenter.segment_document().

    Returns FormGroup objects for backward compatibility with extract_form_groups().
    """
    from apps.public_core.services.document_segmenter import segment_document, persist_segments

    segments = segment_document(pdf_path, state=state, client=client)

    # Update NeubusDocument with classification results
    if neubus_doc:
        form_types_by_page = {}
        for seg in segments:
            for page in range(seg.page_start, seg.page_end + 1):
                form_types_by_page.setdefault(seg.form_type, []).append(page + 1)  # 1-indexed

        neubus_doc.form_types_by_page = form_types_by_page
        neubus_doc.classification_status = "complete"
        neubus_doc.save(update_fields=["form_types_by_page", "classification_status"])

    # Convert to FormGroup for backward compatibility
    form_groups = []
    for seg in segments:
        form_groups.append(FormGroup(
            form_type=seg.form_type,
            pages=list(range(seg.page_start, seg.page_end + 1)),
            confidence=seg.confidence,
        ))

    return form_groups
